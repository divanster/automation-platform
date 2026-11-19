"""A small, closed subset of JSON Schema for structured output. Pure Python, standard library only.

WHY A SUBSET. The project has no JSON Schema library and AI-1a installs none. The subset below is enough to say "an object with these
typed fields" and it fails CLOSED: a schema that uses any other keyword is rejected (``invalid_request``, reason
``invalid_output_schema``) rather than silently ignored, so a constraint a caller believes in is never skipped.

Supported keywords: ``type`` (REQUIRED in every sub-schema; one of object, array, string, integer, number, boolean, null, or a list of
them), ``properties``, ``required`` (names that must also be in ``properties``), ``additionalProperties`` (a boolean), ``items``,
``enum`` (scalars), ``minItems``, ``maxItems``, ``minLength``, ``maxLength``, ``minimum``, ``maximum``, and the annotations ``title``
and ``description``. The root must be an object. ``pattern`` and ``$ref`` are NOT supported (a caller-supplied regular expression is a
denial-of-service risk, and references need a resolver).

Validation never repairs, truncates or defaults anything. ``validate_output`` returns violations as ``"path: rule"`` strings built only from
schema property names and array indexes, never from the checked value (a model might put document text in a key or a value).
"""

from collections.abc import Mapping

from .errors import AIGatewayError

MAX_SCHEMA_DEPTH = 8
MAX_SCHEMA_PROPERTIES = 100
MAX_ENUM_VALUES = 100
MAX_VIOLATIONS = 20

JSON_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
_ANNOTATIONS = frozenset({"title", "description"})
KEYWORDS = frozenset({"type", "properties", "required", "additionalProperties", "items", "enum", "minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum"}) | _ANNOTATIONS


def _bad(detail: str) -> AIGatewayError:
    return AIGatewayError("invalid_request", f"output schema: {detail}", reason="invalid_output_schema")


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _types_of(schema) -> tuple:
    declared = schema["type"]
    return (declared,) if isinstance(declared, str) else tuple(declared)


def check_schema(schema, *, _depth: int = 0, _root: bool = True) -> None:
    """Raise ``invalid_request`` unless ``schema`` is a valid schema of the supported subset with an object root."""
    if _depth >= MAX_SCHEMA_DEPTH:
        raise _bad("nested too deeply")
    if not isinstance(schema, Mapping):
        raise _bad("a schema must be an object")
    unknown = set(schema) - KEYWORDS
    if unknown:
        raise _bad("uses a keyword that is not supported")  # the keyword is schema text from the caller, but we still do not echo it
    if "type" not in schema:
        raise _bad("every schema needs a type")
    declared = schema["type"]
    if isinstance(declared, str):
        types = (declared,)
    elif isinstance(declared, (list, tuple)) and declared and all(isinstance(t, str) for t in declared) and len(set(declared)) == len(declared):
        types = tuple(declared)
    else:
        raise _bad("type must be a type name or a non-empty list of distinct type names")
    if not set(types) <= JSON_TYPES:
        raise _bad("type names an unsupported type")
    if _root and types != ("object",):
        raise _bad("the root type must be object")

    properties = schema.get("properties", {})
    if "properties" in schema:
        if not isinstance(properties, Mapping) or len(properties) > MAX_SCHEMA_PROPERTIES:
            raise _bad("properties must be an object with a bounded number of entries")
        for child in properties.values():
            check_schema(child, _depth=_depth + 1, _root=False)
    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, (list, tuple)) or not all(isinstance(n, str) for n in required) or len(set(required)) != len(required):
            raise _bad("required must be a list of distinct names")
        if not set(required) <= set(properties):
            raise _bad("required names a property that is not declared")
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        raise _bad("additionalProperties must be a boolean")
    if "items" in schema:
        check_schema(schema["items"], _depth=_depth + 1, _root=False)
    if "enum" in schema:
        values = schema["enum"]
        if not isinstance(values, (list, tuple)) or not values or len(values) > MAX_ENUM_VALUES:
            raise _bad("enum must be a non-empty bounded list")
        if not all(v is None or isinstance(v, (bool, int, float, str)) for v in values):
            raise _bad("enum values must be scalars")
    for keyword in ("minItems", "maxItems", "minLength", "maxLength"):
        if keyword in schema and (not isinstance(schema[keyword], int) or isinstance(schema[keyword], bool) or schema[keyword] < 0):
            raise _bad(f"{keyword} must be a non-negative integer")
    for keyword in ("minimum", "maximum"):
        if keyword in schema and not _is_number(schema[keyword]):
            raise _bad(f"{keyword} must be a number")
    for low, high in (("minItems", "maxItems"), ("minLength", "maxLength"), ("minimum", "maximum")):
        if low in schema and high in schema and schema[low] > schema[high]:
            raise _bad(f"{low} is greater than {high}")
    for keyword in _ANNOTATIONS:
        if keyword in schema and not isinstance(schema[keyword], str):
            raise _bad(f"{keyword} must be a string")


def _is_type(value, name: str) -> bool:
    if name == "null":
        return value is None
    if name == "boolean":
        return isinstance(value, bool)
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return _is_number(value)
    if name == "array":
        return isinstance(value, (list, tuple))
    return isinstance(value, Mapping)  # "object"


def _same_json_scalar(a, b) -> bool:
    return isinstance(a, bool) == isinstance(b, bool) and a == b  # True is not 1 in JSON


def validate_output(schema, value) -> tuple:
    """The violations of ``value`` against a schema already accepted by ``check_schema`` (empty: valid)."""
    found: list = []

    def add(path, rule):
        if len(found) < MAX_VIOLATIONS:
            found.append(f"{path}: {rule}")

    def walk(sub, node, path):
        if len(found) >= MAX_VIOLATIONS:
            return
        if not any(_is_type(node, t) for t in _types_of(sub)):
            add(path, "type")
            return
        if "enum" in sub and not any(_same_json_scalar(node, option) for option in sub["enum"]):
            add(path, "enum")
        if isinstance(node, Mapping):
            properties = sub.get("properties", {})
            for name in sub.get("required", ()):
                if name not in node:
                    add(f"{path}.{name}", "required")
            for name, child in properties.items():
                if name in node:
                    walk(child, node[name], f"{path}.{name}")
            if sub.get("additionalProperties", True) is False and any(key not in properties for key in node):
                add(path, "additional_property")  # no key name: it is model output
        elif isinstance(node, (list, tuple)):
            if "minItems" in sub and len(node) < sub["minItems"]:
                add(path, "min_items")
            if "maxItems" in sub and len(node) > sub["maxItems"]:
                add(path, "max_items")
            if "items" in sub:
                for index, element in enumerate(node):
                    walk(sub["items"], element, f"{path}[{index}]")
        elif isinstance(node, str):
            if "minLength" in sub and len(node) < sub["minLength"]:
                add(path, "min_length")
            if "maxLength" in sub and len(node) > sub["maxLength"]:
                add(path, "max_length")
        elif _is_number(node):
            if "minimum" in sub and node < sub["minimum"]:
                add(path, "minimum")
            if "maximum" in sub and node > sub["maximum"]:
                add(path, "maximum")

    walk(schema, value, "$")
    return tuple(found)
