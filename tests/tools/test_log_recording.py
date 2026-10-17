import sys
from pathlib import Path

import pytest

from tests.tools.log_recording import (
    POSIX_PROJECT_ROOT,
    WINDOWS_PROJECT_ROOT_BACKSLASH,
    WINDOWS_PROJECT_ROOT_SLASH,
    assert_no_leak,
    redact_project_root,
    windows_short_path,
)


def test_redact_linux_paths() -> None:
    project = Path("/tmp/pytest-of-runner/pytest-0/test_compile0/sandbox-jvm")
    raw = (
        f"e: file://{project}/core/src/test/kotlin/BrokenTest.kt:11:26 Unresolved\n"
        f"See report at: file://{project}/core/build/reports/tests/test/index.html\n"
    )

    redacted = redact_project_root(raw, project, platform="linux")
    assert_no_leak(redacted, project, platform="linux")

    assert redacted == (
        f"e: file://{POSIX_PROJECT_ROOT}/core/src/test/kotlin/BrokenTest.kt:11:26 Unresolved\n"
        f"See report at: file://{POSIX_PROJECT_ROOT}/core/build/reports/tests/test/index.html\n"
    )


def test_redact_macos_var_and_private_var_forms() -> None:
    project = Path("/var/folders/xx/12345/T/pytest-of-runner/pytest-0/sandbox-jvm")
    private_project = f"/private{project}"
    raw = (
        f"e: file://{private_project}/core/src/test/kotlin/BrokenTest.kt:11:26 Unresolved\n"
        f"Working directory: {project}/core\n"
    )

    redacted = redact_project_root(raw, project, platform="darwin")
    assert_no_leak(redacted, project, platform="darwin")

    assert redacted == (
        f"e: file://{POSIX_PROJECT_ROOT}/core/src/test/kotlin/BrokenTest.kt:11:26 Unresolved\n"
        f"Working directory: {POSIX_PROJECT_ROOT}/core\n"
    )


def test_redact_windows_forward_slash_backslash_short_and_mixed_case() -> None:
    long_root = (
        r"C:\Users\runneradmin\AppData\Local\Temp\pytest-of-runneradmin\pytest-0\sandbox-jvm"
    )
    short_root = r"C:\Users\RUNNER~1\AppData\Local\Temp\PYTEST~1\pytest-0\sandbox-jvm"
    project = Path(long_root)
    raw = (
        "e: file:///C:/Users/runneradmin/AppData/Local/Temp/pytest-of-runneradmin/"
        "pytest-0/sandbox-jvm/core/src/test/kotlin/BrokenTest.kt:11:26 Unresolved\n"
        "e: file:///c:/users/runneradmin/appdata/local/temp/pytest-of-runneradmin/"
        "pytest-0/sandbox-jvm/core/src/test/kotlin/BrokenTest.kt:18:36 Mismatch\n"
        f"See the report at: file:///C:/Users/RUNNER~1/AppData/Local/Temp/PYTEST~1/"
        "pytest-0/sandbox-jvm/core/build/reports/tests/test/index.html\n"
        rf"Execution failed in {long_root}\core\build.gradle.kts" + "\n"
        rf"Short path: {short_root}\core" + "\n"
    )

    redacted = redact_project_root(raw, project, platform="win32", extra_roots=[short_root])
    assert_no_leak(redacted, project, platform="win32", extra_roots=[short_root])

    assert redacted == (
        f"e: file:///{WINDOWS_PROJECT_ROOT_SLASH}/"
        "core/src/test/kotlin/BrokenTest.kt:11:26 Unresolved\n"
        f"e: file:///{WINDOWS_PROJECT_ROOT_SLASH}/"
        "core/src/test/kotlin/BrokenTest.kt:18:36 Mismatch\n"
        f"See the report at: file:///{WINDOWS_PROJECT_ROOT_SLASH}/"
        "core/build/reports/tests/test/index.html\n"
        rf"Execution failed in {WINDOWS_PROJECT_ROOT_BACKSLASH}\core\build.gradle.kts" + "\n"
        rf"Short path: {WINDOWS_PROJECT_ROOT_BACKSLASH}\core" + "\n"
    )


@pytest.mark.parametrize(
    "leaked_text",
    [
        "/tmp/pytest-of-user/pytest-0/other-dir/file.kt",
        r"C:\Users\runneradmin\AppData\Local\Temp\sandbox-jvm\core",
        "file:///c:/users/runneradmin/appdata/local/temp/sandbox-jvm/core",
    ],
)
def test_assert_no_leak_rejects_unredacted_paths(leaked_text: str) -> None:
    project = Path(r"C:\Users\runneradmin\AppData\Local\Temp\sandbox-jvm")

    with pytest.raises(AssertionError, match="leaked in recorded log"):
        assert_no_leak(leaked_text, project, platform="win32")


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="GetShortPathNameW kernel32 API only exists on Windows",
)
def test_windows_short_path_resolves_existing_directory(tmp_path: Path) -> None:
    short = windows_short_path(tmp_path)

    assert short is not None
    assert Path(short).exists()
