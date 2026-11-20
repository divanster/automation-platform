"""Windows identity and filesystem hardening tests for the DPAPI secret store (AI-SEC-1d).

The pure rules (SID comparison, supported identities, owner and ACL trust, volume safety) and the generic file checks run everywhere. The Windows-only tests use the REAL
operating system (SID resolution, ACLs, owners, junctions, hard links, DPAPI), only in dedicated temporary directories and with synthetic credentials; they never touch a real
credential, the environment, a database or the network. A second Windows account is not available to a test, so a foreign directory owner is tested with the owner of
``C:\\Windows`` and a foreign identity with a patched SID or a fake protector."""

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest import mock

from apps.secret_store import (
    CODE_NOT_FOUND,
    CODE_UNAVAILABLE,
    CODE_UNREADABLE,
    SecretStoreError,
)
from config import secret_store_dpapi as module
from config.secret_store_dpapi import (
    LOCK_FILE_NAME,
    WindowsDirectoryPolicy,
    WindowsDpapiSecretStore,
    check_open_file,
    default_canonical_sid,
    descriptor_is_trusted,
    file_name_for,
    identity_supported,
    normalize_approved_sids,
    prepare_windows_secret_directory,
    reject_links,
    volume_is_safe,
)
from tests.secret_store_support import ALL_SENTINELS, REF_A, REF_B, SECRET_A, SECRET_B
from tests.test_secret_store_dpapi import FakePolicy, FakeProtector, build, code_of

IS_WINDOWS = sys.platform == "win32"
USER = "S-1-5-21-1111-2222-3333-1001"
OTHER = "S-1-5-21-1111-2222-3333-1002"
SYSTEM, LOCAL_SERVICE, NETWORK_SERVICE, ADMINS, EVERYONE = "S-1-5-18", "S-1-5-19", "S-1-5-20", "S-1-5-32-544", "S-1-1-0"
TRUSTED_INSTALLER = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"


class IdentityRuleTests(unittest.TestCase):
    def test_ordinary_azure_ad_and_virtual_service_accounts_are_supported(self):
        for sid in (USER, "S-1-5-21-1-2-3-500", "S-1-5-21-1-2-3-1000", "S-1-12-1-1-2-3-4", "S-1-5-80-1-2-3-4-5"):
            self.assertTrue(identity_supported(sid), sid)

    def test_builtin_service_identities_and_everything_else_are_refused_by_default(self):
        for sid in (SYSTEM, LOCAL_SERVICE, NETWORK_SERVICE, ADMINS, EVERYONE, "S-1-5-7", "S-1-5-21-1-2-3-501", "S-1-5-21-1-2-3-512", "S-1-5-21-1-2-3-999", "S-1-15-2-1-2-3-4-5-6-7", "S-1-5-90-0-1", "", "SY", "S-1-5-21", "S-1-5-21-1-2-3-1001-9"):
            self.assertFalse(identity_supported(sid), sid)

    def test_a_builtin_identity_is_supported_only_when_the_operator_approved_exactly_that_sid(self):
        approved = normalize_approved_sids([LOCAL_SERVICE])
        self.assertTrue(identity_supported(LOCAL_SERVICE, approved))
        self.assertFalse(identity_supported(SYSTEM, approved))
        self.assertFalse(identity_supported(NETWORK_SERVICE, approved))
        self.assertFalse(identity_supported(EVERYONE, normalize_approved_sids([LOCAL_SERVICE])))

    def test_the_approval_list_is_strict_and_a_string_is_not_a_list_of_sids(self):
        self.assertEqual(normalize_approved_sids(None), frozenset())
        self.assertEqual(normalize_approved_sids(()), frozenset())
        self.assertEqual(normalize_approved_sids({SYSTEM, LOCAL_SERVICE}), {SYSTEM, LOCAL_SERVICE})
        for bad in (["SY"], [""], [5], [None], ["S-1-5-18 "], "S-1-5-18", 5, ["S-1-5-18", "NS"]):
            self.assertEqual(code_of(self, normalize_approved_sids, bad), CODE_UNAVAILABLE, bad)

    def test_aliases_resolve_by_a_fixed_table_and_unknown_ones_do_not_resolve(self):
        self.assertEqual([default_canonical_sid(x) for x in ("SY", "BA", "LS", "NS", "WD")], [SYSTEM, ADMINS, LOCAL_SERVICE, NETWORK_SERVICE, EVERYONE])
        self.assertEqual(default_canonical_sid(USER), USER)
        for unknown in ("LA", "ZZ", "", "sy", "S-1", "S-1-5-18 "):
            self.assertIsNone(default_canonical_sid(unknown), unknown)


