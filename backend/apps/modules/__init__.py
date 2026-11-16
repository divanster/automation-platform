"""The module contract (MOD-1a, S1a): manifests, versions, a static registry, and the pure capability and permission contracts.

Standard library only. This package is NOT a Django app: it has no models, no migrations, no settings entry and no URLs, and it does
not import Django, any other ``apps.*`` package, or any business workflow package. It can be imported, and its tests run, with plain
``unittest``.

* ``manifest``: immutable, self-validating manifest v1 dataclasses (including permission scopes, default role grants and maintenance
  hook declarations).
* ``versions``: a documented SemVer subset, version ranges, the Core API version.
* ``registry``: an immutable, deterministic registry built from manifest objects, with read-only direct dependency inspection.
* ``roles``: a pure derivation of the default Operator, Approver and Auditor permission sets from manifests.
* ``continuity``: a pure check that namespace ownership is preserved across releases.
* ``errors``: the stable error codes.

Nothing here discovers, imports, installs, enables, enforces or runs a module. The only place that names built-in modules is the
composition root ``config/modules.py``. Design and limits: docs/architecture/module-contract.md.
"""

from .continuity import check_namespace_continuity, next_namespace_history
from .errors import ERROR_CODES, ManifestError, ModuleContractError, RegistryError, VersionError
from .manifest import (
    KNOWN_ROLES,
    MANIFEST_VERSION,
    PERMISSION_SCOPES,
    ApiContribution,
    AuditActionDeclaration,
    ConfigField,
    ConfigSchema,
    MaintenanceHookDeclaration,
    ModuleDependency,
    ModuleKind,
    ModuleManifest,
    PermissionDeclaration,
    RoleGrant,
    TaskHandlerDeclaration,
    UiContribution,
    UpdateMetadata,
    WorkflowDeclaration,
)
from .registry import CORE_CAPABILITIES, ModuleRegistry, RegisteredHook
from .roles import CORE_ROLE_GRANTS, derive_all_permissions, derive_role_permissions
from .versions import CORE_API_VERSION, UPDATER_PROTOCOL_VERSION, Version, VersionRange, parse_range, parse_version

__all__ = [
    "ERROR_CODES", "ManifestError", "ModuleContractError", "RegistryError", "VersionError",
    "KNOWN_ROLES", "MANIFEST_VERSION", "PERMISSION_SCOPES", "ApiContribution", "AuditActionDeclaration", "ConfigField",
    "ConfigSchema", "MaintenanceHookDeclaration", "ModuleDependency", "ModuleKind", "ModuleManifest", "PermissionDeclaration",
    "RoleGrant", "TaskHandlerDeclaration", "UiContribution", "UpdateMetadata", "WorkflowDeclaration",
    "CORE_CAPABILITIES", "ModuleRegistry", "RegisteredHook",
    "CORE_ROLE_GRANTS", "derive_all_permissions", "derive_role_permissions",
    "check_namespace_continuity", "next_namespace_history",
    "CORE_API_VERSION", "UPDATER_PROTOCOL_VERSION", "Version", "VersionRange", "parse_range", "parse_version",
]  # fmt: skip
