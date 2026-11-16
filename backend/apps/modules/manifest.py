"""Module manifest v1: immutable, self-validating declarations. Pure Python, standard library only.

A manifest DECLARES what a module is and what it contributes. It is data, not a loader: nothing here imports, executes, scans or
resolves anything. Every dataclass validates itself in ``__post_init__``, so an invalid manifest cannot be constructed, and every
collection is copied into a tuple, so a built manifest cannot be changed (the dataclasses are frozen).

NO EXECUTABLE PATHS. There is no field that holds a dotted import path, a file path, a command or a URL to code. The only
executable things a manifest can carry are task-handler OBJECTS (``TaskHandlerDeclaration.handler``): an object that someone
already imported and passed in, checked structurally (``kind``, ``validate``, ``run``). A string where an object is expected is
refused. Canonical form and fingerprints exclude handler objects.

IDENTIFIERS AND NAMESPACES. A module owns its ``module_id`` as a namespace, plus any ``extra_namespaces`` it declares (a built-in
module that must keep identifiers that already exist, such as an existing permission prefix, declares that prefix here). Every
task kind, permission, audit action, workflow key, capability it provides and UI contribution id must start with an owned
namespace followed by a dot. Namespaces are unique across modules (registry.py) and a fixed set is reserved for Core
(``RESERVED_NAMESPACES``), so one module cannot define, or shadow, another module's or Core's identifiers.

WHAT IS CHECKED WHERE. This file checks everything that can be decided from ONE manifest. Everything that needs other manifests
(dependencies, clashes between modules, capability providers, compatibility with this Core) is the registry's job.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum

from .errors import ManifestError, VersionError
from .versions import Version, VersionRange, parse_range, parse_version

MANIFEST_VERSION = 1
SUPPORTED_MANIFEST_VERSIONS = frozenset({MANIFEST_VERSION})

# Namespaces that belong to Core today (its audit vocabulary, run states, queue, capabilities, test fixtures) or are reserved for
# it. No module may own one of these, as its id or as an extra namespace.
RESERVED_NAMESPACES = frozenset(
    {
        "core", "test", "platform", "module", "auth", "admin", "health",
        "run", "revision", "approval", "rejection", "audit",
        "task", "case", "document", "documents", "ai", "secrets", "permissions",
    }
)  # fmt: skip

# The product roles a module may grant its permissions to. They are Core's three Django groups (apps.accounts.roles.ROLES); a test
# keeps this tuple equal to that one. A module cannot define a role: new roles are a Core decision, not a manifest field.
KNOWN_ROLES = ("Operator", "Approver", "Auditor")

# First path segments of the HTTP API that Core owns.
RESERVED_API_ROUTES = frozenset({"auth", "health", "modules", "admin"})

MAX_ENTRIES = 100  # per collection: a sanity bound, not a business limit
MAX_TASK_KIND_LENGTH = 64  # apps.tasks.models.Task.kind
MAX_AUDIT_ACTION_LENGTH = 32  # apps.audit.models.AuditEvent.action
MAX_LABEL_LENGTH = 80

_NAMESPACE = re.compile(r"[a-z][a-z0-9_]{1,39}")
_MODULE_ID = re.compile(r"[a-z][a-z0-9_]{2,39}")
_SEGMENT = r"[a-z][a-z0-9_]{0,39}"
_DOTTED = re.compile(rf"{_SEGMENT}(?:\.{_SEGMENT})*")
_DOTTED_2 = re.compile(rf"{_SEGMENT}(?:\.{_SEGMENT})+")
_PERMISSION = re.compile(rf"{_SEGMENT}\.[a-z][a-z0-9_]{{0,59}}")
_HOOK_ID = re.compile(rf"{_SEGMENT}\.{_SEGMENT}")  # exactly <namespace>.<name>: never a deeper, import-path-shaped name
_ROUTE_PREFIX = re.compile(r"[a-z][a-z0-9-]{0,39}")
_UI_PATH = re.compile(r"/(?:[a-z0-9_:-]+(?:/[a-z0-9_:-]+)*)?")
_CHOICE = re.compile(r"[A-Za-z0-9_.-]{1,40}")
_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")


class ModuleKind(str, Enum):
    """What a module is. Only ``business`` modules may declare workflows."""

    BUSINESS = "business"  # workflows, task handlers, permissions, UI
    CONNECTOR = "connector"  # moves documents or records to or from an external system; no business rules
    AI_ADAPTER = "ai_adapter"  # implements the provider-neutral AI gateway interface (the gateway itself is not built)
    SERVICE = "service"  # an external, process-isolated module (not built)


# --- small validators ----------------------------------------------------------------------------------------------------


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _identifier(value, field_name, pattern, max_length=MAX_TASK_KIND_LENGTH) -> str:
    if not isinstance(value, str):
        raise ManifestError("invalid_type", f"{field_name} must be text")
    if len(value) > max_length or pattern.fullmatch(value) is None:
        raise ManifestError("invalid_identifier", f"{field_name} is not a valid identifier")
    return value


def _label(value, field_name, max_length=MAX_LABEL_LENGTH, allow_empty=True) -> str:
    if not isinstance(value, str):
        raise ManifestError("invalid_type", f"{field_name} must be text")
    if len(value) > max_length or not value.isprintable() or (not allow_empty and not value.strip()):
        raise ManifestError("invalid_label", f"{field_name} must be printable text of at most {max_length} characters")
    return value


def _tuple_of(value, kind, field_name) -> tuple:
    if not isinstance(value, (tuple, list)):
        raise ManifestError("invalid_type", f"{field_name} must be a tuple or list")
    if len(value) > MAX_ENTRIES:
        raise ManifestError("too_many_entries", f"{field_name} has more than {MAX_ENTRIES} entries")
    for item in value:
        if not isinstance(item, kind):
            raise ManifestError("invalid_type", f"{field_name} entries must be {kind.__name__} objects")
    return tuple(value)


def _unique(values, code, field_name) -> None:
    if len(set(values)) != len(values):
        raise ManifestError(code, f"{field_name} contains a duplicate")


def _owned(identifier, namespaces, field_name) -> None:
    if identifier.split(".", 1)[0] not in namespaces:
        raise ManifestError("identifier_outside_namespace", f"{field_name} must start with a namespace the module owns followed by a dot")


def _positive_int_or_none(value, field_name) -> int | None:
    if value is None:
        return None
    if not _is_int(value) or value < 1:
        raise ManifestError("invalid_update_metadata", f"{field_name} must be a positive integer or None")
    return value


# --- declarations ----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ModuleDependency:
    """A hard dependency on another module, with the versions of it that are acceptable (``version_range``, normalized)."""

    module_id: str
    version_range: str

    def __post_init__(self):
        _identifier(self.module_id, "dependency module_id", _MODULE_ID, 40)
        try:
            parsed = parse_range(self.version_range)
        except VersionError:
            raise ManifestError("invalid_dependency_range", "a dependency version range is not valid") from None
        object.__setattr__(self, "version_range", str(parsed))

    @property
    def range(self) -> VersionRange:
        return parse_range(self.version_range)

    def to_dict(self) -> dict:
        return {"module_id": self.module_id, "version_range": self.version_range}


@dataclass(frozen=True)
class TaskHandlerDeclaration:
    """A task kind the module handles, optionally with the handler OBJECT (never a path to one).

    ``handler`` is checked structurally against the shape of ``apps.tasks.handlers.TaskHandler`` without importing it: a
    ``kind`` equal to the declared kind, and callable ``validate`` and ``run``. Registering handlers with the worker is a later
    slice; in MOD-1a a declaration is metadata.
    """

    kind: str
    handler: object | None = field(default=None, compare=False)
    description: str = ""

    def __post_init__(self):
        _identifier(self.kind, "task kind", _DOTTED_2, MAX_TASK_KIND_LENGTH)
        _label(self.description, "task handler description", 200)
        handler = self.handler
        if handler is not None:
            if isinstance(handler, (str, bytes)) or not all(hasattr(handler, name) for name in ("kind", "validate", "run")):
                raise ManifestError("invalid_handler", "a handler must be an object with kind, validate and run, never a path or text")
            if not callable(handler.validate) or not callable(handler.run):
                raise ManifestError("invalid_handler", "a handler's validate and run must be callable")
            if handler.kind != self.kind:
                raise ManifestError("handler_kind_mismatch", "the handler's kind differs from the declared task kind")

    def to_dict(self) -> dict:
        return {"kind": self.kind, "has_handler": self.handler is not None, "description": self.description}


@dataclass(frozen=True)
class WorkflowDeclaration:
    """A workflow the module provides: a stable key, an integer definition version, and the task kinds it runs."""

    workflow_key: str
    definition_version: int
    task_kinds: tuple = ()
    description: str = ""

    def __post_init__(self):
        _identifier(self.workflow_key, "workflow key", _DOTTED, MAX_TASK_KIND_LENGTH)
        if not _is_int(self.definition_version) or self.definition_version < 1:
            raise ManifestError("invalid_type", "workflow definition_version must be an integer of at least 1")
        kinds = _tuple_of(self.task_kinds, str, "workflow task_kinds")
        for kind in kinds:
            _identifier(kind, "workflow task kind", _DOTTED_2, MAX_TASK_KIND_LENGTH)
        _unique(kinds, "duplicate_task_kind", "workflow task_kinds")
        _label(self.description, "workflow description", 200)
        object.__setattr__(self, "task_kinds", kinds)

    def to_dict(self) -> dict:
        return {
            "workflow_key": self.workflow_key,
            "definition_version": self.definition_version,
            "task_kinds": sorted(self.task_kinds),
            "description": self.description,
        }


PERMISSION_SCOPES = ("global", "own", "any")


@dataclass(frozen=True)
class PermissionDeclaration:
    """A permission the module defines, as ``<namespace>.<codename>``, which is also the Django ``app_label.codename``.

    ``scope`` says what the permission reaches, and it is the vocabulary the authorization services apply:

    * ``global``: no particular record (for example "may submit a request");
    * ``own``: only records the user owns (for example "may view the requests the user submitted");
    * ``any``: any record (for example "may approve any request that is waiting for review").

    The default is ``any`` (unchanged from MOD-1a), so a declaration that wants a narrower reach must say so. A scope is a DECLARATION:
    nothing in this contract enforces it. Enforcement is the job of the authorization services, which check scope against the record.
    """

    permission_id: str
    scope: str = "any"
    description: str = ""

    def __post_init__(self):
        _identifier(self.permission_id, "permission id", _PERMISSION, 80)
        if not isinstance(self.scope, str) or self.scope not in PERMISSION_SCOPES:
            raise ManifestError("invalid_identifier", "permission scope must be one of global, own, any")
        _label(self.description, "permission description", 200)

    def to_dict(self) -> dict:
        return {"permission_id": self.permission_id, "scope": self.scope, "description": self.description}


@dataclass(frozen=True)
class RoleGrant:
    """A DEFAULT grant: these permissions go to this role. Nothing more is granted, implicitly or by scope.

    ``role`` must be one of ``KNOWN_ROLES`` (``unknown_role`` otherwise). Every permission must be declared by the SAME module
    (``role_grant_undeclared_permission``, checked by the manifest), so a module cannot grant another module's permission or one
    of Core's: Core's own grants are a separate, explicit input to the derivation (roles.py). A grant names at least one
    permission and no permission twice. It is a seed for role setup and says nothing about enforcement.
    """

    role: str
    permissions: tuple = ()

    def __post_init__(self):
        if not isinstance(self.role, str):
            raise ManifestError("invalid_type", "a role grant's role must be text")
        if self.role not in KNOWN_ROLES:
            raise ManifestError("unknown_role", "a role grant names a role that does not exist (Operator, Approver, Auditor)")
        permissions = _tuple_of(self.permissions, str, "role grant permissions")
        if not permissions:
            raise ManifestError("empty_role_grant", "a role grant must name at least one permission")
        for permission in permissions:
            _identifier(permission, "role grant permission", _PERMISSION, 80)
        _unique(permissions, "duplicate_role_grant", "role grant permissions")
        object.__setattr__(self, "permissions", tuple(sorted(permissions)))

    def to_dict(self) -> dict:
        return {"role": self.role, "permissions": list(self.permissions)}


@dataclass(frozen=True)
class MaintenanceHookDeclaration:
    """A DECLARATION that a module will have bookkeeping work the worker runs after every iteration (a future slice wires it up).

    Only declarations exist here: there is no callable, no import path and nothing that executes. ``hook_id`` is an OPAQUE NAME,
    exactly ``<owned namespace>.<name>`` (two segments, so it can never be a deeper, import-path-shaped string), unique across the
    registry. Nothing may ever resolve it as a path: a later slice attaches the real callable through the composition root, by id. ``while_disabled`` says whether the
    worker should keep running the hook when the module is disabled (true for bookkeeping that must not stall, such as moving
    runs whose task failed for good into the failed state). The module that declares the hook is the manifest it appears in;
    the registry reports it together with that module id (``RegisteredHook``).
    """

    hook_id: str
    while_disabled: bool = False
    description: str = ""

    def __post_init__(self):
        _identifier(self.hook_id, "maintenance hook id", _HOOK_ID, MAX_TASK_KIND_LENGTH)
        if not isinstance(self.while_disabled, bool):
            raise ManifestError("invalid_type", "while_disabled must be a boolean")
        _label(self.description, "maintenance hook description", 200)

    def to_dict(self) -> dict:
        return {"hook_id": self.hook_id, "while_disabled": self.while_disabled, "description": self.description}


@dataclass(frozen=True)
class AuditActionDeclaration:
    """An audit action the module emits and the detail keys it may carry (the audit vocabulary is restricted; see apps.audit.events)."""

    action: str
    detail_keys: tuple = ()

    def __post_init__(self):
        _identifier(self.action, "audit action", _DOTTED_2, MAX_AUDIT_ACTION_LENGTH)
        keys = _tuple_of(self.detail_keys, str, "audit detail_keys")
        if len(keys) > 20:
            raise ManifestError("too_many_entries", "audit detail_keys has more than 20 entries")
        for key in keys:
            _identifier(key, "audit detail key", _FIELD_NAME, 40)
        _unique(keys, "invalid_identifier", "audit detail_keys")
        object.__setattr__(self, "detail_keys", keys)

    def to_dict(self) -> dict:
        return {"action": self.action, "detail_keys": sorted(self.detail_keys)}


UI_KINDS = ("route", "navigation")


@dataclass(frozen=True)
class UiContribution:
    """A screen route or a navigation entry for the frontend. ``permission`` is a permission id the user must hold to see it (a hint; the API decides)."""

    contribution_id: str
    kind: str
    path: str
    label: str
    permission: str | None = None

    def __post_init__(self):
        _identifier(self.contribution_id, "ui contribution id", _DOTTED_2, MAX_TASK_KIND_LENGTH)
        if self.kind not in UI_KINDS:
            raise ManifestError("invalid_identifier", "ui contribution kind must be route or navigation")
        if not isinstance(self.path, str) or len(self.path) > 100 or _UI_PATH.fullmatch(self.path) is None:
            raise ManifestError("invalid_identifier", "ui contribution path is not a valid application path")
        _label(self.label, "ui contribution label", 60, allow_empty=False)
        if self.permission is not None:
            _identifier(self.permission, "ui contribution permission", _PERMISSION, 80)

    def to_dict(self) -> dict:
        return {
            "contribution_id": self.contribution_id,
            "kind": self.kind,
            "path": self.path,
            "label": self.label,
            "permission": self.permission,
        }


@dataclass(frozen=True)
class ApiContribution:
    """The first path segment under ``/api/`` the module serves (for example ``requests``). Core-owned segments are reserved."""

    route_prefix: str
    description: str = ""

    def __post_init__(self):
        _identifier(self.route_prefix, "api route prefix", _ROUTE_PREFIX, 40)
        if self.route_prefix in RESERVED_API_ROUTES:
            raise ManifestError("reserved_api_route", "this api route prefix belongs to Core")
        _label(self.description, "api description", 200)

    def to_dict(self) -> dict:
        return {"route_prefix": self.route_prefix, "description": self.description}


CONFIG_FIELD_TYPES = ("bool", "int", "string", "enum", "secret_ref")


@dataclass(frozen=True)
class ConfigField:
    """One typed configuration value. There is no code, no expression and no secret here: ``secret_ref`` names a secret held elsewhere and can have no default."""

    name: str
    field_type: str
    required: bool = False
    default: object = None
    minimum: int | None = None
    maximum: int | None = None
    max_length: int | None = None
    choices: tuple = ()
    description: str = ""

    def __post_init__(self):
        _identifier(self.name, "config field name", _FIELD_NAME, 40)
        if self.field_type not in CONFIG_FIELD_TYPES:
            raise ManifestError("invalid_config_field", "config field type must be one of bool, int, string, enum, secret_ref")
        if not isinstance(self.required, bool):
            raise ManifestError("invalid_config_field", "config field 'required' must be a boolean")
        _label(self.description, "config field description", 200)
        choices = _tuple_of(self.choices, str, "config choices")
        kind = self.field_type
        has_bounds = self.minimum is not None or self.maximum is not None
        if (has_bounds and kind != "int") or (self.max_length is not None and kind not in ("string", "secret_ref")) or (choices and kind != "enum"):
            raise ManifestError("invalid_config_field", "a constraint does not apply to this config field type")
        if kind == "int":
            for bound in (self.minimum, self.maximum):
                if bound is not None and not _is_int(bound):
                    raise ManifestError("invalid_config_field", "int bounds must be integers")
            if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
                raise ManifestError("invalid_config_field", "minimum is greater than maximum")
        max_length = self.max_length
        if kind in ("string", "secret_ref"):
            max_length = 200 if max_length is None else max_length
            if not _is_int(max_length) or not 1 <= max_length <= 1000:
                raise ManifestError("invalid_config_field", "max_length must be an integer from 1 to 1000")
        if kind == "enum":
            if not choices or len(set(choices)) != len(choices) or any(_CHOICE.fullmatch(c) is None for c in choices):
                raise ManifestError("invalid_config_field", "an enum needs one or more distinct short choices")
        self._check_default(kind, max_length, choices)
        object.__setattr__(self, "choices", choices)
        object.__setattr__(self, "max_length", max_length)

    def _check_default(self, kind, max_length, choices) -> None:
        default = self.default
        if default is None:
            return
        ok = (
            (kind == "bool" and isinstance(default, bool))
            or (kind == "int" and _is_int(default) and (self.minimum is None or default >= self.minimum) and (self.maximum is None or default <= self.maximum))
            or (kind == "string" and isinstance(default, str) and len(default) <= max_length and default.isprintable())
            or (kind == "enum" and default in choices)
        )  # secret_ref never has a default
        if not ok:
            raise ManifestError("invalid_config_field", "the default does not fit the config field type and constraints")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "field_type": self.field_type,
            "required": self.required,
            "default": self.default,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "max_length": self.max_length,
            "choices": list(self.choices),
            "description": self.description,
        }


@dataclass(frozen=True)
class ConfigSchema:
    """The configuration a module accepts: a schema version (an integer) and typed fields. Declared only; nothing reads configuration in MOD-1a."""

    schema_version: int
    fields: tuple = ()

    def __post_init__(self):
        if not _is_int(self.schema_version) or self.schema_version < 1:
            raise ManifestError("invalid_config_schema", "config schema_version must be an integer of at least 1")
        fields = _tuple_of(self.fields, ConfigField, "config fields")
        if len(fields) > 50:
            raise ManifestError("too_many_entries", "a config schema has at most 50 fields")
        _unique([f.name for f in fields], "duplicate_config_field", "config fields")
        object.__setattr__(self, "fields", fields)

    def to_dict(self) -> dict:
        return {"schema_version": self.schema_version, "fields": [f.to_dict() for f in sorted(self.fields, key=lambda f: f.name)]}


@dataclass(frozen=True)
class UpdateMetadata:
    """Facts a FUTURE update manager will need. Nothing reads them yet except the registry's protocol check.

    ``data_schema_version``: the version of the module's own stored data, for a migration preflight.
    ``minimum_updater_protocol``: the lowest update-manager protocol that can install or upgrade this module.
    """

    data_schema_version: int | None = None
    minimum_updater_protocol: int | None = None

    def __post_init__(self):
        _positive_int_or_none(self.data_schema_version, "data_schema_version")
        _positive_int_or_none(self.minimum_updater_protocol, "minimum_updater_protocol")

    def to_dict(self) -> dict:
        return {"data_schema_version": self.data_schema_version, "minimum_updater_protocol": self.minimum_updater_protocol}


# --- the manifest -----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ModuleManifest:
    """Manifest v1 of one module. The first six fields are required; everything else is optional."""

    manifest_version: int
    module_id: str
    module_kind: ModuleKind
    module_version: str
    core_api_compatibility: str
    license_feature: str
    display_name: str = ""
    extra_namespaces: tuple = ()
    dependencies: tuple = ()
    capabilities_required: tuple = ()
    capabilities_provided: tuple = ()
    workflows: tuple = ()
    task_handlers: tuple = ()
    permissions: tuple = ()
    audit_actions: tuple = ()
    configuration_schema: ConfigSchema | None = None
    ui_contributions: tuple = ()
    api_contributions: tuple = ()
    update: UpdateMetadata = field(default_factory=UpdateMetadata)
    role_grants: tuple = ()
    maintenance_hooks: tuple = ()

    def __post_init__(self):
        self._check_identity()
        namespaces = self.namespaces
        self._check_dependencies_and_capabilities(namespaces)
        self._check_contributions(namespaces)
        if self.configuration_schema is not None and not isinstance(self.configuration_schema, ConfigSchema):
            raise ManifestError("invalid_type", "configuration_schema must be a ConfigSchema or None")
        if not isinstance(self.update, UpdateMetadata):
            raise ManifestError("invalid_type", "update must be an UpdateMetadata")

    # identity ----------------------------------------------------------------------------------------------------------

    def _check_identity(self) -> None:
        if not _is_int(self.manifest_version) or self.manifest_version not in SUPPORTED_MANIFEST_VERSIONS:
            raise ManifestError("unsupported_manifest_version", "this Core supports manifest version 1 only")
        _identifier(self.module_id, "module_id", _MODULE_ID, 40)
        if self.module_id in RESERVED_NAMESPACES:
            raise ManifestError("reserved_namespace", "this module id is reserved for Core")
        try:
            kind = self.module_kind if isinstance(self.module_kind, ModuleKind) else ModuleKind(self.module_kind)
        except ValueError:
            raise ManifestError("invalid_module_kind", "module_kind must be business, connector, ai_adapter or service") from None
        object.__setattr__(self, "module_kind", kind)
        try:
            parse_version(self.module_version)
        except VersionError:
            raise ManifestError("invalid_module_version", "module_version must be MAJOR.MINOR.PATCH") from None
        try:
            core_range = parse_range(self.core_api_compatibility)
        except VersionError:
            raise ManifestError("invalid_core_api_range", "core_api_compatibility is not a valid version range") from None
        if not core_range.is_bounded_above:
            raise ManifestError("unbounded_core_api_range", "core_api_compatibility must have an upper bound")
        object.__setattr__(self, "core_api_compatibility", str(core_range))
        _identifier(self.license_feature, "license_feature", _DOTTED_2, 64)
        _label(self.display_name, "display_name")
        extras = _tuple_of(self.extra_namespaces, str, "extra_namespaces")
        for namespace in extras:
            _identifier(namespace, "extra namespace", _NAMESPACE, 40)
            if namespace in RESERVED_NAMESPACES:
                raise ManifestError("reserved_namespace", "an extra namespace is reserved for Core")
        if self.module_id in extras:
            raise ManifestError("invalid_identifier", "extra_namespaces repeats the module id")
        _unique(extras, "invalid_identifier", "extra_namespaces")
        object.__setattr__(self, "extra_namespaces", tuple(sorted(extras)))

    @property
    def namespaces(self) -> tuple:
        """The identifier prefixes this module owns: its id and its extra namespaces, sorted."""
        return tuple(sorted({self.module_id, *self.extra_namespaces}))

    @property
    def version(self) -> Version:
        return parse_version(self.module_version)

    @property
    def core_api_range(self) -> VersionRange:
        return parse_range(self.core_api_compatibility)

    # dependencies and capabilities -------------------------------------------------------------------------------------

    def _check_dependencies_and_capabilities(self, namespaces) -> None:
        dependencies = _tuple_of(self.dependencies, ModuleDependency, "dependencies")
        ids = [d.module_id for d in dependencies]
        if self.module_id in ids:
            raise ManifestError("self_dependency", "a module cannot depend on itself")
        _unique(ids, "duplicate_dependency", "dependencies")
        required = _tuple_of(self.capabilities_required, str, "capabilities_required")
        provided = _tuple_of(self.capabilities_provided, str, "capabilities_provided")
        for capability in required + provided:
            _identifier(capability, "capability id", _DOTTED_2, 64)
        _unique(required, "duplicate_capability", "capabilities_required")
        _unique(provided, "duplicate_capability", "capabilities_provided")
        if set(required) & set(provided):
            raise ManifestError("capability_required_and_provided", "a module cannot both require and provide the same capability")
        for capability in provided:
            _owned(capability, namespaces, "a provided capability")
        object.__setattr__(self, "dependencies", dependencies)
        object.__setattr__(self, "capabilities_required", required)
        object.__setattr__(self, "capabilities_provided", provided)

    # contributions -----------------------------------------------------------------------------------------------------

    def _check_contributions(self, namespaces) -> None:
        handlers = _tuple_of(self.task_handlers, TaskHandlerDeclaration, "task_handlers")
        workflows = _tuple_of(self.workflows, WorkflowDeclaration, "workflows")
        permissions = _tuple_of(self.permissions, PermissionDeclaration, "permissions")
        audit_actions = _tuple_of(self.audit_actions, AuditActionDeclaration, "audit_actions")
        ui = _tuple_of(self.ui_contributions, UiContribution, "ui_contributions")
        api = _tuple_of(self.api_contributions, ApiContribution, "api_contributions")

        kinds = [h.kind for h in handlers]
        _unique(kinds, "duplicate_task_kind", "task_handlers")
        for kind in kinds:
            _owned(kind, namespaces, "a task kind")

        if workflows and self.module_kind is not ModuleKind.BUSINESS:
            raise ManifestError("workflow_not_allowed_for_kind", "only business modules may declare workflows")
        keys = [w.workflow_key for w in workflows]
        _unique(keys, "duplicate_workflow", "workflows")
        for workflow in workflows:
            _owned(workflow.workflow_key, namespaces, "a workflow key")
            for kind in workflow.task_kinds:
                if kind not in kinds:
                    raise ManifestError("workflow_task_kind_undeclared", "a workflow uses a task kind the module does not declare")

        permission_ids = [p.permission_id for p in permissions]
        _unique(permission_ids, "duplicate_permission", "permissions")
        for permission_id in permission_ids:
            _owned(permission_id, namespaces, "a permission id")

        actions = [a.action for a in audit_actions]
        _unique(actions, "duplicate_audit_action", "audit_actions")
        for action in actions:
            _owned(action, namespaces, "an audit action")

        ui_ids = [u.contribution_id for u in ui]
        _unique(ui_ids, "duplicate_ui_contribution", "ui_contributions")
        for contribution_id in ui_ids:
            _owned(contribution_id, namespaces, "a ui contribution id")

        _unique([a.route_prefix for a in api], "duplicate_api_route", "api_contributions")

        # Default role grants: a module may grant only what it declares itself (which, by the namespace rule above, it owns).
        grants = _tuple_of(self.role_grants, RoleGrant, "role_grants")
        _unique([g.role for g in grants], "duplicate_role_grant", "role_grants")
        declared = set(permission_ids)
        for grant in grants:
            for permission in grant.permissions:
                if permission not in declared:
                    raise ManifestError("role_grant_undeclared_permission", "a role grant names a permission this module does not declare")

        hooks = _tuple_of(self.maintenance_hooks, MaintenanceHookDeclaration, "maintenance_hooks")
        hook_ids = [h.hook_id for h in hooks]
        _unique(hook_ids, "duplicate_maintenance_hook", "maintenance_hooks")
        for hook_id in hook_ids:
            _owned(hook_id, namespaces, "a maintenance hook id")

        object.__setattr__(self, "role_grants", tuple(sorted(grants, key=lambda g: g.role)))
        object.__setattr__(self, "maintenance_hooks", tuple(sorted(hooks, key=lambda h: h.hook_id)))
        object.__setattr__(self, "task_handlers", handlers)
        object.__setattr__(self, "workflows", workflows)
        object.__setattr__(self, "permissions", permissions)
        object.__setattr__(self, "audit_actions", audit_actions)
        object.__setattr__(self, "ui_contributions", ui)
        object.__setattr__(self, "api_contributions", api)

    # canonical form ----------------------------------------------------------------------------------------------------

    def to_dict(self) -> dict:
        """A plain, JSON-ready description with every collection sorted, so declaration order does not matter. Handler objects are reduced to ``has_handler``."""
        document = {
            "manifest_version": self.manifest_version,
            "module_id": self.module_id,
            "module_kind": self.module_kind.value,
            "module_version": self.module_version,
            "core_api_compatibility": self.core_api_compatibility,
            "license_feature": self.license_feature,
            "display_name": self.display_name,
            "extra_namespaces": list(self.extra_namespaces),
            "dependencies": [d.to_dict() for d in sorted(self.dependencies, key=lambda d: d.module_id)],
            "capabilities_required": sorted(self.capabilities_required),
            "capabilities_provided": sorted(self.capabilities_provided),
            "workflows": [w.to_dict() for w in sorted(self.workflows, key=lambda w: w.workflow_key)],
            "task_handlers": [h.to_dict() for h in sorted(self.task_handlers, key=lambda h: h.kind)],
            "permissions": [p.to_dict() for p in sorted(self.permissions, key=lambda p: p.permission_id)],
            "audit_actions": [a.to_dict() for a in sorted(self.audit_actions, key=lambda a: a.action)],
            "configuration_schema": None if self.configuration_schema is None else self.configuration_schema.to_dict(),
            "ui_contributions": [u.to_dict() for u in sorted(self.ui_contributions, key=lambda u: u.contribution_id)],
            "api_contributions": [a.to_dict() for a in sorted(self.api_contributions, key=lambda a: a.route_prefix)],
            "update": self.update.to_dict(),
        }
        # Added after MOD-1a: written ONLY when present, so every manifest that does not use them keeps exactly the canonical form,
        # and therefore the fingerprint, it had before. Adding an optional field to manifest v1 must not change old fingerprints.
        if self.role_grants:
            document["role_grants"] = [g.to_dict() for g in sorted(self.role_grants, key=lambda g: g.role)]
        if self.maintenance_hooks:
            document["maintenance_hooks"] = [h.to_dict() for h in sorted(self.maintenance_hooks, key=lambda h: h.hook_id)]
        return document

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def fingerprint(self) -> str:
        """SHA-256 (hex) of the canonical form: the same manifest content always gives the same value, in any process."""
        return hashlib.sha256(self.canonical_json().encode("ascii")).hexdigest()
