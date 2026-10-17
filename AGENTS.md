# GradleHealer

Autonomous Python agent built on LangGraph. It analyzes legacy Kotlin/Android code, generates unit tests (MockK, kotlinx-coroutines-test, Turbine) and performs refactorings with self-healing verified through Gradle.

## Core principle

**Generation is probabilistic, verification is deterministic.**
The agent does not infer the outcome of a build: it delegates it to the real compiler and Gradle. Every LLM output remains a hypothesis until `gradlew` (`gradlew.bat` on Windows) returns exit code 0.

## Stack

- **Runtime**: Python 3.12+, asynchronous (`asyncio`). Subprocesses use the low-level `loop.subprocess_exec` with a `SubprocessProtocol`, not `asyncio.create_subprocess_exec`: `Process.wait()` resolves only after every pipe has closed, so a descendant that inherited the pipes (e.g. a Kotlin daemon) would hang it although the child has exited. The protocol reports exit and pipe EOF separately.
- **Agent**: LangGraph (`StateGraph`, conditional edges, reflection loop, checkpointing), from Sprint 2
- **Contracts**: Pydantic V2 for typed payloads and validated structured output
- **Target**: multi-module Android/Kotlin projects with Gradle (Kotlin DSL)
- **Observability**: Langfuse (per-node tracing, token usage, pass@k), from Sprint 4
- **Quality**: `uv`, Ruff (lint + format), mypy strict, pytest + pytest-asyncio, pre-commit, GitHub Actions
- **Platform**: Linux, macOS and Windows, tested in CI (POSIX process groups via `os.killpg` on Linux and macOS; Windows Job Objects via `ctypes` with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`).

## Structure

```text
gradle-healer/
├── AGENTS.md
├── CLAUDE.md                # imports AGENTS.md
├── README.md                # user-facing
├── CONTRIBUTING.md          # development setup, branching and releases
├── CHANGELOG.md             # Keep a Changelog
├── LICENSE                  # Apache-2.0
├── pyproject.toml
├── .pre-commit-config.yaml
├── .github/workflows/
│   ├── ci.yml               # 3-OS checks on PRs and main; integration on non-draft PRs and main; ci-ok gate
│   └── record-logs.yml      # on-demand 3-OS Gradle log fixture recorder
├── src/gradle_healer/
│   ├── core/
│   │   ├── schemas.py       # BuildResult, TargetClassContext, CodePatchPlan
│   │   ├── validation.py    # CodePatchPlan vs TargetClassContext cross-checks
│   │   ├── state.py         # (Sprint 2) AgentState
│   │   └── config.py        # (Sprint 2)
│   ├── tools/
│   │   ├── process_control.py # ProcessController: POSIX process groups / Windows Job Objects
│   │   ├── gradle_runner.py # async process runner -> RunOutcome
│   │   ├── log_parser.py    # RunOutcome -> BuildResult (pure)
│   │   ├── build_check.py   # runner + parser glue
│   │   └── git_helpers.py   # (Sprint 3) targeted rollback
│   ├── graph/               # (Sprint 2) nodes, routing, graph builder
│   ├── prompts/             # (Sprint 2) versioned prompt templates
│   └── cli.py               # (Sprint 4) Typer entry point
├── tests/                   # offline unit tests (fakes/fixtures)
│   ├── core/
│   ├── tools/               # sandbox.py, fake_process.py, log_recording.py
│   └── fixtures/logs/       # recorded Gradle stdout/stderr per OS ({linux,macos,windows}/)
└── evals/
    ├── fixtures/sandbox-jvm/ # Kotlin/JVM Gradle project, green by default
    ├── fixtures/scenarios/   # test files that break the sandbox on purpose
    └── run_benchmark.py      # (Sprint 4)
```

## Graph

```mermaid
flowchart TD
    Ingestion[Ingestion / Target Scanner] --> Analyzer
    Analyzer --> Planner
    Planner --> PatchGenerator
    PatchGenerator --> Verifier[Verifier: Gradle Runner]
    Verifier -->|success| Report
    Verifier -->|compile_error / test_failure, retries left| Reflector
    Reflector --> PatchGenerator
    Verifier -->|timeout / infrastructure, or retries exhausted| Fallback[Fallback: git rollback + report]
```

1. **Ingestion**: locates the target class, the Gradle module it belongs to and its source sets.
2. **Analyzer**: extracts signatures, constructors, injected dependencies, coroutines/Flows and produces the minimal semantic context as a `TargetClassContext`. The parser is tree-sitter-kotlin (Sprint 2); `TargetClassContext` is parser-agnostic and describes the class, not the tool that extracted it.
3. **Planner**: produces a `CodePatchPlan` as LLM structured output. Its JSON Schema must stay portable across structured-output APIs: native types only, no `Field` constraints, no defaults (every field required); every rule lives in a validator and is stated in the field description. Before any code generation, `core/validation.py` cross-checks the plan against the `TargetClassContext` and returns `PlanIssue`s the Planner can be retried with.
4. **PatchGenerator**: generates the Kotlin code and writes whole test files into the module's test source set, resolved from the module configuration rather than hardcoded (`src/test/kotlin` or `src/test/java`). It never overwrites an existing test file (`PlanIssueCode.WOULD_OVERWRITE_EXISTING_TESTS`).
5. **Verifier**: runs `gradlew <task> --console=plain --no-daemon` (`gradlew.bat` on Windows) plus optional extra args (e.g. `--stacktrace`) in an async subprocess. `gradle_runner` returns a raw `RunOutcome` (exit code, timeout, sanitized stdout/stderr); `log_parser` turns it into a `BuildResult`; `build_check.check_build` glues the two.
   - Android module: `:<module>:testDebugUnitTest`
   - Pure JVM module: `:<module>:test`
   - The task must therefore be parameterized, not hardcoded.
6. **Routing**: on `BuildResult.failure_kind`, never on the raw exit code (compile errors and test failures both exit with 1).
   - success → Report
   - `compile_error` / `test_failure` with retries left → Reflector
   - `timeout` / `infrastructure`, or retries exhausted → Fallback

   The `retries` counter lives in `AgentState` and is incremented by the Reflector.
7. **Reflector**: isolates the salient error, prunes build noise and produces targeted feedback for the PatchGenerator, including the previous diff.

## Engineering rules

1. **Typed contracts.** Every hand-off between nodes uses Pydantic models defined in `core/schemas.py`. No untyped dicts in the state, and LLM output is always validated against a schema.
2. **LLM / tool separation.** LLM nodes produce plans and patches; deterministic tools (Gradle, filesystem, git) execute and verify. No tool invokes the LLM.
3. **Context budget.** Never pass whole files or raw Gradle logs to the LLM. The log parser isolates only the salient error:
   - Kotlin compiler error `e: file:///…:line:col`
   - first exception
   - failed assertion

   It also truncates stack traces to the frames of the target package.
4. **Robust subprocesses.** Every Gradle invocation has:
   - an explicit timeout;
   - a process-tree kill on stall (POSIX process group / Windows Job Object);
   - stdout and stderr captured separately;
   - stripping of ANSI codes and progress bars.
5. **Circuit breaker and destructive operations.** Beyond `MAX_RETRIES`, the fallback runs `git checkout` / `git clean` only on the files tracked in the state as modified by the agent. It never operates on paths outside that perimeter.
6. **Test isolation.**
   - `tests/` runs offline, with fakes and fixtures: no real LLM or Gradle.
   - Tests that run real Gradle are marked `@pytest.mark.integration` and excluded from the default run.
   - End-to-end benchmarks live in `evals/`.
7. **Style.**
   - Complete type hints.
   - `async` wherever there is I/O.
   - Pydantic models or dataclasses instead of tuples.
   - No `print`: use `logging`.
8. **Workaround policy.** Targeted handling of a specific, expected exception (e.g. `contextlib.suppress(ProcessLookupError)`) is not a workaround. The following need explicit approval before they reach a commit, plus a comment explaining why:
   - `# type: ignore`, `# noqa`, `# pragma`;
   - broad `except` / `suppress` (`Exception`, `BaseException`, bare `except`);
   - suppressed warnings;
   - sleep-based synchronization;
   - configuration changes made only to silence a tool.

   Never bypass hooks with `--no-verify`. Approved so far:
   - `# type: ignore[misc]` in `tests/core/test_schemas.py` (runtime check that a frozen model rejects assignment);
   - `monkeypatch.setattr(loop, "subprocess_exec", ...)` in `test_cancellation_during_startup_still_kills_tree`, `tests/tools/test_gradle_runner.py` (delays `subprocess_exec` after the child spawns to deterministically land a cancellation in the startup window without sleep-based synchronization).

## Cross-platform rules

- Use `pathlib`; never build paths with string concatenation or hard-coded `/`.
- Never invoke a shell (`sh -c`, `cmd /c`) from product code; pass argument lists.
- Store relative paths POSIX-style in contracts (`SourceLocation`, plans).
- Write files with explicit `encoding="utf-8"` and `newline="\n"` unless the target requires CRLF.
- New OS-touching code (git, file writes, CLI) goes behind an abstraction like `ProcessController` and is covered by the 3-OS CI matrix.
- Tests use `tests/tools/fake_process.py` and `psutil`, never shell tools; platform-specific tests use `skipif` with a reason only when the mechanism does not exist on that OS.
- Synthetic paths for another OS in tests use `PurePosixPath` / `PureWindowsPath`, and host-filesystem calls such as `os.path.realpath` run only on the host's own paths.
- After every push, check the CI run of that commit.

## Commands

```bash
uv sync                          # dependencies from the lockfile
uv run ruff check . --fix        # lint
uv run ruff format .             # format
uv run mypy src tests            # strict type check
uv run pytest                    # unit tests (offline)
uv run pytest -m integration     # tests with real Gradle (JDK 17)
GRADLE_HEALER_RECORD_LOGS=1 uv run pytest -m integration  # re-record tests/fixtures/logs
uv run pre-commit install        # install the git hooks
uv run pre-commit run --all-files
```

## Roadmap

- [x] **Sprint 1: CLI Tooling & Schemas.**
  - [x] Repo setup (`uv`, Ruff, mypy).
  - [x] `tools/gradle_runner.py`: timeout, process-group kill, stream sanitization.
  - [x] `tools/log_parser.py` + `tools/build_check.py`: salient-error parser into `BuildResult`.
  - [x] `core/schemas.py`: `TargetClassContext`, `CodePatchPlan`, `BuildResult`, plus `core/validation.py`.
  - [x] CI (GitHub Actions) and pre-commit.
  - [x] README.
- [x] **Sprint 1.5: Cross-platform (macOS, Windows).**
  - [x] `tools/process_control.py`: `ProcessController` abstraction (POSIX process groups via `os.killpg` / Windows Job Objects via `ctypes` with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`).
  - [x] `gradlew` vs `gradlew.bat` resolution and `extra_args` validation in `tools/gradle_runner.py`.
  - [x] Windows and macOS paths in `tools/log_parser.py` (drive letters, backslashes, `file:///C:/`, case-insensitive root stripping).
  - [x] Portable tests (`tests/tools/fake_process.py` and `psutil`, no `sh` / `pgrep`).
  - [x] 3-OS CI matrix (`ubuntu-latest`, `macos-latest`, `windows-latest`), gated by `ci-ok`.
  - [x] CI triggers: `push` on `main` plus `pull_request` (unit checks on all PRs; integration on non-draft PRs and `main`).
  - [x] Per-OS Gradle log fixtures (`tests/fixtures/logs/{linux,macos,windows}/`) and `.github/workflows/record-logs.yml`.
- [ ] **Sprint 2: LangGraph Core.**
  - `AgentState`.
  - Nodes `analyzer_node` (tree-sitter-kotlin), `planner_node`, `coder_node`, `verifier_node`.
  - Linear pipeline: Ingestion → Analyzer → Planner → Coder → Verifier.
- [ ] **Sprint 3: Reflection & Self-Healing.**
  - `reflector_node` with a prompt specialized in Kotlin/MockK errors.
  - Conditional routing on `failure_kind` with `MAX_RETRIES`.
  - `fallback_node` with targeted git rollback.
- [ ] **Sprint 4: Evals, Tracing & Packaging.**
  - Langfuse on every node.
  - `evals/` + `run_benchmark.py`: pass@k, latency and cost per node.
  - Typer CLI.
  - README with Mermaid diagrams.

**Current sprint: 2**

## Working mode

- Proceed in small, verifiable increments: implement, run ruff + mypy + pytest and fix before moving on.
- Ask for confirmation before adding a dependency or changing a contract in `core/schemas.py`.
- If a detail about a Gradle module, the JVM toolchain or the target project configuration is ambiguous, ask specific questions instead of assuming.
- Propose ticking off a deliverable only with ruff, mypy and pytest green. Advance "Current sprint" only after my explicit confirmation.
- Work on the current sprint's branch (`sprintN`, e.g. `sprint1.5`); out-of-sprint PRs use short-lived `docs/...` or `fix/...` branches. Open the PR as a draft with the branch's first push and mark it ready for review only when the work is done. Do not commit or push to `main`: it is reached only through PRs, merged with a merge commit until every roadmap sprint is done and GradleHealer is complete, tested and usable, and squash-merged from then on, deleting the head branch on merge. Branching, versioning and release conventions are in `CONTRIBUTING.md`.
- Do not add progress logs, session notes or TODOs to this file: history lives in the commits.
- Never add AI-tool attribution to commits, pull requests or comments (no Co-Authored-By, no session trailers, no "Generated with ..." lines).
