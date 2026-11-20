"""DB-free tests of the Windows DPAPI secret store (AI-SEC-1b).

The file-store logic is tested on every platform with a fake protector and a fake directory policy. The tests marked Windows-only run the REAL DPAPI and the REAL directory ACL, only in a
dedicated temporary directory, with test-only sentinel secrets; they never touch a real credential, the environment or the network. Wrong Windows identity cannot be produced from a
single account, so it is tested with the fake protector (and the real blob's failure on a different entropy, which takes the same decryption-failure path)."""

import ast
import base64
import contextlib
import hashlib
import io
import logging
import os
import shutil
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest import mock

from apps.secret_store import (
    CODE_ALREADY_EXISTS,
    CODE_BUSY,
    CODE_INVALID_REFERENCE,
    CODE_NOT_FOUND,
    CODE_UNAVAILABLE,
    CODE_UNREADABLE,
    CODES,
    SecretStore,
    SecretStoreError,
    SecretValue,
)
from config import secret_store_dpapi as module
from config.secret_store_dpapi import (
    FILE_SUFFIX,
    FORMAT_TAG,
    LOCK_FILE_NAME,
    MAX_FILE_BYTES,
    DirectoryLocker,
    DirectoryPolicy,
    EncryptedFileSecretStore,
    InProcessLocker,
    WindowsFileLocker,
    Protector,
    WindowsDpapiSecretStore,
    dacl_is_restrictive,
    file_name_for,
    prepare_windows_secret_directory,
)
from tests.secret_store_support import ALL_SENTINELS, REF_A, REF_B, SECRET_A, SECRET_B

IS_WINDOWS = sys.platform == "win32"
USER = "S-1-5-21-1-2-3-1001"


class FakeProtector(Protector):
    """Test-only reversible 'encryption' bound to an identity and the entropy; it models what DPAPI guarantees, nothing more."""

    def __init__(self, identity="alice"):
        self.identity = identity

    def protect(self, data, entropy):
        return base64.b64encode(self.identity.encode() + b"|" + hashlib.sha256(entropy).hexdigest().encode() + b"|" + data)

    def unprotect(self, blob, entropy):
        identity, digest, data = base64.b64decode(blob, validate=True).split(b"|", 2)
        if identity != self.identity.encode() or digest != hashlib.sha256(entropy).hexdigest().encode():
            raise ValueError("wrong identity or entropy " + SECRET_A)
        return data


def build(directory, protector, policy, locker=None, **kwargs):
    """A store over the in-process locker: the file-store logic without the cross-process lock (the Windows tests below use the real one)."""
    return EncryptedFileSecretStore(directory, protector, policy, InProcessLocker() if locker is None else locker, **kwargs)


class FakePolicy(DirectoryPolicy):
    def __init__(self, refuse=False, refuse_files=False):
        self.refuse = refuse
        self.refuse_files = refuse_files
        self.verified = []
        self.created = []

    def create(self, path):
        self.created.append(path)
        os.mkdir(path)

    def verify(self, path):
        self.verified.append(path)
        if self.refuse:
            raise SecretStoreError(CODE_UNAVAILABLE)

    def verify_file(self, descriptor):
        self.files_verified = getattr(self, "files_verified", 0) + 1
        if self.refuse_files:
            raise SecretStoreError(CODE_UNAVAILABLE)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage() + " " + repr(record.args) + " " + repr(record.__dict__.get("exc_info")))


def code_of(test, callable_, *args, **kwargs):
    with test.assertRaises(SecretStoreError) as caught:
        callable_(*args, **kwargs)
    return caught.exception.code


class TempDirectoryMixin:
    def make_directory(self, name="store"):
        base = tempfile.mkdtemp(prefix="ap-secret-test-")
        self.addCleanup(shutil.rmtree, base, True)
        return os.path.join(base, name)

    def files(self, directory):
        return sorted(name for name in os.listdir(directory) if name != LOCK_FILE_NAME)

    def all_bytes(self, directory):
        return b"".join(Path(directory, name).read_bytes() for name in os.listdir(directory))


