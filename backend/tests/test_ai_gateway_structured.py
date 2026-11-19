"""DB-free tests of the closed JSON Schema subset used for structured output, and of bounded immutable JSON."""

import unittest

from apps.ai_gateway import AIGatewayError
from apps.ai_gateway.jsonvalue import MAX_JSON_DEPTH, MAX_JSON_NODES, canonical_json, estimate_tokens, freeze_json, thaw_json
from apps.ai_gateway.structured import MAX_VIOLATIONS, check_schema, validate_output


def obj(properties, required=(), **extra):
    return {"type": "object", "properties": properties, "required": list(required), **extra}


class CheckSchemaTests(unittest.TestCase):
    def test_valid_schemas_are_accepted(self):
        check_schema(obj({"a": {"type": "string", "minLength": 1, "maxLength": 5, "description": "d"}}, ["a"]))
        check_schema(obj({"n": {"type": ["integer", "null"], "minimum": 0, "maximum": 9}, "l": {"type": "array", "items": {"type": "string"}, "minItems": 0, "maxItems": 3}}))
        check_schema(obj({"e": {"type": "string", "enum": ["x", "y"]}, "o": obj({}, additionalProperties=False)}))
        check_schema({"type": "object"})

    def test_a_schema_that_would_be_silently_ignored_is_refused(self):
        for keyword in ("pattern", "$ref", "oneOf", "anyOf", "allOf", "not", "format", "default", "const", "patternProperties", "uniqueItems", "multipleOf", "$schema"):
            with self.subTest(keyword), self.assertRaises(AIGatewayError) as ctx:
                check_schema(obj({"a": {"type": "string", keyword: "x"}}))
            self.assertEqual((ctx.exception.code, ctx.exception.reason), ("invalid_request", "invalid_output_schema"))
        with self.assertRaises(AIGatewayError):
            check_schema({"type": "object", "pattern": "^a"})

    def test_invalid_schemas_are_refused(self):
        bad = {
            "not a mapping": "object",
            "missing type": {"properties": {}},
            "root not object": {"type": "string"},
            "root list of types": {"type": ["object", "null"]},
            "unknown type": obj({"a": {"type": "float"}}),
            "empty type list": obj({"a": {"type": []}}),
            "duplicate type": obj({"a": {"type": ["string", "string"]}}),
            "type not string": obj({"a": {"type": 5}}),
            "required not declared": obj({"a": {"type": "string"}}, ["b"]),
            "required not list": {"type": "object", "properties": {}, "required": "a"},
            "required duplicate": obj({"a": {"type": "string"}}, ["a", "a"]),
            "additionalProperties not bool": obj({}, additionalProperties={"type": "string"}),
            "properties not mapping": {"type": "object", "properties": []},
            "child without type": obj({"a": {}}),
            "child not mapping": obj({"a": "string"}),
            "enum empty": obj({"a": {"type": "string", "enum": []}}),
            "enum not scalar": obj({"a": {"type": "string", "enum": [["x"]]}}),
            "negative length": obj({"a": {"type": "string", "minLength": -1}}),
            "bool length": obj({"a": {"type": "string", "maxLength": True}}),
            "min greater than max": obj({"a": {"type": "integer", "minimum": 5, "maximum": 1}}),
            "minLength greater than maxLength": obj({"a": {"type": "string", "minLength": 5, "maxLength": 1}}),
            "minimum not number": obj({"a": {"type": "integer", "minimum": "0"}}),
            "annotation not string": obj({"a": {"type": "string", "description": 5}}),
        }
        for label, schema in bad.items():
            with self.subTest(label), self.assertRaises(AIGatewayError) as ctx:
                check_schema(schema)
            self.assertEqual(ctx.exception.code, "invalid_request", label)

    def test_deeply_nested_and_oversized_schemas_are_refused(self):
        node = {"type": "string"}
        for _ in range(12):
            node = {"type": "object", "properties": {"a": node}}
        with self.assertRaises(AIGatewayError):
            check_schema(node)
        with self.assertRaises(AIGatewayError):
            check_schema(obj({f"p{i}": {"type": "string"} for i in range(101)}))

    def test_the_error_does_not_echo_the_schema(self):
        with self.assertRaises(AIGatewayError) as ctx:
            check_schema(obj({"SECRET-PROPERTY": {"type": "string", "SECRET-KEYWORD": 1}}))
        self.assertNotIn("SECRET", str(ctx.exception))


