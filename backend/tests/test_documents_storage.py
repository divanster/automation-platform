"""DB-free tests of the managed document storage (DOC-1): the real ``DocumentStore`` on a real temporary directory behind a recording policy.

What is proven here: where bytes go (only into the protected directory, never the system temporary directory), the size and structure limits, atomic installation, no overwrite, no path from any
user value, link and layout checks, and cleanup after every failure. The platform's real directory policies are tested at the end (POSIX modes on POSIX; the Windows policy end to end on Windows).
"""

import hashlib
import os
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock

from django.test import SimpleTestCase

from apps.documents import storage
from apps.documents.errors import (
    CODE_DISABLED,
    CODE_OBJECT_MISSING,
    CODE_UNAVAILABLE,
    REASON_EMPTY,
    REASON_LENGTH_MISMATCH,
    REASON_MALFORMED_PDF,
    REASON_NOT_A_PDF,
    REASON_TOO_LARGE,
    REASON_TRUNCATED_PDF,
    DocumentRejectedError,
    DocumentStorageError,
)
from apps.documents.rules import new_storage_key
from apps.documents.storage import DocumentStore, PosixDirectoryPolicy, checked_directory, prepare_directory
from tests.documents_support import VALID_PDF, PermissivePolicy, Reader, make_pdf, temporary_root, temporary_store, tree_listing


def rejection(function, *args, **kwargs):
    try:
        function(*args, **kwargs)
    except DocumentRejectedError as error:
        return error.reason
    return None


def storage_code(function, *args, **kwargs):
    try:
        function(*args, **kwargs)
    except DocumentStorageError as error:
        return error.code
    return None


def ingest(store, data, declared=None, **reader):
    return store.ingest(Reader(data, **reader).read, len(data) if declared is None else declared)


class IngestTests(SimpleTestCase):
    def test_a_valid_pdf_is_stored_under_a_random_key_with_its_hash_and_size(self):
        with temporary_store() as (store, root):
            stored = ingest(store, VALID_PDF)
            self.assertEqual((stored.size_bytes, stored.sha256), (len(VALID_PDF), hashlib.sha256(VALID_PDF).hexdigest()))
            shard, name = stored.key.split("/")
            path = os.path.join(root, "objects", shard, name)
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), VALID_PDF)
            self.assertEqual(tree_listing(root), [os.path.join("objects", shard, name)])  # nothing left in incoming/

    def test_short_reads_are_assembled(self):
        with temporary_store() as (store, _):
            data = make_pdf(padding=200_000)
            stored = ingest(store, data, step=7)
            self.assertEqual(stored.sha256, hashlib.sha256(data).hexdigest())

    def test_a_large_file_is_received_in_chunks_not_all_at_once(self):
        with temporary_store() as (store, _):
            data = make_pdf(padding=3 * 1024 * 1024)
            reader = Reader(data)
            store.ingest(reader.read, len(data))
            self.assertGreater(reader.calls, 40)  # 64 KiB chunks
            self.assertLessEqual(storage.CHUNK_BYTES, 64 * 1024)

    def test_two_uploads_of_the_same_bytes_are_two_objects(self):
        with temporary_store() as (store, root):
            first, second = ingest(store, VALID_PDF), ingest(store, VALID_PDF)
            self.assertNotEqual(first.key, second.key)
            self.assertEqual(len(tree_listing(root)), 2)

    def test_the_key_and_the_path_never_contain_anything_from_the_client(self):
        with temporary_store() as (store, root):
            stored = ingest(store, make_pdf(marker=b"client-name-and-content"))
            for part in tree_listing(root) + [stored.key]:
                self.assertNotIn("client", part)
                self.assertRegex(part.replace("\\", "/"), r"^(objects/)?[0-9a-f]{2}/[0-9a-f]{32}$")

    def test_the_bytes_never_touch_the_system_temporary_directory(self):
        def refused(*args, **kwargs):
            raise AssertionError("a temporary file was requested")

        with temporary_store() as (store, root):
            with (
                mock.patch.object(tempfile, "mkstemp", refused),
                mock.patch.object(tempfile, "NamedTemporaryFile", refused),
                mock.patch.object(tempfile, "TemporaryFile", refused),
                mock.patch.object(tempfile, "SpooledTemporaryFile", refused),
                mock.patch.object(tempfile, "mkdtemp", refused),
            ):
                ingest(store, make_pdf(padding=500_000))
            partials_seen = []
            original = os.open

            def spy(path, flags, *args, **kwargs):
                if str(path).endswith(storage.PARTIAL_SUFFIX):
                    partials_seen.append(os.path.dirname(str(path)))
                return original(path, flags, *args, **kwargs)

            with mock.patch.object(os, "open", spy):
                ingest(store, VALID_PDF)
            self.assertEqual(set(partials_seen), {os.path.join(root, "incoming")})  # the only place an upload is staged

    def test_the_module_does_not_use_the_tempfile_module(self):
        import ast

        tree = ast.parse(open(storage.__file__, encoding="utf-8").read())
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        self.assertNotIn("tempfile", imported)


