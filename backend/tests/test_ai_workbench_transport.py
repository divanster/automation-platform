"""DB-free tests of the real HTTPS transport (``config/ai_http_transport.py``) with a FAKE ``http.client`` connection: TLS verification, explicit
proxy only, connect and read timeouts, no redirect, response cap, fixed-kind failures and no leakage. No socket is opened.
"""

import base64
import http.client
import inspect
import os
import ssl
import unittest
from types import SimpleNamespace
from unittest import mock

from apps.ai_gateway import AIGatewayError
from apps.ai_gateway.transport import MAX_RESPONSE_BYTES, HttpRequest, HttpResponse, HttpTransport, TransportError
from config import ai_http_transport
from config.ai_http_transport import StdlibHttpsTransport, build_ssl_context
from tests.ai_gateway_support import PROMPT_SENTINEL, SECRET_SENTINEL

URL = "https://api.workbench.kpmg/genai/azure/openai/deployments/d1/chat/completions?api-version=2024-10-21"


class FakeSocket:
    def __init__(self, log):
        self.log = log

    def settimeout(self, value):
        self.log.append(("settimeout", value))


class FakeResponse:
    def __init__(self, status, data):
        self.status = status
        self.data = data

    def read(self, amount=None):
        return self.data if amount is None else self.data[:amount]


class FakeConnection:
    """Records what the transport does with it. ``script`` names where a failure is raised and what is raised."""

    def __init__(self, log, script, host, port, timeout=None, context=None):
        self.log, self.script = log, script
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.sock = None
        log.append(("create", host, port, timeout))

    def set_tunnel(self, host, port, headers=None):
        self.log.append(("tunnel", host, port, headers))

    def connect(self):
        self.log.append(("connect",))
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
        self.log.append(("close",))

    def _maybe_raise(self, where):
        if where in self.script:
            raise self.script[where]


def make_transport(script=None, **kwargs):
    log = []
    created = []

    def factory(host, port, timeout=None, context=None):
        connection = FakeConnection(log, script or {}, host, port, timeout, context)
        created.append(connection)
        return connection

    return StdlibHttpsTransport(connection_factory=factory, **kwargs), log, created


def make_request(url=URL, connect=10.0, read=90.0):
    return HttpRequest(url, (("Content-Type", "application/json"), ("X-Test-Subscription-Key", SECRET_SENTINEL)), b'{"messages": []}', connect, read)


