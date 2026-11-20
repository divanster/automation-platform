"""Windows DPAPI-backed secret store (AI-SEC-1b). Standard library only (``ctypes`` for the Windows APIs). Not wired into settings, the API or any provider.

DESIGN. Encryption is done ONLY by the operating system: ``CryptProtectData`` and ``CryptUnprotectData`` (crypt32) with the CURRENT-USER scope (the ``CRYPTPROTECT_LOCAL_MACHINE`` flag is
never set) and ``CRYPTPROTECT_UI_FORBIDDEN``. This module designs no cipher, key, IV, MAC or key store. The only addition is DPAPI's own *optional entropy* parameter, set to a fixed
application tag plus the reference, so a ciphertext file copied under another reference does not decrypt.

LAYOUT. One file per reference in an explicit directory the caller names (there is no default and no environment lookup). The file name is ``sha256(tag + reference).hexdigest() + ".dpapi"``:
opaque, fixed length, no secret, no path characters. The content is a 5-byte format tag followed by the DPAPI blob; the plaintext never touches the disk, not even in a temporary file.

WRITES. The ciphertext is written to a new ``.<random>.partial`` file in the same directory, flushed, then installed with an atomic rename (create: ``os.rename``, which refuses an
existing target on Windows; replace: ``os.replace``). A failure removes the partial file and leaves the previous file untouched.

CONCURRENCY (AI-SEC-1c). Every operation runs under a cross-process lock on ``.store.lock`` in the store directory, taken with the Windows ``LockFileEx`` API on a handle opened by that
operation: SHARED for ``get`` and ``exists``, EXCLUSIVE for ``put`` and ``delete``. Readers therefore never meet a rename or an unlink, writers are serialized, and the existence check and the
install of a create, a replace or a delete are one critical section (no lost update, no resurrection of a deleted secret by a late replace). The lock belongs to the handle, so the operating
system releases it when the process exits or is killed; there is no lock to clean up. Waiting is bounded by one per-operation deadline (default 5 s): an expired wait is ``secret_store_busy``.
DPAPI calls on a write run before the lock is taken. A sharing violation from a third party (an antivirus scanner, an indexer) is retried inside the same deadline. Because the exclusive lock
means no other writer is mid-write, a writer first removes every ``.<random>.partial`` file, which is how an interrupted write is cleaned up.

DIRECTORY AND FILES (AI-SEC-1d). The directory must be absolute on a local fixed volume that keeps ACLs (no UNC, device or extended-length path, no removable, network or FAT volume, no cloud-sync folder
name), with no symbolic link, junction or other reparse point anywhere in its path. ``prepare_windows_secret_directory`` is all or nothing: it validates the identity and the location first, creates
only the directory itself (its parent must exist) with a protected DACL for the current identity alone, verifies it and removes it again if that fails; it never changes or adopts an existing
directory that is not already trusted. Every operation re-verifies that the directory is owned by the current identity, SYSTEM or Administrators and that its DACL is protected and allows only the
current identity, SYSTEM and Administrators; trustees are compared as canonical SIDs, so aliases such as SY or NS cannot cause a false mismatch. Every secret file is opened first and then checked
through its OPEN handle: a regular file with exactly one name and no reparse point, a trusted owner and a trusted ACL, before a byte of it is read.

IDENTITY. The store runs only as an ordinary, Azure AD or virtual service account, or as a built-in service identity (LocalSystem, LocalService, NetworkService) that the operator names in
``approved_identity_sids``. Anything else fails closed before any directory or file is touched.

FAILURE. Every failure is a ``SecretStoreError`` with a fixed message. Wrong Windows identity, a damaged or truncated file, a swapped file and an oversized file are all ``secret_unreadable``;
an unusable directory, an unsupported platform or any I/O error is ``secret_store_unavailable``; a lock or a sharing violation that outlasts the deadline is ``secret_store_busy``. Nothing falls back to the environment or another store.

LIMITS. DPAPI protects against other accounts and offline copying of the files; it does NOT protect against code running as the same Windows user (such code can call
``CryptUnprotectData`` itself). Memory is not erased beyond the ``ctypes`` buffers this module zeroes. The lock is advisory for this module's own operations: a process that bypasses the store
and touches the files directly is not excluded. Shared readers can delay a writer for as long as readers keep arriving; there is no fairness guarantee beyond the deadline.
"""

import contextlib
import ctypes
import hashlib
import os
import re
import secrets
import stat
import sys
import threading
import time
from abc import ABC, abstractmethod

