"""DB-free tests: the extensible audit vocabulary (MOD-1b S3). No database is touched and none is needed.

They cover the pure vocabulary (``apps.audit.vocabulary``), the installed-vocabulary state and the event check (``apps.audit.events``), the
composition root (``config/audit_vocabulary.py``), the agreement between the Python action shape, the manifest format and the migration, and the
architecture boundaries. What they cannot show is how PostgreSQL treats the constraints: ``tests/test_audit_vocabulary_postgres.py`` does, and
has not been run in the development VM.
"""

import ast
import os
import random
import re
import string
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from apps.audit import events, recorder
from apps.audit.events import AuditEventError, system_event, user_event
from apps.audit.vocabulary import (
    ACTION_SHAPE_PATTERN,
    FORBIDDEN_DETAIL_KEYS,
    AuditVocabulary,
    AuditVocabularyError,
    ModuleAuditAction,
    action_shape_ok,
)
from apps.modules.errors import ManifestError, RegistryError
from apps.modules.manifest import RESERVED_NAMESPACES, AuditActionDeclaration
from config import audit_vocabulary as composition
from config import modules as module_root
from tests.audit_support import (
    DEMO_ACTION,
    CleanAuditVocabularyMixin,
    audit_module,
    demo_vocabulary,
    installed,
    registry_of,
    vocabulary_of,
)
from tests.module_support import intake_like_manifest, manifest

BACKEND = Path(__file__).resolve().parent.parent
STDLIB = set(sys.stdlib_module_names)
ACTOR, SUBJECT, CASE, RUN = (uuid.uuid4() for _ in range(4))

# A frozen copy of Core's vocabulary: the seven actions that existed before S3, plus "module.enabled" and "module.disabled", which MOD-1b S4 added as Core's
# own actions (the "module" namespace is reserved for Core, so no module can declare them), plus "ai_settings.updated" (the AI settings API; the
# "ai_settings" namespace is Core's too) and "ai_settings.connection_changed" (AI-SET-2: one event per connection a save created, changed or removed).
# The "module" and "ai_settings" subject types are asserted separately. If an action or a detail key of Core ever
# changes, this table is what makes the change visible.
CORE_ACTIONS = (
    "request.submitted",
    "revision.created",
    "request.edited",
    "run.state_changed",
    "approval.recorded",
    "approval.invalidated",
    "rejection.recorded",
    "module.enabled",
    "module.disabled",
    "ai_settings.updated",
    "ai_settings.connection_changed",
)
CORE_KEYS = {
    "request.submitted": {"byte_length", "client_request_id_used"},
    "revision.created": {"revision_number", "content_hash", "origin", "blocking_count", "warning_count"},
    "request.edited": {"base_revision_id", "first_edit"},
    "run.state_changed": {"from_state", "to_state", "cause"},
    "approval.recorded": {"revision_id", "content_hash", "warning_count"},
    "approval.invalidated": {"revision_id", "by_revision_id", "approval_count"},
    "rejection.recorded": {"revision_id", "content_hash", "reason_code"},
    "module.enabled": {"module_id", "installed_version", "registry_fingerprint", "cause"},
    "module.disabled": {"module_id", "installed_version", "registry_fingerprint", "cause"},
    "ai_settings.updated": {"revision", "extraction_mode", "provider_id", "profile_id", "policy_mode", "timeout_seconds", "connection_id", "connection_count", "connections_changed"},
    "ai_settings.connection_changed": {"revision", "change", "connection_id", "provider_type", "locality", "enabled", "has_credential_ref", "profile_count"},
}
CORE_SUBJECTS = ("run", "revision", "approval", "rejection", "module", "ai_settings")


def owners_of(**namespaces):
    return dict(namespaces)


def decl(module_id, action, keys=()):
    return ModuleAuditAction(module_id, action, keys)


def demo_event(action=DEMO_ACTION, subject="run", **details):
    details = details or {"count": 3}
    return user_event(ACTOR, action, subject, SUBJECT, case_id=CASE, run_id=RUN, **details)


class CoreVocabularyIsUnchangedTests(CleanAuditVocabularyMixin, SimpleTestCase):
    def test_core_actions_and_their_detail_keys_are_exactly_the_ones_that_existed(self):
        self.assertEqual(events.ACTIONS, CORE_ACTIONS)
        self.assertEqual({a: set(k) for a, k in events.DETAIL_KEYS.items()}, CORE_KEYS)
        self.assertEqual(events.CORE_VOCABULARY.core_actions, CORE_ACTIONS)
        self.assertEqual({a: set(k) for a, k in events.CORE_VOCABULARY.detail_keys.items()}, CORE_KEYS)

    def test_core_subject_types_are_the_old_four_plus_module_and_ai_settings(self):
        self.assertEqual(events.SUBJECT_TYPES, CORE_SUBJECTS)
        self.assertEqual(events.CORE_VOCABULARY.subject_types, CORE_SUBJECTS)
        self.assertEqual(events.SUBJECT_MODULE, "module")
        self.assertEqual(events.SUBJECT_AI_SETTINGS, "ai_settings")

    def test_the_core_fingerprint_equals_one_built_from_the_frozen_copy(self):
        rebuilt = AuditVocabulary.core(actions=CORE_ACTIONS, detail_keys=CORE_KEYS, subject_types=CORE_SUBJECTS)
        self.assertEqual(rebuilt.fingerprint, events.CORE_VOCABULARY.fingerprint)
        changed = dict(CORE_KEYS, **{"run.state_changed": {"from_state", "to_state"}})
        self.assertNotEqual(AuditVocabulary.core(actions=CORE_ACTIONS, detail_keys=changed, subject_types=CORE_SUBJECTS).fingerprint, rebuilt.fingerprint)

    def test_with_nothing_installed_the_core_vocabulary_is_in_force(self):
        self.assertIs(events.current_vocabulary(), events.CORE_VOCABULARY)
        self.assertEqual(events.CORE_VOCABULARY.module_actions, {})

    def test_every_core_event_is_still_accepted_with_every_allowed_key(self):
        for action, keys in CORE_KEYS.items():
            for subject in CORE_SUBJECTS:
                with self.subTest(action=action, subject=subject):
                    user_event(ACTOR, action, subject, SUBJECT, **{key: "ok_value-1" for key in keys})

    def test_core_still_refuses_what_it_refused(self):
        with self.assertRaises(AuditEventError):
            user_event(ACTOR, "request.deleted", "run", SUBJECT)
        with self.assertRaises(AuditEventError):
            user_event(ACTOR, "run.state_changed", "document", SUBJECT)
        with self.assertRaises(AuditEventError):
            user_event(ACTOR, "run.state_changed", "run", SUBJECT, content_hash="a" * 64)
        with self.assertRaises(AuditEventError):
            user_event(ACTOR, DEMO_ACTION, "run", SUBJECT, count=1)  # a module action is not Core's, and nothing is installed

    def test_no_core_detail_key_names_free_content(self):
        for action, keys in events.DETAIL_KEYS.items():
            with self.subTest(action=action):
                self.assertEqual(set(keys) & FORBIDDEN_DETAIL_KEYS, set())

    def test_the_built_in_modules_declare_no_audit_action_so_the_built_in_composition_equals_core(self):
        # MOD-2: Request Intake is built in, and every audit action it emits is Core's (the request, revision, run, approval and rejection namespaces are
        # reserved), so it declares none and the vocabulary events are checked against is exactly Core's.
        self.assertEqual([m.module_id for m in module_root.BUILTIN_MODULES], ["request_intake"])
        self.assertEqual([a for m in module_root.BUILTIN_MODULES for a in m.audit_actions], [])
        built = composition.build_audit_vocabulary()
        self.assertEqual(built, events.CORE_VOCABULARY)
        self.assertEqual(built.fingerprint, events.CORE_VOCABULARY.fingerprint)

    def test_a_module_without_audit_actions_adds_nothing(self):
        self.assertEqual(vocabulary_of(intake_like_manifest(), manifest(module_id="plain_one")), events.CORE_VOCABULARY)


