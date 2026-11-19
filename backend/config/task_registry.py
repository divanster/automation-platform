"""The composition root of the worker: the one place that assembles the production handler allowlist.

``apps.tasks`` (the queue) knows no business code and cannot import any. The worker entry point
(``runworker``) imports THIS module and installs its ``COMPOSITION`` with
``apps.tasks.worker.install_composition``. The pieces are plain imports of fixed names: no dotted path from a
setting, no dynamic import, no registration at run time.

* ``registry``: ``apps.intake.registry.PRODUCTION_REGISTRY`` (the queue's handlers plus the request-intake
  extraction handler), a read-only mapping.
* ``after_iteration``: the request-intake failure reconciler, run after every worker iteration.
* ``kind_gate`` (MOD-1b S4, MOD-2): ``apps.module_host.gate.make_kind_gate`` over the approved static module registry and the stored module
  state. ``LEGACY_UNGATED_KINDS`` are the task kinds that existed before modules and belong to no module: only ``test.add_numbers`` now. It stays claimable
  with no module state. Request Intake is a registered module since MOD-2, so ``request_intake.extract`` left the legacy set: it is claimable only while
  the module is enabled, version-matched and has state (``module_sync`` creates the state). A disabled module's tasks stay queued, never failed.
* The handler allowlist is still ``PRODUCTION_REGISTRY`` (the handler object is the one it always was). ``check_handler_composition`` verifies at import that its
  kinds are exactly the legacy kinds plus the task kinds the registry declares, so a module kind without a handler, or a handler nobody owns, stops the process
  at start instead of being silently gated or allowed. There is still one worker, one reconciler (``after_iteration``) and no maintenance hook.
* ``prepare_worker`` (AI-2a): called once by ``runworker`` before the first task. It decides, from the stored AI extraction settings (``apps.ai_settings``, if
  an administrator saved any) and otherwise from the environment, and explicitly, which extractor the
  ``request_intake.extract`` handler uses (``config/intake_extraction.py``: ``deterministic`` by default, ``ai`` only when asked for, no automatic fallback),
  fails the start-up on a missing or invalid setting (or an unreadable database), and installs the extractor source. It reads once: there is no hot reload.
  The composition above is unchanged by it.
"""

from django.db import DatabaseError

from apps.ai_settings.services import load_stored_settings
from apps.intake.errors import ExtractionConfigurationError
from apps.intake.handlers import install_extractor_source
from apps.intake.registry import PRODUCTION_REGISTRY
from apps.intake.services import reconcile_after_iteration
from apps.module_host.gate import make_kind_gate
from apps.module_host.rules import check_handler_composition
from apps.tasks.handlers import TEST_ADD_NUMBERS
from apps.tasks.worker import Composition
from config.intake_extraction import build_extractor_source
from config.modules import build_registry

# Task kinds that predate modules and belong to none: the queue's deterministic test handler. They need no module state. (Request Intake's
# ``request_intake.extract`` was legacy before MOD-2; a registered module may not also be legacy, ``make_kind_gate`` refuses it.) A new production kind has to
# be decided on, a module's or legacy, rather than silently held or silently allowed: ``check_handler_composition`` enforces it.
LEGACY_UNGATED_KINDS = frozenset({TEST_ADD_NUMBERS})

MODULE_REGISTRY = build_registry()
check_handler_composition(MODULE_REGISTRY, PRODUCTION_REGISTRY, LEGACY_UNGATED_KINDS)

COMPOSITION = Composition(
    registry=PRODUCTION_REGISTRY,
    after_iteration=reconcile_after_iteration,
    kind_gate=make_kind_gate(MODULE_REGISTRY, LEGACY_UNGATED_KINDS),
)


class WorkerConfigurationError(Exception):
    """The worker cannot start with the current settings. The message names the setting, never its value."""


_LOAD = object()  # "read the stored settings from the database"; ``None`` passed by a test means "no stored row"


def prepare_worker(lease_seconds: int, environ=None, *, transport=None, stored_settings=_LOAD) -> str:
    """Choose and install the Request Intake extractor for this worker process; return one line describing it for the operator.

    The stored AI extraction settings (``apps.ai_settings``) are read ONCE here, so a change saved later reaches this process only when it is restarted. A
    stored row takes precedence over the ``AP_INTAKE_*`` variables; with no row they decide as before. If the database cannot be read (including a table
    that was not migrated) the worker does NOT start and does NOT fall back to the environment: it could otherwise run a mode the administrator turned off.

    Raises ``WorkerConfigurationError`` (the message names the setting, never its value) when the extraction settings are missing or invalid, so the
    worker does not start with a half-configured AI mode and nothing is installed. ``transport`` is for tests. Nothing is connected to except the database read.
    """
    if stored_settings is _LOAD:
        try:
            stored_settings = load_stored_settings()
        except DatabaseError as error:
            raise WorkerConfigurationError(f"The stored AI extraction settings could not be read ({type(error).__name__}); run migrate and check the database.") from None
    try:
        source = build_extractor_source(environ, module_gate=COMPOSITION.kind_gate, lease_seconds=lease_seconds, transport=transport, stored=stored_settings)
    except ExtractionConfigurationError as error:
        raise WorkerConfigurationError(str(error)) from None
    install_extractor_source(source)
    return source.describe()
