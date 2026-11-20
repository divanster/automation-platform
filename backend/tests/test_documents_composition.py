"""DB-free tests of the document storage composition root (``config/document_storage.py``), the operator's ``prepare_document_storage`` command and the Windows policy adapter (DOC-1).

The environment is passed explicitly (the test runner removes every ``AP_DOCUMENT_*`` variable); the real Windows policy is exercised end to end only on Windows, on a temporary directory.
"""

import io
import os
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from apps.documents.errors import CODE_DISABLED, CODE_UNAVAILABLE, DocumentStorageError
from apps.documents.rules import DEFAULT_MAX_BYTES, HARD_MAX_BYTES, MIN_MAX_BYTES
from apps.documents.storage import DirectoryPolicy, DocumentStore, PosixDirectoryPolicy
from config import document_storage as composition
from tests.documents_support import VALID_PDF, PermissivePolicy, Reader, temporary_root, tree_listing

BACKEND = Path(__file__).resolve().parent.parent
REPOSITORY = BACKEND.parent


def code_of(function, *args, **kwargs):
    try:
        function(*args, **kwargs)
    except DocumentStorageError as error:
        return error.code
    return None


class EnvironmentTests(SimpleTestCase):
    def test_unset_means_disabled_and_there_is_no_default_location(self):
        for env in ({}, {"AP_DOCUMENT_STORAGE_DIRECTORY": ""}, {"AP_DOCUMENT_STORAGE_DIRECTORY": "   "}):
            with self.subTest(env=env):
                self.assertEqual(code_of(composition.build_document_store, env, PermissivePolicy()), CODE_DISABLED)
                self.assertEqual(code_of(composition.prepare_document_storage, env, PermissivePolicy()), CODE_DISABLED)

    def test_the_limit_defaults_to_20_mib_and_is_bounded_not_clamped(self):
        self.assertEqual(composition.configured_max_bytes({}), DEFAULT_MAX_BYTES)
        self.assertEqual(composition.configured_max_bytes({"AP_DOCUMENT_MAX_BYTES": ""}), DEFAULT_MAX_BYTES)
        for value in (MIN_MAX_BYTES, 5_000_000, HARD_MAX_BYTES):
            self.assertEqual(composition.configured_max_bytes({"AP_DOCUMENT_MAX_BYTES": str(value)}), value)
        for value in ("0", "-1", str(MIN_MAX_BYTES - 1), str(HARD_MAX_BYTES + 1), "banana", "1e6", "20MB", "1.5"):
            with self.subTest(value=value):
                self.assertEqual(code_of(composition.configured_max_bytes, {"AP_DOCUMENT_MAX_BYTES": value}), CODE_UNAVAILABLE)

    def test_a_store_is_built_from_the_environment_with_the_configured_limit(self):
        with temporary_root() as root:
            composition.prepare_document_storage({"AP_DOCUMENT_STORAGE_DIRECTORY": root}, PermissivePolicy())
            store = composition.build_document_store({"AP_DOCUMENT_STORAGE_DIRECTORY": root, "AP_DOCUMENT_MAX_BYTES": "8192"}, PermissivePolicy())
            self.assertIsInstance(store, DocumentStore)
            self.assertEqual(store.max_bytes, 8192)
            self.assertEqual(store.ingest(Reader(VALID_PDF).read, len(VALID_PDF)).size_bytes, len(VALID_PDF))

    def test_a_missing_layout_fails_closed_and_is_never_created_by_a_request(self):
        with temporary_root() as root:
            os.mkdir(root)
            self.assertEqual(code_of(composition.build_document_store, {"AP_DOCUMENT_STORAGE_DIRECTORY": root}, PermissivePolicy()), CODE_UNAVAILABLE)
            self.assertEqual(os.listdir(root), [])  # only the operator's command creates anything

    def test_the_repository_and_any_static_or_media_root_are_forbidden_locations(self):
        roots = composition.forbidden_roots()
        self.assertIn(str(REPOSITORY), roots)
        for inside in (REPOSITORY / "documents", BACKEND / "media", REPOSITORY / "frontend" / "dist", REPOSITORY):
            with self.subTest(path=str(inside)):
                self.assertEqual(code_of(composition.build_document_store, {"AP_DOCUMENT_STORAGE_DIRECTORY": str(inside)}, PermissivePolicy()), CODE_UNAVAILABLE)
                self.assertEqual(code_of(composition.prepare_document_storage, {"AP_DOCUMENT_STORAGE_DIRECTORY": str(inside)}, PermissivePolicy()), CODE_UNAVAILABLE)
                self.assertFalse(inside.exists() and inside.name == "documents")  # refusing created nothing
        with mock.patch("config.document_storage.settings", SimpleNamespace(BASE_DIR=BACKEND, STATIC_ROOT="/srv/static", MEDIA_ROOT="/srv/media")):
            roots = {os.path.abspath(r) for r in composition.forbidden_roots()}
        self.assertLessEqual({os.path.abspath("/srv/static"), os.path.abspath("/srv/media")}, roots)

    def test_the_directory_policy_is_never_chosen_by_the_caller_of_a_view(self):
        source = (BACKEND / "config" / "document_storage.py").read_text(encoding="utf-8")
        self.assertIn("policy=None", source)
        views = (BACKEND / "apps" / "api" / "views_documents.py").read_text(encoding="utf-8")
        self.assertNotIn("policy", views)


