import re
from typing import Any, Literal

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

# TestCategory and TestLibrary are reached through the module: imported by name, pytest
# would try to collect them as test classes.
from gradle_healer.core import schemas
from gradle_healer.core.schemas import (
    JVM_PACKAGE_PATTERN,
    KOTLIN_IDENTIFIER_PATTERN,
    ClassKind,
    CodePatchPlan,
    FunctionSignature,
    ModuleKind,
    Parameter,
    PlannedTestCase,
    PropertySignature,
    RelatedType,
    TargetClassContext,
    _validate_source_path,
)
from tests.core.user_view_model import (
    SANDBOX,
    SOURCE_FILE,
    TEST_FILE,
    case_data,
    context_data,
    plan_data,
)


def _function(name: str, *types: str) -> dict[str, Any]:
    return {
        "name": name,
        "parameters": [{"name": f"p{index}", "type": type_} for index, type_ in enumerate(types)],
        "return_type": "Unit",
        "is_suspend": False,
        "returns_flow": False,
    }


# --- Shared patterns ---


@pytest.mark.parametrize("value", ["com", "com.example.sandbox", "_a.b1"])
def test_package_pattern_accepts_packages(value: str) -> None:
    assert re.fullmatch(JVM_PACKAGE_PATTERN, value)


@pytest.mark.parametrize("value", ["", "com.", ".com", "com..example", "1com", "com.ex-ample"])
def test_package_pattern_rejects_malformed(value: str) -> None:
    assert not re.fullmatch(JVM_PACKAGE_PATTERN, value)


@pytest.mark.parametrize(("value", "valid"), [("load", True), ("_x1", True), ("1x", False)])
def test_identifier_pattern(value: str, valid: bool) -> None:
    assert bool(re.fullmatch(KOTLIN_IDENTIFIER_PATTERN, value)) is valid


@pytest.mark.parametrize("module", [":", ":core", ":feature-login", ":features:lib.core"])
def test_module_accepts_gradle_paths(module: str) -> None:
    assert TargetClassContext.model_validate(context_data(module=module)).module == module


@pytest.mark.parametrize("module", ["", "::", "core", ":core:", ":core::lib", ": core"])
def test_module_rejects_malformed_paths(module: str) -> None:
    with pytest.raises(ValidationError, match="String should match pattern"):
        TargetClassContext.model_validate(context_data(module=module))


# --- TargetClassContext: sandbox UserViewModel ---


def test_user_view_model_context_is_valid() -> None:
    context = TargetClassContext.model_validate(context_data())

    assert context.fully_qualified_name == "com.example.sandbox.UserViewModel"
    assert context.module_kind is ModuleKind.JVM
    assert context.class_kind is ClassKind.CLASS
    assert context.source_code == (SANDBOX / SOURCE_FILE).read_text(encoding="utf-8")
    assert context.test_libraries[0] is schemas.TestLibrary.JUNIT5
    assert context.related_types[-1].source_file is None


def test_context_json_round_trip() -> None:
    context = TargetClassContext.model_validate(context_data())

    restored = TargetClassContext.model_validate_json(context.model_dump_json())

    assert restored == context


def test_fully_qualified_name_is_not_serialized() -> None:
    assert (
        "fully_qualified_name" not in TargetClassContext.model_validate(context_data()).model_dump()
    )


# --- Path helper ---


@pytest.mark.parametrize(
    ("path", "source_set"),
    [
        ("src/main/kotlin/com/example/Foo.kt", "main"),
        ("src/main/java/com/example/Foo.kt", "main"),
        ("core/src/main/kotlin/com/example/Foo.kt", "main"),
        ("features/login/src/main/java/Foo.kt", "main"),
        ("src/test/kotlin/com/example/FooTest.kt", "test"),
        ("core/src/test/java/com/example/FooTest.kt", "test"),
    ],
)
def test_source_path_accepts(path: str, source_set: Literal["main", "test"]) -> None:
    assert _validate_source_path(path, source_set) == path


