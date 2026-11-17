"""Pure-domain tests (standard-library unittest, no Django, no database) for the module contract: versions and manifests.

Registry behavior is in ``tests/test_module_registry.py`` and the architecture boundaries are in ``tests/test_module_boundaries.py``.
"""

import ast
import dataclasses
import hashlib
import json
import unittest
from pathlib import Path

from apps.modules import errors as module_errors
from apps.modules.errors import ERROR_CODES, ManifestError, ModuleContractError, VersionError
from apps.modules.manifest import (
    RESERVED_NAMESPACES,
    ApiContribution,
    AuditActionDeclaration,
    ConfigField,
    ConfigSchema,
    ModuleDependency,
    ModuleKind,
    ModuleManifest,
    PermissionDeclaration,
    TaskHandlerDeclaration,
    UiContribution,
    UpdateMetadata,
    WorkflowDeclaration,
)
from apps.modules.versions import CORE_API_VERSION, Version, is_core_api_compatible, parse_range, parse_version

from .module_support import FakeHandler, ContractTestCase, dep, full_manifest, manifest

MODULES_DIR = Path(__file__).resolve().parent.parent / "apps" / "modules"


class VersionParsingTests(ContractTestCase):
    def test_valid_versions(self):
        for text, expected in [("0.0.0", (0, 0, 0)), ("1.2.3", (1, 2, 3)), ("10.20.30", (10, 20, 30)), ("999999999.0.1", (999999999, 0, 1))]:
            with self.subTest(text=text):
                self.assertEqual(parse_version(text), Version(*expected))
                self.assertEqual(str(parse_version(text)), text)

    def test_invalid_versions_are_refused_not_repaired(self):
        bad = [
            "", "1", "1.2", "1.2.3.4", "01.2.3", "1.02.3", "1.2.03", "v1.2.3", " 1.2.3", "1.2.3 ", "1.2.3\n", "1.2.3-beta",
            "1.2.3+5", "1.2.x", "-1.2.3", "1.2.3.", "1,2,3", "1000000000.0.0", "١.٢.٣", None, 123, b"1.2.3", ("1", "2", "3"), 1.5,
        ]  # fmt: skip
        for text in bad:
            with self.subTest(text=text):
                self.assertCode("invalid_version", VersionError, parse_version, text)

    def test_versions_compare_numerically_not_as_text(self):
        self.assertGreater(parse_version("1.10.0"), parse_version("1.9.0"))
        self.assertGreater(parse_version("2.0.0"), parse_version("1.99.99"))
        self.assertLess(parse_version("1.2.3"), parse_version("1.2.4"))
        self.assertEqual(parse_version("1.2.3"), parse_version("1.2.3"))
        shuffled = [parse_version(t) for t in ("1.10.0", "1.2.0", "0.9.9", "1.9.0")]
        self.assertEqual([str(v) for v in sorted(shuffled)], ["0.9.9", "1.2.0", "1.9.0", "1.10.0"])


