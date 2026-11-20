"""DB-free tests of the process isolation of text extraction (DOC-2): the REAL child process (``apps/document_text/child.py``) and the parent that supervises it (``isolation.py``).

Real processes are started: the real extractor on synthetic PDFs, and small stand-in scripts that hang, burn CPU, exhaust memory, write too much, write garbage, crash or inspect their environment (so the supervision
can be tested without a real hostile PDF). Nothing touches the network, a database or a real document. The stand-ins put the same operating-system limits on themselves that the real child does
(``child_limits``), so the memory and CPU tests exercise the real mechanism: a Windows job object here, ``setrlimit`` on POSIX (the POSIX path was NOT run in the development VM).
"""

import io
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from apps.document_text import child, child_limits, isolation, pdf_text, rules
from apps.document_text.isolation import ExtractorUnavailableError, IsolatedExtractor
from apps.document_text.rules import ExtractionLimits
from tests.pdf_support import build_pdf

BACKEND = Path(__file__).resolve().parent.parent
FAST = ExtractionLimits(timeout_seconds=20)


def process_alive(pid: int) -> bool:
    if os.name == "nt":
        listing = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in listing
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class ScriptCase(SimpleTestCase):
    """Writes small stand-in child scripts into a temporary directory and runs the real supervisor against them."""

    PRELUDE = textwrap.dedent(
        f"""
        import os, sys
        sys.path.insert(0, {str(BACKEND)!r})
        from apps.document_text import child_limits, rules
        max_pages, max_page_chars, max_total_chars, max_bytes, memory_bytes, timeout_seconds = (int(v) for v in sys.argv[1:7])
        """
    )

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="ap-doctext-test-")
        self.addCleanup(self.directory.cleanup)

    def script(self, body: str, *, limits: bool = True) -> IsolatedExtractor:
        path = Path(self.directory.name) / f"child_{len(list(Path(self.directory.name).iterdir()))}.py"
        header = self.PRELUDE + ("child_limits.apply_limits(memory_bytes, timeout_seconds)\n" if limits else "")
        path.write_text(header + textwrap.dedent(body), encoding="utf-8")
        return IsolatedExtractor(command=(sys.executable, "-s", "-P", "-B", str(path)))


class RealExtractionTests(SimpleTestCase):
    def test_the_real_child_extracts_text_page_by_page(self):
        data = build_pdf([("First page", "second line"), ("Second page",), ()])
        result = IsolatedExtractor()(data, FAST)
        self.assertEqual((result.status, result.error_code, result.page_count), (rules.RESULT_EXTRACTED, "", 3))
        self.assertEqual([(p.page_number, p.status, p.text) for p in result.pages], [(1, "text", "First page\nsecond line"), (2, "text", "Second page"), (3, "empty", "")])
        self.assertEqual(result.extractor_version, pdf_text.library_version())

    def test_the_process_gives_the_same_answer_as_the_library_in_process_and_on_every_run(self):
        data = build_pdf([("Alpha", "beta"), ("Gamma \u20ac",)])
        in_process = pdf_text.extract_pdf_text(data, FAST)
        first, second = IsolatedExtractor()(data, FAST), IsolatedExtractor()(data, FAST)
        self.assertEqual(first, in_process)
        self.assertEqual((first, first.text_sha256), (second, in_process.text_sha256))

    def test_an_encrypted_a_truncated_and_a_garbage_document_are_failed_results_not_errors(self):
        extractor = IsolatedExtractor()
        encrypted = extractor(build_pdf([("secret",)], encrypt=True), FAST)
        self.assertEqual((encrypted.status, encrypted.error_code, encrypted.pages), (rules.RESULT_FAILED, rules.ERR_ENCRYPTED, ()))
        truncated = extractor(build_pdf([("x",), ("y",)])[:200], FAST)
        self.assertEqual((truncated.status, truncated.error_code), (rules.RESULT_FAILED, rules.ERR_MALFORMED))
        garbage = extractor(b"\x00garbage" * 100, FAST)
        self.assertEqual((garbage.status, garbage.error_code), (rules.RESULT_FAILED, rules.ERR_MALFORMED))

    def test_the_limits_reach_the_child(self):
        data = build_pdf([("x",)] * 4)
        self.assertEqual(IsolatedExtractor()(data, ExtractionLimits(max_pages=3)).error_code, rules.ERR_TOO_MANY_PAGES)
        self.assertEqual(IsolatedExtractor()(data, ExtractionLimits(max_bytes=1024)).error_code, rules.ERR_DOCUMENT_TOO_LARGE)  # more bytes than the child may read
        long = IsolatedExtractor()(build_pdf([("x" * 30,)]), ExtractionLimits(max_page_chars=10, max_total_chars=20))
        self.assertEqual(long.status, rules.RESULT_FAILED)



