"""The decisions of the ModuleContext as pure functions over a manifest and a registry (MOD-1b S6). Standard library only: no Django, no database, no other ``apps`` import.

A manifest and a registry are read through their public attributes only (``capabilities_required``, ``task_handlers``, ``workflows``, ``audit_actions``,
``permissions``, ``namespaces``; ``task_kinds``), so these rules cannot reach anything a manifest does not expose. They decide; ``apps.module_host.context``
gathers the facts (state, the actor, the run) and delegates to the existing Core services.

WHAT IS ALLOWED. A module reaches Core only through a capability its manifest lists in ``capabilities_required`` and for which Core has a facade
(``FACADE_CAPABILITIES``): ``task.enqueue``, ``case.run``, ``audit.record`` and ``permissions.check``. ``revision.approval`` is a capability Core provides
but has no facade yet; a capability another module provides is not Core's to expose. Everything a module does through a capability stays inside what its own
manifest declares: its task kinds, its workflow keys (at their declared definition version), its audit actions (with their declared detail keys) and its
permissions (with their declared scope).
"""

from collections.abc import Mapping

from .errors import ModuleContextError
from .rules import DEPENDENCY_UNAVAILABLE, DISABLED, MISSING_STATE, VERSION_MISMATCH

CAP_TASK_ENQUEUE = "task.enqueue"
CAP_CASE_RUN = "case.run"
CAP_AUDIT_RECORD = "audit.record"
CAP_PERMISSIONS_CHECK = "permissions.check"
FACADE_CAPABILITIES = frozenset({CAP_TASK_ENQUEUE, CAP_CASE_RUN, CAP_AUDIT_RECORD, CAP_PERMISSIONS_CHECK})

# Subject types a module may use for its audit events. ``module`` is the module host's own (module.enabled, module.disabled).
MODULE_SUBJECT_TYPES = ("run", "revision", "approval", "rejection")

# Core permissions a module may ask about (scope: any). Declared by no module; a module may not grant them, only check them.
PERMITTED_CORE_PERMISSIONS = frozenset({"audit.view_auditevent"})

_UNAVAILABLE = {
    DISABLED: ("module_disabled", "is disabled"),
    VERSION_MISMATCH: ("module_version_mismatch", "is installed at another version than the registry holds"),
    MISSING_STATE: ("module_state_missing", "has no stored state although the first module sync has completed"),
    DEPENDENCY_UNAVAILABLE: ("module_dependency_unavailable", "needs a dependency that is not available"),
}


def check_capability(manifest, capability) -> None:
    """The manifest must list ``capability`` and Core must have a facade for it."""
    if not isinstance(capability, str) or capability not in FACADE_CAPABILITIES:
        raise ModuleContextError("capability_not_available", "Core has no facade for that capability")
    if capability not in manifest.capabilities_required:
        raise ModuleContextError("capability_not_declared", f"module '{manifest.module_id}' does not declare the capability '{capability}'")


def check_available(module_id: str, availability) -> None:
    """Refuse unless ``availability`` (``rules.Availability``) says the module is available. Maps the host's reasons to stable codes."""
    if availability.available:
        return
    code, text = _UNAVAILABLE.get(availability.reason, ("module_disabled", "is not available"))
    raise ModuleContextError(code, f"module '{module_id}' {text}")


def check_task_kind(registry, manifest, kind) -> None:
    """``kind`` must be a task kind this module declares. A kind another module owns is refused as such."""
    owner = registry.task_kinds.get(kind) if isinstance(kind, str) else None
    if owner is not None and owner != manifest.module_id:
        raise ModuleContextError("task_kind_not_owned", f"the task kind '{kind}' belongs to another module")
    if not isinstance(kind, str) or kind not in {handler.kind for handler in manifest.task_handlers}:
        raise ModuleContextError("task_kind_not_declared", f"module '{manifest.module_id}' does not declare that task kind")


def check_idempotency_key(manifest, key) -> None:
    """A key, when given, starts with a namespace the module owns followed by a dot (``request_intake.extract:<run>:1``). No key is allowed: the queue's own rule."""
    if key is None:
        return
    if not isinstance(key, str) or "." not in key or key.split(".", 1)[0] not in manifest.namespaces:
        raise ModuleContextError("idempotency_key_not_namespaced", "an idempotency key must start with a namespace the module owns followed by a dot")


def declared_workflow(manifest, workflow_key):
    """The module's declaration of ``workflow_key`` (its definition version is the declared one, never the caller's)."""
    for workflow in manifest.workflows:
        if workflow.workflow_key == workflow_key:
            return workflow
    raise ModuleContextError("workflow_not_declared", f"module '{manifest.module_id}' does not declare that workflow")


def check_run_owner(manifest, run_module_id) -> None:
    if run_module_id != manifest.module_id:
        raise ModuleContextError("run_not_owned", "that run was not created under this module")


def check_audit_event(manifest, core_actions, action, subject_type, detail_keys) -> None:
    """The action must be one the module declares (never a Core action), the subject type one a module may use, and every detail key one the declaration lists."""
    if not isinstance(action, str):
        raise ModuleContextError("audit_action_not_declared", "an audit action is text")
    if action in core_actions:
        raise ModuleContextError("audit_action_core_owned", "that audit action belongs to Core")
    declaration = next((a for a in manifest.audit_actions if a.action == action), None)
    if declaration is None:
        raise ModuleContextError("audit_action_not_declared", f"module '{manifest.module_id}' does not declare that audit action")
    if subject_type not in MODULE_SUBJECT_TYPES:
        raise ModuleContextError("audit_subject_type_not_allowed", "a module may use the run, revision, approval or rejection subject types only")
    unknown = set(detail_keys) - set(declaration.detail_keys)
    if unknown:
        raise ModuleContextError("audit_detail_key_not_declared", "a detail key the audit action does not declare")


def access_sets(manifest, permission_ids, core_permissions=PERMITTED_CORE_PERMISSIONS) -> tuple:
    """``(any_permissions, own_permissions)`` for the permissions named, by their DECLARED scope (``own`` reaches the actor's own records; ``any`` and ``global`` any).

    Only the module's own declared permissions and the Core permissions in ``core_permissions`` can be named; anything else is refused, including another
    module's permission and a permission nobody declares. The sets are the input of Core's generic rule (``apps.accounts.access.AccessRule``).
    """
    if isinstance(permission_ids, (str, bytes)) or not isinstance(permission_ids, (tuple, list, frozenset, set)) or not permission_ids:
        raise ModuleContextError("no_permission_requested", "name at least one permission")
    declared: Mapping = {p.permission_id: p.scope for p in manifest.permissions}
    any_set, own_set = set(), set()
    for permission_id in permission_ids:
        if isinstance(permission_id, str) and permission_id in declared:
            (own_set if declared[permission_id] == "own" else any_set).add(permission_id)
        elif isinstance(permission_id, str) and permission_id in core_permissions:
            any_set.add(permission_id)
        else:
            raise ModuleContextError("permission_not_declared", f"module '{manifest.module_id}' may not check that permission")
    return frozenset(any_set), frozenset(own_set)
