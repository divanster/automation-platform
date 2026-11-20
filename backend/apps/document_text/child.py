"""The isolated extraction process: ``python -s -P -B child.py <max_pages> <max_page_chars> <max_total_chars> <max_bytes> <memory_bytes> <timeout_seconds>``.

The parent (``isolation.py``) starts it with a minimal environment (none of the web or worker process's variables: no database settings, no secret, no ``AP_*`` value), writes the PDF to its standard input and reads
ONE answer from its standard output. It puts the operating-system limits on itself first (``child_limits``), reads at most ``max_bytes`` + 1 bytes, extracts and writes the result (``rules.encode_child_output``).
It never opens a file, never touches the network, the database or Django and writes nothing to standard error on purpose. Exit code 0: an answer was written; 2: a ``fatal`` answer was written; anything else:
no usable answer (the parent treats it as ``extractor_failed``).

This file is a script, not a module to import from the application: it adds the backend directory to ``sys.path`` so it can import the two pure modules it needs.
"""

import os
import sys

_BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from apps.document_text import child_limits, rules  # noqa: E402


def run(argv, stdin, stdout) -> int:
    try:
        max_pages, max_page_chars, max_total_chars, max_bytes, memory_bytes, timeout_seconds = (int(value) for value in argv)
        limits = rules.ExtractionLimits(
            max_bytes=max_bytes, max_pages=max_pages, max_page_chars=max_page_chars, max_total_chars=max_total_chars, timeout_seconds=timeout_seconds, memory_bytes=memory_bytes
        )
    except (ValueError, TypeError):
        return 64
    try:
        child_limits.apply_limits(limits.memory_bytes, limits.timeout_seconds)
    except child_limits.LimitsUnavailableError:
        stdout.write(rules.encode_fatal(rules.FATAL_SANDBOX_UNAVAILABLE))
        stdout.flush()
        return 2
    try:
        from apps.document_text import pdf_text

        data = stdin.read(limits.max_bytes + 1)
        if len(data) > limits.max_bytes:
            output = rules.failed_output(rules.ERR_DOCUMENT_TOO_LARGE, extractor_version=pdf_text.library_version())
        else:
            output = pdf_text.extract_pdf_text(data, limits)
        answer = rules.encode_child_output(output)
    except MemoryError:
        answer = None
    if answer is None:
        stdout.write(rules.encode_fatal(rules.FATAL_RESOURCE_LIMIT))
        stdout.flush()
        return 2
    stdout.write(answer)
    stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:], sys.stdin.buffer, sys.stdout.buffer))
