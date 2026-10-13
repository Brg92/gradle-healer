"""Data contracts for GradleHealer.

Build results are produced by deterministic tools and are the only build
information passed to nodes.
"""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --- Common rules ---
# * Every model is immutable and forbids extra fields.
# * Every model and field has a description (it ends up in the JSON Schema read by the LLM).
# * Enums are StrEnum with lowercase values.
# * Free-text strings have a max_length (token budget).


# --- FailureKind ---
# Infrastructure covers: process not started, missing Gradle wrapper,
# daemon crash, unparseable output.
class FailureKind(StrEnum):
    """Category of a failed Gradle build: compile error, test failure, timeout or infrastructure."""

    COMPILE_ERROR = "compile_error"
    TEST_FAILURE = "test_failure"
    TIMEOUT = "timeout"
    INFRASTRUCTURE = "infrastructure"


# --- SourceLocation ---
class SourceLocation(BaseModel):
    """Position in a Kotlin/Java source file, relative to the Gradle project root."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str = Field(
        min_length=1,
        max_length=300,
        description="Source file path relative to the Gradle project root.",
    )
    line: int = Field(ge=1, description="1-based line number in the file.")
    column: int | None = Field(
        default=None,
        ge=1,
        description="1-based column number; absent for stack trace frames.",
    )

    @field_validator("file")
    @classmethod
    def reject_absolute_path(cls, value: str) -> str:
        if value.startswith(("/", "file:")):
            raise ValueError(f"file must be relative to the project root, got {value!r}")
        return value


# --- CompilationError ---
class CompilationError(BaseModel):
    """Kotlin compiler error reported at an exact file, line and column."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["compilation"] = Field(
        default="compilation",
        description="Discriminator: always 'compilation' for Kotlin compiler errors.",
    )
    location: SourceLocation = Field(
        description="Where the compiler reported the error; column is always present.",
    )
    message: str = Field(
        min_length=1,
        max_length=500,
        description="Compiler error message, without the 'e: file:///...' prefix.",
    )
    is_primary: bool = Field(
        description="True for the root-cause error to fix first; follow-up errors are False.",
    )

    @model_validator(mode="after")
    def require_column(self) -> Self:
        if self.location.column is None:
            raise ValueError("CompilationError.location.column must not be None")
        return self


