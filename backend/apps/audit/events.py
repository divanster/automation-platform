"""The audit event vocabulary and the shape of one event: pure, standard library only, no database.

An event says WHO did WHAT to which subject, in which case and run. It never carries business content.
``details`` is a small flat object whose keys are fixed per action (``DETAIL_KEYS`` for Core's actions, the owner's declaration for a
module's) and whose values are booleans, integers, ``None``, or short tokens made only of letters, digits and ``_ . : -`` (identifiers,
state names, counts, hashes, reason codes). A value with a space, a quote, or any other character is
refused, so request text, extracted values, names, e-mail addresses, and exception messages cannot be
stored by accident. Anything that needs more detail than that belongs on the business record the event
points at, where access is controlled.

The vocabulary is Core's fixed set (``ACTIONS``, ``DETAIL_KEYS``, ``SUBJECT_TYPES``, below) plus the actions modules have declared, composed once
by ``config/audit_vocabulary.py`` and installed with ``install_vocabulary`` (``apps.audit.vocabulary``). An event is checked against the
INSTALLED vocabulary, which is Core's alone until one is installed. The database CHECK constraint on the action only checks its shape
(``apps.audit.vocabulary.ACTION_SHAPE_PATTERN``): which actions are allowed is a rule of this Python code, not of the database.
"""

import re
import threading
import uuid
from dataclasses import dataclass, field
from types import MappingProxyType

from .vocabulary import AuditVocabulary, AuditVocabularyError

ACTION_REQUEST_SUBMITTED = "request.submitted"
ACTION_REVISION_CREATED = "revision.created"
ACTION_REQUEST_EDITED = "request.edited"
ACTION_RUN_STATE_CHANGED = "run.state_changed"
ACTION_APPROVAL_RECORDED = "approval.recorded"
ACTION_APPROVAL_INVALIDATED = "approval.invalidated"
ACTION_REJECTION_RECORDED = "rejection.recorded"
# MOD-1b S4: the module host (apps.module_host) enables and disables modules. ``module`` is a namespace the module contract reserves for
# Core, so no module can declare these two; they are Core's own actions, recorded with the ``module`` subject type.
ACTION_MODULE_ENABLED = "module.enabled"
ACTION_MODULE_DISABLED = "module.disabled"
# The AI settings API (apps.ai_settings): a successful change of how Request Intake extracts. Core's own action in the Core-reserved ``ai_settings`` namespace;
# no module can declare it. The subject is the singleton settings row (see ``SUBJECT_AI_SETTINGS``).
ACTION_AI_SETTINGS_UPDATED = "ai_settings.updated"
# AI-SET-2: one event per connection a save created, changed or removed (``change``: ``created``, ``updated`` or ``removed``), in the same transaction as the save. Core's own
# action in the same Core-reserved namespace. It carries identifiers, flags and counts only: never an endpoint, a credential reference or a display name.
ACTION_AI_SETTINGS_CONNECTION_CHANGED = "ai_settings.connection_changed"

ACTIONS = (
    ACTION_REQUEST_SUBMITTED,
    ACTION_REVISION_CREATED,
    ACTION_REQUEST_EDITED,
    ACTION_RUN_STATE_CHANGED,
    ACTION_APPROVAL_RECORDED,
    ACTION_APPROVAL_INVALIDATED,
    ACTION_REJECTION_RECORDED,
    ACTION_MODULE_ENABLED,
    ACTION_MODULE_DISABLED,
    ACTION_AI_SETTINGS_UPDATED,
    ACTION_AI_SETTINGS_CONNECTION_CHANGED,
)

