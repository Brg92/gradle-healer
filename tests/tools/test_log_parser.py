import random
from collections.abc import Mapping
from pathlib import Path

import pytest

from gradle_healer.core.schemas import (
    BuildResult,
    CompilationError,
    FailedTestError,
    FailureKind,
    SourceLocation,
)
from gradle_healer.tools.gradle_runner import RunOutcome
from gradle_healer.tools.log_parser import MAX_ERRORS, parse_build

_LOG_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "logs"
_PROJECT_ROOT = "<PROJECT_ROOT>"
_PACKAGE = "com.example.sandbox"
_SANDBOX_DIR = "core/src/test/kotlin/com/example/sandbox"
_BROKEN_TEST = f"{_SANDBOX_DIR}/BrokenTest.kt"
_FAILING_TEST = f"{_SANDBOX_DIR}/GetUserUseCaseFailingTest.kt"


def _outcome(
    *,
    exit_code: int | None = 1,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
    launch_error: str | None = None,
    duration_seconds: float = 12.5,
) -> RunOutcome:
    return RunOutcome(
        command=("./gradlew", ":core:test", "--console=plain", "--no-daemon"),
        exit_code=exit_code,
        timed_out=timed_out,
        launch_error=launch_error,
        duration_seconds=duration_seconds,
        stdout=stdout,
        stderr=stderr,
    )


def _recorded(scenario: str, exit_code: int) -> RunOutcome:
    return _outcome(
        exit_code=exit_code,
        stdout=(_LOG_FIXTURES / f"{scenario}.stdout.txt").read_text(),
        stderr=(_LOG_FIXTURES / f"{scenario}.stderr.txt").read_text(),
    )


def _parse(
    outcome: RunOutcome,
    *,
    task: str = ":core:test",
    source_index: Mapping[str, str] | None = None,
) -> BuildResult:
    result = parse_build(
        outcome,
        task=task,
        project_root=_PROJECT_ROOT,
        target_package=_PACKAGE,
        source_index=source_index,
    )
    # Every result must survive the JSON round-trip used for checkpointing.
    assert BuildResult.model_validate_json(result.model_dump_json()) == result
    return result


def _compiler_line(line: int, message: str = "Unresolved reference 'X'.") -> str:
    return f"e: file://{_PROJECT_ROOT}/{_BROKEN_TEST}:{line}:5 {message}"


def _test_block(exception_line: str, *frames: str) -> str:
    lines = ["> Task :core:test FAILED", "", "FooTest > does something() FAILED", exception_line]
    return "\n".join([*lines, *(f"        at {frame}" for frame in frames), "1 task"]) + "\n"


# --- Recorded fixtures ---
def test_baseline_is_a_success() -> None:
    outcome = _recorded("baseline", exit_code=0)
    assert "Sharing is only supported" in outcome.stderr

    result = _parse(outcome)

    assert result.success
    assert result.failure_kind is None
    assert result.errors == []
    assert result.failed_task is None
    assert result.summary is None


def test_compile_error_extracts_relative_compiler_errors() -> None:
    result = _parse(_recorded("compile_error", exit_code=1))

    assert result.failure_kind is FailureKind.COMPILE_ERROR
    assert result.failed_task == ":core:compileTestKotlin"
    assert result.summary is None
    assert result.errors == [
        CompilationError(
            location=SourceLocation(file=_BROKEN_TEST, line=11, column=26),
            message="Unresolved reference 'UserService'.",
            is_primary=True,
        ),
        CompilationError(
            location=SourceLocation(file=_BROKEN_TEST, line=11, column=40),
            message="Cannot infer type for type parameter 'T'. Specify it explicitly.",
            is_primary=False,
        ),
        CompilationError(
            location=SourceLocation(file=_BROKEN_TEST, line=18, column=36),
            message="Argument type mismatch: actual type is 'Int', but 'String' was expected.",
            is_primary=False,
        ),
    ]


def _expected_assertion_failure(location: SourceLocation | None) -> FailedTestError:
    return FailedTestError(
        test_class="com.example.sandbox.GetUserUseCaseFailingTest",
        test_method="invoke returns the user name()",
        exception_type="org.opentest4j.AssertionFailedError",
        message="expected: <Grace> but was: <Ada>",
        expected="Grace",
        actual="Ada",
        location=location,
        is_primary=True,
    )


