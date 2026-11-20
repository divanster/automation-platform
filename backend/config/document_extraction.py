"""DOC-2 composition: the runtime a WORKER uses to extract the text of stored PDFs, chosen explicitly from the backend environment. Reads the environment only when a function is called.

The limits come from ``AP_DOCUMENT_EXTRACTION_*`` variables, each optional with a provisional default (NOT measured on real documents) and each checked against the range in
``apps.document_text.rules.ExtractionLimits``; an invalid value is a configuration error that stops the worker at start-up (``runworker``), never a clamp or a silent default:

* ``AP_DOCUMENT_EXTRACTION_MAX_BYTES`` (20 MiB): the largest stored PDF that is extracted; a larger one is a stored ``document_too_large`` result.
* ``AP_DOCUMENT_EXTRACTION_MAX_PAGES`` (200), ``AP_DOCUMENT_EXTRACTION_MAX_PAGE_CHARS`` (100000), ``AP_DOCUMENT_EXTRACTION_MAX_TOTAL_CHARS`` (500000).
* ``AP_DOCUMENT_EXTRACTION_TIMEOUT_SECONDS`` (20): wall clock for the isolated process; its CPU time is limited to the same number.
* ``AP_DOCUMENT_EXTRACTION_MEMORY_MB`` (1024): the isolated process's memory limit.

The worker's lease should cover the timeout plus ``LEASE_MARGIN_SECONDS`` (reading the object, the database work and the result): the queue has no heartbeat, so a run that outlives its lease would be
recovered and run again while still running. A worker whose lease is shorter does NOT claim text extraction tasks (they stay queued) and says so in its log; it still starts and runs everything else as before.

The document store is ``config.document_storage.build_document_store`` (the DOC-1 directory, policy and permissions; unset means disabled, and a task then fails closed and is retried). Nothing here ever
reads a secret, calls a provider or opens a network connection.
"""

import os
from typing import NamedTuple

from apps.document_text import handlers
from apps.document_text.errors import ExtractionConfigurationError
from apps.document_text.execution import ExtractionRuntime
from apps.document_text.extractor_identity import installed_version
from apps.document_text.isolation import IsolatedExtractor
from apps.document_text.rules import MIB, ExtractionLimits
from config.document_storage import build_document_store

LEASE_MARGIN_SECONDS = 15

ENV_MAX_BYTES = "AP_DOCUMENT_EXTRACTION_MAX_BYTES"
ENV_MAX_PAGES = "AP_DOCUMENT_EXTRACTION_MAX_PAGES"
ENV_MAX_PAGE_CHARS = "AP_DOCUMENT_EXTRACTION_MAX_PAGE_CHARS"
ENV_MAX_TOTAL_CHARS = "AP_DOCUMENT_EXTRACTION_MAX_TOTAL_CHARS"
ENV_TIMEOUT_SECONDS = "AP_DOCUMENT_EXTRACTION_TIMEOUT_SECONDS"
ENV_MEMORY_MB = "AP_DOCUMENT_EXTRACTION_MEMORY_MB"

_VARIABLES = (
    (ENV_MAX_BYTES, "max_bytes", 1),
    (ENV_MAX_PAGES, "max_pages", 1),
    (ENV_MAX_PAGE_CHARS, "max_page_chars", 1),
    (ENV_MAX_TOTAL_CHARS, "max_total_chars", 1),
    (ENV_TIMEOUT_SECONDS, "timeout_seconds", 1),
    (ENV_MEMORY_MB, "memory_bytes", MIB),
)


def build_limits(environ=None) -> ExtractionLimits:
    """The limits this process is configured with. Raises ``ExtractionConfigurationError`` naming the variable (never its value)."""
    environ = os.environ if environ is None else environ
    values = {}
    for variable, field, factor in _VARIABLES:
        text = environ.get(variable, "").strip()
        if not text:
            continue  # an empty value counts as unset, the convention of config/env.py
        if not text.isascii() or not text.isdigit():
            raise ExtractionConfigurationError(f"{variable} must be a whole number")
        value = int(text) * factor
        low, high = ExtractionLimits.RANGES[field]
        if not low <= value <= high:
            raise ExtractionConfigurationError(f"{variable} is outside its allowed range")
        values[field] = value
    try:
        return ExtractionLimits(**values)
    except ValueError:
        raise ExtractionConfigurationError(f"{ENV_MAX_PAGE_CHARS} must not exceed {ENV_MAX_TOTAL_CHARS}") from None


def lease_covers(lease_seconds: int, limits: ExtractionLimits) -> bool:
    """Whether a worker lease outlasts the longest extraction (the timeout) plus the reading and database work around it."""
    return lease_seconds >= limits.timeout_seconds + LEASE_MARGIN_SECONDS


def build_runtime(environ=None, *, extractor=None, store_factory=None) -> ExtractionRuntime:
    """The runtime for a worker. ``extractor`` and ``store_factory`` are for tests; production takes the isolated extractor and the configured document store."""
    environ = os.environ if environ is None else environ
    return ExtractionRuntime(
        store_factory=(lambda: build_document_store(environ)) if store_factory is None else store_factory,
        limits=build_limits(environ),
        extractor=IsolatedExtractor() if extractor is None else extractor,
    )


class Preparation(NamedTuple):
    enabled: bool  # False: this worker process must NOT claim text extraction tasks
    description: str  # one line for the operator's log


def prepare_document_extraction(lease_seconds: int, environ=None, *, extractor=None, store_factory=None) -> Preparation:
    """Build the runtime and, when the worker's lease covers it, install it for this worker process. Nothing is connected to and no file is read.

    Raises ``ExtractionConfigurationError`` (the message names the setting, never its value) for an invalid setting, so the worker does not start
    (``config.task_registry.prepare_document_runtime`` is the entry point ``runworker`` calls and turns it into ``WorkerConfigurationError``).

    A lease that is too short for the extraction timeout does NOT stop the worker (that would change how an existing installation with a short ``AP_WORKER_LEASE_SECONDS`` starts): the runtime is NOT
    installed, ``enabled`` is False, the composition root then holds the ``documents.extract_text`` kind back (its tasks stay queued, never failed) and the description says why.
    """
    runtime = build_runtime(environ, extractor=extractor, store_factory=store_factory)
    limits = runtime.limits
    if not lease_covers(lease_seconds, limits):
        handlers.install_runtime(None)
        return Preparation(
            False,
            f"Document text extraction: DISABLED in this worker: its lease ({lease_seconds} s) must be at least the extraction timeout plus {LEASE_MARGIN_SECONDS} s "
            f"({limits.timeout_seconds + LEASE_MARGIN_SECONDS} s); raise AP_WORKER_LEASE_SECONDS or lower {ENV_TIMEOUT_SECONDS}. Text extraction tasks stay queued.",
        )
    handlers.install_runtime(runtime)
    return Preparation(
        True,
        f"Document text extraction: pypdf {installed_version()} in an isolated process; "
        f"timeout {limits.timeout_seconds} s, memory {limits.memory_bytes // MIB} MiB, at most {limits.max_pages} pages, {limits.max_total_chars} characters, {limits.max_bytes // 1024} KiB.",
    )
