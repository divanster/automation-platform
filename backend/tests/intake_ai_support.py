"""Helpers for the AI-2a tests (AI extraction for Request Intake). DB-free; no network, no real credential.

The AI provider is the REAL Workbench adapter behind a FAKE HTTP transport (``tests.ai_workbench_support``), so a test exercises the production path
(composition, gateway, policy, adapter, schema validation, conversion) and only the socket is replaced. No test here, and no code in the development
VM, contacts the Workbench.
"""

import json

from apps.intake.policy import KIND_EXTRACT
from tests.ai_gateway_support import SECRET_SENTINEL
from tests.ai_workbench_support import FakeTransport, answer, status
from tests.intake_support import VALID_TEXT

__all__ = ["VALID_TEXT", "SECRET_SENTINEL", "FakeTransport", "answer", "status", "ai_environ", "module_gate", "output_for", "VALID_OUTPUT", "answer_with", "TEXT_SENTINEL"]

TEXT_SENTINEL = "REQUEST-TEXT-SENTINEL-c41d"

# What a well-behaved model returns for VALID_TEXT.
VALID_OUTPUT = {
    "requester": {"name": "Jane Doe", "contact": "jane@example.invalid"},
    "items": [
        {"description": "A4 paper", "quantity": "10", "unit": "boxes", "notes": "for the print room"},
        {"description": "Stapler", "quantity": "2", "unit": None, "notes": None},
    ],
    "notes": "Needed this week.",
    "ambiguities": [],
}


def output_for(**changes):
    """A copy of ``VALID_OUTPUT`` with the top-level keys in ``changes`` replaced (a key set to the marker ``DROP`` is removed)."""
    document = json.loads(json.dumps(VALID_OUTPUT))
    for key, value in changes.items():
        if value is DROP:
            document.pop(key, None)
        else:
            document[key] = value
    return document


DROP = object()


def answer_with(document, **kwargs):
    """A Workbench HTTP answer whose message content is the JSON text of ``document`` (or ``document`` itself when it is a string)."""
    return answer(document if isinstance(document, str) else json.dumps(document), **kwargs)


def ai_environ(**overrides):
    """A complete, valid environment for ai mode. A value of ``None`` removes the variable."""
    values = {
        "AP_INTAKE_EXTRACTION_MODE": "ai",
        "AP_AI_POLICY_MODE": "company_endpoint",
        "AP_AI_WORKBENCH_API_KEY": SECRET_SENTINEL,
    }
    values.update(overrides)
    return {name: value for name, value in values.items() if value is not None}


def module_gate(*kinds):
    """A kind gate answering ``kinds`` (default: Request Intake's kind), like ``Composition.kind_gate``."""
    answer_kinds = frozenset(kinds) if kinds else frozenset({KIND_EXTRACT})
    return lambda: answer_kinds
