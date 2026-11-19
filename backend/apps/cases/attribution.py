"""Workflow attribution: which module, workflow and workflow definition version a run belongs to. Pure Python, standard library only.

A ``WorkflowRun`` is stamped, when it is created, with three values that never change afterwards:

* ``module_id``: the stable identity of the module that owns the workflow (the manifest's ``module_id``);
* ``workflow_key``: the workflow's stable key (the manifest's ``WorkflowDeclaration.workflow_key``);
* ``workflow_version``: the integer definition version the run started on (the manifest's ``WorkflowDeclaration.definition_version``).

The point is provenance: later, an update or an audit can say which definition a run was created under, even after the
module has moved to a newer version. Attribution describes a run; it changes nothing about how a run behaves (states,
transitions, tasks, revisions, hashes and approvals do not read it).

THE RULES ARE THE MANIFEST'S, WRITTEN TWICE. ``apps.cases`` is Core and may not import ``apps.modules`` (a boundary test pins
it), so the naming rules of manifest v1 are repeated here: the same patterns, the same length limits and the same reserved
namespaces. ``tests/test_workflow_attribution.py`` keeps the copies equal and compares the two on a generated sample, exactly as
the audit vocabulary does for its detail keys. The definition version is a positive 32-bit integer in the manifest
(``MAX_WORKFLOW_DEFINITION_VERSION``), here (``MAX_WORKFLOW_VERSION``) and in the database column; tests keep the limits equal. A value that fails any rule raises
``WorkflowAttributionError`` with a stable ``code``; the message names the field and never repeats the value.

WHAT THIS DOES NOT DO. It does not look anything up: whether a module is registered or enabled, and whether a module really
declares a workflow key, is the registry's knowledge and Core does not have it (Request Intake became a module in MOD-2, but Core still does not import it). The only code
that creates runs, ``apps.intake.submission``, passes a constant. There is no dynamic import, no discovery and no way to supply
these values from a request, a command line or a file.
"""

import re
from dataclasses import dataclass

# The three attributed columns of ``cases.WorkflowRun``; the model's guard and the migrations use this order.
ATTRIBUTION_FIELDS = ("module_id", "workflow_key", "workflow_version")

MODULE_ID_PATTERN = r"^[a-z][a-z0-9_]{2,39}$"  # the manifest's module id: ``[a-z][a-z0-9_]{2,39}``
# One or more dotted segments of ``[a-z][a-z0-9_]{0,39}``, as the manifest's workflow key (at most 64 characters in all).
WORKFLOW_KEY_PATTERN = r"^[a-z][a-z0-9_]{0,39}(\.[a-z][a-z0-9_]{0,39})*$"
MAX_MODULE_ID_LENGTH = 40
MAX_WORKFLOW_KEY_LENGTH = 64  # the manifest's task kind and workflow key limit
MAX_WORKFLOW_VERSION = 2_147_483_647  # a positive 32-bit integer (a PostgreSQL ``integer``); equal to the manifest's MAX_WORKFLOW_DEFINITION_VERSION

# Namespaces Core owns or reserves (``apps.modules.manifest.RESERVED_NAMESPACES``): no module id, and no workflow key, may start with one.
RESERVED_NAMESPACES = frozenset(
    {
        "core", "test", "platform", "module", "auth", "admin", "health",
        "run", "revision", "approval", "rejection", "request", "audit",
        "task", "case", "document", "documents", "ai", "secrets", "permissions",
    }
)  # fmt: skip

_MODULE_ID = re.compile(MODULE_ID_PATTERN)
_WORKFLOW_KEY = re.compile(WORKFLOW_KEY_PATTERN)


class WorkflowAttributionError(ValueError):
    """A module id, workflow key or workflow version is refused. ``code`` is stable; the message never echoes the value."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{message} ({code})")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_attribution(module_id, workflow_key, workflow_version) -> tuple:
    """Return ``(module_id, workflow_key, workflow_version)`` unchanged if all three satisfy the rules, else raise ``WorkflowAttributionError``."""
    if not isinstance(module_id, str):
        raise WorkflowAttributionError("invalid_type", "module_id must be text")
    if len(module_id) > MAX_MODULE_ID_LENGTH or _MODULE_ID.fullmatch(module_id) is None:
        raise WorkflowAttributionError("invalid_module_id", "module_id is not a valid module identifier")
    if module_id in RESERVED_NAMESPACES:
        raise WorkflowAttributionError("reserved_namespace", "module_id is a namespace reserved for Core")

    if not isinstance(workflow_key, str):
        raise WorkflowAttributionError("invalid_type", "workflow_key must be text")
    if len(workflow_key) > MAX_WORKFLOW_KEY_LENGTH or _WORKFLOW_KEY.fullmatch(workflow_key) is None:
        raise WorkflowAttributionError("invalid_workflow_key", "workflow_key is not a valid workflow key")
    if workflow_key.split(".", 1)[0] in RESERVED_NAMESPACES:
        raise WorkflowAttributionError("reserved_namespace", "workflow_key starts with a namespace reserved for Core")

    if not _is_int(workflow_version):
        raise WorkflowAttributionError("invalid_type", "workflow_version must be an integer")
    if workflow_version < 1 or workflow_version > MAX_WORKFLOW_VERSION:
        raise WorkflowAttributionError("invalid_workflow_version", f"workflow_version must be between 1 and {MAX_WORKFLOW_VERSION}")
    return module_id, workflow_key, workflow_version


@dataclass(frozen=True)
class WorkflowAttribution:
    """The validated identity of a workflow definition, as stamped on a run. Immutable; an invalid one cannot be constructed."""

    module_id: str
    workflow_key: str
    workflow_version: int

    def __post_init__(self):
        validate_attribution(self.module_id, self.workflow_key, self.workflow_version)

    def fields(self) -> dict:
        """The column values for ``WorkflowRun``. Validates again, so a value changed after construction (``object.__setattr__``) is still caught."""
        module_id, workflow_key, workflow_version = validate_attribution(self.module_id, self.workflow_key, self.workflow_version)
        return {"module_id": module_id, "workflow_key": workflow_key, "workflow_version": workflow_version}
