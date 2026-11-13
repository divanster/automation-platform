"""The composition root of the worker: the one place that assembles the production handler allowlist.

``apps.tasks`` (the queue) knows no business code and cannot import any. The worker entry point
(``runworker``) imports THIS module and installs its ``COMPOSITION`` with
``apps.tasks.worker.install_composition``. The pieces are plain imports of fixed names: no dotted path from a
setting, no dynamic import, no registration at run time.

* ``registry``: ``apps.intake.registry.PRODUCTION_REGISTRY`` (the queue's handlers plus the request-intake
  extraction handler), a read-only mapping.
* ``after_iteration``: the request-intake failure reconciler, run after every worker iteration.
"""

from apps.intake.registry import PRODUCTION_REGISTRY
from apps.intake.services import reconcile_after_iteration
from apps.tasks.worker import Composition

COMPOSITION = Composition(registry=PRODUCTION_REGISTRY, after_iteration=reconcile_after_iteration)