class SidComparisonTests(unittest.TestCase):
    """The regression of the review: Windows writes SYSTEM, LocalService and NetworkService back as SY, LS and NS."""

    def test_an_alias_trustee_matches_the_identity_it_stands_for(self):
        for sid, alias in ((SYSTEM, "SY"), (LOCAL_SERVICE, "LS"), (NETWORK_SERVICE, "NS")):
            self.assertTrue(descriptor_is_trusted(f"O:BAD:P(A;OICI;FA;;;{alias})", sid, owner_required=True), sid)
            self.assertTrue(descriptor_is_trusted(f"D:PAI(A;OICI;FA;;;{alias})", sid), sid)

    def test_an_alias_never_stands_for_a_different_identity(self):
        self.assertFalse(descriptor_is_trusted("D:P(A;OICI;FA;;;NS)", LOCAL_SERVICE))
        self.assertFalse(descriptor_is_trusted("D:P(A;OICI;FA;;;LS)(A;OICI;FA;;;NS)", LOCAL_SERVICE))
        self.assertFalse(descriptor_is_trusted(f"D:P(A;OICI;FA;;;{USER})(A;OICI;FA;;;WD)", USER))

    def test_the_owner_must_be_the_identity_system_or_administrators(self):
        for owner in (USER, "BA", "SY", ADMINS):
            self.assertTrue(descriptor_is_trusted(f"O:{owner}D:P(A;OICI;FA;;;{USER})", USER, owner_required=True), owner)
        for owner in (OTHER, TRUSTED_INSTALLER, EVERYONE, "WD", "LA", "BU", "S-1-5-32-545"):
            self.assertFalse(descriptor_is_trusted(f"O:{owner}D:P(A;OICI;FA;;;{USER})", USER, owner_required=True), owner)

    def test_a_missing_owner_is_untrusted_when_an_owner_is_required(self):
        self.assertFalse(descriptor_is_trusted(f"D:P(A;OICI;FA;;;{USER})", USER, owner_required=True))
        self.assertTrue(descriptor_is_trusted(f"D:P(A;OICI;FA;;;{USER})", USER))

    def test_an_unresolvable_owner_or_trustee_is_untrusted(self):
        self.assertFalse(descriptor_is_trusted(f"O:LAD:P(A;OICI;FA;;;{USER})", USER, owner_required=True))
        self.assertFalse(descriptor_is_trusted(f"D:P(A;OICI;FA;;;{USER})(A;OICI;FA;;;LA)", USER))

    def test_a_file_acl_may_inherit_but_may_not_grant_anyone_else_or_deny(self):
        self.assertTrue(descriptor_is_trusted(f"O:{USER}D:AI(A;ID;FA;;;{USER})(A;ID;FA;;;SY)", USER, protected=False, owner_required=True))
        for extra in (f"(A;;FR;;;{EVERYONE})", "(A;;FR;;;WD)", f"(A;;FR;;;{OTHER})", "(D;;FW;;;WD)", "(OA;;FR;;;WD)"):
            self.assertFalse(descriptor_is_trusted(f"O:{USER}D:AI(A;ID;FA;;;{USER}){extra}", USER, protected=False, owner_required=True), extra)

    def test_a_directory_acl_must_still_be_protected(self):
        self.assertFalse(descriptor_is_trusted(f"O:{USER}D:AI(A;OICIID;FA;;;{USER})", USER, protected=True, owner_required=True))
        self.assertTrue(descriptor_is_trusted(f"O:{USER}D:AI(A;OICIID;FA;;;{USER})", USER, protected=False, owner_required=True))


class VolumeRuleTests(unittest.TestCase):
    def test_only_a_fixed_disk_that_keeps_acls_is_safe(self):
        self.assertTrue(volume_is_safe(3, 0x8 | 0x1))
        for drive_type in (0, 1, 2, 4, 5, 6):
            self.assertFalse(volume_is_safe(drive_type, 0x8), drive_type)
        self.assertFalse(volume_is_safe(3, 0x1))  # FAT: no persistent ACLs


class OpenFileCheckTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="ap-secret-hard-")
        self.addCleanup(shutil.rmtree, self.base, True)

    def open_file(self, path, flags=os.O_RDONLY):
        descriptor = os.open(path, flags | getattr(os, "O_BINARY", 0))
        self.addCleanup(os.close, descriptor)
        return descriptor

    def test_a_plain_file_passes_and_a_second_hard_link_is_refused(self):
        original = os.path.join(self.base, "a")
        Path(original).write_bytes(b"x")
        check_open_file(self.open_file(original))
        os.link(original, os.path.join(self.base, "b"))
        self.assertEqual(code_of(self, check_open_file, self.open_file(original)), CODE_UNAVAILABLE)

    def test_a_chain_without_links_passes_and_a_missing_component_is_refused(self):
        os.mkdir(os.path.join(self.base, "deep"))
        reject_links(os.path.join(self.base, "deep"))
        self.assertEqual(code_of(self, reject_links, os.path.join(self.base, "missing")), CODE_UNAVAILABLE)


class GenericFileSafetyTests(unittest.TestCase):
    """The file checks of the generic store, with a fake protector and policy."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="ap-secret-hard-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.directory = os.path.join(self.base, "store")
        os.mkdir(self.directory)
        self.policy = FakePolicy()
        self.protector = FakeProtector()
        self.store = build(self.directory, self.protector, self.policy)
        self.path = Path(self.directory, file_name_for(REF_A))

    def test_a_read_verifies_the_open_file_before_it_decrypts_anything(self):
        self.store.put(REF_A, SECRET_A)
        self.policy.files_verified = 0
        with mock.patch.object(FakeProtector, "unprotect", side_effect=AssertionError("decrypted")) as unprotect:
            self.policy.refuse_files = True
            self.assertEqual(code_of(self, self.store.get, REF_A), CODE_UNAVAILABLE)
            unprotect.assert_not_called()
        self.policy.refuse_files = False
        self.assertEqual(self.store.get(REF_A).reveal(), SECRET_A)
        self.assertEqual(self.policy.files_verified, 2)

    def test_a_write_verifies_the_new_file_and_a_refusal_changes_nothing(self):
        self.store.put(REF_A, SECRET_A)
        before = self.path.read_bytes()
        self.policy.refuse_files = True
        self.assertEqual(code_of(self, self.store.put, REF_A, SECRET_B, replace=True), CODE_UNAVAILABLE)
        self.assertEqual(code_of(self, self.store.put, REF_B, SECRET_B), CODE_UNAVAILABLE)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(os.listdir(self.directory)), [file_name_for(REF_A)])

    def test_a_hard_link_to_a_secret_makes_every_operation_refuse_and_the_other_name_is_untouched(self):
        self.store.put(REF_A, SECRET_A)
        elsewhere = os.path.join(self.base, "elsewhere.bin")
        os.link(self.path, elsewhere)
        for call in (lambda: self.store.get(REF_A), lambda: self.store.exists(REF_A), lambda: self.store.delete(REF_A), lambda: self.store.put(REF_A, SECRET_B, replace=True)):
            self.assertEqual(code_of(self, call), CODE_UNAVAILABLE)
        self.assertEqual(Path(elsewhere).read_bytes(), self.path.read_bytes())

    def test_a_foreign_file_hard_linked_into_the_store_is_refused(self):
        foreign = os.path.join(self.base, "foreign.bin")
        Path(foreign).write_bytes(module.FORMAT_TAG + FakeProtector().protect(SECRET_B.encode(), module.entropy_for(REF_B)))
        os.link(foreign, os.path.join(self.directory, file_name_for(REF_B)))
        self.assertEqual(code_of(self, self.store.get, REF_B), CODE_UNAVAILABLE)

    def test_a_filesystem_error_reveals_neither_a_secret_nor_a_path_and_keeps_no_context(self):
        self.store.put(REF_A, SECRET_A)
        leak = f"{self.directory}\\{SECRET_A} {SECRET_B}"
        for target, call in (("os.read", lambda: self.store.get(REF_A)), ("os.unlink", lambda: self.store.delete(REF_A)), ("os.fsync", lambda: self.store.put(REF_A, SECRET_B, replace=True))):
            with mock.patch(target, side_effect=OSError(5, leak)):
                with self.assertRaises(SecretStoreError) as caught:
                    call()
            error = caught.exception
            self.assertEqual(error.code, CODE_UNAVAILABLE, target)
            self.assertIsNone(error.__context__)
            self.assertIsNone(error.__cause__)
            text = str(error) + repr(error) + "".join(traceback.format_exception(error))
            for forbidden in (*ALL_SENTINELS, self.directory):
                self.assertNotIn(forbidden, text)

    def test_a_backend_that_raises_a_plain_exception_leaves_no_context_either(self):
        class Broken(FakeProtector):
            def protect(self, data, entropy):
                raise RuntimeError(SECRET_A)

        with self.assertRaises(SecretStoreError) as caught:
            build(self.directory, Broken(), self.policy).put(REF_B, SECRET_B)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn(SECRET_A, "".join(traceback.format_exception(caught.exception)))


class RestoreTests(unittest.TestCase):
    """What a restored backup can and cannot do. Fake protector: the files are portable only between identities that share the key, which is what DPAPI binds."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="ap-secret-hard-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.original = os.path.join(self.base, "original")
        os.mkdir(self.original)
        self.store = build(self.original, FakeProtector("alice"), FakePolicy())
        self.store.put(REF_A, SECRET_A)
        self.backup = os.path.join(self.base, "backup")
        shutil.copytree(self.original, self.backup)

    def restored(self, identity):
        restored = os.path.join(self.base, f"restored-{identity}")
        shutil.copytree(self.backup, restored)
        return build(restored, FakeProtector(identity), FakePolicy())

    def test_a_restore_under_the_same_identity_reads_the_secret(self):
        self.assertEqual(self.restored("alice").get(REF_A).reveal(), SECRET_A)

    def test_a_restore_under_another_identity_or_machine_is_unreadable_and_never_replaced_by_an_environment_value(self):
        with mock.patch.dict(os.environ, {REF_A: SECRET_B}):
            self.assertEqual(code_of(self, self.restored("bob").get, REF_A), CODE_UNREADABLE)

    def test_nothing_is_restored_automatically(self):
        os.remove(os.path.join(self.original, file_name_for(REF_A)))
        self.assertEqual(code_of(self, self.store.get, REF_A), CODE_NOT_FOUND)
        self.assertFalse(self.store.exists(REF_A))
        self.assertEqual(os.listdir(self.original), [])  # the backup next to it is not consulted

    def test_known_limitation_a_restore_of_an_older_ciphertext_is_not_detected(self):
        """DOCUMENTED, NOT FIXED: a restored older file decrypts and is returned. The store keeps no version or high-water mark outside the directory, so a rollback cannot be detected.
        If detection is ever added this test must change on purpose."""
        self.store.put(REF_A, SECRET_B, replace=True)
        self.assertEqual(self.store.get(REF_A).reveal(), SECRET_B)
        shutil.copy(os.path.join(self.backup, file_name_for(REF_A)), os.path.join(self.original, file_name_for(REF_A)))
        self.assertEqual(self.store.get(REF_A).reveal(), SECRET_A)

    def test_known_limitation_a_deleted_secret_comes_back_from_a_restore(self):
        self.store.delete(REF_A)
        shutil.copy(os.path.join(self.backup, file_name_for(REF_A)), os.path.join(self.original, file_name_for(REF_A)))
        self.assertEqual(self.store.get(REF_A).reveal(), SECRET_A)


