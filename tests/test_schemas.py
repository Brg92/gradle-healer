from typing import Any

import pytest
from pydantic import ValidationError

from gradle_healer.core import schemas
from gradle_healer.core.schemas import (
    BuildResult,
    CompilationError,
    FailureKind,
    SourceLocation,
)

# TestFailureError is accessed through the module: importing the name directly
# would make pytest try to collect it as a test class.

BROKEN_TEST = "core/src/test/kotlin/com/example/sandbox/BrokenTest.kt"
FAILING_TEST = "core/src/test/kotlin/com/example/sandbox/GetUserUseCaseFailingTest.kt"


def _base(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "task": ":core:test",
        "exit_code": 1,
        "timed_out": False,
        "duration_seconds": 1.5,
        "stdout": "",
        "stderr": "",
    }
    data.update(overrides)
    return data


def _compilation(
    line: int = 11, column: int | None = 26, *, is_primary: bool = True
) -> dict[str, Any]:
    return {
        "kind": "compilation",
        "location": {"file": BROKEN_TEST, "line": line, "column": column},
        "message": "Unresolved reference 'UserService'.",
        "is_primary": is_primary,
    }


def _test_failure(*, is_primary: bool = True) -> dict[str, Any]:
    return {
        "kind": "test_failure",
        "test_class": "com.example.sandbox.GetUserUseCaseFailingTest",
        "test_method": "invoke returns the user name",
        "exception_type": "org.opentest4j.AssertionFailedError",
        "message": "expected: <Grace> but was: <Ada>",
        "expected": "Grace",
        "actual": "Ada",
        "location": {"file": FAILING_TEST, "line": 20},
        "is_primary": is_primary,
    }


def _success() -> BuildResult:
    return BuildResult.model_validate(_base(exit_code=0))


def _compile_error() -> BuildResult:
    return BuildResult.model_validate(
        _base(
            failed_task=":core:compileTestKotlin",
            failure_kind="compile_error",
            errors=[_compilation(11, 26), _compilation(18, 36, is_primary=False)],
        )
    )


def _assertion_failure() -> BuildResult:
    return BuildResult.model_validate(
        _base(failed_task=":core:test", failure_kind="test_failure", errors=[_test_failure()])
    )


def _timeout() -> BuildResult:
    return BuildResult.model_validate(
        _base(exit_code=None, timed_out=True, failure_kind="timeout", summary="Killed after 600 s.")
    )


def _infrastructure() -> BuildResult:
    return BuildResult.model_validate(
        _base(exit_code=None, failure_kind="infrastructure", summary="./gradlew not found.")
    )


# ---------------------------------------------------------------------------
# Valid results
# ---------------------------------------------------------------------------


def test_success() -> None:
    result = _success()

    assert result.success is True
    assert result.failure_kind is None
    assert result.errors == []


def test_compile_error_scenario() -> None:
    result = _compile_error()

    assert result.success is False
    assert result.failure_kind is FailureKind.COMPILE_ERROR
    assert all(isinstance(error, CompilationError) for error in result.errors)
    primary = result.errors[0]
    assert primary.is_primary
    assert isinstance(primary, CompilationError)
    assert primary.location == SourceLocation(file=BROKEN_TEST, line=11, column=26)


def test_assertion_failure_scenario() -> None:
    result = _assertion_failure()

    assert result.failure_kind is FailureKind.TEST_FAILURE
    error = result.errors[0]
    assert isinstance(error, schemas.TestFailureError)
    assert (error.expected, error.actual) == ("Grace", "Ada")
    assert error.location is not None
    assert error.location.column is None


@pytest.mark.parametrize(
    ("factory", "kind"),
    [(_timeout, FailureKind.TIMEOUT), (_infrastructure, FailureKind.INFRASTRUCTURE)],
    ids=["timeout", "infrastructure"],
)
def test_failure_without_exit_code(factory: Any, kind: FailureKind) -> None:
    result: BuildResult = factory()

    assert result.success is False
    assert result.exit_code is None
    assert result.failure_kind is kind
    assert result.errors == []


@pytest.mark.parametrize(
    "factory",
    [_success, _compile_error, _assertion_failure, _timeout, _infrastructure],
    ids=["success", "compile_error", "assertion_failure", "timeout", "infrastructure"],
)
def test_json_round_trip(factory: Any) -> None:
    result: BuildResult = factory()

    restored = BuildResult.model_validate_json(result.model_dump_json())

    assert restored == result
    assert [type(error) for error in restored.errors] == [type(error) for error in result.errors]


