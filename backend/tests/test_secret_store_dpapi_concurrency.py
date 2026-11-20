"""Real multiprocess tests of the Windows DPAPI secret store's cross-process lock (AI-SEC-1c).

Windows only. Each test uses a dedicated temporary directory prepared by ``prepare_windows_secret_directory``, synthetic credentials and child Python processes
(``tests/secret_store_workers.py``); it touches no real credential, no environment secret, no database and no network. Children print counters only; every test
also asserts that no synthetic secret appears in any child's output or on disk in plaintext."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from apps.secret_store import (
    CODE_ALREADY_EXISTS,
    CODE_BUSY,
    CODE_NOT_FOUND,
    SecretStoreError,
)
from config.secret_store_dpapi import (
    LOCK_FILE_NAME,
    FORMAT_TAG,
    InProcessLocker,
    WindowsDpapiProtector,
    WindowsDpapiSecretStore,
    WindowsFileLocker,
    entropy_for,
    file_name_for,
    prepare_windows_secret_directory,
)
from tests.secret_store_workers import PREFIX, REFERENCE, VALID, value

BACKEND = Path(__file__).resolve().parent.parent
IS_WINDOWS = sys.platform == "win32"
GENEROUS = 30.0


def code_of(test, callable_, *args, **kwargs):
    with test.assertRaises(SecretStoreError) as caught:
        callable_(*args, **kwargs)
    return caught.exception.code


class ProcessTestCase(unittest.TestCase):
    def setUp(self):
        base = tempfile.mkdtemp(prefix="ap-secret-conc-")
        self.addCleanup(shutil.rmtree, base, True)
        self.directory = os.path.join(base, "store")
        prepare_windows_secret_directory(self.directory)
        self.outputs = []
        self.processes = []
        self.addCleanup(self.assertNothingLeaked)
        self.addCleanup(self.kill_all)

    def store(self, timeout=GENEROUS):
        return WindowsDpapiSecretStore(self.directory, timeout=timeout)

    def spawn(self, mode, timeout=GENEROUS, count=1, tag="x"):
        process = subprocess.Popen(
            [sys.executable, "-B", "-m", "tests.secret_store_workers", mode, self.directory, str(timeout), str(count), tag],
            cwd=str(BACKEND),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.processes.append(process)
        return process

    def finish(self, process, timeout=120):
        out, err = process.communicate(timeout=timeout)
        self.outputs += [out, err]
        self.assertEqual(process.returncode, 0, err)
        return json.loads(out.strip().splitlines()[-1])

    def run_all(self, *processes):
        return [self.finish(process) for process in processes]

    def kill_all(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            try:
                out, err = process.communicate(timeout=10)
                self.outputs += [out or "", err or ""]
            except Exception:
                pass

    def assertNothingLeaked(self):
        for text in self.outputs:
            self.assertNotIn(PREFIX, text)
        for name in os.listdir(self.directory):
            if not os.path.isfile(os.path.join(self.directory, name)):
                continue
            content = Path(self.directory, name).read_bytes()
            self.assertNotIn(PREFIX.encode(), content)
            self.assertNotIn(PREFIX.encode("utf-16-le"), content)

    def names(self):
        return sorted(os.listdir(self.directory))

    def secret_files(self):
        return [name for name in self.names() if name.endswith(".dpapi")]

    def partial_files(self):
        return [name for name in self.names() if name.endswith(".partial")]

    def raw_value(self):
        """The stored value read around the store (no lock): proves the file on disk is always one complete ciphertext."""
        content = Path(self.directory, file_name_for(REFERENCE)).read_bytes()
        self.assertTrue(content.startswith(FORMAT_TAG))
        return WindowsDpapiProtector().unprotect(content[len(FORMAT_TAG) :], entropy_for(REFERENCE)).decode("utf-8")

    def wait_for_line(self, process, expected, limit=30):
        line = []

        def read():
            line.append(process.stdout.readline())

        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        thread.join(limit)
        self.assertEqual(line and line[0].strip(), expected, "the child did not report in time")


@unittest.skipUnless(IS_WINDOWS, "the cross-process lock uses Windows LockFileEx")
class LockPrimitiveTests(ProcessTestCase):
    def test_shared_holders_share_and_exclusive_holders_exclude_everyone(self):
        locker = WindowsFileLocker()
        with locker.hold(self.directory, False, 1.0):
            with locker.hold(self.directory, False, 1.0):
                pass
            self.assertEqual(code_of(self, self.enter, locker, True, 0.2), CODE_BUSY)
        with locker.hold(self.directory, True, 1.0):
            self.assertEqual(code_of(self, self.enter, locker, False, 0.2), CODE_BUSY)
            self.assertEqual(code_of(self, self.enter, locker, True, 0.2), CODE_BUSY)

    def enter(self, locker, exclusive, timeout):
        with locker.hold(self.directory, exclusive, timeout):
            pass

    def test_the_lock_is_released_when_the_body_raises(self):
        locker = WindowsFileLocker()
        with self.assertRaises(ZeroDivisionError):
            with locker.hold(self.directory, True, 1.0):
                1 / 0
        with locker.hold(self.directory, True, 1.0):
            pass

    def test_the_lock_file_is_an_empty_regular_file_in_the_store_directory_and_is_never_removed(self):
        self.store().exists(REFERENCE)
        self.assertIn(LOCK_FILE_NAME, self.names())
        self.assertEqual(Path(self.directory, LOCK_FILE_NAME).read_bytes(), b"")
        self.store().put(REFERENCE, value("a", 1))
        self.store().delete(REFERENCE)
        self.assertIn(LOCK_FILE_NAME, self.names())
        self.assertNotIn(LOCK_FILE_NAME, [file_name_for(REFERENCE)])

    def test_a_lock_file_that_is_not_a_regular_file_is_refused(self):
        os.mkdir(os.path.join(self.directory, LOCK_FILE_NAME))
        self.assertEqual(code_of(self, self.store().exists, REFERENCE), "secret_store_unavailable")

    def test_the_in_process_locker_times_out_with_the_same_stable_code(self):
        locker = InProcessLocker()
        with locker.hold(self.directory, True, 1.0):
            self.assertEqual(code_of(self, self.enter, locker, False, 0.1), CODE_BUSY)


@unittest.skipUnless(IS_WINDOWS, "the cross-process lock uses Windows LockFileEx")
class MultiProcessTests(ProcessTestCase):
    def test_many_reader_processes_never_fail_and_never_see_a_wrong_value(self):
        self.store().put(REFERENCE, value("seed", 0))
        results = self.run_all(*[self.spawn("reader", count=150) for _ in range(4)])
        for result in results:
            self.assertEqual(result["codes"], {"ok": 150})
            self.assertEqual(result["bad"], 0)

    def test_readers_and_a_rotator_run_together_without_a_failed_torn_or_wrong_read(self):
        self.store().put(REFERENCE, value("seed", 0))
        rotator = self.spawn("rotator", count=100, tag="rot")
        readers = [self.spawn("reader", count=150) for _ in range(3)]
        raw = self.spawn("rawcheck", count=300)
        self.assertEqual(self.finish(rotator)["codes"], {"ok": 100})
        for result in self.run_all(*readers):
            self.assertEqual(result["codes"], {"ok": 150})
            self.assertEqual(result["bad"], 0)
        raw_result = self.finish(raw)
        self.assertEqual(raw_result["bad"], 0)  # every file the raw reader got was one complete, decryptable ciphertext
        self.assertGreater(raw_result["codes"].get("ok", 0), 0)
        self.assertEqual(self.store().get(REFERENCE).reveal(), value("rot", 99))
        self.assertEqual(self.partial_files(), [])
        self.assertEqual(self.secret_files(), [file_name_for(REFERENCE)])

    def test_concurrent_rotations_all_succeed_and_leave_one_complete_value(self):
        self.store().put(REFERENCE, value("seed", 0))
        results = self.run_all(*[self.spawn("rotator", count=60, tag=tag) for tag in ("aaa", "bbb", "ccc")])
        for result in results:
            self.assertEqual(result["codes"], {"ok": 60})
        final = self.store().get(REFERENCE).reveal()
        self.assertRegex(final, VALID)
        self.assertTrue(final.endswith("-00059"), "a stale write must not outlive a newer one of the same writer")
        self.assertEqual(self.raw_value(), final)
        self.assertEqual(self.partial_files(), [])

    def test_create_is_create_only_across_processes_and_the_winner_is_what_is_stored(self):
        tags = ["aa", "bb", "cc", "dd", "ee", "ff", "gg", "hh"]
        results = self.run_all(*[self.spawn("creator", tag=tag) for tag in tags])
        outcomes = [next(iter(result["codes"])) for result in results]
        self.assertEqual(sorted(outcomes), ["ok"] + [CODE_ALREADY_EXISTS] * 7)
        winner = results[outcomes.index("ok")]["tag"]
        self.assertEqual(self.store().get(REFERENCE).reveal(), value(winner, 0))

    def test_replace_is_replace_only_across_processes(self):
        results = self.run_all(*[self.spawn("replacer", count=15, tag=tag) for tag in ("aa", "bb", "cc", "dd")])
        for result in results:
            self.assertEqual(result["codes"], {CODE_NOT_FOUND: 15})
        self.assertFalse(self.store().exists(REFERENCE))
        self.assertEqual(self.secret_files(), [])

    def test_exactly_one_of_many_deleters_deletes(self):
        self.store().put(REFERENCE, value("seed", 0))
        results = self.run_all(*[self.spawn("deleter", count=1) for _ in range(6)])
        outcomes = sorted(next(iter(result["codes"])) for result in results)
        self.assertEqual(outcomes, ["ok"] + [CODE_NOT_FOUND] * 5)
        self.assertFalse(self.store().exists(REFERENCE))

    def test_a_replace_waiting_for_the_lock_does_not_resurrect_a_secret_deleted_meanwhile(self):
        self.store().put(REFERENCE, value("seed", 0))
        with WindowsFileLocker().hold(self.directory, True, 5.0):  # stands for a delete that is in its critical section
            replacer = self.spawn("replacer", count=1, tag="late")
            time.sleep(2.0)  # the child is by now waiting for the lock; a child that is slower only makes the test weaker, never wrong
            os.unlink(os.path.join(self.directory, file_name_for(REFERENCE)))
        self.assertEqual(self.finish(replacer)["codes"], {CODE_NOT_FOUND: 1})
        self.assertFalse(self.store().exists(REFERENCE))
        self.assertEqual(self.secret_files(), [])

    def test_a_replace_never_resurrects_a_secret_a_delete_already_removed(self):
        for round_number in range(6):
            self.store().put(REFERENCE, value("seed", round_number))
            replacer = self.spawn("replacer", count=250, tag="rep")
            deleter = self.spawn("deleter", count=40)
            replaced, deleted = self.finish(replacer), self.finish(deleter)
            self.assertEqual(len(deleted["done"]), 1, "the secret exists once, so exactly one delete succeeds")
            removed_at = deleted["done"][0]
            late = [began for began in replaced["ok_starts"] if began > removed_at]
            self.assertEqual(late, [], "a replace that began after the delete finished must not succeed")
            self.assertFalse(self.store().exists(REFERENCE))
            self.assertEqual(self.secret_files(), [])


@unittest.skipUnless(IS_WINDOWS, "the cross-process lock uses Windows LockFileEx")
class TimeoutAndInterruptionTests(ProcessTestCase):
    def hold(self, kind):
        holder = self.spawn("hold", tag=kind)
        self.wait_for_line(holder, "locked")
        return holder

    def test_every_operation_times_out_with_one_stable_code_and_no_detail(self):
        self.store().put(REFERENCE, value("seed", 0))
        holder = self.hold("exclusive")
        store = self.store(timeout=0.3)
        calls = (
            lambda: store.get(REFERENCE),
            lambda: store.exists(REFERENCE),
            lambda: store.put(REFERENCE, value("late", 1), replace=True),
            lambda: store.put("AP_AI_CRED_OTHER", value("late", 2)),
            lambda: store.delete(REFERENCE),
        )
        for call in calls:
            began = time.monotonic()
            with self.assertRaises(SecretStoreError) as caught:
                call()
            self.assertLess(time.monotonic() - began, 5.0, "the wait is bounded by the deadline")
            self.assertEqual(caught.exception.code, CODE_BUSY)
            for text in (str(caught.exception), repr(caught.exception)):
                for forbidden in (PREFIX, REFERENCE, self.directory, "OTHER"):
                    self.assertNotIn(forbidden, text)
        holder.kill()
        holder.wait(30)
        self.assertEqual(self.store(timeout=5).get(REFERENCE).reveal(), value("seed", 0))

    def test_a_shared_holder_blocks_writers_but_not_readers(self):
        self.store().put(REFERENCE, value("seed", 0))
        holder = self.hold("shared")
        short = self.store(timeout=0.3)
        self.assertEqual(short.get(REFERENCE).reveal(), value("seed", 0))
        self.assertTrue(short.exists(REFERENCE))
        self.assertEqual(code_of(self, short.put, REFERENCE, value("late", 1), replace=True), CODE_BUSY)
        self.assertEqual(code_of(self, short.delete, REFERENCE), CODE_BUSY)
        holder.kill()
        holder.wait(30)
        self.assertEqual(self.store(timeout=5).get(REFERENCE).reveal(), value("seed", 0))  # nothing happened to it

    def test_a_killed_lock_holder_releases_the_lock_without_any_cleanup(self):
        holder = self.hold("exclusive")
        self.assertEqual(code_of(self, self.store(timeout=0.3).exists, REFERENCE), CODE_BUSY)
        holder.kill()  # TerminateProcess: no finally block, no atexit
        holder.wait(30)
        self.assertFalse(self.store(timeout=5).exists(REFERENCE))

    def test_a_writer_killed_mid_write_leaves_the_previous_secret_intact_and_is_cleaned_up_by_the_next_writer(self):
        self.store().put(REFERENCE, value("seed", 0))
        stalled = self.spawn("stall", tag="rotate")
        self.wait_for_line(stalled, "writing")
        self.assertEqual(len(self.partial_files()), 1, "the interrupted write left its partial file")
        self.assertEqual(code_of(self, self.store(timeout=0.3).get, REFERENCE), CODE_BUSY)  # the writer still holds the exclusive lock
        stalled.kill()
        stalled.wait(30)
        self.assertEqual(self.raw_value(), value("seed", 0), "the previous file was never touched")
        self.assertEqual(self.store(timeout=5).get(REFERENCE).reveal(), value("seed", 0))
        self.store(timeout=5).put(REFERENCE, value("next", 1), replace=True)
        self.assertEqual(self.partial_files(), [], "the next writer swept the debris")
        self.assertEqual(self.store().get(REFERENCE).reveal(), value("next", 1))

    def test_a_create_killed_mid_write_creates_nothing_and_the_reference_can_still_be_created(self):
        stalled = self.spawn("stall", tag="create")
        self.wait_for_line(stalled, "writing")
        stalled.kill()
        stalled.wait(30)
        self.assertEqual(self.secret_files(), [])
        self.assertFalse(self.store(timeout=5).exists(REFERENCE))
        self.assertEqual(code_of(self, self.store(timeout=5).get, REFERENCE), CODE_NOT_FOUND)
        self.store(timeout=5).put(REFERENCE, value("fresh", 1))
        self.assertEqual(self.partial_files(), [])
        self.assertEqual(self.store().get(REFERENCE).reveal(), value("fresh", 1))

    def test_a_delete_interrupted_by_a_busy_lock_changes_nothing(self):
        self.store().put(REFERENCE, value("seed", 0))
        holder = self.hold("shared")
        self.assertEqual(code_of(self, self.store(timeout=0.2).delete, REFERENCE), CODE_BUSY)
        holder.kill()
        holder.wait(30)
        self.assertEqual(self.raw_value(), value("seed", 0))


@unittest.skipUnless(IS_WINDOWS, "sharing violations are a Windows behaviour")
class ThirdPartyFileHandleTests(ProcessTestCase):
    """A scanner or indexer that has the secret file open (without delete sharing) makes rename and unlink fail briefly; the store retries inside its deadline."""

    def hold_file_open(self, seconds):
        handle = open(os.path.join(self.directory, file_name_for(REFERENCE)), "rb")
        timer = threading.Timer(seconds, handle.close)
        self.addCleanup(handle.close)
        timer.start()
        self.addCleanup(timer.cancel)
        return handle

    def test_a_replace_waits_out_a_brief_sharing_violation(self):
        self.store().put(REFERENCE, value("seed", 0))
        self.hold_file_open(0.4)
        began = time.monotonic()
        self.store(timeout=10).put(REFERENCE, value("new", 1), replace=True)
        self.assertGreaterEqual(time.monotonic() - began, 0.3)
        self.assertEqual(self.store().get(REFERENCE).reveal(), value("new", 1))
        self.assertEqual(self.partial_files(), [])

    def test_a_delete_waits_out_a_brief_sharing_violation(self):
        self.store().put(REFERENCE, value("seed", 0))
        self.hold_file_open(0.4)
        self.store(timeout=10).delete(REFERENCE)
        self.assertFalse(self.store().exists(REFERENCE))

    def test_a_sharing_violation_that_outlasts_the_deadline_is_busy_and_loses_nothing(self):
        self.store().put(REFERENCE, value("seed", 0))
        self.hold_file_open(3.0)
        began = time.monotonic()
        self.assertEqual(code_of(self, self.store(timeout=0.5).put, REFERENCE, value("new", 1), replace=True), CODE_BUSY)
        self.assertEqual(code_of(self, self.store(timeout=0.5).delete, REFERENCE), CODE_BUSY)
        self.assertLess(time.monotonic() - began, 3.0)
        time.sleep(3.2)
        self.assertEqual(self.store().get(REFERENCE).reveal(), value("seed", 0))
        self.assertEqual(self.partial_files(), [])


if __name__ == "__main__":
    unittest.main()