@unittest.skipUnless(IS_WINDOWS, "uses the real Windows security APIs")
class WindowsHardeningCase(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="ap-secret-hard-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.directory = os.path.join(self.base, "store")
        prepare_windows_secret_directory(self.directory)
        self.store = WindowsDpapiSecretStore(self.directory)
        self.policy = WindowsDirectoryPolicy()

    def junction(self, link, target):
        result = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, "the test account cannot create a junction")
        self.addCleanup(lambda: os.path.isdir(link) and os.rmdir(link))

    def icacls(self, *args):
        result = subprocess.run(["icacls", *args], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def secret_path(self, reference=REF_A):
        return os.path.join(self.directory, file_name_for(reference))

    def names(self, directory=None):
        return sorted(os.listdir(directory or self.directory))


class WindowsSidTests(WindowsHardeningCase):
    def test_the_operating_system_resolves_aliases_to_canonical_sids(self):
        for text, sid in (("SY", SYSTEM), ("BA", ADMINS), ("LS", LOCAL_SERVICE), ("NS", NETWORK_SERVICE), ("WD", EVERYONE), (USER, USER)):
            self.assertEqual(self.policy.canonical_sid(text), sid, text)
        for text in ("ZZ", "", "S-1-"):
            self.assertIsNone(self.policy.canonical_sid(text), text)

    def test_what_windows_reads_back_for_a_builtin_identity_matches_that_identity(self):
        api = self.policy._api
        for sid in (SYSTEM, LOCAL_SERVICE, NETWORK_SERVICE, "S-1-5-80-123-456-789-1-2"):
            descriptor = ctypes.c_void_p()
            self.assertTrue(api.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(f"O:BAD:P(A;OICI;FA;;;{sid})", 1, ctypes.byref(descriptor), None))
            text = ctypes.c_void_p()
            self.assertTrue(api.advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 0x5, ctypes.byref(text), None))
            sddl = self.policy._take(text.value)
            api.kernel32.LocalFree(descriptor)
            self.assertTrue(descriptor_is_trusted(sddl, sid, self.policy.canonical_sid, owner_required=True), sddl)
            self.assertFalse(descriptor_is_trusted(sddl, USER, self.policy.canonical_sid, owner_required=True), sddl)

    def test_the_current_identity_is_a_supported_account_and_the_directory_acl_names_it(self):
        self.assertTrue(identity_supported(self.policy.identity()))
        self.policy.verify(self.directory)


