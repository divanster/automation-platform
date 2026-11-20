"""The managed document storage: a directory on a local disk, outside Git and outside every static or media path, holding original documents under random names. Standard library only.

LAYOUT. ``<root>/incoming/`` holds an upload while it is received and checked (``<random>.partial``); ``<root>/objects/<2 hex>/<32 hex>`` holds an accepted document. The root is an explicit
absolute directory that an operator prepared (``prepare_directory``, run by ``manage.py prepare_document_storage``); there is no default, no user-supplied path and no fallback, so an
unconfigured installation fails closed (``document_storage_disabled``). The root must not be, or be inside, a Git working tree, the application's source tree, or a static or media root.

NO USER PATH EVER. A file's name on disk is a fresh random 128-bit key (``rules.new_storage_key``) chosen by this module; the client's file name, content type and owner never reach a path.
A key read back from the database is checked against the exact key shape before it is joined to the objects directory, and the joined path is confirmed to stay inside it (path traversal is
impossible both by construction and by check). Symbolic links, junctions and hard links are refused wherever the storage looks.

RESTRICTIVE PERMISSIONS come from the injected ``DirectoryPolicy``: on POSIX the root must be a directory owned by the process, mode ``0700``, and every file is created ``0600`` and checked through
its open descriptor (owner, no group or other access); on Windows the composition root supplies the same hardened policy the secret store uses (a protected DACL for the current identity,
SYSTEM and Administrators, verified on every operation).

NO PLAINTEXT OUTSIDE THE PROTECTED DIRECTORY. An upload is written straight from the request stream into ``incoming/`` (never into the system temporary directory, never fully in memory),
hashed and counted as it arrives, checked and only then installed under its final key. The size limit is enforced while receiving, so a body larger than declared or allowed stops at the limit.

ATOMIC INSTALL. The checked file is made visible under its final name with ``os.link`` (which refuses an existing name, so nothing is ever overwritten) and the ``incoming`` name is then removed:
a reader sees either no object or the complete one. The file's content is flushed to disk first. The bytes are stored in plaintext (no encryption at rest); the directory's permissions are the
protection, and backup and encryption at rest are operator concerns (docs/architecture/document-storage.md).

FAILURE. Every failure is a ``DocumentRejectedError`` (the upload is not acceptable) or a ``DocumentStorageError`` (the storage cannot be used), each with a fixed message: never a path, a name or
content. A failed or rejected upload leaves nothing in ``incoming/`` or ``objects/``.

LIMITS. No locking is needed (every upload has its own random names). A crash can leave a ``.partial`` file in ``incoming/`` or an object no row refers to; both stay inside the protected
directory and an operator may delete them while no upload is running. There is no virus scan, no quota per user and no re-verification of the hash on download (the size is checked).
"""

import hashlib
import os
import stat
from abc import ABC, abstractmethod
from typing import NamedTuple

from . import pdf
from .errors import (
    CODE_OBJECT_MISSING,
    CODE_UNAVAILABLE,
    REASON_EMPTY,
    REASON_LENGTH_MISMATCH,
    REASON_TOO_LARGE,
    DocumentRejectedError,
    DocumentStorageError,
)
from .rules import is_storage_key, is_valid_max_bytes, new_storage_key

