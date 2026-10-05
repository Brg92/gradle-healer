# GradleHealer

Agente autonomo in Python basato su LangGraph. Analizza codice legacy Kotlin/Android, genera test unitari (MockK, kotlinx-coroutines-test, Turbine) ed esegue refactoring con self-healing verificato tramite Gradle.

## Principio fondamentale

**La generazione è probabilistica, la verifica è deterministica.**
L'agente non deduce l'esito della compilazione: lo delega al compilatore e a Gradle reali. Ogni output dell'LLM resta un'ipotesi finché `./gradlew` non restituisce exit code 0.

## Stack

- **Runtime**: Python 3.12+, asincrono (`asyncio`, `asyncio.create_subprocess_exec`)
- **Agente**: LangGraph (`StateGraph`, edge condizionali, reflection loop, checkpointing)
- **Contratti**: Pydantic V2 per payload tipizzati e structured output validati
- **Target**: progetti Android/Kotlin multi-modulo con Gradle (Kotlin DSL)
- **Osservabilità**: Langfuse (tracing per nodo, token usage, pass@k), dallo Sprint 4
- **Qualità**: `uv`, Ruff (lint + format), mypy strict, pytest + pytest-asyncio

## Struttura

```text
gradle-healer/
├── AGENTS.md
├── CLAUDE.md                # importa AGENTS.md
├── pyproject.toml
├── src/gradle_healer/
│   ├── __init__.py
│   ├── cli.py               # entry point Typer (Sprint 4)
│   ├── core/                # schemas.py, state.py, config.py
│   ├── tools/               # gradle_runner.py, log_parser.py, git_helpers.py
│   ├── graph/               # nodi, routing, builder del grafo
│   └── prompts/             # template dei prompt, versionati
├── tests/                   # unit test offline (fake/fixture)
└── evals/                   # progetti Kotlin fixture con bug intenzionali + benchmark
```

## Grafo

```mermaid
flowchart TD
    Ingestion[Ingestion / Target Scanner] --> Analyzer
    Analyzer --> Planner
    Planner --> PatchGenerator
    PatchGenerator --> Verifier[Verifier: Gradle Runner]
    Verifier -->|exit == 0| Report
    Verifier -->|exit != 0 && retries < MAX_RETRIES| Reflector
    Reflector --> PatchGenerator
    Verifier -->|exit != 0 && retries >= MAX_RETRIES| Fallback[Fallback: rollback git + report]
```

1. **Ingestion**: localizza la classe target, il modulo Gradle di appartenenza e i source set.
2. **Analyzer**: estrae firme, costruttori, dipendenze iniettate, coroutine/Flow e produce il contesto semantico minimo.
3. **Planner**: produce un piano strutturato (schema Pydantic) con i casi di test o i refactoring da applicare.
4. **PatchGenerator**: genera il codice Kotlin e lo scrive nel source set di test del modulo, risolto dalla configurazione del modulo e non hardcoded (`src/test/kotlin` o `src/test/java`).
5. **Verifier**: lancia in subprocess asincrono il task di test del modulo con `--no-daemon --stacktrace` e cattura stdout, stderr ed exit code.
   - Modulo Android: `:<module>:testDebugUnitTest`
   - Modulo JVM puro: `:<module>:test`
   - Il task va quindi parametrizzato e non hardcoded.
6. **Routing**: il contatore `retries` vive in `AgentState` e viene incrementato dal Reflector.
7. **Reflector**: isola l'errore saliente, pota il rumore di build e produce un feedback mirato per il PatchGenerator, includendo il diff precedente.

## Regole di ingegneria

1. **Contratti tipizzati.** Ogni passaggio tra nodi usa modelli Pydantic definiti in `core/schemas.py`. Non si usano dict non tipizzati nello stato, e l'output dell'LLM viene sempre validato contro uno schema.
2. **Separazione LLM / tool.** I nodi LLM producono piani e patch; i tool deterministici (Gradle, filesystem, git) eseguono e verificano. Nessun tool invoca l'LLM.
3. **Budget di contesto.** Mai passare all'LLM file interi o log Gradle grezzi. Il log parser isola solo l'errore saliente:
   - errore del compilatore Kotlin `e: file:///…:line:col`
   - prima eccezione
   - asserzione fallita

   Tronca inoltre gli stacktrace ai frame del package target.
4. **Subprocess robusti.** Ogni invocazione di Gradle ha:
   - un timeout esplicito;
   - kill del process group in caso di stallo;
   - stdout e stderr catturati separatamente;
   - stripping dei codici ANSI e delle progress bar.
5. **Circuit breaker e operazioni distruttive.** Oltre `MAX_RETRIES`, il fallback esegue `git checkout` / `git clean` solo sui file tracciati nello stato come modificati dall'agente. Non opera mai su path esterni a quel perimetro.
6. **Isolamento dei test.**
   - `tests/` gira offline, con fake e fixture: niente LLM o Gradle reali.
   - I test che eseguono Gradle reale, compreso `gradle_runner.py` nello Sprint 1, sono marcati `@pytest.mark.integration` ed esclusi dal run di default.
   - I benchmark end-to-end stanno in `evals/`.
7. **Stile.**
   - Type hints completi.
   - `async` dove c'è I/O.
   - Modelli Pydantic o dataclass al posto di tuple.
   - Niente `print`: usare `logging`.

## Comandi

```bash
uv sync                          # dipendenze da lockfile
uv run ruff check . --fix        # lint
uv run ruff format .             # format
uv run mypy src                  # type check strict
uv run pytest                    # unit test (offline)
uv run pytest -m integration     # test con Gradle reale
```

## Roadmap

- [ ] **Sprint 1: Tooling CLI & Schemi.**
  - Setup repo (`uv`, Ruff, mypy).
  - `tools/gradle_runner.py`: timeout, sanitizzazione degli stream, parser dell'errore saliente.
  - `core/schemas.py`: `TargetClassContext`, `CodePatchPlan`, `BuildResult`.
- [ ] **Sprint 2: LangGraph Core.**
  - `AgentState`.
  - Nodi `analyzer_node`, `planner_node`, `coder_node`, `verifier_node`.
  - Pipeline lineare: Ingestion → Analyzer → Planner → Coder → Verifier.
- [ ] **Sprint 3: Reflection & Self-Healing.**
  - `reflector_node` con prompt specializzato su errori Kotlin/MockK.
  - Routing condizionale con `MAX_RETRIES`.
  - `fallback_node` con rollback git mirato.
- [ ] **Sprint 4: Evals, Tracing & Packaging.**
  - Langfuse su ogni nodo.
  - `evals/` + `run_benchmark.py`: pass@k, latenza e costo per nodo.
  - CLI Typer.
  - README con diagrammi Mermaid.

**Sprint corrente: 1**

## Modalità di lavoro

- Procedi in piccoli incrementi verificabili: implementa, esegui ruff + mypy + pytest e correggi prima di andare avanti.
- Chiedi conferma prima di aggiungere una dipendenza o modificare un contratto in `core/schemas.py`.
- Se un dettaglio su modulo Gradle, toolchain JVM o configurazione del progetto target è ambiguo, fai domande puntuali invece di assumere.
- Proponi di spuntare un deliverable solo con ruff, mypy e pytest verdi. Avanza "Sprint corrente" solo dopo la mia conferma esplicita.
- Lavora sul branch dello sprint corrente (`sprintN`). Non committare né fare push su `main`: ci si arriva solo tramite PR a fine sprint.
- Non aggiungere a questo file log di avanzamento, note di sessione o TODO: lo storico sta nei commit.
