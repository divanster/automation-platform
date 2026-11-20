"""Helpers for the DB-free tests of document storage (DOC-1): real, valid PDFs built in memory, a real store in a temporary directory behind a recording policy, and stand-ins for the
services' database touch points.

``apps.documents.services`` and ``apps.documents.storage`` are exercised for real; only ``load_actor`` (a database read), ``Document`` (the table), ``record_event`` and ``transaction`` (an
audit write in a simulated atomic block with rollback, ``tests/ai_settings_support.FakeStore``) are replaced. What needs a real database (the CHECK constraints, a true rollback, the migrations,
concurrent transactions) is in ``tests/test_documents_postgres.py`` and is NOT run in the development VM. Every document here is synthetic; nothing is sent anywhere.
"""

import contextlib
import datetime
import os
import shutil
import tempfile
import uuid
from types import SimpleNamespace
from unittest import mock

from apps.accounts.authz import Actor
from apps.accounts import roles
from apps.documents import services
from apps.documents.storage import DirectoryPolicy, DocumentStore, prepare_directory
from tests.ai_settings_support import FakeStore

OWNER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
OTHER_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
SENTINEL_NAME = "SECRET-NAME-5d1f"  # a file name that must never reach a log line, an audit event or a path
SENTINEL_TEXT = b"CONTENT-SENTINEL-a82c"  # a content marker that must never reach a log line or an audit event


