"""Pure tests for workflow attribution (MOD-1b S5): the value and its agreement with the manifest. No Django, no database.

They run both with ``python -m unittest tests.test_workflow_attribution_pure`` and through ``manage.py test``. The model guard, the service, the
migrations and the boundaries are in tests/test_workflow_attribution.py; PostgreSQL behaviour is in tests/test_workflow_attribution_postgres.py.

This file is ASCII only.
"""

import dataclasses
import random
import string
import unittest

from apps.cases import attribution
from apps.cases.attribution import ATTRIBUTION_FIELDS, MAX_WORKFLOW_VERSION, WorkflowAttribution, WorkflowAttributionError, validate_attribution
from apps.modules import manifest as module_manifest
from apps.modules.errors import ManifestError
from tests.module_support import manifest


def accepted(function, *args) -> bool:
    try:
        function(*args)
    except (WorkflowAttributionError, ManifestError):
        return False
    return True


class AttributionValueTests(unittest.TestCase):
    def test_a_valid_attribution_keeps_its_values(self):
        value = WorkflowAttribution("request_intake", "request_intake", 1)
        self.assertEqual((value.module_id, value.workflow_key, value.workflow_version), ("request_intake", "request_intake", 1))
        self.assertEqual(value.fields(), {"module_id": "request_intake", "workflow_key": "request_intake", "workflow_version": 1})
        self.assertEqual(tuple(value.fields()), ATTRIBUTION_FIELDS)

    def test_a_dotted_workflow_key_and_the_largest_version_are_valid(self):
        WorkflowAttribution("demo_module", "demo_module.review.second_pass", MAX_WORKFLOW_VERSION)
        WorkflowAttribution("abc", "a" * 40 + "." + "b" * 23, 1)  # 64 characters

    def test_each_invalid_value_has_a_stable_code(self):
        cases = [
            (("Demo", "demo.x", 1), "invalid_module_id"),
            (("ab", "demo.x", 1), "invalid_module_id"),
            (("a" * 41, "demo.x", 1), "invalid_module_id"),
            (("1demo", "demo.x", 1), "invalid_module_id"),
            (("demo module", "demo.x", 1), "invalid_module_id"),
            (("demo\n", "demo.x", 1), "invalid_module_id"),
            (("core", "demo.x", 1), "reserved_namespace"),
            (("request", "demo.x", 1), "reserved_namespace"),
            (("demo_module", "", 1), "invalid_workflow_key"),
            (("demo_module", "Demo", 1), "invalid_workflow_key"),
            (("demo_module", "demo..x", 1), "invalid_workflow_key"),
            (("demo_module", "demo.", 1), "invalid_workflow_key"),
            (("demo_module", ".demo", 1), "invalid_workflow_key"),
            (("demo_module", "demo.x\n", 1), "invalid_workflow_key"),
            (("demo_module", "a" * 40 + "." + "b" * 24, 1), "invalid_workflow_key"),  # 65 characters
            (("demo_module", "core.flow", 1), "reserved_namespace"),
            (("demo_module", "task", 1), "reserved_namespace"),
            (("demo_module", "demo.x", 0), "invalid_workflow_version"),
            (("demo_module", "demo.x", -1), "invalid_workflow_version"),
            (("demo_module", "demo.x", MAX_WORKFLOW_VERSION + 1), "invalid_workflow_version"),
            ((None, "demo.x", 1), "invalid_type"),
            ((b"demo_module", "demo.x", 1), "invalid_type"),
            (("demo_module", None, 1), "invalid_type"),
            (("demo_module", "demo.x", "1"), "invalid_type"),
            (("demo_module", "demo.x", 1.0), "invalid_type"),
            (("demo_module", "demo.x", True), "invalid_type"),
            (("demo_module", "demo.x", None), "invalid_type"),
        ]
        for args, code in cases:
            with self.subTest(args=args):
                with self.assertRaises(WorkflowAttributionError) as caught:
                    WorkflowAttribution(*args)
                self.assertEqual(caught.exception.code, code)
                with self.assertRaises(WorkflowAttributionError):
                    validate_attribution(*args)

    def test_the_error_never_repeats_the_value(self):
        with self.assertRaises(WorkflowAttributionError) as caught:
            WorkflowAttribution("secret-module-NAME", "demo.x", 1)
        self.assertNotIn("secret", str(caught.exception))
        self.assertIsInstance(caught.exception, ValueError)

    def test_it_is_immutable(self):
        value = WorkflowAttribution("demo_module", "demo_module.review", 1)
        for name, new in (("module_id", "other_module"), ("workflow_key", "x.y"), ("workflow_version", 2)):
            with self.subTest(field=name), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(value, name, new)
        self.assertEqual(value, WorkflowAttribution("demo_module", "demo_module.review", 1))
        self.assertEqual(hash(value), hash(WorkflowAttribution("demo_module", "demo_module.review", 1)))

    def test_a_value_altered_after_construction_is_refused_when_it_is_used(self):
        value = WorkflowAttribution("demo_module", "demo_module.review", 1)
        object.__setattr__(value, "workflow_version", 0)
        with self.assertRaises(WorkflowAttributionError):
            value.fields()


