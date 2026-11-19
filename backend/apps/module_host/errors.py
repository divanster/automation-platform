"""Errors of the module host. Pure Python, standard library only.

Every error has a stable ``code`` (listed in ``CODES``) and a message that names the rule and module ids, never a stored value other than ids and
versions. ``apps.module_host`` has no secrets to leak: a module state row holds identifiers, versions and a fingerprint.
"""

CODES = frozenset(
    {
        "invalid_registry",  # not an apps.modules ModuleRegistry
        "invalid_module_id",  # not text of the module id shape
        "unknown_module",  # the module is not in the approved static registry
        "state_missing",  # no ModuleState row: run the module sync first
        "version_mismatch",  # the stored installed version differs from the registered one
        "dependency_not_enabled",  # enabling needs every direct dependency enabled and version-matched
        "dependents_enabled",  # disabling is refused while an enabled module depends on this one
        "namespace_ownership_changed",  # a stored namespace claim now belongs to another module id
        "namespace_released",  # a stored namespace claim is no longer claimed by anybody
        "legacy_kind_claimed",  # a legacy ungated task kind is also declared by a registered module
        "invalid_legacy_kinds",  # the legacy kind set is malformed
        "handler_composition",  # the worker's handler allowlist and the registry's task kinds do not fit (a kind without a handler, or a handler nobody owns)
        "invalid_clock",  # a naive datetime was passed as the time of a change
    }
)


class ModuleHostError(ValueError):
    """A module host operation was refused. ``code`` is stable and one of ``CODES``; nothing was changed."""

    def __init__(self, code: str, message: str):
        if code not in CODES:
            raise ValueError("an undeclared module host error code")
        super().__init__(message)
        self.code = code


# MOD-1b S6: the codes of ``ModuleContextError`` (``apps.module_host.context``), the controlled interface a module reaches Core through.
CONTEXT_CODES = frozenset(
    {
        "invalid_registry",  # not an apps.modules ModuleRegistry
        "invalid_module_id",  # not text of the module id shape
        "unknown_module",  # the module is not in the approved static registry
        "capability_not_declared",  # the manifest does not list this capability in capabilities_required
        "capability_not_available",  # Core has no facade for this capability (revision.approval, or a capability another module provides)
        "module_disabled",  # the module's state row says disabled
        "module_version_mismatch",  # the stored installed version differs from the registered one
        "module_state_missing",  # no state row after the first module sync completed
        "module_dependency_unavailable",  # a direct dependency is not available
        "task_kind_not_owned",  # the task kind belongs to another module
        "task_kind_not_declared",  # the manifest does not declare this task kind
        "idempotency_key_not_namespaced",  # the key does not start with a namespace the module owns followed by a dot
        "workflow_not_declared",  # the manifest does not declare this workflow key
        "run_not_owned",  # the run was created under another module
        "audit_action_core_owned",  # the action is one of Core's
        "audit_action_not_declared",  # the manifest does not declare this audit action
        "audit_detail_key_not_declared",  # a detail key the declaration does not list
        "audit_subject_type_not_allowed",  # the subject type is not one a module may use
        "permission_not_declared",  # neither the module's own permission nor a Core permission a module may check
        "no_permission_requested",  # a permission check names no permission
        "actor_required",  # the actor is neither a user nor the explicit system actor
    }
)


class ModuleContextError(ValueError):
    """A module asked Core for something its manifest and state do not allow. ``code`` is stable and one of ``CONTEXT_CODES``; nothing was changed.

    The message names the rule and identifiers that already passed validation (module id, capability, task kind, workflow key, action): never a payload,
    a detail value, a name or any business content.
    """

    def __init__(self, code: str, message: str):
        if code not in CONTEXT_CODES:
            raise ValueError("an undeclared module context error code")
        super().__init__(message)
        self.code = code
