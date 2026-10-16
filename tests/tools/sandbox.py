"""Helpers shared by the tests that run real Gradle on the sandbox project."""

import asyncio
import os
import shutil
from pathlib import Path

import psutil

REPO_ROOT = Path(__file__).resolve().parents[2]
SANDBOX = REPO_ROOT / "evals" / "fixtures" / "sandbox-jvm"
SCENARIOS = REPO_ROOT / "evals" / "fixtures" / "scenarios"
SCENARIO_TARGET = Path("core/src/test/kotlin/com/example/sandbox")

# Cold sandbox build takes about 3 minutes.
GRADLE_TIMEOUT_SECONDS = 600

# Process termination is delivered asynchronously by the OS kernel, so waiting
# for a killed process or process tree to disappear uses a short bounded timeout.
POLL_TIMEOUT_SECONDS = 5.0


def _norm_path(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(path))).replace("\\", "/")


def _is_alive(proc: psutil.Process) -> bool:
    # A process in zombie status counts as dead (it has been killed, only not reaped yet).
    # AccessDenied is treated as alive so an uninspectable process never hides an orphan.
    try:
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


def is_pid_alive(pid: int) -> bool:
    """Return True if pid exists and is not a zombie."""
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True
    return _is_alive(proc)


async def wait_for_pid_exit(pid: int) -> None:
    """Wait up to POLL_TIMEOUT_SECONDS for pid to terminate."""
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    except psutil.AccessDenied:
        return
    if not _is_alive(proc):
        return
    await asyncio.to_thread(psutil.wait_procs, [proc], timeout=POLL_TIMEOUT_SECONDS)


def _find_processes_mentioning(project: Path) -> list[psutil.Process]:
    target = _norm_path(project).rstrip("/")
    target_prefix = target + "/"
    matches: list[psutil.Process] = []
    for proc in psutil.process_iter():
        try:
            if proc.status() == psutil.STATUS_ZOMBIE:
                continue
            cmdline = [_norm_path(arg) for arg in proc.cmdline()]
            try:
                cwd = _norm_path(proc.cwd())
            except (psutil.NoSuchProcess, psutil.AccessDenied, FileNotFoundError):
                cwd = ""
            mentions_cmd = any(target in arg for arg in cmdline)
            inside_cwd = cwd == target or cwd.startswith(target_prefix)
            if mentions_cmd or inside_cwd:
                matches.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return matches


def _describe_process(proc: psutil.Process) -> str:
    try:
        return f"pid={proc.pid} name={proc.name()!r} cmdline={proc.cmdline()!r}"
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return f"pid={proc.pid} (alive or uninspectable)"


async def assert_no_process_mentions(project: Path) -> None:
    matches = await asyncio.to_thread(_find_processes_mentioning, project)
    if matches:
        await asyncio.to_thread(psutil.wait_procs, matches, timeout=POLL_TIMEOUT_SECONDS)
    alive = [proc for proc in matches if _is_alive(proc)]
    details = "\n".join(_describe_process(proc) for proc in alive)
    assert not alive, f"processes still alive:\n{details}"


def apply_scenario(project: Path, scenario: str) -> None:
    for source in (SCENARIOS / scenario).glob("*.kt"):
        shutil.copy(source, project / SCENARIO_TARGET / source.name)