class AgreementWithTheManifestTests(unittest.TestCase):
    """Core may not import the manifest, so its rules are written twice; these tests keep the two copies saying the same thing."""

    def test_the_reserved_namespaces_are_the_manifests(self):
        self.assertEqual(attribution.RESERVED_NAMESPACES, module_manifest.RESERVED_NAMESPACES)

    def test_the_length_limits_are_the_manifests(self):
        self.assertEqual(attribution.MAX_WORKFLOW_KEY_LENGTH, module_manifest.MAX_TASK_KIND_LENGTH)
        self.assertEqual(attribution.MAX_MODULE_ID_LENGTH, 40)
        self.assertEqual(module_manifest._MODULE_ID.pattern, attribution.MODULE_ID_PATTERN[1:-1])  # the anchored copy is the same expression

    def corpus(self):
        rng = random.Random(20261118)
        alphabet = string.ascii_lowercase[:4] + "AZ09_.- \n"
        generated = ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 14))) for _ in range(2500)]
        edge = ["a" * n for n in (1, 2, 3, 39, 40, 41)] + ["a" * 40 + "." + "b" * n for n in (22, 23, 24)] + sorted(attribution.RESERVED_NAMESPACES)
        return [*generated, *edge, "request_intake", "demo_module", "demo.flow", "x1", "x_1", "a.b.c.d"]

    def test_module_ids_are_accepted_exactly_when_a_manifest_accepts_them(self):
        for text in self.corpus():
            with self.subTest(text=text):
                self.assertEqual(accepted(validate_attribution, text, "demo.x", 1), accepted(lambda value: manifest(module_id=value), text), repr(text))

    def test_workflow_keys_are_accepted_exactly_when_a_manifest_could_declare_them(self):
        """A manifest can declare a key whose first segment its module owns; no module can own a reserved namespace, so those are never declarable."""
        for text in self.corpus():
            shape = accepted(module_manifest.WorkflowDeclaration, text, 1)
            reserved = text.split(".", 1)[0] in module_manifest.RESERVED_NAMESPACES
            with self.subTest(text=text):
                self.assertEqual(accepted(validate_attribution, "demo_module", text, 1), shape and not reserved, repr(text))

    def test_a_reserved_namespace_can_be_neither_a_module_id_nor_a_workflow_namespace_in_a_manifest(self):
        for namespace in sorted(attribution.RESERVED_NAMESPACES):
            with self.subTest(namespace=namespace):
                self.assertFalse(accepted(lambda value: manifest(module_id=value), namespace))
                self.assertFalse(accepted(validate_attribution, namespace, "demo.x", 1))
                self.assertFalse(accepted(validate_attribution, "demo_module", f"{namespace}.flow", 1))

    def test_the_version_limit_is_one_positive_32_bit_integer_everywhere(self):
        self.assertEqual(module_manifest.MAX_WORKFLOW_DEFINITION_VERSION, 2**31 - 1)
        self.assertEqual(MAX_WORKFLOW_VERSION, module_manifest.MAX_WORKFLOW_DEFINITION_VERSION)

    def test_versions_are_accepted_exactly_when_the_manifest_does(self):
        for version in (0, 1, 2, 10, 2**31 - 2, 2**31 - 1, 2**31, 2**31 + 1, 2**40, -1, -(2**31), True, False, "1", 1.0, None):
            declared = accepted(module_manifest.WorkflowDeclaration, "demo_module", version)
            with self.subTest(version=version):
                self.assertEqual(accepted(validate_attribution, "demo_module", "demo_module", version), declared)
                self.assertEqual(declared, isinstance(version, int) and not isinstance(version, bool) and 1 <= version <= 2**31 - 1)

    def test_a_manifest_refuses_a_version_above_the_limit_with_a_stable_code(self):
        with self.assertRaises(ManifestError) as caught:
            module_manifest.WorkflowDeclaration("demo_module", 2**31)
        self.assertEqual(caught.exception.code, "invalid_type")
        module_manifest.WorkflowDeclaration("demo_module", 2**31 - 1)