class WindowsIdentityTests(WindowsHardeningCase):
    def test_a_builtin_service_identity_is_refused_before_anything_is_created(self):
        for sid in (SYSTEM, LOCAL_SERVICE, NETWORK_SERVICE, EVERYONE):
            target = os.path.join(self.base, "fresh")
            before = self.names(self.base)
            with mock.patch.object(WindowsDirectoryPolicy, "current_user_sid", return_value=sid):
                self.assertEqual(code_of(self, prepare_windows_secret_directory, target), CODE_UNAVAILABLE, sid)
                self.assertEqual(code_of(self, WindowsDpapiSecretStore, self.directory), CODE_UNAVAILABLE, sid)
                self.assertEqual(code_of(self, WindowsDirectoryPolicy), CODE_UNAVAILABLE, sid)
            self.assertEqual(self.names(self.base), before, "no directory may be left behind")

    def test_an_approved_builtin_identity_passes_the_identity_check_but_still_has_to_match_the_acl(self):
        with mock.patch.object(WindowsDirectoryPolicy, "current_user_sid", return_value=LOCAL_SERVICE):
            store = WindowsDpapiSecretStore(self.directory, approved_identity_sids=[LOCAL_SERVICE])  # approved: construction works
            self.assertEqual(code_of(self, store.exists, REF_A), CODE_UNAVAILABLE)  # but the directory was prepared for another identity: fail closed
            self.assertEqual(code_of(self, store.put, REF_A, SECRET_A), CODE_UNAVAILABLE)
            self.assertEqual(code_of(self, WindowsDpapiSecretStore, self.directory, approved_identity_sids=[SYSTEM]), CODE_UNAVAILABLE)
        self.assertEqual(self.names(), [])

    def test_an_approval_that_is_not_a_sid_fails_closed(self):
        for bad in (["SY"], "S-1-5-18", [5]):
            self.assertEqual(code_of(self, WindowsDpapiSecretStore, self.directory, approved_identity_sids=bad), CODE_UNAVAILABLE)
            self.assertEqual(code_of(self, prepare_windows_secret_directory, os.path.join(self.base, "x"), approved_identity_sids=bad), CODE_UNAVAILABLE)
        self.assertNotIn("x", self.names(self.base))

    def test_wrong_identity_ciphertext_fails_closed_without_fallback(self):
        directory = os.path.join(self.base, "fake")
        os.mkdir(directory)
        build(directory, FakeProtector("alice"), FakePolicy()).put(REF_A, SECRET_A)
        with mock.patch.dict(os.environ, {REF_A: SECRET_B}):
            self.assertEqual(code_of(self, build(directory, FakeProtector("bob"), FakePolicy()).get, REF_A), CODE_UNREADABLE)


