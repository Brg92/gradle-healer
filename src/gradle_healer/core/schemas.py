"""Data contracts for GradleHealer.

Build results are produced by deterministic tools and are the only build
information passed to nodes. TargetClassContext is the Analyzer output;
CodePatchPlan is the Planner's LLM structured output.
"""

import re
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

# --- GRADLE_TASK_PATH_PATTERN ---
# Shared with tools.gradle_runner, which validates task paths before launching Gradle.
GRADLE_TASK_PATH_PATTERN = r"^(:[A-Za-z0-9_.-]+)+$"

# --- GRADLE_MODULE_PATH_PATTERN ---
# ':' is the root project; otherwise the same segments as a task path, e.g. ':core'.
GRADLE_MODULE_PATH_PATTERN = r"^(?::|(?::[A-Za-z0-9_.-]+)+)$"

# --- JVM name patterns ---
# Plain (non-backticked) identifiers only. JVM_PACKAGE_PATTERN is shared with
# tools.log_parser, which validates target_package with it.
_IDENTIFIER = r"[A-Za-z_]\w*"
KOTLIN_IDENTIFIER_PATTERN = rf"^{_IDENTIFIER}$"
JVM_PACKAGE_PATTERN = rf"^{_IDENTIFIER}(?:\.{_IDENTIFIER})*$"
# A package followed by at least one more identifier, e.g. 'com.example.sandbox.User'.
_QUALIFIED_NAME_PATTERN = rf"^{_IDENTIFIER}(?:\.{_IDENTIFIER})+$"

_IDENTIFIER_RE = re.compile(KOTLIN_IDENTIFIER_PATTERN)
_QUALIFIED_NAME_RE = re.compile(_QUALIFIED_NAME_PATTERN)

_NAME_MAX_CHARS = 150
_TYPE_MAX_CHARS = 200
_PATH_MAX_CHARS = 300


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
        pattern=GRADLE_TASK_PATH_PATTERN,
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
        pattern=GRADLE_TASK_PATH_PATTERN,
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


# =============================================================================
# Analyzer output: TargetClassContext
# =============================================================================
# Produced by a deterministic tool, so Field constraints are allowed. The
# models describe the target class, not the parser that extracted it.


# --- Path rules ---
# Paths are relative to the Gradle project root, '/'-separated and normalized
# (no '', '.' or '..' segments), so that two paths to the same file compare equal.
def _validate_relative_path(value: str) -> str:
    if value.startswith(("/", "file:")):
        raise ValueError(f"path must be relative to the project root, got {value!r}")
    if "\\" in value:
        raise ValueError(f"path must use '/' separators, got {value!r}")
    parts = value.split("/")
    if ".." in parts:
        raise ValueError(f"path must not contain '..' segments, got {value!r}")
    if any(part in ("", ".") for part in parts):
        raise ValueError(f"path must not contain empty or '.' segments, got {value!r}")
    return value


def _validate_source_path(value: str, source_set: Literal["main", "test"]) -> str:
    parts = _validate_relative_path(value).split("/")
    if "androidTest" in parts:
        raise ValueError(f"path must not be in an androidTest source set, got {value!r}")
    file_name = parts[-1]
    if not file_name.endswith(".kt") or file_name == ".kt":
        raise ValueError(f"path must point to a Kotlin '.kt' file, got {value!r}")
    # 'src', source_set must be two consecutive directories before the file name.
    if not any(parts[index : index + 2] == ["src", source_set] for index in range(len(parts) - 2)):
        raise ValueError(f"path must be inside a 'src/{source_set}/' directory, got {value!r}")
    return value


def _duplicates[T](keys: list[T]) -> list[T]:
    """Keys that occur more than once, each reported once, in order of first repetition."""
    seen: set[T] = set()
    repeated: list[T] = []
    for key in keys:
        if key in seen and key not in repeated:
            repeated.append(key)
        seen.add(key)
    return repeated


