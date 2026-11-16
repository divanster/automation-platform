"""Deterministic derivation of the default role permission sets from module manifests. Pure Python, standard library only.

WHAT IT COMPUTES. ``derive_role_permissions(registry)`` answers one question: "if every registered module's default grants were
applied, which permissions would each product role hold?" The result is the union of Core's own grants (``CORE_ROLE_GRANTS``) and
every registered module's ``role_grants``, for the three roles ``KNOWN_ROLES``. It reads manifests and nothing else.

WHAT IT DOES NOT DO. It does not create groups or permissions, does not read or write a database, does not look at which modules are
enabled (a disabled module still holds its grants, so disabling never silently revokes access; blocking a disabled module's actions
is a separate gate), and is not wired to ``setup_roles`` or to ``/api/auth/me/`` yet. Those are later slices; until then
``apps.accounts.roles`` remains the source of truth and a golden test proves the two agree for Request Intake.

NO IMPLICIT PERMISSIONS. Only permissions a manifest explicitly lists in a ``RoleGrant`` appear. A permission that is declared but not
granted to any role belongs to no role, whatever its scope. Core's grants are an explicit, separate input, because a Core permission
(the audit view permission) is not declared by any module and a module may not grant a permission it does not declare.

DETERMINISM. The result has the three roles in ``KNOWN_ROLES`` order, each a ``frozenset``; it does not depend on the order manifests were
registered or passed in.
"""

from collections.abc import Mapping
from types import MappingProxyType

from .errors import RegistryError
from .manifest import _PERMISSION, KNOWN_ROLES
from .registry import CORE_PERMISSIONS, ModuleRegistry

# What Core itself grants by default: the Auditor reads the audit history. It is Core's permission (Django's own view permission for
# the audit model), declared by no module. A test keeps this equal to the corresponding part of apps.accounts.roles.ROLE_PERMISSIONS.
CORE_ROLE_GRANTS = MappingProxyType({"Auditor": frozenset({"audit.view_auditevent"})})


def _validated_core_grants(core_grants) -> dict:
    if not isinstance(core_grants, Mapping):
        raise RegistryError("invalid_core_grants", "core grants must be a mapping of role to permission ids")
    result = {}
    for role, permissions in core_grants.items():
        if not isinstance(role, str) or role not in KNOWN_ROLES:
            raise RegistryError("invalid_core_grants", "core grants name a role that does not exist")
        if isinstance(permissions, (str, bytes)) or not hasattr(permissions, "__iter__"):
            raise RegistryError("invalid_core_grants", "core grants must list permission ids")
        ids = list(permissions)
        for permission in ids:
            if not isinstance(permission, str) or len(permission) > 80 or _PERMISSION.fullmatch(permission) is None:
                raise RegistryError("invalid_core_grants", "core grants name something that is not a permission id")
        result[role] = set(ids)
    return result


def derive_role_permissions(registry: ModuleRegistry, *, core_grants=CORE_ROLE_GRANTS) -> MappingProxyType:
    """``role -> frozenset(permission ids)`` for every role in ``KNOWN_ROLES`` (Operator, Approver, Auditor), derived from ``registry``."""
    if not isinstance(registry, ModuleRegistry):
        raise TypeError("registry must be an apps.modules.registry.ModuleRegistry")
    derived = {role: set() for role in KNOWN_ROLES}
    for role, permissions in _validated_core_grants(core_grants).items():
        derived[role] |= permissions
    for manifest in registry.modules:
        for grant in manifest.role_grants:
            derived[grant.role] |= set(grant.permissions)
    return MappingProxyType({role: frozenset(derived[role]) for role in KNOWN_ROLES})


def derive_all_permissions(registry: ModuleRegistry, *, core_permissions=CORE_PERMISSIONS) -> frozenset:
    """Every permission id the registered modules declare, plus Core's: the set ``/api/auth/me/`` is allowed to report."""
    if not isinstance(registry, ModuleRegistry):
        raise TypeError("registry must be an apps.modules.registry.ModuleRegistry")
    declared = {permission.permission_id for manifest in registry.modules for permission in manifest.permissions}
    return frozenset(declared | set(core_permissions))