# --- FailedTestError ---
class FailedTestError(BaseModel):
    """Unit test that failed with an exception or an assertion mismatch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["test_failure"] = Field(
        default="test_failure",
        description="Discriminator: always 'test_failure' for failed unit tests.",
    )
    test_class: str = Field(
        min_length=1,
        max_length=300,
        description="Fully qualified name of the failing test class.",
    )
    test_method: str = Field(
        min_length=1,
        max_length=300,
        description="Name of the failing test method, as reported by the test runner.",
    )
    exception_type: str = Field(
        min_length=1,
        max_length=300,
        description="Fully qualified name of the exception thrown by the test.",
    )
    message: str = Field(
        max_length=500,
        description="Exception message; may be empty.",
    )
    expected: str | None = Field(
        default=None,
        max_length=200,
        description="Expected value of a failed assertion; set together with actual, "
        "absent for other exceptions.",
    )
    actual: str | None = Field(
        default=None,
        max_length=200,
        description="Actual value of a failed assertion; set together with expected, "
        "absent for other exceptions.",
    )
    location: SourceLocation | None = Field(
        default=None,
        description="First stack trace frame inside the target package, if any.",
    )
    is_primary: bool = Field(
        description="True for the failure to fix first; other failures are False.",
    )

    @model_validator(mode="after")
    def require_expected_and_actual_together(self) -> Self:
        if (self.expected is None) != (self.actual is None):
            raise ValueError("expected and actual must be both set or both None")
        return self


# --- BuildError ---
BuildError = Annotated[CompilationError | FailedTestError, Field(discriminator="kind")]


# --- BuildResult ---
# stdout / stderr are kept for logging and debugging only; the LLM-facing view
# without them is defined in Sprint 2.
class BuildResult(BaseModel):
    """Outcome of one Gradle task invocation, with the salient errors extracted from its output."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task: str = Field(
        max_length=300,
        pattern=r"^(:[A-Za-z0-9_.-]+)+$",
        description="Gradle task path that was requested, e.g. ':core:test': one or more "
        "':'-prefixed segments of letters, digits, '_', '-' or '.', no empty segments, "
        "no whitespace.",
    )
    exit_code: int | None = Field(
        description="Gradle process exit code; None if the process was killed or never started.",
    )
    timed_out: bool = Field(
        description="True if the build was killed for exceeding its timeout.",
    )
    duration_seconds: float = Field(
        ge=0,
        description="Wall-clock duration of the Gradle invocation, in seconds.",
    )
    failed_task: str | None = Field(
        default=None,
        max_length=300,
        pattern=r"^(:[A-Za-z0-9_.-]+)+$",
        description="Gradle task that failed, e.g. ':core:compileTestKotlin': one or more "
        "':'-prefixed segments of letters, digits, '_', '-' or '.', no empty segments, "
        "no whitespace.",
    )
    failure_kind: FailureKind | None = Field(
        description="Category of the failure; None only when the build succeeded.",
    )
    errors: list[BuildError] = Field(
        default_factory=list,
        max_length=10,
        description="Salient errors extracted from the build output; exactly one is primary.",
    )
    summary: str | None = Field(
        default=None,
        max_length=500,
        description="Short human-readable detail, mainly for timeout and infrastructure failures.",
    )
    stdout: str = Field(
        description="Sanitized Gradle stdout (no ANSI codes). Never sent to the LLM.",
    )
    stderr: str = Field(
        description="Sanitized Gradle stderr (no ANSI codes). Never sent to the LLM.",
    )

    # --- BuildResult.success ---
    # Plain @property, NOT @computed_field: a computed field would be serialized
    # and break the JSON round-trip with extra="forbid".
    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    # --- BuildResult invariants ---
    @model_validator(mode="after")
    def check_invariants(self) -> Self:
        # 1. success <=> failure_kind is None.
        if self.success and self.failure_kind is not None:
            raise ValueError(
                f"successful build must have failure_kind None, got {self.failure_kind.value!r}"
            )
        if not self.success and self.failure_kind is None:
            raise ValueError("failed build must have a failure_kind")

        # 2. success => errors empty and failed_task is None.
        if self.success and self.errors:
            raise ValueError("successful build must have no errors")
        if self.success and self.failed_task is not None:
            raise ValueError("successful build must have failed_task None")

        # 3. timed_out => exit_code is None and failure_kind == timeout.
        if self.timed_out and self.exit_code is not None:
            raise ValueError("timed out build must have exit_code None")
        if self.timed_out and self.failure_kind is not FailureKind.TIMEOUT:
            raise ValueError("timed out build must have failure_kind 'timeout'")

        # 4. exit_code is None => failure_kind is timeout or infrastructure.
        if self.exit_code is None and self.failure_kind not in (
            FailureKind.TIMEOUT,
            FailureKind.INFRASTRUCTURE,
        ):
            raise ValueError(
                "build without exit_code must have failure_kind 'timeout' or 'infrastructure'"
            )

        # 5. Error types must match failure_kind; timeout / infrastructure have no errors.
        if self.failure_kind is FailureKind.COMPILE_ERROR and not all(
            isinstance(error, CompilationError) for error in self.errors
        ):
            raise ValueError("failure_kind 'compile_error' allows only CompilationError errors")
        if self.failure_kind is FailureKind.TEST_FAILURE and not all(
            isinstance(error, FailedTestError) for error in self.errors
        ):
            raise ValueError("failure_kind 'test_failure' allows only FailedTestError errors")
        if self.failure_kind in (FailureKind.TIMEOUT, FailureKind.INFRASTRUCTURE) and self.errors:
            raise ValueError(f"failure_kind {self.failure_kind.value!r} must have no errors")

        # 6. If errors is non-empty, exactly one error has is_primary=True.
        primary_count = sum(1 for error in self.errors if error.is_primary)
        if self.errors and primary_count != 1:
            raise ValueError(f"exactly one error must be primary, got {primary_count}")

        return self


# --- Out of scope for schemas ---
# * Capping compilation errors to 5 and normalizing paths is the log_parser's
#   job; the schema only enforces the upper bound and format.
# * Any `match` over BuildError must end with `case _: assert_never(...)`.