# --- ModuleKind ---
class ModuleKind(StrEnum):
    """Kind of Gradle module: pure JVM (':m:test') or Android (':m:testDebugUnitTest')."""

    JVM = "jvm"
    ANDROID = "android"


# --- ClassKind ---
class ClassKind(StrEnum):
    """Kotlin declaration kind of the target class."""

    CLASS = "class"
    DATA_CLASS = "data_class"
    OBJECT = "object"
    ABSTRACT_CLASS = "abstract_class"


# --- TestLibrary ---
class TestLibrary(StrEnum):
    """Test library available on the module's unit test classpath."""

    JUNIT4 = "junit4"
    JUNIT5 = "junit5"
    MOCKK = "mockk"
    TURBINE = "turbine"
    KOTLINX_COROUTINES_TEST = "kotlinx_coroutines_test"


# --- Parameter ---
class Parameter(BaseModel):
    """Named, typed parameter of a constructor or function."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(
        max_length=_NAME_MAX_CHARS,
        pattern=KOTLIN_IDENTIFIER_PATTERN,
        description="Parameter name: a plain Kotlin identifier.",
    )
    type: str = Field(
        min_length=1,
        max_length=_TYPE_MAX_CHARS,
        description="Kotlin type as written in the source, e.g. 'StateFlow<UiState>?'.",
    )


# --- FunctionSignature ---
class FunctionSignature(BaseModel):
    """Signature of a function of the target class that tests may call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(
        max_length=_NAME_MAX_CHARS,
        pattern=KOTLIN_IDENTIFIER_PATTERN,
        description="Function name: a plain Kotlin identifier; overloads share it.",
    )
    parameters: list[Parameter] = Field(
        max_length=20,
        description="Parameters in declaration order; names are unique.",
    )
    return_type: str = Field(
        min_length=1,
        max_length=_TYPE_MAX_CHARS,
        description="Kotlin return type as written in the source; 'Unit' when omitted.",
    )
    is_suspend: bool = Field(description="True if the function is declared 'suspend'.")
    returns_flow: bool = Field(
        description="True if the return type is a Flow (Flow, StateFlow, SharedFlow, ...).",
    )

    @field_validator("parameters")
    @classmethod
    def require_unique_parameter_names(cls, value: list[Parameter]) -> list[Parameter]:
        if repeated := _duplicates([parameter.name for parameter in value]):
            raise ValueError(f"parameter names must be unique, duplicated: {', '.join(repeated)}")
        return value


# --- PropertySignature ---
class PropertySignature(BaseModel):
    """Public property of the target class that exposes observable state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(
        max_length=_NAME_MAX_CHARS,
        pattern=KOTLIN_IDENTIFIER_PATTERN,
        description="Property name: a plain Kotlin identifier.",
    )
    type: str = Field(
        min_length=1,
        max_length=_TYPE_MAX_CHARS,
        description="Kotlin type as written in the source, e.g. 'StateFlow<UiState>'.",
    )
    is_flow: bool = Field(
        description="True if the type is a Flow (Flow, StateFlow, SharedFlow, ...).",
    )


# --- RelatedType ---
class RelatedType(BaseModel):
    """Type the target class depends on, with the declaration tests need to use it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(
        max_length=_PATH_MAX_CHARS,
        pattern=_QUALIFIED_NAME_PATTERN,
        description="Fully qualified name, e.g. 'com.example.sandbox.UserRepository'.",
    )
    source_file: str | None = Field(
        max_length=_PATH_MAX_CHARS,
        description="Declaring file relative to the Gradle project root; None for library types.",
    )
    declaration: str = Field(
        min_length=1,
        max_length=2000,
        description="Declaration excerpt: header and public member signatures, no bodies.",
    )

    @field_validator("source_file")
    @classmethod
    def require_relative_source_file(cls, value: str | None) -> str | None:
        return None if value is None else _validate_relative_path(value)