def test_assertion_failure_without_index_has_no_location() -> None:
    result = _parse(_recorded("assertion_failure", exit_code=1))

    assert result.failure_kind is FailureKind.TEST_FAILURE
    assert result.failed_task == ":core:test"
    assert result.errors == [_expected_assertion_failure(location=None)]


def test_assertion_failure_location_is_resolved_with_the_index() -> None:
    index = {"GetUserUseCaseFailingTest.kt": _FAILING_TEST, "User.kt": "core/src/main/User.kt"}

    result = _parse(_recorded("assertion_failure", exit_code=1), source_index=index)

    assert result.errors == [
        _expected_assertion_failure(location=SourceLocation(file=_FAILING_TEST, line=20))
    ]


def test_assertion_failure_location_needs_the_file_in_the_index() -> None:
    result = _parse(_recorded("assertion_failure", exit_code=1), source_index={"Other.kt": "x"})

    assert isinstance(error := result.errors[0], FailedTestError)
    assert error.location is None


# --- Launch error and timeout ---
def test_launch_error_is_infrastructure() -> None:
    outcome = _outcome(exit_code=None, launch_error="gradlew not found in project directory")

    result = _parse(outcome)

    assert result.failure_kind is FailureKind.INFRASTRUCTURE
    assert result.exit_code is None
    assert result.summary == "gradlew not found in project directory"
    assert result.errors == []


def test_timeout_has_summary_and_no_errors() -> None:
    outcome = _outcome(
        exit_code=None,
        timed_out=True,
        duration_seconds=600.02,
        stdout="> Task :core:compileTestKotlin FAILED\n" + _compiler_line(3),
    )

    result = _parse(outcome)

    assert result.failure_kind is FailureKind.TIMEOUT
    assert result.timed_out
    assert result.summary == "Timed out after 600 s"
    assert result.errors == []
    assert result.failed_task is None


# --- Fallback classification ---
def test_unknown_failure_is_infrastructure_with_what_went_wrong() -> None:
    stderr = (
        "FAILURE: Build failed with an exception.\n\n"
        "* What went wrong:\n"
        "Could not resolve all files for configuration ':core:testRuntimeClasspath'.\n"
        "> Could not find io.mockk:mockk:9.9.9.\n\n"
        "* Try:\n"
    )

    result = _parse(_outcome(stderr=stderr))

    assert result.failure_kind is FailureKind.INFRASTRUCTURE
    assert result.failed_task is None
    assert result.summary == (
        "Could not resolve all files for configuration ':core:testRuntimeClasspath'. "
        "Could not find io.mockk:mockk:9.9.9."
    )


def test_summary_strips_the_project_root() -> None:
    stderr = f"* What went wrong:\nBroken file {_PROJECT_ROOT}/core/build.gradle.kts\n"

    result = _parse(_outcome(stderr=stderr))

    assert result.summary == "Broken file core/build.gradle.kts"


@pytest.mark.parametrize(
    ("project_root", "in_log"),
    [
        (r"C:\Users\dev\repo", r"C:\Users\dev\repo\core\build.gradle.kts"),
        (r"C:\Users\dev\repo", "file:///C:/Users/dev/repo/core/build/reports/index.html"),
        ("C:/Users/dev/repo", r"C:\Users\dev\repo\core\build.gradle.kts"),
    ],
)
def test_summary_strips_windows_project_root(project_root: str, in_log: str) -> None:
    stderr = f"* What went wrong:\nBroken file {in_log}\n"

    result = parse_build(
        _outcome(stderr=stderr),
        task=":core:test",
        project_root=project_root,
        target_package=_PACKAGE,
    )

    assert project_root not in (result.summary or "")
    assert "Users" not in (result.summary or "")


def test_unknown_failure_without_details_reports_the_exit_code() -> None:
    result = _parse(_outcome(exit_code=137, stdout="Killed\n"))

    assert result.failure_kind is FailureKind.INFRASTRUCTURE
    assert result.summary == "Gradle exited with code 137"


def test_missing_exit_code_without_timeout_is_infrastructure() -> None:
    result = _parse(_outcome(exit_code=None, stderr=_compiler_line(3)))

    assert result.failure_kind is FailureKind.INFRASTRUCTURE
    assert result.errors == []


