"""DB-free tests of the secret store abstraction: the interface contract, references, the disabled backend and the absence of plaintext exposure."""

import ast
import copy
import io
import logging
import pickle
import traceback
import unittest
from pathlib import Path

from apps.secret_store import (
    CODE_ALREADY_EXISTS,
    CODE_BUSY,
    CODE_DISABLED,
    CODE_INVALID_REFERENCE,
    CODE_INVALID_SECRET,
    CODE_NOT_FOUND,
    CODE_UNAVAILABLE,
    CODE_UNREADABLE,
    CODES,
    MAX_SECRET_LENGTH,
    DisabledSecretStore,
    SecretStore,
    SecretStoreError,
    SecretValue,
    validate_reference,
)
from tests.secret_store_support import (
    ALL_SENTINELS,
    REF_A,
    REF_B,
    SECRET_A,
    SECRET_B,
    InMemorySecretStore,
    RaisingSecretStore,
    WrongTypeSecretStore,
)

PACKAGE = Path(__file__).resolve().parent.parent / "apps" / "secret_store"
BAD_REFERENCES = ("", " ", "1abc", "_abc", "has space", "a/b", "a\\b", "a" * 97, "ünï", "abc\n", "abc\x00", None, 5, b"abc", ["abc"])


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage() + " " + repr(record.args))


def code_of(test, callable_, *args, **kwargs):
    with test.assertRaises(SecretStoreError) as caught:
        callable_(*args, **kwargs)
    return caught.exception.code


class ReferenceTests(unittest.TestCase):
    def test_valid_opaque_references_are_returned_unchanged(self):
        for reference in (REF_A, REF_B, "a", "A1", "k_1.2:3-4", "a" * 96):
            self.assertEqual(validate_reference(reference), reference)

    def test_invalid_references_are_refused_on_every_operation(self):
        store = InMemorySecretStore()
        for bad in BAD_REFERENCES:
            with self.subTest(reference=bad):
                self.assertEqual(code_of(self, validate_reference, bad), CODE_INVALID_REFERENCE)
                self.assertEqual(code_of(self, store.put, bad, SECRET_A), CODE_INVALID_REFERENCE)
                self.assertEqual(code_of(self, store.put, bad, SECRET_A, replace=True), CODE_INVALID_REFERENCE)
                self.assertEqual(code_of(self, store.get, bad), CODE_INVALID_REFERENCE)
                self.assertEqual(code_of(self, store.delete, bad), CODE_INVALID_REFERENCE)
                self.assertEqual(code_of(self, store.exists, bad), CODE_INVALID_REFERENCE)

    def test_a_reference_is_never_normalized(self):
        store = InMemorySecretStore()
        store.put("Abc", SECRET_A)
        self.assertTrue(store.exists("Abc"))
        self.assertFalse(store.exists("abc"))
        self.assertEqual(code_of(self, store.exists, " Abc"), CODE_INVALID_REFERENCE)

    def test_an_invalid_reference_is_refused_before_a_backend_hook_runs_even_when_disabled(self):
        self.assertEqual(code_of(self, DisabledSecretStore().get, "bad ref"), CODE_INVALID_REFERENCE)

    def test_the_error_does_not_echo_the_reference(self):
        text = "".join(traceback.format_exception(SecretStoreError(CODE_INVALID_REFERENCE)))
        for bad in ("bad ref", "password=hunter2"):
            try:
                InMemorySecretStore().get(bad)
            except SecretStoreError as error:
                text += f"{error} {error!r} {error.args}"
        self.assertNotIn("hunter2", text)
        self.assertNotIn("bad ref", text)