@pytest.mark.parametrize(
    ("path", "source_set", "message"),
    [
        ("/home/u/core/src/main/kotlin/Foo.kt", "main", "relative to the project root"),
        ("file:///core/src/main/kotlin/Foo.kt", "main", "relative to the project root"),
        ("core/src/main/kotlin/../Foo.kt", "main", "'..' segments"),
        ("core\\src\\main\\kotlin\\Foo.kt", "main", "'/' separators"),
        ("core/src/main/kotlin//Foo.kt", "main", "empty or '.' segments"),
        ("core/./src/main/kotlin/Foo.kt", "main", "empty or '.' segments"),
        ("core/src/androidTest/kotlin/FooTest.kt", "test", "androidTest"),
        ("core/src/test/androidTest/FooTest.kt", "test", "androidTest"),
        ("core/src/main/java/Foo.java", "main", "Kotlin '.kt' file"),
        ("core/src/main/kotlin/.kt", "main", "Kotlin '.kt' file"),
        ("core/src/test/kotlin/FooTest.kt", "main", "'src/main/' directory"),
        ("core/src/main/kotlin/Foo.kt", "test", "'src/test/' directory"),
        ("core/main/kotlin/Foo.kt", "main", "'src/main/' directory"),
        ("core/src/Foo.kt", "main", "'src/main/' directory"),
    ],
)
def test_source_path_rejects(path: str, source_set: Literal["main", "test"], message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        _validate_source_path(path, source_set)


def test_context_source_file_must_be_main() -> None:
    with pytest.raises(ValidationError, match=re.escape("'src/main/' directory")):
        TargetClassContext.model_validate(context_data(source_file=TEST_FILE))


def test_context_existing_test_file_must_be_test() -> None:
    with pytest.raises(ValidationError, match=re.escape("'src/test/' directory")):
        TargetClassContext.model_validate(context_data(existing_test_file=SOURCE_FILE))


def test_related_type_source_file_must_be_relative() -> None:
    with pytest.raises(ValidationError, match="relative to the project root"):
        RelatedType(name="com.example.Foo", source_file="/abs/Foo.kt", declaration="class Foo")


def test_related_type_name_must_be_fully_qualified() -> None:
    with pytest.raises(ValidationError, match="String should match pattern"):
        RelatedType(name="Foo", source_file=None, declaration="class Foo")


# --- Signatures and uniqueness ---


def test_overloads_are_accepted() -> None:
    context = TargetClassContext.model_validate(
        context_data(functions=[_function("load", "String"), _function("load", "Int")])
    )

    assert [function.name for function in context.functions] == ["load", "load"]


def test_duplicate_signatures_are_rejected() -> None:
    functions = [_function("load", "String", "Int"), _function("load", "String", "Int")]

    with pytest.raises(ValidationError, match=re.escape("duplicated: load(String, Int)")):
        TargetClassContext.model_validate(context_data(functions=functions))


def test_duplicate_function_parameters_are_rejected() -> None:
    parameters = [{"name": "id", "type": "String"}, {"name": "id", "type": "Int"}]

    with pytest.raises(ValidationError, match="parameter names must be unique, duplicated: id"):
        FunctionSignature(
            name="load",
            parameters=[Parameter.model_validate(p) for p in parameters],
            return_type="Unit",
            is_suspend=False,
            returns_flow=False,
        )


@pytest.mark.parametrize(
    ("field", "items", "message"),
    [
        (
            "constructor_params",
            [{"name": "scope", "type": "A"}, {"name": "scope", "type": "B"}],
            "constructor_params names must be unique, duplicated: scope",
        ),
        (
            "state_properties",
            [{"name": "state", "type": "A", "is_flow": False}] * 2,
            "state_properties names must be unique, duplicated: state",
        ),
        (
            "related_types",
            [{"name": "com.example.A", "source_file": None, "declaration": "class A"}] * 2,
            "related_types names must be unique, duplicated: com.example.A",
        ),
        (
            "test_libraries",
            ["junit5", "mockk", "junit5"],
            "test_libraries must not contain duplicates, duplicated: junit5",
        ),
    ],
)
def test_context_lists_must_be_unique(field: str, items: list[Any], message: str) -> None:
    with pytest.raises(ValidationError, match=re.escape(message)):
        TargetClassContext.model_validate(context_data(**{field: items}))


@pytest.mark.parametrize(
    ("field", "count"),
    [("constructor_params", 21), ("functions", 51), ("state_properties", 21)],
)
def test_context_list_limits(field: str, count: int) -> None:
    items: list[Any] = {
        "constructor_params": [{"name": f"p{i}", "type": "A"} for i in range(count)],
        "functions": [_function(f"f{i}") for i in range(count)],
        "state_properties": [
            {"name": f"s{i}", "type": "A", "is_flow": False} for i in range(count)
        ],
    }[field]

    with pytest.raises(ValidationError, match="List should have at most"):
        TargetClassContext.model_validate(context_data(**{field: items}))


def test_source_code_limit() -> None:
    with pytest.raises(ValidationError, match="at most 20000 characters"):
        TargetClassContext.model_validate(context_data(source_code="x" * 20_001))


# --- CodePatchPlan: valid example ---


def test_user_view_model_plan_is_valid() -> None:
    plan = CodePatchPlan.model_validate(plan_data())

    assert plan.test_class_name == "UserViewModelTest"
    assert [case.category for case in plan.test_cases] == [
        schemas.TestCategory.HAPPY_PATH,
        schemas.TestCategory.ERROR,
        schemas.TestCategory.CANCELLATION,
        schemas.TestCategory.EDGE_CASE,
    ]


def test_plan_json_round_trip() -> None:
    plan = CodePatchPlan.model_validate(plan_data())

    restored = CodePatchPlan.model_validate_json(plan.model_dump_json())

    assert restored == plan


def test_plan_strings_are_stripped() -> None:
    case = case_data(name="  spaced name \n", given=" g ", then="\tt ", mocks=[" getUser "])
    plan = CodePatchPlan.model_validate(
        plan_data(target_class=" com.example.sandbox.UserViewModel ", test_cases=[case])
    )

    stripped = plan.test_cases[0]
    assert (stripped.name, stripped.given, stripped.then) == ("spaced name", "g", "t")
    assert stripped.mocks == ["getUser"]
    assert plan.target_class == "com.example.sandbox.UserViewModel"


@pytest.mark.parametrize("field", list(CodePatchPlan.model_fields))
def test_every_plan_field_is_required(field: str) -> None:
    data = plan_data()
    del data[field]

    with pytest.raises(ValidationError, match="Field required"):
        CodePatchPlan.model_validate(data)


@pytest.mark.parametrize("field", list(PlannedTestCase.model_fields))
def test_every_test_case_field_is_required(field: str) -> None:
    data = case_data()
    del data[field]

    with pytest.raises(ValidationError, match="Field required"):
        PlannedTestCase.model_validate(data)


# --- PlannedTestCase rules ---


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"name": "   "}, "name must not be blank"),
        ({"name": "x" * 151}, "name must be at most 150 characters, got 151"),
        ({"name": "first line\nsecond line"}, "name must be a single line"),
        ({"name": "first line\u2028second line"}, "name must be a single line"),
        ({"name": "calls repo.load()"}, "found forbidden characters '.'"),
        ({"name": "a `b` [c] <d>"}, "found forbidden characters '`[]<>'"),
        ({"name": "a; b/c: d\\e"}, "found forbidden characters ';/:\\\\'"),
        ({"function_under_test": " "}, "function_under_test must not be blank"),
        ({"function_under_test": "repo.load"}, "function_under_test must be a Kotlin identifier"),
        ({"function_under_test": "load()"}, "function_under_test must be a Kotlin identifier"),
        ({"given": ""}, "given must not be blank"),
        ({"given": "g" * 301}, "given must be at most 300 characters, got 301"),
        ({"then": " \n "}, "then must not be blank"),
        ({"then": "t" * 301}, "then must be at most 300 characters, got 301"),
        ({"mocks": ["getUser", " "]}, "mocks[1] must not be blank"),
        ({"mocks": ["getUser", "getUser "]}, "mocks must be unique, duplicated: getUser"),
        ({"category": "smoke"}, "Input should be 'happy_path', 'error'"),
    ],
)
def test_planned_test_case_rejects(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=re.escape(message)):
        PlannedTestCase.model_validate(case_data(**overrides))


