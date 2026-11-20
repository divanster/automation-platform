"""The guarded HTTP(S) transport of the AI Gateway adapters that declare a ``NetworkClass`` (AI-CON-1). Standard library only.

It lives here, outside the pure ``apps/ai_gateway`` package, because it opens sockets and resolves names. Importing this module, and constructing the
transport, make NO network call: a lookup and a connection happen only inside ``send``.

WHAT IT ENFORCES, in this order, before a single byte of the request is sent:

* The request must carry a ``NetworkClass``; without one it is refused (``destination``). The ``https`` scheme is always allowed; plain ``http`` only for the
  ``loopback`` class. A URL with credentials or a fragment is refused.
* The host is resolved HERE, and EVERY address it resolves to (up to the first few) must be permitted for the class (``apps.ai_gateway.network``): ``loopback``
  reaches 127.0.0.0/8 and ::1 only, ``internal`` reaches ONLY addresses inside the request's ``approved_networks`` (the operator's CIDR ranges; none means nothing),
  and ``public`` reaches global addresses only; for ``internal`` and ``public`` the explicit denylist of special-purpose ranges (metadata, link-local, reserved,
  documentation, IPv6 transition forms) is applied first. One forbidden address in the answer refuses the whole call, so a name that mixes an approved and an
  unapproved address is not trusted. An IP literal is checked as it is.
* The connection is made to one of the VALIDATED addresses, not to the name, so a second lookup by the socket layer cannot return something else (DNS rebinding).
  TLS still verifies the certificate against the original host name; there is NO option to switch verification off.
* No redirect is followed (``http.client`` never does), the response is capped at ``MAX_RESPONSE_BYTES``, ``SSLKEYLOGFILE`` is neutralised, and every failure
  becomes a ``TransportError`` with a fixed kind that carries no host, address, header or body.

PROXY. Only an explicit ``proxy_url`` is used (``HTTPS_PROXY``, ``HTTP_PROXY`` and ``NO_PROXY`` are never read), and ONLY for an IP-literal destination, which is
validated here like any other address. A host NAME is refused (``destination``) while a proxy is configured, because the proxy would resolve it out of this
process's sight and no address rule could be applied; there is no acknowledgement or other bypass. A ``loopback`` request is likewise refused while a proxy is
configured, never silently sent around it.

LIMITS. ``getaddrinfo`` cannot be given a timeout, so name resolution is not bounded by the call deadline. The check protects the destination of THIS connection;
it is not a firewall and does not replace network egress rules.
"""

import http.client
import socket
import ssl
from urllib.parse import urlsplit

from apps.ai_gateway.network import DEFAULT_PORTS, NetworkClass, address_permitted, parse_ip
from apps.ai_gateway.transport import MAX_RESPONSE_BYTES, HttpRequest, HttpResponse, HttpTransport, TransportError

from config.ai_http_transport import _parse_proxy, build_ssl_context

MAX_ADDRESSES = 4  # how many resolved addresses are tried; all of them must have passed the check


def system_resolver(host, port):
    """The IP addresses (as text) the operating system resolves ``host`` to. May raise ``OSError`` (name resolution failed)."""
    return [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Connects to a given, already validated address; the host name stays what ``Host`` and TLS verification use."""

    def __init__(self, host, port, *, ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, *, ip, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_ip = ip

    def connect(self):
        raw = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)  # certificate and host name are verified against the original name
        except BaseException:
            raw.close()
            raise


def open_connection(scheme, host, port, ip, timeout, context):
    if scheme == "http":
        return _PinnedHTTPConnection(host, port, ip=ip, timeout=timeout)
    return _PinnedHTTPSConnection(host, port, ip=ip, timeout=timeout, context=context)


class GuardedHttpTransport(HttpTransport):
    def __init__(self, *, proxy_url=None, ca_bundle=None, resolver=system_resolver, connection_factory=open_connection):
        self._context = build_ssl_context(ca_bundle)
        self._proxy = None if proxy_url is None else _parse_proxy(proxy_url)
        self._resolver = resolver
        self._connection_factory = connection_factory

    def __repr__(self):
        return f"GuardedHttpTransport(proxy={'yes' if self._proxy else 'no'})"

    def send(self, request: HttpRequest) -> HttpResponse:
        network_class = request.network_class
        if not isinstance(network_class, NetworkClass):
            raise TransportError("destination")
        try:
            parts = urlsplit(request.url)
            host, port = parts.hostname, parts.port
            credentials = parts.username is not None or parts.password is not None
        except ValueError:
            raise TransportError("protocol") from None
        scheme = parts.scheme
        if scheme not in DEFAULT_PORTS or not host or credentials or parts.fragment:
            raise TransportError("destination")
        if scheme == "http" and network_class is not NetworkClass.LOOPBACK:  # never send over plain http to anything but this machine
            raise TransportError("destination")
        if self._proxy is not None and network_class is NetworkClass.LOOPBACK:  # never around the proxy, never through it
            raise TransportError("destination")
        if self._proxy is not None and parse_ip(host) is None:  # the proxy would resolve the name where no address rule can see it: no acknowledgement, no bypass
            raise TransportError("destination")
        port = port or DEFAULT_PORTS[scheme]
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        addresses = self._validated_addresses(host, port, network_class, request.approved_networks)

        connection = None
        connected = False
        failure = None
        status = body = None
        try:
            if self._proxy is None:
                connection = self._connect_any(scheme, host, port, addresses, request.connect_timeout_seconds)
            else:
                proxy_host, proxy_port, proxy_headers = self._proxy
                connection = http.client.HTTPSConnection(proxy_host, proxy_port, timeout=request.connect_timeout_seconds, context=self._context)
                connection.set_tunnel(host, port, headers=dict(proxy_headers))
                connection.connect()
            connected = True
            connection.sock.settimeout(request.read_timeout_seconds)
            connection.request("POST", target, body=request.body, headers=dict(request.headers))
            response = connection.getresponse()
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                failure = "protocol"
            else:
                status, body = response.status, data
        except TimeoutError:
            failure = "timeout" if connected else "connect"
        except ssl.SSLError:
            failure = "tls"
        except (OSError, http.client.HTTPException):
            failure = "protocol" if connected else "connect"
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:  # nothing useful to do, and nothing to leak
                    pass
        if failure is not None:
            raise TransportError(failure)  # raised outside the except blocks: the original exception is not this error's context
        return HttpResponse(status, body)

    def _validated_addresses(self, host, port, network_class, approved_networks):
        """The addresses to connect to, every one of them permitted. Raises ``TransportError``. A proxy is only ever used with an IP literal (checked in ``send``)."""
        if parse_ip(host) is not None:
            candidates = [host]
        else:
            try:
                candidates = list(self._resolver(host, port))
            except (OSError, UnicodeError):
                raise TransportError("connect") from None
        unique = list(dict.fromkeys(candidates))
        if not unique:
            raise TransportError("connect")
        if not all(isinstance(a, str) and address_permitted(a, network_class, approved_networks) for a in unique):
            raise TransportError("destination")
        return unique[:MAX_ADDRESSES]

    def _connect_any(self, scheme, host, port, addresses, timeout):
        """A connected connection to the first validated address that answers. Raises the last ``OSError`` when none does."""
        last = None
        for ip in addresses:
            candidate = self._connection_factory(scheme, host, port, ip, timeout, self._context)
            try:
                candidate.connect()
                return candidate
            except ssl.SSLError:
                candidate.close()
                raise  # a certificate problem is not repaired by trying another address
            except OSError as error:
                candidate.close()
                last = error
        raise last