from apps.secret_store import (
    CODE_ALREADY_EXISTS,
    CODE_BUSY,
    CODE_NOT_FOUND,
    CODE_UNAVAILABLE,
    CODE_UNREADABLE,
    SecretStore,
    SecretStoreError,
    SecretValue,
)

FORMAT_TAG = b"APSS\x01"
ENTROPY_TAG = b"AP-SECRET-STORE-V1\x00"
FILE_SUFFIX = ".dpapi"
PARTIAL_SUFFIX = ".partial"
LOCK_FILE_NAME = ".store.lock"
MAX_FILE_BYTES = 65536
DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_TIMEOUT_SECONDS = 60.0
_REPARSE_POINT = 0x400
_PARTIAL_NAME = re.compile(r"\.[0-9a-f]{24}\.partial")
_ERROR_ACCESS_DENIED = 5
_ERROR_SHARING_VIOLATION = 32
_ERROR_LOCK_VIOLATION = 33
_TRANSIENT_WINERRORS = (_ERROR_ACCESS_DENIED, _ERROR_SHARING_VIOLATION)  # a delete-pending or scanned file reports either
_FIRST_POLL, _LAST_POLL = 0.002, 0.025


def file_name_for(reference: str) -> str:
    """The opaque file name for ``reference`` (already validated by the caller)."""
    return hashlib.sha256(ENTROPY_TAG + reference.encode("ascii")).hexdigest() + FILE_SUFFIX


def entropy_for(reference: str) -> bytes:
    return ENTROPY_TAG + reference.encode("ascii")


class Protector(ABC):
    """Encrypts and decrypts one blob for the current identity. ``unprotect`` must raise on any failure; it must never return partial data."""

    @abstractmethod
    def protect(self, data: bytes, entropy: bytes) -> bytes: ...

    @abstractmethod
    def unprotect(self, blob: bytes, entropy: bytes) -> bytes: ...


class DirectoryPolicy(ABC):
    """Creates the storage directory with restrictive permissions and verifies, on demand, that they still hold. Both raise ``SecretStoreError`` on any problem."""

    @abstractmethod
    def create(self, path: str) -> None: ...

    @abstractmethod
    def verify(self, path: str) -> None: ...

    @abstractmethod
    def verify_file(self, descriptor: int) -> None:
        """Checks the file behind an OPEN descriptor (owner and ACL) so a read or write acts on exactly the file that was checked."""


def reject_links(path: str, *, leaf: bool = True) -> None:
    """Refuses a path whose components, from the root down to ``path`` itself when ``leaf`` is set, include a symbolic link, a junction or any other reparse point. Fixed error, no path in it."""
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
            raise SecretStoreError(CODE_UNAVAILABLE) from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
            raise SecretStoreError(CODE_UNAVAILABLE)


def check_open_file(descriptor: int) -> None:
    """The open file must be a regular file with exactly one name (no hard link to or from elsewhere) and no reparse point."""
    try:
        info = os.fstat(descriptor)
    except OSError:
        raise SecretStoreError(CODE_UNAVAILABLE) from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
        raise SecretStoreError(CODE_UNAVAILABLE)


class DirectoryLocker(ABC):
    """A bounded, exclusive or shared lock on one store directory. ``hold`` is a context manager: it returns when the lock is held and releases it on exit, however the body ends. It raises
    ``secret_store_busy`` when ``timeout`` seconds pass without getting it and ``secret_store_unavailable`` for any other failure. Exclusive holders exclude everyone; shared holders exclude
    only exclusive ones."""

    @abstractmethod
    def hold(self, directory: str, exclusive: bool, timeout: float): ...


class InProcessLocker(DirectoryLocker):
    """One lock object per instance, shared and exclusive alike: it serializes threads of ONE process only. For tests with a fake protector; the Windows store never uses it."""

    def __init__(self):
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def hold(self, directory, exclusive, timeout):
        if not self._lock.acquire(timeout=max(0.0, timeout)):
            raise SecretStoreError(CODE_BUSY)
        try:
            yield
        finally:
            self._lock.release()


def _pause(delay: float) -> float:
    time.sleep(delay)
    return min(_LAST_POLL, delay * 2)


