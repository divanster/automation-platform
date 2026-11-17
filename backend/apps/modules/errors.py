"""Errors of the module contract. Pure Python, standard library only.

Every error carries a STABLE ``code`` (a short lowercase token, listed in ``ERROR_CODES``) and a message that names the field or
rule that was broken. A message never echoes an arbitrary submitted value; where it names something, that thing is a module id,
namespace or other identifier that has already passed validation. Callers and tests decide from ``code``, never from the text.

``ModuleContractError`` is a ``ValueError`` (like the other pure-domain errors in this project), so an invalid manifest is an
ordinary "bad value" and not an unexpected failure.
"""

# Codes of ``VersionError`` (versions.py).
VERSION_CODES = frozenset({"invalid_version", "invalid_range", "empty_range", "too_many_comparators"})

# Codes of ``ManifestError`` (manifest.py). Several ``duplicate_*`` codes are also raised by the registry for the same kind of
# clash between two modules.
MANIFEST_CODES = frozenset(
    {
        "unsupported_manifest_version",
        "invalid_type",
        "invalid_identifier",
        "invalid_label",
        "invalid_module_kind",
        "invalid_module_version",
        "invalid_core_api_range",
        "unbounded_core_api_range",
        "invalid_dependency_range",
        "reserved_namespace",
        "reserved_api_route",
        "identifier_outside_namespace",
        "self_dependency",
        "capability_required_and_provided",
        "workflow_not_allowed_for_kind",
        "workflow_task_kind_undeclared",
        "invalid_handler",
        "handler_kind_mismatch",
        "invalid_config_field",
        "invalid_config_schema",
        "invalid_update_metadata",
        "too_many_entries",
        "duplicate_dependency",
        "duplicate_capability",
        "duplicate_task_kind",
        "duplicate_workflow",
        "duplicate_permission",
        "duplicate_audit_action",
        "forbidden_audit_detail_key",
        "duplicate_ui_contribution",
        "duplicate_api_route",
        "duplicate_config_field",
        "unknown_role",
        "duplicate_role_grant",
        "empty_role_grant",
        "role_grant_undeclared_permission",
        "duplicate_maintenance_hook",
    }
)

# Codes of ``RegistryError`` (registry.py).
REGISTRY_CODES = frozenset(
    {
        "invalid_manifest_object",
        "duplicate_module_id",
        "duplicate_namespace",
        "duplicate_ui_route",
        "incompatible_core_api",
        "unsupported_updater_protocol",
        "missing_dependency",
        "incompatible_dependency_version",
        "dependency_cycle",
        "missing_capability",
        "capability_not_yet_available",
        "capability_provider_not_dependency",
        "undeclared_ui_permission",
        "unknown_module",
        "invalid_core_grants",
        "invalid_namespace_history",
        "namespace_released",
        "namespace_ownership_changed",
    }
)

ERROR_CODES = VERSION_CODES | MANIFEST_CODES | REGISTRY_CODES


class ModuleContractError(ValueError):
    """Base class. ``code`` is stable; the message names the rule, never a submitted value."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class VersionError(ModuleContractError):
    """A version or a version range is not in the supported SemVer subset (see versions.py)."""


class ManifestError(ModuleContractError):
    """A manifest, or a declaration inside it, is invalid. Raised when the object is built, so an invalid manifest never exists."""


class RegistryError(ModuleContractError):
    """A set of manifests cannot form a registry (clashes, missing or incompatible dependencies, cycles, capabilities)."""