class SupervisionTests(ScriptCase):
    def test_what_the_child_receives_is_exactly_the_bytes_sent(self):
        extractor = self.script(
            """
            import hashlib
            data = sys.stdin.buffer.read()
            sys.stdout.buffer.write(rules.encode_child_output(rules.failed_output(rules.ERR_MALFORMED, extractor_version=hashlib.sha256(data).hexdigest()[:32])))
            """
        )
        import hashlib

        payload = os.urandom(5 * 1024 * 1024)
        self.assertEqual(extractor(payload, FAST).extractor_version, hashlib.sha256(payload).hexdigest()[:32])

    def test_a_child_that_never_reads_its_input_does_not_block_the_parent(self):
        extractor = self.script("sys.stdout.buffer.write(rules.encode_child_output(rules.failed_output(rules.ERR_MALFORMED, extractor_version='1.0')))")
        started = time.monotonic()
        result = extractor(os.urandom(8 * 1024 * 1024), FAST)
        self.assertEqual(result.error_code, rules.ERR_MALFORMED)
        self.assertLess(time.monotonic() - started, 15)

    def test_a_hanging_child_is_killed_at_the_deadline_and_reported_as_a_timeout(self):
        extractor = self.script(
            """
            sys.stdin.buffer.read()
            pidfile = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pid.txt")
            open(pidfile, "w").write(str(os.getpid()))
            sys.stdin.buffer.read()
            import time
            time.sleep(120)
            """
        )
        started = time.monotonic()
        result = extractor(b"x", ExtractionLimits(timeout_seconds=2))
        elapsed = time.monotonic() - started
        self.assertEqual((result.status, result.error_code, result.pages), (rules.RESULT_FAILED, rules.ERR_TIMEOUT, ()))
        self.assertLess(elapsed, 10)
        self.assertGreaterEqual(elapsed, 1.5)
        pid = int((Path(self.directory.name) / "pid.txt").read_text())
        deadline = time.monotonic() + 5
        while process_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(process_alive(pid))  # nothing is left running

    def test_a_child_that_burns_cpu_is_stopped_at_the_deadline(self):
        extractor = self.script("sys.stdin.buffer.read()\nwhile True:\n    pass\n")
        started = time.monotonic()
        result = extractor(b"x", ExtractionLimits(timeout_seconds=2))
        self.assertEqual((result.status, result.error_code), (rules.RESULT_FAILED, rules.ERR_TIMEOUT))
        self.assertLess(time.monotonic() - started, 10)

    def test_a_child_that_exhausts_its_memory_limit_is_a_resource_limit_not_a_crash(self):
        extractor = self.script(
            """
            sys.stdin.buffer.read()
            try:
                hog = bytearray(900 * 1024 * 1024)
                sys.stdout.buffer.write(b"the limit did not hold")
            except MemoryError:
                sys.stdout.buffer.write(rules.encode_fatal(rules.FATAL_RESOURCE_LIMIT))
                sys.exit(2)
            """
        )
        result = extractor(b"x", ExtractionLimits(memory_bytes=256 * rules.MIB))
        self.assertEqual((result.status, result.error_code), (rules.RESULT_FAILED, rules.ERR_RESOURCE_LIMIT))

    def test_the_limits_stop_a_child_that_cannot_report_it(self):
        extractor = self.script(
            """
            sys.stdin.buffer.read()
            hog = bytearray(900 * 1024 * 1024)  # an uncaught MemoryError: a traceback on a discarded stderr and a non-zero exit
            sys.stdout.buffer.write(rules.encode_child_output(rules.failed_output(rules.ERR_MALFORMED, extractor_version="1.0")))
            """
        )
        result = extractor(b"x", ExtractionLimits(memory_bytes=256 * rules.MIB))
        self.assertEqual((result.status, result.error_code), (rules.RESULT_FAILED, rules.ERR_EXTRACTOR_FAILED))

    def test_a_child_cannot_start_another_process_under_its_limits(self):
        extractor = self.script(
            """
            import subprocess
            sys.stdin.buffer.read()
            try:
                subprocess.run([sys.executable, "-c", "pass"], capture_output=True, timeout=20)
                verdict = "spawned"
            except Exception:
                verdict = "blocked"
            version = "1.0-" + verdict
            sys.stdout.buffer.write(rules.encode_child_output(rules.failed_output(rules.ERR_MALFORMED, extractor_version=version)))
            """
        )
        if os.name == "posix" and not hasattr(__import__("resource"), "RLIMIT_NPROC"):
            self.skipTest("no RLIMIT_NPROC on this platform")
        result = extractor(b"x", ExtractionLimits())
        self.assertEqual(result.extractor_version, "1.0-blocked")

    def test_a_child_that_writes_more_than_its_cap_is_stopped_and_refused(self):
        extractor = self.script(
            """
            sys.stdin.buffer.read()
            chunk = b"A" * 65536
            for _ in range(2000):
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            """
        )
        started = time.monotonic()
        result = extractor(b"x", ExtractionLimits(max_pages=1, max_page_chars=10, max_total_chars=10))
        self.assertEqual((result.status, result.error_code), (rules.RESULT_FAILED, rules.ERR_EXTRACTOR_FAILED))
        self.assertLess(time.monotonic() - started, 15)

    def test_garbage_an_empty_answer_and_a_crash_are_extractor_failures(self):
        for body in (
            "sys.stdin.buffer.read()\nsys.stdout.buffer.write(b'not json at all')",
            "sys.stdin.buffer.read()",
            "sys.stdin.buffer.read()\nsys.exit(3)",
            "sys.stdin.buffer.read()\nraise RuntimeError('boom')",
            "sys.stdin.buffer.read()\nsys.stdout.buffer.write(b'{\"version\": 1, \"status\": \"extracted\"}')",
            "sys.stdin.buffer.read()\nsys.stdout.buffer.write(b'x' * 100) \nsys.exit(1)",
        ):
            with self.subTest(body=body.splitlines()[-1]):
                result = self.script(body)(b"x", FAST)
                self.assertEqual((result.status, result.error_code, result.pages), (rules.RESULT_FAILED, rules.ERR_EXTRACTOR_FAILED, ()))

    def test_a_child_that_cannot_put_its_limits_in_force_is_unavailable_not_the_documents_fault(self):
        extractor = self.script("sys.stdin.buffer.read()\nsys.stdout.buffer.write(rules.encode_fatal(rules.FATAL_SANDBOX_UNAVAILABLE))\nsys.exit(2)")
        with self.assertRaises(ExtractorUnavailableError):
            extractor(b"x", FAST)

    def test_a_missing_interpreter_or_script_is_unavailable(self):
        with self.assertRaises(ExtractorUnavailableError):
            IsolatedExtractor(command=(os.path.join(self.directory.name, "no-such-python.exe"),))(b"x", FAST)
        result = IsolatedExtractor(command=(sys.executable, "-s", "-P", "-B", os.path.join(self.directory.name, "missing.py")))(b"x", FAST)
        self.assertEqual((result.status, result.error_code), (rules.RESULT_FAILED, rules.ERR_EXTRACTOR_FAILED))  # the interpreter ran and failed: the document is not blamed for a missing file, but nothing is invented

    def test_the_child_environment_holds_no_application_setting_or_secret(self):
        extractor = self.script(
            """
            sys.stdin.buffer.read()
            leaked = [name for name in os.environ if name.upper().startswith(("AP_", "DJANGO", "DATABASE", "PG", "AWS", "AZURE", "OPENAI", "SECRET")) or "KEY" in name.upper() or "PASSWORD" in name.upper()]
            code = rules.ERR_MALFORMED if not leaked else rules.ERR_TIMEOUT
            sys.stdout.buffer.write(rules.encode_child_output(rules.failed_output(code, extractor_version="1.0")))
            """
        )
        polluted = {"AP_SECRET_KEY": "sentinel-1", "AP_DB_PASSWORD": "sentinel-2", "DJANGO_SETTINGS_MODULE": "x", "PGPASSWORD": "sentinel-3", "OPENAI_API_KEY": "sentinel-4"}
        with mock.patch.dict(os.environ, polluted):
            result = extractor(b"x", FAST)
        self.assertEqual(result.error_code, rules.ERR_MALFORMED)
        environment = isolation.child_environment()
        self.assertLessEqual(set(environment), {"PYTHONHASHSEED", "SYSTEMROOT"})
        self.assertEqual(environment["PYTHONHASHSEED"], "0")

    def test_the_command_and_the_interpreter_flags_are_fixed(self):
        self.assertEqual(isolation.INTERPRETER_FLAGS, ("-s", "-P", "-B"))
        self.assertTrue(isolation.CHILD_SCRIPT.endswith("child.py"))
        self.assertTrue(Path(isolation.CHILD_SCRIPT).is_file())
        self.assertEqual(IsolatedExtractor()._command, (sys.executable, "-s", "-P", "-B", isolation.CHILD_SCRIPT))

    def test_the_supervisor_refuses_what_is_not_bytes_or_limits(self):
        for data, limits in (("text", FAST), (b"x", {"timeout": 5}), (None, FAST), (b"x", None)):
            with self.subTest(data=type(data), limits=type(limits)), self.assertRaises(TypeError):
                IsolatedExtractor()(data, limits)