SUBJECT_RUN = "run"
SUBJECT_REVISION = "revision"
SUBJECT_APPROVAL = "approval"
SUBJECT_REJECTION = "rejection"
SUBJECT_MODULE = "module"  # a module as a subject: ``module.enabled`` and ``module.disabled`` (S4); ``subject_id`` is the ModuleState id
SUBJECT_AI_SETTINGS = "ai_settings"  # the singleton AI extraction settings row; ``subject_id`` is the constant ``apps.ai_settings.models.SETTINGS_SUBJECT_ID``
SUBJECT_TYPES = (SUBJECT_RUN, SUBJECT_REVISION, SUBJECT_APPROVAL, SUBJECT_REJECTION, SUBJECT_MODULE, SUBJECT_AI_SETTINGS)

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
        # identifiers, a version and a fingerprint only; ``cause`` is a short token such as ``admin_command`` or ``sync``
        ACTION_MODULE_ENABLED: frozenset({"module_id", "installed_version", "registry_fingerprint", "cause"}),
        ACTION_MODULE_DISABLED: frozenset({"module_id", "installed_version", "registry_fingerprint", "cause"}),
        # the saved settings as identifiers and numbers only: never a key, header, endpoint, proxy or free text (every value was checked against the catalog).
        # ``connection_id`` is the selected connection (null: the legacy provider); ``connection_count`` and ``connections_changed`` describe the stored list.
        ACTION_AI_SETTINGS_UPDATED: frozenset({"revision", "extraction_mode", "provider_id", "profile_id", "policy_mode", "timeout_seconds", "connection_id", "connection_count", "connections_changed"}),
        ACTION_AI_SETTINGS_CONNECTION_CHANGED: frozenset({"revision", "change", "connection_id", "provider_type", "locality", "enabled", "has_credential_ref", "profile_count"}),
    }
)

_TOKEN = re.compile(r"[A-Za-z0-9_.:\-]{1,100}")

# Core's vocabulary: exactly the constants above, no module in it. Never replaced; a module-aware vocabulary is built on top of it.
CORE_VOCABULARY = AuditVocabulary.core(actions=ACTIONS, detail_keys=DETAIL_KEYS, subject_types=SUBJECT_TYPES)

_installed: AuditVocabulary | None = None
_install_lock = threading.Lock()


def current_vocabulary() -> AuditVocabulary:
    """The vocabulary events are checked against: the installed one, or Core's alone when nothing has been installed."""
    installed = _installed
    return CORE_VOCABULARY if installed is None else installed


def install_vocabulary(vocabulary: AuditVocabulary) -> AuditVocabulary:
    """Make ``vocabulary`` the one events are checked against. Idempotent, and never a silent replacement.

    Installing a vocabulary equal to the installed one (same fingerprint) changes nothing. Installing a different one when one is already
    installed is refused (``vocabulary_conflict``), and so is one built on a different Core part. It is meant to be called once per process
    by the composition root; the first installation may be any vocabulary built on this Core.
    """
    global _installed
    if not isinstance(vocabulary, AuditVocabulary):
        raise TypeError("vocabulary must be an apps.audit.vocabulary.AuditVocabulary")
    if vocabulary.core_fingerprint != CORE_VOCABULARY.core_fingerprint:
        raise AuditVocabularyError("vocabulary_conflict", "the vocabulary was not built on this Core's audit vocabulary")
    with _install_lock:
        if _installed is None or _installed == vocabulary:
            _installed = vocabulary
            return vocabulary
        raise AuditVocabularyError("vocabulary_conflict", "a different audit vocabulary is already installed in this process")


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
        vocabulary = current_vocabulary()
        if not vocabulary.allows_action(self.action):
            raise AuditEventError("unknown audit action")
        if not vocabulary.allows_subject_type(self.subject_type):
            raise AuditEventError("unknown subject type")
        if not isinstance(self.subject_id, uuid.UUID):
            raise AuditEventError("subject_id must be a uuid.UUID")
        for name in ("case_id", "run_id"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, uuid.UUID):
                raise AuditEventError(f"{name} must be a uuid.UUID or None")
        if not isinstance(self.details, dict):
            raise AuditEventError("details must be an object")
        unknown = set(self.details) - vocabulary.detail_keys_of(self.action)
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
