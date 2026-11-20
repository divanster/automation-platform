"""The allowlist the request service enqueues with: exactly one kind, ``documents.extract_text``. A fixed, read-only mapping built at import time from a name imported above; nothing is resolved from a string or
registered at run time. ``config.task_registry`` composes the worker's production allowlist from this handler (the queue app and Request Intake's registry are unchanged)."""

from types import MappingProxyType

from .handlers import EXTRACT_TEXT_HANDLER

DOCUMENT_TEXT_REGISTRY = MappingProxyType({EXTRACT_TEXT_HANDLER.kind: EXTRACT_TEXT_HANDLER})