def checked_directory(directory) -> str:
    """An absolute, normalized directory path that is not inside a Git working tree; anything else is ``secret_store_unavailable`` (the path is never put in a message)."""
    try:
        path = os.fspath(directory)
    except TypeError:
        raise SecretStoreError(CODE_UNAVAILABLE) from None
    if not isinstance(path, str) or not path or "\x00" in path or not os.path.isabs(path):
        raise SecretStoreError(CODE_UNAVAILABLE)
    path = os.path.abspath(path)
    if sys.platform == "win32" and path.startswith("\\\\"):
        raise SecretStoreError(CODE_UNAVAILABLE)  # a network or device path: refused before any file system call can reach the share
    probe = path
    while True:
        if os.path.lexists(os.path.join(probe, ".git")):
            raise SecretStoreError(CODE_UNAVAILABLE)
        parent = os.path.dirname(probe)
        if parent == probe:
            return path
        probe = parent


class EncryptedFileSecretStore(SecretStore):
    """A ``SecretStore`` that keeps one protector-encrypted file per reference. The protector and directory policy are injected; ``WindowsDpapiSecretStore`` supplies the real ones."""

    def __init__(self, directory, protector: Protector, policy: DirectoryPolicy, locker: DirectoryLocker, *, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        if not isinstance(protector, Protector) or not isinstance(policy, DirectoryPolicy) or not isinstance(locker, DirectoryLocker):
            raise SecretStoreError(CODE_UNAVAILABLE)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_TIMEOUT_SECONDS:
            raise SecretStoreError(CODE_UNAVAILABLE)
        self._directory = checked_directory(directory)
        self._protector = protector
        self._policy = policy
        self._locker = locker
        self._timeout = float(timeout)

    def _deadline(self) -> float:
        return time.monotonic() + self._timeout

    def _hold(self, exclusive: bool, deadline: float):
        return self._locker.hold(self._directory, exclusive, max(0.0, deadline - time.monotonic()))

    @staticmethod
    def _retrying(deadline: float, action):
        """``action()``, retried while another process briefly has the file open (a scanner, an indexer). Anything else, or an expired deadline, is a fixed error."""
        delay = _FIRST_POLL
        while True:
            try:
                return action()
            except PermissionError as error:
                if getattr(error, "winerror", None) not in _TRANSIENT_WINERRORS:
                    raise SecretStoreError(CODE_UNAVAILABLE) from None
                if time.monotonic() >= deadline:
                    raise SecretStoreError(CODE_BUSY) from None
            delay = _pause(delay)

    def _ready(self) -> None:
        reject_links(self._directory)
        try:
            info = os.lstat(self._directory)
        except OSError:
            raise SecretStoreError(CODE_UNAVAILABLE) from None
        if not stat.S_ISDIR(info.st_mode):
            raise SecretStoreError(CODE_UNAVAILABLE)
        self._policy.verify(self._directory)

    def _path(self, reference: str) -> str:
        return os.path.join(self._directory, file_name_for(reference))

    @staticmethod
    def _present(path: str) -> bool:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return False
        except OSError:
            raise SecretStoreError(CODE_UNAVAILABLE) from None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
            raise SecretStoreError(CODE_UNAVAILABLE)
        return True

    def _sweep_partials(self) -> None:
        """Called only while holding the exclusive lock, when no writer can be mid-write: every partial file left in the directory is the debris of an interrupted write (ciphertext only)."""
        try:
            names = os.listdir(self._directory)
        except OSError:
            return
        for name in names:
            if _PARTIAL_NAME.fullmatch(name):
                try:
                    os.unlink(os.path.join(self._directory, name))
                except OSError:
                    pass

    def _put(self, reference, secret, replace):
        path = self._path(reference)
        deadline = self._deadline()
        self._ready()
        blob = FORMAT_TAG + self._protector.protect(secret.reveal().encode("utf-8"), entropy_for(reference))  # before the lock: it keeps the exclusive section short
        with self._hold(True, deadline):
            self._sweep_partials()
            present = self._present(path)
            if replace and not present:
                raise SecretStoreError(CODE_NOT_FOUND)
            if not replace and present:
                raise SecretStoreError(CODE_ALREADY_EXISTS)
            partial = os.path.join(self._directory, f".{secrets.token_hex(12)}{PARTIAL_SUFFIX}")
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
                descriptor = os.open(partial, flags, 0o600)
                try:
                    check_open_file(descriptor)
                    self._policy.verify_file(descriptor)
                    view = memoryview(blob)
                    while view:
                        view = view[os.write(descriptor, view) :]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                self._retrying(deadline, lambda: self._install(partial, path, replace))
            finally:
                try:
                    os.unlink(partial)
                except OSError:
                    pass

    @staticmethod
    def _install(partial: str, path: str, replace: bool) -> None:
        try:
            if replace:
                os.replace(partial, path)
            elif sys.platform == "win32":
                os.rename(partial, path)  # refuses an existing target on Windows
            else:
                os.link(partial, path)  # refuses an existing target; the partial file is removed by the caller
        except FileExistsError:
            raise SecretStoreError(CODE_ALREADY_EXISTS) from None
        except PermissionError:
            raise  # for ``_retrying``, which decides whether it is transient
        except OSError:
            raise SecretStoreError(CODE_UNAVAILABLE) from None

    def _get(self, reference):
        path = self._path(reference)
        deadline = self._deadline()
        self._ready()

        def read():
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
            try:
                check_open_file(descriptor)
                self._policy.verify_file(descriptor)  # the owner and ACL of exactly the file that is read, before any byte of it is
                chunks, total = [], 0
                while total <= MAX_FILE_BYTES:
                    chunk = os.read(descriptor, MAX_FILE_BYTES + 1 - total)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                return b"".join(chunks)
            finally:
                os.close(descriptor)

        with self._hold(False, deadline):
            if not self._present(path):
                raise SecretStoreError(CODE_NOT_FOUND)
            try:
                content = self._retrying(deadline, read)
            except FileNotFoundError:
                raise SecretStoreError(CODE_NOT_FOUND) from None
            except OSError:
                raise SecretStoreError(CODE_UNAVAILABLE) from None
        if len(content) > MAX_FILE_BYTES or not content.startswith(FORMAT_TAG) or len(content) == len(FORMAT_TAG):
            raise SecretStoreError(CODE_UNREADABLE)
        try:
            return SecretValue(self._protector.unprotect(content[len(FORMAT_TAG) :], entropy_for(reference)).decode("utf-8"))  # no local ever holds the plaintext
        except Exception:
            raise SecretStoreError(CODE_UNREADABLE) from None

    def _delete(self, reference):
        path = self._path(reference)
        deadline = self._deadline()
        self._ready()
        with self._hold(True, deadline):
            self._sweep_partials()
            if not self._present(path):
                raise SecretStoreError(CODE_NOT_FOUND)
            try:
                self._retrying(deadline, lambda: os.unlink(path))
            except FileNotFoundError:
                raise SecretStoreError(CODE_NOT_FOUND) from None
            except OSError:
                raise SecretStoreError(CODE_UNAVAILABLE) from None

    def _exists(self, reference):
        deadline = self._deadline()
        self._ready()
        with self._hold(False, deadline):
            return self._present(self._path(reference))


# ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------
# Windows: DPAPI and the directory ACL
# ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------

CRYPTPROTECT_UI_FORBIDDEN = 0x1  # CRYPTPROTECT_LOCAL_MACHINE (0x4) is deliberately never used: the scope is the current user
_LOCKFILE_FAIL_IMMEDIATELY = 0x1
_LOCKFILE_EXCLUSIVE_LOCK = 0x2
_DESCRIPTION = "automation-platform secret"
_ERROR_NO_TOKEN = 1008
_SE_FILE_OBJECT = 1
_OWNER_AND_DACL = 0x1 | 0x4  # OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION


def _windows_api():
    if sys.platform != "win32":
        raise SecretStoreError(CODE_UNAVAILABLE)
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", wintypes.BOOL)]

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    pblob = ctypes.POINTER(Blob)
    crypt32.CryptProtectData.argtypes = [pblob, wintypes.LPCWSTR, pblob, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, pblob]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [pblob, ctypes.c_void_p, pblob, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, pblob]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
    kernel32.LockFileEx.restype = wintypes.BOOL
    kernel32.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
    kernel32.UnlockFileEx.restype = wintypes.BOOL
    kernel32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes)]
    kernel32.CreateDirectoryW.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.OpenThreadToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenThreadToken.restype = wintypes.BOOL
    kernel32.GetCurrentThread.restype = wintypes.HANDLE
    kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetDriveTypeW.restype = wintypes.UINT
    kernel32.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, wintypes.DWORD]
    kernel32.GetVolumeInformationW.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.GetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.GetSecurityInfo.restype = wintypes.DWORD
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL

    class Api:
        pass

    api = Api()
    api.wintypes, api.Blob, api.SecurityAttributes, api.SidAndAttributes, api.Overlapped = wintypes, Blob, SecurityAttributes, SidAndAttributes, Overlapped
    api.crypt32, api.advapi32, api.kernel32 = crypt32, advapi32, kernel32
    return api


