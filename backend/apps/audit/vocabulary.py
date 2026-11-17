"""The audit vocabulary as an immutable value: Core's actions plus validated, module-owned declarations. Pure, standard library only.

No Django, no database, no import of ``apps.modules`` or any business code: a composition root (``config/audit_vocabulary.py``) turns a
validated module registry into the plain ``ModuleAuditAction`` values below and passes the namespace owners it learned from the registry.

WHAT THIS ENFORCES (in Python, when an event is constructed; see ``apps.audit.events``):

* The allowed actions are exactly Core's actions plus the actions modules declared. An action nobody declared is refused.
* Core's actions, their detail keys and their subject types are kept as they are. A module cannot claim a Core action, a namespace Core
  uses (the first segment of a Core action) or a reserved namespace, cannot declare an action in a namespace it does not own, and two
  declarations of one action are refused (never "last one wins").
* The detail keys of an action are the ones its owner declared; the value rules (booleans, integers, null, short tokens) stay Core's and
  apply to every action alike.

WHAT THIS DOES NOT ENFORCE. The database CHECK constraint on ``AuditEvent.action`` checks the SHAPE of the text only (``ACTION_SHAPE_PATTERN``),
not membership in this vocabulary: raw SQL, a migration or a database administrator can store any well-formed action and the database will not
notice. Membership is a rule of this Python code. A namespace declaration is a naming rule inside one process, not a sandbox: code admitted to
the process can build events itself, import Core internals or write the table.

The vocabulary is immutable once built, equal vocabularies have equal fingerprints, and building one reads nothing from disk, environment or database.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

VOCABULARY_FORMAT = 1

# The database CHECK on ``AuditEvent.action`` and the Python shape check use this ONE pattern (migration 0002 repeats the text, because a
# migration must not import application code; a test keeps them equal). It accepts what ``AuditActionDeclaration`` accepts in a manifest (two or
# more lower-case dotted segments, each at most 40 characters) and every Core action. The repetition is bounded: the column holds at most 32
# characters, and a segment plus its dot takes at least two, so no valid action has more than 16 segments. PostgreSQL's ``~`` and Python's
# ``re.fullmatch`` read it the same way (only ``^``, ``$``, a bounded group and character classes).
ACTION_SHAPE_PATTERN = r"^[a-z][a-z0-9_]{0,39}(\.[a-z][a-z0-9_]{0,39}){1,15}$"
MAX_ACTION_LENGTH = 32  # apps.audit.models.AuditEvent.action

_ACTION_SHAPE = re.compile(ACTION_SHAPE_PATTERN)
_DETAIL_KEY = re.compile(r"[a-z][a-z0-9_]{0,39}")
_MODULE_ID = re.compile(r"[a-z][a-z0-9_]{2,39}")
MAX_DETAIL_KEYS = 20  # per action, as in a manifest declaration

# A detail key whose NAME suggests content is refused for a module's action, as it is for Core's (tests keep Core's keys clear of these).
FORBIDDEN_DETAIL_KEYS = frozenset({"text", "name", "email", "contact", "notes", "description", "message", "excerpt", "reason", "error", "password", "username"})

_BUILD = object()


class AuditVocabularyError(ValueError):
    """A vocabulary or a declaration is invalid. ``code`` is stable; the message names the rule and the identifiers, never an event's content."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def action_shape_ok(action) -> bool:
    """True when ``action`` is well formed text within the column's length: the same test the database CHECK makes. Says nothing about membership."""
    return isinstance(action, str) and len(action) <= MAX_ACTION_LENGTH and _ACTION_SHAPE.fullmatch(action) is not None


def _namespace_of(action: str) -> str:
    return action.split(".", 1)[0]