class ModuleActionsTests(CleanAuditVocabularyMixin, SimpleTestCase):
    def test_a_declared_action_is_accepted_only_while_its_vocabulary_is_installed(self):
        with self.assertRaises(AuditEventError):
            demo_event()
        with installed(demo_vocabulary()):
            event = demo_event()
            self.assertEqual((event.action, event.details), (DEMO_ACTION, {"count": 3}))
        with self.assertRaises(AuditEventError):
            demo_event()  # restored: nothing leaked out of the block

    def test_system_events_work_for_module_actions_too(self):
        with installed(demo_vocabulary()):
            event = system_event(DEMO_ACTION, "module", SUBJECT, count=1)
        self.assertEqual((event.actor_kind, event.actor_public_id), ("system", None))

    def test_an_undeclared_action_in_a_modules_own_namespace_is_refused(self):
        with installed(demo_vocabulary()):
            for action in ("audit_demo.other", "audit_demo.did_it.more", "audit_demo", "AUDIT_DEMO.did_it", "other.did_it"):
                with self.subTest(action=action), self.assertRaises(AuditEventError):
                    demo_event(action)

    def test_only_the_declared_detail_keys_are_accepted(self):
        with installed(demo_vocabulary()):
            demo_event(count=1, reason_code="x_1")
            demo_event()  # a subset, and an empty one, are fine
            user_event(ACTOR, DEMO_ACTION, "run", SUBJECT)
            for bad in ({"other": 1}, {"count": 1, "other": 1}, {"from_state": "a"}, {"revision_id": "x"}):
                with self.subTest(details=bad), self.assertRaises(AuditEventError) as caught:
                    demo_event(**bad)
                self.assertIn("keys the action does not allow", str(caught.exception))

    def test_a_modules_keys_do_not_leak_to_core_actions_or_to_other_modules(self):
        other = audit_module("other_mod", (("other_mod.did_it", ("size",)),))
        with installed(vocabulary_of(audit_module(), other)):
            with self.assertRaises(AuditEventError):
                user_event(ACTOR, "run.state_changed", "run", SUBJECT, count=1)
            with self.assertRaises(AuditEventError):
                user_event(ACTOR, "other_mod.did_it", "run", SUBJECT, count=1)
            user_event(ACTOR, "other_mod.did_it", "run", SUBJECT, size=2)

    def test_core_actions_keep_working_unchanged_next_to_module_actions(self):
        with installed(demo_vocabulary()):
            for action, keys in CORE_KEYS.items():
                user_event(ACTOR, action, "run", SUBJECT, **{key: 1 for key in keys})
            with self.assertRaises(AuditEventError):
                user_event(ACTOR, "run.state_changed", "run", SUBJECT, count=1)

    def test_the_module_subject_type_is_core_and_other_subject_types_are_still_refused(self):
        user_event(ACTOR, "run.state_changed", "module", SUBJECT, cause="x")  # Core subject type, Core vocabulary
        with installed(demo_vocabulary()):
            demo_event(subject="module")
            for subject in ("document", "Module", "", "modules", None, 5, ["module"]):
                with self.subTest(subject=subject), self.assertRaises(AuditEventError):
                    demo_event(subject=subject)

    def test_the_value_rules_apply_to_module_actions_exactly_as_to_core_actions(self):
        with installed(demo_vocabulary()):
            for value in (None, True, 0, 12, "a" * 100, "a:b-c_d.e", str(uuid.uuid4())):
                with self.subTest(value=value):
                    demo_event(count=value)
            unsafe = ["two words", "Jane Doe", "jane@example.invalid", "line\nbreak", "quote'", "a/b", "", "x" * 101, b"bytes", 1.5, ["list"], {"k": "v"}, uuid.uuid4()]
            for value in unsafe:
                with self.subTest(value=value), self.assertRaises(AuditEventError) as caught:
                    demo_event(count=value)
                self.assertNotIn("Jane", str(caught.exception))

    def test_a_malformed_action_value_is_an_event_error_not_a_type_error(self):
        with installed(demo_vocabulary()):
            for action in (None, 5, ["a.b"], {"a": 1}, b"audit_demo.did_it", ("a", "b")):
                with self.subTest(action=action), self.assertRaises(AuditEventError):
                    user_event(ACTOR, action, "run", SUBJECT)

    def test_the_event_checked_is_the_installed_one_at_the_moment_of_construction(self):
        with installed(demo_vocabulary()):
            event = demo_event()
        self.assertEqual(event.action, DEMO_ACTION)  # an already built, immutable event is not re-judged
        with self.assertRaises(Exception):
            event.action = "x.y"


