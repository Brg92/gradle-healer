"""Cross-platform process-tree control for subprocess execution.

Provides a unified protocol to configure subprocess creation, attach the
spawned process as early as possible, request a graceful stop where the OS
supports it, and forcibly terminate the entire process tree (including
grandchildren that detached from the direct child).

- POSIX (Linux, macOS): places the child in a new session/process group via
  start_new_session=True, sends SIGTERM for graceful stop, and sends SIGKILL
  to the process group via os.killpg.
- Windows: assigns the child to a kernel Job Object configured with
  JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE via ctypes (no third-party dependencies).
  Graceful stop is skipped on Windows because CTRL_BREAK_EVENT triggers a JVM
  thread dump (like SIGQUIT) rather than a shutdown and causes cmd.exe to
  prompt interactively; termination proceeds directly via TerminateJobObject.
"""

import logging
import sys
from typing import Protocol, TypedDict

logger = logging.getLogger(__name__)


class SpawnKwargs(TypedDict, total=False):
    """Keyword arguments forwarded to loop.subprocess_exec."""

    start_new_session: bool


class ProcessController(Protocol):
    """OS-specific lifecycle controller for one spawned subprocess tree."""

    def spawn_kwargs(self) -> SpawnKwargs:
        """Keyword arguments to pass to loop.subprocess_exec."""

    def attach(self, pid: int) -> None:
        """Bind the controller to the newly spawned child PID."""

    def request_stop(self) -> bool:
        """Attempt a graceful stop; return True if a stop signal was sent."""

    def kill_tree(self) -> None:
        """Forcibly terminate the process and all of its descendants."""

    def close(self) -> None:
        """Release any OS resources (such as a Windows Job Object handle)."""


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _ERROR_ACCESS_DENIED = 5
    _ERROR_INVALID_PARAMETER = 87
    _STILL_ACTIVE = 259

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = (
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        )

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = (
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        )

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = (
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        )

    _kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE

    _kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL

    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE

    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

    _kernel32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL

    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateJobObject.restype = wintypes.BOOL

    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL

    class WindowsJobController:
        """Controls a Windows process tree through a kernel Job Object.

        Note on the spawn-to-attach window: CreateProcess runs inside asyncio's
        transport startup and attach() runs in SubprocessProtocol.connection_made()
        right after the pipes are connected (a few milliseconds). A child that
        spawned and exited before connection_made could theoretically leave a
        descendant outside the job; Gradle's wrapper script does not do so.
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE also guarantees the kernel kills the
        job if the agent crashes.
        """

        def __init__(self) -> None:
            job: int | None = _kernel32.CreateJobObjectW(None, None)
            if not job:
                raise ctypes.WinError(ctypes.get_last_error())
            self._job_handle: int | None = job

            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = _kernel32.SetInformationJobObject(
                job,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                err = ctypes.get_last_error()
                self.close()
                raise ctypes.WinError(err)

        def spawn_kwargs(self) -> SpawnKwargs:
            return {}

        def attach(self, pid: int) -> None:
            if self._job_handle is None:
                return
            access = _PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION
            proc_handle: int | None = _kernel32.OpenProcess(access, False, pid)
            if not proc_handle:
                err = ctypes.get_last_error()
                if err == _ERROR_INVALID_PARAMETER:
                    # The process already exited and was reaped before OpenProcess.
                    return
                raise ctypes.WinError(err)
            try:
                if not _kernel32.AssignProcessToJobObject(self._job_handle, proc_handle):
                    err = ctypes.get_last_error()
                    if err == _ERROR_ACCESS_DENIED and not _is_still_active(proc_handle):
                        # Windows returns ERROR_ACCESS_DENIED when assigning a
                        # process that has already terminated.
                        return
                    raise ctypes.WinError(err)
            finally:
                _kernel32.CloseHandle(proc_handle)

        def request_stop(self) -> bool:
            return False

        def kill_tree(self) -> None:
            if self._job_handle is not None and not _kernel32.TerminateJobObject(
                self._job_handle, 1
            ):
                err = ctypes.get_last_error()
                logger.warning(
                    "TerminateJobObject failed (winerror=%d): %s",
                    err,
                    ctypes.FormatError(err).strip(),
                )

        def close(self) -> None:
            if self._job_handle is not None:
                _kernel32.CloseHandle(self._job_handle)
                self._job_handle = None

    def _is_still_active(proc_handle: int) -> bool:
        code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(proc_handle, ctypes.byref(code)):
            return False
        return bool(code.value == _STILL_ACTIVE)

    def default_controller() -> ProcessController:
        """Return a fresh ProcessController for the current platform."""
        return WindowsJobController()

else:
    import os
    import signal

    class PosixProcessController:
        """Controls a POSIX process tree through its process group (setsid + killpg)."""

        def __init__(self) -> None:
            self._pid: int | None = None

        def spawn_kwargs(self) -> SpawnKwargs:
            return {"start_new_session": True}

        def attach(self, pid: int) -> None:
            self._pid = pid

        def request_stop(self) -> bool:
            if self._pid is None:
                return False
            self._signal_group(signal.SIGTERM)
            return True

        def kill_tree(self) -> None:
            if self._pid is None:
                return
            self._signal_group(signal.SIGKILL)

        def close(self) -> None:
            pass

        def _signal_group(self, sig: signal.Signals) -> None:
            if self._pid is None:
                return
            try:
                os.killpg(self._pid, sig)
            except ProcessLookupError:
                return
            except PermissionError as exc:
                # On macOS (XNU killpg1), signaling a process group whose only
                # remaining members are zombies (SZOMB) raises EPERM rather than
                # ESRCH. On Linux EPERM should never happen for a group we
                # created, so log it as a warning.
                if sys.platform == "darwin":
                    return
                logger.warning(
                    "killpg(%d, %s) failed with PermissionError: %s",
                    self._pid,
                    sig.name,
                    exc,
                )

    def default_controller() -> ProcessController:
        """Return a fresh ProcessController for the current platform."""
        return PosixProcessController()