class ChildEntryPointTests(SimpleTestCase):
    ARGS = ["5", "100", "200", "1048576", str(256 * rules.MIB), "20"]

    def run_child(self, data=b"", argv=None, apply=None):
        out = io.BytesIO()
        with mock.patch.object(child.child_limits, "apply_limits", apply or (lambda *a: None)):
            code = child.run(argv if argv is not None else self.ARGS, io.BytesIO(data), out)
        return code, out.getvalue()

    def test_a_normal_run_writes_one_valid_answer_and_exits_zero(self):
        code, raw = self.run_child(build_pdf([("Hello",)]))
        self.assertEqual(code, 0)
        result = rules.parse_child_output(raw, ExtractionLimits(max_pages=5, max_page_chars=100, max_total_chars=200))
        self.assertEqual([p.text for p in result.pages], ["Hello"])

    def test_the_limits_are_put_in_force_before_anything_is_read(self):
        order = []

        class Stream(io.BytesIO):
            def read(self, size=-1):
                order.append("read")
                return super().read(size)

        with mock.patch.object(child.child_limits, "apply_limits", lambda *a: order.append(("limits", a))):
            child.run(self.ARGS, Stream(build_pdf([("x",)])), io.BytesIO())
        self.assertEqual(order[0], ("limits", (256 * rules.MIB, 20)))
        self.assertIn("read", order)

    def test_limits_that_cannot_be_applied_end_in_a_fatal_answer(self):
        def refuse(*args):
            raise child_limits.LimitsUnavailableError("job")

        code, raw = self.run_child(build_pdf([("x",)]), apply=refuse)
        self.assertEqual(code, 2)
        with self.assertRaises(rules.ChildFatal) as caught:
            rules.parse_child_output(raw, ExtractionLimits())
        self.assertEqual(caught.exception.code, rules.FATAL_SANDBOX_UNAVAILABLE)

    def test_a_memory_error_ends_in_a_resource_limit_answer(self):
        with mock.patch.object(pdf_text, "extract_pdf_text", side_effect=MemoryError):
            code, raw = self.run_child(build_pdf([("x",)]))
        self.assertEqual(code, 2)
        with self.assertRaises(rules.ChildFatal) as caught:
            rules.parse_child_output(raw, ExtractionLimits())
        self.assertEqual(caught.exception.code, rules.FATAL_RESOURCE_LIMIT)

    def test_input_beyond_the_byte_limit_is_a_failed_result_not_read_whole(self):
        code, raw = self.run_child(b"x" * 2000, argv=["5", "100", "200", "1024", str(256 * rules.MIB), "20"])
        self.assertEqual(code, 0)
        result = rules.parse_child_output(raw, ExtractionLimits(max_pages=5, max_page_chars=100, max_total_chars=200, max_bytes=1024))
        self.assertEqual((result.status, result.error_code), (rules.RESULT_FAILED, rules.ERR_DOCUMENT_TOO_LARGE))

    def test_bad_arguments_exit_with_a_usage_code_and_write_nothing(self):
        for argv in ([], ["1"], ["a"] * 6, ["0"] * 6, self.ARGS[:-1], [*self.ARGS, "7"]):
            with self.subTest(argv=argv):
                code, raw = self.run_child(b"x", argv=argv)
                self.assertEqual((code, raw), (64, b""))


