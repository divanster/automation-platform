"""The module contract (MOD-1a): manifests, versions and a static registry. Pure Python, standard library only.

This package is NOT a Django app: it has no models, no migrations, no settings entry and no URLs, and it does not import Django,
any other ``apps.*`` package, or any business workflow package. It can be imported, and its tests run, with plain ``unittest``.

* ``manifest``: immutable, self-validating manifest v1 dataclasses.
* ``versions``: a documented SemVer subset, version ranges, the Core API version.
* ``registry``: an immutable, deterministic registry built from manifest objects.
* ``errors``: the stable error codes.

Nothing here discovers, imports, installs, enables or runs a module. The only place that names built-in modules is the composition
root ``config/modules.py``. Design and limits: docs/architecture/module-contract.md.
"""

from .errors import ERROR_CODES, ManifestError, ModuleContractError, RegistryError, VersionError
from .manifest import (
    MANIFEST_VERSION,
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
from .registry import ModuleRegistry
from .versions import CORE_API_VERSION, UPDATER_PROTOCOL_VERSION, Version, VersionRange, parse_range, parse_version

__all__ = [
    "ERROR_CODES", "ManifestError", "ModuleContractError", "RegistryError", "VersionError",
    "MANIFEST_VERSION", "ApiContribution", "AuditActionDeclaration", "ConfigField", "ConfigSchema", "ModuleDependency",
    "ModuleKind", "ModuleManifest", "PermissionDeclaration", "TaskHandlerDeclaration", "UiContribution", "UpdateMetadata",
    "WorkflowDeclaration", "ModuleRegistry",
    "CORE_API_VERSION", "UPDATER_PROTOCOL_VERSION", "Version", "VersionRange", "parse_range", "parse_version",
]  # fmt: skip
