"""Data contracts for GradleHealer.

Build results are produced by deterministic tools and are the only build
information passed to nodes.
"""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Common rules
# ---------------------------------------------------------------------------
# * Every model is immutable and forbids extra fields.
# * Every field has a description (it ends up in the JSON Schema read by the LLM).
# * Enums are StrEnum with lowercase values.
# * Free-text strings have a max_length (token budget).


# ---------------------------------------------------------------------------
# TODO 1 - FailureKind (StrEnum)
# ---------------------------------------------------------------------------
# Members (lowercase values):
#   - compile_error
#   - test_failure
#   - timeout
#   - infrastructure
# Infrastructure covers: process not started, missing Gradle wrapper,
# daemon crash, unparseable output.
class FailureKind(StrEnum):
    COMPILE_ERROR = "compile_error"
    TEST_FAILURE = "test_failure"
    TIMEOUT = "timeout"
    INFRASTRUCTURE = "infrastructure"


# ---------------------------------------------------------------------------
# TODO 2 - SourceLocation
# ---------------------------------------------------------------------------
# Fields:
#   - file: non-empty path RELATIVE to the Gradle project root.
#       It must not start with "/" or "file:" (enforce with a field validator).
#   - line: int >= 1.
#   - column: optional int >= 1 (absent for stack trace frames).
class SourceLocation(BaseModel):
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


# ---------------------------------------------------------------------------
# TODO 3 - CompilationError
# ---------------------------------------------------------------------------
# Fields:
#   - kind: literal "compilation", default "compilation" (discriminator).
#   - location: SourceLocation.
#       Invariant: location.column must not be None.
#   - message: 1-500 chars.
#   - is_primary: bool.
class CompilationError(BaseModel):
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


# ---------------------------------------------------------------------------
# TODO 4 - TestFailureError
# ---------------------------------------------------------------------------
# Fields:
#   - kind: literal "test_failure", default "test_failure" (discriminator).
#   - test_class: fully qualified name, non-empty.
#   - test_method: non-empty.
#   - exception_type: fully qualified name, non-empty.
#   - message: 0-500 chars.
#   - expected / actual: optional, max 200 chars (assertions only).
#   - location: optional SourceLocation (first frame in the target package).
#   - is_primary: bool.
class TestFailureError(BaseModel):
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
        description="Expected value of a failed assertion; absent for other exceptions.",
    )
    actual: str | None = Field(
        default=None,
        max_length=200,
        description="Actual value of a failed assertion; absent for other exceptions.",
    )
    location: SourceLocation | None = Field(
        default=None,
        description="First stack trace frame inside the target package, if any.",
    )
    is_primary: bool = Field(
        description="True for the failure to fix first; other failures are False.",
    )


# ---------------------------------------------------------------------------
# TODO 5 - BuildError
# ---------------------------------------------------------------------------
# Discriminated union of CompilationError and TestFailureError on the `kind`
# field (use Annotated + Field discriminator).
BuildError = Annotated[CompilationError | TestFailureError, Field(discriminator="kind")]


# ---------------------------------------------------------------------------
# TODO 6 - BuildResult fields
# ---------------------------------------------------------------------------
# Fields:
#   - task: requested task path, must start with ":" (e.g. ":core:test").
#   - exit_code: optional int (None if the process was killed or never started).
#   - timed_out: bool.
#   - duration_seconds: float >= 0.
#   - failed_task: optional str (e.g. ":core:compileTestKotlin").
#   - failure_kind: optional FailureKind (None only on success).
#   - errors: list of BuildError, default empty list, max 10 items.
#   - summary: optional, max 500 chars (detail for timeout/infrastructure).
#   - stdout / stderr: sanitized (no ANSI codes); NEVER sent to the LLM.
class BuildResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task: str = Field(
        min_length=2,
        max_length=300,
        pattern=r"^:",
        description="Gradle task path that was requested, e.g. ':core:test'.",
    )
    exit_code: int | None = Field(
        default=None,
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
        min_length=2,
        max_length=300,
        pattern=r"^:",
        description="Gradle task that failed, e.g. ':core:compileTestKotlin'.",
    )
    failure_kind: FailureKind | None = Field(
        default=None,
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

    # -----------------------------------------------------------------------
    # TODO 7 - BuildResult.success
    # -----------------------------------------------------------------------
    # Derived read-only property: exit_code == 0 and not timed_out.
    # Use a plain @property, NOT @computed_field (it would break the JSON
    # round-trip with extra="forbid").
    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    # -----------------------------------------------------------------------
    # TODO 8 - BuildResult invariants
    # -----------------------------------------------------------------------
    # Model validator, mode "after", returns Self:
    #   1. success <=> failure_kind is None.
    #   2. success => errors empty and failed_task is None.
    #   3. timed_out => exit_code is None and failure_kind == timeout.
    #   4. exit_code is None => failure_kind is timeout or infrastructure.
    #   5. compile_error => all errors are CompilationError;
    #      test_failure => all errors are TestFailureError;
    #      timeout / infrastructure => errors empty.
    #   6. If errors is non-empty, exactly one error has is_primary=True.
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

        # 5. Error types must match failure_kind.
        if self.failure_kind is FailureKind.COMPILE_ERROR and not all(
            isinstance(error, CompilationError) for error in self.errors
        ):
            raise ValueError("failure_kind 'compile_error' allows only CompilationError errors")
        if self.failure_kind is FailureKind.TEST_FAILURE and not all(
            isinstance(error, TestFailureError) for error in self.errors
        ):
            raise ValueError("failure_kind 'test_failure' allows only TestFailureError errors")
        if self.failure_kind in (FailureKind.TIMEOUT, FailureKind.INFRASTRUCTURE) and self.errors:
            raise ValueError(f"failure_kind {self.failure_kind.value!r} must have no errors")

        # 6. If errors is non-empty, exactly one error has is_primary=True.
        primary_count = sum(1 for error in self.errors if error.is_primary)
        if self.errors and primary_count != 1:
            raise ValueError(f"exactly one error must be primary, got {primary_count}")

        return self


# ---------------------------------------------------------------------------
# Out of scope for schemas
# ---------------------------------------------------------------------------
# * Capping compilation errors to 5 and normalizing paths is the log_parser's
#   job; the schema only enforces the upper bound and format.
# * The LLM-facing view of BuildResult (without stdout/stderr) is defined in
#   Sprint 2.
# * Any `match` over BuildError must end with `case _: assert_never(...)`.
