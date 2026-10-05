# Failure scenarios for `sandbox-jvm`

Each scenario is a single Kotlin test file kept outside the module's source set,
so the default sandbox build stays green. To reproduce one, apply it to a
**temporary copy** of the sandbox (never to `evals/fixtures/sandbox-jvm/` itself):

```bash
tmp=$(mktemp -d)
tar -C evals/fixtures/sandbox-jvm --exclude=build --exclude=.gradle --exclude=.kotlin -cf - . | tar -C "$tmp" -xf -
cp evals/fixtures/scenarios/<scenario>/*.kt "$tmp/core/src/test/kotlin/com/example/sandbox/"
(cd "$tmp" && ./gradlew :core:test --no-daemon --stacktrace)
```

Target path for every scenario:
`core/src/test/kotlin/com/example/sandbox/`

Gradle exits with **1** in both cases. Tell them apart by the failed task and
the error marker, not by the exit code.

| Scenario | File | Failed task | Expected output |
|---|---|---|---|
| `assertion_failure` | `GetUserUseCaseFailingTest.kt` | `> Task :core:test FAILED` | `org.opentest4j.AssertionFailedError: expected: <Grace> but was: <Ada>`, first target frame `com.example.sandbox.GetUserUseCaseFailingTest…(GetUserUseCaseFailingTest.kt:20)` |
| `compile_error` | `BrokenTest.kt` | `> Task :core:compileTestKotlin FAILED` | `e: file:///…/BrokenTest.kt:11:26 Unresolved reference 'UserService'.` (plus a follow-up type-inference error on the same line and `18:36 Argument type mismatch: actual type is 'Int', but 'String' was expected.`) |

## Note on test output

The sandbox configures `testLogging { exceptionFormat = FULL }`, so the
assertion message and stack trace appear on the console. A real target project
may not do this: there the console only reports `N tests completed, M failed`,
and the details live in `build/test-results/test/*.xml`.
