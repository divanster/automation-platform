"""DB-free tests of the guarded transport (``config/ai_guarded_transport.py``) with a FAKE resolver and a FAKE connection factory: the resolved-address rules, plain
http for loopback only, connection to the validated address, an explicit proxy only, no redirect, timeouts, the response cap and fixed-kind failures.
No name is resolved and no socket is opened by these tests (the two ``socket`` uses are patched).
"""

import http.client
import os
import socket
import ssl
import unittest
from unittest import mock

from apps.ai_gateway import AIGatewayError
from apps.ai_gateway.network import NetworkClass
from apps.ai_gateway.transport import MAX_RESPONSE_BYTES, HttpRequest, HttpResponse, HttpTransport, TransportError
from config import ai_guarded_transport
from config.ai_guarded_transport import GuardedHttpTransport
from config.ai_http_transport import StdlibHttpsTransport
from tests.ai_gateway_support import PROMPT_SENTINEL, SECRET_SENTINEL

L, I, P = NetworkClass.LOOPBACK, NetworkClass.INTERNAL, NetworkClass.PUBLIC


class FakeSocket:
    def __init__(self, log):
        self.log = log

    def settimeout(self, value):
        self.log.append(("settimeout", value))


class FakeResponse:
    def __init__(self, status, data):
        self.status, self.data = status, data

    def read(self, amount=None):
        return self.data if amount is None else self.data[:amount]


class FakeConnection:
    def __init__(self, log, script, scheme, host, port, ip, timeout, context):
        self.log, self.script = log, script
        self.scheme, self.host, self.port, self.ip, self.timeout, self.context = scheme, host, port, ip, timeout, context
        self.sock = None
        log.append(("create", scheme, host, port, ip, timeout))

    def connect(self):
        self.log.append(("connect", self.ip))
        failing = self.script.get("connect_fail", {})
        if self.ip in failing:
            raise failing[self.ip]
        self._maybe_raise("connect")
        self.sock = FakeSocket(self.log)

    def request(self, method, target, body=None, headers=None):
        self.log.append(("request", method, target, body, headers))
        self._maybe_raise("request")

    def getresponse(self):
        self.log.append(("getresponse",))
        self._maybe_raise("getresponse")
        return FakeResponse(*self.script.get("response", (200, b"{}")))

    def close(self):
        self.log.append(("close", self.ip))

    def _maybe_raise(self, where):
        if where in self.script:
            raise self.script[where]


class FakeProxyConnection(FakeConnection):
    def set_tunnel(self, host, port, headers=None):
        self.log.append(("tunnel", host, port, headers))


def make_transport(resolutions=None, script=None, **kwargs):
    """``resolutions`` maps a host name to the addresses the fake resolver returns (a list) or raises (an exception)."""
    log, resolved, created = [], [], []
    table = resolutions or {}

    def resolver(host, port):
        resolved.append((host, port))
        answer = table[host]
        if isinstance(answer, BaseException):
            raise answer
        return list(answer)

    def factory(scheme, host, port, ip, timeout, context):
        connection = FakeConnection(log, script or {}, scheme, host, port, ip, timeout, context)
        created.append(connection)
        return connection

    transport = GuardedHttpTransport(resolver=resolver, connection_factory=factory, **kwargs)
    transport.log, transport.resolved, transport.created = log, resolved, created
    return transport


COMPANY_NETWORKS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fd12::/16")


def make_request(url="https://llm.corp.example.invalid:443/v1/chat/completions", network_class=I, connect=10.0, read=60.0, approved_networks=None):
    headers = (("Content-Type", "application/json"), ("Authorization", f"Bearer {SECRET_SENTINEL}"))
    if approved_networks is None:
        approved_networks = COMPANY_NETWORKS if network_class is I else ()
    return HttpRequest(url, headers, b'{"messages": []}', connect, read, network_class, approved_networks)


CORP = "llm.corp.example.invalid"