def make_pdf(padding: int = 0, marker: bytes = b"") -> bytes:
    """A small, structurally complete PDF with a classic cross-reference table. ``padding`` adds that many bytes of stream data (to reach a size); ``marker`` is embedded content."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
    ]
    if padding or marker:
        data = marker + b"\x00" * padding
        objects.append(b"<< /Length " + str(len(data)).encode() + b" >>\nstream\n" + data + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def make_xref_stream_pdf() -> bytes:
    """A PDF 1.5 style file whose cross-reference is a stream object (no ``trailer`` keyword)."""
    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\n")
    xref = len(out)
    out += b"2 0 obj\n<< /Type /XRef /Size 3 /W [1 2 1] /Root 1 0 R /Length 0 >>\nstream\n\nendstream\nendobj\n"
    out += f"startxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


VALID_PDF = make_pdf()


class Reader:
    """A request stream stand-in: ``read(n)`` returns at most ``n`` bytes, at most ``step`` at a time (a socket returns short reads)."""

    def __init__(self, data: bytes, step: int | None = None, fail_after: int | None = None):
        self.data, self.step, self.fail_after, self.position = data, step, fail_after, 0
        self.calls = 0

    def read(self, size: int) -> bytes:
        self.calls += 1
        if self.fail_after is not None and self.position >= self.fail_after:
            raise OSError("the client went away")
        size = size if self.step is None else min(size, self.step)
        chunk = self.data[self.position : self.position + size]
        self.position += len(chunk)
        return chunk


class PermissivePolicy(DirectoryPolicy):
    """A recording policy that accepts everything (the platform's real policy is tested separately): so the storage logic runs the same on every platform."""

    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def _record(self, name):
        self.calls.append(name)
        if self.fail_on == name:
            from apps.documents.errors import CODE_UNAVAILABLE, DocumentStorageError

            raise DocumentStorageError(CODE_UNAVAILABLE)

    def create(self, path):
        self._record("create")
        os.mkdir(path)

    def verify(self, path):
        self._record("verify")

    def verify_file(self, descriptor):
        self._record("verify_file")


@contextlib.contextmanager
def temporary_root():
    """A fresh directory under the system temporary directory (outside the repository), removed afterwards."""
    parent = tempfile.mkdtemp(prefix="ap-doc-test-")
    try:
        yield os.path.join(parent, "documents")
    finally:
        shutil.rmtree(parent, ignore_errors=True)


@contextlib.contextmanager
def temporary_store(max_bytes=20 * 1024 * 1024, policy=None):
    """A real ``DocumentStore`` in a temporary directory prepared the way the operator's command prepares it."""
    policy = PermissivePolicy() if policy is None else policy
    with temporary_root() as root:
        prepare_directory(root, policy)
        yield DocumentStore(root, policy, max_bytes=max_bytes), root


def tree_listing(root):
    """Every file under ``root`` as relative paths, sorted (to assert nothing was left behind)."""
    found = []
    for folder, _, names in os.walk(root):
        found += [os.path.relpath(os.path.join(folder, name), root) for name in names]
    return sorted(found)


# --- the services' database touch points ------------------------------------------------------------------


def actor(*, pk=7, public_id=OWNER_ID, permissions=(), is_superuser=False):
    return Actor(user=SimpleNamespace(pk=pk, public_id=public_id), pk=pk, public_id=public_id, is_superuser=is_superuser, permissions=frozenset(permissions))


OPERATOR = actor(permissions=roles.DOCUMENT_ROLE_PERMISSIONS[roles.OPERATOR])
OTHER_OPERATOR = actor(pk=8, public_id=OTHER_ID, permissions=roles.DOCUMENT_ROLE_PERMISSIONS[roles.OPERATOR])
APPROVER = actor(pk=9, public_id=uuid.UUID("44444444-4444-4444-8444-444444444444"), permissions=roles.DOCUMENT_ROLE_PERMISSIONS[roles.APPROVER])
AUDITOR = actor(pk=10, public_id=uuid.UUID("55555555-5555-4555-8555-555555555555"), permissions=roles.DOCUMENT_ROLE_PERMISSIONS[roles.AUDITOR])
NOBODY = actor(pk=11, public_id=uuid.UUID("66666666-6666-4666-8666-666666666666"))
ROOT = actor(pk=12, public_id=uuid.UUID("77777777-7777-4777-8777-777777777777"), is_superuser=True)


class FakeQuerySet:
    def __init__(self, rows):
        self.rows = list(rows)

    def select_related(self, *names):
        return self

    def filter(self, **conditions):
        return FakeQuerySet(row for row in self.rows if all(getattr(row, name) == value for name, value in conditions.items()))

    def order_by(self, *names):
        rows = list(self.rows)
        for name in reversed(names):
            rows.sort(key=lambda row: getattr(row, name.lstrip("-")), reverse=name.startswith("-"))
        return FakeQuerySet(rows)

    def count(self):
        return len(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def __getitem__(self, item):
        return self.rows[item]


class FakeDocuments:
    """The ``documents_document`` table. ``fail_create`` makes the insert raise (a constraint, a lost connection)."""

    def __init__(self, fail_create=None):
        self.rows = []
        self.fail_create = fail_create
        self.clock = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        self.objects = self

    def create(self, **fields):
        if self.fail_create is not None:
            raise self.fail_create
        owner = fields.pop("owner")
        self.clock += datetime.timedelta(seconds=1)
        row = SimpleNamespace(pk=fields["id"], owner=owner, owner_id=owner.pk, uploaded_at=self.clock, processing_state="uploaded", **fields)
        self.rows.append(row)
        return row

    def add(self, owner, **fields):
        """A row as if uploaded earlier (``owner`` an Actor)."""
        key = fields.pop("storage_key")
        return self.create(id=uuid.uuid4(), owner=owner.user, original_filename=fields.pop("original_filename", "a.pdf"), content_type="application/pdf",
                           size_bytes=fields.pop("size_bytes"), sha256=fields.pop("sha256"), storage_key=key)  # fmt: skip

    def select_related(self, *names):
        return FakeQuerySet(self.rows)

    def snapshot(self):
        return list(self.rows)


class FakeDatabase(FakeStore):
    """``FakeStore`` (the audit events and the simulated transaction) plus the document table. A rollback also drops the rows inserted in the block."""

    def __init__(self, documents=None, audit_error=None):
        super().__init__(audit_error=audit_error)
        self.documents = FakeDocuments() if documents is None else documents

    @contextlib.contextmanager
    def atomic(self):
        outermost = self.depth == 0
        saved_rows = self.documents.snapshot() if outermost else None
        try:
            with super().atomic():
                yield
        except BaseException:
            if outermost:
                self.documents.rows[:] = saved_rows
            raise


@contextlib.contextmanager
def patched_documents(db, who, atomic=None):
    """Run with the services' database touch points replaced. ``who`` is the Actor ``load_actor`` returns, or an exception to raise. ``atomic`` replaces the simulated transaction (an ambiguous commit)."""
    lookup = mock.Mock(side_effect=who) if isinstance(who, BaseException) else mock.Mock(return_value=who)
    with (
        mock.patch.object(services, "load_actor", lookup),
        mock.patch.object(services, "Document", SimpleNamespace(objects=db.documents)),
        mock.patch.object(services, "record_event", db.record_event),
        mock.patch.object(services, "transaction") as tx,
    ):
        tx.atomic.side_effect = db.atomic if atomic is None else atomic
        yield lookup
