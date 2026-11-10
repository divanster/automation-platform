"""Pure-domain RUNTIME boundary tests (standard-library unittest, no Django).

WHAT THESE TESTS ACTUALLY SHOW

1. In a clean child interpreter, Django, DRF, PostgreSQL/SQLite drivers, network and HTTP
   libraries, and AI provider SDKs are made un-importable (``sys.modules[name] = None``).
   The package still imports and the whole extract-and-validate flow still runs, so none of
   it depends on those modules.
2. In the same child, an audit hook (``sys.addaudithook``) records every audit event raised
   while the flow runs. None of the recorded events is a file open, a socket or network
   event, a subprocess or process-spawn event, a database-connection event, a directory
   operation, or a dynamic code load.
3. In-process, with ``socket`` and ``open`` replaced by functions that raise, the flow still
   runs.

WHAT THEY DO NOT SHOW. Audit events cover Python-level operations. They do not cover a C
extension that performs system calls without raising an audit event, and they are not a
sandbox. This is evidence about this code's behavior over the inputs exercised, not a
guarantee for all inputs. The static import scan lives in
test_request_intake_boundaries.py and likewise proves only what the code can import.
"""

import builtins
import json
import socket
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.intake_support import VALID_TEXT
from workflows.request_intake import DeterministicTestExtractor, ValidationLimits, validate_request

BACKEND_DIR = Path(__file__).resolve().parent.parent

# Modules that must be un-importable in the child. None is needed by the package.
BLOCKED_MODULES = [
    "django",
    "rest_framework",
    "psycopg",
    "psycopg2",
    "sqlite3",
    "_sqlite3",
    "socket",
    "_socket",
    "ssl",
    "urllib",
    "urllib.request",
    "http",
    "http.client",
    "requests",
    "anthropic",
    "openai",
    "subprocess",
]

# Audit-event names (exact) or prefixes (ending in ".") that must NOT occur while the flow runs.
FORBIDDEN_EXACT = {
    "open",
    "os.system",
    "os.fork",
    "os.forkpty",
    "os.listdir",
    "os.scandir",
    "os.mkdir",
    "os.remove",
    "os.rename",
    "os.rmdir",
    "os.chmod",
    "os.chown",
    "os.truncate",
    "os.putenv",
    "os.unsetenv",
    "exec",
    "compile",
    "ctypes.dlopen",
    "winreg.OpenKey",
}
FORBIDDEN_PREFIXES = (
    "socket.",
    "subprocess.",
    "os.exec",
    "os.spawn",
    "os.posix_spawn",
    "shutil.",
    "sqlite3.",
    "urllib.",
    "http.",
    "ftplib.",
    "smtplib.",
    "ssl.",
    "pty.",
    "mmap.",
    "tempfile.",
)

CHILD_PROGRAM = r"""
import json, sys

backend_dir = sys.argv[1]
blocked = json.loads(sys.argv[2])
sys.path.insert(0, backend_dir)

for name in blocked:
    sys.modules[name] = None  # any later "import <name>" raises ImportError

import workflows.request_intake as ri          # must import with those modules blocked

events = []
recording = False
def hook(event, args):
    if recording:
        events.append(event)
sys.addaudithook(hook)

extractor = ri.DeterministicTestExtractor()
texts = [
    "Requester: Jane Doe\nContact: j@example.invalid\nItem: 10 | boxes | A4 paper | note\nItem: 2.50 | kg | Coffee\nNotes: soon\n",
    "Requester: A\nItem: a few | | Staplers\nItem: broken\nItem: -3 | | Neg\nItem: 0 | | Zero\n",
    "",
    "prose that is not the template",
]
limits = ri.ValidationLimits(max_quantity=100, max_text_length=40)

recording = True
outcomes = []
for text in texts:
    extraction = extractor.extract(text)
    for kwargs in ({}, {"limits": limits}, {"source_text": text}, {"limits": limits, "source_text": text, "ambiguities": extraction.ambiguities}):
        result = ri.validate_request(extraction.candidate, **kwargs)
        outcomes.append([f.code.value for f in result.findings])
ri.to_decimal(2.5); ri.to_decimal(10 ** 40)
recording = False

leaked = sorted(m for m in sys.modules if (m == "django" or m.startswith("django.")) and sys.modules[m] is not None)
print(json.dumps({"events": sorted(set(events)), "outcomes": len(outcomes), "django_loaded": leaked}))
"""


