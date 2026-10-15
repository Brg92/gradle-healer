"""Helpers shared by the tests that run real Gradle on the sandbox project."""

import asyncio
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from gradle_healer.tools.gradle_runner import RunOutcome, run_process

REPO_ROOT = Path(__file__).resolve().parents[2]
SANDBOX = REPO_ROOT / "evals" / "fixtures" / "sandbox-jvm"
SCENARIOS = REPO_ROOT / "evals" / "fixtures" / "scenarios"
SCENARIO_TARGET = Path("core/src/test/kotlin/com/example/sandbox")

# Cold sandbox build takes about 3 minutes.
GRADLE_TIMEOUT_SECONDS = 600

# SIGKILL is delivered asynchronously and the orphan is reaped by init, so
# "the process is gone" is checked with a short bounded poll.
POLL_TIMEOUT_SECONDS = 5.0
_POLL_INTERVAL_SECONDS = 0.05


# ASYNC110 suggests an Event, but nothing notifies us when init reaps an
# orphan, so there is no event to wait on.
async def poll_until(condition: Callable[[], Awaitable[bool]]) -> None:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while not await condition() and time.monotonic() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


async def assert_no_process_mentions(project: Path) -> None:
    # pgrep exits with 1 when no process command line matches.
    async def pgrep() -> RunOutcome:
        return await run_process(["pgrep", "-af", str(project)], cwd=project, timeout_seconds=10)

    async def none_alive() -> bool:
        return (await pgrep()).exit_code == 1

    await poll_until(none_alive)
    outcome = await pgrep()
    assert outcome.exit_code == 1, f"processes still alive:\n{outcome.stdout}"


def apply_scenario(project: Path, scenario: str) -> None:
    for source in (SCENARIOS / scenario).glob("*.kt"):
        shutil.copy(source, project / SCENARIO_TARGET / source.name)
