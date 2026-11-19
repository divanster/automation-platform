"""The HTTP seam of the AI Gateway adapters. Pure Python, standard library only; it opens no socket.

An HTTP-based adapter (``adapters/workbench.py``) never talks to the network itself. It builds an ``HttpRequest``, hands it to an
``HttpTransport`` and reads an ``HttpResponse``. The package stays free of ``socket``, ``ssl`` and ``http``: the one real transport lives
outside it (``config/ai_http_transport.py``) and tests use a recording fake, so the adapter's wire behaviour is testable with no network.

A transport MUST: verify TLS certificates and host names; honour BOTH timeouts; never follow a redirect (a redirect would carry the
credential to another host); cap the response size; and raise only ``TransportError`` with one of the fixed kinds, never an exception that
holds a URL, a header, a body or a credential.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

TRANSPORT_ERROR_KINDS = frozenset(
    {
        "timeout",  # the connection was made, or may have been, and no answer arrived in time
        "connect",  # the connection could not be made (nothing reached the server)
        "tls",  # certificate or handshake failure: not transient
        "protocol",  # malformed or oversized HTTP answer
    }
)

MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class TransportError(Exception):
    """A transport failure. The message is fixed per ``kind``; it never carries a URL, header, body or credential."""

    def __init__(self, kind: str):
        if kind not in TRANSPORT_ERROR_KINDS:
            raise ValueError("unknown transport error kind")
        self.kind = kind
        super().__init__(f"HTTP transport failure: {kind}")


@dataclass(frozen=True)
class HttpRequest:
    """One POST. ``headers`` (it holds the credential) and ``body`` (it holds the prompt) are excluded from ``repr``."""

    url: str = field(repr=False)  # an https URL; the query may name an API version, never a secret
    headers: tuple = field(repr=False)  # ((name, value), ...)
    body: bytes = field(repr=False)
    connect_timeout_seconds: float
    read_timeout_seconds: float


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes = field(repr=False)


class HttpTransport(ABC):
    @abstractmethod
    def send(self, request: HttpRequest) -> HttpResponse:
        """Send the request once and return the answer whatever its status; raise ``TransportError`` when there is none."""
