"""Operating-system limits a child process puts on ITSELF before it reads any untrusted byte. Standard library only (``ctypes`` on Windows, ``resource`` on POSIX); used only by ``child.py``.

WINDOWS. The process is assigned to a new job object that limits its committed memory (``ProcessMemoryLimit``), its CPU time, the number of active processes to ONE (so it cannot start another program) and
kills it when the job closes. A job can be nested on Windows 8 and later; if the assignment is refused the limits are NOT in force and ``LimitsUnavailableError`` is raised (fail closed).

POSIX. ``RLIMIT_AS`` (address space) and ``RLIMIT_CPU`` are required; core dumps and file creation are switched off (``RLIMIT_CORE``, ``RLIMIT_FSIZE``) and process creation is refused where ``RLIMIT_NPROC`` can
say so. A platform where a required limit cannot be set (macOS does not honor ``RLIMIT_AS``) fails closed. The POSIX path has NOT been run in the development VM (Windows).

These limits bound resource use; they are not a security sandbox. The process keeps the account's file and network access: it is trusted to read only standard input and write only standard output because
it is our code, and what it writes is validated by the parent (``rules.parse_child_output``).
"""

import os


class LimitsUnavailableError(Exception):
    """The limits could not be put in force."""


def apply_limits(memory_bytes: int, cpu_seconds: int) -> None:
    if os.name == "nt":
        _apply_windows(memory_bytes, cpu_seconds)
    elif os.name == "posix":
        _apply_posix(memory_bytes, cpu_seconds)
    else:
        raise LimitsUnavailableError("unsupported platform")


def _apply_posix(memory_bytes: int, cpu_seconds: int) -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    except (ImportError, AttributeError, ValueError, OSError):
        raise LimitsUnavailableError("limits") from None
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    except (ImportError, AttributeError, ValueError, OSError):
        pass  # best effort: not every POSIX platform has it


_JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_job_handle = None  # kept for the life of the process


def windows_structures():
    """The ``ctypes`` structures of ``JOBOBJECT_EXTENDED_LIMIT_INFORMATION`` (a function so they are built only on Windows and a test can check their size)."""
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    return ExtendedLimits


def _apply_windows(memory_bytes: int, cpu_seconds: int) -> None:
    global _job_handle
    import ctypes
    from ctypes import wintypes

    ExtendedLimits = windows_structures()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise LimitsUnavailableError("job")
    info = ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_PROCESS_TIME | _JOB_OBJECT_LIMIT_ACTIVE_PROCESS | _JOB_OBJECT_LIMIT_PROCESS_MEMORY | _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    info.BasicLimitInformation.PerProcessUserTimeLimit = cpu_seconds * 10_000_000  # 100-nanosecond units
    info.BasicLimitInformation.ActiveProcessLimit = 1
    info.ProcessMemoryLimit = memory_bytes
    if not kernel32.SetInformationJobObject(job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info)):
        raise LimitsUnavailableError("job")
    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        raise LimitsUnavailableError("job")
    _job_handle = job
