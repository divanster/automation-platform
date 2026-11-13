"""The production handler allowlist: the queue's own handlers plus the request-intake handler.

A fixed, read-only mapping built once at import time from names imported above. Nothing is resolved from a
string, imported by name, evaluated, or registered at run time, and nothing a user supplies can add a kind.
``apps.tasks.handlers.HANDLERS`` stays exactly as it was (the queue app knows no business code); this module
only COMPOSES a larger allowlist. ``config.task_registry`` hands it to the worker, and
``apps.intake.submission`` passes it to ``enqueue_task``, so the kind is checked at enqueue and again at claim.

The queue's deterministic test handler (``test.add_numbers``) stays in the composed allowlist, as it was in
the allowlist before this slice; whether production should drop it is an open decision (see the slice document).
"""

from types import MappingProxyType

from apps.tasks.handlers import HANDLERS

from .handlers import EXTRACT_HANDLER

PRODUCTION_REGISTRY = MappingProxyType({**HANDLERS, EXTRACT_HANDLER.kind: EXTRACT_HANDLER})