class DirectConnectionTests(unittest.TestCase):
    def test_it_is_an_http_transport_and_makes_no_connection_when_built(self):
        transport, log, created = make_transport()
        self.assertIsInstance(transport, HttpTransport)
        self.assertEqual((log, created), ([], []))

    def test_a_request_is_one_verified_tls_connection_with_both_timeouts_set_explicitly(self):
        transport, log, created = make_transport({"response": (200, b'{"ok": true}')})
        response = transport.send(make_request(connect=7.0, read=55.0))
        self.assertEqual(response, HttpResponse(200, b'{"ok": true}'))
        (connection,) = created
        self.assertEqual(log[0], ("create", "api.workbench.kpmg", 443, 7.0))  # connect timeout: TCP and handshake
        self.assertIn(("settimeout", 55.0), log)  # read timeout from the connected socket on
        self.assertLess(log.index(("connect",)), log.index(("settimeout", 55.0)))
        self.assertEqual([entry[0] for entry in log], ["create", "connect", "settimeout", "request", "getresponse", "close"])
        self.assertEqual(connection.timeout, 7.0)

    def test_tls_verification_is_on_for_the_context_the_connection_gets(self):
        transport, _, created = make_transport()
        transport.send(make_request())
        context = created[0].context
        self.assertEqual((context.verify_mode, context.check_hostname), (ssl.CERT_REQUIRED, True))
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_there_is_no_way_to_switch_verification_off(self):
        self.assertEqual(set(inspect.signature(StdlibHttpsTransport.__init__).parameters) - {"self"}, {"proxy_url", "ca_bundle", "connection_factory", "max_response_bytes"})  # the one added knob can only LOWER the response cap
        self.assertEqual((build_ssl_context().verify_mode, build_ssl_context().check_hostname), (ssl.CERT_REQUIRED, True))
        source = inspect.getsource(ai_http_transport)
        for forbidden in ("_create_unverified_context", "CERT_NONE", "check_hostname = False", "verify_mode ="):
            self.assertNotIn(forbidden, source)

    def test_tls_session_secrets_are_never_written_even_if_the_environment_asks(self):
        with mock.patch.dict(os.environ, {"SSLKEYLOGFILE": "C:/temp/keys-SENTINEL.log"}):
            self.assertIsNone(build_ssl_context().keylog_filename)
            transport, _, created = make_transport()
            transport.send(make_request())
        self.assertIsNone(created[0].context.keylog_filename)

    def test_a_context_without_verification_is_refused(self):
        weak = SimpleNamespace(verify_mode=ssl.CERT_NONE, check_hostname=False, keylog_filename=None, load_verify_locations=lambda **_: None)
        with mock.patch.object(ssl, "create_default_context", return_value=weak), self.assertRaises(AIGatewayError) as ctx:
            build_ssl_context()
        self.assertEqual(ctx.exception.reason, "tls_verification_disabled")

    def test_the_request_is_a_post_of_the_exact_target_body_and_headers(self):
        transport, log, _ = make_transport()
        transport.send(make_request())
        request = next(entry for entry in log if entry[0] == "request")
        self.assertEqual(request[1:3], ("POST", "/genai/azure/openai/deployments/d1/chat/completions?api-version=2024-10-21"))
        self.assertEqual(request[3], b'{"messages": []}')
        self.assertEqual(request[4], {"Content-Type": "application/json", "X-Test-Subscription-Key": SECRET_SENTINEL})

    def test_an_explicit_port_is_used(self):
        transport, log, _ = make_transport()
        transport.send(make_request("https://example.invalid:8443/x"))
        self.assertEqual(log[0], ("create", "example.invalid", 8443, 10.0))

    def test_a_non_https_url_is_refused_before_any_connection(self):
        for url in ("http://api.workbench.kpmg/x", "ftp://host/x", "https:///x", "https://host:notaport/x", "/relative"):
            transport, log, created = make_transport()
            with self.subTest(url), self.assertRaises(TransportError) as ctx:
                transport.send(make_request(url))
            self.assertEqual((ctx.exception.kind, created), ("protocol", []))

    def test_a_redirect_is_returned_not_followed(self):
        transport, log, created = make_transport({"response": (302, b"")})
        self.assertEqual(transport.send(make_request()).status, 302)
        self.assertEqual(len(created), 1)  # no second connection to the Location

    def test_error_statuses_are_returned_for_the_adapter_to_classify(self):
        for code in (401, 429, 500):
            transport, _, _ = make_transport({"response": (code, b"{}")})
            self.assertEqual(transport.send(make_request()).status, code)

    def test_the_connection_is_always_closed(self):
        for where in ("connect", "request", "getresponse"):
            transport, log, _ = make_transport({where: OSError("boom")})
            with self.subTest(where), self.assertRaises(TransportError):
                transport.send(make_request())
            self.assertEqual(log[-1], ("close",))

    def test_a_response_over_the_cap_is_refused_and_the_cap_is_what_is_read(self):
        transport, _, _ = make_transport({"response": (200, b"x" * (MAX_RESPONSE_BYTES + 1))})
        with self.assertRaises(TransportError) as ctx:
            transport.send(make_request())
        self.assertEqual(ctx.exception.kind, "protocol")
        transport, _, _ = make_transport({"response": (200, b"x" * MAX_RESPONSE_BYTES)})
        self.assertEqual(len(transport.send(make_request()).body), MAX_RESPONSE_BYTES)

    def test_a_lower_cap_can_be_asked_for_and_a_higher_or_invalid_one_never(self):
        transport, _, _ = make_transport({"response": (200, b"x" * 1025)}, max_response_bytes=1024)
        with self.assertRaises(TransportError) as ctx:
            transport.send(make_request())
        self.assertEqual(ctx.exception.kind, "protocol")
        transport, _, _ = make_transport({"response": (200, b"x" * 1024)}, max_response_bytes=1024)
        self.assertEqual(len(transport.send(make_request()).body), 1024)
        for bad in (0, -1, MAX_RESPONSE_BYTES + 1, True, "10", 1.5):
            with self.subTest(bad), self.assertRaises(AIGatewayError):
                make_transport(max_response_bytes=bad)