class PlatformPolicyTests(SimpleTestCase):
    def test_posix_uses_the_posix_policy(self):
        if os.name != "posix":
            self.skipTest("POSIX only")
        with mock.patch.object(sys, "platform", "linux"):
            self.assertIsInstance(composition._platform_policy({}), PosixDirectoryPolicy)

    def test_an_unknown_platform_fails_closed(self):
        with mock.patch.object(sys, "platform", "plan9"), mock.patch.object(os, "name", "plan9"):
            self.assertEqual(code_of(composition._platform_policy, {}), CODE_UNAVAILABLE)

    def test_windows_wraps_the_hardened_secret_store_directory_policy_and_translates_its_errors(self):
        from apps.secret_store import CODE_UNAVAILABLE as SECRET_UNAVAILABLE
        from apps.secret_store import SecretStoreError

        created = {}

        class FakeWindowsPolicy:
            def __init__(self, approved, expected):
                created["args"] = (approved, expected)
                self.calls = []

            def create(self, path):
                self.calls.append(("create", path))

            def verify(self, path):
                raise SecretStoreError(SECRET_UNAVAILABLE)

            def verify_file(self, descriptor):
                raise SecretStoreError(SECRET_UNAVAILABLE)

        fake_module = ModuleType("config.secret_store_dpapi")
        fake_module.SecretStoreError = SecretStoreError
        fake_module.WindowsDirectoryPolicy = FakeWindowsPolicy
        env = {"AP_DOCUMENT_STORAGE_APPROVED_IDENTITY_SIDS": "S-1-5-19, S-1-5-20", "AP_DOCUMENT_STORAGE_EXPECTED_IDENTITY_SID": "S-1-5-21-1-2-3-1001"}
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(sys.modules, {"config.secret_store_dpapi": fake_module}):
            policy = composition._platform_policy(env)
            self.assertIsInstance(policy, DirectoryPolicy)
            self.assertEqual(created["args"], (("S-1-5-19", "S-1-5-20"), "S-1-5-21-1-2-3-1001"))
            policy.create("x")
            self.assertEqual(code_of(policy.verify, "x"), CODE_UNAVAILABLE)  # a SecretStoreError is never leaked: it becomes the document storage's error
            self.assertEqual(code_of(policy.verify_file, 0), CODE_UNAVAILABLE)

    def test_windows_construction_failure_is_the_document_storage_error(self):
        from apps.secret_store import CODE_UNAVAILABLE as SECRET_UNAVAILABLE
        from apps.secret_store import SecretStoreError

        def refuse(approved, expected):
            raise SecretStoreError(SECRET_UNAVAILABLE)

        fake_module = ModuleType("config.secret_store_dpapi")
        fake_module.SecretStoreError = SecretStoreError
        fake_module.WindowsDirectoryPolicy = refuse
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(sys.modules, {"config.secret_store_dpapi": fake_module}):
            self.assertEqual(code_of(composition._platform_policy, {}), CODE_UNAVAILABLE)

    def test_the_windows_policy_is_the_one_the_secret_store_uses_not_a_copy(self):
        source = (BACKEND / "config" / "document_storage.py").read_text(encoding="utf-8")
        self.assertIn("from config.secret_store_dpapi import SecretStoreError, WindowsDirectoryPolicy", source)
        self.assertNotIn("ConvertStringSecurityDescriptor", source)  # no ACL code of its own


