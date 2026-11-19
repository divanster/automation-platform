"""Bounded, immutable JSON values. Pure Python, standard library only.

A request schema and a provider output are JSON data of unknown size and shape. ``freeze_json`` checks that a value is plain JSON (null,
boolean, integer, finite float, string, array, object with string keys), within a depth and node budget, and returns an immutable copy
(tuples and read-only mappings), so nothing the caller keeps a reference to can change it afterwards. Nothing else is accepted: no
``Decimal``, bytes, set, date or arbitrary object.
"""

import json
import math
from collections.abc import Mapping
from types import MappingProxyType

from .errors import AIGatewayError

MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 10_000


def freeze_json(value, *, code: str, reason: str):
    """Return an immutable deep copy of a plain JSON ``value``, or raise ``AIGatewayError(code, reason=reason)``."""
    budget = [MAX_JSON_NODES]

    def walk(node, depth):
        budget[0] -= 1
        if budget[0] < 0 or depth > MAX_JSON_DEPTH:
            raise AIGatewayError(code, "the JSON value is too large or too deeply nested", reason=reason)
        if node is None or isinstance(node, (bool, int, str)):
            return node
        if isinstance(node, float):
            if not math.isfinite(node):
                raise AIGatewayError(code, "the JSON value contains a non-finite number", reason=reason)
            return node
        if isinstance(node, (list, tuple)):
            return tuple(walk(item, depth + 1) for item in node)
        if isinstance(node, Mapping):
            items = []
            for key, item in node.items():
                if not isinstance(key, str):
                    raise AIGatewayError(code, "a JSON object key is not a string", reason=reason)
                items.append((key, walk(item, depth + 1)))
            return MappingProxyType(dict(items))
        raise AIGatewayError(code, "the value is not plain JSON data", reason=reason)

    return walk(value, 0)


def thaw_json(value):
    """The mutable (dict and list) form of a frozen value, for serialization."""
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_json(item) for item in value]
    return value


def canonical_json(value) -> str:
    """Deterministic compact JSON text: sorted keys, no whitespace, non-ASCII kept, NaN refused."""
    return json.dumps(thaw_json(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def estimate_tokens(text: str) -> int:
    """A deterministic, documented PRE-FLIGHT estimate of the tokens in ``text``: UTF-8 bytes divided by three, rounded up.

    It is deliberately on the cautious side for English (about four characters per token) and close to one token per character for scripts
    that use three bytes per character. It is a guard, not a tokenizer: the provider's own limit is authoritative.
    """
    return (len(text.encode("utf-8")) + 2) // 3
