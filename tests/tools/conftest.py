import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.tools.sandbox import SANDBOX, assert_no_process_mentions


# --- Sandbox project for integration tests ---
@pytest.fixture(scope="session")
def sandbox_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    template = tmp_path_factory.mktemp("sandbox-template") / "sandbox-jvm"
    shutil.copytree(SANDBOX, template, ignore=shutil.ignore_patterns("build", ".gradle", ".kotlin"))
    return template


@pytest.fixture
async def sandbox_project(sandbox_template: Path, tmp_path: Path) -> AsyncIterator[Path]:
    project = tmp_path / "sandbox-jvm"
    shutil.copytree(sandbox_template, project)
    yield project
    await assert_no_process_mentions(project)
