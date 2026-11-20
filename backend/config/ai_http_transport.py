"""The real HTTPS transport of the AI Gateway adapters (``apps.ai_gateway.transport.HttpTransport``). Standard library only.

It lives here, outside the pure ``apps/ai_gateway`` package, because it is the one piece that opens sockets. Importing this module, and
constructing the transport, make NO network call: a call happens only inside ``send``.

* TLS: certificates and host names are always verified with the platform's trust store (``ssl.create_default_context``), plus an optional
  extra CA bundle for a company TLS-inspection proxy. There is NO option to switch verification off.
* Proxy: only an explicit ``proxy_url`` (``http://host:port``, optionally ``user:password@``) is used, through an HTTP CONNECT tunnel, and
  then every request uses it. ``HTTPS_PROXY``, ``HTTP_PROXY`` and ``NO_PROXY`` are never read, so nothing is bypassed or added implicitly.
  Without ``proxy_url`` the connection is direct. Only basic proxy authentication is supported (no NTLM or Kerberos).
* Timeouts: ``connect_timeout_seconds`` bounds the TCP connect, the proxy tunnel and the TLS handshake; ``read_timeout_seconds`` bounds each
  later socket operation AND, through a watchdog (``_Watchdog``), the whole exchange: when it is used up the socket is shut down from a timer
  thread, the call fails with ``timeout`` and anything read is discarded, so a slow-dripping server cannot hold the caller longer than the
  adapter's remaining deadline. Name resolution happens before a socket exists and is not covered.
* ``SSLKEYLOGFILE`` is neutralised: the standard library would otherwise write TLS session secrets of this traffic to that file.
* No redirect is followed (``http.client`` never does), so a credential is never replayed to another host. No ``Accept-Encoding`` is sent.
* The response is read up to ``MAX_RESPONSE_BYTES`` and refused beyond it.
* Failures become ``TransportError`` with a fixed kind; the underlying exception (which can name a host or proxy) is dropped.
"""

import base64
import http.client
import socket
import ssl
import threading
import time
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


def checked_response_limit(max_response_bytes) -> int:
    """The response cap a transport enforces: ``MAX_RESPONSE_BYTES`` by default, or a LOWER positive integer (never a higher one)."""
    if max_response_bytes is None:
        return MAX_RESPONSE_BYTES
    if not isinstance(max_response_bytes, int) or isinstance(max_response_bytes, bool) or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES:
        raise configuration_error("invalid_max_response_bytes", "the response size limit is invalid")
    return max_response_bytes


class _Watchdog:
    """The wall-clock budget of ONE request (``read_timeout_seconds``, which the adapters set to the time left of the call's deadline).

    A socket timeout bounds each operation, not the whole exchange: a server that sends one byte every few seconds can keep ``read`` going for as long as it likes. When the budget is
    used up this shuts the connection's socket down from a timer thread, which makes the blocked read return at once; ``expired`` then tells ``send`` to report ``timeout`` and to discard
    whatever was read (it may be truncated). It is cancelled when the request ends. Name resolution is not covered (it happens before a socket exists)."""

    def __init__(self, seconds):
        self._connection = None
        self._end = time.monotonic() + seconds
        self.expired = threading.Event()
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.daemon = True

    def remaining(self) -> float:
        return self._end - time.monotonic()

    def start(self):
        self._timer.start()

    def watch(self, connection):
        self._connection = connection

    def cancel(self):
        self._timer.cancel()

    def _expire(self):
        self.expired.set()
        sock = getattr(self._connection, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:  # already closed, or not a real socket: the flag is what send() checks
                pass


class StdlibHttpsTransport(HttpTransport):
    def __init__(self, *, proxy_url=None, ca_bundle=None, connection_factory=http.client.HTTPSConnection, max_response_bytes=None):
        self._context = build_ssl_context(ca_bundle)
        self._proxy = None if proxy_url is None else _parse_proxy(proxy_url)
        self._connection_factory = connection_factory
        self._max_response_bytes = checked_response_limit(max_response_bytes)

    def __repr__(self):
        return f"StdlibHttpsTransport(proxy={'yes' if self._proxy else 'no'})"

    def send(self, request: HttpRequest) -> HttpResponse:
        if request.network_class is not None:  # a rule about resolved addresses that this transport cannot enforce: refuse rather than ignore it
            raise TransportError("destination")
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
        watchdog = _Watchdog(request.read_timeout_seconds)
        watchdog.start()
        try:
            if self._proxy is None:
                connection = self._connection_factory(host, port, timeout=request.connect_timeout_seconds, context=self._context)
            else:
                proxy_host, proxy_port, proxy_headers = self._proxy
                connection = self._connection_factory(proxy_host, proxy_port, timeout=request.connect_timeout_seconds, context=self._context)
                connection.set_tunnel(host, port, headers=dict(proxy_headers))
            watchdog.watch(connection)
            connection.connect()  # connect timeout: TCP, proxy tunnel and TLS handshake
            connected = True
            connection.sock.settimeout(request.read_timeout_seconds)  # read timeout from here on
            connection.request("POST", target, body=request.body, headers=dict(request.headers))
            response = connection.getresponse()
            data = response.read(self._max_response_bytes + 1)
            if len(data) > self._max_response_bytes:
                failure = "protocol"
            else:
                status, body = response.status, data
        except TimeoutError:
            failure = "timeout" if connected else "connect"
        except ssl.SSLError:
            failure = "tls"
        except (OSError, ValueError, http.client.HTTPException):  # ValueError: a socket shut down by the watchdog while it was being read
            failure = "protocol" if connected else "connect"
        finally:
            watchdog.cancel()
            if connection is not None:
                try:
                    connection.close()
                except Exception:  # nothing useful to do, and nothing to leak
                    pass
        if watchdog.expired.is_set():
            failure = "timeout"  # the whole exchange outlasted its budget: whatever arrived is discarded
        if failure is not None:
            raise TransportError(failure)  # raised outside the except blocks: the original exception is not this error's context
        return HttpResponse(status, body)