def test_test_name_may_contain_spaces_and_punctuation() -> None:
    name = "load(id) emits Error, then recovers - even if 'blank'!"

    assert PlannedTestCase.model_validate(case_data(name=name)).name == name


# --- CodePatchPlan rules ---


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"kind": "refactor"}, "Input should be 'generate_tests'"),
        ({"target_class": "UserViewModel"}, "target_class must be a fully qualified class name"),
        ({"target_class": "com.example."}, "target_class must be a fully qualified class name"),
        ({"target_class": ""}, "target_class must not be blank"),
        (
            {"test_file": "core/src/main/kotlin/com/example/sandbox/UserViewModelTest.kt"},
            "'src/test/' directory",
        ),
        (
            {"test_file": "core/src/androidTest/kotlin/com/example/sandbox/UserViewModelTest.kt"},
            "androidTest",
        ),
        ({"test_file": "/abs/src/test/kotlin/UserViewModelTest.kt"}, "relative to the project"),
        (
            {"test_file": "core/src/test/kotlin/com/example/sandbox/OtherTest.kt"},
            "test_file must be named 'UserViewModelTest.kt', got 'OtherTest.kt'",
        ),
        (
            {"test_class_name": "user-view-model-Test"},
            "test_class_name must be a Kotlin identifier",
        ),
        ({"test_class_name": "UserViewModelSpec"}, "must end with 'Test'"),
        ({"test_class_name": "Test"}, "must end with 'Test' after a non-empty prefix"),
        ({"test_cases": []}, "test_cases must contain 1 to 10 items, got 0"),
        (
            {"test_cases": [case_data(name=f"case {i}") for i in range(11)]},
            "test_cases must contain 1 to 10 items, got 11",
        ),
        (
            {"test_cases": [case_data(name="Loads User"), case_data(name="loads user")]},
            "test case names must be unique ignoring case, duplicated: loads user",
        ),
        (
            {
                "test_cases": [
                    case_data(category="error"),
                    case_data(name="b", category="edge_case"),
                ]
            },
            "test_cases must include at least one 'happy_path' case",
        ),
        ({"rationale": "  "}, "rationale must not be blank"),
        ({"rationale": "r" * 501}, "rationale must be at most 500 characters, got 501"),
    ],
)
def test_code_patch_plan_rejects(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=re.escape(message)):
        CodePatchPlan.model_validate(plan_data(**overrides))