class VersionRangeTests(ContractTestCase):
    def test_ranges_are_normalized_to_one_canonical_text(self):
        cases = {
            ">=1.0.0,<2.0.0": ">=1.0.0,<2.0.0",
            ">=1.0.0, <2.0.0": ">=1.0.0,<2.0.0",
            "<2.0.0,>=1.0.0": ">=1.0.0,<2.0.0",
            ">= 1.0.0": ">=1.0.0",
            "==1.2.3": "==1.2.3",
            ">1.0.0,<=2.0.0": ">1.0.0,<=2.0.0",
            ">=1.0.0,>=1.5.0,<3.0.0,<2.0.0": ">=1.5.0,<2.0.0",
            ">=1.0.0,>1.0.0": ">1.0.0",
            "<=2.0.0,<2.0.0": "<2.0.0",
            ">=1.2.3,<=1.2.3": "==1.2.3",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(str(parse_range(text)), expected)

    def test_invalid_ranges(self):
        for text in ["", ",", ">=1.0.0,", "1.0.0", "^1.0.0", "~1.0.0", ">=1.0", ">=1.0.0 <2.0.0", ">=1.0.0||>=2.0.0", "=1.0.0", ">=1.0.0-beta", ">=*", None, 5, ">=1.0.0," + "x" * 70]:
            with self.subTest(text=text):
                self.assertCode("invalid_range", VersionError, parse_range, text)

    def test_more_than_four_comparators_are_refused(self):
        self.assertCode("too_many_comparators", VersionError, parse_range, ">=1.0.0,>=1.0.1,>=1.0.2,>=1.0.3,>=1.0.4")

    def test_a_range_nothing_can_satisfy_is_refused(self):
        for text in [">=2.0.0,<1.0.0", ">1.0.0,<1.0.0", ">=1.0.0,<1.0.0", "==1.0.0,==2.0.0", ">1.0.0,<=1.0.0"]:
            with self.subTest(text=text):
                self.assertCode("empty_range", VersionError, parse_range, text)

    def test_containment_at_the_boundaries(self):
        table = {
            ">=1.2.0,<2.0.0": {"0.0.0": False, "1.1.9": False, "1.2.0": True, "1.9.9": True, "2.0.0": False, "2.0.1": False},
            "==1.2.3": {"1.2.2": False, "1.2.3": True, "1.2.4": False},
            ">1.0.0": {"1.0.0": False, "1.0.1": True, "99.0.0": True},
            "<=1.0.0": {"0.0.0": True, "1.0.0": True, "1.0.1": False},
        }
        for text, cases in table.items():
            for version, expected in cases.items():
                with self.subTest(range=text, version=version):
                    self.assertIs(parse_range(text).contains(parse_version(version)), expected)

    def test_only_a_version_can_be_tested(self):
        with self.assertRaises(TypeError):
            parse_range(">=1.0.0").contains("1.0.0")

    def test_upper_bound_detection(self):
        self.assertTrue(parse_range(">=1.0.0,<2.0.0").is_bounded_above)
        self.assertTrue(parse_range("==1.0.0").is_bounded_above)
        self.assertFalse(parse_range(">=1.0.0").is_bounded_above)

    def test_core_api_compatibility_check(self):
        self.assertTrue(is_core_api_compatible(parse_range(">=1.0.0,<2.0.0")))
        self.assertTrue(is_core_api_compatible(parse_range(">=1.0.0,<2.0.0"), Version(1, 9, 9)))
        self.assertFalse(is_core_api_compatible(parse_range(">=1.0.0,<2.0.0"), Version(2, 0, 0)))
        self.assertFalse(is_core_api_compatible(parse_range(">=2.0.0,<3.0.0")))
        self.assertEqual(CORE_API_VERSION, Version(1, 0, 0))


class ManifestConstructionTests(ContractTestCase):
    def test_a_minimal_manifest_is_valid_and_normalized(self):
        m = manifest(core_api_compatibility=">=1.0.0, <2.0.0")
        self.assertEqual(m.module_id, "demo_module")
        self.assertIs(m.module_kind, ModuleKind.BUSINESS)
        self.assertEqual(m.core_api_compatibility, ">=1.0.0,<2.0.0")
        self.assertEqual(m.version, Version(1, 0, 0))
        self.assertEqual(m.namespaces, ("demo_module",))
        for name in ("dependencies", "capabilities_required", "capabilities_provided", "workflows", "task_handlers", "permissions", "audit_actions", "ui_contributions", "api_contributions", "extra_namespaces"):
            self.assertEqual(getattr(m, name), ())
        self.assertIsNone(m.configuration_schema)
        self.assertEqual(m.update, UpdateMetadata())

    def test_the_six_required_fields_are_required(self):
        required = {f.name for f in dataclasses.fields(ModuleManifest) if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING}
        self.assertEqual(required, {"manifest_version", "module_id", "module_kind", "module_version", "core_api_compatibility", "license_feature"})

    def test_a_manifest_using_every_declaration_is_valid(self):
        m = full_manifest()
        self.assertEqual(m.namespaces, ("full_module", "legacy_ns"))
        self.assertEqual(len(m.permissions), 2)
        self.assertEqual(m.update.data_schema_version, 3)

    def test_every_kind_is_accepted_as_enum_or_text(self):
        for kind in ModuleKind:
            self.assertIs(manifest(module_kind=kind).module_kind, kind)
            self.assertIs(manifest(module_kind=kind.value).module_kind, kind)

    def test_a_built_manifest_cannot_be_changed(self):
        m = manifest()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            m.module_id = "other_module"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            m.permissions = ()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            m.update.data_schema_version = 2

    def test_collections_are_copied_into_tuples(self):
        permissions = [PermissionDeclaration("demo_module.a"), PermissionDeclaration("demo_module.b")]
        m = manifest(permissions=permissions)
        self.assertIsInstance(m.permissions, tuple)
        permissions.append(PermissionDeclaration("demo_module.c"))
        self.assertEqual(len(m.permissions), 2)

    def test_unsupported_manifest_versions_are_refused(self):
        for value in (0, 2, 99, -1, True, "1", 1.0, None):
            with self.subTest(value=value):
                self.assertCode("unsupported_manifest_version", ManifestError, manifest, manifest_version=value)

    def test_module_ids(self):
        for bad in ["Demo_module", "ab", "1abc", "a-b", "x" * 41, "", "demo module", "démo"]:
            with self.subTest(module_id=bad):
                self.assertCode("invalid_identifier", ManifestError, manifest, module_id=bad, license_feature="mod.demo_module")
        for bad in (None, 5, b"demo_module"):
            with self.subTest(module_id=bad):
                self.assertCode("invalid_type", ManifestError, manifest, module_id=bad, license_feature="mod.demo_module")
        for reserved in ("core", "test", "audit", "run", "revision", "task", "case", "secrets", "platform"):
            with self.subTest(reserved=reserved):
                self.assertCode("reserved_namespace", ManifestError, manifest, module_id=reserved, license_feature="mod.x_feature")

    def test_module_kind_must_be_known(self):
        for bad in ("plugin", "BUSINESS", "", None, 3):
            with self.subTest(kind=bad):
                self.assertCode("invalid_module_kind", ManifestError, manifest, module_kind=bad)

    def test_module_version_must_be_a_valid_version(self):
        for bad in ("1.0", "v1.0.0", "1.0.0-rc1", "", None, 1):
            with self.subTest(version=bad):
                self.assertCode("invalid_module_version", ManifestError, manifest, module_version=bad)

    def test_core_api_compatibility(self):
        self.assertCode("invalid_core_api_range", ManifestError, manifest, core_api_compatibility="1.0.0")
        self.assertCode("invalid_core_api_range", ManifestError, manifest, core_api_compatibility=">=2.0.0,<1.0.0")
        self.assertCode("invalid_core_api_range", ManifestError, manifest, core_api_compatibility=None)
        self.assertCode("unbounded_core_api_range", ManifestError, manifest, core_api_compatibility=">=1.0.0")
        self.assertEqual(manifest(core_api_compatibility="==1.0.0").core_api_compatibility, "==1.0.0")

    def test_license_feature_is_a_dotted_identifier(self):
        for bad in ("demo", "Mod.demo", "mod.", ".demo", "mod demo", "mod.demo!", ""):
            with self.subTest(feature=bad):
                self.assertCode("invalid_identifier", ManifestError, manifest, license_feature=bad)
        self.assertEqual(manifest(license_feature="mod.demo_module.advanced").license_feature, "mod.demo_module.advanced")

    def test_display_name_is_printable_and_bounded(self):
        self.assertCode("invalid_label", ManifestError, manifest, display_name="two\nlines")
        self.assertCode("invalid_label", ManifestError, manifest, display_name="x" * 81)
        self.assertCode("invalid_type", ManifestError, manifest, display_name=5)

    def test_extra_namespaces(self):
        self.assertEqual(manifest(extra_namespaces=["zeta", "alpha"]).extra_namespaces, ("alpha", "zeta"))
        for reserved in ("audit", "core", "run"):
            self.assertCode("reserved_namespace", ManifestError, manifest, extra_namespaces=(reserved,))
        self.assertCode("invalid_identifier", ManifestError, manifest, extra_namespaces=("Bad",))
        self.assertCode("invalid_identifier", ManifestError, manifest, extra_namespaces=("alpha", "alpha"))
        self.assertCode("invalid_identifier", ManifestError, manifest, extra_namespaces=("demo_module",))
        self.assertCode("invalid_type", ManifestError, manifest, extra_namespaces="alpha")

    def test_the_reserved_namespaces_cover_the_core_vocabulary(self):
        for name in ("run", "revision", "approval", "rejection", "request", "audit", "task", "case", "documents", "ai", "secrets", "core", "test"):
            self.assertIn(name, RESERVED_NAMESPACES)


class ExtraNamespaceOwnershipTests(ContractTestCase):
    """Owner decision: extra namespaces preserve legacy identifiers, but only by explicit declaration, with no implicit inheritance."""

    def test_a_module_owns_only_its_id_unless_it_declares_more(self):
        self.assertEqual(manifest().namespaces, ("demo_module",))
        self.assertEqual(manifest(extra_namespaces=("legacy_ns",)).namespaces, ("demo_module", "legacy_ns"))

    def test_the_module_id_is_always_owned_and_cannot_be_dropped_or_redeclared(self):
        self.assertIn("demo_module", manifest(extra_namespaces=("legacy_ns",)).namespaces)
        self.assertCode("invalid_identifier", ManifestError, manifest, extra_namespaces=("demo_module",))

    def test_an_identifier_in_an_extra_namespace_is_accepted_only_when_that_namespace_is_declared(self):
        legacy = PermissionDeclaration("legacy_ns.view_it")
        self.assertCode("identifier_outside_namespace", ManifestError, manifest, permissions=(legacy,))
        self.assertEqual(manifest(extra_namespaces=("legacy_ns",), permissions=(legacy,)).permissions, (legacy,))

    def test_ownership_matches_the_first_segment_exactly_so_nothing_leaks_by_prefix(self):
        owns_intake = dict(extra_namespaces=("intake",))
        for lookalike in ("intake_extra.run", "intakes.run", "in.run", "request.run", "intake2.run"):
            with self.subTest(identifier=lookalike):
                self.assertCode("identifier_outside_namespace", ManifestError, manifest, task_handlers=(TaskHandlerDeclaration(lookalike),), **owns_intake)
        # Deeper names under an owned first segment are the module's own.
        self.assertEqual(len(manifest(task_handlers=(TaskHandlerDeclaration("intake.sub.run"),), **owns_intake).task_handlers), 1)
        # Owning "demo_module" does not make "demo" or "demo_module_two" owned either.
        for lookalike in ("demo.run", "demo_module_two.run"):
            with self.subTest(identifier=lookalike):
                self.assertCode("identifier_outside_namespace", ManifestError, manifest, task_handlers=(TaskHandlerDeclaration(lookalike),))

    def test_a_dependency_grants_nothing_from_the_namespaces_of_what_it_depends_on(self):
        for identifier_field, declaration in (
            ("permissions", PermissionDeclaration("alpha_mod.view_it")),
            ("task_handlers", TaskHandlerDeclaration("alpha_mod.run")),
            ("audit_actions", AuditActionDeclaration("alpha_mod.did_it")),
            ("workflows", WorkflowDeclaration("alpha_mod", 1)),
            ("ui_contributions", UiContribution("alpha_mod.page", "route", "/a", "A")),
        ):
            with self.subTest(field=identifier_field):
                self.assertCode("identifier_outside_namespace", ManifestError, manifest, dependencies=(dep("alpha_mod"),), **{identifier_field: (declaration,)})
        self.assertCode("identifier_outside_namespace", ManifestError, manifest, dependencies=(dep("alpha_mod"),), capabilities_provided=("alpha_mod.service",))

    def test_declared_extra_namespaces_are_immutable_and_sorted(self):
        m = manifest(extra_namespaces=["zeta_ns", "alpha_ns"])
        self.assertEqual(m.extra_namespaces, ("alpha_ns", "zeta_ns"))
        self.assertIsInstance(m.extra_namespaces, tuple)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            m.extra_namespaces = ()
        with self.assertRaises(AttributeError):
            m.extra_namespaces.append("other_ns")  # a tuple has no append

    def test_reserved_core_namespaces_can_never_be_claimed_as_extras(self):
        for reserved in sorted(RESERVED_NAMESPACES):
            if len(reserved) < 2:
                continue
            with self.subTest(reserved=reserved):
                self.assertCode("reserved_namespace", ManifestError, manifest, extra_namespaces=(reserved,))

    def test_extra_namespaces_change_the_fingerprint_because_ownership_is_part_of_the_manifest(self):
        self.assertNotEqual(manifest().fingerprint(), manifest(extra_namespaces=("legacy_ns",)).fingerprint())


class DependencyAndCapabilityTests(ContractTestCase):
    def test_dependencies(self):
        m = manifest(dependencies=[dep("other_module", ">=1.0.0, <2.0.0")])
        self.assertEqual(m.dependencies[0].version_range, ">=1.0.0,<2.0.0")
        self.assertEqual(str(m.dependencies[0].range), ">=1.0.0,<2.0.0")
        self.assertCode("self_dependency", ManifestError, manifest, dependencies=(dep("demo_module"),))
        self.assertCode("duplicate_dependency", ManifestError, manifest, dependencies=(dep("other_module"), dep("other_module", ">=1.1.0")))
        self.assertCode("invalid_dependency_range", ManifestError, ModuleDependency, "other_module", "^1.0.0")
        self.assertCode("invalid_dependency_range", ManifestError, ModuleDependency, "other_module", ">=2.0.0,<1.0.0")
        self.assertCode("invalid_identifier", ManifestError, ModuleDependency, "Other", ">=1.0.0")
        self.assertCode("invalid_type", ManifestError, manifest, dependencies=("other_module",))

    def test_capabilities(self):
        m = manifest(capabilities_required=("task.enqueue",), capabilities_provided=("demo_module.service",))
        self.assertEqual(m.capabilities_provided, ("demo_module.service",))
        self.assertCode("capability_required_and_provided", ManifestError, manifest, capabilities_required=("demo_module.a",), capabilities_provided=("demo_module.a",))
        self.assertCode("identifier_outside_namespace", ManifestError, manifest, capabilities_provided=("other_module.service",))
        self.assertCode("identifier_outside_namespace", ManifestError, manifest, capabilities_provided=("task.enqueue",))
        self.assertCode("invalid_identifier", ManifestError, manifest, capabilities_required=("single",))
        self.assertCode("duplicate_capability", ManifestError, manifest, capabilities_required=("task.enqueue", "task.enqueue"))
        self.assertCode("duplicate_capability", ManifestError, manifest, capabilities_provided=("demo_module.a", "demo_module.a"))
        self.assertCode("invalid_type", ManifestError, manifest, capabilities_required="task.enqueue")


class TaskHandlerAndWorkflowTests(ContractTestCase):
    def test_task_kinds_are_namespaced_and_bounded(self):
        self.assertEqual(manifest(task_handlers=[TaskHandlerDeclaration("demo_module.extract")]).task_handlers[0].kind, "demo_module.extract")
        self.assertCode("identifier_outside_namespace", ManifestError, manifest, task_handlers=(TaskHandlerDeclaration("other_module.run"),))
        self.assertCode("identifier_outside_namespace", ManifestError, manifest, task_handlers=(TaskHandlerDeclaration("test.add_numbers"),))
        self.assertCode("duplicate_task_kind", ManifestError, manifest, task_handlers=(TaskHandlerDeclaration("demo_module.run"), TaskHandlerDeclaration("demo_module.run")))
        self.assertCode("invalid_identifier", ManifestError, TaskHandlerDeclaration, "single")
        self.assertCode("invalid_identifier", ManifestError, TaskHandlerDeclaration, "demo_module." + "x" * 60)
        self.assertCode("invalid_identifier", ManifestError, TaskHandlerDeclaration, "Demo.run")

    def test_a_handler_must_be_an_object_never_a_path(self):
        for path in ("apps.intake.handlers:EXTRACT_HANDLER", "apps.intake.handlers.EXTRACT_HANDLER", "C:\\plugins\\handler.py", b"bytes"):
            with self.subTest(path=path):
                self.assertCode("invalid_handler", ManifestError, TaskHandlerDeclaration, "demo_module.run", path)

    def test_a_text_or_bytes_object_is_refused_even_if_it_carries_the_handler_attributes(self):
        # A dotted path is text. Dressing it up with kind, validate and run attributes must not make it acceptable: the
        # explicit text check, not the shape check, is what keeps a path out.
        class DisguisedPath(str):
            kind = "demo_module.run"
            validate = staticmethod(lambda payload: payload)
            run = staticmethod(lambda payload: {})

        class DisguisedBytes(bytes):
            kind = "demo_module.run"
            validate = staticmethod(lambda payload: payload)
            run = staticmethod(lambda payload: {})

        self.assertCode("invalid_handler", ManifestError, TaskHandlerDeclaration, "demo_module.run", DisguisedPath("apps.intake.handlers:EXTRACT_HANDLER"))
        self.assertCode("invalid_handler", ManifestError, TaskHandlerDeclaration, "demo_module.run", DisguisedBytes(b"apps.intake.handlers"))

    def test_a_handler_needs_the_task_handler_shape(self):
        class NoRun:
            kind = "demo_module.run"
            validate = staticmethod(lambda p: p)

        class NotCallable:
            kind = "demo_module.run"
            validate = None
            run = 5

        self.assertCode("invalid_handler", ManifestError, TaskHandlerDeclaration, "demo_module.run", NoRun())
        self.assertCode("invalid_handler", ManifestError, TaskHandlerDeclaration, "demo_module.run", NotCallable())
        self.assertCode("invalid_handler", ManifestError, TaskHandlerDeclaration, "demo_module.run", object())
        self.assertCode("handler_kind_mismatch", ManifestError, TaskHandlerDeclaration, "demo_module.run", FakeHandler("demo_module.other"))
        declared = TaskHandlerDeclaration("demo_module.run", FakeHandler("demo_module.run"))
        self.assertIsNotNone(declared.handler)

    def test_workflows(self):
        task = TaskHandlerDeclaration("demo_module.run")
        ok = manifest(task_handlers=(task,), workflows=(WorkflowDeclaration("demo_module", 2, ("demo_module.run",)),))
        self.assertEqual(ok.workflows[0].definition_version, 2)
        self.assertCode("workflow_task_kind_undeclared", ManifestError, manifest, workflows=(WorkflowDeclaration("demo_module", 1, ("demo_module.run",)),))
        self.assertCode("identifier_outside_namespace", ManifestError, manifest, workflows=(WorkflowDeclaration("other_flow", 1),))
        self.assertCode("duplicate_workflow", ManifestError, manifest, workflows=(WorkflowDeclaration("demo_module", 1), WorkflowDeclaration("demo_module", 2)))
        for bad in (0, -1, True, "1", None, 1.0):
            with self.subTest(version=bad):
                self.assertCode("invalid_type", ManifestError, WorkflowDeclaration, "demo_module", bad)
        self.assertCode("duplicate_task_kind", ManifestError, WorkflowDeclaration, "demo_module", 1, ("demo_module.run", "demo_module.run"))

    def test_only_business_modules_declare_workflows(self):
        for kind in ("connector", "ai_adapter", "service"):
            with self.subTest(kind=kind):
                self.assertCode("workflow_not_allowed_for_kind", ManifestError, manifest, module_kind=kind, workflows=(WorkflowDeclaration("demo_module", 1),))
        self.assertEqual(len(manifest(module_kind="connector", task_handlers=(TaskHandlerDeclaration("demo_module.sync"),)).task_handlers), 1)


class PermissionAuditUiApiTests(ContractTestCase):
    def test_permissions(self):
        self.assertEqual(manifest(permissions=(PermissionDeclaration("demo_module.view_it", "own", "view"),)).permissions[0].scope, "own")
        self.assertCode("identifier_outside_namespace", ManifestError, manifest, permissions=(PermissionDeclaration("other_module.view_it"),))
        self.assertCode("duplicate_permission", ManifestError, manifest, permissions=(PermissionDeclaration("demo_module.a"), PermissionDeclaration("demo_module.a", "own")))
        self.assertCode("invalid_identifier", ManifestError, PermissionDeclaration, "demo_module.a.b")
        self.assertCode("invalid_identifier", ManifestError, PermissionDeclaration, "demo_module.a", "everyone")
        self.assertCode("invalid_identifier", ManifestError, PermissionDeclaration, "demo_module")

    def test_audit_actions_respect_the_database_column_length(self):
        ok = AuditActionDeclaration("demo_module.did_it", ("count",))
        self.assertEqual(ok.detail_keys, ("count",))
        self.assertCode("invalid_identifier", ManifestError, AuditActionDeclaration, "demo_module." + "x" * 30)  # over 32 characters
        self.assertCode("identifier_outside_namespace", ManifestError, manifest, audit_actions=(AuditActionDeclaration("run.state_changed"),))
        self.assertCode("duplicate_audit_action", ManifestError, manifest, audit_actions=(AuditActionDeclaration("demo_module.a"), AuditActionDeclaration("demo_module.a")))
        self.assertCode("invalid_identifier", ManifestError, AuditActionDeclaration, "demo_module.a", ("Bad Key",))
        self.assertCode("invalid_identifier", ManifestError, AuditActionDeclaration, "demo_module.a", ("k", "k"))
        self.assertCode("too_many_entries", ManifestError, AuditActionDeclaration, "demo_module.a", tuple(f"k{i}" for i in range(21)))

    def test_ui_contributions(self):
        ok = UiContribution("demo_module.list", "route", "/demo/:id", "Demo", "demo_module.view")
        self.assertEqual(ok.path, "/demo/:id")
        self.assertEqual(UiContribution("demo_module.home", "navigation", "/", "Home").permission, None)
        for bad_path in ("demo", "/Demo", "/demo/", "/de mo", "/" + "a" * 120, "", None):
            with self.subTest(path=bad_path):
                self.assertCode("invalid_identifier", ManifestError, UiContribution, "demo_module.x", "route", bad_path, "Label")
        self.assertCode("invalid_identifier", ManifestError, UiContribution, "demo_module.x", "page", "/x", "Label")
        self.assertCode("invalid_label", ManifestError, UiContribution, "demo_module.x", "route", "/x", "")
        self.assertCode("invalid_identifier", ManifestError, UiContribution, "demo_module.x", "route", "/x", "Label", "bad permission")
        self.assertCode("identifier_outside_namespace", ManifestError, manifest, ui_contributions=(UiContribution("other_module.x", "route", "/x", "L"),))
        self.assertCode("duplicate_ui_contribution", ManifestError, manifest, ui_contributions=(UiContribution("demo_module.x", "route", "/x", "L"), UiContribution("demo_module.x", "navigation", "/x", "L")))

    def test_api_routes(self):
        self.assertEqual(manifest(api_contributions=(ApiContribution("requests"),)).api_contributions[0].route_prefix, "requests")
        for reserved in ("auth", "health", "modules", "admin"):
            with self.subTest(prefix=reserved):
                self.assertCode("reserved_api_route", ManifestError, ApiContribution, reserved)
        self.assertCode("invalid_identifier", ManifestError, ApiContribution, "Requests")
        self.assertCode("invalid_identifier", ManifestError, ApiContribution, "a/b")
        self.assertCode("duplicate_api_route", ManifestError, manifest, api_contributions=(ApiContribution("same"), ApiContribution("same")))

    def test_collections_are_bounded(self):
        many = tuple(PermissionDeclaration(f"demo_module.p{i}") for i in range(101))
        self.assertCode("too_many_entries", ManifestError, manifest, permissions=many)


class ConfigurationAndUpdateMetadataTests(ContractTestCase):
    def test_a_valid_schema(self):
        schema = ConfigSchema(2, (ConfigField("size", "int", default=5, minimum=1, maximum=9), ConfigField("name", "string"), ConfigField("tok", "secret_ref", required=True)))
        self.assertEqual(schema.fields[1].max_length, 200)
        self.assertEqual(manifest(configuration_schema=schema).configuration_schema.schema_version, 2)

    def test_invalid_fields(self):
        bad = [
            dict(name="x", field_type="float"),
            dict(name="x", field_type="bool", default="yes"),
            dict(name="x", field_type="int", default=True),
            dict(name="x", field_type="int", default=11, minimum=1, maximum=10),
            dict(name="x", field_type="int", minimum=5, maximum=1),
            dict(name="x", field_type="int", minimum=1.5),
            dict(name="x", field_type="string", minimum=1),
            dict(name="x", field_type="string", default="abc", max_length=2),
            dict(name="x", field_type="string", max_length=0),
            dict(name="x", field_type="string", max_length=5000),
            dict(name="x", field_type="bool", max_length=5),
            dict(name="x", field_type="enum"),
            dict(name="x", field_type="enum", choices=("a", "a")),
            dict(name="x", field_type="enum", choices=("a b",)),
            dict(name="x", field_type="enum", choices=("a",), default="b"),
            dict(name="x", field_type="string", choices=("a",)),
            dict(name="x", field_type="secret_ref", default="hunter2"),
            dict(name="x", field_type="bool", required="yes"),
        ]
        for kwargs in bad:
            with self.subTest(kwargs=kwargs):
                self.assertCode("invalid_config_field", ManifestError, ConfigField, **kwargs)

    def test_a_field_name_must_be_a_valid_identifier(self):
        self.assertCode("invalid_identifier", ManifestError, ConfigField, "Bad", "int")
        self.assertCode("invalid_identifier", ManifestError, ConfigField, "has space", "int")

    def test_schema_rules(self):
        self.assertCode("duplicate_config_field", ManifestError, ConfigSchema, 1, (ConfigField("a", "bool"), ConfigField("a", "int")))
        for version in (0, -1, True, "1", None):
            with self.subTest(version=version):
                self.assertCode("invalid_config_schema", ManifestError, ConfigSchema, version)
        self.assertCode("too_many_entries", ManifestError, ConfigSchema, 1, tuple(ConfigField(f"f{i}", "bool") for i in range(51)))
        self.assertCode("invalid_type", ManifestError, manifest, configuration_schema={"fields": []})

    def test_a_secret_reference_cannot_carry_a_value(self):
        self.assertCode("invalid_config_field", ManifestError, ConfigField, "api_secret", "secret_ref", default="s3cr3t")
        self.assertIsNone(ConfigField("api_secret", "secret_ref").default)

    def test_update_metadata(self):
        self.assertEqual(manifest(update=UpdateMetadata(2, 1)).update.minimum_updater_protocol, 1)
        for bad in (0, -1, True, "1", 1.5):
            for name in ("data_schema_version", "minimum_updater_protocol"):
                with self.subTest(name=name, value=bad):
                    self.assertCode("invalid_update_metadata", ManifestError, UpdateMetadata, **{name: bad})
        self.assertCode("invalid_type", ManifestError, manifest, update={"data_schema_version": 1})


class CanonicalFormAndFingerprintTests(ContractTestCase):
    def test_the_fingerprint_is_the_sha256_of_the_canonical_json(self):
        m = full_manifest()
        expected = hashlib.sha256(json.dumps(m.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()
        self.assertEqual(m.fingerprint(), expected)
        self.assertEqual(len(m.fingerprint()), 64)

    def test_the_canonical_form_is_json_and_holds_no_handler_object(self):
        m = full_manifest()
        text = m.canonical_json()
        json.loads(text)
        self.assertNotIn("FakeHandler", text)
        self.assertIn('"has_handler":true', text)

    def test_declaration_order_does_not_change_the_fingerprint(self):
        a = manifest(permissions=(PermissionDeclaration("demo_module.a"), PermissionDeclaration("demo_module.b")), capabilities_required=("task.enqueue", "audit.record"))
        b = manifest(permissions=(PermissionDeclaration("demo_module.b"), PermissionDeclaration("demo_module.a")), capabilities_required=("audit.record", "task.enqueue"))
        self.assertEqual(a.fingerprint(), b.fingerprint())

    def test_equivalent_ranges_give_the_same_fingerprint(self):
        a = manifest(core_api_compatibility=">=1.0.0, <2.0.0")
        b = manifest(core_api_compatibility="<2.0.0,>=1.0.0")
        self.assertEqual(a.fingerprint(), b.fingerprint())

    def test_any_content_change_changes_the_fingerprint(self):
        base = manifest().fingerprint()
        changes = [
            dict(module_version="1.0.1"),
            dict(display_name="Name"),
            dict(license_feature="mod.other_feature"),
            dict(core_api_compatibility=">=1.0.0,<3.0.0"),
            dict(module_kind="connector"),
            dict(permissions=(PermissionDeclaration("demo_module.a"),)),
            dict(update=UpdateMetadata(data_schema_version=1)),
        ]
        seen = {base}
        for change in changes:
            with self.subTest(change=change):
                fingerprint = manifest(**change).fingerprint()
                self.assertNotIn(fingerprint, seen)
                seen.add(fingerprint)

    def test_a_declared_handler_object_is_not_part_of_the_fingerprint_but_its_presence_is(self):
        without = manifest(task_handlers=(TaskHandlerDeclaration("demo_module.run"),)).fingerprint()
        with_a = manifest(task_handlers=(TaskHandlerDeclaration("demo_module.run", FakeHandler("demo_module.run")),)).fingerprint()
        with_b = manifest(task_handlers=(TaskHandlerDeclaration("demo_module.run", FakeHandler("demo_module.run", is_test_handler=True)),)).fingerprint()
        self.assertNotEqual(without, with_a)
        self.assertEqual(with_a, with_b)


class NoExecutablePathTests(ContractTestCase):
    """The contract has no field through which a path, command or URL to code could be supplied."""

    FORBIDDEN = {"import_path", "entry_point", "entrypoint", "module_path", "script", "command", "url", "callable", "dotted_path", "executable", "path_to_code", "package_path"}

    def test_no_manifest_field_can_hold_an_import_path_or_command(self):
        classes = [ModuleManifest, ModuleDependency, TaskHandlerDeclaration, WorkflowDeclaration, PermissionDeclaration, AuditActionDeclaration, UiContribution, ApiContribution, ConfigField, ConfigSchema, UpdateMetadata]
        for cls in classes:
            for f in dataclasses.fields(cls):
                with self.subTest(cls=cls.__name__, field=f.name):
                    self.assertNotIn(f.name, self.FORBIDDEN)

    def test_the_only_object_valued_field_is_the_task_handler(self):
        object_valued = []
        for cls in (ModuleManifest, ModuleDependency, TaskHandlerDeclaration, WorkflowDeclaration, PermissionDeclaration, AuditActionDeclaration, UiContribution, ApiContribution, ConfigField, ConfigSchema, UpdateMetadata):
            for f in dataclasses.fields(cls):
                if "object" in str(f.type):
                    object_valued.append(f"{cls.__name__}.{f.name}")
        self.assertEqual(sorted(object_valued), ["ConfigField.default", "TaskHandlerDeclaration.handler"])  # a config default is a plain bool, int or text

    def test_the_package_never_imports_or_executes_anything_dynamically(self):
        forbidden_names = {"__import__", "import_module", "exec", "eval", "run_path", "spec_from_file_location", "iter_modules", "walk_packages", "getattr_static"}
        forbidden_builtins = {"compile", "open", "input", "globals", "locals", "vars"}  # as bare names; re.compile is a method and is fine
        forbidden_modules = {"importlib", "pkgutil", "runpy", "subprocess", "os", "glob", "ctypes", "pickle", "marshal", "shutil", "socket", "urllib", "http"}
        for path in MODULES_DIR.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(alias.name.split(".")[0], forbidden_modules, f"{path.name}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    self.assertNotIn(node.module.split(".")[0], forbidden_modules, f"{path.name}: from {node.module}")
                elif isinstance(node, ast.Name):
                    self.assertNotIn(node.id, forbidden_names | forbidden_builtins, f"{path.name}: {node.id}")
                elif isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, forbidden_names, f"{path.name}: .{node.attr}")


class ImmutabilityAndDeclarativeMetadataTests(ContractTestCase):
    def test_every_dataclass_in_the_package_is_frozen(self):
        import importlib

        found = []
        for module_name in ("apps.modules.manifest", "apps.modules.versions"):
            module = importlib.import_module(module_name)
            for name, obj in vars(module).items():
                if isinstance(obj, type) and dataclasses.is_dataclass(obj) and obj.__module__ == module_name:
                    found.append(name)
                    self.assertTrue(obj.__dataclass_params__.frozen, f"{name} is not frozen")
        self.assertGreaterEqual(len(found), 12)
        self.assertIn("ModuleManifest", found)
        self.assertIn("Version", found)
        self.assertIn("VersionRange", found)

    def test_update_metadata_is_two_optional_integers_and_nothing_more(self):
        self.assertEqual({f.name for f in dataclasses.fields(UpdateMetadata)}, {"data_schema_version", "minimum_updater_protocol"})
        self.assertEqual(UpdateMetadata(), UpdateMetadata(None, None))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            UpdateMetadata().data_schema_version = 2

    def test_nothing_acts_on_update_metadata_except_the_updater_protocol_comparison(self):
        # The registry reads exactly one update field; the data schema version and the workflow definition versions are carried, not used.
        source = (MODULES_DIR / "registry.py").read_text(encoding="utf-8")
        self.assertIn("minimum_updater_protocol", source)
        self.assertNotIn("data_schema_version", source)
        self.assertNotIn("definition_version", source)
        for path in MODULES_DIR.glob("*.py"):
            if path.name in ("manifest.py", "errors.py"):
                continue
            self.assertNotIn("data_schema_version", path.read_text(encoding="utf-8"), path.name)


class ErrorCodeTests(unittest.TestCase):
    def test_every_code_used_in_the_package_is_declared_and_every_declared_code_is_used(self):
        used = set()
        for path in MODULES_DIR.glob("*.py"):
            if path.name == "errors.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                index = {"ManifestError": 0, "VersionError": 0, "RegistryError": 0, "_claim": 3, "_unique": 1}.get(name)
                if index is not None and len(node.args) > index and isinstance(node.args[index], ast.Constant):
                    used.add(node.args[index].value)
        self.assertEqual(used - ERROR_CODES, set(), "codes raised but not declared in errors.py")
        self.assertEqual(ERROR_CODES - used, set(), "codes declared in errors.py but never raised")

    def test_the_code_groups_do_not_overlap_except_for_shared_duplicate_codes(self):
        overlap = module_errors.MANIFEST_CODES & module_errors.REGISTRY_CODES
        self.assertEqual(overlap, set())
        self.assertTrue(issubclass(ManifestError, ModuleContractError) and issubclass(VersionError, ModuleContractError))
        self.assertTrue(issubclass(ModuleContractError, ValueError))

    def test_messages_never_echo_a_submitted_value(self):
        secret = "s3cr3t-handler-path"
        for call in (
            lambda: TaskHandlerDeclaration("demo_module.run", secret),
            lambda: manifest(module_version=secret),
            lambda: manifest(core_api_compatibility=secret),
            lambda: manifest(display_name=secret + "\n"),
            lambda: ConfigField("name", "secret_ref", default=secret),
            lambda: ModuleDependency("other_module", secret),
        ):
            with self.assertRaises(ModuleContractError) as caught:
                call()
            self.assertNotIn(secret, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