class RejectionTests(SimpleTestCase):
    def assertNothingLeft(self, root):
        self.assertEqual(tree_listing(root), [])

    def test_a_body_larger_than_the_limit_stops_at_the_limit_and_leaves_nothing(self):
        with temporary_store(max_bytes=2048) as (store, root):
            data = make_pdf(padding=50_000)
            reader = Reader(data)
            self.assertEqual(rejection(store.ingest, reader.read, len(data)), REASON_TOO_LARGE)
            self.assertLess(reader.position, 2048 + storage.CHUNK_BYTES)  # it did not read the rest
            self.assertNothingLeft(root)

    def test_the_limit_is_exact(self):
        data = VALID_PDF
        with temporary_store(max_bytes=max(len(data), 1024)) as (store, _):
            self.assertTrue(ingest(store, data).key)
        with temporary_store(max_bytes=1024) as (store, root):
            big = make_pdf(padding=1100)
            self.assertEqual(rejection(ingest, store, big), REASON_TOO_LARGE)
            self.assertNothingLeft(root)

    def test_fewer_bytes_than_declared_is_a_length_mismatch(self):
        with temporary_store() as (store, root):
            self.assertEqual(rejection(ingest, store, VALID_PDF, declared=len(VALID_PDF) + 10), REASON_LENGTH_MISMATCH)
            self.assertNothingLeft(root)

    def test_more_bytes_than_declared_is_a_length_mismatch(self):
        with temporary_store() as (store, root):
            self.assertEqual(rejection(ingest, store, VALID_PDF, declared=len(VALID_PDF) - 10), REASON_LENGTH_MISMATCH)
            self.assertNothingLeft(root)

    def test_a_stream_that_breaks_is_a_length_mismatch(self):
        with temporary_store() as (store, root):
            self.assertEqual(rejection(ingest, store, make_pdf(padding=300_000), step=1000, fail_after=100_000), REASON_LENGTH_MISMATCH)
            self.assertNothingLeft(root)

    def test_garbage_is_refused_after_its_first_bytes_without_reading_the_rest(self):
        with temporary_store() as (store, root):
            data = b"This is plain text, not a PDF. " * 100_000
            reader = Reader(data)
            self.assertEqual(rejection(store.ingest, reader.read, len(data)), REASON_NOT_A_PDF)
            self.assertLessEqual(reader.position, storage.CHUNK_BYTES)
            self.assertNothingLeft(root)

    def test_a_tiny_body_and_an_empty_body_are_refused(self):
        with temporary_store() as (store, root):
            self.assertEqual(rejection(ingest, store, b"%PDF"), REASON_NOT_A_PDF)
            self.assertEqual(rejection(ingest, store, b""), REASON_EMPTY)
            self.assertNothingLeft(root)

    def test_a_truncated_pdf_and_a_malformed_one_are_refused_and_leave_nothing(self):
        with temporary_store() as (store, root):
            self.assertEqual(rejection(ingest, store, VALID_PDF[: len(VALID_PDF) // 2]), REASON_TRUNCATED_PDF)
            self.assertEqual(rejection(ingest, store, VALID_PDF.replace(b"trailer", b"trailxx")), REASON_MALFORMED_PDF)
            self.assertNothingLeft(root)

    def test_other_file_types_with_a_pdf_name_are_refused_by_content(self):
        samples = (b"PK\x03\x04" + b"\x00" * 500, b"\x89PNG\r\n\x1a\n" + b"\x00" * 500, b"MZ" + b"\x90" * 500, b"#!/bin/sh\nrm -rf /\n" * 20, b"<html><script>alert(1)</script></html>", b"\xff\xd8\xff\xe0" + b"\x00" * 500)
        with temporary_store() as (store, root):
            for data in samples:
                with self.subTest(prefix=data[:6]):
                    self.assertEqual(rejection(ingest, store, data), REASON_NOT_A_PDF)
            self.assertNothingLeft(root)

    def test_a_polyglot_with_bytes_before_the_header_is_refused(self):
        with temporary_store() as (store, root):
            self.assertEqual(rejection(ingest, store, b"PK\x03\x04" + VALID_PDF), REASON_NOT_A_PDF)
            self.assertNothingLeft(root)

    def test_a_failing_policy_stops_the_upload_before_a_byte_is_written(self):
        with temporary_store(policy=PermissivePolicy()) as (store, root):
            store._policy.fail_on = "verify_file"
            reader = Reader(VALID_PDF)
            self.assertEqual(storage_code(store.ingest, reader.read, len(VALID_PDF)), CODE_UNAVAILABLE)
            self.assertEqual((reader.calls, tree_listing(root)), (0, []))
            store._policy.fail_on = "verify"
            self.assertEqual(storage_code(ingest, store, VALID_PDF), CODE_UNAVAILABLE)

    def test_a_write_failure_is_a_storage_error_and_leaves_nothing(self):
        with temporary_store() as (store, root):
            with mock.patch.object(os, "write", side_effect=OSError("disk full")):
                self.assertEqual(storage_code(ingest, store, VALID_PDF), CODE_UNAVAILABLE)
            self.assertNothingLeft(root)

    def test_an_interruption_during_receive_cleans_up_and_propagates(self):
        with temporary_store() as (store, root):
            reader = Reader(make_pdf(padding=300_000), step=1000)

            def interrupted(size):
                if reader.position > 50_000:
                    raise KeyboardInterrupt()
                return reader.read(size)

            with self.assertRaises(KeyboardInterrupt):
                store.ingest(interrupted, len(reader.data))
            self.assertNothingLeft(root)

    def test_an_error_message_holds_no_path_name_or_content(self):
        with temporary_store() as (store, root):
            for data in (b"x" * 100, VALID_PDF[:100]):
                try:
                    ingest(store, data)
                except DocumentRejectedError as error:
                    text = str(error) + repr(error.args)
                    self.assertNotIn(root, text)


class InstallTests(SimpleTestCase):
    def test_an_existing_object_is_never_overwritten_and_a_collision_picks_another_key(self):
        with temporary_store() as (store, root):
            first = ingest(store, VALID_PDF)
            fresh = new_storage_key()
            sequence = iter([new_storage_key(), first.key, fresh])  # the staging name, a key that collides with the first object, a fresh key
            with mock.patch.object(storage, "new_storage_key", lambda: next(sequence)):
                second = ingest(store, make_pdf(marker=b"different"))
            self.assertEqual(second.key, fresh)
            with open(os.path.join(root, "objects", *first.key.split("/")), "rb") as handle:
                self.assertEqual(handle.read(), VALID_PDF)  # the first object is untouched

    def test_if_every_attempt_collides_the_upload_fails_closed_and_leaves_nothing_new(self):
        with temporary_store() as (store, root):
            first = ingest(store, VALID_PDF)
            with mock.patch.object(storage, "new_storage_key", return_value=first.key):
                self.assertEqual(storage_code(ingest, store, make_pdf(marker=b"x")), CODE_UNAVAILABLE)
            self.assertEqual(len(tree_listing(root)), 1)

    def test_an_object_appears_whole_or_not_at_all(self):
        with temporary_store() as (store, root):
            seen = []
            real_link = os.link

            def spy(source, target, *args, **kwargs):
                with open(source, "rb") as handle:
                    seen.append(handle.read() == VALID_PDF)  # the file is complete and flushed BEFORE it gets its final name
                self.assertFalse(os.path.exists(target))
                return real_link(source, target, *args, **kwargs)

            with mock.patch.object(os, "link", spy):
                ingest(store, VALID_PDF)
            self.assertEqual(seen, [True])

    def test_a_failure_while_installing_removes_the_staged_file_and_any_new_name(self):
        with temporary_store() as (store, root):
            real_unlink = os.unlink
            state = {"first": True}

            def flaky(path, *args, **kwargs):
                if state["first"] and str(path).endswith(storage.PARTIAL_SUFFIX):
                    state["first"] = False
                    raise OSError("busy")  # the removal of the staged name right after the link
                return real_unlink(path, *args, **kwargs)

            with mock.patch.object(os, "unlink", flaky):
                self.assertEqual(storage_code(ingest, store, VALID_PDF), CODE_UNAVAILABLE)
            self.assertEqual(tree_listing(root), [])  # neither the final name nor the staged file remains

    def test_concurrent_uploads_all_succeed_with_distinct_whole_objects(self):
        with temporary_store() as (store, root):
            payloads = [make_pdf(marker=f"upload-{n}".encode(), padding=20_000 + n) for n in range(12)]
            results, errors = {}, []
            gate = threading.Barrier(len(payloads))

            def work(number):
                try:
                    gate.wait(timeout=10)
                    results[number] = ingest(store, payloads[number], step=4096)
                except BaseException as error:  # noqa: BLE001
                    errors.append(error)

            threads = [threading.Thread(target=work, args=(n,)) for n in range(len(payloads))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            self.assertEqual(errors, [])
            self.assertEqual(len({stored.key for stored in results.values()}), len(payloads))
            for number, stored in results.items():
                with open(os.path.join(root, "objects", *stored.key.split("/")), "rb") as handle:
                    self.assertEqual(hashlib.sha256(handle.read()).hexdigest(), stored.sha256)
                self.assertEqual(stored.sha256, hashlib.sha256(payloads[number]).hexdigest())
            self.assertEqual(len(tree_listing(root)), len(payloads))  # and nothing left in incoming/


class PathBoundaryTests(SimpleTestCase):
    HOSTILE = (
        "../x", "../../etc/passwd", "..\\..\\x", "/etc/passwd", "C:\\Windows\\win.ini", "ab/../../x", "ab/..", "../ab/" + "c" * 32, "ab/" + "c" * 32 + "/..", "ab/" + "c" * 32 + "\n", "ab\\" + "c" * 32,
        "AB/" + "C" * 32, "", None, 5, "ab/" + "c" * 31, "ab/" + "c" * 32 + ".pdf", "ab//" + "c" * 32, "\\\\server\\share\\x", "ab/c:stream",
    )

    def test_no_hostile_key_resolves_to_a_path(self):
        with temporary_store() as (store, root):
            for key in self.HOSTILE:
                with self.subTest(key=key):
                    self.assertEqual(storage_code(store._path_for, key), CODE_UNAVAILABLE)
                    self.assertEqual(storage_code(store.open, key, 1), CODE_UNAVAILABLE)
                    self.assertEqual(storage_code(store.delete, key), CODE_UNAVAILABLE)

    def test_a_valid_key_resolves_inside_the_objects_directory_only(self):
        with temporary_store() as (store, root):
            for _ in range(50):
                key = new_storage_key()
                path = store._path_for(key)
                self.assertEqual(os.path.dirname(os.path.dirname(path)), os.path.join(root, "objects"))

    def test_a_file_outside_the_root_cannot_be_read_or_deleted_through_the_store(self):
        with temporary_store() as (store, root):
            outside = os.path.join(os.path.dirname(root), "outside.pdf")
            with open(outside, "wb") as handle:
                handle.write(VALID_PDF)
            for key in self.HOSTILE:
                try:
                    store.delete(key)
                    store.open(key, len(VALID_PDF)).close()
                except DocumentStorageError:
                    pass
            self.assertTrue(os.path.exists(outside))

    def test_an_object_that_is_a_symbolic_link_is_not_followed(self):
        with temporary_store() as (store, root):
            outside = os.path.join(os.path.dirname(root), "outside.pdf")
            with open(outside, "wb") as handle:
                handle.write(VALID_PDF)
            key = new_storage_key()
            shard = os.path.join(root, "objects", key.split("/")[0])
            os.mkdir(shard)
            try:
                os.symlink(outside, os.path.join(shard, key.split("/")[1]))
            except (OSError, NotImplementedError, AttributeError):
                self.skipTest("symbolic links cannot be created here")
            self.assertIn(storage_code(store.open, key, len(VALID_PDF)), (CODE_UNAVAILABLE, CODE_OBJECT_MISSING))

    def test_a_hard_linked_object_is_refused(self):
        with temporary_store() as (store, root):
            outside = os.path.join(os.path.dirname(root), "outside.pdf")
            with open(outside, "wb") as handle:
                handle.write(VALID_PDF)
            key = new_storage_key()
            shard = os.path.join(root, "objects", key.split("/")[0])
            os.mkdir(shard)
            try:
                os.link(outside, os.path.join(shard, key.split("/")[1]))
            except OSError:
                self.skipTest("hard links cannot be created here")
            self.assertEqual(storage_code(store.open, key, len(VALID_PDF)), CODE_UNAVAILABLE)


class ReadTests(SimpleTestCase):
    def test_a_stored_object_is_streamed_back_exactly_in_chunks(self):
        with temporary_store() as (store, _):
            data = make_pdf(padding=300_000)
            stored = ingest(store, data)
            stream = store.open(stored.key, stored.size_bytes)
            chunks = list(stream)
            stream.close()
            self.assertEqual(b"".join(chunks), data)
            self.assertTrue(all(len(chunk) <= storage.CHUNK_BYTES for chunk in chunks))
            self.assertGreater(len(chunks), 3)

    def test_a_stream_can_be_closed_without_being_read(self):
        with temporary_store() as (store, _):
            stored = ingest(store, VALID_PDF)
            stream = store.open(stored.key, stored.size_bytes)
            handle = stream._handle
            stream.close()
            self.assertTrue(handle.closed)
            # and the object can then be removed (on Windows an open handle would refuse it)
            self.assertTrue(store.delete(stored.key))

    def test_a_missing_object_or_a_size_that_does_not_match_is_object_missing(self):
        with temporary_store() as (store, root):
            stored = ingest(store, VALID_PDF)
            self.assertEqual(storage_code(store.open, new_storage_key(), 10), CODE_OBJECT_MISSING)
            self.assertEqual(storage_code(store.open, stored.key, stored.size_bytes + 1), CODE_OBJECT_MISSING)
            self.assertEqual(storage_code(store.open, stored.key, stored.size_bytes - 1), CODE_OBJECT_MISSING)
            with open(os.path.join(root, "objects", *stored.key.split("/")), "ab") as handle:
                handle.write(b"tampered")
            self.assertEqual(storage_code(store.open, stored.key, stored.size_bytes), CODE_OBJECT_MISSING)

    def test_the_handle_is_not_leaked_when_the_policy_refuses_the_file(self):
        with temporary_store() as (store, _):
            stored = ingest(store, VALID_PDF)
            store._policy.fail_on = "verify_file"
            self.assertEqual(storage_code(store.open, stored.key, stored.size_bytes), CODE_UNAVAILABLE)
            store._policy.fail_on = None
            self.assertTrue(store.delete(stored.key))  # nothing holds it open

    def test_delete_removes_and_reports_whether_there_was_one(self):
        with temporary_store() as (store, root):
            stored = ingest(store, VALID_PDF)
            self.assertTrue(store.delete(stored.key))
            self.assertFalse(store.delete(stored.key))
            self.assertEqual(tree_listing(root), [])


class LayoutAndLocationTests(SimpleTestCase):
    def test_an_unconfigured_or_unsafe_location_is_refused(self):
        with temporary_root() as root:
            for value in (None, "", "relative/dir", ".", "documents", "\x00", 5, b"x"):
                with self.subTest(value=value):
                    self.assertEqual(storage_code(checked_directory, value), CODE_UNAVAILABLE)
            self.assertEqual(storage_code(checked_directory, root + "\x00x"), CODE_UNAVAILABLE)

    def test_a_directory_inside_a_git_working_tree_is_refused(self):
        with temporary_root() as root:
            os.makedirs(os.path.join(os.path.dirname(root), "repo", ".git"))
            self.assertEqual(storage_code(checked_directory, os.path.join(os.path.dirname(root), "repo", "documents")), CODE_UNAVAILABLE)
            self.assertEqual(storage_code(checked_directory, os.path.join(os.path.dirname(root), "repo")), CODE_UNAVAILABLE)

    def test_a_directory_inside_or_equal_to_a_forbidden_root_is_refused(self):
        with temporary_root() as root:
            parent = os.path.dirname(root)
            for candidate in (parent, root, os.path.join(root, "deeper")):
                with self.subTest(candidate=candidate):
                    self.assertEqual(storage_code(checked_directory, candidate, [parent]), CODE_UNAVAILABLE)
            self.assertEqual(checked_directory(root, [os.path.join(parent, "other")]), root)
            self.assertEqual(checked_directory(root, ["", None]), root)  # an unset static or media root forbids nothing

    def test_the_forbidden_comparison_is_by_path_component_not_by_prefix(self):
        with temporary_root() as root:
            sibling = os.path.join(os.path.dirname(root), "documents-extra")
            self.assertEqual(checked_directory(sibling, [root]), sibling)

    @unittest.skipUnless(sys.platform == "win32", "UNC paths are a Windows concern")
    def test_a_network_path_is_refused_before_any_file_system_call(self):
        with mock.patch.object(os, "lstat", side_effect=AssertionError("touched the file system")):
            self.assertEqual(storage_code(checked_directory, "\\\\server\\share\\documents"), CODE_UNAVAILABLE)

    def test_prepare_creates_the_layout_and_is_repeatable_and_never_changes_an_existing_directory(self):
        policy = PermissivePolicy()
        with temporary_root() as root:
            prepare_directory(root, policy)
            self.assertEqual(sorted(os.listdir(root)), ["incoming", "objects"])
            self.assertIn("create", policy.calls)
            with open(os.path.join(root, "objects", "keep.txt"), "w") as handle:
                handle.write("kept")
            before = list(policy.calls)
            prepare_directory(root, policy)
            self.assertNotIn("create", policy.calls[len(before) :])
            self.assertTrue(os.path.exists(os.path.join(root, "objects", "keep.txt")))

    def test_prepare_needs_an_existing_parent_and_a_policy_that_approves(self):
        with temporary_root() as root:
            parent = os.path.dirname(root)
            self.assertEqual(storage_code(prepare_directory, os.path.join(parent, "missing-parent", "documents"), PermissivePolicy()), CODE_UNAVAILABLE)
            self.assertFalse(os.path.exists(os.path.join(parent, "missing-parent")))
            existing = os.path.join(parent, "pre")
            os.mkdir(existing)
            self.assertEqual(storage_code(prepare_directory, existing, PermissivePolicy(fail_on="verify")), CODE_UNAVAILABLE)
            self.assertEqual(os.listdir(existing), [])  # an existing directory the policy refuses is left exactly as it was
            self.assertEqual(storage_code(prepare_directory, root, object()), CODE_UNAVAILABLE)

    def test_a_store_needs_a_valid_limit_a_policy_and_a_prepared_layout(self):
        with temporary_root() as root:
            self.assertEqual(storage_code(DocumentStore, root, PermissivePolicy(), max_bytes=10), CODE_UNAVAILABLE)
            self.assertEqual(storage_code(DocumentStore, root, object(), max_bytes=2048), CODE_UNAVAILABLE)
            os.mkdir(root)
            self.assertEqual(storage_code(DocumentStore, root, PermissivePolicy(), max_bytes=2048), CODE_UNAVAILABLE)  # incoming/ and objects/ are missing
            prepare_directory(root, PermissivePolicy())
            DocumentStore(root, PermissivePolicy(), max_bytes=2048)

    def test_a_swapped_layout_is_noticed_on_the_next_operation(self):
        with temporary_store() as (store, root):
            ingest(store, VALID_PDF)
            os.rename(os.path.join(root, "objects"), os.path.join(root, "objects-moved"))
            self.assertEqual(storage_code(ingest, store, VALID_PDF), CODE_UNAVAILABLE)

    def test_the_policy_is_consulted_on_every_operation(self):
        with temporary_store() as (store, _):
            store._policy.calls.clear()
            stored = ingest(store, VALID_PDF)
            store.open(stored.key, stored.size_bytes).close()
            store.delete(stored.key)
            self.assertGreaterEqual(store._policy.calls.count("verify"), 3)
            self.assertGreaterEqual(store._policy.calls.count("verify_file"), 2)


class MakeSureNothingIsServedTests(SimpleTestCase):
    def test_there_is_no_media_or_static_root_for_documents_to_hide_in(self):
        import re
        from pathlib import Path

        from django.conf import settings

        self.assertEqual(settings.MEDIA_ROOT, "")  # Django's default: nothing is configured to serve uploads
        self.assertFalse(getattr(settings, "STATIC_ROOT", None))
        for module in ("base", "development", "production"):
            source = (Path(settings.BASE_DIR) / "config" / "settings" / f"{module}.py").read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"^(MEDIA_ROOT|MEDIA_URL|STATIC_ROOT|STORAGES|DEFAULT_FILE_STORAGE)\b", source, re.M), module)
        self.assertEqual(settings.FILE_UPLOAD_HANDLERS, ["django.core.files.uploadhandler.MemoryFileUploadHandler"])
        self.assertLessEqual(settings.FILE_UPLOAD_MAX_MEMORY_SIZE, 256 * 1024)


@unittest.skipUnless(os.name == "posix", "POSIX permission modes")
class PosixPolicyTests(SimpleTestCase):
    def test_the_root_must_be_private_to_this_process(self):
        policy = PosixDirectoryPolicy()
        with temporary_root() as root:
            policy.create(root)
            self.assertEqual(stat.S_IMODE(os.lstat(root).st_mode), 0o700)
            policy.verify(root)
            for mode in (0o750, 0o705, 0o777, 0o770):
                os.chmod(root, mode)
                with self.subTest(mode=oct(mode)):
                    self.assertEqual(storage_code(policy.verify, root), CODE_UNAVAILABLE)
            os.chmod(root, 0o700)

    def test_a_file_must_be_private_to_this_process(self):
        policy = PosixDirectoryPolicy()
        with temporary_root() as root:
            policy.create(root)
            path = os.path.join(root, "f")
            for mode, ok in ((0o600, True), (0o640, False), (0o604, False), (0o666, False)):
                descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    os.fchmod(descriptor, mode)
                    with self.subTest(mode=oct(mode)):
                        self.assertEqual(storage_code(policy.verify_file, descriptor) is None, ok)
                finally:
                    os.close(descriptor)
                    os.unlink(path)

    def test_files_and_directories_the_store_creates_are_private(self):
        with temporary_store(policy=PosixDirectoryPolicy()) as (store, root):
            stored = ingest(store, VALID_PDF)
            for path in (root, os.path.join(root, "incoming"), os.path.join(root, "objects"), os.path.join(root, "objects", stored.key.split("/")[0])):
                self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode) & 0o077, 0, path)
            self.assertEqual(stat.S_IMODE(os.lstat(os.path.join(root, "objects", *stored.key.split("/"))).st_mode), 0o600)

    def test_a_loosened_root_stops_the_store(self):
        with temporary_store(policy=PosixDirectoryPolicy()) as (store, root):
            os.chmod(root, 0o755)
            self.assertEqual(storage_code(ingest, store, VALID_PDF), CODE_UNAVAILABLE)
            os.chmod(root, 0o700)


class PolicyTypeTests(SimpleTestCase):
    def test_disabled_is_its_own_code(self):
        self.assertNotEqual(CODE_DISABLED, CODE_UNAVAILABLE)
        self.assertEqual(DocumentStorageError("anything else").code, CODE_UNAVAILABLE)