class WindowsDpapiProtector(Protector):
    """DPAPI, current-user scope. A blob only decrypts for the Windows account that encrypted it, with the same entropy."""

    def __init__(self):
        self._api = _windows_api()

    def protect(self, data: bytes, entropy: bytes) -> bytes:
        api = self._api
        buffer = ctypes.create_string_buffer(bytes(data), len(data))
        extra = ctypes.create_string_buffer(bytes(entropy), len(entropy))
        source = api.Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        salt = api.Blob(len(entropy), ctypes.cast(extra, ctypes.POINTER(ctypes.c_char)))
        result = api.Blob()
        try:
            if not api.crypt32.CryptProtectData(ctypes.byref(source), _DESCRIPTION, ctypes.byref(salt), None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(result)):
                raise SecretStoreError(CODE_UNAVAILABLE)
            try:
                return ctypes.string_at(result.pbData, result.cbData)
            finally:
                api.kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))
        finally:
            ctypes.memset(buffer, 0, len(data))

    def unprotect(self, blob: bytes, entropy: bytes) -> bytes:
        api = self._api
        buffer = ctypes.create_string_buffer(bytes(blob), len(blob))
        extra = ctypes.create_string_buffer(bytes(entropy), len(entropy))
        source = api.Blob(len(blob), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        salt = api.Blob(len(entropy), ctypes.cast(extra, ctypes.POINTER(ctypes.c_char)))
        result = api.Blob()
        if not api.crypt32.CryptUnprotectData(ctypes.byref(source), None, ctypes.byref(salt), None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(result)):
            raise SecretStoreError(CODE_UNREADABLE)
        try:
            return ctypes.string_at(result.pbData, result.cbData)
        finally:
            ctypes.memset(result.pbData, 0, result.cbData)
            api.kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


_ACE = re.compile(r"\(([^()]*)\)")
_DESCRIPTOR = re.compile(r"(?:O:(?P<owner>S-\d+(?:-\d+)+|[A-Z]{2}))?(?:G:(?:S-\d+(?:-\d+)+|[A-Z]{2}))?(?P<dacl>D:.*)?")
_SID_TEXT = re.compile(r"S-1-\d+(?:-\d+)+")
_ACCOUNT_SID = re.compile(r"S-1-5-21-\d+-\d+-\d+-(\d+)")  # local, domain and group-managed service accounts
_VIRTUAL_ACCOUNT_SID = re.compile(r"S-1-5-80-\d+-\d+-\d+-\d+-\d+")  # NT SERVICE\<name> virtual service accounts
_AZURE_AD_SID = re.compile(r"S-1-12-1-\d+-\d+-\d+-\d+")  # Azure AD accounts
SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
_TRUSTED_FIXED_SIDS = frozenset({SYSTEM_SID, ADMINISTRATORS_SID})
_SDDL_ALIASES = {"SY": SYSTEM_SID, "BA": ADMINISTRATORS_SID, "LS": "S-1-5-19", "NS": "S-1-5-20", "WD": "S-1-1-0", "AU": "S-1-5-11", "BU": "S-1-5-32-545", "IU": "S-1-5-4"}


def default_canonical_sid(text: str):
    """A SID or SDDL alias as a canonical SID string, from a fixed table, or ``None`` for anything it cannot resolve. The Windows policy resolves with the operating system instead."""
    if _SID_TEXT.fullmatch(text):
        return text
    return _SDDL_ALIASES.get(text)


def normalize_approved_sids(values) -> frozenset:
    """The operator's explicit approval of service identities, as a set of SID strings; anything that is not a SID string is ``secret_store_unavailable`` (fail closed)."""
    try:
        items = [] if values is None else list(values)
    except TypeError:
        raise SecretStoreError(CODE_UNAVAILABLE) from None
    if not all(isinstance(item, str) and _SID_TEXT.fullmatch(item) for item in items):
        raise SecretStoreError(CODE_UNAVAILABLE)
    return frozenset(items)


def identity_supported(sid: str, approved=frozenset()) -> bool:
    """Whether the store may run as ``sid``. Ordinary accounts (local, domain, gMSA with a RID of 500 or at least 1000), Azure AD accounts and virtual service accounts are supported;
    everything else (LocalSystem, LocalService, NetworkService, built-in groups, anonymous, Everyone, AppContainers, the Guest account) only when the operator approved that exact SID."""
    if sid in approved:
        return True
    account = _ACCOUNT_SID.fullmatch(sid)
    if account:
        rid = int(account.group(1))
        return rid == 500 or rid >= 1000
    return bool(_VIRTUAL_ACCOUNT_SID.fullmatch(sid) or _AZURE_AD_SID.fullmatch(sid))


def descriptor_is_trusted(sddl: str, user_sid: str, canonical=default_canonical_sid, *, protected: bool = True, owner_required: bool = False) -> bool:
    """Whether a security descriptor in SDDL form (owner and DACL) is trusted: the owner, when present (required when ``owner_required``), is ``user_sid``, SYSTEM or Administrators; the DACL is
    protected (no inherited entries) when ``protected``; it has only allow entries; every trustee, after ``canonical`` resolves aliases to SID strings, is ``user_sid``, SYSTEM or Administrators;
    and ``user_sid`` has at least one entry. An unresolvable trustee or owner is untrusted."""
    parsed = _DESCRIPTOR.fullmatch(sddl)
    if parsed is None or parsed.group("dacl") is None:
        return False
    trusted = {user_sid, *_TRUSTED_FIXED_SIDS}
    owner = parsed.group("owner")
    if owner is None:
        if owner_required:
            return False
    elif canonical(owner) not in trusted:
        return False
    dacl = parsed.group("dacl")[2:]
    head, _, _ = dacl.partition("(")
    if protected and "P" not in head:
        return False
    aces = _ACE.findall(dacl)
    if not aces:
        return False
    seen_user = False
    for ace in aces:
        fields = ace.split(";")
        if len(fields) < 6 or fields[0] != "A":
            return False
        trustee = canonical(fields[5])
        if trustee not in trusted:
            return False
        seen_user = seen_user or trustee == user_sid
    return seen_user


def dacl_is_restrictive(sddl: str, user_sid: str, canonical=default_canonical_sid) -> bool:
    """A protected DACL for ``user_sid`` (plus SYSTEM and Administrators) with a trusted owner when one is given. Kept for the directory rule; see ``descriptor_is_trusted``."""
    return descriptor_is_trusted(sddl, user_sid, canonical, protected=True)


def volume_is_safe(drive_type: int, flags: int) -> bool:
    """A fixed local disk whose file system keeps ACLs (``FILE_PERSISTENT_ACLS``): not a network, removable, optical or RAM drive, and not FAT."""
    return drive_type == 3 and bool(flags & 0x8)  # DRIVE_FIXED, FILE_PERSISTENT_ACLS


_UNSAFE_PATH_CHARACTERS = re.compile(r'[<>"|?*]')
_RECOGNIZED_ENTRY = re.compile(r"[0-9a-f]{64}\.dpapi|\.[0-9a-f]{24}\.partial|\.store\.lock")


class WindowsDirectoryPolicy(DirectoryPolicy):
    """Creates the directory with a protected DACL granting the current user alone full control, and checks that the location, the directory and every secret file are still trusted.

    IDENTITY. The identity is the thread token when the thread impersonates (the one DPAPI uses), else the process token. It must be supported (``identity_supported``): an ordinary, Azure AD or
    virtual service account, or a SID the operator listed in ``approved_identity_sids`` (for LocalSystem ``S-1-5-18``, LocalService ``S-1-5-19``, NetworkService ``S-1-5-20``). Anything else
    fails closed, at construction and again on every check."""

    def __init__(self, approved_identity_sids=()):
        self._api = _windows_api()
        self._approved = normalize_approved_sids(approved_identity_sids)
        self.identity()

    def _location(self, path: str) -> None:
        """The location must be a plain local path on a fixed ACL-capable volume: no UNC, device or extended-length prefix, no stream or wildcard syntax, no trailing dot or space, no cloud-sync folder."""
        drive, rest = os.path.splitdrive(path)
        if path.startswith("\\\\") or len(drive) != 2 or drive[1] != ":" or not drive[0].isalpha() or not rest.startswith("\\"):
            raise SecretStoreError(CODE_UNAVAILABLE)
        parts = [part for part in rest.split("\\") if part]
        if _UNSAFE_PATH_CHARACTERS.search(rest) or ":" in rest or any(part != part.rstrip(" .") or part.lower().startswith("onedrive") for part in parts):
            raise SecretStoreError(CODE_UNAVAILABLE)
        api = self._api
        root = drive + "\\"
        flags = self._api.wintypes.DWORD()
        if not api.kernel32.GetVolumeInformationW(root, None, 0, None, None, ctypes.byref(flags), None, 0):
            raise SecretStoreError(CODE_UNAVAILABLE)
        if not volume_is_safe(api.kernel32.GetDriveTypeW(root), flags.value):
            raise SecretStoreError(CODE_UNAVAILABLE)

    def _take(self, pointer) -> str:
        api = self._api
        try:
            return ctypes.wstring_at(pointer)
        finally:
            api.kernel32.LocalFree(pointer)

    def canonical_sid(self, text: str):
        """``text`` (a SID string or an SDDL alias such as SY, BA, LS, NS, LA) as a canonical SID string, resolved by Windows; ``None`` when it does not resolve."""
        api = self._api
        sid = ctypes.c_void_p()
        if not api.advapi32.ConvertStringSidToSidW(text, ctypes.byref(sid)):
            return None
        try:
            out = ctypes.c_void_p()
            if not api.advapi32.ConvertSidToStringSidW(sid, ctypes.byref(out)):
                return None
            return self._take(out.value)
        finally:
            api.kernel32.LocalFree(sid)

    def identity(self) -> str:
        """The current identity's SID, after checking that it is supported."""
        sid = self.current_user_sid()
        if not identity_supported(sid, self._approved):
            raise SecretStoreError(CODE_UNAVAILABLE)
        return sid

    def current_user_sid(self) -> str:
        api, wintypes = self._api, self._api.wintypes
        token = wintypes.HANDLE()
        if not api.advapi32.OpenThreadToken(api.kernel32.GetCurrentThread(), 0x8, True, ctypes.byref(token)):  # TOKEN_QUERY, as the process: an impersonating thread has its own identity
            if ctypes.get_last_error() != _ERROR_NO_TOKEN or not api.advapi32.OpenProcessToken(api.kernel32.GetCurrentProcess(), 0x8, ctypes.byref(token)):
                raise SecretStoreError(CODE_UNAVAILABLE)
        try:
            needed = wintypes.DWORD()
            api.advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))  # TokenUser
            buffer = ctypes.create_string_buffer(needed.value)
            if not needed.value or not api.advapi32.GetTokenInformation(token, 1, buffer, needed.value, ctypes.byref(needed)):
                raise SecretStoreError(CODE_UNAVAILABLE)
            owner = api.SidAndAttributes.from_buffer(buffer)
            text = ctypes.c_void_p()
            if not api.advapi32.ConvertSidToStringSidW(owner.Sid, ctypes.byref(text)):
                raise SecretStoreError(CODE_UNAVAILABLE)
            return self._take(text.value)
        finally:
            api.kernel32.CloseHandle(token)

    def create(self, path: str) -> None:
        """Creates ``path`` itself, whose parent must already exist and be free of links; nothing else is created. The identity and the location are checked first, so a refusal leaves nothing behind."""
        api = self._api
        sid = self.identity()
        self._location(path)
        reject_links(os.path.dirname(path), leaf=True)
        sddl = f"D:P(A;OICI;FA;;;{sid})"
        descriptor = ctypes.c_void_p()
        if not api.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise SecretStoreError(CODE_UNAVAILABLE)
        try:
            attributes = api.SecurityAttributes(ctypes.sizeof(api.SecurityAttributes), descriptor, False)
            if not api.kernel32.CreateDirectoryW(path, ctypes.byref(attributes)):
                raise SecretStoreError(CODE_UNAVAILABLE)
        finally:
            api.kernel32.LocalFree(descriptor)

    def _sddl(self, fetch) -> str:
        api = self._api
        descriptor = ctypes.c_void_p()
        if fetch(descriptor) != 0:
            raise SecretStoreError(CODE_UNAVAILABLE)
        try:
            text = ctypes.c_void_p()
            if not api.advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, _OWNER_AND_DACL, ctypes.byref(text), None):
                raise SecretStoreError(CODE_UNAVAILABLE)
            return self._take(text.value)
        finally:
            api.kernel32.LocalFree(descriptor)

    def verify(self, path: str) -> None:
        api = self._api
        sid = self.identity()
        self._location(path)
        sddl = self._sddl(lambda out: api.advapi32.GetNamedSecurityInfoW(path, _SE_FILE_OBJECT, _OWNER_AND_DACL, None, None, None, None, ctypes.byref(out)))
        if not descriptor_is_trusted(sddl, sid, self.canonical_sid, protected=True, owner_required=True):
            raise SecretStoreError(CODE_UNAVAILABLE)

    def verify_file(self, descriptor: int) -> None:
        """The owner and ACL of the file behind the open ``descriptor``: trusted owner, only the current identity, SYSTEM and Administrators, allow entries only. Inherited entries are expected here."""
        import msvcrt

        api = self._api
        sid = self.identity()
        handle = msvcrt.get_osfhandle(descriptor)
        sddl = self._sddl(lambda out: api.advapi32.GetSecurityInfo(handle, _SE_FILE_OBJECT, _OWNER_AND_DACL, None, None, None, None, ctypes.byref(out)))
        if not descriptor_is_trusted(sddl, sid, self.canonical_sid, protected=False, owner_required=True):
            raise SecretStoreError(CODE_UNAVAILABLE)


