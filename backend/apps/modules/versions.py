"""A small, documented subset of Semantic Versioning, and the compatibility checks built on it. Standard library only.

THE SUBSET. This is deliberately not a package manager and not complete SemVer.

* A version is exactly ``MAJOR.MINOR.PATCH``: three non-negative decimal integers separated by dots, each ``0`` or without a
  leading zero, at most nine digits. ``1.2.3`` is valid; ``1.2``, ``01.2.3``, ``v1.2.3``, `` 1.2.3`` and ``1.2.3\\n`` are not.
* There are NO pre-release tags (``-beta``) and NO build metadata (``+5``): they are refused, not ignored. A built-in module
  is released or it is not; if pre-releases are ever needed the contract gets a new manifest version.
* Versions compare numerically by (major, minor, patch).
* A RANGE is one to four comparators joined by commas, ALL of which must hold: ``>=1.2.0,<2.0.0``. The operators are ``>=``,
  ``>``, ``<=``, ``<`` and ``==``. Whitespace around commas and between an operator and its version is allowed. There is no
  ``^``, ``~``, wildcard, ``||`` or hyphen syntax.
* A range is normalized to its tightest equivalent interval and printed canonically (``>=1.0.0,<2.0.0``; a pin is
  ``==1.2.3``), so equal ranges have equal text. A range no version can satisfy (``>=2.0.0,<1.0.0``) is refused.

CORE API VERSION. ``CORE_API_VERSION`` is the version of the module-facing Core interface. A manifest states which versions it
supports in ``core_api_compatibility``; it must have an upper bound (a module may not claim to work with every future Core).
Rule of thumb for the Core's own releases: a breaking change to the interface raises the major version.

UPDATER PROTOCOL. ``UPDATER_PROTOCOL_VERSION`` is the highest update-manager protocol this Core understands. It is ``0``: no
update manager exists yet, so a manifest asking for a minimum protocol of 1 or more is refused (see registry.py).
"""

import re
from dataclasses import dataclass

from .errors import VersionError

_PART = r"(0|[1-9][0-9]{0,8})"
_VERSION = re.compile(rf"{_PART}\.{_PART}\.{_PART}")
_COMPARATOR = re.compile(rf"(>=|<=|==|>|<)\s*({_PART}\.{_PART}\.{_PART})")

MAX_COMPARATORS = 4
MAX_RANGE_LENGTH = 64


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


CORE_API_VERSION = Version(1, 0, 0)
UPDATER_PROTOCOL_VERSION = 0


def parse_version(text) -> Version:
    """``"1.2.3"`` -> ``Version(1, 2, 3)``, or ``VersionError('invalid_version')``."""
    if not isinstance(text, str) or _VERSION.fullmatch(text) is None:
        raise VersionError("invalid_version", "a version must be MAJOR.MINOR.PATCH (no pre-release or build tags)")
    major, minor, patch = (int(part) for part in text.split("."))
    return Version(major, minor, patch)


@dataclass(frozen=True)
class VersionRange:
    """An interval of versions. ``None`` for a bound means unbounded on that side."""

    lower: Version | None = None
    lower_inclusive: bool = True
    upper: Version | None = None
    upper_inclusive: bool = False

    def contains(self, version: Version) -> bool:
        if not isinstance(version, Version):
            raise TypeError("version must be a Version")
        if self.lower is not None:
            if version < self.lower or (version == self.lower and not self.lower_inclusive):
                return False
        if self.upper is not None:
            if version > self.upper or (version == self.upper and not self.upper_inclusive):
                return False
        return True

    @property
    def is_bounded_above(self) -> bool:
        return self.upper is not None

    def __str__(self) -> str:
        if self.lower is not None and self.lower == self.upper and self.lower_inclusive and self.upper_inclusive:
            return f"=={self.lower}"
        parts = []
        if self.lower is not None:
            parts.append(f"{'>=' if self.lower_inclusive else '>'}{self.lower}")
        if self.upper is not None:
            parts.append(f"{'<=' if self.upper_inclusive else '<'}{self.upper}")
        return ",".join(parts)


def _tighter_lower(current, candidate):
    """The higher lower bound; at the same version an exclusive bound is tighter."""
    if current is None:
        return candidate
    (cv, ci), (nv, ni) = current, candidate
    if nv > cv or (nv == cv and not ni and ci):
        return candidate
    return current


def _tighter_upper(current, candidate):
    """The lower upper bound; at the same version an exclusive bound is tighter."""
    if current is None:
        return candidate
    (cv, ci), (nv, ni) = current, candidate
    if nv < cv or (nv == cv and not ni and ci):
        return candidate
    return current


def parse_range(text) -> VersionRange:
    """Parse the documented range subset, or raise ``VersionError`` (``invalid_range``, ``too_many_comparators``, ``empty_range``)."""
    if not isinstance(text, str) or not text or len(text) > MAX_RANGE_LENGTH:
        raise VersionError("invalid_range", "a version range must be non-empty text of at most 64 characters")
    parts = text.split(",")
    if len(parts) > MAX_COMPARATORS:
        raise VersionError("too_many_comparators", f"a version range may have at most {MAX_COMPARATORS} comparators")
    lower = upper = None  # each: (Version, inclusive) or None
    for part in parts:
        match = _COMPARATOR.fullmatch(part.strip())
        if match is None:
            raise VersionError("invalid_range", "each comparator must be one of >=, >, <=, <, == followed by MAJOR.MINOR.PATCH")
        operator, version = match.group(1), parse_version(match.group(2))
        if operator == ">=":
            lower = _tighter_lower(lower, (version, True))
        elif operator == ">":
            lower = _tighter_lower(lower, (version, False))
        elif operator == "<=":
            upper = _tighter_upper(upper, (version, True))
        elif operator == "<":
            upper = _tighter_upper(upper, (version, False))
        else:  # ==
            lower = _tighter_lower(lower, (version, True))
            upper = _tighter_upper(upper, (version, True))
    if lower is not None and upper is not None:
        (lv, li), (uv, ui) = lower, upper
        if lv > uv or (lv == uv and not (li and ui)):
            raise VersionError("empty_range", "no version can satisfy this range")
    return VersionRange(
        lower=None if lower is None else lower[0],
        lower_inclusive=True if lower is None else lower[1],
        upper=None if upper is None else upper[0],
        upper_inclusive=False if upper is None else upper[1],
    )


def is_core_api_compatible(core_api_range: VersionRange, core_api_version: Version = CORE_API_VERSION) -> bool:
    """True when ``core_api_version`` is inside the range a manifest declares."""
    return core_api_range.contains(core_api_version)
