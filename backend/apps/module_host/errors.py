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
