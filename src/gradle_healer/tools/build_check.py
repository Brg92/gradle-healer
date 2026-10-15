"""Run one Gradle task and interpret its output as a BuildResult.

Glue between the raw runner (gradle_runner) and the pure interpretation
(log_parser): the only place where both the filesystem and the parser meet.
"""

import asyncio
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from gradle_healer.core.schemas import BuildResult
from gradle_healer.tools.gradle_runner import run_gradle
from gradle_healer.tools.log_parser import parse_build, validate_target_package

_SOURCE_SUFFIXES = (".kt", ".java")
_SKIPPED_DIRS = frozenset({"build", ".gradle"})


# --- Source index ---
# Stack traces name a source file without its directory; the index turns
# "GetUserUseCaseFailingTest.kt" back into a project-relative path. A name that
# occurs more than once is ambiguous and left out rather than guessed.
def build_source_index(project_dir: Path) -> dict[str, str]:
    """Map each Kotlin/Java source file name under a src/ directory to its relative path.

    build/ and .gradle/ directories are skipped; file names that occur more
    than once are omitted. Blocking: call it off the event loop.
    """
    found: list[tuple[str, str]] = []
    for directory, dir_names, file_names in project_dir.walk():
        relative_dir = directory.relative_to(project_dir)
        if "src" not in relative_dir.parts:
            # Only above src/: inside it, "build" can be a package name.
            dir_names[:] = [name for name in dir_names if name not in _SKIPPED_DIRS]
            continue
        found.extend(
            (name, (relative_dir / name).as_posix())
            for name in file_names
            if name.endswith(_SOURCE_SUFFIXES)
        )

    occurrences = Counter(name for name, _ in found)
    return {name: path for name, path in found if occurrences[name] == 1}


# --- check_build ---
async def check_build(
    project_dir: Path,
    task: str,
    *,
    target_package: str,
    timeout_seconds: float,
    extra_args: Sequence[str] = (),
) -> BuildResult:
    """Run task through the project's Gradle wrapper and return the interpreted result.

    Invalid arguments raise ValueError before Gradle is launched; every build
    outcome, including launch failures and timeouts, is returned as data.
    """
    validate_target_package(target_package)
    outcome = await run_gradle(
        project_dir, task, timeout_seconds=timeout_seconds, extra_args=extra_args
    )
    project_root = await asyncio.to_thread(project_dir.resolve)
    source_index = await asyncio.to_thread(build_source_index, project_root)
    return parse_build(
        outcome,
        task=task,
        project_root=str(project_root),
        target_package=target_package,
        source_index=source_index,
    )