def forbidden_events(events):
    bad = []
    for event in events:
        if event in FORBIDDEN_EXACT or event.startswith(FORBIDDEN_PREFIXES):
            bad.append(event)
    return bad


class CleanInterpreterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        completed = subprocess.run(
            [sys.executable, "-I", "-c", CHILD_PROGRAM, str(BACKEND_DIR), json.dumps(BLOCKED_MODULES)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        cls.completed = completed
        cls.report = json.loads(completed.stdout) if completed.returncode == 0 and completed.stdout.strip() else None

    def test_the_flow_runs_with_django_databases_network_and_providers_blocked(self):
        self.assertEqual(self.completed.returncode, 0, self.completed.stderr[-2000:])
        self.assertIsNotNone(self.report, self.completed.stderr[-2000:])
        self.assertEqual(self.report["outcomes"], 16)  # 4 texts x 4 validation variants all completed

    def test_django_was_never_loaded_by_the_package(self):
        self.assertIsNotNone(self.report)
        self.assertEqual(self.report["django_loaded"], [])

    def test_no_file_network_process_database_or_dynamic_code_audit_events_occurred(self):
        self.assertIsNotNone(self.report)
        self.assertEqual(forbidden_events(self.report["events"]), [], self.report["events"])

    def test_the_recorded_events_are_few_and_harmless(self):
        # Documented so a surprising new event is noticed: print them on failure.
        self.assertIsNotNone(self.report)
        self.assertLess(len(self.report["events"]), 25, self.report["events"])

    def test_the_forbidden_event_filter_works(self):
        self.assertEqual(
            forbidden_events(["object.__getattr__", "open", "socket.connect", "subprocess.Popen", "sqlite3.connect", "os.system", "import"]),
            ["open", "socket.connect", "subprocess.Popen", "sqlite3.connect", "os.system"],
        )

    def test_the_audit_hook_really_sees_file_access(self):
        # Control experiment: the same child setup DOES record an 'open' when code opens a file,
        # so an empty result above is meaningful and not a broken hook.
        program = (
            "import sys\n"
            "events=[]\n"
            "sys.addaudithook(lambda e,a: events.append(e))\n"
            "open(sys.argv[1], 'rb').close()\n"
            "print('open' in events)\n"
        )
        completed = subprocess.run([sys.executable, "-I", "-c", program, str(Path(__file__))], capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.stdout.strip(), "True", completed.stderr[-500:])


class InProcessGuardTests(unittest.TestCase):
    """With socket and open replaced by functions that raise, the flow still completes."""

    def test_no_socket_or_file_access_through_the_normal_apis(self):
        def refuse(*args, **kwargs):
            raise AssertionError("forbidden access attempted")

        with mock.patch.object(socket, "socket", refuse), mock.patch.object(socket, "create_connection", refuse), mock.patch.object(
            socket, "getaddrinfo", refuse
        ), mock.patch.object(builtins, "open", refuse):
            extraction = DeterministicTestExtractor().extract(VALID_TEXT)
            result = validate_request(
                extraction.candidate,
                limits=ValidationLimits(max_quantity=100, max_text_length=50),
                source_text=VALID_TEXT,
                ambiguities=extraction.ambiguities,
            )
        self.assertTrue(result.is_valid)
        self.assertEqual(result.findings, ())


if __name__ == "__main__":
    unittest.main()
