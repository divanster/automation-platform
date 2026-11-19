"""The worker's kind gate, built from the static registry and the stored module state (MOD-1b S4).

``make_kind_gate(registry, legacy_kinds)`` returns the callable that ``apps.tasks.worker.Composition.kind_gate`` expects (S2): no arguments, a
frozenset of the task kinds that may be claimed. The worker narrows the answer to the kinds it has a handler for, fails closed if the gate raises or
answers badly, re-raises a ``DatabaseError`` so the existing connection recovery runs, and leaves every task of a kind that is not allowed QUEUED
(held, never failed). Nothing in ``apps.tasks`` changes.

THE ANSWER is ``rules.allowed_kinds``: the LEGACY kinds plus the kinds of every available module (``rules.assess_modules``: enabled, version-matched,
state present once the first sync has completed (the durable marker), dependencies available). A kind that is neither legacy nor declared by an available module is absent, so
a disabled module's kinds, a version-mismatched module's kinds and unknown kinds are all held or left alone.

LEGACY KINDS are the task kinds that existed before modules and belong to none: ``test.add_numbers``. They are claimable with no module state at
all. Request Intake's ``request_intake.extract`` was legacy until MOD-2 registered the module and removed it from the set (``rules.check_legacy_kinds``
refuses a kind that is in both, loudly, when this gate is built); it is gated by the module state like any module's kind.

COST. With an EMPTY registry the gate answers the legacy set without reading the database. With modules (Request Intake, since MOD-2) it reads the state rows in one query
per worker iteration, the same read as ``services.read_host_state`` (the rows, then the durable initialization marker).
"""

from collections.abc import Callable

from apps.modules.registry import ModuleRegistry

from . import rules
from .errors import ModuleHostError
from .services import read_host_state


def make_kind_gate(registry, legacy_kinds) -> Callable[[], frozenset]:
    """The kind gate for ``registry``. Raises ``ModuleHostError`` when the legacy kinds are malformed or one of them is declared by a module."""
    if not isinstance(registry, ModuleRegistry):
        raise ModuleHostError("invalid_registry", "the registry must be an apps.modules.registry.ModuleRegistry")
    legacy = rules.check_legacy_kinds(registry, legacy_kinds)
    if len(registry) == 0:
        return lambda: legacy  # no module can be gated: nothing to read

    def kind_gate() -> frozenset:
        state = read_host_state()
        return rules.allowed_kinds(registry, rules.assess_modules(registry, state.facts, initialized=state.initialized), legacy)

    return kind_gate