# --- TargetClassContext ---
class TargetClassContext(BaseModel):
    """Minimal semantic context of the class under test, produced by the Analyzer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    module: str = Field(
        max_length=_PATH_MAX_CHARS,
        pattern=GRADLE_MODULE_PATH_PATTERN,
        description="Gradle module path: ':' for the root project, otherwise one or more "
        "':'-prefixed segments of letters, digits, '_', '-' or '.', e.g. ':core'.",
    )
    module_kind: ModuleKind = Field(
        description="Whether the module is a pure JVM or an Android module.",
    )
    source_file: str = Field(
        max_length=_PATH_MAX_CHARS,
        description="Kotlin file declaring the class, relative to the Gradle project root, "
        "inside a 'src/main/' directory.",
    )
    package: str = Field(
        max_length=_PATH_MAX_CHARS,
        pattern=JVM_PACKAGE_PATTERN,
        description="Package of the class, e.g. 'com.example.sandbox'.",
    )
    class_name: str = Field(
        max_length=_NAME_MAX_CHARS,
        pattern=KOTLIN_IDENTIFIER_PATTERN,
        description="Simple name of the class: a plain Kotlin identifier.",
    )
    class_kind: ClassKind = Field(description="Kotlin declaration kind of the class.")
    source_code: str = Field(
        min_length=1,
        max_length=20_000,
        description="Source code of the class declaration.",
    )
    constructor_params: list[Parameter] = Field(
        max_length=20,
        description="Primary constructor parameters in declaration order; names are unique.",
    )
    functions: list[FunctionSignature] = Field(
        max_length=50,
        description="Public functions; overloads allowed, signatures (name and parameter "
        "types) are unique.",
    )
    state_properties: list[PropertySignature] = Field(
        max_length=20,
        description="Public properties exposing observable state; names are unique.",
    )
    related_types: list[RelatedType] = Field(
        max_length=20,
        description="Types the class depends on; fully qualified names are unique.",
    )
    existing_test_file: str | None = Field(
        max_length=_PATH_MAX_CHARS,
        description="Existing unit test file for the class, relative to the Gradle project "
        "root, inside a 'src/test/' directory; None if there is none.",
    )
    test_libraries: list[TestLibrary] = Field(
        description="Test libraries on the module's unit test classpath; no duplicates.",
    )

    # --- TargetClassContext.fully_qualified_name ---
    # Plain @property for the same reason as BuildResult.success.
    @property
    def fully_qualified_name(self) -> str:
        return f"{self.package}.{self.class_name}"

    @field_validator("source_file")
    @classmethod
    def require_main_source_file(cls, value: str) -> str:
        return _validate_source_path(value, "main")

    @field_validator("existing_test_file")
    @classmethod
    def require_test_source_file(cls, value: str | None) -> str | None:
        return None if value is None else _validate_source_path(value, "test")

    @field_validator("constructor_params")
    @classmethod
    def require_unique_constructor_params(cls, value: list[Parameter]) -> list[Parameter]:
        if repeated := _duplicates([parameter.name for parameter in value]):
            raise ValueError(
                f"constructor_params names must be unique, duplicated: {', '.join(repeated)}"
            )
        return value

    @field_validator("functions")
    @classmethod
    def require_unique_signatures(cls, value: list[FunctionSignature]) -> list[FunctionSignature]:
        signatures = [
            (function.name, tuple(parameter.type for parameter in function.parameters))
            for function in value
        ]
        if repeated := _duplicates(signatures):
            rendered = ", ".join(f"{name}({', '.join(types)})" for name, types in repeated)
            raise ValueError(f"function signatures must be unique, duplicated: {rendered}")
        return value

    @field_validator("state_properties")
    @classmethod
    def require_unique_properties(cls, value: list[PropertySignature]) -> list[PropertySignature]:
        if repeated := _duplicates([prop.name for prop in value]):
            raise ValueError(
                f"state_properties names must be unique, duplicated: {', '.join(repeated)}"
            )
        return value

    @field_validator("related_types")
    @classmethod
    def require_unique_related_types(cls, value: list[RelatedType]) -> list[RelatedType]:
        if repeated := _duplicates([related.name for related in value]):
            raise ValueError(
                f"related_types names must be unique, duplicated: {', '.join(repeated)}"
            )
        return value

    @field_validator("test_libraries")
    @classmethod
    def require_unique_libraries(cls, value: list[TestLibrary]) -> list[TestLibrary]:
        if repeated := _duplicates(value):
            rendered = ", ".join(library.value for library in repeated)
            raise ValueError(f"test_libraries must not contain duplicates, duplicated: {rendered}")
        return value


# =============================================================================
# Planner output: CodePatchPlan (LLM structured output)
# =============================================================================
# The JSON Schema must be portable across LLM structured-output APIs:
# * only native types (str, bool, list, Literal, StrEnum, nested models);
# * no Field constraints and no defaults: every field is required;
# * every rule lives in a validator and is stated in the field description.
# Strings are stripped before they are checked. Lengths are checked before a
# value is echoed in an error message, so messages stay short.

_TEST_NAME_MAX_CHARS = 150
_TEST_TEXT_MAX_CHARS = 300
_RATIONALE_MAX_CHARS = 500
_MAX_TEST_CASES = 10
# Characters Kotlin rejects in a backtick-quoted JVM name; line breaks are checked apart.
_FORBIDDEN_TEST_NAME_CHARS = "`.;[]/<>:\\"


def _clean_text(value: str, field: str, max_chars: int) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{field} must not be blank")
    if len(text) > max_chars:
        raise ValueError(f"{field} must be at most {max_chars} characters, got {len(text)}")
    return text


# --- TestCategory ---
class TestCategory(StrEnum):
    """Category of a planned test case."""

    HAPPY_PATH = "happy_path"
    ERROR = "error"
    EDGE_CASE = "edge_case"
    CANCELLATION = "cancellation"


# --- PlannedTestCase ---
class PlannedTestCase(BaseModel):
    """One unit test to generate, described as a given/then pair."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(
        description="Test method name, used as a backtick-quoted Kotlin name: one line, "
        f"at most {_TEST_NAME_MAX_CHARS} characters, none of "
        f"{' '.join(_FORBIDDEN_TEST_NAME_CHARS)}. Unique within the plan, ignoring case.",
    )
    function_under_test: str = Field(
        description="Name of the function or state property of the target class this test "
        "exercises: a plain Kotlin identifier.",
    )
    category: TestCategory = Field(description="Category of the test case.")
    given: str = Field(
        description=f"Preconditions and stubbed behaviour, at most {_TEST_TEXT_MAX_CHARS} "
        "characters.",
    )
    then: str = Field(
        description=f"Expected outcome to assert, at most {_TEST_TEXT_MAX_CHARS} characters.",
    )
    mocks: list[str] = Field(
        description="Constructor dependencies to mock with MockK, each given as the "
        "constructor parameter name or its type; unique, possibly empty.",
    )
    uses_turbine: bool = Field(
        description="True if the test collects a Flow with Turbine's test { }.",
    )

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str) -> str:
        name = _clean_text(value, "name", _TEST_NAME_MAX_CHARS)
        if len(name.splitlines()) > 1:
            raise ValueError("name must be a single line")
        if forbidden := "".join(dict.fromkeys(c for c in name if c in _FORBIDDEN_TEST_NAME_CHARS)):
            raise ValueError(
                f"name must be a valid JVM backtick name, found forbidden characters {forbidden!r}"
            )
        return name

    @field_validator("function_under_test")
    @classmethod
    def check_function_under_test(cls, value: str) -> str:
        name = _clean_text(value, "function_under_test", _NAME_MAX_CHARS)
        if not _IDENTIFIER_RE.fullmatch(name):
            raise ValueError(f"function_under_test must be a Kotlin identifier, got {name!r}")
        return name

    @field_validator("given", "then")
    @classmethod
    def check_text(cls, value: str, info: ValidationInfo) -> str:
        return _clean_text(value, str(info.field_name), _TEST_TEXT_MAX_CHARS)

    @field_validator("mocks")
    @classmethod
    def check_mocks(cls, value: list[str]) -> list[str]:
        mocks = [
            _clean_text(mock, f"mocks[{index}]", _TYPE_MAX_CHARS)
            for index, mock in enumerate(value)
        ]
        if repeated := _duplicates(mocks):
            raise ValueError(f"mocks must be unique, duplicated: {', '.join(repeated)}")
        return mocks


