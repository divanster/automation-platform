"""Namespace continuity across releases: a pure check, with nothing stored. Pure Python, standard library only.

THE RULE (owner decision: namespace ownership is preserved across releases). Identifiers that already exist in a database (permission
rows, audit actions, task kinds, workflow keys) carry a namespace, so a namespace must keep belonging to the module id that claimed it:

* it must never pass to a DIFFERENT module id (``namespace_ownership_changed``);
* it must never be released: a namespace that was claimed before and is claimed by nobody now is refused (``namespace_released``),
  because its identifiers may still be stored. A module may ADD namespaces in a later release.

WHAT IS HERE. ``check_namespace_continuity(previous_owners, registry)`` compares a mapping of earlier claims (namespace -> module id)
with the namespaces the registry claims now; ``next_namespace_history`` returns the history to remember after a successful check
(earlier claims plus new ones). The mapping is supplied by the caller.

WHAT IS NOT. Nothing persists the history and nothing calls these functions: the module state and update slices will store the history
in the database and run the check before a release is activated. Until then this is the rule, written as code and tested, not yet applied.
"""

from collections.abc import Mapping
from types import MappingProxyType

from .errors import RegistryError
from .registry import ModuleRegistry


def _validated_history(previous_owners) -> dict:
    if not isinstance(previous_owners, Mapping):
        raise RegistryError("invalid_namespace_history", "namespace history must be a mapping of namespace to module id")
    for namespace, owner in previous_owners.items():
        if not isinstance(namespace, str) or not namespace or not isinstance(owner, str) or not owner:
            raise RegistryError("invalid_namespace_history", "namespace history must map non-empty text to non-empty text")
    return dict(previous_owners)


def check_namespace_continuity(previous_owners, registry: ModuleRegistry) -> None:
    """Raise ``RegistryError`` if any earlier namespace claim was released or handed to another module. Checked in namespace order."""
    if not isinstance(registry, ModuleRegistry):
        raise TypeError("registry must be an apps.modules.registry.ModuleRegistry")
    history = _validated_history(previous_owners)
    current = registry.namespace_owners
    for namespace in sorted(history):
        owner = current.get(namespace)
        if owner is None:
            raise RegistryError("namespace_released", f"namespace '{namespace}' was claimed by '{history[namespace]}' and is no longer claimed")
        if owner != history[namespace]:
            raise RegistryError("namespace_ownership_changed", f"namespace '{namespace}' belonged to '{history[namespace]}' and is now claimed by '{owner}'")


def next_namespace_history(previous_owners, registry: ModuleRegistry) -> MappingProxyType:
    """The history to keep after ``registry`` is accepted: the earlier claims plus any new ones, sorted. Refuses what the check refuses."""
    check_namespace_continuity(previous_owners, registry)
    merged = dict(_validated_history(previous_owners))
    merged.update(registry.namespace_owners)
    return MappingProxyType(dict(sorted(merged.items())))
