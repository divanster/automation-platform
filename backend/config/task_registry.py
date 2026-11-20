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
* The handler allowlist is ``WORKER_REGISTRY`` since DOC-2: ``PRODUCTION_REGISTRY`` (unchanged: the queue's handlers plus Request Intake's) plus Core's ``documents.extract_text`` handler
  (``apps.document_text``). ``check_handler_composition`` verifies at import that its
  kinds are exactly the ungated kinds (the legacy ``test.add_numbers`` and Core's ``documents.extract_text``) plus the task kinds the registry declares, so a module kind without a handler, or a handler nobody owns, stops the process
  at start instead of being silently gated or allowed. There is still one worker and one reconciler (``after_iteration``); DOC-2 adds one maintenance hook, ``documents.reconcile_text``, which records
  ``extraction_unavailable`` for text extractions whose task failed for good. The document extraction RUNTIME (store, limits, isolated extractor) is installed separately, from the same entry point, by
  ``config/document_extraction.prepare_document_extraction``.
* ``prepare_worker`` (AI-2a): called once by ``runworker`` before the first task. It decides, from the stored AI extraction settings (``apps.ai_settings``, if
  an administrator saved any) and otherwise from the environment, and explicitly, which extractor the
  ``request_intake.extract`` handler uses (``config/intake_extraction.py``: ``deterministic`` by default, ``ai`` only when asked for, no automatic fallback),
  fails the start-up on a missing or invalid setting (or an unreadable database), and installs the extractor source. It reads once: there is no hot reload.
  The composition above is unchanged by it.
"""

from types import MappingProxyType

from django.db import DatabaseError

from apps.ai_settings.services import load_stored_settings
from apps.document_text.errors import ExtractionConfigurationError as DocumentExtractionConfigurationError
from apps.document_text.execution import reconcile_after_iteration as reconcile_document_text
from apps.document_text.handlers import EXTRACT_TEXT_HANDLER
from apps.document_text.rules import KIND_EXTRACT_TEXT
from apps.intake.errors import ExtractionConfigurationError
from apps.intake.handlers import install_extractor_source
from apps.intake.registry import PRODUCTION_REGISTRY
from apps.intake.services import reconcile_after_iteration
from apps.module_host.gate import make_kind_gate
from apps.module_host.rules import check_handler_composition
from apps.tasks.handlers import TEST_ADD_NUMBERS
from apps.tasks.worker import Composition, MaintenanceHook
from config.document_extraction import prepare_document_extraction
from config.intake_extraction import build_extractor_source
from config.modules import build_registry

# Task kinds that predate modules and belong to none: the queue's deterministic test handler. They need no module state. (Request Intake's
# ``request_intake.extract`` was legacy before MOD-2; a registered module may not also be legacy, ``make_kind_gate`` refuses it.) A new production kind has to
# be decided on, a module's or legacy, rather than silently held or silently allowed: ``check_handler_composition`` enforces it.
LEGACY_UNGATED_KINDS = frozenset({TEST_ADD_NUMBERS})

# DOC-2: Core's own task kinds. Text extraction (``documents.extract_text``, ``apps.document_text``) is Core, not a module (documents are Core's, docs/architecture/document-storage.md section 1), so it
# belongs to no module and needs no module state; it is claimable by every worker that has its runtime installed. Listed apart from the legacy kinds because it is not legacy: a Core kind is decided on,
# here, on purpose, exactly like a module's.
CORE_UNGATED_KINDS = frozenset({KIND_EXTRACT_TEXT})
UNGATED_KINDS = LEGACY_UNGATED_KINDS | CORE_UNGATED_KINDS

# The worker's handler allowlist: Request Intake's composed registry (unchanged) plus Core's text extraction handler.
WORKER_REGISTRY = MappingProxyType({**PRODUCTION_REGISTRY, EXTRACT_TEXT_HANDLER.kind: EXTRACT_TEXT_HANDLER})

MODULE_REGISTRY = build_registry()
check_handler_composition(MODULE_REGISTRY, WORKER_REGISTRY, UNGATED_KINDS)

_module_kind_gate = make_kind_gate(MODULE_REGISTRY, UNGATED_KINDS)
_held_kinds: frozenset = frozenset()  # kinds THIS process holds back (set once by ``prepare_document_runtime``); empty unless the worker's lease is too short for text extraction


def kind_gate() -> frozenset:
    """The module kind gate (``apps.module_host.gate``) minus the kinds this worker process holds back. A held kind stays queued, never failed."""
    return _module_kind_gate() - _held_kinds


COMPOSITION = Composition(
    registry=WORKER_REGISTRY,
    after_iteration=reconcile_after_iteration,
    maintenance_hooks=(MaintenanceHook("documents.reconcile_text", reconcile_document_text),),
    kind_gate=kind_gate,
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


def prepare_document_runtime(lease_seconds: int, environ=None, **kwargs) -> str:
    """DOC-2: install the PDF text extraction runtime for this worker process (``config/document_extraction.py``) and return one line describing it for the operator.

    Raises ``WorkerConfigurationError`` (the message names the setting, never its value) when an ``AP_DOCUMENT_EXTRACTION_*`` setting is invalid, so the worker does not start. When the lease is too short for the
    extraction timeout the worker still starts, but this process then HOLDS BACK ``documents.extract_text`` (its tasks stay queued, never failed; the returned line says so). Independent of ``prepare_worker``: it
    neither reads nor changes Request Intake's extractor. Nothing is connected to and no file is read."""
    global _held_kinds
    try:
        prepared = prepare_document_extraction(lease_seconds, environ, **kwargs)
    except DocumentExtractionConfigurationError as error:
        raise WorkerConfigurationError(str(error)) from None
    _held_kinds = frozenset() if prepared.enabled else CORE_UNGATED_KINDS
    return prepared.description