class DestinationTests(unittest.TestCase):
    def refused(self, transport, request):
        with self.assertRaises(TransportError) as ctx:
            transport.send(request)
        self.assertEqual(ctx.exception.kind, "destination")
        self.assertEqual(transport.created, [])  # nothing was connected, so nothing was sent

    def test_it_is_an_http_transport_and_resolves_and_connects_to_nothing_when_built(self):
        transport = make_transport()
        self.assertIsInstance(transport, HttpTransport)
        self.assertEqual((transport.log, transport.resolved, transport.created), ([], [], []))

    def test_a_request_without_a_network_class_is_refused_before_anything_happens(self):
        for value in (None, "internal", 1):
            with self.subTest(value=value):
                transport = make_transport({CORP: ["10.1.1.1"]})
                self.refused(transport, make_request(network_class=value))
                self.assertEqual(transport.resolved, [])

    def test_plain_http_is_allowed_for_loopback_only(self):
        transport = make_transport()
        transport.send(make_request("http://127.0.0.1:11434/v1/chat/completions", L))
        self.assertEqual(transport.created[0].scheme, "http")
        for network_class in (I, P):
            with self.subTest(network_class=network_class):
                self.refused(make_transport({CORP: ["10.1.1.1"]}), make_request(f"http://{CORP}/v1/chat/completions", network_class))

    def test_odd_schemes_credentials_and_fragments_are_refused(self):
        for url in ("ftp://h.example.invalid/x", "file:///etc/passwd", f"https://user:{SECRET_SENTINEL}@{CORP}/v1", f"https://user@{CORP}/v1", f"https://{CORP}/v1#frag", "https:///v1", "gopher://h/"):
            with self.subTest(url=url):
                self.refused(make_transport({CORP: ["10.1.1.1"]}), make_request(url))

    def test_a_loopback_host_name_must_resolve_to_loopback_only(self):
        transport = make_transport({"localhost": ["127.0.0.1", "::1"]})
        transport.send(make_request("http://localhost:8000/v1/chat/completions", L))
        self.assertEqual({c.ip for c in transport.created}, {"127.0.0.1"})  # the first address that connects
        for answer in (["10.0.0.1"], ["127.0.0.1", "10.0.0.1"], ["8.8.8.8"]):
            with self.subTest(answer=answer):
                self.refused(make_transport({"localhost": answer}), make_request("http://localhost:8000/v1/chat/completions", L))

    def test_an_internal_endpoint_may_resolve_only_inside_the_approved_ranges(self):
        for answer in (["10.1.2.3"], ["192.168.0.5", "172.20.0.1"], ["fd12::5"], ["::ffff:10.0.0.1"], ["10.1.2.3", "10.1.2.3"]):
            with self.subTest(answer=answer):
                transport = make_transport({CORP: answer})
                self.assertEqual(transport.send(make_request()), HttpResponse(200, b"{}"))

    def test_an_internal_endpoint_is_refused_an_address_outside_the_approved_ranges_even_a_public_one(self):
        for answer in (["8.8.8.8"], ["93.184.216.34"], ["2606:4700:4700::1111"], ["11.0.0.1"], ["172.32.0.1"], ["10.1.2.3", "8.8.8.8"], ["8.8.8.8", "10.1.2.3"], ["fd00::1"]):
            with self.subTest(answer=answer):
                self.refused(make_transport({CORP: answer}), make_request())

    def test_an_internal_endpoint_with_no_approved_ranges_reaches_nothing(self):
        for approved in ((), None):
            with self.subTest(approved=approved):
                transport = make_transport({CORP: ["10.1.2.3"]})
                request = make_request()
                request = HttpRequest(request.url, request.headers, request.body, 1.0, 2.0, I, () if approved is None else approved)
                self.refused(transport, request)

    def test_only_the_approved_ranges_of_the_request_count_not_a_wider_list_elsewhere(self):
        narrow = make_request(approved_networks=("10.20.0.0/16",))
        self.refused(make_transport({CORP: ["10.30.0.1"]}), narrow)
        make_transport({CORP: ["10.20.5.5"]}).send(narrow)

    def test_an_internal_endpoint_must_not_resolve_to_a_dangerous_address_even_inside_an_approved_range(self):
        wide = make_request(approved_networks=("0.0.0.0/1", "128.0.0.0/1", "::/1", "8000::/1"))  # not a list the adapter could build; the transport still holds the line
        for answer in (["127.0.0.1"], ["::1"], ["169.254.169.254"], ["0.0.0.0"], ["224.0.0.1"], ["::ffff:127.0.0.1"], ["64:ff9b::7f00:1"], ["10.0.0.1", "127.0.0.1"], ["fe80::1"], ["not-an-ip"]):
            with self.subTest(answer=answer):
                self.refused(make_transport({CORP: answer}), wide)

    def test_special_purpose_and_metadata_destinations_are_refused_for_every_remote_class(self):
        special = ("0.1.2.3", "169.254.169.254", "169.254.170.2", "198.18.0.1", "192.0.0.192", "192.88.99.1", "168.63.129.16", "100.100.100.200", "2001:db8::1", "fd00:ec2::254", "64:ff9b::a00:1",
                   "2002:a00:1::", "2001:0:4136:e378:8000:63bf:3fff:fdd2", "::ffff:169.254.169.254")  # fmt: skip
        wide = ("0.0.0.0/1", "128.0.0.0/1", "::/1", "8000::/1")
        for ip in special:
            with self.subTest(ip=ip, network_class="internal"):
                self.refused(make_transport({CORP: [ip]}), make_request(approved_networks=wide))
            with self.subTest(ip=ip, network_class="public"):
                self.refused(make_transport({"api.vendor.example.invalid": [ip]}), make_request("https://api.vendor.example.invalid:443/v1/chat/completions", P))
            with self.subTest(ip=ip, network_class="literal"):
                transport = make_transport()
                self.refused(transport, make_request(f"https://{'[' + ip + ']' if ':' in ip else ip}:443/x", I, approved_networks=wide))
                self.assertEqual(transport.resolved, [])

    def test_a_public_endpoint_must_resolve_to_global_addresses_only(self):
        url = "https://api.vendor.example.invalid:443/v1/chat/completions"
        transport = make_transport({"api.vendor.example.invalid": ["8.8.8.8", "93.184.216.34"]})
        transport.send(make_request(url, P))
        for answer in (["10.0.0.1"], ["8.8.8.8", "10.0.0.1"], ["127.0.0.1"], ["100.64.0.1"], ["169.254.169.254"], ["fd00::1"]):
            with self.subTest(answer=answer):
                self.refused(make_transport({"api.vendor.example.invalid": answer}), make_request(url, P))

    def test_a_literal_address_is_checked_as_it_is_and_never_resolved(self):
        for url, network_class in (("https://127.0.0.1:443/x", I), ("https://169.254.169.254:443/x", P), ("https://10.0.0.5:443/x", P), ("https://[::1]:443/x", I), ("https://8.8.8.8:443/x", I)):
            with self.subTest(url=url):
                transport = make_transport()
                self.refused(transport, make_request(url, network_class))
                self.assertEqual(transport.resolved, [])
        transport = make_transport()
        transport.send(make_request("https://10.0.0.5:8443/x", I))
        self.assertEqual((transport.resolved, transport.created[0].ip), ([], "10.0.0.5"))

    def test_a_failed_or_empty_resolution_is_a_connect_failure_not_a_connection(self):
        for answer in (socket.gaierror(8, "nodename nor servname"), OSError("x"), UnicodeError("x"), []):
            with self.subTest(answer=repr(answer)):
                transport = make_transport({CORP: answer})
                with self.assertRaises(TransportError) as ctx:
                    transport.send(make_request())
                self.assertEqual(ctx.exception.kind, "connect")
                self.assertEqual(transport.created, [])

    def test_the_connection_is_made_to_the_validated_address_and_the_name_is_looked_up_once(self):
        transport = make_transport({CORP: ["10.1.2.3"]})
        transport.send(make_request())
        self.assertEqual(transport.resolved, [(CORP, 443)])
        (connection,) = transport.created
        self.assertEqual((connection.host, connection.ip, connection.port), (CORP, "10.1.2.3", 443))  # the name stays for Host and TLS; the socket goes to the address

    def test_dns_rebinding_cannot_swap_the_destination_after_the_check(self):
        # A hostile resolver answers an approved address the first time and a forbidden one afterwards (what a rebinding attacker does between check and use).
        # Every address that is connected to is the one that was validated, and the later forbidden answer is refused outright.
        answers = [["10.1.2.3"], ["169.254.169.254"], ["10.1.2.3"], ["127.0.0.1"]]
        log, created = [], []

        def resolver(host, port):
            return answers.pop(0)

        def factory(scheme, host, port, ip, timeout, context):
            connection = FakeConnection(log, {}, scheme, host, port, ip, timeout, context)
            created.append(connection)
            return connection

        transport = GuardedHttpTransport(resolver=resolver, connection_factory=factory)
        transport.send(make_request())
        with self.assertRaises(TransportError) as ctx:
            transport.send(make_request())
        self.assertEqual(ctx.exception.kind, "destination")
        transport.send(make_request())
        with self.assertRaises(TransportError):
            transport.send(make_request())
        self.assertEqual([c.ip for c in created], ["10.1.2.3", "10.1.2.3"])  # nothing was ever dialled except a validated address
        self.assertEqual(answers, [])  # exactly one lookup per call: the socket layer is never asked to resolve the name again

    def test_a_mixed_answer_is_refused_whole_even_when_the_first_address_is_fine(self):
        transport = make_transport({CORP: ["10.1.2.3", "169.254.169.254"]})
        with self.assertRaises(TransportError) as ctx:
            transport.send(make_request())
        self.assertEqual((ctx.exception.kind, transport.created), ("destination", []))

    def test_it_fails_over_to_the_next_validated_address_on_a_connection_error_only(self):
        script = {"connect_fail": {"10.0.0.1": ConnectionRefusedError()}}
        transport = make_transport({CORP: ["10.0.0.1", "10.0.0.2"]}, script)
        transport.send(make_request())
        self.assertEqual([entry[1] for entry in transport.log if entry[0] == "connect"], ["10.0.0.1", "10.0.0.2"])
        script = {"connect_fail": {"10.0.0.1": ssl.SSLCertVerificationError("bad")}}
        transport = make_transport({CORP: ["10.0.0.1", "10.0.0.2"]}, script)
        with self.assertRaises(TransportError) as ctx:
            transport.send(make_request())
        self.assertEqual((ctx.exception.kind, [e[1] for e in transport.log if e[0] == "connect"]), ("tls", ["10.0.0.1"]))

    def test_at_most_a_few_addresses_are_tried(self):
        addresses = [f"10.0.0.{n}" for n in range(1, 10)]
        failing = {ip: ConnectionRefusedError() for ip in addresses}
        transport = make_transport({CORP: addresses}, {"connect_fail": failing})
        with self.assertRaises(TransportError) as ctx:
            transport.send(make_request())
        self.assertEqual((ctx.exception.kind, len(transport.created)), ("connect", ai_guarded_transport.MAX_ADDRESSES))