class LimitsModuleTests(SimpleTestCase):
    @unittest.skipUnless(os.name == "nt", "the job object structures are Windows only")
    def test_the_windows_structures_have_the_sizes_the_api_expects(self):
        import ctypes

        # 64-bit: JOBOBJECT_EXTENDED_LIMIT_INFORMATION is 144 bytes, 32-bit: 112
        expected = 144 if ctypes.sizeof(ctypes.c_void_p) == 8 else 112
        self.assertEqual(ctypes.sizeof(child_limits.windows_structures()), expected)

    def test_an_unsupported_platform_fails_closed(self):
        with mock.patch.object(child_limits.os, "name", "java"), self.assertRaises(child_limits.LimitsUnavailableError):
            child_limits.apply_limits(256 * rules.MIB, 5)

    def test_posix_limits_that_cannot_be_set_fail_closed(self):
        import types

        failing = types.SimpleNamespace(RLIMIT_AS=9, RLIMIT_CPU=0, RLIMIT_CORE=4, RLIMIT_FSIZE=1, setrlimit=mock.Mock(side_effect=ValueError("not honoured")))
        with mock.patch.dict(sys.modules, {"resource": failing}), self.assertRaises(child_limits.LimitsUnavailableError):
            child_limits._apply_posix(256 * rules.MIB, 5)

    def test_posix_limits_are_the_four_required_ones_and_the_best_effort_process_limit(self):
        import types

        calls = []
        fake = types.SimpleNamespace(RLIMIT_AS=9, RLIMIT_CPU=0, RLIMIT_CORE=4, RLIMIT_FSIZE=1, RLIMIT_NPROC=6, setrlimit=lambda which, value: calls.append((which, value)))
        with mock.patch.dict(sys.modules, {"resource": fake}):
            child_limits._apply_posix(256 * rules.MIB, 5)
        self.assertEqual(calls, [(9, (256 * rules.MIB, 256 * rules.MIB)), (0, (5, 6)), (4, (0, 0)), (1, (0, 0)), (6, (0, 0))])

    def test_a_missing_process_limit_is_tolerated_but_a_missing_memory_limit_is_not(self):
        import types

        no_nproc = types.SimpleNamespace(RLIMIT_AS=9, RLIMIT_CPU=0, RLIMIT_CORE=4, RLIMIT_FSIZE=1, setrlimit=lambda *a: None)
        with mock.patch.dict(sys.modules, {"resource": no_nproc}):
            child_limits._apply_posix(256 * rules.MIB, 5)
        no_address_space = types.SimpleNamespace(RLIMIT_CPU=0, RLIMIT_CORE=4, RLIMIT_FSIZE=1, setrlimit=lambda *a: None)
        with mock.patch.dict(sys.modules, {"resource": no_address_space}), self.assertRaises(child_limits.LimitsUnavailableError):
            child_limits._apply_posix(256 * rules.MIB, 5)