class StoreContract:
    """The SecretStore contract, run against every backend of this module."""

    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.make_store()

    def test_it_is_a_secret_store_and_its_repr_names_only_the_class(self):
        self.assertIsInstance(self.store, SecretStore)
        self.assertEqual(repr(self.store), f"<{type(self.store).__name__}>")

    def test_create_then_get_returns_the_secret(self):
        self.assertIsNone(self.store.put(REF_A, SECRET_A))
        self.assertTrue(self.store.exists(REF_A))
        self.assertEqual(self.store.get(REF_A).reveal(), SECRET_A)
        self.assertIsInstance(self.store.get(REF_A), SecretValue)

    def test_a_missing_reference_is_not_found_and_does_not_exist(self):
        self.assertFalse(self.store.exists(REF_A))
        self.assertEqual(code_of(self, self.store.get, REF_A), CODE_NOT_FOUND)
        self.assertEqual(code_of(self, self.store.delete, REF_A), CODE_NOT_FOUND)

    def test_create_refuses_an_existing_reference_and_keeps_the_old_value(self):
        self.store.put(REF_A, SECRET_A)
        self.assertEqual(code_of(self, self.store.put, REF_A, SECRET_B), CODE_ALREADY_EXISTS)
        self.assertEqual(self.store.get(REF_A).reveal(), SECRET_A)

    def test_replace_refuses_a_missing_reference_and_creates_nothing(self):
        self.assertEqual(code_of(self, self.store.put, REF_A, SECRET_A, replace=True), CODE_NOT_FOUND)
        self.assertFalse(self.store.exists(REF_A))

    def test_rotation_replaces_the_value_and_nothing_else(self):
        self.store.put(REF_A, SECRET_A)
        self.store.put(REF_B, SECRET_B)
        self.store.put(REF_A, SECRET_B, replace=True)
        self.assertEqual(self.store.get(REF_A).reveal(), SECRET_B)
        self.store.put(REF_A, SECRET_A, replace=True)
        self.assertEqual(self.store.get(REF_A).reveal(), SECRET_A)
        self.assertEqual(self.store.get(REF_B).reveal(), SECRET_B)

    def test_delete_removes_only_that_reference_and_cannot_be_repeated(self):
        self.store.put(REF_A, SECRET_A)
        self.store.put(REF_B, SECRET_B)
        self.assertIsNone(self.store.delete(REF_A))
        self.assertFalse(self.store.exists(REF_A))
        self.assertEqual(code_of(self, self.store.get, REF_A), CODE_NOT_FOUND)
        self.assertEqual(code_of(self, self.store.delete, REF_A), CODE_NOT_FOUND)
        self.assertEqual(self.store.get(REF_B).reveal(), SECRET_B)
        self.store.put(REF_A, SECRET_A)  # a deleted reference can be created again

    def test_references_that_differ_only_in_case_are_different(self):
        self.store.put("Alpha", SECRET_A)
        self.store.put("alpha", SECRET_B)
        self.assertEqual(self.store.get("Alpha").reveal(), SECRET_A)
        self.assertEqual(self.store.get("alpha").reveal(), SECRET_B)

    def test_an_invalid_reference_or_value_is_refused_before_any_storage(self):
        for bad in ("", "a/b", "..\\x", None):
            self.assertEqual(code_of(self, self.store.get, bad), CODE_INVALID_REFERENCE)
        self.assertEqual(code_of(self, self.store.put, REF_A, ""), "invalid_secret")
        self.assertFalse(self.store.exists(REF_A))

    def test_a_secret_with_non_ascii_text_round_trips(self):
        text = "sk-test-SENTINEL-\u00e9\u4e2d-9z"
        self.store.put(REF_A, text)
        self.assertEqual(self.store.get(REF_A).reveal(), text)


class FakeBackendContractTests(StoreContract, TempDirectoryMixin, unittest.TestCase):
    def make_store(self):
        self.directory = self.make_directory()
        os.mkdir(self.directory)
        return build(self.directory, FakeProtector(), FakePolicy())


