# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

### Known limitations

- Linux only; macOS and Windows are planned for Sprint 1.5.
- Failed tests are read from Gradle's console output, so their details need
  `testLogging { exceptionFormat = FULL }`; JUnit XML reports, which most
  Android projects rely on, are not parsed yet.
- Java compiler errors are reported as `compile_error` without individual
  errors.
- No end-to-end agent and no command-line interface yet.

[Unreleased]: https://github.com/Brg92/gradle-healer/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Brg92/gradle-healer/releases/tag/v0.1.0