@dataclass(frozen=True)
class ModuleAuditAction:
    """One audit action a module declares, with the detail keys it may carry. Plain data: validated on construction, immutable."""

    module_id: str
    action: str
    detail_keys: tuple = ()

    def __post_init__(self):
        if not isinstance(self.module_id, str) or _MODULE_ID.fullmatch(self.module_id) is None:
            raise AuditVocabularyError("invalid_declaration", "a module audit action needs a valid module id")
        if not action_shape_ok(self.action):
            raise AuditVocabularyError("invalid_action_shape", "an audit action must be two or more lower-case dotted identifiers of at most 32 characters")
        if not isinstance(self.detail_keys, (tuple, list, frozenset, set)):
            raise AuditVocabularyError("invalid_declaration", "detail_keys must be a tuple, list or set of text")
        keys = tuple(self.detail_keys)
        if len(keys) > MAX_DETAIL_KEYS:
            raise AuditVocabularyError("invalid_detail_key", f"an audit action has at most {MAX_DETAIL_KEYS} detail keys")
        for key in keys:
            if not isinstance(key, str) or _DETAIL_KEY.fullmatch(key) is None:
                raise AuditVocabularyError("invalid_detail_key", "a detail key must be a lower-case identifier of at most 40 characters")
            if key in FORBIDDEN_DETAIL_KEYS:
                raise AuditVocabularyError("forbidden_detail_key", f"the detail key '{key}' names free content and is not allowed")
        if len(set(keys)) != len(keys):
            raise AuditVocabularyError("invalid_detail_key", "detail_keys contains a duplicate")
        object.__setattr__(self, "detail_keys", tuple(sorted(keys)))


def _read_only(mapping: dict) -> MappingProxyType:
    return MappingProxyType(dict(mapping))