class DeclarationRefusalTests(CleanAuditVocabularyMixin, SimpleTestCase):
    CORE = events.CORE_VOCABULARY

    def assertCode(self, code, function, *args, **kwargs):
        with self.assertRaises(AuditVocabularyError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        return caught.exception

    def build(self, declarations, owners=None, reserved=frozenset()):
        return self.CORE.with_modules(declarations, owners, reserved)

    def test_a_module_cannot_claim_a_core_action(self):
        for action in CORE_ACTIONS:
            with self.subTest(action=action):
                self.assertCode("core_action_claimed", self.build, [decl("some_mod", action)], owners_of(**{action.split(".")[0]: "some_mod"}))

    def test_a_module_cannot_declare_in_a_namespace_core_uses_even_if_the_registry_would_allow_it(self):
        # The manifest reserves these too (so no registry can say a module owns them); the vocabulary refuses them on its own as well, as defence in
        # depth, even when it is handed an owners mapping and no reserved set.
        for namespace in ("request", "run", "revision", "approval", "rejection"):
            with self.subTest(namespace=namespace):
                self.assertCode("core_namespace_claimed", self.build, [decl("some_mod", f"{namespace}.brand_new")], owners_of(**{namespace: "some_mod"}))

    def test_reserved_namespaces_are_refused_even_when_a_mapping_says_the_module_owns_them(self):
        for namespace in sorted(RESERVED_NAMESPACES):
            with self.subTest(namespace=namespace):
                self.assertCode(
                    "core_namespace_claimed", self.build, [decl("some_mod", f"{namespace}.thing")], owners_of(**{namespace: "some_mod"}), RESERVED_NAMESPACES
                )

    def test_the_namespace_must_be_owned_by_the_declaring_module(self):
        self.assertCode("namespace_not_owned", self.build, [decl("some_mod", "some_mod.did_it")], owners_of())
        self.assertCode("namespace_not_owned", self.build, [decl("some_mod", "some_mod.did_it")], owners_of(some_mod="another_mod"))
        self.assertCode("namespace_not_owned", self.build, [decl("some_mod", "another_mod.did_it")], owners_of(some_mod="some_mod"))
        self.assertCode("namespace_not_owned", self.build, [decl("some_mod", "some.did_it")], owners_of(some_mod="some_mod"))  # prefix is not ownership
        self.build([decl("some_mod", "some_mod.did_it")], owners_of(some_mod="some_mod"))
        self.build([decl("some_mod", "legacy.did_it")], owners_of(some_mod="some_mod", legacy="some_mod"))  # an owned extra namespace

    def test_owners_are_required_when_there_is_anything_to_check(self):
        self.assertCode("invalid_declaration", self.build, [decl("some_mod", "some_mod.did_it")], None)
        self.assertCode("invalid_declaration", self.build, [decl("some_mod", "some_mod.did_it")], [("some_mod", "some_mod")])
        self.assertEqual(self.build([], None), self.CORE)

    def test_duplicates_and_conflicting_declarations_are_refused_not_overwritten(self):
        owners = owners_of(some_mod="some_mod")
        error = self.assertCode("duplicate_action", self.build, [decl("some_mod", "some_mod.did_it", ("a",)), decl("some_mod", "some_mod.did_it", ("a",))], owners)
        self.assertIn("some_mod.did_it", str(error))
        self.assertCode("duplicate_action", self.build, [decl("some_mod", "some_mod.did_it", ("a",)), decl("some_mod", "some_mod.did_it", ("b",))], owners)
        both = owners_of(some_mod="some_mod")
        self.assertCode("namespace_not_owned", self.build, [decl("some_mod", "some_mod.did_it"), decl("rival_mod", "some_mod.did_it")], both)

    def test_when_several_declarations_are_wrong_the_error_does_not_depend_on_their_order(self):
        owners = owners_of(some_mod="some_mod")
        wrong_namespace = decl("some_mod", "elsewhere.did_it")  # sorts first: refused as not owned
        core_prefix = decl("some_mod", "request.later")  # refused as a Core namespace
        for order in ([wrong_namespace, core_prefix], [core_prefix, wrong_namespace]):
            error = self.assertCode("namespace_not_owned", self.build, order, owners)
            self.assertIn("elsewhere.did_it", str(error))

    def test_two_manifests_that_declare_one_action_never_reach_the_vocabulary(self):
        first = audit_module("first_mod", (("shared.did_it", ()),), extra_namespaces=("shared",))
        second = audit_module("second_mod", (("shared.did_it", ()),), extra_namespaces=("shared",))
        with self.assertRaises(RegistryError):
            registry_of(first, second)

    def test_a_manifest_cannot_declare_a_core_action_either(self):
        for action in CORE_ACTIONS:
            with self.subTest(action=action), self.assertRaises(ManifestError):
                audit_module("some_mod", ((action, ()),))

    def test_a_manifest_cannot_own_a_core_audit_prefix_so_no_registry_can_reach_the_vocabulary_with_one(self):
        for prefix in ("request", "run", "revision", "approval", "rejection"):
            with self.subTest(prefix=prefix):
                with self.assertRaises(ManifestError) as caught:
                    audit_module("some_mod", ((f"{prefix}.deleted", ("count",)),), extra_namespaces=(prefix,))
                self.assertEqual(caught.exception.code, "reserved_namespace")

    def test_the_action_must_have_the_shape(self):
        bad = ["", "single", ".a.b", "a.b.", "a..b", "A.b", "a.B", "a.1b", "1a.b", "a.b-c", "a.b c", "a.b\n", "a.b\r\n", "\na.b", "ä.b", "a.b/c", "a" * 33 + ".b", "x." + "a" * 31]
        for action in bad:
            with self.subTest(action=action):
                self.assertCode("invalid_action_shape", ModuleAuditAction, "some_mod", action)
        for action in (None, 5, b"a.b", ["a.b"]):
            with self.subTest(action=action):
                self.assertCode("invalid_action_shape", ModuleAuditAction, "some_mod", action)

    def test_the_detail_keys_are_checked(self):
        for keys in (("Upper",), ("a b",), ("1a",), ("x" * 41,), ("a", "a"), tuple(f"key{i}" for i in range(21)), (5,), (None,), ("",)):
            with self.subTest(keys=keys):
                self.assertCode("invalid_detail_key", ModuleAuditAction, "some_mod", "some_mod.did_it", keys)
        for name in sorted(FORBIDDEN_DETAIL_KEYS):
            with self.subTest(name=name):
                self.assertCode("forbidden_detail_key", ModuleAuditAction, "some_mod", "some_mod.did_it", (name,))
        self.assertCode("invalid_declaration", ModuleAuditAction, "some_mod", "some_mod.did_it", "count")  # a bare string is not a key list
        self.assertCode("invalid_declaration", ModuleAuditAction, "Bad Id", "some_mod.did_it", ())
        self.assertCode("invalid_declaration", ModuleAuditAction, None, "some_mod.did_it", ())

    def test_a_manifest_key_naming_content_is_refused_by_the_manifest_itself(self):
        for name in sorted(FORBIDDEN_DETAIL_KEYS):
            with self.subTest(name=name), self.assertRaises(ManifestError) as caught:
                audit_module("some_mod", (("some_mod.did_it", (name,)),))
            self.assertEqual(caught.exception.code, "forbidden_audit_detail_key")

    def test_the_declarations_must_be_declaration_objects(self):
        for bad in ("some_mod.did_it", b"x", 5, [("some_mod", "some_mod.did_it")], [{"action": "some_mod.did_it"}], [AuditActionDeclaration("some_mod.did_it")]):
            with self.subTest(bad=bad):
                self.assertCode("invalid_declaration", self.build, bad, owners_of(some_mod="some_mod"))

    def test_the_core_part_is_validated(self):
        ok_keys = {"a.b": set()}
        self.assertCode("invalid_core_vocabulary", AuditVocabulary.core, actions=("a.b", "a.b"), detail_keys=ok_keys, subject_types=("run",))
        self.assertCode("invalid_core_vocabulary", AuditVocabulary.core, actions=("a.b",), detail_keys=ok_keys, subject_types=("run", "run"))
        self.assertCode("invalid_core_vocabulary", AuditVocabulary.core, actions=("BAD",), detail_keys={"BAD": set()}, subject_types=("run",))
        self.assertCode("invalid_core_vocabulary", AuditVocabulary.core, actions=("a.b",), detail_keys={}, subject_types=("run",))
        self.assertCode("invalid_core_vocabulary", AuditVocabulary.core, actions=("a.b",), detail_keys={"a.b": set(), "c.d": set()}, subject_types=("run",))

    def test_error_messages_name_identifiers_never_event_content(self):
        error = self.assertCode("namespace_not_owned", self.build, [decl("some_mod", "other_ns.did_it", ("count",))], owners_of())
        self.assertNotIn("count", str(error))


class VocabularyValueTests(CleanAuditVocabularyMixin, SimpleTestCase):
    def test_actions_are_core_first_in_core_order_then_modules_sorted(self):
        vocabulary = vocabulary_of(
            audit_module("zulu_mod", (("zulu_mod.b", ()), ("zulu_mod.a", ()))), audit_module("alpha_mod", (("alpha_mod.z", ()),))
        )
        self.assertEqual(vocabulary.actions, CORE_ACTIONS + ("alpha_mod.z", "zulu_mod.a", "zulu_mod.b"))
        self.assertEqual(dict(vocabulary.module_actions), {"alpha_mod.z": "alpha_mod", "zulu_mod.a": "zulu_mod", "zulu_mod.b": "zulu_mod"})
        self.assertEqual(vocabulary.core_actions, CORE_ACTIONS)

    def test_queries(self):
        vocabulary = demo_vocabulary()
        self.assertTrue(vocabulary.allows_action(DEMO_ACTION) and vocabulary.allows_action("run.state_changed"))
        self.assertFalse(vocabulary.allows_action("audit_demo.nope") or vocabulary.allows_action(None) or vocabulary.allows_action(["a"]))
        self.assertTrue(vocabulary.allows_subject_type("module") and not vocabulary.allows_subject_type("document") and not vocabulary.allows_subject_type(None))
        self.assertEqual(vocabulary.detail_keys_of(DEMO_ACTION), frozenset({"count", "reason_code"}))
        self.assertEqual(vocabulary.detail_keys_of("run.state_changed"), frozenset(CORE_KEYS["run.state_changed"]))
        self.assertEqual((vocabulary.owner_of(DEMO_ACTION), vocabulary.owner_of("run.state_changed")), ("audit_demo", None))
        for method in (vocabulary.detail_keys_of, vocabulary.owner_of):
            with self.assertRaises(AuditVocabularyError) as caught:
                method("audit_demo.nope")  # an unknown action is an error, never an empty answer
            self.assertEqual(caught.exception.code, "unknown_action")

    def test_the_result_is_deterministic_and_independent_of_input_order(self):
        declarations = [decl("zulu_mod", "zulu_mod.b", ("y", "x")), decl("zulu_mod", "zulu_mod.a"), decl("alpha_mod", "alpha_mod.z", ("k",))]
        owners = owners_of(zulu_mod="zulu_mod", alpha_mod="alpha_mod")
        results = {events.CORE_VOCABULARY.with_modules(order, owners).fingerprint for order in (declarations, declarations[::-1], declarations[1:] + declarations[:1])}
        self.assertEqual(len(results), 1)
        self.assertEqual(results.pop(), events.CORE_VOCABULARY.with_modules(tuple(declarations), owners).fingerprint)

    def test_composition_does_not_depend_on_manifest_order(self):
        manifests = [audit_module("zulu_mod", (("zulu_mod.a", ("x",)),)), audit_module("alpha_mod", (("alpha_mod.z", ()),)), manifest(module_id="plain_one")]
        first = vocabulary_of(*manifests)
        self.assertEqual(vocabulary_of(*manifests[::-1]), first)
        self.assertEqual(vocabulary_of(*manifests[::-1]).actions, first.actions)

    def test_the_fingerprint_is_the_same_in_other_processes(self):
        script = (
            "import sys; sys.path.insert(0, sys.argv[1]);"
            "from tests.audit_support import vocabulary_of, audit_module;"
            "v = vocabulary_of(audit_module('zulu_mod', (('zulu_mod.a', ('x', 'y')),)), audit_module('alpha_mod', (('alpha_mod.z', ()),)));"
            "print(v.fingerprint)"
        )
        outputs = set()
        for seed in ("0", "1", "12345", "random"):
            done = subprocess.run([sys.executable, "-c", script, str(BACKEND)], capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": seed}, cwd=BACKEND, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            outputs.add(done.stdout.strip())
        self.assertEqual(len(outputs), 1, outputs)

    def test_the_fingerprint_reflects_every_part_of_a_declaration(self):
        base = vocabulary_of(audit_module("some_mod", (("some_mod.did_it", ("a",)),)))
        self.assertNotEqual(base, vocabulary_of(audit_module("some_mod", (("some_mod.did_it", ("a", "b")),))))
        self.assertNotEqual(base, vocabulary_of(audit_module("some_mod", (("some_mod.did_it2", ("a",)),))))
        self.assertNotEqual(base, events.CORE_VOCABULARY)
        self.assertEqual(base, vocabulary_of(audit_module("some_mod", (("some_mod.did_it", ("a",)),))))
        self.assertEqual(hash(base), hash(vocabulary_of(audit_module("some_mod", (("some_mod.did_it", ("a",)),)))))
        self.assertEqual(base.core_fingerprint, events.CORE_VOCABULARY.core_fingerprint)

    def test_it_is_immutable(self):
        vocabulary = demo_vocabulary()
        for name in ("actions", "_actions", "extra", "_fingerprint"):
            with self.subTest(name=name):
                with self.assertRaises(AttributeError):
                    setattr(vocabulary, name, ())
                with self.assertRaises(AttributeError):
                    delattr(vocabulary, name)
        self.assertFalse(hasattr(vocabulary, "__dict__"))
        self.assertIsInstance(vocabulary.actions, tuple)
        self.assertIsInstance(vocabulary.subject_types, tuple)
        self.assertIsInstance(vocabulary.detail_keys[DEMO_ACTION], frozenset)
        for mapping in (vocabulary.detail_keys, vocabulary.module_actions):
            with self.assertRaises(TypeError):
                mapping["x.y"] = frozenset()
        with self.assertRaises(AttributeError):
            vocabulary.detail_keys[DEMO_ACTION].add("x")
        with self.assertRaises(TypeError):
            AuditVocabulary()  # only core() and with_modules() create one

    def test_changing_the_inputs_afterwards_changes_nothing(self):
        declarations = [decl("some_mod", "some_mod.did_it", ["a"])]
        owners = {"some_mod": "some_mod"}
        vocabulary = events.CORE_VOCABULARY.with_modules(declarations, owners)
        declarations.append(decl("some_mod", "some_mod.more"))
        owners["some_mod"] = "someone_else"
        self.assertEqual(vocabulary.actions, CORE_ACTIONS + ("some_mod.did_it",))
        self.assertEqual(vocabulary.owner_of("some_mod.did_it"), "some_mod")

    def test_a_declaration_is_frozen_and_normalizes_its_keys(self):
        declaration = decl("some_mod", "some_mod.did_it", ["b", "a"])
        self.assertEqual(declaration.detail_keys, ("a", "b"))
        self.assertEqual(decl("some_mod", "some_mod.did_it", {"b", "a"}), declaration)
        with self.assertRaises(Exception):
            declaration.action = "x.y"

    def test_extending_does_not_touch_the_base_vocabulary(self):
        before = events.CORE_VOCABULARY.fingerprint
        demo_vocabulary()
        self.assertEqual(events.CORE_VOCABULARY.fingerprint, before)
        self.assertEqual(events.CORE_VOCABULARY.actions, CORE_ACTIONS)


class InstallationTests(CleanAuditVocabularyMixin, SimpleTestCase):
    def test_nothing_is_installed_at_the_start_of_every_test(self):
        self.assertIsNone(events._installed)
        self.assertIs(events.current_vocabulary(), events.CORE_VOCABULARY)

    def test_installing_makes_the_vocabulary_current(self):
        vocabulary = demo_vocabulary()
        self.assertIs(events.install_vocabulary(vocabulary), vocabulary)
        self.assertIs(events.current_vocabulary(), vocabulary)
        demo_event()

    def test_installing_it_again_is_idempotent_even_for_an_equal_but_distinct_object(self):
        first = events.install_vocabulary(demo_vocabulary())
        again = events.install_vocabulary(demo_vocabulary())
        self.assertEqual(again, first)
        self.assertEqual(events.current_vocabulary(), first)
        events.install_vocabulary(first)

    def test_a_conflicting_installation_is_refused_and_changes_nothing(self):
        first = events.install_vocabulary(demo_vocabulary())
        other = vocabulary_of(audit_module("other_mod", (("other_mod.did_it", ()),)))
        for conflicting in (other, events.CORE_VOCABULARY, vocabulary_of(audit_module(actions=((DEMO_ACTION, ("count",)),)))):
            with self.subTest(conflicting=repr(conflicting)), self.assertRaises(AuditVocabularyError) as caught:
                events.install_vocabulary(conflicting)
            self.assertEqual(caught.exception.code, "vocabulary_conflict")
            self.assertIs(events.current_vocabulary(), first)

    def test_a_core_only_installation_is_also_not_silently_replaced(self):
        events.install_vocabulary(events.CORE_VOCABULARY)
        with self.assertRaises(AuditVocabularyError):
            events.install_vocabulary(demo_vocabulary())
        with self.assertRaises(AuditEventError):
            demo_event()

    def test_only_a_vocabulary_built_on_this_core_can_be_installed(self):
        foreign = AuditVocabulary.core(actions=("a.b",), detail_keys={"a.b": set()}, subject_types=("run",))
        with self.assertRaises(AuditVocabularyError) as caught:
            events.install_vocabulary(foreign)
        self.assertEqual(caught.exception.code, "vocabulary_conflict")
        for bad in (None, "vocabulary", {"actions": ()}, events.CORE_VOCABULARY.actions):
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                events.install_vocabulary(bad)
        self.assertIsNone(events._installed)

    def test_the_composition_root_installs_idempotently_and_refuses_a_second_different_registry(self):
        registry = registry_of(audit_module())
        first = composition.install_audit_vocabulary(registry)
        self.assertEqual(composition.install_audit_vocabulary(registry), first)
        self.assertEqual(composition.install_audit_vocabulary(registry_of(audit_module())), first)
        with self.assertRaises(AuditVocabularyError):
            composition.install_audit_vocabulary(registry_of(audit_module("other_mod", (("other_mod.did_it", ()),))))
        self.assertEqual(events.current_vocabulary(), first)
        demo_event()

    def test_installing_the_built_ins_changes_nothing_while_there_are_none(self):
        installed_vocabulary = composition.install_audit_vocabulary()
        self.assertEqual(installed_vocabulary, events.CORE_VOCABULARY)
        self.assertEqual(composition.install_audit_vocabulary(), installed_vocabulary)
        self.assertEqual(events.current_vocabulary().actions, CORE_ACTIONS)

    def test_a_failed_build_installs_nothing(self):
        registry = registry_of(audit_module())
        failure = AuditVocabularyError("duplicate_action", "simulated")
        with mock.patch.object(AuditVocabulary, "with_modules", side_effect=failure), self.assertRaises(AuditVocabularyError):
            composition.install_audit_vocabulary(registry)
        self.assertIsNone(events._installed)

    def test_the_registry_argument_must_be_a_registry(self):
        for bad in ([audit_module()], audit_module(), {"audit_demo": 1}, "registry"):
            with self.subTest(bad=type(bad).__name__), self.assertRaises(TypeError):
                composition.build_audit_vocabulary(bad)

    def test_installation_runs_under_the_lock(self):
        taken = []

        class Recording:
            def __enter__(self):
                taken.append("enter")

            def __exit__(self, *exc):
                taken.append("exit")

        with mock.patch.object(events, "_install_lock", Recording()):
            events.install_vocabulary(demo_vocabulary())
            with self.assertRaises(AuditVocabularyError):
                events.install_vocabulary(events.CORE_VOCABULARY)  # the refusal is decided under the lock too
        self.assertEqual(taken, ["enter", "exit", "enter", "exit"])

    def test_concurrent_identical_installations_all_succeed(self):
        vocabularies = [demo_vocabulary() for _ in range(8)]
        barrier, errors = threading.Barrier(8), []

        def install(vocabulary):
            barrier.wait(timeout=10)
            try:
                events.install_vocabulary(vocabulary)
            except Exception as error:  # noqa: BLE001 - collected and asserted below
                errors.append(error)

        threads = [threading.Thread(target=install, args=(v,)) for v in vocabularies]
        [t.start() for t in threads]
        [t.join(timeout=30) for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(events.current_vocabulary(), vocabularies[0])

    def test_concurrent_different_installations_leave_exactly_one_winner(self):
        candidates = [vocabulary_of(audit_module(f"mod_{i:02d}", ((f"mod_{i:02d}.did_it", ()),))) for i in range(8)]
        barrier, outcomes = threading.Barrier(8), []

        def install(vocabulary):
            barrier.wait(timeout=10)
            try:
                events.install_vocabulary(vocabulary)
                outcomes.append(("won", vocabulary))
            except AuditVocabularyError as error:
                outcomes.append((error.code, vocabulary))

        threads = [threading.Thread(target=install, args=(v,)) for v in candidates]
        [t.start() for t in threads]
        [t.join(timeout=30) for t in threads]
        winners = [v for result, v in outcomes if result == "won"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(sorted(result for result, _ in outcomes), ["vocabulary_conflict"] * 7 + ["won"])
        self.assertEqual(events.current_vocabulary(), winners[0])


class StateIsolationTests(CleanAuditVocabularyMixin, SimpleTestCase):
    """Two tests that each install something. Whatever order the runner uses, the second must not see the first's vocabulary."""

    def test_isolation_one(self):
        self.assertIsNone(events._installed)
        events.install_vocabulary(demo_vocabulary())

    def test_isolation_two(self):
        self.assertIsNone(events._installed)
        events.install_vocabulary(vocabulary_of(audit_module("other_mod", (("other_mod.did_it", ()),))))

    def test_the_context_manager_restores_even_after_an_error(self):
        with self.assertRaises(RuntimeError):
            with installed(demo_vocabulary()):
                raise RuntimeError("boom")
        self.assertIsNone(events._installed)


class CompositionTests(CleanAuditVocabularyMixin, SimpleTestCase):
    def test_every_registered_modules_actions_and_keys_are_composed(self):
        vocabulary = vocabulary_of(
            audit_module("alpha_mod", (("alpha_mod.one", ("a", "b")), ("alpha_mod.two", ()))),
            audit_module("beta_mod", (("beta_mod.one", ("c",)),)),
            intake_like_manifest(),
        )
        self.assertEqual(
            {a: sorted(vocabulary.detail_keys[a]) for a in vocabulary.module_actions},
            {"alpha_mod.one": ["a", "b"], "alpha_mod.two": [], "beta_mod.one": ["c"]},
        )
        self.assertEqual(dict(vocabulary.module_actions), {"alpha_mod.one": "alpha_mod", "alpha_mod.two": "alpha_mod", "beta_mod.one": "beta_mod"})

    def test_the_composition_hands_the_registrys_owners_and_the_reserved_namespaces_to_the_vocabulary(self):
        registry = registry_of(audit_module())
        with mock.patch.object(AuditVocabulary, "with_modules", autospec=True, side_effect=AuditVocabulary.with_modules) as spy:
            composition.build_audit_vocabulary(registry)
        (_, declarations, owners, reserved), _kwargs = spy.call_args
        self.assertEqual([(d.module_id, d.action, d.detail_keys) for d in declarations], [("audit_demo", DEMO_ACTION, ("count", "reason_code"))])
        self.assertIs(owners, registry.namespace_owners)
        self.assertEqual(reserved, RESERVED_NAMESPACES)

    def test_an_extra_namespace_the_module_owns_is_accepted(self):
        vocabulary = vocabulary_of(audit_module("legacy_mod", (("older.did_it", ("a",)),), extra_namespaces=("older",)))
        self.assertEqual(vocabulary.owner_of("older.did_it"), "legacy_mod")

    def test_another_modules_namespace_is_refused_by_the_manifest_before_composition(self):
        with self.assertRaises(ManifestError):
            audit_module("some_mod", (("other_mod.did_it", ()),))

    def test_a_dependency_grants_no_namespace(self):
        from tests.module_support import dep

        provider = audit_module("provider_mod", (("provider_mod.did_it", ()),))
        with self.assertRaises(ManifestError):
            audit_module("user_mod", (("provider_mod.did_it", ()),), dependencies=(dep("provider_mod"),))
        self.assertIn("provider_mod.did_it", vocabulary_of(provider).module_actions)

    def test_the_longest_manifest_action_is_accepted_and_one_more_character_is_not(self):
        action = "a_module." + "b" * 23  # 32 characters
        self.assertEqual(len(action), 32)
        self.assertIn(action, vocabulary_of(audit_module("a_module", ((action, ()),))).actions)
        with self.assertRaises(ManifestError):
            audit_module("a_module", ((action + "b", ()),))

    def test_importing_the_composition_root_needs_no_django_and_reads_no_database(self):
        script = (
            "import sys; sys.path.insert(0, sys.argv[1]);"
            "import config.audit_vocabulary as c;"
            "assert 'django' not in sys.modules, 'django was imported';"
            "assert c.build_audit_vocabulary().actions;"
            "assert 'django' not in sys.modules, 'django was imported by building';"
            "print('ok')"
        )
        env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
        done = subprocess.run([sys.executable, "-c", script, str(BACKEND)], capture_output=True, text=True, env=env, cwd=BACKEND, timeout=60)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "ok"), done.stderr)


class ShapeAgreementTests(SimpleTestCase):
    """The Python shape, the manifest format and the database CHECK (a literal in the migration) describe one set of strings."""

    @staticmethod
    def manifest_accepts(text) -> bool:
        try:
            AuditActionDeclaration(text)
        except ManifestError:
            return False
        return True

    def test_every_core_action_and_the_examples_have_the_shape(self):
        for action in (*CORE_ACTIONS, "a.b", "a_1.b_2", "mod_x.did_it", "a.b.c.d", "a" * 15 + "." + "b" * 16):
            with self.subTest(action=action):
                self.assertTrue(action_shape_ok(action))

    def test_the_boundaries(self):
        self.assertTrue(action_shape_ok(".".join("a" * 16)))  # 16 one-letter segments: 31 characters
        self.assertFalse(action_shape_ok(".".join("a" * 17)))  # 33 characters
        self.assertTrue(action_shape_ok("a." + "b" * 30))  # exactly 32
        self.assertFalse(action_shape_ok("a." + "b" * 31))  # 33
        for text in ("a.b\n", "a.b\n\n", "a.b ", " a.b", "a.b\x00", "a.b "):
            self.assertFalse(action_shape_ok(text), repr(text))

    def test_the_shape_and_the_manifest_agree_on_a_large_generated_sample(self):
        rng = random.Random(20261117)
        alphabet = string.ascii_lowercase[:6] + "AZ09_.-. "
        checked = 0
        for _ in range(20000):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 36)))
            if len(text) <= 32:
                self.assertEqual(action_shape_ok(text), self.manifest_accepts(text), repr(text))
                checked += 1
            else:
                self.assertFalse(action_shape_ok(text), repr(text))
        self.assertGreater(checked, 10000)

    def test_a_structured_sample_of_valid_and_nearly_valid_actions(self):
        rng = random.Random(7)
        segment = lambda: rng.choice(string.ascii_lowercase) + "".join(rng.choice(string.ascii_lowercase + string.digits + "_") for _ in range(rng.randint(0, 12)))  # noqa: E731
        for _ in range(3000):
            text = ".".join(segment() for _ in range(rng.randint(1, 5)))
            if len(text) <= 32:
                self.assertEqual(action_shape_ok(text), "." in text, text)
                self.assertEqual(action_shape_ok(text), self.manifest_accepts(text), text)

    def test_the_pattern_is_anchored_and_bounded_and_uses_only_portable_syntax(self):
        self.assertTrue(ACTION_SHAPE_PATTERN.startswith("^") and ACTION_SHAPE_PATTERN.endswith("$"))
        self.assertNotIn("*", ACTION_SHAPE_PATTERN)
        self.assertNotIn("+", ACTION_SHAPE_PATTERN)
        for construct in ("(?", "\\d", "\\w", "\\s", "\\b", "\\Z", "\\A", "[[:", "|"):
            self.assertNotIn(construct, ACTION_SHAPE_PATTERN, construct)

    def test_the_migration_literal_equals_the_model_constraint_and_the_pattern(self):
        import importlib

        from django.db import migrations as django_migrations

        from apps.audit.models import AuditEvent

        migration = importlib.import_module("apps.audit.migrations.0002_extensible_audit_vocabulary").Migration
        added = {op.constraint.name: op.constraint for op in migration.operations if isinstance(op, django_migrations.AddConstraint)}
        self.assertEqual(set(added), {"audit_event_action_shape", "audit_event_subject_type_valid"})
        self.assertEqual(added["audit_event_action_shape"].condition.children[0], ("action__regex", ACTION_SHAPE_PATTERN))
        model = {c.name: c for c in AuditEvent._meta.constraints}
        self.assertEqual(added["audit_event_action_shape"], model["audit_event_action_shape"])
        # migration 0003 replaced the subject-type list of 0002 (it added "ai_settings"); the current literal is 0003's and equals the model's
        later = importlib.import_module("apps.audit.migrations.0003_ai_settings_subject_type").Migration
        replaced = {op.constraint.name: op.constraint for op in later.operations if isinstance(op, django_migrations.AddConstraint)}
        self.assertEqual(set(replaced), {"audit_event_subject_type_valid"})
        self.assertEqual(replaced["audit_event_subject_type_valid"], model["audit_event_subject_type_valid"])


