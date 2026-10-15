import asyncio
import logging
import os
import shutil
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest

from gradle_healer.tools import gradle_runner
from gradle_healer.tools.gradle_runner import RunOutcome, run_gradle, run_process

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SANDBOX = _REPO_ROOT / "evals" / "fixtures" / "sandbox-jvm"
_SCENARIOS = _REPO_ROOT / "evals" / "fixtures" / "scenarios"
_SCENARIO_TARGET = Path("core/src/test/kotlin/com/example/sandbox")
_LOG_FIXTURES = _REPO_ROOT / "tests" / "fixtures" / "logs"

# Cold sandbox build takes about 3 minutes.
_GRADLE_TIMEOUT_SECONDS = 600

# SIGKILL is delivered asynchronously and the orphan is reaped by init, so
# "the process is gone" is checked with a short bounded poll.
_POLL_TIMEOUT_SECONDS = 5.0
_POLL_INTERVAL_SECONDS = 0.05


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# ASYNC110 suggests an Event, but nothing notifies us when init reaps an
# orphan, so there is no event to wait on.
async def _poll_until(condition: Callable[[], Awaitable[bool]]) -> None:
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    while not await condition() and time.monotonic() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


async def _wait_for_exit(pid: int) -> None:
    async def gone() -> bool:
        return not _pid_exists(pid)

    await _poll_until(gone)


def _assert_pid_gone(pid: int) -> None:
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def _read_pid_from_fifo(fifo: Path) -> int:
    # Opening a FIFO for reading blocks until the script opens it for writing,
    # so this returns only after the grandchild has been forked.
    async with asyncio.timeout(_POLL_TIMEOUT_SECONDS):
        return int(await asyncio.to_thread(fifo.read_text))


# --- Exit code and streams ---
async def test_captures_exit_code_and_separate_streams(tmp_path: Path) -> None:
    args = ["sh", "-c", "echo out; echo err 1>&2"]

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
    outcome = await run_process(["sh", "-c", "exit 3"], cwd=tmp_path, timeout_seconds=5)

    assert outcome.exit_code == 3
    assert not outcome.timed_out
    assert outcome.launch_error is None


async def test_runs_in_cwd(tmp_path: Path) -> None:
    outcome = await run_process(["pwd", "-P"], cwd=tmp_path, timeout_seconds=5)

    assert outcome.stdout == f"{tmp_path}\n"


# --- Timeout and process group ---
async def test_timeout_preserves_partial_output(tmp_path: Path) -> None:
    outcome = await run_process(
        ["sh", "-c", "echo started; sleep 30"], cwd=tmp_path, timeout_seconds=1
    )

    assert outcome.timed_out
    assert outcome.exit_code is None
    assert "started" in outcome.stdout
    assert outcome.duration_seconds < 5


async def test_timeout_kills_orphaned_grandchild(tmp_path: Path) -> None:
    outcome = await run_process(
        ["sh", "-c", "sleep 30 & echo $!; wait"], cwd=tmp_path, timeout_seconds=1
    )
    grandchild = int(outcome.stdout.split()[0])

    await _wait_for_exit(grandchild)

    assert outcome.timed_out
    _assert_pid_gone(grandchild)


async def test_exit_with_grandchild_holding_pipes_is_not_a_timeout(tmp_path: Path) -> None:
    # The background sleep inherits stdout/stderr, so EOF never comes on its
    # own: only the bounded drain lets the run finish before the timeout.
    outcome = await run_process(
        ["sh", "-c", "sleep 30 & echo $!; exit 0"], cwd=tmp_path, timeout_seconds=10
    )
    grandchild = int(outcome.stdout.split()[0])

    await _wait_for_exit(grandchild)

    assert outcome.exit_code == 0
    assert not outcome.timed_out
    # The 2 s pipe drain must not count towards the reported duration.
    assert outcome.duration_seconds < 1.5
    _assert_pid_gone(grandchild)


