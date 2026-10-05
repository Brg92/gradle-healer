import importlib


def test_package_imports() -> None:
    for name in ("core", "tools", "graph", "prompts"):
        importlib.import_module(f"gradle_healer.{name}")