@pytest.mark.parametrize(
    ("failed_task", "failure_kind"),
    [
        (":core:compileTestKotlin", FailureKind.COMPILE_ERROR),
        (":app:compileDebugJavaWithJavac", FailureKind.COMPILE_ERROR),
        (":core:test", FailureKind.TEST_FAILURE),
        (":app:testDebugUnitTest", FailureKind.TEST_FAILURE),
        (":core:jar", FailureKind.INFRASTRUCTURE),
    ],
)
def test_failed_task_kind_is_the_fallback(failed_task: str, failure_kind: FailureKind) -> None:
    stderr = f"* What went wrong:\nExecution failed for task '{failed_task}'.\n> Unparsed.\n"

    result = _parse(_outcome(stderr=stderr))

    assert result.failure_kind is failure_kind
    assert result.failed_task == failed_task
    assert result.errors == []
    assert result.summary == f"Execution failed for task '{failed_task}'. Unparsed."


def test_invalid_failed_task_is_ignored() -> None:
    result = _parse(_outcome(stdout="> Task :core:te$t FAILED\n> Task :: FAILED\n"))

    assert result.failed_task is None


# --- Compiler errors ---
def test_compiler_warnings_are_ignored() -> None:
    stderr = f"w: file://{_PROJECT_ROOT}/{_BROKEN_TEST}:1:1 Unused variable.\n" + _compiler_line(2)

    result = _parse(_outcome(stderr=stderr))

    lines = [e.location.line for e in result.errors if isinstance(e, CompilationError)]
    assert lines == [2]


def test_duplicate_compiler_errors_are_deduplicated_in_order() -> None:
    stderr = "\n".join([_compiler_line(5), _compiler_line(2), _compiler_line(5)])
    stdout = _compiler_line(2) + "\n" + _compiler_line(9)

    result = _parse(_outcome(stdout=stdout, stderr=stderr))

    lines = [e.location.line for e in result.errors if isinstance(e, CompilationError)]
    assert lines == [5, 2, 9]
    assert [error.is_primary for error in result.errors] == [True, False, False]


def test_compiler_errors_are_capped() -> None:
    stderr = "\n".join(_compiler_line(line) for line in range(1, 9))

    result = _parse(_outcome(stderr=stderr))

    lines = [e.location.line for e in result.errors if isinstance(e, CompilationError)]
    assert lines == list(range(1, MAX_ERRORS + 1))


def test_compiler_errors_outside_the_root_are_excluded_and_counted() -> None:
    stderr = "\n".join(
        [
            "e: file:///elsewhere/Generated.kt:1:1 Outside one.",
            _compiler_line(4),
            "e: /elsewhere/Other.kt:2:2 Outside two.",
        ]
    )

    result = _parse(_outcome(stderr=stderr))

    assert result.failure_kind is FailureKind.COMPILE_ERROR
    assert len(result.errors) == 1
    assert result.summary == "2 compiler errors outside the project root"


def test_only_compiler_errors_outside_the_root_still_classify_as_compile_error() -> None:
    result = _parse(_outcome(stderr="e: file:///elsewhere/Generated.kt:1:1 Outside.\n"))

    assert result.failure_kind is FailureKind.COMPILE_ERROR
    assert result.errors == []
    assert result.summary == "1 compiler error outside the project root"


def test_long_compiler_message_is_truncated() -> None:
    result = _parse(_outcome(stderr=_compiler_line(1, "x" * 2000)))

    assert isinstance(error := result.errors[0], CompilationError)
    assert len(error.message) == 500
    assert error.message.endswith("…")


