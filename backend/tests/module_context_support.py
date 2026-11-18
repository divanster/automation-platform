"""Shared helpers for the ModuleContext tests (MOD-1b S6). Test only; never imported by the application. Pure Python except where noted.

``ctx_module`` is a SYNTHETIC business module that declares every capability the context has a facade for, one task kind, one workflow (definition
version 3), one audit action and three permissions that reuse the existing ``intake.*`` ids (so real Django users with the real roles can be checked against
them). Nothing registers it anywhere: tests build a ``ModuleRegistry`` from it.
"""

from apps.modules.manifest import (
    AuditActionDeclaration,
    PermissionDeclaration,
    RoleGrant,
    TaskHandlerDeclaration,
    WorkflowDeclaration,
)
from apps.modules.registry import ModuleRegistry
from tests.module_support import FakeHandler, dep, named

CTX = "ctx_mod"
OTHER = "other_mod"
KIND = "ctx_mod.work"
OTHER_KIND = "other_mod.work"
FLOW = "ctx_mod.flow"
FLOW_VERSION = 3
ACTION = "ctx_mod.did_it"
ACTION_KEYS = ("count", "reason_code")
OTHER_ACTION = "other_mod.did_it"
P_SUBMIT, P_VIEW_OWN, P_VIEW_ANY = "intake.submit_request", "intake.view_own_request", "intake.view_any_request"
ALL_CAPABILITIES = ("task.enqueue", "case.run", "audit.record", "permissions.check")


def ctx_module(module_id=CTX, **overrides):
    values = dict(
        extra_namespaces=("intake",),
        capabilities_required=ALL_CAPABILITIES,
        task_handlers=(TaskHandlerDeclaration(KIND, FakeHandler(KIND), "does work"),),
        workflows=(WorkflowDeclaration(FLOW, FLOW_VERSION, (KIND,), "a flow"),),
        audit_actions=(AuditActionDeclaration(ACTION, ACTION_KEYS),),
        permissions=(
            PermissionDeclaration(P_SUBMIT, "global", "submit"),
            PermissionDeclaration(P_VIEW_OWN, "own", "view own"),
            PermissionDeclaration(P_VIEW_ANY, "any", "view any"),
        ),
        role_grants=(RoleGrant("Operator", (P_SUBMIT, P_VIEW_OWN)), RoleGrant("Auditor", (P_VIEW_ANY,))),
    )
    values.update(overrides)
    return named(module_id, **values)


def other_module(**overrides):
    values = dict(
        capabilities_required=ALL_CAPABILITIES,
        task_handlers=(TaskHandlerDeclaration(OTHER_KIND, FakeHandler(OTHER_KIND), "does work"),),
        workflows=(WorkflowDeclaration("other_mod.flow", 1, (OTHER_KIND,), "a flow"),),
        audit_actions=(AuditActionDeclaration(OTHER_ACTION, ACTION_KEYS),),
    )
    values.update(overrides)
    return named(OTHER, **values)


def registry(*manifests) -> ModuleRegistry:
    return ModuleRegistry.build(manifests or (ctx_module(), other_module()))


def dependent(module_id="dep_mod", on=CTX, **overrides):
    return named(module_id, dependencies=(dep(on),), **overrides)