class FailureMappingTests(unittest.TestCase):
    def kind(self, where, exception):
        transport, _, _ = make_transport({where: exception})
        with self.assertRaises(TransportError) as ctx:
            transport.send(make_request())
        return ctx.exception

    def test_failures_map_to_fixed_kinds(self):
        table = [
            ("connect", TimeoutError("t"), "connect"),
            ("connect", ConnectionRefusedError("r"), "connect"),
            ("connect", OSError("dns"), "connect"),
            ("connect", ssl.SSLCertVerificationError("bad cert"), "tls"),
            ("connect", ssl.SSLError("handshake"), "tls"),
            ("request", TimeoutError("t"), "timeout"),
            ("request", ConnectionResetError("reset"), "protocol"),
            ("getresponse", TimeoutError("read timed out"), "timeout"),
            ("getresponse", ConnectionResetError("reset"), "protocol"),
            ("getresponse", http.client.BadStatusLine("x"), "protocol"),
            ("getresponse", http.client.RemoteDisconnected("x"), "protocol"),
            ("getresponse", ssl.SSLError("read"), "tls"),
        ]  # fmt: skip
        for where, exception, kind in table:
            with self.subTest(where=where, error=type(exception).__name__):
                self.assertEqual(self.kind(where, exception).kind, kind)

    def test_a_timeout_before_the_connection_is_a_connect_failure_and_after_it_a_timeout(self):
        self.assertEqual(self.kind("connect", TimeoutError()).kind, "connect")
        self.assertEqual(self.kind("getresponse", TimeoutError()).kind, "timeout")

    def test_an_underlying_exception_that_names_everything_leaks_nothing_and_is_not_chained(self):
        text = f"{SECRET_SENTINEL} {PROMPT_SENTINEL} api.workbench.kpmg http://user:pw@proxy:8080"
        for where in ("connect", "request", "getresponse"):
            for exception in (OSError(text), TimeoutError(text), ssl.SSLError(text), http.client.HTTPException(text)):
                error = self.kind(where, exception)
                with self.subTest(where=where, error=type(exception).__name__):
                    for fragment in (SECRET_SENTINEL, PROMPT_SENTINEL, "workbench", "proxy", "pw"):
                        self.assertNotIn(fragment, f"{error} {error!r} {error.args}")
                    self.assertIsNone(error.__cause__)
                    self.assertIsNone(error.__context__)

    def test_only_the_fixed_kinds_exist(self):
        with self.assertRaises(ValueError):
            TransportError("made-up")