@pytest.mark.parametrize(
    ("project_root", "error_prefix"),
    [
        (r"C:\Users\runner\work\repo", "e: file:///C:/Users/runner/work/repo/"),
        (r"C:\Users\runner\work\repo", "e: file:/C:/Users/runner/work/repo/"),
        (r"C:\Users\runner\work\repo", "e: file://C:/Users/runner/work/repo/"),
        (r"C:\Users\runner\work\repo", "e: file:C:/Users/runner/work/repo/"),
        (r"C:\Users\runner\work\repo", r"e: C:\Users\runner\work\repo\\".rstrip("\\") + "\\"),
        (r"C:\Users\runner\work\repo", "e: C:/Users/runner/work/repo/"),
        (r"c:\users\runner\work\repo", "e: file:///C:/Users/runner/work/repo/"),
        (r"C:\Users\Runner\Work\Repo", "e: file:///c:/users/runner/work/repo/"),
        ("/Users/runner/work/repo", "e: file:///Users/runner/work/repo/"),
        (
            "/private/var/folders/xx/T/pytest-0/sandbox",
            "e: /private/var/folders/xx/T/pytest-0/sandbox/",
        ),
    ],
)
def test_compiler_error_paths_across_platforms_normalize_to_posix_relative(
    project_root: str, error_prefix: str
) -> None:
    rel_file = _BROKEN_TEST.replace("/", "\\") if "\\" in error_prefix else _BROKEN_TEST
    stderr = f"{error_prefix}{rel_file}:11:26 Unresolved reference 'UserService'.\n"

    result = parse_build(
        _outcome(stderr=stderr),
        task=":core:test",
        project_root=project_root,
        target_package=_PACKAGE,
    )

    assert result.failure_kind is FailureKind.COMPILE_ERROR
    assert result.summary is None
    assert result.errors == [
        CompilationError(
            location=SourceLocation(file=_BROKEN_TEST, line=11, column=26),
            message="Unresolved reference 'UserService'.",
            is_primary=True,
        )
    ]


def test_windows_compiler_error_outside_root_is_counted() -> None:
    stderr = (
        "e: file:///D:/other/Generated.kt:1:1 Outside on D drive.\n"
        r"e: C:\Users\runner\other\Other.kt:2:2 Outside on C drive." + "\n"
    )

    result = parse_build(
        _outcome(stderr=stderr),
        task=":core:test",
        project_root=r"C:\Users\runner\work\repo",
        target_package=_PACKAGE,
    )

    assert result.failure_kind is FailureKind.COMPILE_ERROR
    assert result.errors == []
    assert result.summary == "2 compiler errors outside the project root"


# --- Test failures ---
def test_exception_without_expected_and_actual() -> None:
    stdout = _test_block(
        "    java.lang.IllegalStateException: boom",
        "app//com.example.sandbox.FooTest.does something(FooTest.kt:7)",
    )

    result = _parse(_outcome(stdout=stdout), source_index={"FooTest.kt": "core/FooTest.kt"})

    assert result.errors == [
        FailedTestError(
            test_class="com.example.sandbox.FooTest",
            test_method="does something()",
            exception_type="java.lang.IllegalStateException",
            message="boom",
            location=SourceLocation(file="core/FooTest.kt", line=7),
            is_primary=True,
        )
    ]


def test_exception_without_message_and_frames_falls_back_to_simple_name() -> None:
    result = _parse(_outcome(stdout=_test_block("    io.mockk.MockKException")))

    assert isinstance(error := result.errors[0], FailedTestError)
    assert error.test_class == "FooTest"
    assert error.message == ""
    assert error.location is None


def test_location_is_the_first_package_frame_and_class_the_test_frame() -> None:
    stdout = _test_block(
        "    java.lang.IllegalStateException: boom",
        "app//org.junit.Assert.fail(Assert.java:1)",
        "app//com.example.sandbox.UserRepository$DefaultImpls.load(UserRepository.kt:12)",
        "app//com.example.sandbox.FooTest$does something$1.invokeSuspend(FooTest.kt:7)",
    )
    index = {"UserRepository.kt": "core/UserRepository.kt", "FooTest.kt": "core/FooTest.kt"}

    result = _parse(_outcome(stdout=stdout), source_index=index)

    assert isinstance(error := result.errors[0], FailedTestError)
    assert error.test_class == "com.example.sandbox.FooTest"
    assert error.location == SourceLocation(file="core/UserRepository.kt", line=12)


def test_short_exception_format_gives_the_location() -> None:
    stdout = _test_block("    org.opentest4j.AssertionFailedError at FooTest.kt:7")

    result = _parse(_outcome(stdout=stdout), source_index={"FooTest.kt": "core/FooTest.kt"})

    assert isinstance(error := result.errors[0], FailedTestError)
    assert error.exception_type == "org.opentest4j.AssertionFailedError"
    assert error.location == SourceLocation(file="core/FooTest.kt", line=7)