class ExchangeTests(unittest.TestCase):
    def test_a_request_is_one_post_with_both_timeouts_and_the_given_headers(self):
        transport = make_transport({CORP: ["10.1.2.3"]}, {"response": (200, b'{"ok": true}')})
        response = transport.send(make_request(connect=7.0, read=55.0))
        self.assertEqual(response, HttpResponse(200, b'{"ok": true}'))
        self.assertEqual([entry[0] for entry in transport.log], ["create", "connect", "settimeout", "request", "getresponse", "close"])
        self.assertEqual(transport.log[0][-1], 7.0)
        self.assertIn(("settimeout", 55.0), transport.log)
        _, method, target, body, headers = next(e for e in transport.log if e[0] == "request")
        self.assertEqual((method, target, body), ("POST", "/v1/chat/completions", b'{"messages": []}'))
        self.assertEqual(headers["Authorization"], f"Bearer {SECRET_SENTINEL}")

    def test_the_context_verifies_certificates_and_host_names(self):
        transport = make_transport({CORP: ["10.1.2.3"]})
        transport.send(make_request())
        context = transport.created[0].context
        self.assertEqual((context.verify_mode, context.check_hostname), (ssl.CERT_REQUIRED, True))

    def test_a_redirect_is_returned_as_an_answer_and_never_followed(self):
        transport = make_transport({CORP: ["10.1.2.3"]}, {"response": (302, b"")})
        self.assertEqual(transport.send(make_request()).status, 302)
        self.assertEqual(len(transport.created), 1)
        self.assertEqual(len([e for e in transport.log if e[0] == "request"]), 1)

    def test_failures_become_fixed_kinds_and_the_connection_is_closed(self):
        cases = [
            ({"connect": TimeoutError()}, "connect"), ({"connect": ConnectionRefusedError()}, "connect"), ({"connect": ssl.SSLError("x")}, "tls"),
            ({"request": TimeoutError()}, "timeout"), ({"getresponse": TimeoutError()}, "timeout"), ({"getresponse": ConnectionResetError()}, "protocol"),
            ({"getresponse": http.client.RemoteDisconnected("x")}, "protocol"), ({"request": ssl.SSLError("x")}, "tls"),
        ]  # fmt: skip
        for script, kind in cases:
            with self.subTest(script=list(script), kind=kind):
                transport = make_transport({CORP: ["10.1.2.3"]}, script)
                with self.assertRaises(TransportError) as ctx:
                    transport.send(make_request())
                self.assertEqual(ctx.exception.kind, kind)
                self.assertIsNone(ctx.exception.__cause__)
                self.assertTrue(ctx.exception.__suppress_context__ or ctx.exception.__context__ is None)
                self.assertIn("close", [e[0] for e in transport.log])

    def test_the_response_is_capped(self):
        transport = make_transport({CORP: ["10.1.2.3"]}, {"response": (200, b"x" * (MAX_RESPONSE_BYTES + 5))})
        with self.assertRaises(TransportError) as ctx:
            transport.send(make_request())
        self.assertEqual(ctx.exception.kind, "protocol")
        transport = make_transport({CORP: ["10.1.2.3"]}, {"response": (200, b"x" * MAX_RESPONSE_BYTES)})
        self.assertEqual(len(transport.send(make_request()).body), MAX_RESPONSE_BYTES)

    def test_a_lower_cap_can_be_asked_for_and_a_higher_or_invalid_one_never(self):
        transport = make_transport({CORP: ["10.1.2.3"]}, {"response": (200, b"x" * 1025)}, max_response_bytes=1024)
        with self.assertRaises(TransportError) as ctx:
            transport.send(make_request())
        self.assertEqual(ctx.exception.kind, "protocol")
        transport = make_transport({CORP: ["10.1.2.3"]}, {"response": (200, b"x" * 1024)}, max_response_bytes=1024)
        self.assertEqual(len(transport.send(make_request()).body), 1024)
        for bad in (0, -1, MAX_RESPONSE_BYTES + 1, True, "10", 1.5):
            with self.subTest(bad), self.assertRaises(AIGatewayError):
                make_transport(max_response_bytes=bad)

    def test_nothing_secret_is_in_a_repr_or_an_error(self):
        transport = make_transport({CORP: ["127.0.0.1"]})
        with self.assertRaises(TransportError) as ctx:
            transport.send(make_request())
        for text in (repr(transport), str(ctx.exception), repr(ctx.exception), repr(make_request())):
            for hidden in (SECRET_SENTINEL, CORP, "127.0.0.1", PROMPT_SENTINEL):
                self.assertNotIn(hidden, text)