class ValidateOutputTests(unittest.TestCase):
    schema = obj(
        {
            "name": {"type": "string", "minLength": 2, "maxLength": 5},
            "count": {"type": "integer", "minimum": 1, "maximum": 3},
            "ratio": {"type": "number"},
            "flag": {"type": "boolean"},
            "kind": {"type": "string", "enum": ["a", "b"]},
            "maybe": {"type": ["string", "null"]},
            "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 2},
            "child": obj({"x": {"type": "integer"}}, ["x"], additionalProperties=False),
        },
        ["name"],
        additionalProperties=False,
    )

    def check(self, value):
        return validate_output(self.schema, freeze_json(value, code="invalid_request", reason="t"))

    def test_a_valid_value_has_no_violations(self):
        self.assertEqual(self.check({"name": "abc"}), ())
        self.assertEqual(self.check({"name": "abc", "count": 2, "ratio": 1, "flag": True, "kind": "a", "maybe": None, "tags": ["t"], "child": {"x": 1}}), ())

    def test_each_rule_is_reported_with_a_path_and_a_rule_name(self):
        cases = {
            ("$.name: required",): {},
            ("$.name: type",): {"name": 5},
            ("$.name: min_length",): {"name": "a"},
            ("$.name: max_length",): {"name": "abcdef"},
            ("$.count: type",): {"name": "ab", "count": "2"},
            ("$.count: minimum",): {"name": "ab", "count": 0},
            ("$.count: maximum",): {"name": "ab", "count": 4},
            ("$.ratio: type",): {"name": "ab", "ratio": "1"},
            ("$.flag: type",): {"name": "ab", "flag": 1},
            ("$.kind: enum",): {"name": "ab", "kind": "c"},
            ("$.maybe: type",): {"name": "ab", "maybe": 3},
            ("$.tags: min_items",): {"name": "ab", "tags": []},
            ("$.tags: max_items",): {"name": "ab", "tags": ["a", "b", "c"]},
            ("$.tags[1]: type",): {"name": "ab", "tags": ["a", 2]},
            ("$.child.x: required",): {"name": "ab", "child": {}},
            ("$.child: additional_property",): {"name": "ab", "child": {"x": 1, "y": 2}},
            ("$: additional_property",): {"name": "ab", "extra": 1},
            ("$: type",): [],
        }
        for expected, value in cases.items():
            with self.subTest(expected):
                self.assertEqual(self.check(value), expected)

    def test_booleans_are_not_numbers_and_integers_are_not_floats(self):
        self.assertEqual(self.check({"name": "ab", "count": True}), ("$.count: type",))
        self.assertEqual(self.check({"name": "ab", "count": 2.0}), ("$.count: type",))
        self.assertEqual(self.check({"name": "ab", "ratio": True}), ("$.ratio: type",))
        enum_schema = obj({"v": {"type": ["integer", "boolean"], "enum": [1]}}, ["v"])
        self.assertEqual(validate_output(enum_schema, {"v": True}), ("$.v: enum",))  # True is not 1
        self.assertEqual(validate_output(enum_schema, {"v": 1}), ())

    def test_nothing_is_repaired_or_defaulted(self):
        value = freeze_json({"name": "toolongvalue"}, code="invalid_request", reason="t")
        self.assertEqual(validate_output(self.schema, value), ("$.name: max_length",))
        self.assertEqual(value["name"], "toolongvalue")

    def test_violations_never_contain_values_or_unexpected_key_names(self):
        found = self.check({"name": "SECRET-VALUE", "SECRET-KEY": "SECRET-VALUE-2", "kind": "SECRET-ENUM"})
        self.assertTrue(found)
        self.assertNotIn("SECRET", " ".join(found))

    def test_violations_are_bounded(self):
        schema = {"type": "object", "properties": {"l": {"type": "array", "items": {"type": "integer"}}}}
        found = validate_output(schema, {"l": ["x"] * 500})
        self.assertEqual(len(found), MAX_VIOLATIONS)


class JsonValueTests(unittest.TestCase):
    def freeze(self, value):
        return freeze_json(value, code="invalid_request", reason="t")

    def test_plain_json_is_frozen_deeply(self):
        source = {"a": [1, {"b": None}], "c": "x", "d": 1.5, "e": True}
        frozen = self.freeze(source)
        source["a"].append(2)
        self.assertEqual(thaw_json(frozen), {"a": [1, {"b": None}], "c": "x", "d": 1.5, "e": True})
        with self.assertRaises(TypeError):
            frozen["a"] = 1
        self.assertIsInstance(frozen["a"], tuple)
        with self.assertRaises(TypeError):
            frozen["a"][1]["b"] = 1

    def test_anything_that_is_not_plain_json_is_refused(self):
        import decimal

        for bad in (decimal.Decimal("1"), b"x", {1, 2}, object(), float("nan"), float("inf"), {1: "x"}, {"a": object()}, lambda: 1):
            with self.subTest(repr(bad)[:30]), self.assertRaises(AIGatewayError):
                self.freeze(bad)

    def test_size_and_depth_are_bounded(self):
        deep = []
        for _ in range(MAX_JSON_DEPTH + 2):
            deep = [deep]
        with self.assertRaises(AIGatewayError):
            self.freeze(deep)
        with self.assertRaises(AIGatewayError):
            self.freeze(list(range(MAX_JSON_NODES + 1)))
        self.freeze(list(range(100)))

    def test_canonical_json_is_deterministic(self):
        a = canonical_json(self.freeze({"b": 1, "a": [True, None, "é"]}))
        b = canonical_json(self.freeze({"a": [True, None, "é"], "b": 1}))
        self.assertEqual(a, b)
        self.assertEqual(a, '{"a":[true,null,"é"],"b":1}')

    def test_the_token_estimate_is_deterministic_and_cautious(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("abc"), 1)
        self.assertEqual(estimate_tokens("abcd"), 2)
        self.assertEqual(estimate_tokens("é"), 1)
        self.assertGreaterEqual(estimate_tokens("word " * 100), 100)
