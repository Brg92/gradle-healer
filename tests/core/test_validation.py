from typing import Any

import pytest
from pydantic import ValidationError

from gradle_healer.core.schemas import CodePatchPlan, TargetClassContext
from gradle_healer.core.validation import PlanIssue, PlanIssueCode, validate_plan_against_context
from tests.core.user_view_model import TEST_FILE, case_data, context_data, plan_data


def _validate(
    plan: dict[str, Any] | None = None, context: dict[str, Any] | None = None
) -> list[PlanIssue]:
    return validate_plan_against_context(
        CodePatchPlan.model_validate(plan or plan_data()),
        TargetClassContext.model_validate(context or context_data()),
    )


def _codes(issues: list[PlanIssue]) -> list[tuple[PlanIssueCode, str]]:
    return [(issue.code, issue.path) for issue in issues]


def _function(name: str, *, is_suspend: bool = False, returns_flow: bool = False) -> dict[str, Any]:
    return {
        "name": name,
        "parameters": [],
        "return_type": "Unit",
        "is_suspend": is_suspend,
        "returns_flow": returns_flow,
    }


def test_valid_plan_has_no_issues() -> None:
    assert _validate() == []


# --- One test per issue code ---


def test_target_mismatch() -> None:
    issues = _validate(plan_data(target_class="com.example.sandbox.GetUserUseCase"))

    assert _codes(issues) == [(PlanIssueCode.TARGET_MISMATCH, "target_class")]
    assert "'com.example.sandbox.UserViewModel'" in issues[0].message


def test_unknown_function() -> None:
    plan = plan_data(test_cases=[case_data(), case_data(name="b", function_under_test="refresh")])

    issues = _validate(plan)

    assert _codes(issues) == [(PlanIssueCode.UNKNOWN_FUNCTION, "test_cases[1].function_under_test")]
    assert issues[0].message == (
        "'refresh' is not a function or state property of UserViewModel; known: load, state"
    )


def test_state_property_is_a_valid_function_under_test() -> None:
    plan = plan_data(test_cases=[case_data(function_under_test="state")])

    assert _validate(plan) == []


def test_unknown_mock() -> None:
    plan = plan_data(test_cases=[case_data(mocks=["getUser", "UserRepository"])])

    issues = _validate(plan)

    assert _codes(issues) == [(PlanIssueCode.UNKNOWN_MOCK, "test_cases[0].mocks[1]")]
    assert issues[0].message == (
        "mock 'UserRepository' is not a constructor parameter name or type; "
        "constructor: (getUser: GetUserUseCase, scope: CoroutineScope)"
    )


def test_missing_test_framework() -> None:
    context = context_data(test_libraries=["mockk", "kotlinx_coroutines_test", "turbine"])

    issues = _validate(context=context)

    assert _codes(issues) == [(PlanIssueCode.MISSING_TEST_FRAMEWORK, "test_cases")]


@pytest.mark.parametrize("framework", ["junit4", "junit5"])
def test_either_junit_version_is_enough(framework: str) -> None:
    context = context_data(
        test_libraries=[framework, "mockk", "kotlinx_coroutines_test", "turbine"]
    )

    assert _validate(context=context) == []


@pytest.mark.parametrize(
    ("library", "path"),
    [
        ("mockk", "test_cases[0].mocks"),
        ("turbine", "test_cases[0].uses_turbine"),
        # The first test case whose function_under_test is the 'state' Flow.
        ("kotlinx_coroutines_test", "test_cases[3].function_under_test"),
    ],
)
def test_missing_library(library: str, path: str) -> None:
    libraries = ["junit5", "mockk", "kotlinx_coroutines_test", "turbine"]
    libraries.remove(library)

    issues = _validate(context=context_data(test_libraries=libraries))

    assert _codes(issues) == [(PlanIssueCode.MISSING_LIBRARY, path)]
    assert repr(library) in issues[0].message


@pytest.mark.parametrize(
    "function",
    [_function("load", is_suspend=True), _function("load", returns_flow=True)],
    ids=["suspend", "returns-flow"],
)
def test_coroutine_function_requires_coroutines_test(function: dict[str, Any]) -> None:
    plan = plan_data(test_cases=[case_data(mocks=[], uses_turbine=False)])
    context = context_data(functions=[function], test_libraries=["junit5"])

    issues = _validate(plan, context)

    assert _codes(issues) == [(PlanIssueCode.MISSING_LIBRARY, "test_cases[0].function_under_test")]


def test_plain_function_needs_no_extra_library() -> None:
    plan = plan_data(test_cases=[case_data(mocks=[], uses_turbine=False)])

    assert _validate(plan, context_data(test_libraries=["junit4"])) == []


def test_would_overwrite_existing_tests() -> None:
    issues = _validate(context=context_data(existing_test_file=TEST_FILE))

    assert _codes(issues) == [(PlanIssueCode.WOULD_OVERWRITE_EXISTING_TESTS, "test_file")]


def test_new_file_next_to_existing_tests_is_fine() -> None:
    existing = "core/src/test/kotlin/com/example/sandbox/UserViewModelLegacyTest.kt"

    assert _validate(context=context_data(existing_test_file=existing)) == []


@pytest.mark.parametrize(
    "test_file",
    [
        "core/src/test/kotlin/UserViewModelTest.kt",
        "core/src/test/kotlin/com/example/UserViewModelTest.kt",
        "core/src/test/kotlin/com/example/sandbox/ui/UserViewModelTest.kt",
    ],
)
def test_package_mismatch(test_file: str) -> None:
    issues = _validate(plan_data(test_file=test_file))

    assert _codes(issues) == [(PlanIssueCode.PACKAGE_MISMATCH, "test_file")]
    assert "'com/example/sandbox'" in issues[0].message