class InterfaceContractTests(unittest.TestCase):
    """What every backend must do. A future real backend subclasses this with its own ``make_store``."""

    def make_store(self) -> SecretStore:
        return InMemorySecretStore()

    def test_the_interface_is_exactly_put_get_delete_exists(self):
        public = {name for name in vars(SecretStore) if not name.startswith("_")}
        self.assertEqual(public, {"put", "get", "delete", "exists", "backend_id"})  # backend_id: a fixed token naming the backend, recorded by audit events

    def test_put_then_get_round_trips_as_a_secret_value(self):
        store = self.make_store()
        self.assertIsNone(store.put(REF_A, SECRET_A))
        value = store.get(REF_A)
        self.assertIsInstance(value, SecretValue)
        self.assertEqual(value.reveal(), SECRET_A)
        self.assertEqual(value, SecretValue(SECRET_A))

    def test_a_secret_value_is_accepted_by_put(self):
        store = self.make_store()
        store.put(REF_A, SecretValue(SECRET_A))
        self.assertEqual(store.get(REF_A).reveal(), SECRET_A)

    def test_a_missing_secret_is_an_error_never_a_default(self):
        store = self.make_store()
        self.assertEqual(code_of(self, store.get, REF_A), CODE_NOT_FOUND)
        self.assertEqual(code_of(self, store.delete, REF_A), CODE_NOT_FOUND)
        self.assertFalse(store.exists(REF_A))

    def test_exists_reflects_put_and_delete(self):
        store = self.make_store()
        store.put(REF_A, SECRET_A)
        self.assertTrue(store.exists(REF_A))
        self.assertFalse(store.exists(REF_B))
        store.delete(REF_A)
        self.assertFalse(store.exists(REF_A))

    def test_create_refuses_to_overwrite_and_keeps_the_original(self):
        store = self.make_store()
        store.put(REF_A, SECRET_A)
        self.assertEqual(code_of(self, store.put, REF_A, SECRET_B), CODE_ALREADY_EXISTS)
        self.assertEqual(code_of(self, store.put, REF_A, SECRET_B, replace=False), CODE_ALREADY_EXISTS)
        self.assertEqual(store.get(REF_A).reveal(), SECRET_A)

    def test_replace_rotates_an_existing_secret(self):
        store = self.make_store()
        store.put(REF_A, SECRET_A)
        self.assertIsNone(store.put(REF_A, SECRET_B, replace=True))
        self.assertEqual(store.get(REF_A).reveal(), SECRET_B)

    def test_replace_of_a_missing_secret_is_refused_and_creates_nothing(self):
        store = self.make_store()
        self.assertEqual(code_of(self, store.put, REF_A, SECRET_A, replace=True), CODE_NOT_FOUND)
        self.assertFalse(store.exists(REF_A))

    def test_delete_removes_only_that_reference_and_is_not_idempotent(self):
        store = self.make_store()
        store.put(REF_A, SECRET_A)
        store.put(REF_B, SECRET_B)
        store.delete(REF_A)
        self.assertEqual(code_of(self, store.get, REF_A), CODE_NOT_FOUND)
        self.assertEqual(store.get(REF_B).reveal(), SECRET_B)
        self.assertEqual(code_of(self, store.delete, REF_A), CODE_NOT_FOUND)

    def test_a_deleted_reference_can_be_created_again(self):
        store = self.make_store()
        store.put(REF_A, SECRET_A)
        store.delete(REF_A)
        store.put(REF_A, SECRET_B)
        self.assertEqual(store.get(REF_A).reveal(), SECRET_B)

    def test_references_are_independent(self):
        store = self.make_store()
        store.put(REF_A, SECRET_A)
        store.put(REF_B, SECRET_B)
        self.assertEqual((store.get(REF_A).reveal(), store.get(REF_B).reveal()), (SECRET_A, SECRET_B))

    def test_unacceptable_values_are_refused_and_store_nothing(self):
        store = self.make_store()
        for bad in ("", "   ", " padded ", "line\nbreak", "tab\there", "nul\x00", "x" * (MAX_SECRET_LENGTH + 1), None, 5, b"bytes", ["x"]):
            with self.subTest(value=repr(bad)[:20]):
                self.assertEqual(code_of(self, store.put, REF_A, bad), CODE_INVALID_SECRET)
                self.assertEqual(code_of(self, store.put, REF_A, bad, replace=True), CODE_INVALID_SECRET)
        self.assertFalse(store.exists(REF_A))
        store.put(REF_A, "x" * MAX_SECRET_LENGTH)

    def test_an_invalid_value_does_not_disturb_a_stored_secret(self):
        store = self.make_store()
        store.put(REF_A, SECRET_A)
        self.assertEqual(code_of(self, store.put, REF_A, "", replace=True), CODE_INVALID_SECRET)
        self.assertEqual(store.get(REF_A).reveal(), SECRET_A)


class DisabledBackendTests(unittest.TestCase):
    def test_every_operation_fails_closed(self):
        store = DisabledSecretStore()
        self.assertEqual(code_of(self, store.put, REF_A, SECRET_A), CODE_DISABLED)
        self.assertEqual(code_of(self, store.put, REF_A, SECRET_A, replace=True), CODE_DISABLED)
        self.assertEqual(code_of(self, store.get, REF_A), CODE_DISABLED)
        self.assertEqual(code_of(self, store.delete, REF_A), CODE_DISABLED)

    def test_exists_raises_instead_of_answering_false(self):
        self.assertEqual(code_of(self, DisabledSecretStore().exists, REF_A), CODE_DISABLED)

    def test_it_keeps_nothing_and_has_no_state(self):
        store = DisabledSecretStore()
        code_of(self, store.put, REF_A, SECRET_A)
        self.assertEqual(vars(store), {})
        self.assertEqual(code_of(self, store.get, REF_A), CODE_DISABLED)

    def test_an_invalid_value_is_refused_as_such_and_a_valid_one_is_not_stored(self):
        self.assertEqual(code_of(self, DisabledSecretStore().put, REF_A, ""), CODE_INVALID_SECRET)

    def test_it_does_not_consult_the_environment_or_any_other_source(self):
        import os

        saved = os.environ.get(REF_A)
        os.environ[REF_A] = SECRET_A
        try:
            self.assertEqual(code_of(self, DisabledSecretStore().get, REF_A), CODE_DISABLED)
        finally:
            if saved is None:
                del os.environ[REF_A]
            else:
                os.environ[REF_A] = saved

    def test_it_is_a_secret_store(self):
        self.assertIsInstance(DisabledSecretStore(), SecretStore)
        self.assertEqual(repr(DisabledSecretStore()), "<DisabledSecretStore>")