def test_success_is_not_serialized() -> None:
    assert "success" not in _success().model_dump()


# ---------------------------------------------------------------------------
# Field rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/home/user/core/Foo.kt", "file:///core/Foo.kt"])
def test_source_location_rejects_non_relative_path(path: str) -> None:
    with pytest.raises(ValidationError, match="relative to the project root"):
        SourceLocation(file=path, line=1)


def test_compilation_error_requires_column() -> None:
    with pytest.raises(ValidationError, match="column must not be None"):
        CompilationError.model_validate(_compilation(column=None))


@pytest.mark.parametrize("field", ["task", "failed_task"])
def test_task_paths_must_start_with_colon(field: str) -> None:
    with pytest.raises(ValidationError, match="String should match pattern"):
        BuildResult.model_validate(
            _base(failure_kind="test_failure", errors=[_test_failure()], **{field: "core:test"})
        )


def test_unknown_discriminator_is_rejected() -> None:
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        BuildResult.model_validate(
            _base(failure_kind="compile_error", errors=[{**_compilation(), "kind": "lint"}])
        )


def test_at_most_ten_errors() -> None:
    errors = [_compilation(line, is_primary=line == 1) for line in range(1, 12)]

    with pytest.raises(ValidationError, match="at most 10 items"):
        BuildResult.model_validate(_base(failure_kind="compile_error", errors=errors))


def test_extra_fields_are_forbidden() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        BuildResult.model_validate(_base(exit_code=0, retries=1))


def test_models_are_frozen() -> None:
    result = _success()

    with pytest.raises(ValidationError, match="Instance is frozen"):
        result.exit_code = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# BuildResult invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        pytest.param(
            {"exit_code": 0, "failure_kind": "compile_error"},
            "successful build must have failure_kind None",
            id="1-success-with-failure-kind",
        ),
        pytest.param({}, "failed build must have a failure_kind", id="1-failure-without-kind"),
        pytest.param(
            {"exit_code": 0, "failed_task": ":core:test"},
            "successful build must have failed_task None",
            id="2-success-with-failed-task",
        ),
        pytest.param(
            {"exit_code": 143, "timed_out": True, "failure_kind": "timeout"},
            "timed out build must have exit_code None",
            id="3-timeout-with-exit-code",
        ),
        pytest.param(
            {"exit_code": None, "timed_out": True, "failure_kind": "infrastructure"},
            "timed out build must have failure_kind 'timeout'",
            id="3-timeout-wrong-kind",
        ),
        pytest.param(
            {"exit_code": None, "failure_kind": "compile_error"},
            "build without exit_code must have failure_kind",
            id="4-no-exit-code-compile-error",
        ),
        pytest.param(
            {"failure_kind": "compile_error", "errors": [_test_failure()]},
            "allows only CompilationError",
            id="5-compile-error-with-test-failure",
        ),
        pytest.param(
            {"failure_kind": "test_failure", "errors": [_compilation()]},
            "allows only TestFailureError",
            id="5-test-failure-with-compilation",
        ),
        pytest.param(
            {
                "exit_code": None,
                "timed_out": True,
                "failure_kind": "timeout",
                "errors": [_compilation()],
            },
            "'timeout' must have no errors",
            id="5-timeout-with-errors",
        ),
        pytest.param(
            {"exit_code": None, "failure_kind": "infrastructure", "errors": [_compilation()]},
            "'infrastructure' must have no errors",
            id="5-infrastructure-with-errors",
        ),
        pytest.param(
            {"failure_kind": "compile_error", "errors": [_compilation(11), _compilation(18)]},
            "exactly one error must be primary, got 2",
            id="6-two-primaries",
        ),
        pytest.param(
            {"failure_kind": "compile_error", "errors": [_compilation(is_primary=False)]},
            "exactly one error must be primary, got 0",
            id="6-no-primary",
        ),
    ],
)
def test_invariant_violations(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        BuildResult.model_validate(_base(**overrides))


def test_compile_error_without_extracted_errors_is_valid() -> None:
    result = BuildResult.model_validate(_base(failure_kind="compile_error"))

    assert result.errors == []