class DetailKeyRulesAgreeTests(SimpleTestCase):
    """A manifest and the audit vocabulary apply ONE detail-key rule (the packages cannot import each other, so it is written twice and pinned)."""

    @staticmethod
    def manifest_accepts(keys) -> bool:
        try:
            AuditActionDeclaration("some_mod.did_it", keys)
        except ManifestError:
            return False
        return True

    @staticmethod
    def vocabulary_accepts(keys) -> bool:
        try:
            ModuleAuditAction("some_mod", "some_mod.did_it", keys)
        except AuditVocabularyError:
            return False
        return True

    def test_the_constants_are_identical(self):
        from apps.audit import vocabulary as vocabulary_module
        from apps.modules import manifest as manifest_module

        self.assertEqual(manifest_module.FORBIDDEN_AUDIT_DETAIL_KEYS, FORBIDDEN_DETAIL_KEYS)
        self.assertEqual(manifest_module.MAX_AUDIT_DETAIL_KEYS, 20)
        self.assertEqual(vocabulary_module.MAX_DETAIL_KEYS, manifest_module.MAX_AUDIT_DETAIL_KEYS)

    def test_both_refuse_the_same_keys(self):
        free_content = sorted(FORBIDDEN_DETAIL_KEYS)  # reserved sensitive-content names
        malformed = ["Upper", "camelCase", "has space", "dash-ed", "dot.ted", "1leading", "_leading", "caf" + chr(233), "tab" + chr(9), "new" + chr(10) + "line", "", " ", "a" * 41, "x" + chr(10)]
        for key in free_content + malformed:
            with self.subTest(key=key):
                self.assertFalse(self.manifest_accepts((key,)), key)
                self.assertFalse(self.vocabulary_accepts((key,)), key)

    def test_both_accept_the_same_keys(self):
        for key in ("count", "reason_code", "a", "a" * 40, "k1", "x_y_z", "namespace", "names", "textual", "error_code", "notes_count"):
            with self.subTest(key=key):
                self.assertTrue(self.manifest_accepts((key,)), key)
                self.assertTrue(self.vocabulary_accepts((key,)), key)

    def test_both_apply_the_same_count_and_duplicate_rules(self):
        twenty = tuple(f"key{i}" for i in range(20))
        self.assertTrue(self.manifest_accepts(twenty) and self.vocabulary_accepts(twenty))
        self.assertFalse(self.manifest_accepts(twenty + ("one_more",)) or self.vocabulary_accepts(twenty + ("one_more",)))
        self.assertFalse(self.manifest_accepts(("a", "a")) or self.vocabulary_accepts(("a", "a")))
        for bad in ((5,), (None,), (b"x",), ("ok", 5)):
            self.assertFalse(self.manifest_accepts(bad) or self.vocabulary_accepts(bad), bad)

    def test_whatever_a_manifest_accepts_can_be_composed_on_a_generated_sample(self):
        rng = random.Random(1117)
        alphabet = string.ascii_lowercase[:4] + "AZ09_-. " + "".join(k[0] for k in sorted(FORBIDDEN_DETAIL_KEYS))
        pool = [*FORBIDDEN_DETAIL_KEYS, *("".join(rng.choice(alphabet) for _ in range(rng.randint(0, 45))) for _ in range(4000))]
        accepted = 0
        for key in pool:
            by_manifest, by_vocabulary = self.manifest_accepts((key,)), self.vocabulary_accepts((key,))
            self.assertEqual(by_manifest, by_vocabulary, repr(key))
            accepted += by_manifest
        self.assertGreater(accepted, 100)
        self.assertLess(accepted, len(pool))

    def test_a_registered_manifest_always_composes(self):
        registry = registry_of(audit_module("some_mod", (("some_mod.did_it", ("count", "names", "reason_code")),)))
        self.assertEqual(composition.build_audit_vocabulary(registry).detail_keys_of("some_mod.did_it"), frozenset({"count", "names", "reason_code"}))


