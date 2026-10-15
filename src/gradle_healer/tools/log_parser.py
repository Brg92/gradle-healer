"""Interpretation of a raw Gradle run into a BuildResult.

Pure functions only: no I/O, no subprocesses. A RunOutcome is classified
(launch error, timeout, success, compile error, test failure, infrastructure)
and its salient errors are extracted with regexes. Raw logs are never
forwarded anywhere: errors and summary carry only short, truncated excerpts,
and stdout / stderr are copied as-is only into the BuildResult fields
reserved for debugging.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from gradle_healer.core.schemas import (
    GRADLE_TASK_PATH_PATTERN,
    BuildResult,
    CompilationError,
    FailedTestError,
    FailureKind,
    SourceLocation,
)
from gradle_healer.tools.gradle_runner import RunOutcome

MAX_ERRORS = 5
_SUMMARY_MAX_CHARS = 500
_MESSAGE_MAX_CHARS = 500
_VALUE_MAX_CHARS = 200
_NAME_MAX_CHARS = 300
_ELLIPSIS = "…"

_TASK_PATH = re.compile(GRADLE_TASK_PATH_PATTERN)
_PACKAGE_NAME = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")

# --- Log patterns ---
# Each example line comes from the recorded fixtures in tests/fixtures/logs/;
# a trailing '\' marks a line wrapped here that is a single line in the log.

# e: file://<PROJECT_ROOT>/core/src/test/kotlin/com/example/sandbox/BrokenTest.kt:11:26 \
#   Unresolved reference 'UserService'.
# Warnings start with "w: " and are ignored.
_COMPILER_ERROR = re.compile(
    r"^e: (?:file://)?(?P<path>[^:]+):(?P<line>\d+):(?P<column>\d+) (?P<message>.+)$",
    re.MULTILINE,
)

# > Task :core:compileTestKotlin FAILED
_FAILED_TASK = re.compile(r"^> Task (?P<task>:\S+) FAILED$", re.MULTILINE)

# Execution failed for task ':core:compileTestKotlin' \
#   (registered by plugin 'org.jetbrains.kotlin.jvm').
_EXECUTION_FAILED = re.compile(r"Execution failed for task '(?P<task>:[^']+)'")

# GetUserUseCaseFailingTest > invoke returns the user name() FAILED
_TEST_HEADER = re.compile(r"^(?P<test_class>\w+) > (?P<test_method>.+) FAILED$")

# exceptionFormat FULL (the sandbox):
#     org.opentest4j.AssertionFailedError: expected: <Grace> but was: <Ada>
# exceptionFormat SHORT (Gradle's default; not in the fixtures):
#     org.opentest4j.AssertionFailedError at GetUserUseCaseFailingTest.kt:20
_EXCEPTION = re.compile(
    r"^\s+(?P<type>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+)"
    r"(?:: (?P<message>.*)| at (?P<file>[^\s:()]+\.(?:kt|java)):(?P<line>\d+))?$"
)

# expected: <Grace> but was: <Ada>  (JUnit 5; JUnit 4 omits the spaces before '<')
_EXPECTED_ACTUAL = re.compile(r"expected: ?<(?P<expected>.*?)> but was: ?<(?P<actual>.*?)>")

# * What went wrong:
# Execution failed for task ':core:test'.
# > There were failing tests. See the report at: file://<PROJECT_ROOT>/core/build/reports/tests/test/index.html
_WHAT_WENT_WRONG = re.compile(
    r"^\* What went wrong:\n(?P<what>\S.*)(?:\n> (?P<cause>.+))?", re.MULTILINE
)

# Last segment of a task path: compileKotlin, compileTestKotlin, compileDebugJavaWithJavac, ...
_COMPILE_TASK_NAME = re.compile(r"^compile\w*(?:Kotlin|Java)")
# test (JVM) or testDebugUnitTest (Android).
_TEST_TASK_NAME = re.compile(r"^test(?:\w*UnitTest)?$")


#     at app//com.example.sandbox.GetUserUseCaseFailingTest$invoke returns the user name$1 \
#   .invokeSuspend(GetUserUseCaseFailingTest.kt:20)
# Optional class-loader prefix ("app//"), the class up to the first '$' or
# '.<method>', then "(File.kt:line)". Frames without a line number do not match.
def _frame_pattern(target_package: str) -> re.Pattern[str]:
    return re.compile(
        r"^\s+at (?:\S*/)?"
        rf"(?P<class_name>{re.escape(target_package)}\.[\w.]*?\w+)"
        r"(?:\$[^(]*)?\.[^.(]+"
        r"\((?P<file>[^\s:()]+\.(?:kt|java)):(?P<line>\d+)\)$"
    )


# --- Helpers ---
def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - len(_ELLIPSIS)] + _ELLIPSIS


def _valid_task(task: str) -> str | None:
    return task if _TASK_PATH.fullmatch(task) and len(task) <= _NAME_MAX_CHARS else None


def _find_failed_task(outcome: RunOutcome) -> str | None:
    for pattern, text in (
        (_FAILED_TASK, outcome.stdout),
        (_FAILED_TASK, outcome.stderr),
        (_EXECUTION_FAILED, outcome.stderr),
        (_EXECUTION_FAILED, outcome.stdout),
    ):
        for match in pattern.finditer(text):
            if (task := _valid_task(match["task"])) is not None:
                return task
    return None


def _task_name(task: str | None) -> str:
    return task.rsplit(":", 1)[-1] if task else ""


def _what_went_wrong(outcome: RunOutcome, project_root: str) -> str | None:
    for text in (outcome.stderr, outcome.stdout):
        if match := _WHAT_WENT_WRONG.search(text):
            parts = [match["what"].strip()]
            if match["cause"]:
                parts.append(match["cause"].strip())
            # Keeps the user's absolute paths out of the summary.
            return " ".join(parts).replace(project_root + "/", "")
    return None


def _relative_path(path: str, project_root: str) -> str | None:
    prefix = project_root.rstrip("/") + "/"
    if not path.startswith(prefix):
        return None
    relative = path[len(prefix) :]
    if not relative or len(relative) > _NAME_MAX_CHARS or relative.startswith(("/", "file:")):
        return None
    return relative


# --- Compiler errors ---
@dataclass(frozen=True, slots=True)
class _CompilerErrors:
    errors: list[CompilationError]
    found: int
    outside_root: int


def _extract_compiler_errors(outcome: RunOutcome, project_root: str) -> _CompilerErrors:
    seen: set[tuple[str, int, int, str]] = set()
    unique: list[tuple[str, int, int, str]] = []
    for text in (outcome.stderr, outcome.stdout):
        for match in _COMPILER_ERROR.finditer(text):
            key = (
                match["path"],
                int(match["line"]),
                int(match["column"]),
                match["message"].strip(),
            )
            if key not in seen:
                seen.add(key)
                unique.append(key)

    errors: list[CompilationError] = []
    outside_root = 0
    for path, line, column, message in unique:
        relative = _relative_path(path, project_root)
        if relative is None:
            outside_root += 1
            continue
        if line < 1 or column < 1 or not message or len(errors) == MAX_ERRORS:
            continue
        errors.append(
            CompilationError(
                location=SourceLocation(file=relative, line=line, column=column),
                message=_truncate(message, _MESSAGE_MAX_CHARS),
                is_primary=not errors,
            )
        )
    return _CompilerErrors(errors=errors, found=len(unique), outside_root=outside_root)


# --- Test failures ---
def _indented_block(lines: list[str], start: int) -> list[str]:
    block: list[str] = []
    for line in lines[start:]:
        if not line[:1].isspace():
            break
        block.append(line)
    return block


def _test_failure(
    header: re.Match[str],
    block: list[str],
    *,
    frame: re.Pattern[str],
    source_index: Mapping[str, str],
    is_primary: bool,
) -> FailedTestError | None:
    exception = _EXCEPTION.match(block[0]) if block else None
    if exception is None or len(exception["type"]) > _NAME_MAX_CHARS:
        return None

    message = exception["message"] or ""
    expected: str | None = None
    actual: str | None = None
    if values := _EXPECTED_ACTUAL.search(message):
        expected = _truncate(values["expected"], _VALUE_MAX_CHARS)
        actual = _truncate(values["actual"], _VALUE_MAX_CHARS)

    simple_name = header["test_class"]
    test_class = simple_name
    file_name: str | None = exception["file"]
    line = int(exception["line"]) if exception["line"] else None
    for frame_line in block[1:]:
        if match := frame.match(frame_line):
            if file_name is None:
                file_name, line = match["file"], int(match["line"])
            # The first frame in the package may be production code: the test
            # class is the first frame whose simple name matches the header.
            if match["class_name"].rsplit(".", 1)[-1] == simple_name:
                test_class = match["class_name"]
                break

    location: SourceLocation | None = None
    if file_name is not None and line is not None and line >= 1:
        relative = source_index.get(file_name)
        if relative is not None and 0 < len(relative) <= _NAME_MAX_CHARS:
            location = SourceLocation(file=relative, line=line)

    return FailedTestError(
        test_class=_truncate(test_class, _NAME_MAX_CHARS),
        test_method=_truncate(header["test_method"], _NAME_MAX_CHARS),
        exception_type=exception["type"],
        message=_truncate(message, _MESSAGE_MAX_CHARS),
        expected=expected,
        actual=actual,
        location=location,
        is_primary=is_primary,
    )


def _extract_test_failures(
    stdout: str, target_package: str, source_index: Mapping[str, str]
) -> list[FailedTestError]:
    frame = _frame_pattern(target_package)
    lines = stdout.splitlines()
    failures: list[FailedTestError] = []
    for index, text in enumerate(lines):
        if len(failures) == MAX_ERRORS:
            break
        if header := _TEST_HEADER.match(text):
            failure = _test_failure(
                header,
                _indented_block(lines, index + 1),
                frame=frame,
                source_index=source_index,
                is_primary=not failures,
            )
            if failure is not None:
                failures.append(failure)
    return failures


# --- parse_build ---
def validate_target_package(target_package: str) -> None:
    """Raise ValueError unless target_package is a dotted JVM package name."""
    if not _PACKAGE_NAME.fullmatch(target_package):
        raise ValueError(
            f"target_package must be a package name like 'com.example', got {target_package!r}"
        )


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


# exit_code and timed_out are derived from the outcome, so that every branch
# of parse_build stays consistent with the BuildResult invariants.
def _build_result(
    outcome: RunOutcome,
    task: str,
    failure_kind: FailureKind | None,
    *,
    failed_task: str | None = None,
    errors: Sequence[CompilationError | FailedTestError] = (),
    summary: str | None = None,
) -> BuildResult:
    launched = outcome.launch_error is None
    return BuildResult(
        task=task,
        exit_code=outcome.exit_code if launched and not outcome.timed_out else None,
        timed_out=launched and outcome.timed_out,
        duration_seconds=max(outcome.duration_seconds, 0.0),
        failed_task=failed_task,
        failure_kind=failure_kind,
        errors=list(errors),
        summary=_truncate(summary, _SUMMARY_MAX_CHARS) if summary else None,
        stdout=outcome.stdout,
        stderr=outcome.stderr,
    )


def parse_build(
    outcome: RunOutcome,
    *,
    task: str,
    project_root: str,
    target_package: str,
    source_index: Mapping[str, str] | None = None,
) -> BuildResult:
    """Classify a Gradle run and extract its salient errors.

    Classification order: launch error, timeout, success, compile error,
    test failure, infrastructure. Unexpected log content never raises: it
    falls back to infrastructure with a summary. Only invalid arguments
    (task path, target_package) raise ValueError.

    project_root is the absolute project directory, used to make compiler
    paths relative; source_index maps a source file name to its path
    relative to project_root and is used to locate test failures.
    """
    if _valid_task(task) is None:
        raise ValueError(f"task must be a Gradle task path like ':core:test', got {task!r}")
    validate_target_package(target_package)

    # a) The process never started.
    if outcome.launch_error is not None:
        return _build_result(
            outcome, task, FailureKind.INFRASTRUCTURE, summary=outcome.launch_error
        )

    # b) Killed for exceeding its timeout.
    if outcome.timed_out:
        return _build_result(
            outcome,
            task,
            FailureKind.TIMEOUT,
            summary=f"Timed out after {outcome.duration_seconds:.0f} s",
        )

    # c) Success.
    if outcome.exit_code == 0:
        return _build_result(outcome, task, None)

    # d) Failure: compiler errors, then test failures, then the failed task's
    # kind, then infrastructure.
    what_went_wrong = _what_went_wrong(outcome, project_root)
    if outcome.exit_code is None:
        return _build_result(
            outcome,
            task,
            FailureKind.INFRASTRUCTURE,
            summary=what_went_wrong or "Gradle exited without an exit code",
        )

    failed_task = _find_failed_task(outcome)
    compiler = _extract_compiler_errors(outcome, project_root)
    if compiler.found:
        summary = None
        if compiler.outside_root:
            summary = f"{_plural(compiler.outside_root, 'compiler error')} outside the project root"
        return _build_result(
            outcome,
            task,
            FailureKind.COMPILE_ERROR,
            failed_task=failed_task,
            errors=compiler.errors,
            summary=summary,
        )

    failures = _extract_test_failures(outcome.stdout, target_package, source_index or {})
    if failures:
        return _build_result(
            outcome, task, FailureKind.TEST_FAILURE, failed_task=failed_task, errors=failures
        )

    task_name = _task_name(failed_task)
    if _COMPILE_TASK_NAME.match(task_name):
        return _build_result(
            outcome,
            task,
            FailureKind.COMPILE_ERROR,
            failed_task=failed_task,
            summary=what_went_wrong,
        )
    if _TEST_TASK_NAME.match(task_name):
        return _build_result(
            outcome,
            task,
            FailureKind.TEST_FAILURE,
            failed_task=failed_task,
            summary=what_went_wrong,
        )

    return _build_result(
        outcome,
        task,
        FailureKind.INFRASTRUCTURE,
        failed_task=failed_task,
        summary=what_went_wrong or f"Gradle exited with code {outcome.exit_code}",
    )