def test_plan_extra_fields_are_forbidden() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CodePatchPlan.model_validate(plan_data(confidence=0.9))


# --- JSON Schema ---

_NON_PORTABLE_KEYWORDS = {
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "default",
    "format",
}


def _subschemas(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Every schema node: the root, $defs, properties, items and combinators, recursively."""
    nodes = [schema]
    children: list[dict[str, Any]] = [
        *schema.get("$defs", {}).values(),
        *schema.get("properties", {}).values(),
        *schema.get("anyOf", []),
        *schema.get("allOf", []),
        *schema.get("oneOf", []),
    ]
    if isinstance(schema.get("items"), dict):
        children.append(schema["items"])
    for child in children:
        nodes.extend(_subschemas(child))
    return nodes


def test_plan_json_schema_is_portable() -> None:
    nodes = _subschemas(CodePatchPlan.model_json_schema())

    assert len([node for node in nodes if node.get("type") == "object"]) == 2
    for node in nodes:
        assert not _NON_PORTABLE_KEYWORDS & node.keys(), node
        if node.get("type") == "object":
            assert node["additionalProperties"] is False, node.get("title")
            assert sorted(node["required"]) == sorted(node["properties"]), node.get("title")


@pytest.mark.parametrize(
    "model",
    [
        Parameter,
        FunctionSignature,
        PropertySignature,
        RelatedType,
        TargetClassContext,
        PlannedTestCase,
        CodePatchPlan,
    ],
)
def test_models_document_every_field(model: type[BaseModel]) -> None:
    schema = model.model_json_schema()

    assert schema.get("description")
    assert all(field.description for field in model.model_fields.values())


@pytest.mark.parametrize("enum", [ModuleKind, ClassKind, schemas.TestLibrary, schemas.TestCategory])
def test_enums_have_schema_description(enum: type) -> None:
    assert TypeAdapter(enum).json_schema().get("description")