class WindowsPrepareTests(WindowsHardeningCase):
    def test_a_failed_verification_after_creation_removes_the_directory_just_created(self):
        target = os.path.join(self.base, "fresh")
        with mock.patch.object(WindowsDirectoryPolicy, "verify", side_effect=SecretStoreError(CODE_UNAVAILABLE)):
            self.assertEqual(code_of(self, prepare_windows_secret_directory, target), CODE_UNAVAILABLE)
        self.assertFalse(os.path.lexists(target))

    def test_a_missing_parent_is_not_created(self):
        target = os.path.join(self.base, "missing-parent", "store")
        self.assertEqual(code_of(self, prepare_windows_secret_directory, target), CODE_UNAVAILABLE)
        self.assertFalse(os.path.lexists(os.path.dirname(target)))

    def test_an_unsafe_location_is_refused_and_nothing_is_created(self):
        base = self.base
        unsafe = (
            "\\\\?\\" + os.path.join(base, "ext"),
            "\\\\.\\" + os.path.join(base, "dev"),
            os.path.join(base, "stream:name"),
            os.path.join(base, "wild*card"),
            os.path.join(base, "OneDrive - Corp", "store"),
            os.path.join(base, "onedrive", "store"),
        )
        before = self.names(base)
        for path in unsafe:
            self.assertEqual(code_of(self, prepare_windows_secret_directory, path), CODE_UNAVAILABLE, path)
        self.assertEqual(self.names(base), before)

    def test_a_drive_relative_unc_device_and_malformed_location_is_refused_before_any_io(self):
        for path in ("C:relative", "\\\\localhost\\c$\\ap-secret-nope", "\\\\?\\C:\\x", "\\\\.\\C:\\x", "\\x", "C:\\x\\trailing.", "C:\\x\\trailing ", "C:\\x\\a:b", "C:\\x\\a|b", "C:\\Users\\OneDrive - Corp\\store"):
            self.assertEqual(code_of(self, self.policy._location, path), CODE_UNAVAILABLE, path)
        self.policy._location(self.directory)

    def test_a_unc_directory_is_refused_by_the_store_without_touching_the_network(self):
        with mock.patch("os.path.lexists", side_effect=AssertionError("the share was probed")), mock.patch("os.lstat", side_effect=AssertionError("the share was probed")):
            self.assertEqual(code_of(self, WindowsDpapiSecretStore, "\\\\localhost\\c$\\ap-secret-nope"), CODE_UNAVAILABLE)
            self.assertEqual(code_of(self, prepare_windows_secret_directory, "//localhost/c$/ap-secret-nope"), CODE_UNAVAILABLE)

    def test_a_location_that_is_not_a_fixed_acl_volume_is_refused(self):
        with mock.patch.object(module, "volume_is_safe", return_value=False):
            self.assertEqual(code_of(self, self.policy.verify, self.directory), CODE_UNAVAILABLE)
            self.assertEqual(code_of(self, prepare_windows_secret_directory, os.path.join(self.base, "fresh")), CODE_UNAVAILABLE)
        self.assertFalse(os.path.lexists(os.path.join(self.base, "fresh")))
        self.policy.verify(self.directory)

    def test_an_existing_directory_is_adopted_only_when_trusted_and_holding_only_store_files(self):
        self.store.put(REF_A, SECRET_A)
        prepare_windows_secret_directory(self.directory)  # idempotent
        Path(self.directory, "notes.txt").write_text("not ours")
        self.assertEqual(code_of(self, prepare_windows_secret_directory, self.directory), CODE_UNAVAILABLE)
        self.assertEqual(self.store.get(REF_A).reveal(), SECRET_A)

    def test_an_existing_directory_with_inherited_permissions_is_not_adopted_or_changed(self):
        loose = os.path.join(self.base, "loose")
        os.mkdir(loose)
        before = subprocess.run(["icacls", loose], capture_output=True, text=True).stdout
        self.assertEqual(code_of(self, prepare_windows_secret_directory, loose), CODE_UNAVAILABLE)
        self.assertEqual(subprocess.run(["icacls", loose], capture_output=True, text=True).stdout, before)

    def descriptor_of(self, path):
        return self.policy._sddl(lambda out: self.policy._api.advapi32.GetNamedSecurityInfoW(path, 1, 0x5, None, None, None, None, ctypes.byref(out)))

    def test_a_directory_owned_by_someone_else_is_not_trusted(self):
        windows = os.environ.get("SystemRoot", r"C:\Windows")
        owner, _, _ = self.descriptor_of(windows).partition("D:")
        sid = self.policy.identity()
        if self.policy.canonical_sid(owner[2:]) in {sid, SYSTEM, ADMINS}:
            self.skipTest("on this machine the Windows directory is owned by a trusted principal")
        self.assertFalse(descriptor_is_trusted(f"{owner}D:P(A;OICI;FA;;;{sid})", sid, self.policy.canonical_sid, owner_required=True), "the foreign owner alone must make it untrusted")
        self.assertTrue(descriptor_is_trusted(self.descriptor_of(self.directory), sid, self.policy.canonical_sid, owner_required=True))
        self.assertEqual(code_of(self, prepare_windows_secret_directory, windows), CODE_UNAVAILABLE)
        self.assertEqual(code_of(self, self.policy.verify, windows), CODE_UNAVAILABLE)