CHUNK_BYTES = 64 * 1024
INCOMING_DIR = "incoming"
OBJECTS_DIR = "objects"
PARTIAL_SUFFIX = ".partial"
_REPARSE_POINT = 0x400
_INSTALL_ATTEMPTS = 4
_O_BINARY = getattr(os, "O_BINARY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class StoredObject(NamedTuple):
    key: str
    size_bytes: int
    sha256: str


def _unavailable() -> DocumentStorageError:
    return DocumentStorageError(CODE_UNAVAILABLE)


def _is_link(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _make_directory(path: str) -> None:
    """Create ONE directory that is private to this process. On POSIX that is mode ``0700``. On Windows NO mode is passed on purpose: since Python 3.13 ``os.mkdir(path, 0o700)`` there replaces the
    inherited ACL with its own (SYSTEM, Administrators and ``OWNER RIGHTS``), which the directory policy would then rightly refuse; without a mode the directory inherits the protected ACL of the
    storage root, which is exactly the restriction the policy verifies."""
    if os.name == "posix":
        os.mkdir(path, 0o700)
    else:
        os.mkdir(path)


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:  # different drives, or a mix of absolute and relative
        return False


def reject_links(path: str, *, leaf: bool = True) -> None:
    """Refuse a path with a symbolic link, junction or other reparse point on the way from the root down to ``path`` (``leaf``: including ``path`` itself)."""
    probe, chain = path, []
    while True:
        chain.append(probe)
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    for item in chain if leaf else chain[1:]:
        try:
            info = os.lstat(item)
        except OSError:
            raise _unavailable() from None
        if _is_link(info):
            raise _unavailable()


def checked_directory(directory, forbidden=()) -> str:
    """An absolute, normalized directory path that is not in a Git working tree and not inside (or equal to) a ``forbidden`` root; anything else is ``document_storage_unavailable``."""
    try:
        path = os.fspath(directory)
    except TypeError:
        raise _unavailable() from None
    if not isinstance(path, str) or not path or "\x00" in path or not os.path.isabs(path):
        raise _unavailable()
    path = os.path.abspath(path)
    if path.startswith("\\\\"):
        raise _unavailable()  # a network or device path: refused before any file system call can reach the share
    for root in forbidden:
        if root and _inside(path, os.path.abspath(os.fspath(root))):
            raise _unavailable()
    probe = path
    while True:
        if os.path.lexists(os.path.join(probe, ".git")):
            raise _unavailable()
        parent = os.path.dirname(probe)
        if parent == probe:
            return path
        probe = parent


def check_open_file(descriptor: int) -> os.stat_result:
    """The open file must be a regular file with exactly one name (no hard link to or from elsewhere) and no reparse point. Returns its ``fstat``."""
    try:
        info = os.fstat(descriptor)
    except OSError:
        raise _unavailable() from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
        raise _unavailable()
    return info


class DirectoryPolicy(ABC):
    """Creates the storage root with restrictive permissions and verifies, on demand, that they still hold. Every method raises ``DocumentStorageError`` on any problem."""

    @abstractmethod
    def create(self, path: str) -> None: ...

    @abstractmethod
    def verify(self, path: str) -> None: ...

    @abstractmethod
    def verify_file(self, descriptor: int) -> None:
        """Checks the file behind an OPEN descriptor (owner and permissions), so a read or write acts on exactly the file that was checked."""


class PosixDirectoryPolicy(DirectoryPolicy):
    """The root must be a real directory owned by this process with no group or other access (``0700``); a file must be owned by this process with no group or other access (``0600``)."""

    def __init__(self):
        if not hasattr(os, "geteuid"):
            raise _unavailable()

    def create(self, path: str) -> None:
        try:
            os.mkdir(path, 0o700)
        except OSError:
            raise _unavailable() from None
        try:
            self.verify(path)
        except DocumentStorageError:
            try:
                os.rmdir(path)
            except OSError:
                pass
            raise

    def verify(self, path: str) -> None:
        try:
            info = os.lstat(path)
        except OSError:
            raise _unavailable() from None
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise _unavailable()

    def verify_file(self, descriptor: int) -> None:
        try:
            info = os.fstat(descriptor)
        except OSError:
            raise _unavailable() from None
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise _unavailable()


def prepare_directory(directory, policy: DirectoryPolicy, forbidden=()) -> None:
    """The operator's explicit set-up, repeatable. A missing root is created (its parent must exist and be free of links) with the policy's restrictive permissions; an EXISTING root is
    never changed, only verified. ``incoming`` and ``objects`` are created inside it when missing."""
    if not isinstance(policy, DirectoryPolicy):
        raise _unavailable()
    root = checked_directory(directory, forbidden)
    if os.path.lexists(root):
        reject_links(root)
        policy.verify(root)
    else:
        reject_links(os.path.dirname(root))
        policy.create(root)
    for name in (INCOMING_DIR, OBJECTS_DIR):
        path = os.path.join(root, name)
        try:
            _make_directory(path)
        except FileExistsError:
            pass
        except OSError:
            raise _unavailable() from None
    DocumentStore(root, policy, max_bytes=1024, forbidden=forbidden)  # the finished layout must pass the same checks a running process makes


class DocumentStream:
    """The open stored file as an iterable of chunks with a ``close`` (so Django's streaming response closes it, even if iteration never started). Not seekable by the caller; one pass."""

    def __init__(self, handle, size_bytes: int):
        self._handle = handle
        self.size_bytes = size_bytes

    def __iter__(self):
        while True:
            chunk = self._handle.read(CHUNK_BYTES)
            if not chunk:
                return
            yield chunk

    def close(self) -> None:
        self._handle.close()


class DocumentStore:
    """The filesystem storage of original documents. ``policy`` decides what restrictive means on this platform; ``max_bytes`` is the size limit of one document."""

    backend_id = "filesystem"

    def __init__(self, directory, policy: DirectoryPolicy, *, max_bytes: int, forbidden=()):
        if not isinstance(policy, DirectoryPolicy) or not is_valid_max_bytes(max_bytes):
            raise _unavailable()
        self._root = checked_directory(directory, forbidden)
        self._policy = policy
        self.max_bytes = max_bytes
        self._incoming = os.path.join(self._root, INCOMING_DIR)
        self._objects = os.path.join(self._root, OBJECTS_DIR)
        self._ready()

    def _ready(self) -> None:
        """The root is a trusted directory and ``incoming`` and ``objects`` are real directories inside it. Checked on every operation, so a swapped or loosened directory is noticed."""
        reject_links(self._root)
        self._policy.verify(self._root)
        for path in (self._incoming, self._objects):
            try:
                info = os.lstat(path)
            except OSError:
                raise _unavailable() from None
            if not stat.S_ISDIR(info.st_mode) or _is_link(info):
                raise _unavailable()

    def _path_for(self, key: str) -> str:
        if not is_storage_key(key):
            raise _unavailable()
        shard, name = key.split("/")
        path = os.path.join(self._objects, shard, name)
        if not _inside(path, self._objects) or os.path.dirname(os.path.dirname(path)) != self._objects:
            raise _unavailable()
        return path

    # --- receive -------------------------------------------------------------------------------------------

    def ingest(self, read, declared_length: int) -> StoredObject:
        """Receive ``declared_length`` bytes from ``read(n)`` into ``incoming/``, check them and install them under a new random key.

        ``read`` is the request stream's ``read``; it may return fewer bytes than asked and returns ``b""`` at the end. Rejections (``DocumentRejectedError``): ``too_large``, ``length_mismatch`` (fewer or
        more bytes than declared, or the stream failed), ``not_a_pdf``, ``truncated_pdf``, ``malformed_pdf``. Nothing remains on disk after a failure.
        """
        self._ready()
        temporary = os.path.join(self._incoming, new_storage_key().split("/")[1] + PARTIAL_SUFFIX)
        descriptor = self._create_file(temporary)
        try:
            try:
                size, digest = self._receive(descriptor, read, declared_length)
                self._check_pdf(descriptor, size)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            key = self._install(temporary)
        except BaseException as error:
            self._remove(temporary)
            if isinstance(error, (DocumentRejectedError, DocumentStorageError)) or not isinstance(error, OSError):
                raise
            raise _unavailable() from None
        return StoredObject(key, size, digest)

    def _create_file(self, path: str) -> int:
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | _O_BINARY | _O_NOFOLLOW, 0o600)
        except OSError:
            raise _unavailable() from None
        try:
            check_open_file(descriptor)
            self._policy.verify_file(descriptor)
        except BaseException:
            os.close(descriptor)
            self._remove(path)
            raise
        return descriptor

    def _receive(self, descriptor: int, read, declared_length: int):
        digest = hashlib.sha256()
        head = b""
        total = 0
        while total <= declared_length:
            try:
                chunk = read(min(CHUNK_BYTES, declared_length + 1 - total))
            except OSError:
                raise DocumentRejectedError(REASON_LENGTH_MISMATCH) from None  # the client went away or the stream broke
            if not chunk:
                break
            chunk = bytes(chunk)
            total += len(chunk)
            if total > self.max_bytes:
                raise DocumentRejectedError(REASON_TOO_LARGE)
            if total > declared_length:
                raise DocumentRejectedError(REASON_LENGTH_MISMATCH)
            if len(head) < pdf.HEADER_BYTES:
                head = (head + chunk)[: pdf.HEADER_BYTES]
                if len(head) >= pdf.HEADER_BYTES:
                    pdf.check_header(head)  # garbage is refused after its first bytes, before more is written
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                view = view[os.write(descriptor, view) :]
        if total != declared_length:
            raise DocumentRejectedError(REASON_LENGTH_MISMATCH)
        if total == 0:
            raise DocumentRejectedError(REASON_EMPTY)
        return total, digest.hexdigest()

    @staticmethod
    def _check_pdf(descriptor: int, size: int) -> None:
        def read_at(offset: int, length: int) -> bytes:
            os.lseek(descriptor, offset, os.SEEK_SET)
            out = b""
            while len(out) < length:
                piece = os.read(descriptor, length - len(out))
                if not piece:
                    break
                out += piece
            return out

        pdf.validate_pdf(read_at, size)

    def _install(self, temporary: str) -> str:
        for _ in range(_INSTALL_ATTEMPTS):
            key = new_storage_key()
            final = self._path_for(key)
            shard = os.path.dirname(final)
            try:
                _make_directory(shard)
            except FileExistsError:
                pass
            if _is_link(os.lstat(shard)):
                raise _unavailable()
            try:
                os.link(temporary, final)  # refuses an existing name: nothing is ever overwritten
            except FileExistsError:
                continue
            try:
                os.unlink(temporary)
            except OSError:
                self._remove(final)
                raise
            self._sync_directory(shard)
            return key
        raise _unavailable()

    @staticmethod
    def _sync_directory(path: str) -> None:
        if os.name != "posix":
            return  # Windows cannot open a directory for fsync; NTFS journals the metadata
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    @staticmethod
    def _remove(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    # --- read and delete -----------------------------------------------------------------------------------

    def open(self, key: str, expected_size: int) -> DocumentStream:
        """The stored object as a stream, after checking it is exactly one regular file owned as the policy requires and has the size the metadata recorded; ``document_object_missing`` otherwise."""
        self._ready()
        path = self._path_for(key)
        try:
            descriptor = os.open(path, os.O_RDONLY | _O_BINARY | _O_NOFOLLOW)
        except FileNotFoundError:
            raise DocumentStorageError(CODE_OBJECT_MISSING) from None
        except OSError:
            raise _unavailable() from None
        try:
            info = check_open_file(descriptor)
            self._policy.verify_file(descriptor)
            if info.st_size != expected_size:
                raise DocumentStorageError(CODE_OBJECT_MISSING)
            handle = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
        return DocumentStream(handle, expected_size)

    def delete(self, key: str) -> bool:
        """Remove an object by key (the cleanup of an upload whose database transaction failed). ``False`` when there was none."""
        self._ready()
        path = self._path_for(key)
        try:
            os.unlink(path)
        except FileNotFoundError:
            return False
        except OSError:
            raise _unavailable() from None
        return True