class BackendFailureTests(unittest.TestCase):
    def test_a_foreign_exception_carrying_a_secret_is_replaced_and_its_cause_dropped(self):
        store = RaisingSecretStore(RuntimeError(f"backend said {SECRET_A}"))
        operations = (
            lambda: store.put(REF_A, SECRET_B),
            lambda: store.put(REF_A, SECRET_B, replace=True),
            lambda: store.get(REF_A),
            lambda: store.delete(REF_A),
            lambda: store.exists(REF_A),
        )
        for operation in operations:
            with self.assertRaises(SecretStoreError) as caught:
                operation()
            error = caught.exception
            self.assertEqual(error.code, CODE_UNAVAILABLE)
            self.assertIsNone(error.__cause__)
            self.assertTrue(error.__suppress_context__)
            text = "".join(traceback.format_exception(error)) + f"{error} {error!r}"
            for sentinel in (SECRET_A, SECRET_B):
                self.assertNotIn(sentinel, text)

    def test_a_backend_that_returns_the_wrong_type_fails_closed(self):
        store = WrongTypeSecretStore()
        self.assertEqual(code_of(self, store.get, REF_A), CODE_UNAVAILABLE)
        self.assertEqual(code_of(self, store.exists, REF_A), CODE_UNAVAILABLE)

    def test_a_store_error_from_a_backend_passes_through_unchanged(self):
        store = RaisingSecretStore(SecretStoreError(CODE_NOT_FOUND))
        self.assertEqual(code_of(self, store.get, REF_A), CODE_NOT_FOUND)

    def test_an_unknown_code_becomes_unavailable_and_every_code_has_a_fixed_message(self):
        self.assertEqual(SecretStoreError("anything").code, CODE_UNAVAILABLE)
        self.assertEqual(set(CODES), {CODE_INVALID_REFERENCE, CODE_INVALID_SECRET, CODE_NOT_FOUND, CODE_ALREADY_EXISTS, CODE_DISABLED, CODE_UNAVAILABLE, CODE_UNREADABLE, CODE_BUSY})
        for code in CODES:
            self.assertEqual(str(SecretStoreError(code)), str(SecretStoreError(code)))
            self.assertEqual(pickle.loads(pickle.dumps(SecretStoreError(code))).code, code)

    def test_the_abstract_class_cannot_be_instantiated(self):
        with self.assertRaises(TypeError):
            SecretStore()