async def test_external_cancellation_kills_grandchild(tmp_path: Path) -> None:
    fifo = tmp_path / "pid.fifo"
    os.mkfifo(fifo)
    script = 'sleep 30 & echo $! > "$1"; wait'
    task = asyncio.create_task(
        run_process(["sh", "-c", script, "sh", str(fifo)], cwd=tmp_path, timeout_seconds=30)
    )
    grandchild = await _read_pid_from_fifo(fifo)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _wait_for_exit(grandchild)

    _assert_pid_gone(grandchild)


class _CleanupStarted(logging.Handler):
    """Signals the moment run_process enters its process-group cleanup."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.event = asyncio.Event()

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage().startswith(gradle_runner._KILLING_PROCESS_GROUP_LOG_PREFIX):
            self.event.set()


async def test_second_cancellation_during_cleanup_still_kills_group(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Ignoring SIGTERM (inherited by the grandchild) forces cleanup through the
    # full grace period and the final SIGKILL, which gives the second
    # cancellation a window in which to land.
    fifo = tmp_path / "pid.fifo"
    os.mkfifo(fifo)
    script = 'trap "" TERM; sleep 30 & echo $! > "$1"; wait'
    caplog.set_level(logging.DEBUG, logger=gradle_runner.__name__)
    cleanup_started = _CleanupStarted()
    gradle_runner.logger.addHandler(cleanup_started)
    try:
        task = asyncio.create_task(
            run_process(["sh", "-c", script, "sh", str(fifo)], cwd=tmp_path, timeout_seconds=30)
        )
        grandchild = await _read_pid_from_fifo(fifo)

        task.cancel()
        await cleanup_started.event.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        gradle_runner.logger.removeHandler(cleanup_started)
    await _wait_for_exit(grandchild)

    _assert_pid_gone(grandchild)


# --- Launch errors ---
async def test_missing_executable_is_a_launch_error(tmp_path: Path) -> None:
    outcome = await run_process([str(tmp_path / "does-not-exist")], cwd=tmp_path, timeout_seconds=5)

    assert outcome.exit_code is None
    assert not outcome.timed_out
    assert outcome.launch_error == "cannot launch 'does-not-exist': No such file or directory"
    assert (outcome.stdout, outcome.stderr) == ("", "")


async def test_non_executable_file_is_a_launch_error(tmp_path: Path) -> None:
    script = tmp_path / "script.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o644)

    outcome = await run_process([str(script)], cwd=tmp_path, timeout_seconds=5)

    assert outcome.exit_code is None
    assert outcome.launch_error == "cannot launch 'script.sh': Permission denied"
    assert str(tmp_path) not in outcome.launch_error


# --- Output handling ---
async def test_output_is_tail_truncated(tmp_path: Path) -> None:
    full = "".join(f"{i}\n" for i in range(1, 1001))
    limit = 100

    outcome = await run_process(
        ["seq", "1", "1000"], cwd=tmp_path, timeout_seconds=5, max_output_bytes=limit
    )

    dropped = len(full) - limit
    assert outcome.stdout == f"[gradle-healer: {dropped} bytes truncated]\n{full[-limit:]}"


async def test_output_within_limit_has_no_marker(tmp_path: Path) -> None:
    outcome = await run_process(
        ["sh", "-c", "printf 12345"], cwd=tmp_path, timeout_seconds=5, max_output_bytes=5
    )

    assert outcome.stdout == "12345"


async def test_output_is_sanitized(tmp_path: Path) -> None:
    script = r"printf '\033[1;31mred\033[0m\r\nnext\rbar\033]0;title\007\n\377\n'"

    outcome = await run_process(["sh", "-c", script], cwd=tmp_path, timeout_seconds=5)

    assert outcome.stdout == "red\nnext\nbar\n�\n"


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
            ["true"],
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


async def test_run_gradle_without_gradlew_is_a_launch_error(tmp_path: Path) -> None:
    outcome = await run_gradle(tmp_path, ":core:test", timeout_seconds=5)

    assert outcome.exit_code is None
    assert not outcome.timed_out
    assert outcome.launch_error == "gradlew not found in project directory"


async def test_run_gradle_builds_the_wrapper_command(tmp_path: Path) -> None:
    gradlew = tmp_path / "gradlew"
    gradlew.write_text('#!/bin/sh\npwd -P\necho "$@"\n')
    gradlew.chmod(0o755)

    outcome = await run_gradle(
        tmp_path, ":core:test", timeout_seconds=5, extra_args=["--stacktrace"]
    )

    assert outcome.command == (
        str(gradlew),
        ":core:test",
        "--console=plain",
        "--no-daemon",
        "--stacktrace",
    )
    assert outcome.exit_code == 0
    assert outcome.stdout == (f"{tmp_path}\n:core:test --console=plain --no-daemon --stacktrace\n")


# --- Integration: real Gradle on the sandbox scenarios ---
@pytest.fixture(scope="session")
def sandbox_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    template = tmp_path_factory.mktemp("sandbox-template") / "sandbox-jvm"
    shutil.copytree(
        _SANDBOX, template, ignore=shutil.ignore_patterns("build", ".gradle", ".kotlin")
    )
    return template


@pytest.fixture
async def sandbox_project(sandbox_template: Path, tmp_path: Path) -> AsyncIterator[Path]:
    project = tmp_path / "sandbox-jvm"
    shutil.copytree(sandbox_template, project)
    yield project
    await _assert_no_process_mentions(project)


async def _assert_no_process_mentions(project: Path) -> None:
    # pgrep exits with 1 when no process command line matches.
    async def pgrep() -> RunOutcome:
        return await run_process(["pgrep", "-af", str(project)], cwd=project, timeout_seconds=10)

    async def none_alive() -> bool:
        return (await pgrep()).exit_code == 1

    await _poll_until(none_alive)
    outcome = await pgrep()
    assert outcome.exit_code == 1, f"processes still alive:\n{outcome.stdout}"


def _apply_scenario(project: Path, scenario: str) -> None:
    for source in (_SCENARIOS / scenario).glob("*.kt"):
        shutil.copy(source, project / _SCENARIO_TARGET / source.name)


async def _run_scenario(project: Path, scenario: str) -> RunOutcome:
    outcome = await run_gradle(
        project,
        ":core:test",
        timeout_seconds=_GRADLE_TIMEOUT_SECONDS,
        extra_args=["--stacktrace"],
    )
    logging.getLogger(__name__).info(
        "%s: exit_code=%s duration=%.1fs", scenario, outcome.exit_code, outcome.duration_seconds
    )
    if os.environ.get("GRADLE_HEALER_RECORD_LOGS") == "1":
        await asyncio.to_thread(_record_logs, project, scenario, outcome)
    return outcome


def _record_logs(project: Path, scenario: str, outcome: RunOutcome) -> None:
    _LOG_FIXTURES.mkdir(parents=True, exist_ok=True)
    for stream, text in (("stdout", outcome.stdout), ("stderr", outcome.stderr)):
        deterministic = text.replace(str(project), "<PROJECT_ROOT>")
        (_LOG_FIXTURES / f"{scenario}.{stream}.txt").write_text(deterministic)


@pytest.mark.integration
async def test_gradle_baseline_passes(sandbox_project: Path) -> None:
    outcome = await _run_scenario(sandbox_project, "baseline")

    assert outcome.exit_code == 0, outcome.stdout + outcome.stderr
    assert not outcome.timed_out


@pytest.mark.integration
async def test_gradle_assertion_failure(sandbox_project: Path) -> None:
    _apply_scenario(sandbox_project, "assertion_failure")

    outcome = await _run_scenario(sandbox_project, "assertion_failure")

    assert outcome.exit_code == 1
    assert "AssertionFailedError" in outcome.stdout + outcome.stderr


@pytest.mark.integration
async def test_gradle_compile_error(sandbox_project: Path) -> None:
    _apply_scenario(sandbox_project, "compile_error")

    outcome = await _run_scenario(sandbox_project, "compile_error")

    output = outcome.stdout + outcome.stderr
    assert outcome.exit_code == 1
    assert "e: file:" in output
    assert "BrokenTest.kt:11:26" in output