class RequestNamespaceIsCoreTests(CleanAuditVocabularyMixin, SimpleTestCase):
    """Owner decision: ``request`` is a Core namespace (``request.submitted`` and ``request.edited`` are Core audit actions)."""

    def test_it_is_reserved(self):
        self.assertIn("request", RESERVED_NAMESPACES)

    def test_a_module_cannot_own_it_as_its_id_or_as_an_extra_namespace(self):
        with self.assertRaises(ManifestError) as caught:
            manifest(module_id="request")
        self.assertEqual(caught.exception.code, "reserved_namespace")
        with self.assertRaises(ManifestError) as caught:
            manifest(extra_namespaces=("request",))
        self.assertEqual(caught.exception.code, "reserved_namespace")

    def test_a_module_cannot_declare_a_request_action(self):
        for action in ("request.deleted", "request.submitted"):
            with self.subTest(action=action), self.assertRaises(ManifestError) as caught:
                manifest(audit_actions=(AuditActionDeclaration(action),))
            self.assertEqual(caught.exception.code, "identifier_outside_namespace")

    def test_namespaces_that_merely_start_with_request_are_ordinary(self):
        vocabulary = vocabulary_of(audit_module("request_intake", (("request_intake.did_it", ("count",)),)), audit_module("requests_mod", (("requests_mod.did_it", ()),)))
        self.assertEqual(set(vocabulary.module_actions), {"request_intake.did_it", "requests_mod.did_it"})

    def test_the_core_request_actions_remain_valid_everywhere(self):
        self.assertIn("request.submitted", events.CORE_VOCABULARY.actions)
        self.assertIn("request.edited", events.CORE_VOCABULARY.actions)
        user_event(ACTOR, "request.submitted", "run", SUBJECT, byte_length=10, client_request_id_used=False)
        user_event(ACTOR, "request.edited", "revision", SUBJECT, base_revision_id=None, first_edit=True)
        self.assertTrue(action_shape_ok("request.submitted") and action_shape_ok("request.edited"))
        with installed(demo_vocabulary()):
            user_event(ACTOR, "request.submitted", "run", SUBJECT, byte_length=10)

    def test_no_module_that_exists_or_is_modelled_breaks(self):
        from tests.module_support import full_manifest

        self.assertEqual(module_root.build_registry().module_ids, ("request_intake",))
        registry = registry_of(intake_like_manifest(), full_manifest(), audit_module(), manifest(module_id="plain_one"))
        self.assertEqual(set(registry.module_ids), {"request_intake", "full_module", "audit_demo", "plain_one"})
        self.assertEqual(registry.namespace_owners["intake"], "request_intake")  # the synthetic Request Intake keeps its own namespaces
        self.assertNotIn("request", registry.namespace_owners)
        composition.build_audit_vocabulary(registry)