class ProxyTests(unittest.TestCase):
    def test_without_a_proxy_the_connection_is_direct_and_the_environment_is_ignored(self):
        env = {"HTTPS_PROXY": "http://env-proxy:3128", "https_proxy": "http://env-proxy:3128", "HTTP_PROXY": "http://env-proxy:3128", "ALL_PROXY": "http://env-proxy:3128"}
        with mock.patch.dict(os.environ, env):
            transport, log, _ = make_transport()
            transport.send(make_request())
        self.assertEqual(log[0], ("create", "api.workbench.kpmg", 443, 10.0))
        self.assertFalse([entry for entry in log if entry[0] == "tunnel"])

    def test_an_explicit_proxy_is_a_connect_tunnel_to_the_real_host(self):
        transport, log, _ = make_transport(proxy_url="http://proxy.example.invalid:3128")
        transport.send(make_request())
        self.assertEqual(log[0], ("create", "proxy.example.invalid", 3128, 10.0))
        self.assertEqual(log[1], ("tunnel", "api.workbench.kpmg", 443, {}))
        self.assertLess(log.index(("connect",)), log.index(next(e for e in log if e[0] == "request")))
        self.assertEqual(next(e for e in log if e[0] == "request")[2], "/genai/azure/openai/deployments/d1/chat/completions?api-version=2024-10-21")

    def test_the_proxy_default_port_and_a_trailing_slash(self):
        transport, log, _ = make_transport(proxy_url="http://proxy.example.invalid/")
        transport.send(make_request())
        self.assertEqual(log[0][1:3], ("proxy.example.invalid", 8080))

    def test_proxy_credentials_are_sent_only_as_proxy_authorization_and_never_shown(self):
        transport, log, _ = make_transport(proxy_url="http://us%40er:p%3Aw@proxy.example.invalid:3128")
        transport.send(make_request())
        expected = "Basic " + base64.b64encode(b"us@er:p:w").decode()
        self.assertEqual(log[1][3], {"Proxy-Authorization": expected})
        request = next(e for e in log if e[0] == "request")
        self.assertNotIn("Proxy-Authorization", request[4])  # not sent to the target
        self.assertNotIn("p%3Aw", repr(transport) + str(transport))
        self.assertNotIn("proxy.example.invalid", repr(transport))

    def test_the_environment_cannot_bypass_an_explicit_proxy(self):
        with mock.patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "api.workbench.kpmg,*"}):
            transport, log, _ = make_transport(proxy_url="http://proxy.example.invalid:3128")
            transport.send(make_request())
        self.assertEqual(log[0][1:3], ("proxy.example.invalid", 3128))
        self.assertTrue([e for e in log if e[0] == "tunnel"])

    def test_a_bad_proxy_url_is_refused_without_echoing_it(self):
        for bad in ("https://proxy:3128", "proxy:3128", "ftp://proxy", "http://", "http://proxy:99999", "http://proxy:3128/path", "http://proxy:3128?x=1", "socks5://proxy:1080", ""):
            with self.subTest(bad), self.assertRaises(AIGatewayError) as ctx:
                StdlibHttpsTransport(proxy_url=bad + "#" + SECRET_SENTINEL if bad else bad)
            self.assertEqual((ctx.exception.code, ctx.exception.reason), ("configuration_error", "invalid_proxy_url"))
            self.assertNotIn(SECRET_SENTINEL, f"{ctx.exception} {ctx.exception.args}")


class CaBundleTests(unittest.TestCase):
    def test_a_missing_ca_bundle_is_a_configuration_error_that_does_not_echo_the_path(self):
        with self.assertRaises(AIGatewayError) as ctx:
            StdlibHttpsTransport(ca_bundle="C:/does/not/exist-SENTINEL.pem")
        self.assertEqual((ctx.exception.code, ctx.exception.reason), ("configuration_error", "invalid_ca_bundle"))
        self.assertNotIn("SENTINEL", f"{ctx.exception} {ctx.exception.args}")

    def test_an_extra_ca_bundle_is_added_to_a_verifying_context(self):
        loaded = []
        context = ssl.create_default_context()
        with mock.patch.object(ssl, "create_default_context", return_value=context), mock.patch.object(context, "load_verify_locations", side_effect=lambda **kw: loaded.append(kw)):
            result = build_ssl_context("company-ca.pem")
        self.assertEqual((loaded, result.verify_mode, result.check_hostname), ([{"cafile": "company-ca.pem"}], ssl.CERT_REQUIRED, True))


class NoNetworkTests(unittest.TestCase):
    def test_building_the_transport_opens_no_socket_and_resolves_no_name(self):
        import socket

        def boom(*args, **kwargs):
            raise AssertionError("network access")

        with mock.patch.object(socket, "socket", boom), mock.patch.object(socket, "create_connection", boom), mock.patch.object(socket, "getaddrinfo", boom):
            StdlibHttpsTransport()
            StdlibHttpsTransport(proxy_url="http://proxy.example.invalid:3128")


if __name__ == "__main__":
    unittest.main()
