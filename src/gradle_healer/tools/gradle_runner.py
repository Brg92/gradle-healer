"""Asynchronous process runner for GradleHealer.

Launches processes (Gradle in particular) asynchronously and returns what
happened as raw data. It never interprets output: classifying a build and
extracting its salient errors belongs to log_parser.

Process-tree cleanup is delegated to ProcessController (POSIX process groups
on Linux/macOS, Job Objects on Windows).
"""

import asyncio
import contextlib
import logging
import re
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from gradle_healer.core.schemas import GRADLE_TASK_PATH_PATTERN
from gradle_healer.tools.process_control import ProcessController, default_controller

logger = logging.getLogger(__name__)

_DEFAULT_MAX_OUTPUT_BYTES = 2_000_000
_STDOUT_FD = 1
_STDERR_FD = 2
# How long the output pipes may stay open after the direct child has exited.
_PIPE_DRAIN_SECONDS = 2.0
# Logged when cleanup starts; tests use it to detect that moment.
_KILLING_PROCESS_TREE_LOG_PREFIX = "Killing process tree"
_TASK_PATH = re.compile(GRADLE_TASK_PATH_PATTERN)
# Rejects spaces, quotes, and cmd.exe / shell metacharacters (% ! ^ & | < > " ( ))
# while allowing Gradle flags, test filters (*, $), comma lists, and @argfiles.
_SAFE_EXTRA_ARG = re.compile(r"^[A-Za-z0-9_./:=+*$,@-]+$")
_WRAPPER_NAME = "gradlew.bat" if sys.platform == "win32" else "gradlew"

# ESC followed by one of:
#   [ params intermediates final  -> CSI (colors, cursor movement, line erase)
#   ] text, ended by BEL or ESC \ -> OSC (window title, hyperlinks)
#   one byte in @-Z \ ] ^ _       -> remaining two-byte escapes
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")


# --- RunOutcome ---
# Raw facts about one process run, kept free of interpretation so that
# parsing and classification can live in a separate, testable layer.
@dataclass(frozen=True, slots=True)
class RunOutcome:
    """Raw, uninterpreted outcome of one process run.

    exit_code is None when the process timed out or was never launched;
    launch_error explains the latter. stdout and stderr are sanitized (UTF-8,
    no ANSI codes, '\\n' line endings) and possibly tail-truncated.
    """

    command: tuple[str, ...]
    exit_code: int | None
    timed_out: bool
    launch_error: str | None
    duration_seconds: float
    stdout: str
    stderr: str


# --- Output capture ---
@dataclass(slots=True)
class _TailBuffer:
    """Keeps only the last max_bytes of a stream, counting what was dropped."""

    max_bytes: int
    data: bytearray = field(default_factory=bytearray)
    dropped_bytes: int = 0

    def append(self, chunk: bytes) -> None:
        self.data += chunk
        overflow = len(self.data) - self.max_bytes
        if overflow > 0:
            del self.data[:overflow]
            self.dropped_bytes += overflow

    def text(self) -> str:
        text = _sanitize(bytes(self.data))
        if self.dropped_bytes:
            return f"[gradle-healer: {self.dropped_bytes} bytes truncated]\n{text}"
        return text


