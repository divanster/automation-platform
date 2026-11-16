"""Shared helpers for the module-contract tests. Pure Python: no Django, no database. Test only; never imported by the application."""

import unittest
from dataclasses import dataclass

from apps.modules.manifest import (
    ApiContribution,
    AuditActionDeclaration,
    ModuleDependency,
    ModuleManifest,
    PermissionDeclaration,
    TaskHandlerDeclaration,
    UiContribution,
    WorkflowDeclaration,
)

BASE = {
    "manifest_version": 1,
    "module_id": "demo_module",
    "module_kind": "business",
    "module_version": "1.0.0",
    "core_api_compatibility": ">=1.0.0,<2.0.0",
    "license_feature": "mod.demo_module",
}


def manifest(**overrides) -> ModuleManifest:
    """A valid manifest; ``overrides`` replace or add fields. The license feature follows the module id unless given."""
    values = dict(BASE)
    if "module_id" in overrides and "license_feature" not in overrides:
        values["license_feature"] = f"mod.{overrides['module_id']}"
    values.update(overrides)
    return ModuleManifest(**values)


def named(module_id: str, **overrides) -> ModuleManifest:
    return manifest(module_id=module_id, **overrides)


def dep(module_id: str, version_range: str = ">=1.0.0,<2.0.0") -> ModuleDependency:
    return ModuleDependency(module_id, version_range)


@dataclass(frozen=True)
class FakeHandler:
    """Has the shape of ``apps.tasks.handlers.TaskHandler`` without importing it."""

    kind: str
    validate: object = staticmethod(lambda payload: payload)
    run: object = staticmethod(lambda payload: {})
    is_test_handler: bool = False


def full_manifest(module_id: str = "full_module", **overrides) -> ModuleManifest:
    """A manifest that uses every optional declaration, all inside the module's own namespace."""
    from apps.modules.manifest import ConfigField, ConfigSchema, UpdateMetadata

    ns = module_id
    values = dict(
        module_id=module_id,
        license_feature=f"mod.{module_id}",
        display_name="Full module",
        extra_namespaces=("legacy_ns",),
        capabilities_required=("task.enqueue", "audit.record"),
        capabilities_provided=(f"{ns}.service",),
        workflows=(WorkflowDeclaration(ns, 1, (f"{ns}.run",), "a workflow"),),
        task_handlers=(TaskHandlerDeclaration(f"{ns}.run", FakeHandler(f"{ns}.run"), "runs it"),),
        permissions=(
            PermissionDeclaration(f"{ns}.do_it", "own", "do it"),
            PermissionDeclaration("legacy_ns.view_it", "any"),
        ),
        audit_actions=(AuditActionDeclaration(f"{ns}.did_it", ("count", "reason_code")),),
        configuration_schema=ConfigSchema(
            1,
            (
                ConfigField("enabled_flag", "bool", default=True),
                ConfigField("batch_size", "int", default=10, minimum=1, maximum=100),
                ConfigField("label", "string", max_length=50, default="x"),
                ConfigField("mode", "enum", choices=("fast", "safe"), default="safe"),
                ConfigField("api_secret", "secret_ref", required=True),
            ),
        ),
        ui_contributions=(
            UiContribution(f"{ns}.screen", "route", f"/{ns}", "Screen", f"{ns}.do_it"),
            UiContribution(f"{ns}.nav", "navigation", f"/{ns}", "Full module", "audit.view_auditevent"),
        ),
        api_contributions=(ApiContribution("full-module", "the api"),),
        update=UpdateMetadata(data_schema_version=3, minimum_updater_protocol=None),
    )
    values.update(overrides)
    return manifest(**values)


class ContractTestCase(unittest.TestCase):
    def assertCode(self, code, error_type, function, *args, **kwargs):
        """``function(...)`` raises ``error_type`` with this stable ``code``."""
        with self.assertRaises(error_type) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        return caught.exception