class RecorderUnchangedTests(CleanAuditVocabularyMixin, SimpleTestCase):
    def test_a_module_event_outside_a_transaction_is_refused_before_any_query(self):
        with installed(demo_vocabulary()):
            event = demo_event()
            with mock.patch.object(recorder.AuditEvent.objects, "create") as create:
                with self.assertRaises(recorder.AuditTransactionError):
                    recorder.record_event(event)
                create.assert_not_called()

    def test_a_module_event_inside_a_transaction_is_written_with_the_same_fields_as_any_event(self):
        with installed(demo_vocabulary()):
            event = demo_event(subject="module")
            with mock.patch.object(recorder.connection, "in_atomic_block", True), mock.patch.object(recorder.AuditEvent.objects, "create") as create:
                recorder.record_event(event)
        create.assert_called_once_with(
            actor_kind="user", actor_public_id=ACTOR, action=DEMO_ACTION, subject_type="module", subject_id=SUBJECT, case_id=CASE, run_id=RUN, details={"count": 3}
        )

    def test_an_undeclared_action_never_reaches_the_recorder(self):
        with mock.patch.object(recorder.connection, "in_atomic_block", True), mock.patch.object(recorder.AuditEvent.objects, "create") as create:
            with self.assertRaises(AuditEventError):
                recorder.record_event(demo_event())
            create.assert_not_called()

    def test_there_is_one_recorder_and_one_place_that_creates_rows(self):
        defined, creating = [], []
        for top in ("apps", "config", "workflows"):
            for path in sorted((BACKEND / top).rglob("*.py")):
                if "migrations" in path.parts:
                    continue
                source = path.read_text(encoding="utf-8")
                if "def record_event" in source:
                    defined.append(path.relative_to(BACKEND).as_posix())
                if re.search(r"AuditEvent\.objects\.(create|bulk_create|get_or_create|update_or_create)|(?<!class )(?<![A-Za-z])AuditEvent\(", source):
                    creating.append(path.relative_to(BACKEND).as_posix())
        self.assertEqual(defined, ["apps/audit/recorder.py"])
        self.assertEqual(creating, ["apps/audit/recorder.py"])  # (the model class statement is not a row)


