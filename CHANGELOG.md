# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-10-17

Cross-platform support for macOS and Windows alongside Linux (Sprint 1.5).

### Added

- Native macOS and Windows support alongside Linux.
- `tools/process_control.py`: `ProcessController` abstraction with POSIX process
  groups (`os.killpg`) on Linux and macOS and Windows Job Objects
  (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) on Windows.
- Parsing of Windows and macOS paths in compiler output (`tools/log_parser.py`:
  drive letters, backslashes, `file:///C:/...` URIs and case-insensitive
  project-root stripping).
- Per-OS recorded Gradle log fixtures under
  `tests/fixtures/logs/{linux,macos,windows}/` and the
  `.github/workflows/record-logs.yml` workflow to re-record them across all
  three platforms.

### Changed

- `run_gradle` resolves `gradlew.bat` on Windows (`gradlew` on Linux and macOS)
  and validates `extra_args` against blank or newline-containing entries.
- The missing-wrapper infrastructure error names the platform wrapper (`gradlew`
  on Linux and macOS, `gradlew.bat` on Windows).
- CI runs on Linux, macOS and Windows (`ubuntu-latest`, `macos-latest`,
  `windows-latest`), with `ci-ok` as the single required check.
- CI triggers run on pull requests targeting `main` and pushes to `main` (unit
  checks on all pull requests; integration tests on non-draft pull requests and
  `main`).
- README tagline clarifies that the agent is still in development.

### Fixed

- A cancellation during subprocess startup could orphan a grandchild; the
  process tree is now terminated before re-raising `CancelledError`.
- `PermissionError` (`EPERM`) from `os.killpg` on zombie-only process groups on
  macOS is now suppressed during cleanup.

## [0.1.0] - 2026-10-16

First release: the deterministic tooling of the Verifier and the data
contracts the agent will be built on (Sprint 1).

### Added

- Data contracts in `core/schemas.py`: `BuildResult` with typed compilation and
  test errors, `TargetClassContext` and `CodePatchPlan`, plus
  `core/validation.py` to cross-check a plan against its target class.
- `tools/gradle_runner.py`: async Gradle runner with an explicit timeout,
  process-group kill on stall, separate stdout/stderr capture and stripping of
  ANSI codes and progress bars.
- `tools/log_parser.py`: turns a raw run into a `BuildResult`, extracting
  Kotlin compiler errors, failed tests and the stack frames of the target
  package.
- `tools/build_check.py`: `check_build`, which runs a task and parses its output
  in one call.
- `evals/fixtures/sandbox-jvm`, a Kotlin/JVM Gradle project, with
  `compile_error` and `assertion_failure` scenarios.
- Recorded Gradle logs in `tests/fixtures/logs` for offline parser tests.
- GitHub Actions CI: lint, format, type check and unit tests on every push;
  integration tests with real Gradle on pull requests to `main`.
- pre-commit hooks for Ruff, mypy and basic file hygiene.
- README, CONTRIBUTING, CHANGELOG and LICENSE.
- Apache-2.0 license.
- Security policy (SECURITY.md).

### Known limitations

- Linux only; macOS and Windows are planned for Sprint 1.5.
- Failed tests are read from Gradle's console output, so their details need
  `testLogging { exceptionFormat = FULL }`; JUnit XML reports, which most
  Android projects rely on, are not parsed yet.
- Java compiler errors are reported as `compile_error` without individual
  errors.
- No end-to-end agent and no command-line interface yet.

[Unreleased]: https://github.com/Brg92/gradle-healer/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Brg92/gradle-healer/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Brg92/gradle-healer/releases/tag/v0.1.0
