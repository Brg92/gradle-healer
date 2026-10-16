"""Valid TargetClassContext and CodePatchPlan payloads for the sandbox UserViewModel.

Signatures are filled by hand from the real file in evals/fixtures/sandbox-jvm.
Builders return fresh dicts so tests can override fields freely.
"""

from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SANDBOX = REPO_ROOT / "evals" / "fixtures" / "sandbox-jvm"
SOURCE_FILE = "core/src/main/kotlin/com/example/sandbox/UserViewModel.kt"
TEST_FILE = "core/src/test/kotlin/com/example/sandbox/UserViewModelTest.kt"


def context_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "module": ":core",
        "module_kind": "jvm",
        "source_file": SOURCE_FILE,
        "package": "com.example.sandbox",
        "class_name": "UserViewModel",
        "class_kind": "class",
        "source_code": (SANDBOX / SOURCE_FILE).read_text(encoding="utf-8"),
        "constructor_params": [
            {"name": "getUser", "type": "GetUserUseCase"},
            {"name": "scope", "type": "CoroutineScope"},
        ],
        "functions": [
            {
                "name": "load",
                "parameters": [{"name": "id", "type": "String"}],
                "return_type": "Unit",
                "is_suspend": False,
                "returns_flow": False,
            }
        ],
        "state_properties": [{"name": "state", "type": "StateFlow<UiState>", "is_flow": True}],
        "related_types": [
            {
                "name": "com.example.sandbox.GetUserUseCase",
                "source_file": "core/src/main/kotlin/com/example/sandbox/GetUserUseCase.kt",
                "declaration": "class GetUserUseCase(private val repository: UserRepository) {\n"
                "    suspend operator fun invoke(id: String): User\n"
                "    fun observe(id: String): Flow<User>\n"
                "}",
            },
            {
                "name": "com.example.sandbox.UiState",
                "source_file": "core/src/main/kotlin/com/example/sandbox/UiState.kt",
                "declaration": "sealed interface UiState {\n"
                "    data object Loading : UiState\n"
                "    data class Success(val user: User) : UiState\n"
                "    data class Error(val message: String) : UiState\n"
                "}",
            },
            {
                "name": "com.example.sandbox.User",
                "source_file": "core/src/main/kotlin/com/example/sandbox/User.kt",
                "declaration": "data class User(\n"
                "    val id: String, val name: String, val email: String\n"
                ")",
            },
            {
                "name": "kotlinx.coroutines.CoroutineScope",
                "source_file": None,
                "declaration": "public interface CoroutineScope {\n"
                "    public val coroutineContext: CoroutineContext\n"
                "}",
            },
        ],
        "existing_test_file": None,
        "test_libraries": ["junit5", "mockk", "kotlinx_coroutines_test", "turbine"],
    }
    data.update(overrides)
    return data


def case_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "load emits Success when the use case returns a user",
        "function_under_test": "load",
        "category": "happy_path",
        "given": "getUser returns User 42",
        "then": "state emits Loading, then Success with User 42",
        "mocks": ["getUser"],
        "uses_turbine": True,
    }
    data.update(overrides)
    return data


def plan_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "kind": "generate_tests",
        "target_class": "com.example.sandbox.UserViewModel",
        "test_file": TEST_FILE,
        "test_class_name": "UserViewModelTest",
        "test_cases": [
            case_data(),
            case_data(
                name="load emits Error with the exception message when the use case throws",
                category="error",
                given="getUser throws IllegalStateException('boom')",
                then="state emits Loading, then Error('boom')",
                mocks=["GetUserUseCase"],
            ),
            case_data(
                name="load cancels the previous request when called again",
                category="cancellation",
                given="the first getUser call suspends forever",
                then="only the second user is emitted",
                uses_turbine=False,
            ),
            case_data(
                name="state starts as Loading",
                function_under_test="state",
                category="edge_case",
                given="a new UserViewModel",
                then="state.value is Loading",
                mocks=[],
                uses_turbine=False,
            ),
        ],
        "rationale": "Covers the success and error branches of load, cancellation of the "
        "previous job, and the initial state exposed by the StateFlow.",
    }
    data.update(overrides)
    return data
