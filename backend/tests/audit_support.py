"""Shared helpers for the audit-vocabulary tests (MOD-1b S3). Test only; never imported by the application.

The installed vocabulary is process-wide state. ``installed`` patches it for the length of a ``with`` block and restores it, and
``CleanAuditVocabularyMixin`` fails a test that starts with anything installed, so a leak from one test cannot hide in another.
"""

from contextlib import contextmanager
from unittest import mock

from apps.audit import events
from apps.audit.vocabulary import AuditVocabulary
from apps.modules.manifest import AuditActionDeclaration
from apps.modules.registry import ModuleRegistry
from config.audit_vocabulary import build_audit_vocabulary
from tests.module_support import manifest

DEMO_ACTION = "audit_demo.did_it"
DEMO_KEYS = ("count", "reason_code")


def audit_module(module_id="audit_demo", actions=((DEMO_ACTION, DEMO_KEYS),), **overrides):
    """A SYNTHETIC module manifest that declares audit actions (``(action, detail_keys)`` pairs). Tests only; no module is built in."""
    declared = tuple(AuditActionDeclaration(action, tuple(keys)) for action, keys in actions)
    return manifest(module_id=module_id, audit_actions=declared, **overrides)


def registry_of(*manifests) -> ModuleRegistry:
    return ModuleRegistry.build(manifests)


def vocabulary_of(*manifests) -> AuditVocabulary:
    return build_audit_vocabulary(registry_of(*manifests))


@contextmanager
def installed(vocabulary):
    """Make ``vocabulary`` the installed one inside the block, then put back whatever was there (nothing, in a clean test)."""
    with mock.patch.object(events, "_installed", vocabulary):
        yield vocabulary


def demo_vocabulary() -> AuditVocabulary:
    return vocabulary_of(audit_module())


class CleanAuditVocabularyMixin:
    """Mix in first. Every test starts with no vocabulary installed and leaves none behind."""

    def setUp(self):
        super().setUp()
        self.assertIsNone(events._installed, "a previous test leaked an installed audit vocabulary")
        patcher = mock.patch.object(events, "_installed", None)
        patcher.start()
        self.addCleanup(patcher.stop)
