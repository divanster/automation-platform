"""The real HTTPS transport of the AI Gateway adapters (``apps.ai_gateway.transport.HttpTransport``). Standard library only.

It lives here, outside the pure ``apps/ai_gateway`` package, because it is the one piece that opens sockets. Importing this module, and
constructing the transport, make NO network call: a call happens only inside ``send``.

* TLS: certificates and host names are always verified with the platform's trust store (``ssl.create_default_context``), plus an optional
  extra CA bundle for a company TLS-inspection proxy. There is NO option to switch verification off.
* Proxy: only an explicit ``proxy_url`` (``http://host:port``, optionally ``user:password@``) is used, through an HTTP CONNECT tunnel, and
  then every request uses it. ``HTTPS_PROXY``, ``HTTP_PROXY`` and ``NO_PROXY`` are never read, so nothing is bypassed or added implicitly.
  Without ``proxy_url`` the connection is direct. Only basic proxy authentication is supported (no NTLM or Kerberos).
* Timeouts: ``connect_timeout_seconds`` bounds the TCP connect, the proxy tunnel and the TLS handshake; ``read_timeout_seconds`` bounds each
  later socket operation. A slow-dripping server can exceed the read timeout in total; the adapter's deadline and the gateway refuse a
  late answer, they cannot interrupt this call.
* ``SSLKEYLOGFILE`` is neutralised: the standard library would otherwise write TLS session secrets of this traffic to that file.
* No redirect is followed (``http.client`` never does), so a credential is never replayed to another host. No ``Accept-Encoding`` is sent.
* The response is read up to ``MAX_RESPONSE_BYTES`` and refused beyond it.
* Failures become ``TransportError`` with a fixed kind; the underlying exception (which can name a host or proxy) is dropped.
"""

import base64
import http.client
import ssl
from urllib.parse import unquote, urlsplit

from apps.ai_gateway.errors import configuration_error
from apps.ai_gateway.transport import MAX_RESPONSE_BYTES, HttpRequest, HttpResponse, HttpTransport, TransportError

DEFAULT_HTTPS_PORT = 443
DEFAULT_PROXY_PORT = 8080


def _parse_proxy(proxy_url):
    """``(host, port, headers)`` of an explicit ``http://[user:password@]host[:port]`` proxy; configuration_error otherwise (never echoing it)."""
    try:
        parts = urlsplit(proxy_url)
        port = parts.port
    except ValueError:
        raise configuration_error("invalid_proxy_url", "the proxy URL is invalid") from None
    if parts.scheme != "http" or not parts.hostname or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise configuration_error("invalid_proxy_url", "the proxy URL must look like http://host:port")
    headers = ()
    if parts.username is not None:
        credentials = f"{unquote(parts.username)}:{unquote(parts.password or '')}".encode()
        headers = (("Proxy-Authorization", "Basic " + base64.b64encode(credentials).decode("ascii")),)
    return parts.hostname, port or DEFAULT_PROXY_PORT, headers


def build_ssl_context(ca_bundle=None) -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.keylog_filename = None  # create_default_context honours SSLKEYLOGFILE; TLS session secrets must never be written to disk
    if ca_bundle is not None:
        try:
            context.load_verify_locations(cafile=ca_bundle)
        except (OSError, ssl.SSLError, ValueError, TypeError):
            raise configuration_error("invalid_ca_bundle", "the extra CA bundle cannot be read") from None
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:  # cannot happen with the default context; never run without it
        raise configuration_error("tls_verification_disabled", "TLS verification must stay enabled")
    return context


class StdlibHttpsTransport(HttpTransport):
    def __init__(self, *, proxy_url=None, ca_bundle=None, connection_factory=http.client.HTTPSConnection):
        self._context = build_ssl_context(ca_bundle)
        self._proxy = None if proxy_url is None else _parse_proxy(proxy_url)
        self._connection_factory = connection_factory

    def __repr__(self):
        return f"StdlibHttpsTransport(proxy={'yes' if self._proxy else 'no'})"

    def send(self, request: HttpRequest) -> HttpResponse:
        try:
            parts = urlsplit(request.url)
            host, port = parts.hostname, parts.port or DEFAULT_HTTPS_PORT
        except ValueError:
            raise TransportError("protocol") from None
        if parts.scheme != "https" or not host:  # never send a credential over plain HTTP
            raise TransportError("protocol")
        target = parts.path + (f"?{parts.query}" if parts.query else "")

        connection = None
        connected = False
        failure = None
        status = body = None
        try:
            if self._proxy is None:
                connection = self._connection_factory(host, port, timeout=request.connect_timeout_seconds, context=self._context)
            else:
                proxy_host, proxy_port, proxy_headers = self._proxy
                connection = self._connection_factory(proxy_host, proxy_port, timeout=request.connect_timeout_seconds, context=self._context)
                connection.set_tunnel(host, port, headers=dict(proxy_headers))
            connection.connect()  # connect timeout: TCP, proxy tunnel and TLS handshake
            connected = True
            connection.sock.settimeout(request.read_timeout_seconds)  # read timeout from here on
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