def _sanitize(data: bytes) -> str:
    # Tool output is not guaranteed to be valid UTF-8; a bad byte must not crash the run.
    text = data.decode("utf-8", errors="replace")
    text = _ANSI_ESCAPE.sub("", text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


# asyncio's Process.wait() resolves only once the child has exited AND every
# pipe has closed, so a descendant that inherited the pipes (e.g. a background
# daemon) makes it hang although the child is long gone. The protocol
# callbacks report the two events separately.
class _CaptureProtocol(asyncio.SubprocessProtocol):
    """Feeds stdout/stderr into tail buffers and signals exit and EOF separately."""

    def __init__(self, max_output_bytes: int, controller: ProcessController) -> None:
        self.stdout = _TailBuffer(max_output_bytes)
        self.stderr = _TailBuffer(max_output_bytes)
        self.exited = asyncio.Event()
        self.pipes_closed = asyncio.Event()
        self.attach_error: OSError | None = None
        self._open_pipes = {_STDOUT_FD, _STDERR_FD}
        self._controller = controller

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        # Called by BaseSubprocessTransport._connect_pipes right after the
        # pipes are connected. An exception raised here would go to the event
        # loop's exception handler rather than the caller, so capture any
        # OSError for run_process to abort the child and report a launch_error.
        if isinstance(transport, asyncio.SubprocessTransport):
            try:
                self._controller.attach(transport.get_pid())
            except OSError as exc:
                self.attach_error = exc

    def pipe_data_received(self, fd: int, data: bytes) -> None:
        (self.stdout if fd == _STDOUT_FD else self.stderr).append(data)

    def pipe_connection_lost(self, fd: int, exc: Exception | None) -> None:
        self._open_pipes.discard(fd)
        if not self._open_pipes:
            self.pipes_closed.set()

    def process_exited(self) -> None:
        self.exited.set()


# Once the child has exited, its pipes get a bounded drain period to deliver
# the remaining output; past that, the tree kill that follows takes care of
# whoever still holds them.
async def _drain(protocol: _CaptureProtocol) -> None:
    try:
        async with asyncio.timeout(_PIPE_DRAIN_SECONDS):
            await protocol.pipes_closed.wait()
    except TimeoutError:
        logger.debug(
            "Output pipes still open %.1fs after exit, held by a descendant", _PIPE_DRAIN_SECONDS
        )


# --- Process tree cleanup ---
# The exit event only tracks the direct child; grandchildren (e.g. a Kotlin
# daemon) survive unless the whole group / job is forcibly terminated.
async def _terminate_tree(
    controller: ProcessController, exited: asyncio.Event, grace_seconds: float
) -> None:
    if controller.request_stop():
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(grace_seconds):
                await exited.wait()

    controller.kill_tree()
    await exited.wait()


# The cleanup runs in its own task behind a shield, so a cancellation that
# reaches the caller mid-cleanup cannot interrupt it: it is remembered and
# re-raised only after the whole tree has been killed and reaped.
async def _kill_process_tree(
    controller: ProcessController,
    pid: int,
    exited: asyncio.Event,
    grace_seconds: float = 2.0,
) -> None:
    logger.debug("%s %d", _KILLING_PROCESS_TREE_LOG_PREFIX, pid)
    cleanup = asyncio.create_task(_terminate_tree(controller, exited, grace_seconds))
    cancelled: asyncio.CancelledError | None = None
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as exc:
            cancelled = exc
    if cancelled is not None:
        raise cancelled


# loop.subprocess_exec spawns the OS process synchronously and then suspends
# while connecting pipes. Shielding the spawn task ensures that if a
# cancellation lands mid-startup, the transport is still returned to the
# caller so the finally block can kill the whole process tree before
# re-raising CancelledError.
async def _spawn_process(
    loop: asyncio.AbstractEventLoop,
    command: tuple[str, ...],
    *,
    cwd: Path,
    max_output_bytes: int,
    controller: ProcessController,
) -> tuple[asyncio.SubprocessTransport, _CaptureProtocol, asyncio.CancelledError | None]:
    spawn_task: asyncio.Task[tuple[asyncio.SubprocessTransport, _CaptureProtocol]] = (
        asyncio.create_task(
            loop.subprocess_exec(
                lambda: _CaptureProtocol(max_output_bytes, controller),
                *command,
                cwd=cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **controller.spawn_kwargs(),
            )
        )
    )
    cancelled: asyncio.CancelledError | None = None
    while not spawn_task.done():
        try:
            await asyncio.shield(spawn_task)
        except asyncio.CancelledError as exc:
            cancelled = exc
    if spawn_task.exception() is not None and cancelled is not None:
        raise cancelled
    transport, protocol = spawn_task.result()
    return transport, protocol, cancelled


# --- Validation ---
def _validate_limits(timeout_seconds: float, max_output_bytes: int) -> None:
    if timeout_seconds <= 0:
        raise ValueError(f"timeout_seconds must be > 0, got {timeout_seconds}")
    if max_output_bytes <= 0:
        raise ValueError(f"max_output_bytes must be > 0, got {max_output_bytes}")


def _validate_extra_args(extra_args: Sequence[str]) -> None:
    if isinstance(extra_args, str):
        raise TypeError("extra_args must be a sequence of arguments, not a single string")
    for arg in extra_args:
        if not _SAFE_EXTRA_ARG.fullmatch(arg):
            raise ValueError(
                f"extra_args contains invalid or unsafe argument {arg!r}; "
                f"must match {_SAFE_EXTRA_ARG.pattern}"
            )


def _describe_launch_error(exc: OSError, command: tuple[str, ...], cwd: Path) -> str:
    # Built from strerror and the executable's name only, so the message
    # carries no absolute paths from the user's machine.
    reason = exc.strerror or type(exc).__name__
    if exc.filename is not None and str(exc.filename) == str(cwd):
        return f"cannot enter working directory: {reason}"
    return f"cannot launch {Path(command[0]).name!r}: {reason}"


# --- run_process ---
async def run_process(
    args: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
    controller_factory: Callable[[], ProcessController] = default_controller,
) -> RunOutcome:
    """Run a process to completion or timeout and return its raw outcome.

    A failure to launch (missing or non-executable program) is returned as
    data in launch_error; only invalid arguments raise ValueError. The
    process tree is always killed before returning, even on cancellation.
    """
    if isinstance(args, str):
        raise TypeError("args must be a sequence of arguments, not a single string")
    if not args:
        raise ValueError("args must not be empty")
    _validate_limits(timeout_seconds, max_output_bytes)

    command = tuple(args)
    logger.debug("Launching %s in %s", command, cwd)
    start = time.perf_counter()
    loop = asyncio.get_running_loop()
    try:
        controller = controller_factory()
    except OSError as exc:
        launch_error = _describe_launch_error(exc, command, cwd)
        logger.debug("Could not initialize process controller for %s: %s", command, launch_error)
        return RunOutcome(
            command=command,
            exit_code=None,
            timed_out=False,
            launch_error=launch_error,
            duration_seconds=time.perf_counter() - start,
            stdout="",
            stderr="",
        )

    try:
        try:
            transport, protocol, cancelled = await _spawn_process(
                loop,
                command,
                cwd=cwd,
                max_output_bytes=max_output_bytes,
                controller=controller,
            )
        except OSError as exc:
            launch_error = _describe_launch_error(exc, command, cwd)
            logger.debug("Could not launch %s: %s", command, launch_error)
            return RunOutcome(
                command=command,
                exit_code=None,
                timed_out=False,
                launch_error=launch_error,
                duration_seconds=time.perf_counter() - start,
                stdout="",
                stderr="",
            )

        exit_code: int | None = None
        timed_out = False
        try:
            if cancelled is not None:
                raise cancelled
            if protocol.attach_error is not None:
                with contextlib.suppress(ProcessLookupError):
                    transport.kill()
                launch_error = _describe_launch_error(protocol.attach_error, command, cwd)
                logger.debug("Could not attach %s: %s", command, launch_error)
                return RunOutcome(
                    command=command,
                    exit_code=None,
                    timed_out=False,
                    launch_error=launch_error,
                    duration_seconds=time.perf_counter() - start,
                    stdout="",
                    stderr="",
                )
            # The timeout covers the direct child only: once it has exited, the run
            # is not a timeout even if the pipes need the drain period to close.
            try:
                async with asyncio.timeout(timeout_seconds):
                    await protocol.exited.wait()
            except TimeoutError:
                timed_out = True
            # Measured before the drain and the tree cleanup, so neither counts.
            # On cancellation the function raises and no duration is reported.
            duration_seconds = time.perf_counter() - start
            if not timed_out:
                exit_code = transport.get_returncode()
                await _drain(protocol)
        finally:
            try:
                await _kill_process_tree(controller, transport.get_pid(), protocol.exited)
            finally:
                transport.close()
    finally:
        controller.close()

    logger.debug(
        "Finished %s: exit_code=%s timed_out=%s duration=%.2fs",
        command,
        exit_code,
        timed_out,
        duration_seconds,
    )
    return RunOutcome(
        command=command,
        exit_code=exit_code,
        timed_out=timed_out,
        launch_error=None,
        duration_seconds=duration_seconds,
        stdout=protocol.stdout.text(),
        stderr=protocol.stderr.text(),
    )


# --- run_gradle ---
async def run_gradle(
    project_dir: Path,
    task: str,
    *,
    timeout_seconds: float,
    extra_args: Sequence[str] = (),
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
) -> RunOutcome:
    """Run one Gradle task through the project's wrapper and return its raw outcome.

    task must be a Gradle task path such as ':core:test'. A missing gradlew is
    returned as data in launch_error, like any other launch failure.
    """
    if not _TASK_PATH.fullmatch(task):
        raise ValueError(f"task must be a Gradle task path like ':core:test', got {task!r}")
    _validate_limits(timeout_seconds, max_output_bytes)
    _validate_extra_args(extra_args)

    gradlew = project_dir / _WRAPPER_NAME
    command = (str(gradlew), task, "--console=plain", "--no-daemon", *extra_args)
    if not await asyncio.to_thread(gradlew.is_file):
        return RunOutcome(
            command=command,
            exit_code=None,
            timed_out=False,
            launch_error=f"{_WRAPPER_NAME} not found in project directory",
            duration_seconds=0.0,
            stdout="",
            stderr="",
        )

    return await run_process(
        command,
        cwd=project_dir,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )
