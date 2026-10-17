import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from gradle_healer.tools import gradle_runner
from gradle_healer.tools.gradle_runner import RunOutcome, run_gradle, run_process
from gradle_healer.tools.process_control import (
    ProcessController,
    SpawnKwargs,
    default_controller,
)
from tests.tools.fake_process import FAKE_PROCESS, fake_cmd
from tests.tools.sandbox import (
    GRADLE_TIMEOUT_SECONDS,
    POLL_TIMEOUT_SECONDS,
    REPO_ROOT,
    apply_scenario,
    is_pid_alive,
    wait_for_pid_exit,
)

_LOG_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "logs"


def _assert_pid_gone(pid: int) -> None:
    assert not is_pid_alive(pid), f"expected process {pid} to be gone"


@contextlib.asynccontextmanager
async def _pid_receiver() -> AsyncIterator[tuple[int, asyncio.Future[int]]]:
    """Listen on a loopback ephemeral port for a helper process to report its PID."""
    loop = asyncio.get_running_loop()
    pid_future: asyncio.Future[int] = loop.create_future()

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            if line and not pid_future.done():
                pid_future.set_result(int(line.decode("ascii").strip()))
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
    try:
        port = int(server.sockets[0].getsockname()[1])
        yield port, pid_future
    finally:
        server.close()
        await server.wait_closed()


async def _await_reported_pid(pid_future: asyncio.Future[int]) -> int:
    async with asyncio.timeout(POLL_TIMEOUT_SECONDS):
        return await pid_future


def _same_path(first: str | Path, second: str | Path) -> bool:
    return os.path.samefile(first, second)


def _write_fake_gradlew(project_dir: Path) -> Path:
    if sys.platform == "win32":
        wrapper = project_dir / "gradlew.bat"
        script = f'@echo off\n"{sys.executable}" "{FAKE_PROCESS}" cwd %*\n'
        wrapper.write_text(script, encoding="utf-8", newline="\r\n")
        return wrapper
    wrapper = project_dir / "gradlew"
    script = (
        f"#!{sys.executable}\n"
        "import subprocess, sys\n"
        f"cmd = [sys.executable, {str(FAKE_PROCESS)!r}, 'cwd', *sys.argv[1:]]\n"
        "raise SystemExit(subprocess.call(cmd))\n"
    )
    wrapper.write_text(script, encoding="utf-8", newline="\n")
    wrapper.chmod(0o755)
    return wrapper


# --- Exit code and streams ---
async def test_captures_exit_code_and_separate_streams(tmp_path: Path) -> None:
    args = fake_cmd("emit", "--stdout", "out\n", "--stderr", "err\n")

    outcome = await run_process(args, cwd=tmp_path, timeout_seconds=5)

    assert outcome == RunOutcome(
        command=tuple(args),
        exit_code=0,
        timed_out=False,
        launch_error=None,
        duration_seconds=outcome.duration_seconds,
        stdout="out\n",
        stderr="err\n",
    )
    assert outcome.duration_seconds >= 0


async def test_non_zero_exit_code_is_returned(tmp_path: Path) -> None:
    outcome = await run_process(
        fake_cmd("emit", "--exit-code", "3"), cwd=tmp_path, timeout_seconds=5
    )

    assert outcome.exit_code == 3
    assert not outcome.timed_out
    assert outcome.launch_error is None


async def test_runs_in_cwd(tmp_path: Path) -> None:
    outcome = await run_process(fake_cmd("cwd"), cwd=tmp_path, timeout_seconds=5)

    assert outcome.exit_code == 0
    assert _same_path(outcome.stdout.strip(), tmp_path)


# --- Timeout and process group ---
async def test_timeout_preserves_partial_output(tmp_path: Path) -> None:
    outcome = await run_process(
        fake_cmd("emit", "--stdout", "started\n", "--sleep", "30"),
        cwd=tmp_path,
        timeout_seconds=1,
    )

    assert outcome.timed_out
    assert outcome.exit_code is None
    assert "started" in outcome.stdout
    assert outcome.duration_seconds < 5


async def test_timeout_kills_orphaned_grandchild(tmp_path: Path) -> None:
    outcome = await run_process(
        fake_cmd("spawn-grandchild", "--wait"), cwd=tmp_path, timeout_seconds=1
    )
    grandchild = int(outcome.stdout.split()[0])

    await wait_for_pid_exit(grandchild)

    assert outcome.timed_out
    _assert_pid_gone(grandchild)


async def test_exit_with_grandchild_holding_pipes_is_not_a_timeout(tmp_path: Path) -> None:
    # The background sleep inherits stdout/stderr, so EOF never comes on its
    # own: only the bounded drain lets the run finish before the timeout.
    outcome = await run_process(fake_cmd("spawn-grandchild"), cwd=tmp_path, timeout_seconds=10)
    grandchild = int(outcome.stdout.split()[0])

    await wait_for_pid_exit(grandchild)

    assert outcome.exit_code == 0
    assert not outcome.timed_out
    # The 2 s pipe drain must not count towards the reported duration.
    assert outcome.duration_seconds < 1.5
    _assert_pid_gone(grandchild)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process groups and zombie processes do not exist on Windows",
)
async def test_exit_with_zombie_in_process_group_succeeds(tmp_path: Path) -> None:
    # The sleeping grandchild forks an immediate child that becomes an unreaped
    # zombie in the process group while the grandchild holds the pipes open and
    # the direct child exits. On macOS, killpg on a group with zombies raises
    # EPERM; PosixProcessController must tolerate it without failing the run.
    outcome = await run_process(
        fake_cmd("spawn-grandchild", "--with-zombie"), cwd=tmp_path, timeout_seconds=10
    )
    grandchild = int(outcome.stdout.split()[0])

    await wait_for_pid_exit(grandchild)

    assert outcome.exit_code == 0
    assert not outcome.timed_out
    _assert_pid_gone(grandchild)


async def test_external_cancellation_kills_grandchild(tmp_path: Path) -> None:
    async with _pid_receiver() as (port, pid_future):
        task = asyncio.create_task(
            run_process(
                fake_cmd("spawn-grandchild", "--wait", "--report-port", str(port)),
                cwd=tmp_path,
                timeout_seconds=30,
            )
        )
        grandchild = await _await_reported_pid(pid_future)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await wait_for_pid_exit(grandchild)

    _assert_pid_gone(grandchild)