class PrepareCommandTests(SimpleTestCase):
    def run_command(self, env, policy=None):
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch("config.document_storage._platform_policy", return_value=PermissivePolicy() if policy is None else policy):
            call_command("prepare_document_storage", stdout=out)
        return out.getvalue()

    def test_it_creates_the_layout_prints_no_path_and_is_repeatable(self):
        with temporary_root() as root:
            first = self.run_command({"AP_DOCUMENT_STORAGE_DIRECTORY": root})
            second = self.run_command({"AP_DOCUMENT_STORAGE_DIRECTORY": root})
            self.assertEqual(first, "The document storage directory is ready.\n")
            self.assertEqual(second, first)
            self.assertEqual(sorted(os.listdir(root)), ["incoming", "objects"])
            self.assertNotIn(root, first)

    def test_it_explains_without_a_path_when_the_storage_is_unconfigured_or_unsafe(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AP_DOCUMENT_STORAGE_DIRECTORY", None)
            with self.assertRaises(CommandError) as caught:
                call_command("prepare_document_storage", stdout=io.StringIO())
        self.assertIn("document_storage_disabled", str(caught.exception))
        with self.assertRaises(CommandError) as caught:
            self.run_command({"AP_DOCUMENT_STORAGE_DIRECTORY": str(REPOSITORY / "documents")})
        self.assertIn("document_storage_unavailable", str(caught.exception))
        self.assertNotIn(str(REPOSITORY), str(caught.exception))
        self.assertFalse((REPOSITORY / "documents").exists())

    def test_an_existing_directory_the_policy_does_not_trust_is_not_changed(self):
        with temporary_root() as root:
            os.mkdir(root)
            with open(os.path.join(root, "keep.txt"), "w") as handle:
                handle.write("kept")
            with self.assertRaises(CommandError):
                self.run_command({"AP_DOCUMENT_STORAGE_DIRECTORY": root}, PermissivePolicy(fail_on="verify"))
            self.assertEqual(os.listdir(root), ["keep.txt"])


@unittest.skipUnless(sys.platform == "win32", "the Windows directory policy")
class RealWindowsPolicyTests(SimpleTestCase):
    """The hardened Windows policy of the secret store, used for real on a temporary directory: a protected DACL for the current identity, verified on every operation. Skipped when this machine's
    temporary location is one the policy refuses by design (a cloud-synced or non-NTFS folder)."""

    def prepared(self, root):
        env = {"AP_DOCUMENT_STORAGE_DIRECTORY": root}
        try:
            composition.prepare_document_storage(env)
        except DocumentStorageError:
            self.skipTest("this temporary location is not acceptable to the Windows directory policy (or the identity is not supported)")
        return env

    def test_prepare_store_upload_read_and_delete_with_the_real_policy(self):
        with temporary_root() as root:
            env = self.prepared(root)
            store = composition.build_document_store(env)
            stored = store.ingest(Reader(VALID_PDF).read, len(VALID_PDF))
            stream = store.open(stored.key, stored.size_bytes)
            self.assertEqual(b"".join(stream), VALID_PDF)
            stream.close()
            self.assertTrue(store.delete(stored.key))
            self.assertEqual(tree_listing(root), [])

    def test_the_directory_is_protected_and_a_loosened_directory_stops_the_store(self):
        import subprocess

        with temporary_root() as root:
            env = self.prepared(root)
            store = composition.build_document_store(env)
            stored = store.ingest(Reader(VALID_PDF).read, len(VALID_PDF))  # it works before the directory is loosened (so the refusal below is the policy's)
            result = subprocess.run(["icacls", root, "/grant", "Everyone:(R)"], capture_output=True, text=True)
            if result.returncode != 0:
                self.skipTest("icacls could not change the directory")
            self.assertEqual(code_of(store.ingest, Reader(VALID_PDF).read, len(VALID_PDF)), CODE_UNAVAILABLE)
            self.assertEqual(code_of(store.open, stored.key, stored.size_bytes), CODE_UNAVAILABLE)
            self.assertEqual(code_of(composition.build_document_store, env), CODE_UNAVAILABLE)

    def test_a_file_that_somebody_else_can_read_is_refused_through_its_open_handle(self):
        import subprocess

        with temporary_root() as root:
            env = self.prepared(root)
            store = composition.build_document_store(env)
            stored = store.ingest(Reader(VALID_PDF).read, len(VALID_PDF))
            path = os.path.join(root, "objects", *stored.key.split("/"))
            result = subprocess.run(["icacls", path, "/grant", "Everyone:(R)"], capture_output=True, text=True)
            if result.returncode != 0:
                self.skipTest("icacls could not change the file")
            self.assertEqual(code_of(store.open, stored.key, stored.size_bytes), CODE_UNAVAILABLE)