class AuditVocabulary:
    """An immutable set of audit actions, their detail keys and the subject types. Create it with ``AuditVocabulary.core`` and ``with_modules``."""

    __slots__ = ("_core_actions", "_core_detail_keys", "_subject_types", "_actions", "_detail_keys", "_owners", "_fingerprint", "_core_fingerprint")

    def __init__(self, *, _token=None, **state):
        if _token is not _BUILD:
            raise TypeError("an AuditVocabulary is created with AuditVocabulary.core() or .with_modules()")
        for name, value in state.items():
            object.__setattr__(self, name, value)

    def __setattr__(self, name, value):
        raise AttributeError("an AuditVocabulary is immutable")

    def __delattr__(self, name):
        raise AttributeError("an AuditVocabulary is immutable")

    # construction ------------------------------------------------------------------------------------------------------

    @classmethod
    def core(cls, *, actions, detail_keys, subject_types) -> "AuditVocabulary":
        """The vocabulary Core owns, with no module in it. ``detail_keys`` maps each action to the keys it may carry."""
        actions = tuple(actions)
        subject_types = tuple(subject_types)
        if len(set(actions)) != len(actions) or len(set(subject_types)) != len(subject_types):
            raise AuditVocabularyError("invalid_core_vocabulary", "the Core vocabulary repeats an action or a subject type")
        if not all(action_shape_ok(a) for a in actions):
            raise AuditVocabularyError("invalid_core_vocabulary", "a Core action does not have the action shape the database accepts")
        if set(detail_keys) != set(actions):
            raise AuditVocabularyError("invalid_core_vocabulary", "every Core action needs exactly one detail key list")
        frozen_keys = {action: frozenset(detail_keys[action]) for action in actions}
        return cls._make(actions, frozen_keys, subject_types, {}, {})

    def with_modules(self, module_actions=(), namespace_owners=None, reserved_namespaces=frozenset()) -> "AuditVocabulary":
        """A new vocabulary: this one's Core part plus ``module_actions`` (``ModuleAuditAction`` values), or ``AuditVocabularyError``.

        ``namespace_owners`` is the registry's ``namespace -> module id`` mapping: an action's first segment must be owned by the module that
        declares it. ``reserved_namespaces`` are namespaces nobody may use for a module action (Core's, as the registry knows them); the
        first segments of Core's own actions are always refused as well. Declarations are applied in a fixed order (by action), so the
        result depends only on their content.
        """
        if isinstance(module_actions, (str, bytes)) or not hasattr(module_actions, "__iter__"):
            raise AuditVocabularyError("invalid_declaration", "module_actions must be an iterable of ModuleAuditAction objects")
        declared = list(module_actions)
        if not all(isinstance(item, ModuleAuditAction) for item in declared):
            raise AuditVocabularyError("invalid_declaration", "every module audit declaration must be a ModuleAuditAction")
        if declared and not isinstance(namespace_owners, Mapping):
            raise AuditVocabularyError("invalid_declaration", "namespace owners are needed to check module audit actions")
        core_namespaces = {_namespace_of(a) for a in self._core_actions}
        reserved = frozenset(reserved_namespaces) | core_namespaces

        owners: dict[str, str] = {}
        module_keys: dict[str, frozenset] = {}
        for item in sorted(declared, key=lambda d: (d.action, d.module_id)):
            if item.action in self._core_detail_keys:
                raise AuditVocabularyError("core_action_claimed", f"module '{item.module_id}' declares the Core audit action '{item.action}'")
            namespace = _namespace_of(item.action)
            if namespace in reserved:
                raise AuditVocabularyError("core_namespace_claimed", f"module '{item.module_id}' declares '{item.action}' in the reserved or Core namespace '{namespace}'")
            if namespace_owners.get(namespace) != item.module_id:
                raise AuditVocabularyError("namespace_not_owned", f"module '{item.module_id}' does not own the namespace of the audit action '{item.action}'")
            if item.action in owners:
                raise AuditVocabularyError("duplicate_action", f"audit action '{item.action}' is declared by both '{owners[item.action]}' and '{item.module_id}'")
            owners[item.action] = item.module_id
            module_keys[item.action] = frozenset(item.detail_keys)
        return self._make(self._core_actions, dict(self._core_detail_keys), self._subject_types, owners, module_keys)

    @classmethod
    def _make(cls, core_actions, core_detail_keys, subject_types, owners, module_keys) -> "AuditVocabulary":
        detail_keys = dict(core_detail_keys)
        detail_keys.update({a: module_keys[a] for a in sorted(module_keys)})
        core_document = {
            "format": VOCABULARY_FORMAT,
            "actions": list(core_actions),
            "detail_keys": {a: sorted(core_detail_keys[a]) for a in core_actions},
            "subject_types": list(subject_types),
        }
        document = dict(core_document)
        document["module_actions"] = [{"action": a, "module_id": owners[a], "detail_keys": sorted(detail_keys[a])} for a in sorted(owners)]
        return cls(
            _token=_BUILD,
            _core_actions=tuple(core_actions),
            _core_detail_keys=_read_only({a: core_detail_keys[a] for a in core_actions}),
            _subject_types=tuple(subject_types),
            _actions=tuple(detail_keys),
            _detail_keys=_read_only(detail_keys),
            _owners=_read_only({a: owners[a] for a in sorted(owners)}),
            _fingerprint=_digest(document),
            _core_fingerprint=_digest(core_document),
        )

    # read access -------------------------------------------------------------------------------------------------------

    @property
    def actions(self) -> tuple:
        """Core's actions in their declared order, then the module actions sorted by name."""
        return self._actions

    @property
    def core_actions(self) -> tuple:
        return self._core_actions

    @property
    def module_actions(self) -> MappingProxyType:
        """module-owned action -> the id of the module that declared it."""
        return self._owners

    @property
    def subject_types(self) -> tuple:
        return self._subject_types

    @property
    def detail_keys(self) -> MappingProxyType:
        """action -> the frozenset of detail keys it may carry (every action, Core's and the modules')."""
        return self._detail_keys

    @property
    def fingerprint(self) -> str:
        """SHA-256 of the canonical form: equal vocabularies have equal fingerprints, in any process."""
        return self._fingerprint

    @property
    def core_fingerprint(self) -> str:
        """The fingerprint of the Core part alone: it tells whether two vocabularies were built on the same Core."""
        return self._core_fingerprint

    def allows_action(self, action) -> bool:
        return isinstance(action, str) and action in self._detail_keys

    def allows_subject_type(self, subject_type) -> bool:
        return isinstance(subject_type, str) and subject_type in self._subject_types

    def detail_keys_of(self, action: str) -> frozenset:
        """The detail keys ``action`` may carry; an action that is not in the vocabulary is an error, never an empty set."""
        if not self.allows_action(action):
            raise AuditVocabularyError("unknown_action", "that audit action is not in the vocabulary")
        return self._detail_keys[action]

    def owner_of(self, action: str) -> str | None:
        """The module that declared ``action``, or ``None`` for a Core action. An unknown action is an error."""
        if not self.allows_action(action):
            raise AuditVocabularyError("unknown_action", "that audit action is not in the vocabulary")
        return self._owners.get(action)

    def __eq__(self, other) -> bool:
        return isinstance(other, AuditVocabulary) and self._fingerprint == other._fingerprint

    def __hash__(self) -> int:
        return hash(self._fingerprint)

    def __repr__(self) -> str:
        return f"AuditVocabulary(core={len(self._core_actions)}, modules={len(self._owners)}, fingerprint={self._fingerprint[:12]})"


def _digest(document: dict) -> str:
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()