def _imports(path: Path) -> set:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(("." * node.level) + (node.module or ""))
    return found


def _code_only(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body and isinstance(getattr(node.body[0], "value", None), ast.Constant):
            node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


class ArchitectureTests(SimpleTestCase):
    def test_the_vocabulary_is_standard_library_only(self):
        self.assertEqual({name for name in _imports(BACKEND / "apps/audit/vocabulary.py") if name.split(".")[0] not in STDLIB}, set())

    def test_events_imports_only_the_standard_library_and_the_vocabulary(self):
        outside = {name for name in _imports(BACKEND / "apps/audit/events.py") if name.split(".")[0] not in STDLIB}
        self.assertEqual(outside, {".vocabulary"})

    def test_no_audit_package_imports_the_module_package_the_api_or_business_code(self):
        forbidden = ("apps.modules", "config", "apps.intake", "apps.api", "apps.tasks", "workflows", "apps.accounts")
        for path in sorted((BACKEND / "apps" / "audit").rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            for name in _imports(path):
                with self.subTest(file=path.name, imported=name):
                    self.assertFalse(any(name == f or name.startswith(f + ".") for f in forbidden), f"{path.name} imports {name}")

    def test_the_audit_app_config_has_no_ready_hook_and_installs_nothing(self):
        tree = ast.parse((BACKEND / "apps/audit/apps.py").read_text(encoding="utf-8"))
        self.assertEqual([n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)], [])  # no ready(), no other method

    def test_only_the_composition_root_installs_a_vocabulary_and_nothing_calls_it_yet(self):
        callers = []
        for top in ("apps", "config", "workflows"):
            for path in sorted((BACKEND / top).rglob("*.py")):
                if "__pycache__" in path.parts or "migrations" in path.parts:
                    continue
                code = _code_only(path)
                if "install_audit_vocabulary" in code or "install_vocabulary(" in code:
                    callers.append(path.relative_to(BACKEND).as_posix())
        self.assertEqual(sorted(callers), ["apps/audit/events.py", "config/audit_vocabulary.py"])

    def test_the_composition_root_imports_only_what_it_is_allowed_to(self):
        allowed = {"apps.audit.events", "apps.audit.vocabulary", "apps.modules.manifest", "apps.modules.registry", "config.modules"}
        for name in _imports(BACKEND / "config/audit_vocabulary.py"):
            with self.subTest(imported=name):
                self.assertTrue(name.split(".")[0] in STDLIB or name in allowed or name in {"apps.audit.events.CORE_VOCABULARY"}, name)
        self.assertEqual(len(module_root.BUILTIN_MODULES), 1)  # MOD-2: Request Intake

    def test_no_dynamic_import_callable_path_or_runtime_discovery(self):
        banned_imports = {"importlib", "pkgutil", "os", "glob", "pathlib", "subprocess", "socket", "pickle", "ctypes", "django", "sys"}
        banned_names = {"__import__", "eval", "exec", "open", "globals", "locals", "import_module", "import_string", "entry_points", "environ", "getenv", "settings", "listdir", "walk_packages"}
        for path in (BACKEND / "apps/audit/vocabulary.py", BACKEND / "apps/audit/events.py", BACKEND / "config/audit_vocabulary.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            names |= {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
            with self.subTest(file=path.name):
                self.assertEqual(names & banned_names, set())
                self.assertEqual({name.split(".")[0] for name in _imports(path)} & banned_imports, set())
                self.assertNotIn("compile", {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)})  # re.compile is fine

    def test_the_worker_composition_and_the_module_composition_root_are_unchanged(self):
        from config.task_registry import COMPOSITION

        self.assertIsNotNone(COMPOSITION.kind_gate)  # S4 installs the module gate; it has nothing to do with the audit vocabulary
        self.assertEqual(COMPOSITION.maintenance_hooks, ())
        source = (BACKEND / "config" / "task_registry.py").read_text(encoding="utf-8")
        self.assertNotIn("audit", source)
        self.assertEqual(len(module_root.BUILTIN_MODULES), 1)  # MOD-2: Request Intake

    def test_the_module_package_is_still_free_of_audit_and_django_imports(self):
        for path in sorted((BACKEND / "apps" / "modules").rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            for name in _imports(path):
                self.assertFalse(name.split(".")[0] in {"django", "config"} or name.startswith("apps.audit"), f"{path.name} imports {name}")

    def test_the_migration_never_deletes_rewrites_or_runs_raw_sql(self):
        code = _code_only(BACKEND / "apps/audit/migrations/0002_extensible_audit_vocabulary.py")
        for word in ("RunSQL", "DeleteModel", "RemoveField", "AlterField", ".delete(", ".update(", ".bulk_update(", "DELETE", "UPDATE", "TRUNCATE", "cursor", "execute"):
            self.assertNotIn(word, code, word)

    def test_the_audit_migrations_form_one_chain_and_0003_is_the_leaf(self):
        from django.db.migrations.loader import MigrationLoader

        loader = MigrationLoader(None, ignore_no_migrations=True)
        self.assertEqual(loader.get_migration("audit", "0002_extensible_audit_vocabulary").dependencies, [("audit", "0001_initial")])
        self.assertEqual(loader.get_migration("audit", "0003_ai_settings_subject_type").dependencies, [("audit", "0002_extensible_audit_vocabulary")])
        self.assertEqual([node for node in loader.graph.leaf_nodes() if node[0] == "audit"], [("audit", "0003_ai_settings_subject_type")])

    def test_model_state_and_migrations_agree_without_a_database(self):
        from django.apps import apps as django_apps
        from django.db.migrations.autodetector import MigrationAutodetector
        from django.db.migrations.loader import MigrationLoader
        from django.db.migrations.questioner import NonInteractiveMigrationQuestioner
        from django.db.migrations.state import ProjectState

        loader = MigrationLoader(None, ignore_no_migrations=True)
        detector = MigrationAutodetector(loader.project_state(), ProjectState.from_apps(django_apps), NonInteractiveMigrationQuestioner(specified_apps={"audit"}))
        self.assertEqual(detector.changes(graph=loader.graph, trim_to_apps={"audit"}), {})


class OldSchemaHelperTests(SimpleTestCase):
    """The PostgreSQL migration tests build rows for the OLD schema (audit 0001). They must use the frozen historical actions, never the live vocabulary.

    MOD-1b S4 added ``module.enabled`` and ``module.disabled`` to ``events.ACTIONS``. The old schema's fixed list refuses them (correctly), so a helper that inserted
    ``events.ACTIONS`` under audit 0001 failed with ``audit_event_action_valid`` in three tests (it was not a defect of the migration or of any production code).
    """

    def migration(self, name):
        import importlib

        return importlib.import_module(f"apps.audit.migrations.{name}")

    def old_fixed_list_of_migration_0001(self):
        constraints = self.migration("0001_initial").Migration.operations[0].options["constraints"]
        (valid,) = [c for c in constraints if c.name == "audit_event_action_valid"]
        (key, values), = valid.condition.children
        self.assertEqual(key, "action__in")
        return tuple(values)

    def postgres_test_constant(self, name):
        tree = ast.parse((BACKEND / "tests" / "test_audit_vocabulary_postgres.py").read_text(encoding="utf-8"))
        (assigned,) = [n for n in tree.body if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == name for t in n.targets)]
        return ast.literal_eval(assigned.value)

    def test_the_frozen_old_actions_are_exactly_the_old_fixed_list_and_the_migration_guard(self):
        frozen = self.postgres_test_constant("OLD_ACTIONS")
        self.assertEqual(frozen, self.old_fixed_list_of_migration_0001())
        self.assertEqual(frozen, tuple(self.migration("0002_extensible_audit_vocabulary").OLD_ACTIONS))
        self.assertEqual(frozen, events.ACTIONS[: len(frozen)])  # the live vocabulary still starts with them, unchanged

    def test_the_live_vocabulary_has_actions_the_old_schema_refuses_which_is_why_the_helper_failed(self):
        refused_by_the_old_schema = set(events.ACTIONS) - set(self.old_fixed_list_of_migration_0001())
        self.assertEqual(
            refused_by_the_old_schema,
            {events.ACTION_MODULE_ENABLED, events.ACTION_MODULE_DISABLED, events.ACTION_AI_SETTINGS_UPDATED, events.ACTION_AI_SETTINGS_CONNECTION_CHANGED},
        )

    def test_the_old_schema_helper_does_not_read_the_live_vocabulary(self):
        tree = ast.parse((BACKEND / "tests" / "test_audit_vocabulary_postgres.py").read_text(encoding="utf-8"))
        (cls,) = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AuditMigrationTests"]
        (helper,) = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "insert_old_vocabulary_rows"]
        used = {f"{n.value.id}.{n.attr}" for n in ast.walk(helper) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
        self.assertNotIn("events.ACTIONS", used)
        self.assertIn("OLD_ACTIONS", {n.id for n in ast.walk(helper) if isinstance(n, ast.Name)})

    def test_no_test_inserts_the_live_vocabulary_into_the_old_schema(self):
        # every use of events.ACTIONS inside AuditMigrationTests must come AFTER the test has migrated to audit 0002 (the new schema)
        source = (BACKEND / "tests" / "test_audit_vocabulary_postgres.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        (cls,) = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AuditMigrationTests"]
        for function in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
            uses = [n.lineno for n in ast.walk(function) if isinstance(n, ast.Attribute) and ast.unparse(n) == "events.ACTIONS"]
            if not uses:
                continue
            forward = [n.lineno for n in ast.walk(function) if isinstance(n, ast.Call) and ast.unparse(n) == "self.migrate(AUDIT_0002)"]
            with self.subTest(test=function.name):
                self.assertTrue(forward and min(uses) > min(forward), "events.ACTIONS is used before the test has migrated to audit 0002")