class WindowsJunctionTests(WindowsHardeningCase):
    def test_a_junction_in_a_parent_component_is_refused_by_the_store_and_by_prepare(self):
        parent = os.path.join(self.base, "parent")
        os.mkdir(parent)
        prepare_windows_secret_directory(os.path.join(parent, "inner"))
        link = os.path.join(self.base, "link")
        self.junction(link, parent)
        through = os.path.join(link, "inner")
        self.assertTrue(os.path.isdir(through))
        store = WindowsDpapiSecretStore(through)
        for call in (lambda: store.exists(REF_A), lambda: store.put(REF_A, SECRET_A), lambda: store.get(REF_A), lambda: store.delete(REF_A)):
            self.assertEqual(code_of(self, call), CODE_UNAVAILABLE)
        self.assertEqual(code_of(self, prepare_windows_secret_directory, through), CODE_UNAVAILABLE)
        self.assertEqual(code_of(self, prepare_windows_secret_directory, os.path.join(link, "new")), CODE_UNAVAILABLE)
        self.assertEqual(os.listdir(os.path.join(parent, "inner")), [])
        self.assertFalse(os.path.lexists(os.path.join(parent, "new")))

    def test_a_junction_as_the_store_directory_is_refused(self):
        link = os.path.join(self.base, "leaf-link")
        self.junction(link, self.directory)
        self.assertEqual(code_of(self, WindowsDpapiSecretStore(link).exists, REF_A), CODE_UNAVAILABLE)
        self.assertEqual(code_of(self, prepare_windows_secret_directory, link), CODE_UNAVAILABLE)

    def test_a_junction_in_place_of_a_secret_file_is_refused(self):
        outside = os.path.join(self.base, "outside")
        os.mkdir(outside)
        self.junction(self.secret_path(REF_B), outside)
        self.assertEqual(code_of(self, self.store.exists, REF_B), CODE_UNAVAILABLE)
        self.assertEqual(code_of(self, self.store.get, REF_B), CODE_UNAVAILABLE)
        self.assertEqual(code_of(self, self.store.delete, REF_B), CODE_UNAVAILABLE)
        self.assertEqual(os.listdir(outside), [])

    def test_a_junction_in_place_of_the_lock_file_is_refused(self):
        fresh = os.path.join(self.base, "fresh")
        prepare_windows_secret_directory(fresh)
        outside = os.path.join(self.base, "outside")
        os.mkdir(outside)
        self.junction(os.path.join(fresh, LOCK_FILE_NAME), outside)
        self.assertEqual(code_of(self, WindowsDpapiSecretStore(fresh).exists, REF_A), CODE_UNAVAILABLE)
        self.assertEqual(os.listdir(outside), [])