# --- CodePatchPlan ---
class CodePatchPlan(BaseModel):
    """Planner output: the unit test file to generate for the target class."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["generate_tests"] = Field(
        description="Kind of patch: always 'generate_tests'.",
    )
    target_class: str = Field(
        description="Fully qualified name of the class under test, e.g. "
        "'com.example.sandbox.UserViewModel'.",
    )
    test_file: str = Field(
        description="Test file to create, relative to the Gradle project root: '/'-separated, "
        "inside a 'src/test/' directory (never androidTest), named '<test_class_name>.kt'.",
    )
    test_class_name: str = Field(
        description="Simple name of the test class: a Kotlin identifier ending with 'Test', "
        "e.g. 'UserViewModelTest'.",
    )
    test_cases: list[PlannedTestCase] = Field(
        description=f"Test cases to generate: 1 to {_MAX_TEST_CASES}, names unique ignoring "
        "case, at least one 'happy_path'.",
    )
    rationale: str = Field(
        description=f"Why these test cases were chosen, at most {_RATIONALE_MAX_CHARS} characters.",
    )

    @field_validator("target_class")
    @classmethod
    def check_target_class(cls, value: str) -> str:
        name = _clean_text(value, "target_class", _PATH_MAX_CHARS)
        if not _QUALIFIED_NAME_RE.fullmatch(name):
            raise ValueError(
                f"target_class must be a fully qualified class name like "
                f"'com.example.Foo', got {name!r}"
            )
        return name

    @field_validator("test_file")
    @classmethod
    def check_test_file(cls, value: str) -> str:
        return _validate_source_path(_clean_text(value, "test_file", _PATH_MAX_CHARS), "test")

    @field_validator("test_class_name")
    @classmethod
    def check_test_class_name(cls, value: str) -> str:
        name = _clean_text(value, "test_class_name", _NAME_MAX_CHARS)
        if not _IDENTIFIER_RE.fullmatch(name):
            raise ValueError(f"test_class_name must be a Kotlin identifier, got {name!r}")
        # A bare 'Test' would clash with the JUnit @Test annotation.
        if not name.endswith("Test") or name == "Test":
            raise ValueError(
                f"test_class_name must end with 'Test' after a non-empty prefix, got {name!r}"
            )
        return name

    @field_validator("test_cases")
    @classmethod
    def check_test_cases(cls, value: list[PlannedTestCase]) -> list[PlannedTestCase]:
        if not 1 <= len(value) <= _MAX_TEST_CASES:
            raise ValueError(
                f"test_cases must contain 1 to {_MAX_TEST_CASES} items, got {len(value)}"
            )
        if repeated := _duplicates([case.name.casefold() for case in value]):
            raise ValueError(
                f"test case names must be unique ignoring case, duplicated: {', '.join(repeated)}"
            )
        if all(case.category is not TestCategory.HAPPY_PATH for case in value):
            raise ValueError("test_cases must include at least one 'happy_path' case")
        return value

    @field_validator("rationale")
    @classmethod
    def check_rationale(cls, value: str) -> str:
        return _clean_text(value, "rationale", _RATIONALE_MAX_CHARS)

    @model_validator(mode="after")
    def require_file_named_after_class(self) -> Self:
        expected = f"{self.test_class_name}.kt"
        file_name = self.test_file.rsplit("/", 1)[-1]
        if file_name != expected:
            raise ValueError(f"test_file must be named {expected!r}, got {file_name!r}")
        return self
