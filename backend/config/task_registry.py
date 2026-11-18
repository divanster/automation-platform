"""The composition root of the worker: the one place that assembles the production handler allowlist.

``apps.tasks`` (the queue) knows no business code and cannot import any. The worker entry point
(``runworker``) imports THIS module and installs its ``COMPOSITION`` with
``apps.tasks.worker.install_composition``. The pieces are plain imports of fixed names: no dotted path from a
setting, no dynamic import, no registration at run time.

* ``registry``: ``apps.intake.registry.PRODUCTION_REGISTRY`` (the queue's handlers plus the request-intake
  extraction handler), a read-only mapping.
* ``after_iteration``: the request-intake failure reconciler, run after every worker iteration.
* ``kind_gate`` (MOD-1b S4): ``apps.module_host.gate.make_kind_gate`` over the approved static module registry and the stored module
  state. ``LEGACY_UNGATED_KINDS`` are the task kinds that existed before modules; they stay claimable with no module state, so Request Intake's
  ``request_intake.extract`` works exactly as before. Request Intake is NOT a module yet: MOD-2 registers it and must remove its kind from the
  legacy set in the same change (``make_kind_gate`` refuses a kind that is both legacy and declared by a registered module).
"""

from apps.intake.handlers import EXTRACT_HANDLER
from apps.intake.registry import PRODUCTION_REGISTRY
from apps.intake.services import reconcile_after_iteration
from apps.module_host.gate import make_kind_gate
from apps.tasks.handlers import TEST_ADD_NUMBERS
from apps.tasks.worker import Composition
from config.modules import build_registry

# Task kinds that predate modules and need no module state. A test pins this set against PRODUCTION_REGISTRY, so a new production kind has to be
# decided on (a module's, or legacy) rather than silently held or silently allowed.
LEGACY_UNGATED_KINDS = frozenset({TEST_ADD_NUMBERS, EXTRACT_HANDLER.kind})

COMPOSITION = Composition(
    registry=PRODUCTION_REGISTRY,
    after_iteration=reconcile_after_iteration,
    kind_gate=make_kind_gate(build_registry(), LEGACY_UNGATED_KINDS),
)