class FileLayoutTests(TempDirectoryMixin, unittest.TestCase):
    def setUp(self):
        self.directory = self.make_directory()
        os.mkdir(self.directory)
        self.policy = FakePolicy()
        self.store = build(self.directory, FakeProtector(), self.policy)

    def test_the_file_name_is_opaque_and_carries_neither_reference_nor_secret(self):
        self.store.put(REF_A, SECRET_A)
        (name,) = self.files(self.directory)
        self.assertEqual(name, file_name_for(REF_A))
        self.assertRegex(name, r"\A[0-9a-f]{64}\.dpapi\Z")
        for text in (REF_A, REF_A.lower(), SECRET_A):
            self.assertNotIn(text.lower(), name)
        self.assertTrue(name.endswith(FILE_SUFFIX))

    def test_a_success_leaves_no_partial_file_and_no_plaintext_on_disk(self):
        self.store.put(REF_A, SECRET_A)
        self.store.put(REF_A, SECRET_B, replace=True)
        self.assertEqual(self.files(self.directory), [file_name_for(REF_A)])
        content = self.all_bytes(self.directory)
        for secret in ALL_SENTINELS:
            for encoding in ("utf-8", "utf-16-le"):
                self.assertNotIn(secret.encode(encoding), content)
        self.assertTrue(content.startswith(FORMAT_TAG))

    def test_every_operation_verifies_the_directory_policy_first(self):
        self.store.put(REF_A, SECRET_A)
        self.store.exists(REF_A)
        self.store.get(REF_A)
        self.store.put(REF_A, SECRET_B, replace=True)
        self.store.delete(REF_A)
        self.assertEqual(len(self.policy.verified), 5)

    def test_a_refused_policy_blocks_every_operation_and_writes_nothing(self):
        self.store.put(REF_A, SECRET_A)
        self.policy.refuse = True
        before = self.all_bytes(self.directory)
        for call in (
            lambda: self.store.put(REF_B, SECRET_B),
            lambda: self.store.put(REF_A, SECRET_B, replace=True),
            lambda: self.store.get(REF_A),
            lambda: self.store.exists(REF_A),
            lambda: self.store.delete(REF_A),
        ):
            self.assertEqual(code_of(self, call), CODE_UNAVAILABLE)
        self.assertEqual(self.all_bytes(self.directory), before)
        self.assertEqual(len(self.files(self.directory)), 1)

    def test_a_missing_directory_is_unavailable_for_every_operation_including_exists(self):
        gone = self.make_directory("never-created")
        store = build(gone, FakeProtector(), FakePolicy())
        for call in (lambda: store.exists(REF_A), lambda: store.get(REF_A), lambda: store.put(REF_A, SECRET_A), lambda: store.delete(REF_A)):
            self.assertEqual(code_of(self, call), CODE_UNAVAILABLE)
        self.assertFalse(os.path.exists(gone))

    def test_a_directory_that_is_a_file_is_unavailable(self):
        target = self.make_directory("plain-file")
        Path(target).write_bytes(b"x")
        self.assertEqual(code_of(self, build(target, FakeProtector(), FakePolicy()).exists, REF_A), CODE_UNAVAILABLE)

    def test_a_relative_directory_is_refused(self):
        for bad in ("relative", ".", "", None, 5):
            self.assertEqual(code_of(self, build, bad, FakeProtector(), FakePolicy()), CODE_UNAVAILABLE)

    def test_a_directory_inside_a_git_working_tree_is_refused(self):
        base = self.make_directory("repo")
        os.makedirs(os.path.join(base, ".git"))
        inside = os.path.join(base, "deep", "secrets")
        self.assertEqual(code_of(self, build, inside, FakeProtector(), FakePolicy()), CODE_UNAVAILABLE)
        self.assertEqual(code_of(self, build, base, FakeProtector(), FakePolicy()), CODE_UNAVAILABLE)

    def test_a_link_as_the_directory_is_refused(self):
        link = self.make_directory("link")
        try:
            os.symlink(self.directory, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this account cannot create symbolic links")
        self.assertEqual(code_of(self, build(link, FakeProtector(), FakePolicy()).exists, REF_A), CODE_UNAVAILABLE)

    def test_a_non_file_where_the_secret_should_be_is_unavailable(self):
        os.mkdir(os.path.join(self.directory, file_name_for(REF_A)))
        for call in (lambda: self.store.exists(REF_A), lambda: self.store.get(REF_A), lambda: self.store.put(REF_A, SECRET_A, replace=True)):
            self.assertEqual(code_of(self, call), CODE_UNAVAILABLE)

    def test_the_backend_only_accepts_a_real_protector_policy_locker_and_timeout(self):
        self.assertEqual(code_of(self, build, self.directory, object(), FakePolicy()), CODE_UNAVAILABLE)
        self.assertEqual(code_of(self, build, self.directory, FakeProtector(), object()), CODE_UNAVAILABLE)
        self.assertEqual(code_of(self, EncryptedFileSecretStore, self.directory, FakeProtector(), FakePolicy(), object()), CODE_UNAVAILABLE)
        for bad in (0, -1, 61, True, None, "5", float("nan")):
            self.assertEqual(code_of(self, build, self.directory, FakeProtector(), FakePolicy(), timeout=bad), CODE_UNAVAILABLE, bad)


class WrongIdentityTests(TempDirectoryMixin, unittest.TestCase):
    def test_another_identity_cannot_read_and_no_environment_credential_is_used(self):
        directory = self.make_directory()
        os.mkdir(directory)
        build(directory, FakeProtector("alice"), FakePolicy()).put(REF_A, SECRET_A)
        other = build(directory, FakeProtector("bob"), FakePolicy())
        with mock.patch.dict(os.environ, {REF_A: SECRET_B, "AP_AI_CRED_X": SECRET_B}):
            with self.assertRaises(SecretStoreError) as caught:
                other.get(REF_A)
        self.assertEqual(caught.exception.code, CODE_UNREADABLE)
        self.assertTrue(other.exists(REF_A))  # existence is not decryption
        for text in (str(caught.exception), repr(caught.exception), "".join(traceback.format_exception(caught.exception))):
            for secret in ALL_SENTINELS:
                self.assertNotIn(secret, text)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_the_original_identity_still_reads_after_the_other_one_failed(self):
        directory = self.make_directory()
        os.mkdir(directory)
        alice = build(directory, FakeProtector("alice"), FakePolicy())
        alice.put(REF_A, SECRET_A)
        code_of(self, build(directory, FakeProtector("bob"), FakePolicy()).get, REF_A)
        self.assertEqual(alice.get(REF_A).reveal(), SECRET_A)

    def test_another_identity_can_neither_replace_nor_silently_overwrite_what_it_cannot_read(self):
        directory = self.make_directory()
        os.mkdir(directory)
        build(directory, FakeProtector("alice"), FakePolicy()).put(REF_A, SECRET_A)
        bob = build(directory, FakeProtector("bob"), FakePolicy())
        self.assertEqual(code_of(self, bob.put, REF_A, SECRET_B), CODE_ALREADY_EXISTS)
        bob.put(REF_A, SECRET_B, replace=True)  # an explicit, deliberate rotation by the new identity is allowed and re-encrypts for it
        self.assertEqual(bob.get(REF_A).reveal(), SECRET_B)

    def test_a_non_windows_platform_fails_closed_at_construction(self):
        directory = self.make_directory()
        os.mkdir(directory)
        with mock.patch.object(module.sys, "platform", "linux"):
            self.assertEqual(code_of(self, WindowsDpapiSecretStore, directory), CODE_UNAVAILABLE)
            self.assertEqual(code_of(self, prepare_windows_secret_directory, directory), CODE_UNAVAILABLE)


class CorruptionTests(TempDirectoryMixin, unittest.TestCase):
    def setUp(self):
        self.directory = self.make_directory()
        os.mkdir(self.directory)
        self.store = build(self.directory, FakeProtector(), FakePolicy())
        self.store.put(REF_A, SECRET_A)
        self.path = Path(self.directory, file_name_for(REF_A))

    def assertUnreadable(self):
        with self.assertRaises(SecretStoreError) as caught:
            self.store.get(REF_A)
        self.assertEqual(caught.exception.code, CODE_UNREADABLE)
        for secret in ALL_SENTINELS:
            self.assertNotIn(secret, str(caught.exception) + repr(caught.exception))

    def test_flipped_bytes_are_unreadable(self):
        data = bytearray(self.path.read_bytes())
        data[len(FORMAT_TAG) + 3] ^= 0xFF
        self.path.write_bytes(bytes(data))
        self.assertUnreadable()

    def test_truncation_is_unreadable(self):
        self.path.write_bytes(self.path.read_bytes()[:-5])
        self.assertUnreadable()

    def test_an_empty_file_a_tag_only_file_and_garbage_are_unreadable(self):
        for content in (b"", FORMAT_TAG, b"garbage", b"\x00" * 64, FORMAT_TAG + b"\xff\xfe"):
            self.path.write_bytes(content)
            self.assertUnreadable()

    def test_the_wrong_format_tag_is_unreadable(self):
        self.path.write_bytes(b"APSS\x02" + self.path.read_bytes()[len(FORMAT_TAG) :])
        self.assertUnreadable()

    def test_an_oversized_file_is_unreadable_without_being_read_whole(self):
        self.path.write_bytes(FORMAT_TAG + b"A" * (MAX_FILE_BYTES + 10))
        self.assertUnreadable()

    def test_decrypted_bytes_that_are_not_a_valid_secret_are_unreadable(self):
        for payload in (b"\xff\xfe\xfd", b"", b" padded ", b"line\nbreak"):
            self.path.write_bytes(FORMAT_TAG + FakeProtector().protect(payload, module.entropy_for(REF_A)))
            self.assertUnreadable()

    def test_a_file_copied_under_another_reference_is_unreadable(self):
        shutil.copy(self.path, Path(self.directory, file_name_for(REF_B)))
        with self.assertRaises(SecretStoreError) as caught:
            self.store.get(REF_B)
        self.assertEqual(caught.exception.code, CODE_UNREADABLE)

    def test_a_corrupted_file_can_be_replaced_deliberately_and_then_reads_again(self):
        self.path.write_bytes(b"garbage")
        self.store.put(REF_A, SECRET_B, replace=True)
        self.assertEqual(self.store.get(REF_A).reveal(), SECRET_B)

    def test_corruption_never_falls_back_to_the_environment(self):
        self.path.write_bytes(b"garbage")
        with mock.patch.dict(os.environ, {REF_A: SECRET_B}):
            self.assertEqual(code_of(self, self.store.get, REF_A), CODE_UNREADABLE)

    def test_a_protector_failure_carrying_a_secret_is_replaced_by_a_fixed_code(self):
        class Leaky(FakeProtector):
            def unprotect(self, blob, entropy):
                raise RuntimeError(SECRET_A)

        store = build(self.directory, Leaky(), FakePolicy())
        with self.assertRaises(SecretStoreError) as caught:
            store.get(REF_A)
        self.assertEqual(caught.exception.code, CODE_UNREADABLE)
        self.assertNotIn(SECRET_A, "".join(traceback.format_exception(caught.exception)))


class AtomicWriteTests(TempDirectoryMixin, unittest.TestCase):
    def setUp(self):
        self.directory = self.make_directory()
        os.mkdir(self.directory)
        self.store = build(self.directory, FakeProtector(), FakePolicy())

    def assertOnly(self, *references):
        self.assertEqual(self.files(self.directory), sorted(file_name_for(ref) for ref in references))

    def test_a_failed_replace_keeps_the_previous_secret_and_leaves_no_partial_file(self):
        self.store.put(REF_A, SECRET_A)
        with mock.patch("os.replace", side_effect=OSError("disk " + SECRET_B)):
            self.assertEqual(code_of(self, self.store.put, REF_A, SECRET_B, replace=True), CODE_UNAVAILABLE)
        self.assertEqual(self.store.get(REF_A).reveal(), SECRET_A)
        self.assertOnly(REF_A)

    def test_a_failed_create_leaves_nothing(self):
        target = "os.rename" if IS_WINDOWS else "os.link"
        with mock.patch(target, side_effect=OSError("disk " + SECRET_B)):
            self.assertEqual(code_of(self, self.store.put, REF_A, SECRET_B), CODE_UNAVAILABLE)
        self.assertOnly()
        self.assertFalse(self.store.exists(REF_A))

    def test_a_write_failure_leaves_nothing_and_the_error_carries_no_secret(self):
        with mock.patch("os.write", side_effect=OSError("disk " + SECRET_B)):
            with self.assertRaises(SecretStoreError) as caught:
                self.store.put(REF_A, SECRET_B)
        self.assertEqual(caught.exception.code, CODE_UNAVAILABLE)
        self.assertNotIn(SECRET_B, "".join(traceback.format_exception(caught.exception)))
        self.assertOnly()

    def test_an_fsync_failure_leaves_nothing_and_keeps_the_old_secret_on_replace(self):
        self.store.put(REF_A, SECRET_A)
        with mock.patch("os.fsync", side_effect=OSError("flush")):
            self.assertEqual(code_of(self, self.store.put, REF_A, SECRET_B, replace=True), CODE_UNAVAILABLE)
        self.assertEqual(self.store.get(REF_A).reveal(), SECRET_A)
        self.assertOnly(REF_A)

    def test_a_protector_failure_writes_nothing_not_even_a_partial_file(self):
        class Broken(FakeProtector):
            def protect(self, data, entropy):
                raise RuntimeError(SECRET_A)

        store = build(self.directory, Broken(), FakePolicy())
        self.assertEqual(code_of(self, store.put, REF_A, SECRET_A), CODE_UNAVAILABLE)
        self.assertOnly()

    def test_short_writes_are_completed(self):
        real = os.write
        with mock.patch("os.write", side_effect=lambda fd, data: real(fd, bytes(data[:3]))):
            self.store.put(REF_A, SECRET_A)
        self.assertEqual(self.store.get(REF_A).reveal(), SECRET_A)
        self.assertOnly(REF_A)

    def test_a_create_that_loses_a_race_to_another_writer_does_not_overwrite_it(self):
        other = build(self.directory, FakeProtector(), FakePolicy())
        real = module.EncryptedFileSecretStore._present
        raced = []

        def present_then_race(path):
            result = real(path)
            if not result and not raced:
                raced.append(True)
                other.put(REF_A, SECRET_B)
            return result

        with mock.patch.object(module.EncryptedFileSecretStore, "_present", staticmethod(present_then_race)):
            self.assertEqual(code_of(self, self.store.put, REF_A, SECRET_A), CODE_ALREADY_EXISTS)
        self.assertEqual(self.store.get(REF_A).reveal(), SECRET_B)
        self.assertOnly(REF_A)
    def test_the_partial_file_is_in_the_store_directory_and_named_without_the_reference(self):
        seen = []
        real = os.open

        def spy(path, *args, **kwargs):
            seen.append(path)
            return real(path, *args, **kwargs)

        with mock.patch("os.open", side_effect=spy):
            self.store.put(REF_A, SECRET_A)
        (partial,) = seen
        self.assertEqual(os.path.dirname(partial), self.directory)
        self.assertTrue(os.path.basename(partial).endswith(".partial"))
        self.assertNotIn(REF_A, partial)


class RedactionTests(TempDirectoryMixin, unittest.TestCase):
    def setUp(self):
        self.capture = Capture()
        root = logging.getLogger()
        self._level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self.capture)
        self.addCleanup(root.removeHandler, self.capture)
        self.addCleanup(root.setLevel, self._level)
        self.directory = self.make_directory()
        os.mkdir(self.directory)
        self.store = build(self.directory, FakeProtector(), FakePolicy())

    def test_no_operation_writes_to_streams_or_logs_and_no_text_contains_a_secret(self):
        out, err = io.StringIO(), io.StringIO()
        texts = []
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.store.put(REF_A, SECRET_A)
            value = self.store.get(REF_A)
            texts += [repr(value), str(value), f"{value}", repr(self.store)]
            self.store.put(REF_A, SECRET_B, replace=True)
            self.store.exists(REF_A)
            for call in (lambda: self.store.put(REF_A, SECRET_B), lambda: self.store.get(REF_B), lambda: self.store.delete(REF_B)):
                with self.assertRaises(SecretStoreError) as caught:
                    call()
                texts += [str(caught.exception), repr(caught.exception), "".join(traceback.format_exception(caught.exception))]
            self.store.delete(REF_A)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        everything = "\n".join(texts + self.capture.lines + [self.directory])
        for secret in ALL_SENTINELS:
            self.assertNotIn(secret, everything)

    def test_error_messages_hold_neither_reference_nor_path(self):
        for call in (lambda: self.store.get(REF_B), lambda: build("relative", FakeProtector(), FakePolicy())):
            with self.assertRaises(SecretStoreError) as caught:
                call()
            self.assertNotIn(REF_B, str(caught.exception))
            self.assertNotIn(self.directory, str(caught.exception))
            self.assertNotIn("relative", str(caught.exception))

    def test_every_code_has_a_fixed_message_including_the_new_ones(self):
        self.assertIn(CODE_UNREADABLE, CODES)
        self.assertIn(CODE_BUSY, CODES)
        self.assertEqual(str(SecretStoreError(CODE_UNREADABLE)), "The stored secret cannot be read.")
        self.assertEqual(str(SecretStoreError(CODE_BUSY)), "The secret store is busy; try again later.")