class NoLeakageTests(unittest.TestCase):
    def setUp(self):
        self.capture = Capture()
        root = logging.getLogger()
        level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self.capture)
        self.addCleanup(root.removeHandler, self.capture)
        self.addCleanup(root.setLevel, level)

    def assertClean(self, *texts):
        blob = "\n".join(texts) + "\n" + "\n".join(self.capture.lines)
        for sentinel in ALL_SENTINELS:
            self.assertNotIn(sentinel, blob)

    def test_a_secret_value_is_redacted_everywhere_it_can_be_printed(self):
        value = SecretValue(SECRET_A)
        rendered = (repr(value), str(value), f"{value}", f"{value!r}", format(value, ">30"), "%s %r" % (value, value), repr([value]), repr({"k": value}), str((value,)))
        self.assertClean(*rendered)
        self.assertIn("redacted", repr(value))

    def test_a_secret_value_is_in_no_traceback_or_exception_text(self):
        value = SecretValue(SECRET_A)
        try:
            raise ValueError(value)
        except ValueError as error:
            self.assertClean("".join(traceback.format_exception(error)), str(error), repr(error))

    def test_a_secret_value_is_in_no_log_line(self):
        value = SecretValue(SECRET_A)
        logger = logging.getLogger("tests.secret_store")
        logger.info("value %s", value)
        logger.info("value %r", value)
        logger.error("failed", exc_info=(ValueError, ValueError(value), None))
        self.assertClean()

    def test_a_secret_value_cannot_be_pickled_copied_hashed_or_mutated(self):
        value = SecretValue(SECRET_A)
        for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
            with self.assertRaises(TypeError):
                pickle.dumps(value, protocol)
        with self.assertRaises(TypeError):
            copy.copy(value)
        with self.assertRaises(TypeError):
            copy.deepcopy(value)
        with self.assertRaises(TypeError):
            hash(value)
        with self.assertRaises(AttributeError):
            value._text = "other"
        with self.assertRaises(AttributeError):
            value.other = 1
        with self.assertRaises(AttributeError):
            del value._text
        with self.assertRaises(TypeError):
            vars(value)

    def test_equality_is_only_between_secret_values(self):
        self.assertEqual(SecretValue(SECRET_A), SecretValue(SECRET_A))
        self.assertNotEqual(SecretValue(SECRET_A), SecretValue(SECRET_B))
        self.assertNotEqual(SecretValue(SECRET_A), SECRET_A)
        self.assertNotEqual(SECRET_A, SecretValue(SECRET_A))

    def test_a_store_repr_and_every_error_hold_no_secret(self):
        store = InMemorySecretStore()
        store.put(REF_A, SECRET_A)
        texts = [repr(store), str(store)]
        for operation in (
            lambda: store.put(REF_A, SECRET_B),
            lambda: store.put(REF_B, SECRET_B, replace=True),
            lambda: store.get(REF_B),
            lambda: store.delete(REF_B),
            lambda: store.put("bad ref", SECRET_B),
            lambda: store.put(REF_B, SECRET_B + "\n"),
            lambda: DisabledSecretStore().put(REF_A, SECRET_B),
        ):
            try:
                operation()
            except SecretStoreError as error:
                texts.extend(("".join(traceback.format_exception(error)), str(error), repr(error), repr(error.args), error.code))
        self.assertClean(*texts)

    def test_a_refused_value_is_not_in_the_error(self):
        try:
            SecretValue(SECRET_A + "\n")
        except SecretStoreError as error:
            self.assertClean("".join(traceback.format_exception(error)), repr(error))
        else:
            self.fail("expected a refusal")

    def test_exists_get_and_delete_write_nothing_to_streams_or_logs(self):
        store = InMemorySecretStore()
        store.put(REF_A, SECRET_A)
        import contextlib

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            store.exists(REF_A)
            store.get(REF_A)
            store.put(REF_A, SECRET_B, replace=True)
            store.delete(REF_A)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        self.assertClean()


class BoundaryTests(unittest.TestCase):
    def sources(self):
        return {path: path.read_text(encoding="utf-8") for path in PACKAGE.glob("*.py")}

    def imported_modules(self, source):
        names = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
        return names

    def test_the_package_is_pure_standard_library(self):
        allowed = {"abc", "hmac", "re"}
        for path, source in self.sources().items():
            with self.subTest(file=path.name):
                self.assertLessEqual(self.imported_modules(source), allowed)

    def test_the_package_reads_no_environment_logs_nothing_and_prints_nothing(self):
        forbidden = ("environ", "getenv", "logging", "print(", "open(", "socket", "subprocess", "ctypes", "winreg", "django", "psycopg", "hashlib", "base64", "cryptography")
        for path, source in self.sources().items():
            code = self._executable_text(source)
            for token in forbidden:
                with self.subTest(file=path.name, token=token):
                    self.assertNotIn(token, code)

    @staticmethod
    def _executable_text(code):
        """The source without docstrings, so a sentence about a rule is not mistaken for a use of it."""
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and node.body and isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant):
                node.body[0].value = ast.Constant(value="")
        return ast.unparse(tree)

    def test_the_package_is_not_an_installed_app_and_has_no_models_or_migrations(self):
        self.assertEqual({path.name for path in PACKAGE.iterdir() if path.is_file() or path.is_dir()} - {"__pycache__"}, {"__init__.py", "errors.py", "store.py", "values.py"})

    def test_only_the_composition_roots_the_dpapi_backend_and_the_credential_service_import_the_store(self):
        backend = PACKAGE.parent.parent
        importers = set()
        for root in (backend / "apps", backend / "config"):
            for path in root.rglob("*.py"):
                if PACKAGE in path.parents or "migrations" in path.parts:
                    continue
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
                    if any(name == "apps.secret_store" or name.startswith(("apps.secret_store.", "config.secret_store")) for name in names):
                        importers.add(path.relative_to(backend).as_posix())
        # AI-SEC-2: the backend itself, the composition that chooses a store, the connection composition that reads a managed key, the credential service and the route wiring.
        # No settings rule, API view, provider adapter, Request Intake code or worker command imports a store.
        self.assertEqual(
            importers,
            {"config/secret_store_dpapi.py", "config/secret_store.py", "config/ai_connections.py", "apps/ai_credentials/services.py", "config/urls.py"},
        )


if __name__ == "__main__":
    unittest.main()
