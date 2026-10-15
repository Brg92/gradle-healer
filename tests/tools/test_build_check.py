from pathlib import Path

import pytest

from gradle_healer.core.schemas import (
    BuildResult,
    CompilationError,
    FailedTestError,
    FailureKind,
    SourceLocation,
)
from gradle_healer.tools.build_check import build_source_index, check_build
from tests.tools.sandbox import GRADLE_TIMEOUT_SECONDS, SCENARIO_TARGET, apply_scenario

_PACKAGE = "com.example.sandbox"


def _touch(root: Path, relative: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


# --- Source index ---
def test_source_index_maps_unique_source_names_under_src(tmp_path: Path) -> None:
    for relative in (
        "core/src/main/kotlin/a/User.kt",
        "core/src/test/java/a/UserTest.java",
        "core/src/main/kotlin/a/Dup.kt",
        "app/src/main/kotlin/b/Dup.kt",
        "core/build/generated/src/Generated.kt",
        "core/src/main/kotlin/build/InBuildDir.kt",
        ".gradle/src/Cached.kt",
        "buildSrc/Plugin.kt",
        "core/src/main/resources/notes.txt",
    ):
        _touch(tmp_path, relative)

    assert build_source_index(tmp_path) == {
        "User.kt": "core/src/main/kotlin/a/User.kt",
        "UserTest.java": "core/src/test/java/a/UserTest.java",
        "InBuildDir.kt": "core/src/main/kotlin/build/InBuildDir.kt",
    }


def test_source_index_of_a_missing_directory_is_empty(tmp_path: Path) -> None:
    assert build_source_index(tmp_path / "missing") == {}


async def test_check_build_rejects_an_invalid_package_before_running(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="target_package"):
        await check_build(tmp_path, ":core:test", target_package="com.", timeout_seconds=5)


async def test_check_build_without_gradlew_is_infrastructure(tmp_path: Path) -> None:
    result = await check_build(tmp_path, ":core:test", target_package=_PACKAGE, timeout_seconds=5)

    assert result.failure_kind is FailureKind.INFRASTRUCTURE
    assert result.summary == "gradlew not found in project directory"


# --- Integration: real Gradle on the sandbox scenarios ---
async def _check(project: Path) -> BuildResult:
    return await check_build(
        project,
        ":core:test",
        target_package=_PACKAGE,
        timeout_seconds=GRADLE_TIMEOUT_SECONDS,
        extra_args=["--stacktrace"],
    )


@pytest.mark.integration
async def test_check_build_baseline_succeeds(sandbox_project: Path) -> None:
    result = await _check(sandbox_project)

    assert result.success, result.stdout + result.stderr
    assert result.failure_kind is None
    assert result.errors == []


@pytest.mark.integration
async def test_check_build_compile_error(sandbox_project: Path) -> None:
    apply_scenario(sandbox_project, "compile_error")

    result = await _check(sandbox_project)

    broken_test = (SCENARIO_TARGET / "BrokenTest.kt").as_posix()
    assert result.failure_kind is FailureKind.COMPILE_ERROR
    assert result.failed_task == ":core:compileTestKotlin"
    assert [error.location for error in result.errors if isinstance(error, CompilationError)] == [
        SourceLocation(file=broken_test, line=11, column=26),
        SourceLocation(file=broken_test, line=11, column=40),
        SourceLocation(file=broken_test, line=18, column=36),
    ]
    assert result.errors[0].is_primary


@pytest.mark.integration
async def test_check_build_assertion_failure(sandbox_project: Path) -> None:
    apply_scenario(sandbox_project, "assertion_failure")

    result = await _check(sandbox_project)

    assert result.failure_kind is FailureKind.TEST_FAILURE
    assert result.failed_task == ":core:test"
    assert len(result.errors) == 1
    assert isinstance(error := result.errors[0], FailedTestError)
    assert error.test_class == "com.example.sandbox.GetUserUseCaseFailingTest"
    assert (error.expected, error.actual) == ("Grace", "Ada")
    assert error.location == SourceLocation(
        file="core/src/test/kotlin/com/example/sandbox/GetUserUseCaseFailingTest.kt", line=20
    )