class WindowsFileLocker(DirectoryLocker):
    """A cross-process lock on ``.store.lock`` in the store directory, by ``LockFileEx`` on a handle this call opens (1 byte at offset 0). The lock belongs to the handle, so Windows releases it
    when the process exits or is killed, and two threads of one process exclude each other like two processes. The wait polls with ``LOCKFILE_FAIL_IMMEDIATELY`` until the deadline."""

    def __init__(self):
        self._api = _windows_api()

    @contextlib.contextmanager
    def hold(self, directory, exclusive, timeout):
        import msvcrt

        api = self._api
        path = os.path.join(directory, LOCK_FILE_NAME)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            pass
        except OSError:
            raise SecretStoreError(CODE_UNAVAILABLE) from None
        else:
            if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
                raise SecretStoreError(CODE_UNAVAILABLE)
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_BINARY, 0o600)
        except OSError:
            raise SecretStoreError(CODE_UNAVAILABLE) from None
        try:
            check_open_file(descriptor)  # one name, regular, no reparse point
            handle = msvcrt.get_osfhandle(descriptor)
            flags = _LOCKFILE_FAIL_IMMEDIATELY | (_LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0)
            deadline = time.monotonic() + max(0.0, timeout)
            delay = _FIRST_POLL
            while not api.kernel32.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(api.Overlapped())):
                if ctypes.get_last_error() != _ERROR_LOCK_VIOLATION:
                    raise SecretStoreError(CODE_UNAVAILABLE)
                if time.monotonic() >= deadline:
                    raise SecretStoreError(CODE_BUSY)
                delay = _pause(delay)
            try:
                yield
            finally:
                api.kernel32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(api.Overlapped()))
        finally:
            os.close(descriptor)


