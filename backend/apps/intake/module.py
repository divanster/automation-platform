"""The manifest of Request Intake, the first built-in module (MOD-2). Pure Python: no Django, no models, no database, no import of the handler.

``MANIFEST`` DECLARES what Request Intake already is; it changes nothing it does. Every identifier below exists today and keeps its spelling: the module id,
the workflow key and its definition version (the values stored on every ``WorkflowRun`` since S5), the task kind, the six ``intake.*`` permissions (the model
``RequestSource`` declares them; ``apps.accounts.roles`` lists who holds them), the default role grants, the REST prefix and the screens. The ``intake``
prefix is kept through ``extra_namespaces`` so no permission is renamed.

The module is registered by ``config/modules.py`` (the one place that names a built-in module). Nothing here is imported by Core: ``apps.accounts``, ``apps.audit``,
``apps.cases``, ``apps.tasks``, ``apps.modules`` and the module host never import ``apps.intake`` (tests pin that).

WHAT IS DELIBERATELY NOT DECLARED.

* No audit action. ``request.submitted``, ``request.edited``, ``revision.created``, ``run.state_changed``, ``approval.recorded``, ``approval.invalidated`` and
  ``rejection.recorded`` are CORE's actions (the ``request``, ``revision``, ``run``, ``approval`` and ``rejection`` namespaces are reserved for Core and Core's
  recorder emits them); declaring one here would be refused. The audit vocabulary Request Intake needs is Core's own, unchanged.
* No task handler OBJECT. The declaration names the kind only. The handler stays where it is (``apps.intake.handlers.EXTRACT_HANDLER``, composed into
  ``apps.intake.registry.PRODUCTION_REGISTRY``) because importing it would import Django models, and this file must stay importable before Django is ready.
  ``config/task_registry.py`` checks that the declared kind has exactly that handler.
* No maintenance hook. The failure reconciler keeps running as the worker's ``after_iteration`` hook after every iteration, whether or not the module is enabled
  (the behaviour a declared ``while_disabled`` hook would have). Moving it to a named maintenance hook is a later, separate decision; the S2 hook seam is untouched.
* No configuration schema, no dependency, no provided capability and no update metadata: Request Intake has none of them today.

The screens (``ui_contributions``) mirror ``frontend/src/App.tsx`` and ``Layout.tsx`` (a test compares them as text; the frontend itself does not read the manifest).
A route parameter is written ``:run_id`` here because a declared path is lower case only; the frontend's own name for it is ``:runId``.
"""

from apps.modules.manifest import (
    ApiContribution,
    ModuleManifest,
    PermissionDeclaration,
    RoleGrant,
    TaskHandlerDeclaration,
    UiContribution,
    WorkflowDeclaration,
)

from .policy import KIND_EXTRACT, MODULE_ID, WORKFLOW_KEY, WORKFLOW_VERSION

PERM_SUBMIT = "intake.submit_request"
PERM_VIEW_OWN = "intake.view_own_request"
PERM_EDIT_OWN = "intake.edit_own_request"
PERM_VIEW_ANY = "intake.view_any_request"
PERM_APPROVE = "intake.approve_request"
PERM_REJECT = "intake.reject_request"

MANIFEST = ModuleManifest(
    manifest_version=1,
    module_id=MODULE_ID,
    module_kind="business",
    module_version="1.0.0",
    core_api_compatibility=">=1.0.0,<2.0.0",
    license_feature="mod.request_intake",
    display_name="Request Intake",
    extra_namespaces=("intake",),
    capabilities_required=("task.enqueue", "audit.record", "case.run", "revision.approval", "permissions.check"),
    workflows=(WorkflowDeclaration(WORKFLOW_KEY, WORKFLOW_VERSION, (KIND_EXTRACT,), "turns a text request into a reviewed, approvable draft"),),
    task_handlers=(TaskHandlerDeclaration(KIND_EXTRACT, None, "extracts a request"),),
    permissions=(
        # The descriptions equal the permission names the model declares (a test compares them).
        PermissionDeclaration(PERM_SUBMIT, "global", "Can submit a request"),
        PermissionDeclaration(PERM_VIEW_OWN, "own", "Can view the requests the user submitted"),
        PermissionDeclaration(PERM_EDIT_OWN, "own", "Can edit the requests the user submitted"),
        PermissionDeclaration(PERM_VIEW_ANY, "any", "Can view any request"),
        PermissionDeclaration(PERM_APPROVE, "any", "Can approve a request that is waiting for review"),
        PermissionDeclaration(PERM_REJECT, "any", "Can reject a request that is waiting for review"),
    ),
    role_grants=(
        RoleGrant("Operator", (PERM_SUBMIT, PERM_VIEW_OWN, PERM_EDIT_OWN)),
        RoleGrant("Approver", (PERM_VIEW_ANY, PERM_APPROVE, PERM_REJECT)),
        RoleGrant("Auditor", (PERM_VIEW_ANY,)),  # Core adds the Auditor's audit view permission; it is not this module's
    ),
    ui_contributions=(
        UiContribution("request_intake.route_requests", "route", "/requests", "Requests"),
        UiContribution("request_intake.route_new_request", "route", "/requests/new", "New request", PERM_SUBMIT),
        UiContribution("request_intake.route_request_detail", "route", "/requests/:run_id", "Request"),
        UiContribution("request_intake.nav_requests", "navigation", "/requests", "Requests"),
        UiContribution("request_intake.nav_new_request", "navigation", "/requests/new", "New request", PERM_SUBMIT),
    ),
    api_contributions=(ApiContribution("requests", "the request intake REST API under /api/requests/"),),
)