class ProxyTests(unittest.TestCase):
    def make(self, resolutions=None, script=None, proxy="http://proxy.corp.example.invalid:3128"):
        log, created, resolved = [], [], []

        def resolver(host, port):
            resolved.append(host)
            return (resolutions or {})[host]

        def direct(scheme, host, port, ip, timeout, context):
            raise AssertionError("a proxied request must not connect directly")

        transport = GuardedHttpTransport(proxy_url=proxy, resolver=resolver, connection_factory=direct)

        def proxy_factory(host, port, timeout=None, context=None):
            connection = FakeProxyConnection(log, script or {}, "https", host, port, host, timeout, context)
            created.append(connection)
            return connection

        patcher = mock.patch.object(ai_guarded_transport.http.client, "HTTPSConnection", proxy_factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        transport.log, transport.created, transport.resolved = log, created, resolved
        return transport

    def test_a_host_name_is_refused_through_a_proxy_for_every_class_and_nothing_is_resolved_or_connected(self):
        cases = ((CORP, I, ["10.1.2.3"]), ("api.vendor.example.invalid", P, ["8.8.8.8"]), ("localhost", L, ["127.0.0.1"]))
        for host, network_class, answer in cases:
            scheme = "http" if network_class is L else "https"
            with self.subTest(host=host):
                transport = self.make({host: answer})
                request = make_request(f"{scheme}://{host}:443/v1/chat/completions", network_class)
                with self.assertRaises(TransportError) as ctx:
                    transport.send(request)
                self.assertEqual(ctx.exception.kind, "destination")
                self.assertEqual((transport.resolved, transport.created, transport.log), ([], [], []))  # no lookup, no connection, no tunnel, nothing sent
                for text in (str(ctx.exception), repr(ctx.exception), repr(transport)):
                    self.assertNotIn(SECRET_SENTINEL, text)
                    self.assertNotIn(host, text)

    def test_an_address_literal_inside_the_approved_range_goes_through_the_proxy_as_a_tunnel(self):
        transport = self.make()
        transport.send(make_request("https://10.0.0.5:8443/v1/chat/completions", I))
        self.assertEqual(transport.resolved, [])
        (connection,) = transport.created
        self.assertEqual((connection.host, connection.port), ("proxy.corp.example.invalid", 3128))
        self.assertIn(("tunnel", "10.0.0.5", 8443, {}), transport.log)

    def test_an_address_literal_outside_the_approved_range_or_special_purpose_is_refused_before_the_proxy(self):
        for url, network_class in (("https://169.254.169.254:443/x", I), ("https://8.8.8.8:443/x", I), ("https://10.0.0.5:443/x", P), ("https://[fd00:ec2::254]:443/x", I), ("https://168.63.129.16:443/x", P)):
            with self.subTest(url=url):
                transport = self.make()
                with self.assertRaises(TransportError) as ctx:
                    transport.send(make_request(url, network_class))
                self.assertEqual((ctx.exception.kind, transport.created), ("destination", []))

    def test_a_loopback_request_is_refused_while_a_proxy_is_configured(self):
        transport = self.make()
        for url in ("http://127.0.0.1:11434/v1/chat/completions", "http://localhost:11434/v1/chat/completions"):
            with self.subTest(url=url), self.assertRaises(TransportError) as ctx:
                transport.send(make_request(url, L))
            self.assertEqual(ctx.exception.kind, "destination")
        self.assertEqual(transport.created, [])

    def test_proxy_credentials_become_a_proxy_authorization_header_and_stay_out_of_the_repr(self):
        transport = self.make(proxy="http://user:" + SECRET_SENTINEL + "@proxy.corp.example.invalid:3128")
        transport.send(make_request("https://10.0.0.5:8443/x", I))
        tunnel = next(e for e in transport.log if e[0] == "tunnel")
        self.assertIn("Proxy-Authorization", tunnel[3])
        self.assertNotIn(SECRET_SENTINEL, repr(transport))

    def test_an_invalid_proxy_url_is_a_configuration_error_that_does_not_echo_it(self):
        from apps.ai_gateway import AIGatewayError

        for url in ("https://proxy.invalid:3128", "socks5://proxy.invalid", "http://", f"http://u:{SECRET_SENTINEL}@proxy.invalid:99999"):
            with self.subTest(url=url), self.assertRaises(AIGatewayError) as ctx:
                GuardedHttpTransport(proxy_url=url)
            self.assertNotIn(SECRET_SENTINEL, str(ctx.exception))

    def test_the_proxy_environment_variables_are_never_read(self):
        env = {"HTTPS_PROXY": "http://env-proxy.invalid:1", "HTTP_PROXY": "http://env-proxy.invalid:1", "https_proxy": "http://env-proxy.invalid:1", "NO_PROXY": "*"}
        with mock.patch.dict(os.environ, env):
            transport = make_transport({CORP: ["10.1.2.3"]})
            transport.send(make_request())
        self.assertEqual(transport.created[0].ip, "10.1.2.3")  # a direct, pinned connection
        self.assertEqual(repr(transport), "GuardedHttpTransport(proxy=no)")


class PinnedConnectionTests(unittest.TestCase):
    """The real connection classes connect to the validated address, never to the name. ``create_connection`` is patched: no socket is opened."""

    def test_the_plain_connection_dials_the_pinned_address(self):
        calls = []
        with mock.patch.object(socket, "create_connection", side_effect=lambda address, timeout: calls.append((address, timeout)) or (_ for _ in ()).throw(ConnectionRefusedError())):
            connection = ai_guarded_transport.open_connection("http", "localhost", 11434, "127.0.0.1", 5.0, None)
            with self.assertRaises(ConnectionRefusedError):
                connection.connect()
        self.assertEqual(calls, [(("127.0.0.1", 11434), 5.0)])
        self.assertEqual(connection.host, "localhost")

    def test_the_tls_connection_dials_the_pinned_address_and_verifies_the_original_name(self):
        calls, wrapped = [], []
        fake_socket = mock.Mock()
        context = mock.Mock()
        context.wrap_socket.side_effect = lambda sock, server_hostname: wrapped.append(server_hostname) or sock
        with mock.patch.object(socket, "create_connection", side_effect=lambda address, timeout: calls.append(address) or fake_socket):
            connection = ai_guarded_transport.open_connection("https", CORP, 443, "10.1.2.3", 5.0, context)
            connection.connect()
        self.assertEqual((calls, wrapped), ([("10.1.2.3", 443)], [CORP]))

    def test_a_failed_handshake_closes_the_raw_socket(self):
        fake_socket = mock.Mock()
        context = mock.Mock()
        context.wrap_socket.side_effect = ssl.SSLError("handshake")
        with mock.patch.object(socket, "create_connection", return_value=fake_socket):
            connection = ai_guarded_transport.open_connection("https", CORP, 443, "10.1.2.3", 5.0, context)
            with self.assertRaises(ssl.SSLError):
                connection.connect()
        fake_socket.close.assert_called_once()

    def test_the_system_resolver_returns_the_addresses_of_the_answer_as_text(self):
        info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 443)), (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd00::1", 443, 0, 0))]
        with mock.patch.object(socket, "getaddrinfo", return_value=info) as lookup:
            self.assertEqual(ai_guarded_transport.system_resolver(CORP, 443), ["10.1.2.3", "fd00::1"])
        lookup.assert_called_once()


class WorkbenchTransportRegressionTests(unittest.TestCase):
    def test_the_workbench_transport_refuses_a_request_that_needs_address_rules_it_cannot_enforce(self):
        created = []
        transport = StdlibHttpsTransport(connection_factory=lambda *a, **k: created.append(1))
        for network_class in (L, I, P):
            with self.subTest(network_class=network_class), self.assertRaises(TransportError) as ctx:
                transport.send(HttpRequest("https://api.workbench.kpmg/x", (), b"", 1.0, 2.0, network_class))
            self.assertEqual(ctx.exception.kind, "destination")
        self.assertEqual(created, [])

    def test_a_request_without_a_class_is_unchanged_for_the_workbench_transport(self):
        request = HttpRequest("https://api.workbench.kpmg/x", (), b"", 1.0, 2.0)
        self.assertIsNone(request.network_class)


if __name__ == "__main__":
    unittest.main()