def test_junit4_expected_and_actual() -> None:
    stdout = _test_block("    java.lang.AssertionError: expected:<1> but was:<2>")

    result = _parse(_outcome(stdout=stdout))

    assert isinstance(error := result.errors[0], FailedTestError)
    assert (error.expected, error.actual) == ("1", "2")


def test_long_test_failure_values_are_truncated() -> None:
    stdout = _test_block(f"    java.lang.AssertionError: expected: <{'a' * 900}> but was: <b>")

    result = _parse(_outcome(stdout=stdout))

    assert isinstance(error := result.errors[0], FailedTestError)
    assert len(error.message) == 500
    assert error.expected is not None
    assert len(error.expected) == 200
    assert error.actual == "b"


def test_test_failures_are_capped_and_one_is_primary() -> None:
    blocks = [
        f"T{index}Test > case{index}() FAILED\n    java.lang.AssertionError: no {index}\n"
        for index in range(8)
    ]

    result = _parse(_outcome(stdout="".join(blocks)))

    assert result.failure_kind is FailureKind.TEST_FAILURE
    failures = [error for error in result.errors if isinstance(error, FailedTestError)]
    assert [failure.test_class for failure in failures] == [f"T{i}Test" for i in range(5)]
    assert [failure.is_primary for failure in failures] == [True, False, False, False, False]


def test_header_without_exception_line_is_skipped() -> None:
    stdout = "> Task :core:test FAILED\nFooTest > case() FAILED\n2 tests completed, 1 failed\n"

    result = _parse(_outcome(stdout=stdout))

    assert result.failure_kind is FailureKind.TEST_FAILURE
    assert result.errors == []


# --- Arguments and robustness ---
@pytest.mark.parametrize(
    ("task", "target_package"),
    [("core:test", _PACKAGE), (":core:test", ""), (":core:test", "com..example")],
)
def test_invalid_arguments_raise(task: str, target_package: str) -> None:
    with pytest.raises(ValueError, match="must be"):
        parse_build(
            _outcome(), task=task, project_root=_PROJECT_ROOT, target_package=target_package
        )


_GARBAGE_FRAGMENTS = [
    "",
    "\n",
    "e: ",
    "e: file:///x:0:0 zero",
    "e: file:///:1:1  ",
    f"e: file://{_PROJECT_ROOT}/:1:1 empty path",
    f"e: file://{_PROJECT_ROOT}/{'d/' * 200}F.kt:1:1 long path",
    "e: file://<PROJECT_ROOT>//abs.kt:1:1 double slash",
    "> Task :: FAILED",
    "> Task : FAILED",
    f"> Task :{'a' * 400} FAILED",
    "Execution failed for task ':'",
    " > x FAILED",
    "X > FAILED",
    "X >  FAILED",
    "FooTest > m() FAILED",
    "    ",
    "    a.b: ",
    f"    {'a.' * 300}B: message",
    "    java.lang.Error at :0",
    "    java.lang.Error at F.kt:0",
    "        at com.example.sandbox.(F.kt:1)",
    "        at com.example.sandbox.A.b(F.kt:0)",
    "        at app//com.example.sandbox.A$.b(:1)",
    "expected: <> but was: <",
    "* What went wrong:",
    "* What went wrong:\n",
    "* What went wrong:\n> ",
    "\x00�​",
    "e: file:///x:99999999999999999999:1 huge line",
]


@pytest.mark.parametrize("seed", range(50))
@pytest.mark.parametrize("exit_code", [1, None, 0])
def test_garbage_input_never_raises(seed: int, exit_code: int | None) -> None:
    rng = random.Random(seed)
    stdout = "\n".join(rng.choices(_GARBAGE_FRAGMENTS, k=rng.randint(0, 40)))
    stderr = "\n".join(rng.choices(_GARBAGE_FRAGMENTS, k=rng.randint(0, 40)))

    result = _parse(
        _outcome(exit_code=exit_code, stdout=stdout, stderr=stderr),
        source_index={"F.kt": "core/F.kt", "": "x"},
    )

    assert result.success == (exit_code == 0)
