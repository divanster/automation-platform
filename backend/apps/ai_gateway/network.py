"""Destination rules for HTTP adapters (AI-CON-1). Pure Python, standard library only; it resolves nothing and opens no connection.

Three things live here so that the adapter (which decides WHERE it may send) and the one real transport (which checks the RESOLVED address just before it
connects) share a single definition:

* ``Origin`` and the strict URL grammar: ``scheme://host[:port]`` with an optional path, no credentials, no query, no fragment, no percent-encoding, a lower-case
  host that is either a dotted-decimal IPv4 literal, a bracketed IPv6 literal or a DNS name whose last label contains a letter (so ``2130706433``, ``0x7f.1`` and
  ``0177.0.0.1`` are not hosts at all), and a port from 1 to 65535.
* ``NetworkClass`` and ``address_permitted``: which IP addresses a locality may reach. ``local`` reaches loopback only. ``company_endpoint`` reaches ONLY addresses inside
  the CIDR ranges the operator approved explicitly (``approved_networks``; none approved means nothing is reachable). ``cloud`` reaches global addresses only.
  For both of the last two, an explicit denylist of special-purpose ranges (``DENIED_NETWORKS``: this-network, loopback, link-local and so the cloud metadata
  addresses, carrier-grade NAT, benchmarking, IETF protocol, documentation, 6to4 relay, multicast, reserved, the AWS IPv6 and Azure metadata ranges, and the IPv6
  forms that can embed an IPv4 address: IPv4-compatible, IPv4-translated, NAT64, 6to4, Teredo) is applied FIRST and cannot be overridden by an approved range. It does
  not rely on ``ipaddress.is_private`` or ``is_global``, which differ between Python versions and classify several of these ranges as usable. IPv4-mapped IPv6
  addresses are judged as the IPv4 address they carry.
* ``parse_networks``: the strict parser of an approved-range list (CIDR notation only, no host bits, no range that is too broad or touches a denied range).

The classification is a code-level rule, not a firewall: a transport that does not call ``address_permitted`` on the address it connects to enforces nothing, and
an HTTP proxy resolves names itself, out of this module's sight, so a hostname is never sent through one (docs/architecture/ai-gateway.md, section 17).
"""

import ipaddress
import re
from dataclasses import dataclass
from enum import StrEnum

from .errors import configuration_error
from .policy import DataLocality

DEFAULT_PORTS = {"http": 80, "https": 443}

_URL = re.compile(r"(https?)://(\[[0-9a-f:.]{2,45}\]|[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?)(?::([0-9]{1,5}))?(/[A-Za-z0-9._~/-]*)?")
_NUMERIC_HOST = re.compile(r"[0-9.]+")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_CIDR = re.compile(r"[0-9a-f:.]{2,45}/[0-9]{1,3}")

# Ranges that no company or cloud endpoint may ever resolve to, whatever the operator approves. Written out, not derived from ``ipaddress`` flags.
DENIED_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        # IPv4
        "0.0.0.0/8",  # "this network" (0.0.0.0 reaches the local host on some systems)
        "100.64.0.0/10",  # carrier-grade NAT; includes the Alibaba metadata address 100.100.100.200
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local: the AWS, Azure, GCP, Oracle and DigitalOcean metadata address 169.254.169.254 and the ECS task endpoint 169.254.170.2
        "168.63.129.16/32",  # the Azure host (wire server) address, which looks like a public one
        "192.0.0.0/24",  # IETF protocol assignments (includes 192.0.0.192, an Oracle metadata address)
        "192.0.2.0/24",  # documentation (TEST-NET-1)
        "192.88.99.0/24",  # deprecated 6to4 relay anycast
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",  # documentation (TEST-NET-2)
        "203.0.113.0/24",  # documentation (TEST-NET-3)
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved, and the limited broadcast 255.255.255.255
        # IPv6
        "::/96",  # unspecified, loopback and the deprecated IPv4-compatible forms
        "::ffff:0:0/96",  # IPv4-mapped (judged as the IPv4 address it carries before it gets here; kept for the overlap check of approved ranges)
        "::ffff:0:0:0/96",  # IPv4-translated (SIIT)
        "64:ff9b::/96",  # NAT64
        "64:ff9b:1::/48",  # local-use NAT64
        "100::/64",  # discard-only
        "2001::/23",  # IETF protocol assignments, including Teredo (2001::/32) and the ORCHID ranges
        "2001:db8::/32",  # documentation
        "2002::/16",  # 6to4
        "3fff::/20",  # documentation
        "5f00::/16",  # segment routing SIDs
        "fd00:ec2::/32",  # the AWS IPv6 metadata range (fd00:ec2::254)
        "fe80::/10",  # link-local
        "fec0::/10",  # deprecated site-local
        "ff00::/8",  # multicast
    )
)

MIN_PREFIX_LENGTH = {4: 8, 6: 16}  # an approved range broader than this is refused
MAX_APPROVED_NETWORKS = 20


class NetworkClass(StrEnum):
    LOOPBACK = "loopback"
    INTERNAL = "internal"
    PUBLIC = "public"


NETWORK_CLASS_BY_LOCALITY = {DataLocality.LOCAL: NetworkClass.LOOPBACK, DataLocality.COMPANY_ENDPOINT: NetworkClass.INTERNAL, DataLocality.CLOUD: NetworkClass.PUBLIC}


