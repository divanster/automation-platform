"""The audit event vocabulary and the shape of one event: pure, standard library only, no database.

An event says WHO did WHAT to which subject, in which case and run. It never carries business content.
``details`` is a small flat object whose keys are fixed per action (``DETAIL_KEYS``) and whose values are
booleans, integers, ``None``, or short tokens made only of letters, digits and ``_ . : -`` (identifiers,
state names, counts, hashes, reason codes). A value with a space, a quote, or any other character is
refused, so request text, extracted values, names, e-mail addresses, and exception messages cannot be
stored by accident. Anything that needs more detail than that belongs on the business record the event
points at, where access is controlled.

The vocabulary is fixed: a database CHECK constraint lists the same actions, so a new action needs a
migration (the same way a new run state does).
"""

import re
import uuid
from dataclasses import dataclass, field
from types import MappingProxyType

ACTION_REQUEST_SUBMITTED = "request.submitted"
ACTION_REVISION_CREATED = "revision.created"
ACTION_REQUEST_EDITED = "request.edited"
ACTION_RUN_STATE_CHANGED = "run.state_changed"
ACTION_APPROVAL_RECORDED = "approval.recorded"
ACTION_APPROVAL_INVALIDATED = "approval.invalidated"
ACTION_REJECTION_RECORDED = "rejection.recorded"

ACTIONS = (
    ACTION_REQUEST_SUBMITTED,
    ACTION_REVISION_CREATED,
    ACTION_REQUEST_EDITED,
    ACTION_RUN_STATE_CHANGED,
    ACTION_APPROVAL_RECORDED,
    ACTION_APPROVAL_INVALIDATED,
    ACTION_REJECTION_RECORDED,
)

SUBJECT_RUN = "run"
SUBJECT_REVISION = "revision"
SUBJECT_APPROVAL = "approval"
SUBJECT_REJECTION = "rejection"
SUBJECT_TYPES = (SUBJECT_RUN, SUBJECT_REVISION, SUBJECT_APPROVAL, SUBJECT_REJECTION)

ACTOR_USER = "user"  # a login user, identified by ``User.public_id``
ACTOR_SYSTEM = "system"  # the worker or the application itself; not a login user
ACTOR_KINDS = (ACTOR_USER, ACTOR_SYSTEM)

# The only keys ``details`` may hold, per action.
DETAIL_KEYS = MappingProxyType(
    {
        ACTION_REQUEST_SUBMITTED: frozenset({"byte_length", "client_request_id_used"}),
        ACTION_REVISION_CREATED: frozenset({"revision_number", "content_hash", "origin", "blocking_count", "warning_count"}),
        ACTION_REQUEST_EDITED: frozenset({"base_revision_id", "first_edit"}),
        ACTION_RUN_STATE_CHANGED: frozenset({"from_state", "to_state", "cause"}),
        ACTION_APPROVAL_RECORDED: frozenset({"revision_id", "content_hash", "warning_count"}),
        ACTION_APPROVAL_INVALIDATED: frozenset({"revision_id", "by_revision_id", "approval_count"}),
        ACTION_REJECTION_RECORDED: frozenset({"revision_id", "content_hash", "reason_code"}),
    }
)

_TOKEN = re.compile(r"[A-Za-z0-9_.:\-]{1,100}")


class AuditEventError(ValueError):
    """An event is malformed. The message names the rule, never the offending value."""


def _check_detail_value(key, value):
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, str) and _TOKEN.fullmatch(value):
        return
    raise AuditEventError(f"the audit detail '{key}' must be a boolean, an integer, null, or a short token")


@dataclass(frozen=True)
class NewAuditEvent:
    """One event to record. Validated on construction; immutable."""

    actor_kind: str
    actor_public_id: uuid.UUID | None
    action: str
    subject_type: str
    subject_id: uuid.UUID
    case_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None
    details: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.actor_kind not in ACTOR_KINDS:
            raise AuditEventError("unknown actor kind")
        if self.actor_kind == ACTOR_USER:
            if not isinstance(self.actor_public_id, uuid.UUID):
                raise AuditEventError("a user actor needs a public id (uuid.UUID)")
        elif self.actor_public_id is not None:
            raise AuditEventError("the system actor has no public id")
        if self.action not in ACTIONS:
            raise AuditEventError("unknown audit action")
        if self.subject_type not in SUBJECT_TYPES:
            raise AuditEventError("unknown subject type")
        if not isinstance(self.subject_id, uuid.UUID):
            raise AuditEventError("subject_id must be a uuid.UUID")
        for name in ("case_id", "run_id"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, uuid.UUID):
                raise AuditEventError(f"{name} must be a uuid.UUID or None")
        if not isinstance(self.details, dict):
            raise AuditEventError("details must be an object")
        unknown = set(self.details) - DETAIL_KEYS[self.action]
        if unknown:
            raise AuditEventError(f"details has keys the action does not allow: {sorted(unknown)}")
        for key, value in self.details.items():
            _check_detail_value(key, value)
        object.__setattr__(self, "details", dict(self.details))  # a private copy: the caller's dict cannot change it later


def user_event(actor_public_id, action, subject_type, subject_id, *, case_id=None, run_id=None, **details) -> NewAuditEvent:
    """An event done by a login user."""
    return NewAuditEvent(ACTOR_USER, actor_public_id, action, subject_type, subject_id, case_id, run_id, details)


def system_event(action, subject_type, subject_id, *, case_id=None, run_id=None, **details) -> NewAuditEvent:
    """An event done by the system (the worker), not by a login user."""
    return NewAuditEvent(ACTOR_SYSTEM, None, action, subject_type, subject_id, case_id, run_id, details)
