# Contributing to GradleHealer

Rules for coding agents live in [AGENTS.md](AGENTS.md); this guide covers the
same conventions for people.

## Development setup

- Python 3.12 and [uv](https://docs.astral.sh/uv/).
- JDK 17, only for the integration tests (they build the bundled sandbox
  project with real Gradle).
- Linux. macOS and Windows are not supported yet.

```bash
uv sync                      # install dependencies from the lockfile
uv run pre-commit install    # install the git hooks
```

## Commands

```bash
uv run ruff check . --fix                                  # lint
uv run ruff format .                                       # format
uv run mypy src tests                                      # strict type check
uv run pytest                                              # offline unit tests
uv run pytest -m integration                               # real Gradle on the sandbox (JDK 17)
GRADLE_HEALER_RECORD_LOGS=1 uv run pytest -m integration   # re-record tests/fixtures/logs
uv run pre-commit run --all-files                          # every hook once
```

Unit tests run offline, with fakes and recorded fixtures: no real LLM and no
real Gradle. Tests that run Gradle are marked `@pytest.mark.integration` and are
excluded from the default run. A cold sandbox build takes about three minutes.

## Project layout

```text
gradle-healer/
├── AGENTS.md                 # rules for coding agents (CLAUDE.md imports it)
├── CHANGELOG.md
├── CONTRIBUTING.md
├── pyproject.toml
├── .pre-commit-config.yaml
├── .github/workflows/ci.yml  # checks on every push; integration tests on PRs to main
├── src/gradle_healer/
│   ├── core/                 # schemas.py, validation.py
│   ├── tools/                # gradle_runner.py, log_parser.py, build_check.py
│   ├── graph/                # Sprint 2
│   └── prompts/              # Sprint 2
├── tests/
│   ├── core/                 # schema and validation tests
│   ├── tools/                # runner, parser, build_check; sandbox helpers
│   └── fixtures/logs/        # recorded Gradle stdout/stderr per scenario
└── evals/fixtures/
    ├── sandbox-jvm/          # Kotlin/JVM Gradle project, green by default
    └── scenarios/            # test files that break it on purpose
```

## Branching and releases

- `main` is always green. Changes reach it only through pull requests with
  passing CI, never through rebase merges:
  - until every sprint on the roadmap is done and GradleHealer is complete,
    tested and usable, merge with a merge commit, so that every commit of the
    branch stays in the history;
  - from then on, squash-merge, so that each pull request is a single commit
    on `main`.
- Delete the head branch when the pull request is merged.
- Each sprint has its own branch, `sprintN` (for example `sprint1.5`). Work
  outside a sprint goes on short-lived `docs/...` or `fix/...` branches.
- Open the pull request as a draft with the first push of the branch, so that
  CI runs on every push, and mark it ready for review only when the work is
  done.
- Versions follow [Semantic Versioning](https://semver.org/), staying on 0.x
  while the API is unstable. Each sprint close is tagged `vX.Y.Z` on `main` and
  published as a GitHub Release with notes from [CHANGELOG.md](CHANGELOG.md).
- Hotfix: branch `fix/...` from `main`, open a pull request, and tag a patch
  release after the merge.

## Commits

- Everything in the repository (code, comments, docs, commit messages) is in
  English.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/).
- Never bypass the hooks with `--no-verify`.

## Licensing

Contributions are licensed under the [Apache License 2.0](LICENSE), the same
license as the project (inbound = outbound).

## Security

Do not report vulnerabilities in public issues; see [SECURITY.md](SECURITY.md).

## Workaround policy

Targeted handling of a specific, expected exception (for example
`contextlib.suppress(ProcessLookupError)` around `os.killpg`) is normal code.
The following need explicit approval and a comment explaining why:

- `# type: ignore`, `# noqa`, `# pragma`;
- broad `except` or `suppress` (`Exception`, `BaseException`, bare `except`);
- suppressed warnings;
- sleep-based synchronization;
- configuration changes made only to silence a tool.

Approved so far:

| Location | Workaround | Reason |
|---|---|---|
| `tests/core/test_schemas.py` | `# type: ignore[misc]` | Asserts that a frozen model rejects assignment at runtime. |
| `tests/tools/sandbox.py` (`poll_until`) | `# noqa: ASYNC110` + bounded polling | Nothing signals when init reaps an orphaned process, so there is no event to wait on. |