@dataclass(frozen=True)
class Origin:
    scheme: str
    host: str  # an IPv6 literal is stored without its brackets
    port: int  # always explicit

    @property
    def authority(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"

    def __str__(self):
        return f"{self.scheme}://{self.authority}"


def parse_ip(text):
    """The IP address a string spells in its canonical, unambiguous form, or ``None``. Zone identifiers and non-dotted-decimal IPv4 spellings are not addresses."""
    if not isinstance(text, str) or "%" in text:
        return None
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def is_loopback_name(host: str) -> bool:
    """``localhost`` (and anything under it) or a loopback literal: a host that names this machine."""
    ip = parse_ip(host)
    return host == "localhost" or host.endswith(".localhost") or (ip is not None and _unwrap(ip).is_loopback)


def _unwrap(ip):
    return ip.ipv4_mapped if ip.version == 6 and ip.ipv4_mapped is not None else ip


def _denied(ip) -> bool:
    return any(ip.version == network.version and ip in network for network in DENIED_NETWORKS)


def _approved(approved_networks):
    """The ``ip_network`` objects of an approved-range list; ``None`` when any entry is not a valid network (the caller then refuses everything)."""
    if isinstance(approved_networks, (str, bytes)):
        return None
    try:
        return tuple(ipaddress.ip_network(text) for text in approved_networks)
    except (ValueError, TypeError):
        return None


def address_permitted(text, network_class, approved_networks=()) -> bool:
    """Whether an IP address (as text) may be connected to under ``network_class``. Anything that is not an IP address is refused.

    ``approved_networks`` (CIDR strings) is consulted for ``INTERNAL`` only, which reaches nothing without it. The denylist applies before it and to ``PUBLIC``.
    """
    ip = parse_ip(text)
    if ip is None:
        return False
    ip = _unwrap(ip)
    if network_class is NetworkClass.LOOPBACK:
        return ip.is_loopback
    if _denied(ip) or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved or (ip.version == 6 and ip.is_site_local):
        return False
    if network_class is NetworkClass.PUBLIC:
        return ip.is_global
    if network_class is NetworkClass.INTERNAL:
        approved = _approved(approved_networks)
        return bool(approved) and any(ip.version == network.version and ip in network for network in approved)
    return False


def parse_networks(values, *, field: str = "approved_networks") -> tuple:
    """The approved ranges ``values`` spell, as sorted canonical CIDR strings. Raises ``configuration_error`` naming ``field``, never echoing a value.

    Each entry must be plain CIDR notation with no host bits set (``10.20.0.0/16``, not ``10.20.0.1/16`` or a netmask), no broader than /8 (IPv4) or /16
    (IPv6), and must not overlap any denied range, so an approved range can never open loopback, link-local, metadata or another special-purpose address.
    """
    if isinstance(values, (str, bytes)) or not hasattr(values, "__iter__"):
        raise configuration_error(f"invalid_{field}", f"{field} must be a collection of CIDR ranges")
    texts = tuple(values)
    if not texts or len(texts) > MAX_APPROVED_NETWORKS:
        raise configuration_error(f"invalid_{field}", f"{field} must list from 1 to {MAX_APPROVED_NETWORKS} CIDR ranges")
    networks = set()
    for text in texts:
        if not isinstance(text, str) or _CIDR.fullmatch(text) is None or "%" in text:
            raise configuration_error(f"invalid_{field}", f"{field} contains an entry that is not a CIDR range")
        try:
            network = ipaddress.ip_network(text, strict=True)
        except ValueError:
            raise configuration_error(f"invalid_{field}", f"{field} contains an entry that is not a valid CIDR range") from None
        if network.prefixlen < MIN_PREFIX_LENGTH[network.version]:
            raise configuration_error(f"invalid_{field}", f"{field} contains a range that is too broad")
        if any(network.version == denied.version and network.overlaps(denied) for denied in DENIED_NETWORKS):
            raise configuration_error(f"invalid_{field}", f"{field} contains a range that includes a denied special-purpose address range")
        networks.add(network)
    return tuple(sorted((str(network) for network in networks)))



def _valid_host(host: str) -> bool:
    if host.startswith("["):
        ip = parse_ip(host[1:-1])
        return ip is not None and ip.version == 6
    if _NUMERIC_HOST.fullmatch(host):
        ip = parse_ip(host)
        return ip is not None and ip.version == 4
    labels = host.split(".")
    if all(re.fullmatch(r"0x[0-9a-f]*|[0-9]+", label) for label in labels):
        return False  # a spelling some resolvers read as an IPv4 address (0x7f000001): not a name
    return len(host) <= 253 and all(_LABEL.fullmatch(label) for label in labels) and any(c.isalpha() for c in labels[-1])


def parse_url(text, *, field: str = "url", allow_path: bool = True):
    """``(Origin, path)`` of a strictly formed ``http(s)`` URL. Raises ``configuration_error`` naming ``field``, never echoing the text."""
    match = _URL.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise configuration_error(f"invalid_{field}", f"{field} is not a valid http(s) URL without credentials, query or fragment")
    scheme, host, port, path = match.groups()
    path = path or ""
    if not _valid_host(host):
        raise configuration_error(f"invalid_{field}", f"{field} has an invalid host")
    if host.startswith("["):
        host = str(ipaddress.ip_address(host[1:-1]))  # the canonical form, so equal addresses compare equal
    port_number = DEFAULT_PORTS[scheme] if port is None else int(port)
    if not 1 <= port_number <= 65535:
        raise configuration_error(f"invalid_{field}", f"{field} has an invalid port")
    if path and (not allow_path or ".." in path or "//" in path or "/." in path):
        raise configuration_error(f"invalid_{field}", f"{field} has an invalid path")
    return Origin(scheme, host, port_number), path