class WindowsFileSecurityTests(WindowsHardeningCase):
    def tamper(self, reference=REF_A):
        self.icacls(self.secret_path(reference), "/grant", f"*{EVERYONE}:R")

    def test_a_secret_file_with_an_extra_grant_is_refused_before_it_is_read_and_a_rotation_heals_it(self):
        self.store.put(REF_A, SECRET_A)
        self.tamper()
        with mock.patch.object(module.WindowsDpapiProtector, "unprotect", side_effect=AssertionError("decrypted")) as unprotect:
            self.assertEqual(code_of(self, self.store.get, REF_A), CODE_UNAVAILABLE)
            unprotect.assert_not_called()
        self.assertTrue(self.store.exists(REF_A))
        self.store.put(REF_A, SECRET_B, replace=True)  # the new file inherits the directory ACL again
        self.assertEqual(self.store.get(REF_A).reveal(), SECRET_B)

    def test_a_deny_entry_or_a_disabled_inheritance_with_a_foreign_grant_is_refused(self):
        self.store.put(REF_A, SECRET_A)
        self.icacls(self.secret_path(), "/deny", f"*{EVERYONE}:W")
        self.assertEqual(code_of(self, self.store.get, REF_A), CODE_UNAVAILABLE)
        self.store.delete(REF_A)  # a tampered file can still be removed and the reference created again
        self.store.put(REF_A, SECRET_A)
        self.icacls(self.secret_path(), "/inheritance:r", "/grant:r", f"*{EVERYONE}:F")
        self.assertEqual(code_of(self, self.store.get, REF_A), CODE_UNAVAILABLE)

    def test_a_file_with_the_expected_acl_is_read_and_an_unrelated_tampered_file_does_not_affect_it(self):
        self.store.put(REF_A, SECRET_A)
        self.store.put(REF_B, SECRET_B)
        self.tamper(REF_B)
        self.assertEqual(self.store.get(REF_A).reveal(), SECRET_A)
        self.assertEqual(code_of(self, self.store.get, REF_B), CODE_UNAVAILABLE)

    def test_a_hard_link_to_a_real_secret_file_is_refused_and_leaves_the_other_name_alone(self):
        self.store.put(REF_A, SECRET_A)
        elsewhere = os.path.join(self.base, "copy.bin")
        os.link(self.secret_path(), elsewhere)
        for call in (lambda: self.store.get(REF_A), lambda: self.store.exists(REF_A), lambda: self.store.delete(REF_A), lambda: self.store.put(REF_A, SECRET_B, replace=True)):
            self.assertEqual(code_of(self, call), CODE_UNAVAILABLE)
        self.assertEqual(Path(elsewhere).read_bytes(), Path(self.secret_path()).read_bytes())

    def test_a_foreign_file_hard_linked_in_is_refused(self):
        foreign = os.path.join(self.base, "foreign.bin")
        Path(foreign).write_bytes(module.FORMAT_TAG + b"x" * 80)
        os.link(foreign, self.secret_path(REF_B))
        self.assertEqual(code_of(self, self.store.get, REF_B), CODE_UNAVAILABLE)

    def test_file_errors_reveal_no_secret_and_no_path(self):
        self.store.put(REF_A, SECRET_A)
        with mock.patch("os.read", side_effect=OSError(5, f"{self.directory} {SECRET_A}")):
            with self.assertRaises(SecretStoreError) as caught:
                self.store.get(REF_A)
        text = str(caught.exception) + repr(caught.exception) + "".join(traceback.format_exception(caught.exception))
        for forbidden in (SECRET_A, self.directory):
            self.assertNotIn(forbidden, text)
        self.assertIsNone(caught.exception.__context__)


class WindowsRestoreTests(WindowsHardeningCase):
    def test_a_restore_that_lost_the_directory_acl_is_refused_and_not_adopted(self):
        self.store.put(REF_A, SECRET_A)
        restored = os.path.join(self.base, "restored")
        shutil.copytree(self.directory, restored)  # a plain file copy: the new directory has inherited, not protected, permissions
        self.assertEqual(code_of(self, WindowsDpapiSecretStore(restored).get, REF_A), CODE_UNAVAILABLE)
        self.assertEqual(code_of(self, prepare_windows_secret_directory, restored), CODE_UNAVAILABLE)

    def test_a_restore_into_a_freshly_prepared_directory_reads_back_under_the_same_account(self):
        self.store.put(REF_A, SECRET_A)
        fresh = os.path.join(self.base, "fresh")
        prepare_windows_secret_directory(fresh)
        for name in os.listdir(self.directory):
            if name != LOCK_FILE_NAME:
                shutil.copy(os.path.join(self.directory, name), os.path.join(fresh, name))  # a copy inherits the new directory's ACL
        self.assertEqual(WindowsDpapiSecretStore(fresh).get(REF_A).reveal(), SECRET_A)

    def test_known_limitation_a_restored_older_real_ciphertext_is_not_detected(self):
        self.store.put(REF_A, SECRET_A)
        old = Path(self.secret_path()).read_bytes()
        self.store.put(REF_A, SECRET_B, replace=True)
        self.assertEqual(self.store.get(REF_A).reveal(), SECRET_B)
        Path(self.secret_path()).unlink()
        Path(self.secret_path()).write_bytes(old)
        self.assertEqual(self.store.get(REF_A).reveal(), SECRET_A)

    def test_a_blob_of_another_reference_or_a_foreign_copy_does_not_decrypt(self):
        self.store.put(REF_A, SECRET_A)
        self.store.put(REF_B, SECRET_B)
        os.replace(self.secret_path(REF_A), self.secret_path(REF_A) + ".keep")
        shutil.copy(self.secret_path(REF_B), self.secret_path(REF_A))
        self.assertEqual(code_of(self, self.store.get, REF_A), CODE_UNREADABLE)


if __name__ == "__main__":
    unittest.main()