class WindowsDpapiSecretStore(EncryptedFileSecretStore):
    """The Windows DPAPI secret store. ``directory`` must already exist and be prepared (``prepare_windows_secret_directory``); on any other platform construction fails closed.
    ``timeout`` bounds the wait of one operation for the cross-process lock (seconds, above 0 and at most 60). ``approved_identity_sids`` lists the built-in service identities the operator
    explicitly allows this process to run as (``S-1-5-18`` LocalSystem, ``S-1-5-19`` LocalService, ``S-1-5-20`` NetworkService); the default is none, and any other identity that is not an
    ordinary, Azure AD or virtual service account fails closed."""

    def __init__(self, directory, *, timeout: float = DEFAULT_TIMEOUT_SECONDS, approved_identity_sids=()):
        super().__init__(directory, WindowsDpapiProtector(), WindowsDirectoryPolicy(approved_identity_sids), WindowsFileLocker(), timeout=timeout)


def prepare_windows_secret_directory(directory, *, approved_identity_sids=()) -> None:
    """Explicit, operator-run set-up, all or nothing.

    The identity is validated and the location checked BEFORE anything is created, so a refusal leaves no directory. A missing directory is created (its parent must exist and be free of links)
    with a protected DACL for the current identity alone, then verified; if that verification fails the directory just created is removed. An EXISTING directory is never changed and never adopted
    unless it is already trusted (trusted owner, protected restrictive ACL, no links in its path) and holds nothing but this store's own files."""
    path = checked_directory(directory)
    policy = WindowsDirectoryPolicy(approved_identity_sids)
    if os.path.lexists(path):
        reject_links(path)
        policy.verify(path)
        try:
            entries = os.listdir(path)
        except OSError:
            raise SecretStoreError(CODE_UNAVAILABLE) from None
        if not all(_RECOGNIZED_ENTRY.fullmatch(name) for name in entries):
            raise SecretStoreError(CODE_UNAVAILABLE)
        return
    policy.create(path)
    try:
        reject_links(path)
        policy.verify(path)
    except BaseException:
        try:
            os.rmdir(path)
        except OSError:
            pass
        raise
