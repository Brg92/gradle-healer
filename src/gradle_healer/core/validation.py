"""Cross-checks between a CodePatchPlan and the TargetClassContext it was planned from.

CodePatchPlan validators only check the plan in isolation. This module checks
it against the Analyzer's facts and returns structured issues that the
Planner can be retried with. Pure functions only: no I/O, no LLM.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from gradle_healer.core.schemas import CodePatchPlan, TargetClassContext, TestLibrary

_MESSAGE_MAX_CHARS = 300
_VALUE_MAX_CHARS = 80
_ELLIPSIS = "…"


# --- PlanIssueCode ---
class PlanIssueCode(StrEnum):
    """Category of an inconsistency between a plan and the target class context."""

    TARGET_MISMATCH = "target_mismatch"
    UNKNOWN_FUNCTION = "unknown_function"
    UNKNOWN_MOCK = "unknown_mock"
    MISSING_TEST_FRAMEWORK = "missing_test_framework"
    MISSING_LIBRARY = "missing_library"
    WOULD_OVERWRITE_EXISTING_TESTS = "would_overwrite_existing_tests"
    PACKAGE_MISMATCH = "package_mismatch"
    WRONG_MODULE = "wrong_module"


# --- PlanIssue ---
class PlanIssue(BaseModel):
    """One inconsistency between a plan and the target class context."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: PlanIssueCode = Field(description="Category of the issue.")
    path: str = Field(
        min_length=1,
        max_length=200,
        description="Location of the offending value in the plan, e.g. 'test_cases[2].mocks[0]'.",
    )
    message: str = Field(
        min_length=1,
        max_length=_MESSAGE_MAX_CHARS,
        description="Human-readable explanation, addressed to the Planner.",
    )


# --- Helpers ---
def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - len(_ELLIPSIS)] + _ELLIPSIS


def _issue(code: PlanIssueCode, path: str, message: str) -> PlanIssue:
    return PlanIssue(code=code, path=path, message=_truncate(message, _MESSAGE_MAX_CHARS))


def _quote(value: str) -> str:
    return repr(_truncate(value, _VALUE_MAX_CHARS))


# 'Repository<User>?' -> 'Repository': generics and the nullability marker are ignored.
def _base_type(type_name: str) -> str:
    return type_name.split("<", 1)[0].strip().removesuffix("?").strip()


# Module directory: everything before the first 'src/<source_set>/' pair, '' for the
# root project. Independent of the module path, so a custom projectDir works too.
# Both paths were checked by _validate_source_path, so the pair always exists.
def _module_dir(path: str, source_set: Literal["main", "test"]) -> str:
    parts = path.split("/")
    index = next(i for i in range(len(parts) - 2) if parts[i : i + 2] == ["src", source_set])
    return "/".join(parts[:index])


def _describe_dir(module_dir: str) -> str:
    return repr(module_dir) if module_dir else "the project root"


def _uses_coroutines(member: str, context: TargetClassContext) -> bool:
    # Overload-aware: any overload that is suspend or returns a Flow counts.
    return any(
        function.name == member and (function.is_suspend or function.returns_flow)
        for function in context.functions
    ) or any(prop.name == member and prop.is_flow for prop in context.state_properties)


# --- validate_plan_against_context ---
def validate_plan_against_context(
    plan: CodePatchPlan, context: TargetClassContext
) -> list[PlanIssue]:
    """Return every inconsistency between plan and context; empty if the plan is consistent.

    Issues are ordered by plan field (target_class; test_file: overwrite, module,
    package; then each test case in order), followed by missing test framework
    and missing libraries.
    """
    issues: list[PlanIssue] = []

    # 1. target_class.
    if plan.target_class != context.fully_qualified_name:
        issues.append(
            _issue(
                PlanIssueCode.TARGET_MISMATCH,
                "target_class",
                f"target_class is {_quote(plan.target_class)} but the class under test is "
                f"{context.fully_qualified_name!r}",
            )
        )

    # 2. test_file.
    if plan.test_file == context.existing_test_file:
        issues.append(
            _issue(
                PlanIssueCode.WOULD_OVERWRITE_EXISTING_TESTS,
                "test_file",
                f"test_file {plan.test_file!r} already exists and must not be overwritten; "
                "choose a new file name",
            )
        )
    expected_dir = _module_dir(context.source_file, "main")
    found_dir = _module_dir(plan.test_file, "test")
    if found_dir != expected_dir:
        issues.append(
            _issue(
                PlanIssueCode.WRONG_MODULE,
                "test_file",
                f"test_file must be in the module of the class under test: expected "
                f"{_describe_dir(expected_dir)}, found {_describe_dir(found_dir)}",
            )
        )
    package_dirs = context.package.split(".")
    test_dirs = plan.test_file.split("/")[:-1]
    if test_dirs[-len(package_dirs) :] != package_dirs:
        issues.append(
            _issue(
                PlanIssueCode.PACKAGE_MISMATCH,
                "test_file",
                f"test_file directory must end with {'/'.join(package_dirs)!r} "
                f"(package {context.package!r})",
            )
        )

    # 3. Test cases.
    members = [function.name for function in context.functions]
    members += [prop.name for prop in context.state_properties if prop.name not in members]
    known_mocks = {parameter.name for parameter in context.constructor_params}
    known_mocks |= {_base_type(parameter.type) for parameter in context.constructor_params}
    constructor = ", ".join(
        f"{parameter.name}: {parameter.type}" for parameter in context.constructor_params
    )
    # Library -> path of the first test case that needs it.
    needed: dict[TestLibrary, str] = {}

    for index, case in enumerate(plan.test_cases):
        base = f"test_cases[{index}]"
        if case.function_under_test not in members:
            issues.append(
                _issue(
                    PlanIssueCode.UNKNOWN_FUNCTION,
                    f"{base}.function_under_test",
                    f"{_quote(case.function_under_test)} is not a function or state property "
                    f"of {context.class_name}; known: {', '.join(members) or 'none'}",
                )
            )
        for mock_index, mock in enumerate(case.mocks):
            if mock not in known_mocks and _base_type(mock) not in known_mocks:
                issues.append(
                    _issue(
                        PlanIssueCode.UNKNOWN_MOCK,
                        f"{base}.mocks[{mock_index}]",
                        f"mock {_quote(mock)} is not a constructor parameter name or type; "
                        f"constructor: ({constructor})",
                    )
                )
        if case.mocks:
            needed.setdefault(TestLibrary.MOCKK, f"{base}.mocks")
        if case.uses_turbine:
            needed.setdefault(TestLibrary.TURBINE, f"{base}.uses_turbine")
        if _uses_coroutines(case.function_under_test, context):
            needed.setdefault(TestLibrary.KOTLINX_COROUTINES_TEST, f"{base}.function_under_test")

    # 4. Test framework and libraries.
    if not {TestLibrary.JUNIT4, TestLibrary.JUNIT5} & set(context.test_libraries):
        issues.append(
            _issue(
                PlanIssueCode.MISSING_TEST_FRAMEWORK,
                "test_cases",
                f"module {context.module!r} has neither JUnit 4 nor JUnit 5 on its test classpath",
            )
        )
    for library in (TestLibrary.MOCKK, TestLibrary.TURBINE, TestLibrary.KOTLINX_COROUTINES_TEST):
        if library in needed and library not in context.test_libraries:
            issues.append(
                _issue(
                    PlanIssueCode.MISSING_LIBRARY,
                    needed[library],
                    f"plan requires {library.value!r}, which is not on the test classpath of "
                    f"module {context.module!r}",
                )
            )

    return issues