class _CleanupStarted(logging.Handler):
    """Signals the moment run_process enters its process-tree cleanup."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.event = asyncio.Event()

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage().startswith(gradle_runner._KILLING_PROCESS_TREE_LOG_PREFIX):
            self.event.set()


async def test_cancellation_during_startup_still_kills_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression test: loop.subprocess_exec spawns the OS child before finishing
    # pipe setup. If run_process is cancelled before subprocess_exec returns,
    # the shield on _spawn_process must still hand the transport to the finally
    # block so the grandchild is killed before CancelledError is re-raised.
    # Approved monkeypatch: delaying loop.subprocess_exec after the real child
    # has spawned is the only way to deterministically land a cancellation in
    # the startup window without sleep-based synchronization.
    loop = asyncio.get_running_loop()
    orig_subprocess_exec = loop.subprocess_exec
    release_spawn = asyncio.Event()

    async def delayed_subprocess_exec(*args: Any, **kwargs: Any) -> Any:
        result = await orig_subprocess_exec(*args, **kwargs)
        await release_spawn.wait()
        return result

    monkeypatch.setattr(loop, "subprocess_exec", delayed_subprocess_exec)
    async with _pid_receiver() as (port, pid_future):
        task = asyncio.create_task(
            run_process(
                fake_cmd("spawn-grandchild", "--wait", "--report-port", str(port)),
                cwd=tmp_path,
                timeout_seconds=30,
            )
        )
        grandchild = await _await_reported_pid(pid_future)

        # task.cancel() is queued on the event loop before release_spawn.set(), so
        # the CancelledError hits _spawn_process while spawn_task is still pending.
        task.cancel()
        loop.call_soon(release_spawn.set)
        with pytest.raises(asyncio.CancelledError):
            await task
    await wait_for_pid_exit(grandchild)

    _assert_pid_gone(grandchild)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="SIGTERM grace period before SIGKILL is POSIX-only; Windows terminates the job directly",
)
async def test_second_cancellation_during_cleanup_still_kills_group(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Ignoring SIGTERM (inherited by the grandchild) forces cleanup through the
    # full grace period and the final SIGKILL, which gives the second
    # cancellation a window in which to land.
    caplog.set_level(logging.DEBUG, logger=gradle_runner.__name__)
    cleanup_started = _CleanupStarted()
    gradle_runner.logger.addHandler(cleanup_started)
    try:
        async with _pid_receiver() as (port, pid_future):
            task = asyncio.create_task(
                run_process(
                    fake_cmd(
                        "spawn-grandchild",
                        "--wait",
                        "--ignore-term",
                        "--report-port",
                        str(port),
                    ),
                    cwd=tmp_path,
                    timeout_seconds=30,
                )
            )
            grandchild = await _await_reported_pid(pid_future)

            task.cancel()
            await cleanup_started.event.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        gradle_runner.logger.removeHandler(cleanup_started)
    await wait_for_pid_exit(grandchild)

    _assert_pid_gone(grandchild)


# --- Launch errors ---
async def test_missing_executable_is_a_launch_error(tmp_path: Path) -> None:
    outcome = await run_process([str(tmp_path / "does-not-exist")], cwd=tmp_path, timeout_seconds=5)

    assert outcome.exit_code is None
    assert not outcome.timed_out
    assert outcome.launch_error is not None
    assert outcome.launch_error.startswith("cannot launch 'does-not-exist': ")
    assert str(tmp_path) not in outcome.launch_error
    assert (outcome.stdout, outcome.stderr) == ("", "")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX execute permission bits do not exist on Windows",
)
async def test_non_executable_file_is_a_launch_error(tmp_path: Path) -> None:
    script = tmp_path / "script.py"
    script.write_text(f"#!{sys.executable}\nprint('hi')\n", encoding="utf-8")
    script.chmod(0o644)

    outcome = await run_process([str(script)], cwd=tmp_path, timeout_seconds=5)

    assert outcome.exit_code is None
    assert not outcome.timed_out
    assert outcome.launch_error is not None
    assert outcome.launch_error.startswith("cannot launch 'script.py': ")
    assert str(tmp_path) not in outcome.launch_error


async def test_controller_init_error_is_returned_as_launch_error(tmp_path: Path) -> None:
    def broken_controller() -> ProcessController:
        raise OSError(24, "Too many open files")

    outcome = await run_process(
        fake_cmd("emit"),
        cwd=tmp_path,
        timeout_seconds=5,
        controller_factory=broken_controller,
    )

    assert outcome.exit_code is None
    assert not outcome.timed_out
    exe_name = Path(sys.executable).name
    assert outcome.launch_error == f"cannot launch {exe_name!r}: Too many open files"


async def test_controller_attach_error_kills_child_and_returns_launch_error(
    tmp_path: Path,
) -> None:
    real = default_controller()
    attached_pids: list[int] = []

    class FailingAttachController:
        def spawn_kwargs(self) -> SpawnKwargs:
            return real.spawn_kwargs()

        def attach(self, pid: int) -> None:
            attached_pids.append(pid)
            real.attach(pid)
            raise PermissionError(13, "Access is denied")

        def request_stop(self) -> bool:
            return real.request_stop()

        def kill_tree(self) -> None:
            real.kill_tree()

        def close(self) -> None:
            real.close()

    outcome = await run_process(
        fake_cmd("sleep", "30"),
        cwd=tmp_path,
        timeout_seconds=5,
        controller_factory=FailingAttachController,
    )

    assert outcome.exit_code is None
    assert not outcome.timed_out
    exe_name = Path(sys.executable).name
    assert outcome.launch_error == f"cannot launch {exe_name!r}: Access is denied"
    assert len(attached_pids) == 1
    await wait_for_pid_exit(attached_pids[0])
    _assert_pid_gone(attached_pids[0])


# --- Output handling ---
async def test_output_is_tail_truncated(tmp_path: Path) -> None:
    full = "".join(f"{i}\n" for i in range(1, 1001))
    limit = 100

    outcome = await run_process(
        fake_cmd("lines", "1000"), cwd=tmp_path, timeout_seconds=5, max_output_bytes=limit
    )

    dropped = len(full) - limit
    assert outcome.stdout == f"[gradle-healer: {dropped} bytes truncated]\n{full[-limit:]}"


async def test_output_within_limit_has_no_marker(tmp_path: Path) -> None:
    outcome = await run_process(
        fake_cmd("emit", "--stdout", "12345"),
        cwd=tmp_path,
        timeout_seconds=5,
        max_output_bytes=5,
    )

    assert outcome.stdout == "12345"


async def test_output_is_sanitized(tmp_path: Path) -> None:
    outcome = await run_process(
        fake_cmd("raw-sanitization-sample"), cwd=tmp_path, timeout_seconds=5
    )

    assert outcome.stdout == "red\nnext\nbar\n\ufffd\n"


# --- Input validation ---
@pytest.mark.parametrize(
    ("timeout_seconds", "max_output_bytes", "message"),
    [
        (0, 1, "timeout_seconds"),
        (-1, 1, "timeout_seconds"),
        (5, 0, "max_output_bytes"),
    ],
)
async def test_run_process_rejects_invalid_limits(
    tmp_path: Path, timeout_seconds: float, max_output_bytes: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        await run_process(
            fake_cmd("emit"),
            cwd=tmp_path,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )


async def test_run_process_rejects_empty_args(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="args"):
        await run_process([], cwd=tmp_path, timeout_seconds=5)


async def test_run_process_rejects_a_single_string(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="args"):
        await run_process("true", cwd=tmp_path, timeout_seconds=5)


@pytest.mark.parametrize(
    "task", ["", "test", "core:test", ":core:", "::test", ":core test", ":core:test\n"]
)
async def test_run_gradle_rejects_invalid_task_paths(tmp_path: Path, task: str) -> None:
    with pytest.raises(ValueError, match="task"):
        await run_gradle(tmp_path, task, timeout_seconds=5)


async def test_run_gradle_rejects_non_positive_timeout(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        await run_gradle(tmp_path, ":core:test", timeout_seconds=0)


async def test_run_gradle_rejects_single_string_extra_args(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="extra_args"):
        await run_gradle(tmp_path, ":core:test", timeout_seconds=5, extra_args="--stacktrace")


@pytest.mark.parametrize(
    "unsafe_arg",
    [
        "",
        "--tests com.example.FooTest",
        "foo&bar",
        "foo|bar",
        "foo>out",
        "foo<in",
        "foo^bar",
        "%PATH%",
        "!VAR!",
        '"quoted"',
        "(sub)",
        "--flag\n",
    ],
)
async def test_run_gradle_rejects_unsafe_extra_args(tmp_path: Path, unsafe_arg: str) -> None:
    with pytest.raises(ValueError, match="extra_args"):
        await run_gradle(tmp_path, ":core:test", timeout_seconds=5, extra_args=[unsafe_arg])


async def test_run_gradle_without_gradlew_is_a_launch_error(tmp_path: Path) -> None:
    outcome = await run_gradle(tmp_path, ":core:test", timeout_seconds=5)

    expected_wrapper = "gradlew.bat" if sys.platform == "win32" else "gradlew"
    assert outcome.exit_code is None
    assert not outcome.timed_out
    assert outcome.launch_error == f"{expected_wrapper} not found in project directory"


async def test_run_gradle_builds_the_wrapper_command(tmp_path: Path) -> None:
    gradlew = _write_fake_gradlew(tmp_path)
    extra = ["--stacktrace", "--tests", "com.example.*Test$Inner", "-Plist=a,b", "@args.txt"]

    outcome = await run_gradle(tmp_path, ":core:test", timeout_seconds=5, extra_args=extra)

    assert outcome.command == (
        str(gradlew),
        ":core:test",
        "--console=plain",
        "--no-daemon",
        *extra,
    )
    assert outcome.exit_code == 0
    lines = outcome.stdout.splitlines()
    assert len(lines) == 2
    assert _same_path(lines[0], tmp_path)
    assert lines[1] == f":core:test --console=plain --no-daemon {' '.join(extra)}"


# --- Integration: real Gradle on the sandbox scenarios ---
async def _run_scenario(project: Path, scenario: str) -> RunOutcome:
    outcome = await run_gradle(
        project,
        ":core:test",
        timeout_seconds=GRADLE_TIMEOUT_SECONDS,
        extra_args=["--stacktrace"],
    )
    logging.getLogger(__name__).info(
        "%s: exit_code=%s duration=%.1fs", scenario, outcome.exit_code, outcome.duration_seconds
    )
    if os.environ.get("GRADLE_HEALER_RECORD_LOGS") == "1":
        await asyncio.to_thread(_record_logs, project, scenario, outcome)
    return outcome


def _fixture_os_dir() -> str:
    match sys.platform:
        case "linux":
            return "linux"
        case "darwin":
            return "macos"
        case "win32":
            return "windows"
        case _:
            raise RuntimeError(f"unsupported platform for log fixtures: {sys.platform!r}")


def _record_logs(project: Path, scenario: str, outcome: RunOutcome) -> None:
    os_dir = _LOG_FIXTURES / _fixture_os_dir()
    os_dir.mkdir(parents=True, exist_ok=True)
    for stream, text in (("stdout", outcome.stdout), ("stderr", outcome.stderr)):
        deterministic = text.replace(str(project), "<PROJECT_ROOT>")
        (os_dir / f"{scenario}.{stream}.txt").write_text(
            deterministic, encoding="utf-8", newline="\n"
        )


@pytest.mark.integration
async def test_gradle_baseline_passes(sandbox_project: Path) -> None:
    outcome = await _run_scenario(sandbox_project, "baseline")

    assert outcome.exit_code == 0, outcome.stdout + outcome.stderr
    assert not outcome.timed_out


@pytest.mark.integration
async def test_gradle_assertion_failure(sandbox_project: Path) -> None:
    apply_scenario(sandbox_project, "assertion_failure")

    outcome = await _run_scenario(sandbox_project, "assertion_failure")

    assert outcome.exit_code == 1
    assert "AssertionFailedError" in outcome.stdout + outcome.stderr


@pytest.mark.integration
async def test_gradle_compile_error(sandbox_project: Path) -> None:
    apply_scenario(sandbox_project, "compile_error")

    outcome = await _run_scenario(sandbox_project, "compile_error")

    output = outcome.stdout + outcome.stderr
    assert outcome.exit_code == 1
    assert "e: file:" in output
    assert "BrokenTest.kt:11:26" in output