@pytest.mark.parametrize(
    ("test_file", "found"),
    [
        ("app/src/test/kotlin/com/example/sandbox/UserViewModelTest.kt", "'app'"),
        ("src/test/kotlin/com/example/sandbox/UserViewModelTest.kt", "the project root"),
    ],
    ids=["other-module", "root-instead-of-core"],
)
def test_wrong_module(test_file: str, found: str) -> None:
    issues = _validate(plan_data(test_file=test_file))

    assert _codes(issues) == [(PlanIssueCode.WRONG_MODULE, "test_file")]
    assert issues[0].message == (
        f"test_file must be in the module of the class under test: expected 'core', found {found}"
    )


@pytest.mark.parametrize(
    ("module", "source_file", "test_file"),
    [
        (
            ":",
            "src/main/kotlin/com/example/sandbox/UserViewModel.kt",
            "src/test/kotlin/com/example/sandbox/UserViewModelTest.kt",
        ),
        # Custom projectDir: the module path does not match the directory.
        (
            ":core",
            "modules/core-impl/src/main/kotlin/com/example/sandbox/UserViewModel.kt",
            "modules/core-impl/src/test/java/com/example/sandbox/UserViewModelTest.kt",
        ),
    ],
    ids=["root-module", "custom-project-dir"],
)
def test_same_module_directory_is_fine(module: str, source_file: str, test_file: str) -> None:
    context = context_data(module=module, source_file=source_file)

    assert _validate(plan_data(test_file=test_file), context) == []


# --- Overloads and type matching ---


def test_overloaded_function_is_known() -> None:
    overloads = [
        {**_function("load"), "parameters": [{"name": "id", "type": "String"}]},
        {**_function("load", is_suspend=True), "parameters": [{"name": "id", "type": "Int"}]},
    ]
    plan = plan_data(test_cases=[case_data(mocks=[], uses_turbine=False)])

    # Any overload being suspend makes coroutines-test required.
    issues = _validate(plan, context_data(functions=overloads, test_libraries=["junit5"]))

    assert _codes(issues) == [(PlanIssueCode.MISSING_LIBRARY, "test_cases[0].function_under_test")]


@pytest.mark.parametrize(
    ("declared", "mock"),
    [
        ("Repository<User>?", "Repository"),
        ("Repository<User>", "Repository<User>?"),
        ("Repository?", "Repository"),
        ("Map<String, List<Int>>", "Map<String, Int>"),
        ("Repository<User>?", "repository"),
    ],
)
def test_mock_type_ignores_nullability_and_generics(declared: str, mock: str) -> None:
    context = context_data(constructor_params=[{"name": "repository", "type": declared}])
    plan = plan_data(test_cases=[case_data(mocks=[mock], uses_turbine=False)])

    assert _validate(plan, context) == []


def test_mock_must_not_match_another_name_partially() -> None:
    context = context_data(constructor_params=[{"name": "repository", "type": "UserRepository"}])
    plan = plan_data(test_cases=[case_data(mocks=["Repository"], uses_turbine=False)])

    assert _codes(_validate(plan, context)) == [
        (PlanIssueCode.UNKNOWN_MOCK, "test_cases[0].mocks[0]")
    ]


# --- Multiple issues ---


def test_all_issues_are_returned_in_order() -> None:
    plan = plan_data(
        target_class="com.example.sandbox.Other",
        test_file="app/src/test/kotlin/OtherTest.kt",
        test_class_name="OtherTest",
        test_cases=[
            case_data(mocks=["api", "getUser", "db"]),
            case_data(name="b", function_under_test="refresh", mocks=[], uses_turbine=False),
        ],
    )
    context = context_data(existing_test_file="app/src/test/kotlin/OtherTest.kt", test_libraries=[])

    issues = _validate(plan, context)

    assert _codes(issues) == [
        (PlanIssueCode.TARGET_MISMATCH, "target_class"),
        (PlanIssueCode.WOULD_OVERWRITE_EXISTING_TESTS, "test_file"),
        (PlanIssueCode.WRONG_MODULE, "test_file"),
        (PlanIssueCode.PACKAGE_MISMATCH, "test_file"),
        (PlanIssueCode.UNKNOWN_MOCK, "test_cases[0].mocks[0]"),
        (PlanIssueCode.UNKNOWN_MOCK, "test_cases[0].mocks[2]"),
        (PlanIssueCode.UNKNOWN_FUNCTION, "test_cases[1].function_under_test"),
        (PlanIssueCode.MISSING_TEST_FRAMEWORK, "test_cases"),
        (PlanIssueCode.MISSING_LIBRARY, "test_cases[0].mocks"),
        (PlanIssueCode.MISSING_LIBRARY, "test_cases[0].uses_turbine"),
    ]
    assert _validate(plan, context) == issues


def test_long_values_keep_messages_within_limit() -> None:
    plan = plan_data(test_cases=[case_data(mocks=["M" * 200])])

    (issue,) = _validate(plan)

    assert issue.code is PlanIssueCode.UNKNOWN_MOCK
    assert len(issue.message) <= 300
    assert "…" in issue.message


def test_plan_issue_contract() -> None:
    with pytest.raises(ValidationError, match="at most 300 characters"):
        PlanIssue(code=PlanIssueCode.UNKNOWN_MOCK, path="test_cases[0]", message="x" * 301)