class DaclRuleTests(unittest.TestCase):
    def test_a_protected_dacl_for_the_current_user_only_is_restrictive(self):
        self.assertTrue(dacl_is_restrictive(f"D:P(A;OICI;FA;;;{USER})", USER))
        self.assertTrue(dacl_is_restrictive(f"D:PAI(A;OICI;FA;;;{USER})", USER))

    def test_system_and_administrators_are_tolerated_beside_the_user(self):
        self.assertTrue(dacl_is_restrictive(f"D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{USER})", USER))
        self.assertTrue(dacl_is_restrictive(f"D:P(A;OICI;FA;;;S-1-5-18)(A;OICI;FA;;;S-1-5-32-544)(A;OICI;FA;;;{USER})", USER))

    def test_an_unprotected_dacl_is_refused(self):
        self.assertFalse(dacl_is_restrictive(f"D:AI(A;OICIID;FA;;;{USER})", USER))
        self.assertFalse(dacl_is_restrictive(f"D:(A;OICI;FA;;;{USER})", USER))

    def test_any_other_trustee_is_refused(self):
        for other in ("WD", "AU", "BU", "S-1-5-21-1-2-3-1002", "IU", "S-1-1-0", "S-1-5-32-545"):
            self.assertFalse(dacl_is_restrictive(f"D:P(A;OICI;FA;;;{USER})(A;OICI;FR;;;{other})", USER), other)

    def test_deny_entries_odd_input_and_a_missing_user_are_refused(self):
        self.assertFalse(dacl_is_restrictive(f"D:P(D;OICI;FA;;;WD)(A;OICI;FA;;;{USER})", USER))
        self.assertFalse(dacl_is_restrictive("D:P", USER))
        self.assertFalse(dacl_is_restrictive("D:P(A;OICI;FA;;;SY)", USER))
        self.assertFalse(dacl_is_restrictive("", USER))
        self.assertFalse(dacl_is_restrictive("O:BAG:BA", USER))
        self.assertFalse(dacl_is_restrictive("D:P(A;;)", USER))

    def test_an_alias_the_table_cannot_resolve_is_untrusted_unless_the_operating_system_resolver_is_used(self):
        sid = "S-1-5-21-1-2-3-500"
        self.assertFalse(dacl_is_restrictive(f"D:P(A;OICI;FA;;;LA)(A;OICI;FA;;;{sid})", sid))
        resolver = {"LA": sid}.get
        self.assertTrue(dacl_is_restrictive(f"D:P(A;OICI;FA;;;LA)(A;OICI;FA;;;{sid})", sid, lambda text: resolver(text, text)))
        self.assertFalse(dacl_is_restrictive(f"D:P(A;OICI;FA;;;LA)(A;OICI;FA;;;{USER})", USER, lambda text: resolver(text, text)))


@unittest.skipUnless(IS_WINDOWS, "Windows DPAPI is only available on Windows")
class WindowsIntegrationTests(StoreContract, TempDirectoryMixin, unittest.TestCase):
    """The real DPAPI and ACL, in a dedicated temporary directory, with test-only sentinel secrets."""

    def make_store(self):
        self.directory = self.make_directory()
        prepare_windows_secret_directory(self.directory)
        return WindowsDpapiSecretStore(self.directory)

    def path_of(self, reference):
        return Path(self.directory, file_name_for(reference))

    def test_the_ciphertext_file_holds_no_plaintext_in_any_common_encoding(self):
        self.store.put(REF_A, SECRET_A)
        self.store.put(REF_A, SECRET_B, replace=True)
        content = self.all_bytes(self.directory)
        for secret in ALL_SENTINELS:
            for encoding in ("utf-8", "utf-16-le"):
                self.assertNotIn(secret.encode(encoding), content)
        self.assertEqual(self.files(self.directory), [file_name_for(REF_A)])

    def test_a_flipped_byte_and_truncation_fail_closed(self):
        self.store.put(REF_A, SECRET_A)
        original = self.path_of(REF_A).read_bytes()
        for damaged in (original[:-1], original[: len(FORMAT_TAG) + 10], original[:-8] + bytes(b ^ 0xFF for b in original[-8:])):
            self.path_of(REF_A).write_bytes(damaged)
            self.assertEqual(code_of(self, self.store.get, REF_A), CODE_UNREADABLE)
        flipped = bytearray(original)
        flipped[len(original) // 2] ^= 0x01
        self.path_of(REF_A).write_bytes(bytes(flipped))
        self.assertEqual(code_of(self, self.store.get, REF_A), CODE_UNREADABLE)

    def test_a_blob_made_for_another_reference_does_not_decrypt(self):
        self.store.put(REF_A, SECRET_A)
        self.store.put(REF_B, SECRET_B)
        shutil.copy(self.path_of(REF_A), self.path_of(REF_B))
        self.assertEqual(code_of(self, self.store.get, REF_B), CODE_UNREADABLE)
        self.assertEqual(self.store.get(REF_A).reveal(), SECRET_A)

    def test_a_blob_for_a_different_entropy_fails_through_the_wrong_identity_path(self):
        protector = module.WindowsDpapiProtector()
        blob = protector.protect(SECRET_A.encode(), b"one")
        self.assertEqual(protector.unprotect(blob, b"one").decode(), SECRET_A)
        with self.assertRaises(SecretStoreError) as caught:
            protector.unprotect(blob, b"two")
        self.assertEqual(caught.exception.code, CODE_UNREADABLE)

    def test_dpapi_is_called_with_the_ui_forbidden_flag_only_so_the_scope_is_the_current_user(self):
        protector = module.WindowsDpapiProtector()
        flags = []
        real = protector._api.crypt32

        class Spy:
            def CryptProtectData(self, *args):
                flags.append(args[5])
                return real.CryptProtectData(*args)

            def CryptUnprotectData(self, *args):
                flags.append(args[5])
                return real.CryptUnprotectData(*args)

        protector._api.crypt32 = Spy()
        protector.unprotect(protector.protect(b"abc", b"e"), b"e")
        self.assertEqual(flags, [1, 1])

    def test_a_blob_is_not_machine_scoped_and_differs_each_time(self):
        protector = module.WindowsDpapiProtector()
        self.assertNotEqual(protector.protect(b"abc", b"e"), protector.protect(b"abc", b"e"))

    def test_the_prepared_directory_is_restricted_to_the_current_user_and_prepare_is_repeatable(self):
        policy = module.WindowsDirectoryPolicy()
        policy.verify(self.directory)
        prepare_windows_secret_directory(self.directory)
        self.store.put(REF_A, SECRET_A)
        policy.verify(self.directory)

    def test_a_directory_with_inherited_permissions_is_refused_and_never_loosened_or_adopted(self):
        loose = self.make_directory("loose")
        os.mkdir(loose)
        with self.assertRaises(SecretStoreError) as caught:
            prepare_windows_secret_directory(loose)
        self.assertEqual(caught.exception.code, CODE_UNAVAILABLE)
        store = WindowsDpapiSecretStore(loose)
        for call in (lambda: store.put(REF_A, SECRET_A), lambda: store.exists(REF_A), lambda: store.get(REF_A)):
            self.assertEqual(code_of(self, call), CODE_UNAVAILABLE)
        self.assertEqual(os.listdir(loose), [])

    def test_a_network_path_is_refused(self):
        with self.assertRaises(SecretStoreError):
            module.WindowsDirectoryPolicy().verify("\\\\localhost\\c$\\nope")


class PackageBoundaryTests(unittest.TestCase):
    def test_the_abstraction_package_is_untouched_by_this_backend(self):
        package = Path(module.__file__).resolve().parent.parent / "apps" / "secret_store"
        self.assertEqual({p.name for p in package.iterdir()} - {"__pycache__"}, {"__init__.py", "errors.py", "store.py", "values.py"})

    def test_the_module_reads_no_environment_and_makes_no_network_subprocess_or_home_made_crypto(self):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and node.body and isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant):
                node.body[0].value = ast.Constant(value="")
        code = ast.unparse(tree)
        for token in ("environ", "getenv", "socket", "subprocess", "urllib", "http", "logging", "print(", "winreg", "django", "cryptography", "base64", "hmac", "AES", "Fernet", "random", "LOCAL_MACHINE"):
            self.assertNotIn(token, code, token)
        imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        self.assertLessEqual(imported, {"contextlib", "ctypes", "hashlib", "msvcrt", "os", "re", "secrets", "stat", "sys", "threading", "time"})

if __name__ == "__main__":
    unittest.main()
