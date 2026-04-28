# Symphony Implementation (`river_gang`) — Core + HTTP + linear_graphql

## Overview
Реализация Symphony Service Specification v1 (см. `SPED.md`) на Python 3.11+ под именем пакета `river_gang`. Long-running daemon: poll Linear → claim issue → создаёт изолированный per-issue workspace → запускает Codex app-server в нём → стримит события → ретраит/реконсилит. Цель iteration: Core Conformance (§18.1) + два OPTIONAL extensions: HTTP server (§13.7) и `linear_graphql` client-side tool (§10.5). Out of scope этой итерации: SSH worker (Appendix A), персистентность retry queue.

## Context (from discovery)
- Greenfield. В `/Users/artyomtakachou/projects/sympthony/` только `SPED.md`. Кода нет, git нет.
- Spec самодостаточный, 2169 строк, нормативный (RFC 2119).
- Внешние зависимости рантайма: Linear GraphQL API, локальная FS, `codex app-server` бинарь, env-creds (`LINEAR_API_KEY`).
- Dispatcher state — одна in-memory authority (§7), single-writer modification.

## Development Approach
- **testing approach**: **TDD** — для каждой публичной функции сначала пишется failing-test, потом реализация до green. Внешние зависимости (Linear HTTP, Codex stdio, FS) изолируются через тонкие interfaces, тесты используют fakes.
- complete each task fully before moving to the next
- make small, focused changes
- **CRITICAL: every task MUST include new/updated tests** for code changes in that task
- **CRITICAL: all tests must pass before starting next task** — no exceptions
- **CRITICAL: update this plan file when scope changes during implementation**
- run `uv run pytest` + `uv run mypy src/river_gang` + `uv run ruff check src tests` after each change
- maintain backward compatibility внутри itself (config schema not yet released)

### Stack Decisions (fixed before planning)
- Runtime: Python 3.11+, asyncio single event loop owns orchestrator state
- HTTP: FastAPI + uvicorn (uvicorn programmatic, not CLI)
- Linear: httpx + raw GraphQL strings (per spec §11.2 keep query construction isolated)
- Codex transport: `asyncio.create_subprocess_exec` через `bash -lc <codex.command>`, stdio JSON-line
- Approval/sandbox: `approval_policy=never` + `turn_sandbox_policy=workspace-write` (auto-approve, sandbox блокирует запись/сеть вне workspace на уровне OS)
- Build: `uv` + `pyproject.toml`
- Quality: `pytest`, `pytest-asyncio`, `ruff`, `mypy --strict` для `src/river_gang`
- Template engine: `python-liquid` (active maintenance, MIT, explicit `Environment(undefined=StrictUndefined)`)
- GraphQL parsing for `linear_graphql` validation: `graphql-core` (robust operation counting, ~1MB)
- Package import name: `river_gang`. Каталог проекта остаётся `sympthony`.

### Documented Implementation-Defined Choices (spec ambiguities pinned)
Spec оставляет ряд вопросов на implementation. Здесь — наш выбор и rationale:
1. `after_create` hook failure → удаляем partially-prepared workspace dir (§9.3 `MAY`). Rationale: избегаем накопления stale dirs.
2. `monitor_handle` в `RunningEntry` (§16.4) → `None` placeholder. Rationale: spec не определяет, в текущей итерации не нужен.
3. Linear candidate state filter → server-side через `state: { name: { in: $activeStates } }`. Rationale: избегаем загрузки terminal issues через pagination.
4. `WORKFLOW.md` начинается с `---` но без закрывающего `---` → `WorkflowParseError`. Rationale: явная ошибка лучше silent fall-through.
5. `tracker.api_key` empty after $-resolution → `EffectiveConfig.tracker.api_key is None` (не пустая строка). Rationale: единственное представление "missing".
6. `recent_events` ring buffer → bounded `deque(maxlen=50)` per running entry. Rationale: достаточно для debugging UI без unlimited memory.
7. Graceful shutdown sequence — см. Task 38.
8. Codex `initialize` payload `clientInfo: {name: "river-gang", version: <pkg>}`. Rationale: совместимость с LSP-style protocol expectations.
9. No observer broadcast bus: HTTP читает `build_snapshot()` at request time; `notify_observers()` из §16 алгоритмов не имплементируется как pub/sub.

## Testing Strategy
- **unit tests**: required for every task. Mock Linear HTTP через `respx`, Codex stdio через in-memory `asyncio.StreamReader/StreamWriter` fakes, FS через `tmp_path` fixture
- **integration tests**: для orchestrator end-to-end сценариев — фейковый Linear adapter + фейковый Codex client, реальные asyncio loops и реальная FS
- **e2e**: проект CLI-only, web UI нет; UI-Playwright не требуется. Вместо e2e — `tests/e2e/` с реальным `subprocess.run([sys.executable, "-m", "river_gang", ...])` против `respx`-mocked Linear endpoint
- **conformance**: финальный sweep по §17 чек-листу spec'а с явной маппой test_id → §17.x bullet
- treat ALL test failures as blocking — no `-k`, `-x`, `xfail` без письменной TODO в plan'е

## Progress Tracking
- mark completed items with `[x]` immediately when done
- add newly discovered tasks with ➕ prefix
- document issues/blockers with ⚠️ prefix
- update plan if implementation deviates from original scope
- keep plan in sync with actual work done

## Solution Overview

```
river_gang/
├── workflow/        — WORKFLOW.md loader, parser, watcher
├── config/          — typed config view, $VAR resolution, validation
├── tracker/         — Linear adapter (httpx + raw GraphQL), normalization
├── workspace/       — sanitization, FS lifecycle, hook runner
├── prompt/          — strict template engine wrapper
├── codex/           — app-server stdio client, event parsing, token accounting
├── orchestrator/    — single-authority state machine, dispatch, retry, reconcile
├── tools/           — linear_graphql client-side tool
├── http/            — FastAPI app, /api/v1/*, dashboard
├── observability/   — structured logging, snapshot view
├── cli.py           — argparse entry
└── __main__.py
```

Все мутации `OrchestratorState` сериализуются через `asyncio.Queue` → один writer-таск → workers только посылают сообщения. Это даёт single-authority по spec §7, без локов.

## Technical Details
- **Config layer** возвращает immutable `EffectiveConfig` dataclass; reload создаёт новый snapshot, активные сессии не перезапускаются (spec §6.2)
- **Workspace key**: `re.sub(r"[^A-Za-z0-9._-]", "_", identifier)`; путь нормализуется через `Path.resolve()`, проверка `is_relative_to(root)`
- **Retry queue**: `dict[issue_id, RetryEntry]` + `asyncio.TimerHandle`; backoff `min(10000 * 2**(attempt-1), max_retry_backoff_ms)` в ms
- **Codex events**: парсятся в `CodexEvent` discriminated union; cumulative tokens читаются из `thread/tokenUsage/updated` или `total_token_usage`, дельта считается против `last_reported_*`
- **HTTP server** запускается как `uvicorn.Server` внутри того же event loop через `asyncio.create_task(server.serve())`
- **`linear_graphql` tool**: regex-валидация что в документе одна `query|mutation` operation; reuse `tracker.LinearClient.execute_raw()`

## What Goes Where
- **Implementation Steps** (`[ ]` checkboxes): код, тесты, документы внутри репозитория
- **Post-Completion** (no checkboxes): реальный smoke с настоящим Linear-токеном, ручная проверка `codex app-server` против реального бинаря, packaging/deploy

## Implementation Steps

---
### M0 — Project Foundation

#### Task 1: Initialize repo, package, deps

**Files:**
- Create: `pyproject.toml`
- Create: `src/river_gang/__init__.py`
- Create: `src/river_gang/__main__.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `.gitignore`
- Create: `README.md`

- [ ] init `git` repo, write `.gitignore` (`.venv/`, `__pycache__/`, `.pytest_cache/`, `dist/`, `build/`, `*.egg-info/`, `sandbox/`)
- [ ] write `pyproject.toml`: project metadata, `requires-python = ">=3.11"`, runtime deps (`httpx`, `pyyaml`, `python-liquid`, `graphql-core`, `fastapi`, `uvicorn`, `watchfiles`, `jinja2`), dev deps (`pytest`, `pytest-asyncio`, `respx`, `ruff`, `mypy`), `[tool.ruff]`, `[tool.mypy]` strict для `river_gang`, `[project.scripts] river-gang = "river_gang.cli:main"`
- [ ] `uv sync` — verify env builds cleanly
- [ ] write minimal `__main__.py` printing version, `cli.py` stub raising `NotImplementedError`
- [ ] write `tests/test_smoke.py`: `import river_gang` + version field present
- [ ] write `tests/conftest.py` with shared fixtures: `tmp_workspace_root`, `frozen_clock`
- [ ] add `tests/test_pyproject.py`: assert package name = `river_gang`, version present
- [ ] run `uv run pytest && uv run ruff check && uv run mypy src/river_gang` — must pass before next task

#### Task 2: Logging foundation

**Files:**
- Create: `src/river_gang/observability/__init__.py`
- Create: `src/river_gang/observability/logging.py`
- Create: `tests/observability/__init__.py`
- Create: `tests/observability/test_logging.py`

- [ ] write tests first: (a) `configure_logging()` устанавливает root level и key=value formatter; (b) `contextvars.ContextVar` для `issue_id`, `issue_identifier`, `session_id` автоматически попадают в каждый log record когда выставлены (см. §13.1); (c) sink failure не падает (см. §13.2)
- [ ] implement `logging.py` поверх stdlib `logging` с custom `Filter`/`Formatter`, читающим `contextvars`. Единственный механизм context — `contextvars.ContextVar`; без отдельных `with_*` helper'ов
- [ ] add helper `redact_secrets(value)` — не логирует `LINEAR_API_KEY` или поля содержащие `api_key`/`token` (§15.3)
- [ ] write tests for `redact_secrets`
- [ ] run tests — must pass before next task

---
### M1 — Workflow Loader and Config

#### Task 3: WORKFLOW.md parser (front matter + body)

**Files:**
- Create: `src/river_gang/workflow/__init__.py`
- Create: `src/river_gang/workflow/loader.py`
- Create: `src/river_gang/workflow/errors.py`
- Create: `tests/workflow/__init__.py`
- Create: `tests/workflow/test_loader.py`
- Create: `tests/workflow/fixtures/`

- [ ] write tests first: missing file → `MissingWorkflowFile`; invalid YAML → `WorkflowParseError`; YAML scalar (not map) → `FrontMatterNotAMap`; valid file with `---` front matter → returns `WorkflowDefinition(config=dict, prompt_template=str)`; file без `---` → весь файл = prompt body, `config={}`; trailing whitespace в body trimmed; **file starts with `---` но без закрывающего `---` → `WorkflowParseError` (НЕ fall-through к body-only)**
- [ ] implement `errors.py` с typed exception hierarchy (`MissingWorkflowFile`, `WorkflowParseError`, `FrontMatterNotAMap`)
- [ ] implement `loader.py::load_workflow(path)` — reads file, splits на `---` блок (только если первая строка ровно `---`), парсит YAML, возвращает `WorkflowDefinition` dataclass
- [ ] add fixtures: `valid_with_fm.md`, `valid_no_fm.md`, `invalid_yaml.md`, `non_map_yaml.md`
- [ ] run tests — must pass before next task

#### Task 4: Typed config view + defaults

**Files:**
- Create: `src/river_gang/config/__init__.py`
- Create: `src/river_gang/config/schema.py`
- Create: `src/river_gang/config/defaults.py`
- Create: `tests/config/__init__.py`
- Create: `tests/config/test_schema.py`
- Create: `tests/config/test_defaults.py`

- [ ] write tests first для каждого поля §6.4 cheat sheet: defaults (`tracker.endpoint=https://api.linear.app/graphql`, `polling.interval_ms=30000`, `agent.max_concurrent_agents=10`, `agent.max_turns=20`, `agent.max_retry_backoff_ms=300000`, `hooks.timeout_ms=60000`, `codex.command="codex app-server"`, `codex.turn_timeout_ms=3600000`, `codex.read_timeout_ms=5000`, `codex.stall_timeout_ms=300000`); active/terminal states defaults; per-state map normalization (`lowercase` keys, отбрасывает невалидные значения)
- [ ] implement `schema.py` — frozen dataclasses: `TrackerConfig`, `PollingConfig`, `WorkspaceConfig`, `HooksConfig`, `AgentConfig`, `CodexConfig`, `EffectiveConfig` (root)
- [ ] implement `defaults.py::apply_defaults(raw_dict) -> EffectiveConfig`
- [ ] add tests для unknown top-level keys → ignored (forward compat, §5.3)
- [ ] add tests для type coercion (string → int конверсия для numeric полей; non-numeric → coercion failure surfaced as `ConfigCoercionError`)
- [ ] note: validation invalid `agent.max_turns` / `hooks.timeout_ms` живёт в Task 6 (preflight), не здесь — Task 4 только coerce + apply defaults
- [ ] run tests — must pass before next task

#### Task 5: `$VAR` indirection + path normalization

**Files:**
- Modify: `src/river_gang/config/schema.py`
- Create: `src/river_gang/config/resolution.py`
- Create: `tests/config/test_resolution.py`

- [ ] write tests first: `tracker.api_key="$LINEAR_API_KEY"` resolves через `os.environ`; **missing var or empty string → field в `EffectiveConfig` ставится в `None` (НЕ пустую строку)** так чтобы Task 6 валидация могла проверять `is None` (§5.3.1 "treated as missing"); literal token (no `$` prefix) passed through; `~` expansion в `workspace.root`; `$WORKSPACE_HOME` в path; relative `workspace.root` resolved relative to dir containing WORKFLOW.md (§5.3.3); absolute paths unchanged; URI fields (endpoint) НЕ модифицируются (§6.1)
- [ ] implement `resolution.py::resolve_env_vars(value)` — `re.fullmatch(r"\$([A-Z_][A-Z0-9_]*)", s)` → `os.environ.get(name, "")`
- [ ] implement `resolution.py::normalize_path(value, *, base_dir)` с `~`, `$VAR`, абсолютизацией
- [ ] integrate в `apply_defaults` → `resolve_and_validate(raw, workflow_dir)` возвращает `EffectiveConfig`
- [ ] tests для типа path coercion: `tracker.endpoint` (URI) НЕ обработан normalize_path
- [ ] run tests — must pass before next task

#### Task 6: Dispatch preflight validation

**Files:**
- Create: `src/river_gang/config/validation.py`
- Create: `tests/config/test_validation.py`

- [ ] write tests first для §6.3 каждого правила: `tracker.kind` отсутствует → fail; `tracker.kind="github"` (unsupported) → fail; `tracker.api_key is None` после $-resolution → fail; `tracker.project_slug` отсутствует когда `kind=linear` → fail; `codex.command` пустая строка → fail; `tracker.endpoint` отсутствует с `kind=linear` → default применён, no error; `agent.max_turns ≤ 0` или non-int → fail (§5.3.5); `hooks.timeout_ms ≤ 0` или non-int → fail (§5.3.4); valid конфиг → ok
- [ ] implement `validation.py::ValidationResult(ok: bool, errors: list[str])` + `validate_for_dispatch(config) -> ValidationResult`
- [ ] add fmt helper `format_error_for_operator(result)`
- [ ] tests для format helper
- [ ] run tests — must pass before next task

#### Task 7: Workflow file watcher + dynamic reload

**Files:**
- Create: `src/river_gang/workflow/watcher.py`
- Create: `tests/workflow/test_watcher.py`

- [ ] write tests first: вариация WORKFLOW.md disk-write → callback called в течение ≤500ms; invalid reload не выкидывает exception, эмитит operator-visible error через logging mock; multiple rapid changes coalesced (debounce); stop() корректно останавливает watcher; **в тесте имитировать "session in flight" → reload НЕ должен restart активный worker (§6.2)**
- [ ] implement `watcher.py::WorkflowWatcher` поверх `watchfiles.awatch()` async generator; debounce 200ms; on change: `load_workflow → apply_defaults → resolve_and_validate`; на success → callback(EffectiveConfig); на failure → log error и keep last good
- [ ] add `LastKnownGoodHolder` для атомарного swap effective config
- [ ] tests для LastKnownGoodHolder thread-safety (asyncio.Lock)
- [ ] run tests — must pass before next task

---
### M2 — Linear Tracker Client

#### Task 8: Linear HTTP transport + auth

**Files:**
- Create: `src/river_gang/tracker/__init__.py`
- Create: `src/river_gang/tracker/errors.py`
- Create: `src/river_gang/tracker/linear_transport.py`
- Create: `tests/tracker/__init__.py`
- Create: `tests/tracker/test_transport.py`

- [ ] write tests first (`respx`): missing api_key → `MissingTrackerApiKey` raised на construction; happy path POST к endpoint с `Authorization: <token>` header (no `Bearer ` per Linear convention, проверить spec); non-200 → `LinearApiStatus(status, body)`; transport exception → `LinearApiRequest`; `errors[]` в response → `LinearGraphQLErrors(errors_list)`; malformed JSON → `LinearUnknownPayload`; timeout=30s применяется
- [ ] implement `errors.py` с typed exception hierarchy (§11.4 categories + `UnsupportedTrackerKind`, `MissingTrackerProjectSlug`, `LinearMissingEndCursor`)
- [ ] implement `linear_transport.py::LinearTransport(endpoint, api_key)` обёртка `httpx.AsyncClient` с `execute(query: str, variables: dict) -> dict`
- [ ] tests для retry-free режима (transport ошибки пробрасываются, retry — на верхнем уровне)
- [ ] run tests — must pass before next task

#### Task 9: Issue normalization

**Files:**
- Create: `src/river_gang/tracker/issue.py`
- Create: `tests/tracker/test_normalization.py`
- Create: `tests/tracker/fixtures/`

- [ ] write tests first: parse Linear issue payload в `Issue` dataclass per §4.1.1; labels lowercased; blockers извлекаются из `inverseRelations` где `type=blocks`; priority non-int → None; `createdAt`/`updatedAt` парсятся как `datetime` ISO-8601; missing optional поля (`description`, `branchName`, `url`, `priority`) → None; `description` populated когда present; **`parse_issue` raises `IssueMissingRequiredField` при отсутствии любого из `id`/`identifier`/`title`/`state`** (§8.2 eligibility — лучше отсеивать на parse, чем на dispatch)
- [ ] add fixture JSON files: `issue_basic.json`, `issue_with_blockers.json`, `issue_no_priority.json`, `issue_with_labels.json`
- [ ] implement `issue.py::Issue` frozen dataclass + `parse_issue(payload: dict) -> Issue`
- [ ] tests для blockers: terminal blocker filtering (extracted but state preserved per §4.1.1)
- [ ] run tests — must pass before next task

#### Task 10: Candidate fetch + pagination

**Files:**
- Create: `src/river_gang/tracker/queries.py`
- Create: `src/river_gang/tracker/client.py`
- Create: `tests/tracker/test_client_candidates.py`

- [ ] write tests first: `fetch_candidate_issues(active_states, project_slug)` возвращает все issues across pages; пейджинг через `pageInfo.endCursor` + `hasNextPage`; sortable result порядок сохраняется; query содержит `project: { slugId: { eq: $projectSlug } }`; missing `endCursor` при `hasNextPage=true` → `LinearMissingEndCursor`; page size = 50; пустой первый ответ → empty list
- [ ] implement `queries.py::CANDIDATES_QUERY` raw GraphQL string с server-side фильтром `state: { name: { in: $activeStates } }` (Linear `IssueFilter` поддерживает; избегаем загрузки terminal issues через pagination)
- [ ] implement `client.py::LinearClient(transport, project_slug)` с `fetch_candidate_issues(active_states) -> list[Issue]`
- [ ] tests с многостраничным `respx` mock (3 pages)
- [ ] run tests — must pass before next task

#### Task 11: State refresh + terminal fetch

**Files:**
- Modify: `src/river_gang/tracker/queries.py`
- Modify: `src/river_gang/tracker/client.py`
- Create: `tests/tracker/test_client_refresh.py`

- [ ] write tests first: `fetch_issue_states_by_ids([])` возвращает `[]` без HTTP-вызова (§17.3); `fetch_issue_states_by_ids(["id1","id2"])` использует variable type `[ID!]` (§11.2) → assert query contains `[ID!]`; `fetch_issues_by_states(state_names)` — для startup terminal cleanup; partial responses correctly mapped
- [ ] implement `STATE_REFRESH_QUERY` + `TERMINAL_FETCH_QUERY` raw strings
- [ ] add `client.py::fetch_issue_states_by_ids`, `fetch_issues_by_states`
- [ ] tests для error mapping consistency
- [ ] run tests — must pass before next task

---
### M3 — Workspace Manager

#### Task 12: Workspace key sanitization + path safety

**Files:**
- Create: `src/river_gang/workspace/__init__.py`
- Create: `src/river_gang/workspace/safety.py`
- Create: `tests/workspace/__init__.py`
- Create: `tests/workspace/test_safety.py`

- [ ] write tests first: `sanitize_key("ABC-123")` == `"ABC-123"`; `sanitize_key("a/b\\c d")` == `"a_b_c_d"`; `sanitize_key("")` raises; only `[A-Za-z0-9._-]` survive (§9.5 invariant 3); `validate_within_root(root, candidate)` accept paths inside root, reject `..`-traversal или absolute paths за пределами; both paths normalized via `Path.resolve(strict=False)`; symlink containing path validation
- [ ] implement `safety.py::sanitize_key(identifier)` и `validate_within_root(root, candidate)`
- [ ] tests с tricky: `../etc/passwd`, симлинки, double-dot
- [ ] run tests — must pass before next task

#### Task 13: Hook runner + timeout

**Files:**
- Create: `src/river_gang/workspace/hooks.py`
- Create: `tests/workspace/test_hooks.py`

- [ ] write tests first: `run_hook(script, cwd, timeout_ms)` возвращает `HookResult(exit_code, stdout, stderr, duration_ms)`; success exit 0 → ok; exit !=0 → ok=False; timeout → terminate process group, returns timeout result; cwd проверяется (script видит правильный pwd); empty/None script → no-op `HookResult.skipped()`; stdout/stderr усечение в логах (§15.4)
- [ ] implement `hooks.py::run_hook(script, *, cwd, timeout_ms)` через `asyncio.create_subprocess_exec("bash", "-lc", script, cwd=cwd, start_new_session=True, ...)` с `asyncio.wait_for`; на timeout — `os.killpg(os.getpgid(proc.pid), SIGTERM)`, затем (после grace ~2s) `SIGKILL` escalation; module docstring явно фиксирует POSIX-only assumption
- [ ] tests для truncation в логах: hook output 1MB → лог содержит первые N байт + `[truncated]`
- [ ] tests для process group kill при timeout (запуск `bash -lc "sleep 10"`)
- [ ] run tests — must pass before next task

#### Task 14: Workspace lifecycle (create/reuse/delete)

**Files:**
- Create: `src/river_gang/workspace/manager.py`
- Create: `tests/workspace/test_manager.py`

- [ ] write tests first: `ensure_for_issue(identifier)` создаёт dir при отсутствии, `created_now=True`; повторный вызов → `created_now=False`, та же path; `after_create` hook вызван только когда `created_now=True`; **`after_create` failure → workspace dir удалена + raise (наша implementation-defined policy per §9.3 `MAY` — задокументирована в "Implementation-Defined Choices")**; existing non-directory path at expected location → typed error (`WorkspaceNotADirectory`); `cleanup_for_issue(identifier)` удаляет dir; `before_remove` hook вызван перед удалением, failure logged but ignored; `before_remove` skipped если dir не существует
- [ ] implement `manager.py::WorkspaceManager(config, hook_runner)` с `ensure_for_issue`, `cleanup_for_issue`, `path_for_issue`
- [ ] verify §9.5 invariants enforced: sanitization + within-root + cwd на dispatch (последнее в M5)
- [ ] tests для concurrent ensure_for_issue (asyncio gather) — выживает только одна директория
- [ ] run tests — must pass before next task

---
### M4 — Prompt Rendering

#### Task 15: Strict template engine wrapper

**Files:**
- Create: `src/river_gang/prompt/__init__.py`
- Create: `src/river_gang/prompt/render.py`
- Create: `src/river_gang/prompt/errors.py`
- Create: `tests/prompt/__init__.py`
- Create: `tests/prompt/test_render.py`

- [ ] write tests first: `render(template, issue, attempt)` рендерит `{{ issue.identifier }}` `{{ issue.title }}` etc.; unknown variable → `TemplateRenderError` (strict mode); unknown filter → `TemplateRenderError`; `attempt=None` доступен в template как nil; nested labels iterable (`{% for l in issue.labels %}`); blockers iterable; empty template → fallback minimal prompt `"You are working on an issue from Linear."` (§5.4); template parse error → `TemplateParseError`
- [ ] implement `render.py::render_prompt(template, *, issue, attempt)` через `liquidpy` (или эквивалент с strict undefined=raise)
- [ ] tests с реальным WORKFLOW prompt примером
- [ ] tests для fallback: `render_prompt("", ...)` returns fallback
- [ ] run tests — must pass before next task

---
### M5 — Codex App-Server Client

#### Task 16: Subprocess launch + stdio framing

**Files:**
- Create: `src/river_gang/codex/__init__.py`
- Create: `src/river_gang/codex/process.py`
- Create: `src/river_gang/codex/errors.py`
- Create: `tests/codex/__init__.py`
- Create: `tests/codex/test_process.py`
- Create: `tests/codex/fakes.py`

- [ ] write tests first: `launch(command, cwd)` invokes `bash -lc <command>` с правильным cwd; cwd validation — passing path вне workspace_root → `InvalidWorkspaceCwd`; command не существует → `CodexNotFound`; max line size 10MB (§10.1); stdout/stderr split (stderr — diagnostic, не protocol); subprocess exit во время чтения → `PortExit`
- [ ] implement `errors.py` (§10.6 categories): `CodexNotFound`, `InvalidWorkspaceCwd`, `ResponseTimeout`, `TurnTimeout`, `PortExit`, `ResponseError`, `TurnFailed`, `TurnCancelled`, `TurnInputRequired`
- [ ] implement `process.py::CodexProcess` async context manager: launch + read JSON-line frames из stdout, ignore stderr (log under DEBUG)
- [ ] add `tests/codex/fakes.py::FakeCodexProcess` отдающий заданную последовательность frames
- [ ] tests с реальным `bash -c 'echo {"x":1}; echo {"x":2}'` для smoke
- [ ] run tests — must pass before next task

#### Task 17: App-server protocol — startup, thread, turn

**Files:**
- Create: `src/river_gang/codex/protocol.py`
- Create: `src/river_gang/codex/client.py`
- Create: `tests/codex/test_protocol.py`
- Create: `tests/codex/test_client_startup.py`

- [ ] write tests first: protocol message builders для `initialize`, `thread.start`, `turn.start` соответствуют schema полям описанным в §10.2 — cwd передан, approval_policy/sandbox supplied, prompt body передан в первом turn'е, continuation turns — только continuation guidance (no original prompt); `read_timeout_ms` enforced для startup requests (через `asyncio.wait_for` с `TimeoutError` → `ResponseTimeout`); `thread_id`/`turn_id` извлекаются из responses; `session_id = f"{thread_id}-{turn_id}"`; turn title содержит `<identifier>: <title>` если protocol поддерживает; **`initialize` payload содержит `clientInfo: {"name": "river-gang", "version": <pkg version>}`** (§17.5)
- [ ] implement `protocol.py` — message dataclasses + JSON serialization (request/response correlation через id)
- [ ] implement `client.py::CodexClient` высокоуровневая обёртка: `start_session(workspace, prompt, issue, approval_policy, sandbox_policy)` → возвращает `Session(thread_id, first_turn)`. Каждый sync request обёрнут в `asyncio.wait_for(read, timeout=read_timeout_ms/1000)` → `ResponseTimeout` на TimeoutError
- [ ] tests с FakeCodexProcess симулирующим заданный handshake
- [ ] tests для startup_failed event emission
- [ ] run tests — must pass before next task

#### Task 18: Streaming turn processing + completion conditions

**Files:**
- Modify: `src/river_gang/codex/client.py`
- Create: `tests/codex/test_client_streaming.py`

- [ ] write tests first: `stream_turn(session, prompt, on_event)` корректно классифицирует completion: success → `TurnResult.succeeded`; failure → `TurnFailed`; cancelled → `TurnCancelled`; `turn_timeout_ms` exceeded → `TurnTimeout` + worker завершён; subprocess exit during stream → `PortExit`; user-input-required → `TurnInputRequired` (per high-trust policy = failure, §10.5); каждый event эмитит callback с `RuntimeEvent(event, timestamp, codex_app_server_pid, payload, usage?)`
- [ ] implement streaming loop с `asyncio.wait_for(turn_timeout_ms)` и обработкой completion signals
- [ ] **implement `CodexClient.stop_session(session)`** — отправляет graceful shutdown по protocol, ожидает exit с timeout, escalates до `proc.terminate()` → `proc.kill()` если subprocess висит. Используется worker'ом (Task 32) на каждом exit branch (success, failure, prompt error, refresh error per §16.5)
- [ ] tests для `stop_session`: clean stop (subprocess завершается на graceful) и forced kill (subprocess игнорирует graceful → terminate → kill)
- [ ] tests для continuation: после `succeeded` worker может вызвать `stream_turn` снова на same session, app-server alive
- [ ] tests для unsupported tool call: agent requests `unknown_tool` → клиент возвращает failure в protocol-format и не валит сессию (§10.5)
- [ ] run tests — must pass before next task

#### Task 19: Token accounting + rate limits

**Files:**
- Create: `src/river_gang/codex/usage.py`
- Create: `tests/codex/test_usage.py`

- [ ] write tests first: cumulative тоталы извлекаются из `thread/tokenUsage/updated` payload (§13.5); из `total_token_usage` внутри token-count wrapper events; `last_token_usage` дельты ИГНОРИРУЮТСЯ для дашборд totals; `usage` map в generic event НЕ интерпретируется как cumulative; повторные absolute totals → дельта против `last_reported_*` корректно избегает double-counting; rate-limit payload track'ается из любого update'а
- [ ] implement `usage.py::extract_cumulative_tokens(event_payload) -> TokenSnapshot | None`
- [ ] implement `usage.py::extract_rate_limits(event_payload) -> RateLimitSnapshot | None`
- [ ] tests для каждой shape payload (3-4 фикстуры)
- [ ] run tests — must pass before next task

#### Task 20: Approval policy enforcement

**Files:**
- Create: `src/river_gang/codex/policy.py`
- Create: `tests/codex/test_policy.py`

- [ ] write tests first: при `approval_policy=never` любой incoming approval-request → auto-approve response отправлен, эмитится `approval_auto_approved` event; user-input-required → fail run; sandbox `workspace-write` указывается в startup payload; `approval_policy` и `sandbox_policy` фактически переданы в `thread.start`/`turn.start`
- [ ] implement `policy.py::ApprovalHandler` принимающий decision policy, реагирующий на protocol approval messages
- [ ] add **trust posture** docstring в `policy.py`: ссылка на §10.5/§15.1, явная декларация: `approval_policy=never`, `sandbox=workspace-write`, user-input fails immediately. Этот docstring МОЖЕТ быть единственным комментарием в файле — он документирует обязательное implementation-defined требование spec'а
- [ ] tests для switching policy через config
- [ ] run tests — must pass before next task

#### Task 21: Stall detection helper

**Files:**
- Create: `src/river_gang/codex/stall.py`
- Create: `tests/codex/test_stall.py`

- [ ] write tests first: `should_terminate_for_stall(now, last_event_at, started_at, stall_timeout_ms)` returns False если elapsed < timeout; True если >; uses `last_event_at` если set, else `started_at`; `stall_timeout_ms=0` → always False (disabled)
- [ ] implement `stall.py::should_terminate_for_stall`
- [ ] tests для edge cases (timeout exactly equal, negative)
- [ ] run tests — must pass before next task

---
### M6 — `linear_graphql` Client-Side Tool

#### Task 22: Tool input validation

**Files:**
- Create: `src/river_gang/tools/__init__.py`
- Create: `src/river_gang/tools/linear_graphql.py`
- Create: `tests/tools/__init__.py`
- Create: `tests/tools/test_linear_graphql_input.py`

- [ ] write tests first (§10.5 contract): preferred shape `{"query": str, "variables": dict}` принимается; `query` пустая → invalid input; `variables` non-object → invalid input; raw query string (shorthand) принимается; multiple operations (e.g. `query A {} mutation B {}`) → reject; single operation accepted; valid `mutation` accepted
- [ ] implement `linear_graphql.py::validate_input(raw) -> tuple[str, dict]` с `LinearGraphqlInvalidInput` exception
- [ ] add helper `count_operations(document) -> int` через `graphql.parse(document)` из `graphql-core` (см. Stack Decisions); regex намеренно избегается из-за false positives на string literals и fragments
- [ ] tests для count_operations: `query`, `mutation`, named, anonymous, fragment-only (count=0), invalid GraphQL → `LinearGraphqlInvalidInput`
- [ ] run tests — must pass before next task

#### Task 23: Tool execution against Linear transport

**Files:**
- Modify: `src/river_gang/tools/linear_graphql.py`
- Create: `tests/tools/test_linear_graphql_execute.py`

- [ ] write tests first (§10.5 result semantics): success + no errors → `success=true` + data preserved; top-level GraphQL `errors` present → `success=false` но GraphQL response body preserved; transport failure → `success=false` + error payload; missing auth → `success=false`; reuse already-configured `LinearTransport` (don't read raw token from disk)
- [ ] implement `linear_graphql.py::LinearGraphqlTool(transport)` с `async def execute(raw_input) -> ToolResult`
- [ ] add `ToolResult` dataclass с `success`, `data`, `errors`, `error_message`
- [ ] tests с `respx` для каждого случая
- [ ] run tests — must pass before next task

#### Task 24: Tool advertisement в Codex session startup

**Files:**
- Modify: `src/river_gang/codex/client.py`
- Create: `tests/codex/test_tools_advertisement.py`

- [ ] write tests first: при `start_session` если `tracker.kind=linear` → tool spec для `linear_graphql` advertised в protocol startup payload; если `kind != linear` → not advertised; agent's tool call для `linear_graphql` маршрутизируется в `LinearGraphqlTool.execute`; неизвестный tool name → failure response, session продолжается (§10.5)
- [ ] implement: hard-coded dispatch в `CodexClient` (n=1 tool, registry избыточен). На tool-call message: `if name == "linear_graphql": result = await tool.execute(input)` else failure response. Без `ToolRegistry` абстракции — добавим если появится второй tool
- [ ] tests с FakeCodexProcess симулирующим tool-call sequence
- [ ] run tests — must pass before next task

---
### M6.5 — Integration Test Fakes (prereq для M7)

#### Task 24a: Cross-cutting test fakes for orchestrator integration

**Files:**
- Create: `tests/tracker/fakes.py`
- Create: `tests/workspace/fakes.py`
- Modify: `tests/codex/fakes.py` (extends FakeCodexProcess from Task 16 to FakeCodexClient)

- [ ] write tests first для самих fakes (фикстуры assert default behaviors): `FakeTracker(candidates=[...], state_refreshes={id: state})` corretly реализует `LinearClient` interface — `fetch_candidate_issues`, `fetch_issue_states_by_ids`, `fetch_issues_by_states`; can simulate transport failure; can change candidates between calls
- [ ] implement `tests/tracker/fakes.py::FakeTracker` — in-memory `LinearClient`-shaped двойник (Protocol-based typing, no inheritance — keeps tests independent of internal client class structure)
- [ ] implement `tests/workspace/fakes.py::FakeWorkspaceManager` — track ensure/cleanup calls, configurable hook outcomes, no real FS writes
- [ ] extend `tests/codex/fakes.py::FakeCodexClient` (вдобавок к `FakeCodexProcess` из Task 16) — `start_session`, `stream_turn`, `stop_session` driven заданным scenario списком (queue of intended turn outcomes)
- [ ] each fake exposes `.calls: list[tuple[method, args]]` для assert ordering
- [ ] run tests — must pass before next task

---
### M7 — Orchestrator State Machine

#### Task 25: Runtime state dataclass

**Files:**
- Create: `src/river_gang/orchestrator/__init__.py`
- Create: `src/river_gang/orchestrator/state.py`
- Create: `tests/orchestrator/__init__.py`
- Create: `tests/orchestrator/test_state.py`

- [ ] write tests first per §4.1.8: `OrchestratorState` поля `running`, `claimed`, `retry_attempts`, `completed`, `codex_totals`, `codex_rate_limits`, `poll_interval_ms`, `max_concurrent_agents`; `RunningEntry` поля per §4.1.6 + §16.4: `worker_handle: asyncio.Task`, `monitor_handle: None` (placeholder — spec ambiguity, не используется в этой итерации, см. Implementation-Defined Choices), `identifier`, `issue`, `session_id`, token counters, `started_at`, `recent_events: deque[RuntimeEvent]` (`maxlen=50` — для `/api/v1/{identifier}.recent_events` в M9), `last_error: str | None`, `restart_count: int`; state mutations через explicit methods на классе
- [ ] implement `state.py` dataclasses
- [ ] tests для helper'ов: `available_slots()`, `count_in_state(state_name)`, `is_claimed(issue_id)`
- [ ] tests для `recent_events` ring buffer: добавление 60 events → хранится 50 последних
- [ ] run tests — must pass before next task

#### Task 26: Single-writer mailbox (asyncio.Queue)

**Files:**
- Create: `src/river_gang/orchestrator/mailbox.py`
- Create: `tests/orchestrator/test_mailbox.py`

- [ ] write tests first: `Mailbox` принимает discriminated `OrchestratorMessage` union (PollTick, WorkerExit, CodexUpdate, RetryTimerFired, ConfigReloaded, Shutdown); single writer-таск consumes сообщения и mutate state; producers (workers) только send; concurrent producers → сообщения обрабатываются в FIFO порядке относительно их send (queue contract); типы сообщений правильно discriminated после dequeue (mypy assertion)
- [ ] implement `mailbox.py::Mailbox` обёртка `asyncio.Queue` + типы сообщений
- [ ] tests с pytest-asyncio gather: 100 producers с unique seq IDs → consumer наблюдает все 100 в порядке отправки на per-producer basis
- [ ] run tests — must pass before next task

#### Task 27: Candidate filtering + sorting

**Files:**
- Create: `src/river_gang/orchestrator/dispatch.py`
- Create: `tests/orchestrator/test_dispatch_filter.py`

- [ ] write tests first per §8.2: state in active_states accepted; in terminal_states excluded; already в running excluded; в claimed excluded; `Todo` issue с non-terminal blocker excluded (§8.2 blocker rule); `Todo` issue с terminal blocker accepted; sort: priority asc (1..4), null last, then created_at oldest first, then identifier lex. (Note: missing-required-fields отсев живёт в `parse_issue` Task 9 — issue до этого слоя не доходит)
- [ ] implement `dispatch.py::filter_candidates(issues, state)`, `sort_for_dispatch(issues)`
- [ ] tests для sort stability — equal-priority equal-time → identifier tie-break
- [ ] run tests — must pass before next task

#### Task 28: Concurrency control (global + per-state)

**Files:**
- Modify: `src/river_gang/orchestrator/dispatch.py`
- Create: `tests/orchestrator/test_concurrency.py`

- [ ] write tests first per §8.3: `available_slots(state, max_global, current_running, per_state_map, current_per_state)` returns min(global_slots, state_slots); state_slots = `per_state_map.get(state.lower(), global_slots)`; running по `state` считаются на основе current `running` map
- [ ] implement helper `concurrency_check(issue, state, config)` → bool/slots
- [ ] tests для cases: глобал лимит исчерпан; глобал есть но per-state выбран; per-state нет в map → fallback global
- [ ] run tests — must pass before next task

#### Task 29: Retry queue + backoff

**Files:**
- Create: `src/river_gang/orchestrator/retry.py`
- Create: `tests/orchestrator/test_retry.py`

- [ ] write tests first per §8.4: `compute_backoff_ms(attempt, max_cap)` returns `min(10000 * 2**(attempt-1), max_cap)`; `attempt=1` → 10000; `attempt=2` → 20000; `attempt=10` capped; continuation delay = 1000ms; `schedule_retry(state, issue_id, attempt, kind)` cancels existing timer first, stores new RetryEntry; `cancel_retry(state, issue_id)` removes
- [ ] implement `retry.py::RetryEntry`, `compute_backoff_ms`, `schedule_retry`, `cancel_retry`, `RetryKind = Literal["continuation", "failure"]`
- [ ] tests для timer cancellation на shutdown
- [ ] run tests — must pass before next task

#### Task 30: Reconciliation: stall detection part

**Files:**
- Create: `src/river_gang/orchestrator/reconcile.py`
- Create: `tests/orchestrator/test_reconcile_stall.py`

- [ ] write tests first per §8.5 Part A: для каждого running entry — `elapsed_ms` от `last_codex_timestamp` (если есть event) или `started_at`; `> stall_timeout_ms` → mark for termination + retry; `stall_timeout_ms <= 0` → skip; не stalled → no-op
- [ ] implement `reconcile.py::detect_stalls(state, now, stall_timeout_ms) -> list[issue_id]`
- [ ] tests для timing edge cases
- [ ] run tests — must pass before next task

#### Task 31: Reconciliation: tracker state refresh part

**Files:**
- Modify: `src/river_gang/orchestrator/reconcile.py`
- Create: `tests/orchestrator/test_reconcile_tracker.py`

- [ ] write tests first per §8.5 Part B: refresh fetches state by ids; terminal state → terminate worker + cleanup workspace; active → update issue snapshot in running entry; neither → terminate without cleanup; refresh failure → keep workers running, no termination
- [ ] implement `reconcile.py::reconcile_running_with_tracker(state, refreshed_issues, terminal_states, active_states) -> ReconcileActions`
- [ ] tests для each transition
- [ ] run tests — must pass before next task

#### Task 32: Worker attempt orchestration (workspace + agent loop)

**Files:**
- Create: `src/river_gang/orchestrator/worker.py`
- Create: `tests/orchestrator/test_worker.py`

- [ ] write tests first per §16.5: `run_agent_attempt(issue, attempt, mailbox)` создаёт workspace, запускает `before_run` hook, стартует Codex session, выполняет turns в loop пока `issue.state` active и `turn_number < max_turns`; **explicit test: `turn_number=1` получает full rendered prompt, `turn_number≥2` получает только continuation guidance** (§7.1, §16.5); on workspace failure / `before_run` failure / session failure / prompt failure / turn failure → call `stop_session` (если session открыта) + `after_run` hook (best-effort, errors logged not raised) + `WorkerExit(reason=str)` (abnormal); refresh issue state каждый turn через FakeTracker; on normal exit — `stop_session` + `after_run` (best-effort) + `WorkerExit(reason=normal)` to mailbox; **`before_remove` НЕ вызывается worker'ом — это hook для terminal-state cleanup в reconcile (§9.4 vs §16.5 differentiation)**
- [ ] implement `worker.py::run_agent_attempt(...)` — реальный async function (использует FakeCodex/Workspace/Tracker из Task 24a в тестах)
- [ ] tests для max_turns boundary (выход при достижении)
- [ ] tests для refresh failure inside loop
- [ ] tests для `after_run` hook failure → logged, не превращает success в failure (§9.4)
- [ ] run tests — must pass before next task

#### Task 33: Dispatch issue: spawn worker + state mutation

**Files:**
- Modify: `src/river_gang/orchestrator/dispatch.py`
- Create: `tests/orchestrator/test_dispatch_spawn.py`

- [ ] write tests first per §16.4: `dispatch_issue(state, issue, attempt, worker_factory)` spawns worker task, populates `running[issue.id]` + `claimed`; removes existing retry; spawn failure → schedule retry с attempt = next_attempt
- [ ] implement `dispatch.py::dispatch_issue` mutating helper (working на mailbox-style state)
- [ ] tests для spawn failure path
- [ ] run tests — must pass before next task

#### Task 34: Worker exit handler + retry scheduling

**Files:**
- Create: `src/river_gang/orchestrator/lifecycle.py`
- Create: `tests/orchestrator/test_lifecycle.py`

- [ ] write tests first per §16.6: `on_worker_exit(state, issue_id, reason)` removes running entry, добавляет runtime seconds в totals, normal exit → schedule continuation retry (1s) + add to completed (bookkeeping); abnormal → exponential backoff retry; identifier preserved
- [ ] implement `lifecycle.py::on_worker_exit`, `add_runtime_seconds_to_totals`, `next_attempt_from(running_entry)` (uses `retry_attempt`)
- [ ] tests для each branch
- [ ] run tests — must pass before next task

#### Task 35: Retry timer fired handler

**Files:**
- Modify: `src/river_gang/orchestrator/lifecycle.py`
- Create: `tests/orchestrator/test_retry_timer.py`

- [ ] write tests first per §16.6 + §8.4: `on_retry_timer(state, issue_id, fetch_candidates_fn)` — pop retry entry; missing → no-op; fetch fails → reschedule с error; issue not in candidates → release claim; no slots → reschedule с `error="no available orchestrator slots"`; eligible + slots → dispatch
- [ ] implement `lifecycle.py::on_retry_timer`
- [ ] tests для each path
- [ ] run tests — must pass before next task

#### Task 36: Codex update event handler

**Files:**
- Modify: `src/river_gang/orchestrator/lifecycle.py`
- Create: `tests/orchestrator/test_codex_update.py`

- [ ] write tests first per §7.3: `on_codex_update(state, issue_id, event)` updates `last_codex_event`, `last_codex_timestamp`, `last_codex_message`; **на `session_started` или first event новой turn → updates `running_entry.session_id` к текущему `<thread_id>-<turn_id>`** (per-turn id update, не только first turn — иначе dashboard показывает stale id после turn 2+); appends event в `recent_events` deque; tokens delta вычислен против `last_reported_*`, добавлен в `codex_totals`; `last_reported_*` обновлены к новому absolute; rate_limits updated если present; missing running entry → drop event silently
- [ ] implement `lifecycle.py::on_codex_update`
- [ ] tests для double-counting prevention
- [ ] run tests — must pass before next task

#### Task 37: Poll-and-dispatch tick loop

**Files:**
- Create: `src/river_gang/orchestrator/loop.py`
- Create: `tests/orchestrator/test_loop.py`

- [ ] write tests first per §16.2: `on_tick(state, ctx)` — reconcile first; **defensive reload**: re-load+validate workflow config до dispatch (covers missed watch events per §6.2 "SHOULD also re-validate/reload defensively during runtime operations"); on validation fail → skip dispatch + reschedule tick + log; fetch candidates; on fetch fail → skip + reschedule + log; sort + dispatch до slots exhausted; reschedule next tick по `state.poll_interval_ms`
- [ ] implement `loop.py::Orchestrator` главный класс — owns mailbox, runs writer-task, schedules ticks через `loop.call_later`
- [ ] integration test: simulate full tick с FakeTracker + FakeCodexClient → verify dispatch happened, running populated
- [ ] run tests — must pass before next task

#### Task 38: Service startup orchestration

**Files:**
- Create: `src/river_gang/orchestrator/startup.py`
- Create: `tests/orchestrator/test_startup.py`

- [ ] write tests first per §16.1 startup ordering (matches spec алгоритм): (1) `configure_logging` first (чтобы validation failures были visible); (2) start observability outputs; (3) start workflow watcher; (4) initialize state; (5) `validate_dispatch_config` — failure → operator-visible error через logging + `fail_startup` (return exit code, no exception leak); (6) `startup_terminal_workspace_cleanup` — failure → log warning + continue (§8.6); (7) schedule immediate tick; (8) enter event loop
- [ ] write tests first для graceful shutdown sequence (SIGINT/SIGTERM): (a) stop accepting new ticks (cancel scheduled `call_later`); (b) shutdown HTTP server (`uvicorn.Server.should_exit = True` + await server task); (c) cancel all retry timers; (d) для каждого running worker: cancel `worker_handle`, await с bounded grace (`shutdown_grace_ms`, default 30000), force-kill Codex subprocess через `stop_session` если grace expired; (e) stop workflow watcher; exit cleanly
- [ ] implement `startup.py::start_service` async entry с явным shutdown sequencer
- [ ] tests с `asyncio.subprocess` SIGINT — process exits within bounded time (≤35s), exit code 0
- [ ] tests для startup validation failure → exit nonzero + error logged до handlers configured
- [ ] run tests — must pass before next task

#### Task 39: Issue/session log context propagation

**Files:**
- Modify: `src/river_gang/orchestrator/loop.py`
- Modify: `src/river_gang/orchestrator/worker.py`
- Modify: `src/river_gang/observability/logging.py`
- Create: `tests/orchestrator/test_log_context.py`

- [ ] write tests first per §13.1: каждый log в worker содержит `issue_id` + `issue_identifier`; coding-agent session lifecycle logs содержат `session_id`; `key=value` formatting; `completed`/`failed`/`retrying` outcomes присутствуют где применимо
- [ ] implement context vars (`contextvars.ContextVar`) для issue_id, identifier, session_id, добавляемые автоматически в logging records
- [ ] tests с capturing log handler
- [ ] run tests — must pass before next task

---
### M8 — Snapshot / Observability View

#### Task 40: Runtime snapshot builder

**Files:**
- Create: `src/river_gang/observability/snapshot.py`
- Create: `tests/observability/test_snapshot.py`

- [ ] write tests first per §13.3: `build_snapshot(state, now)` returns `Snapshot` с `running` rows (incl. `turn_count`), `retrying` rows, `codex_totals` (input/output/total/seconds_running incl. active), `rate_limits`, `generated_at`; aggregate runtime = cumulative_ended + sum(active_elapsed); empty state → empty arrays + zero totals
- [ ] implement `snapshot.py::Snapshot` dataclass + `build_snapshot(state, now)` pure function
- [ ] tests для active session contribution к seconds_running
- [ ] tests для shape per §13.7.2 example
- [ ] run tests — must pass before next task

---
### M9 — HTTP Server Extension

#### Task 41: FastAPI app skeleton + lifecycle

**Files:**
- Create: `src/river_gang/http/__init__.py`
- Create: `src/river_gang/http/app.py`
- Create: `src/river_gang/http/server.py`
- Create: `tests/http/__init__.py`
- Create: `tests/http/test_app.py`

- [ ] write tests first: `create_app(orchestrator)` returns FastAPI instance; loopback bind default `127.0.0.1`; `start_server(app, port)` запускает uvicorn в текущем event loop как task; `port=0` → ephemeral; **bound port returned non-zero и реально listenable** (test делает `httpx.AsyncClient` против `http://127.0.0.1:<bound_port>/`); CLI `--port` overrides `server.port` config; `stop()` корректно останавливает (`server.should_exit = True; await task`)
- [ ] implement `app.py::create_app`, `server.py::start_server`: создать `uvicorn.Config` + `uvicorn.Server`, `await server.startup()` чтобы получить bound socket, прочитать port из `server.servers[0].sockets[0].getsockname()[1]`, далее `task = asyncio.create_task(server.main_loop())`; вернуть handle с `port` и `stop()`
- [ ] tests с `httpx.AsyncClient(app=app)` (in-process) для unit testing routes
- [ ] tests с реальным `start_server` для bound-port discovery
- [ ] run tests — must pass before next task

#### Task 42: GET /api/v1/state

**Files:**
- Create: `src/river_gang/http/routes_state.py`
- Modify: `src/river_gang/http/app.py`
- Create: `tests/http/test_state_endpoint.py`

- [ ] write tests first per §13.7.2: GET `/api/v1/state` returns JSON shape с `generated_at`, `counts.running`, `counts.retrying`, `running[]`, `retrying[]`, `codex_totals`, `rate_limits`; running entry содержит `turn_count`; пустой state → пустые arrays
- [ ] implement Pydantic models + route handler that calls `build_snapshot()`
- [ ] tests с populated FakeOrchestratorState
- [ ] tests для `405 Method Not Allowed` на POST
- [ ] run tests — must pass before next task

#### Task 43: GET /api/v1/{identifier}

**Files:**
- Create: `src/river_gang/http/routes_issue.py`
- Modify: `src/river_gang/http/app.py`
- Create: `tests/http/test_issue_endpoint.py`

- [ ] write tests first: GET `/api/v1/MT-649` returns issue details per §13.7.2; running issue → `status="running"` + `running` block; retrying issue → `retry` block; unknown issue → `404` + `{"error":{"code":"issue_not_found","message":"..."}}`; **populated fields**: `attempts.restart_count` (from RunningEntry, см. Task 25), `attempts.current_retry_attempt` (from RetryEntry); `recent_events[]` (from `RunningEntry.recent_events` deque, до 50 элементов); `last_error` (from `RunningEntry.last_error`); **deferred fields** (returned with safe defaults): `tracked = {}`, `logs.codex_session_logs = []` (Codex session logs не пишутся в файл в текущей итерации — explicit gap, задокументирован в `docs/conformance.md`)
- [ ] implement route + Pydantic models + lookup helper в `OrchestratorState`
- [ ] tests для each status path и каждого populated field
- [ ] run tests — must pass before next task

#### Task 44: POST /api/v1/refresh

**Files:**
- Create: `src/river_gang/http/routes_refresh.py`
- Modify: `src/river_gang/http/app.py`
- Create: `tests/http/test_refresh_endpoint.py`

- [ ] write tests first per §13.7.2: POST `/api/v1/refresh` отправляет PollTick в mailbox + reconcile; returns `202 Accepted` body `{"queued": true, "coalesced": false, "requested_at": "...", "operations": ["poll", "reconcile"]}`. Coalescing — `MAY` per spec, не реализуем (избежание сложности; всегда `coalesced: false`)
- [ ] implement route — простой enqueue в mailbox без debounce/coalescing
- [ ] tests для каждого вызова возвращающего independent `requested_at`
- [ ] run tests — must pass before next task

#### Task 45: Dashboard at `/`

**Files:**
- Create: `src/river_gang/http/routes_dashboard.py`
- Create: `src/river_gang/http/templates/dashboard.html`
- Modify: `src/river_gang/http/app.py`
- Create: `tests/http/test_dashboard.py`

- [ ] write tests first: GET `/` returns `text/html`; HTML содержит `Active sessions`, `Retry queue`, `Token consumption`, current snapshot data; renders empty-state when nothing running
- [ ] implement server-rendered HTML через простой Jinja2 template (или f-string template для минимизации deps); read snapshot at request time
- [ ] tests для snapshot-driven content
- [ ] run tests — must pass before next task

#### Task 46: Error envelope + 405 handling

**Files:**
- Modify: `src/river_gang/http/app.py`
- Create: `tests/http/test_error_envelope.py`

- [ ] write tests first per §13.7.2 API design notes: ошибки возвращают `{"error":{"code":"...","message":"..."}}`; unsupported method на defined route → 405 + envelope; unknown route → 404 + envelope (FastAPI default `{"detail": "Not Found"}` должен быть переопределён); validation errors (422 → 400 в нашем envelope) тоже в envelope
- [ ] implement: `@app.exception_handler(StarletteHTTPException)` обёртка для всех HTTP-ошибок включая 404/405; `@app.exception_handler(RequestValidationError)` для 422 → 400 envelope. Убедиться что custom handler срабатывает для unknown route (FastAPI вызывает default 404 handler — нужно `app.add_exception_handler(404, ...)` или handler на `StarletteHTTPException`)
- [ ] tests для each path: known route POST → 405; unknown route GET → 404; both возвращают `error` envelope
- [ ] run tests — must pass before next task

---
### M10 — CLI and Process Lifecycle

#### Task 47: CLI argparse + workflow path resolution

**Files:**
- Modify: `src/river_gang/cli.py`
- Create: `tests/test_cli.py`

- [ ] write tests first per §17.7: positional `path-to-WORKFLOW.md` принят; отсутствует → `./WORKFLOW.md`; nonexistent explicit path → exit nonzero + сообщение в stderr; missing default → exit nonzero; `--port N` override; `--help` shows usage. (Note: §17.7 bullets "exits with success when application starts and shuts down normally" / "exits nonzero when startup fails or host process exits abnormally" покрываются в Task 48 integration tests)
- [ ] implement `cli.py::main(argv=None) -> int`
- [ ] tests с `subprocess.run([sys.executable, "-m", "river_gang", ...])` для exit codes
- [ ] run tests — must pass before next task

#### Task 48: CLI startup integration

**Files:**
- Modify: `src/river_gang/cli.py`
- Modify: `src/river_gang/__main__.py`
- Create: `tests/test_cli_startup.py`

- [ ] write tests first: cli wires `start_service(workflow_path, port=...)`; startup validation failure → exit 1 + operator-visible error; SIGINT → graceful shutdown exit 0; abnormal exit → nonzero
- [ ] implement integration в `cli.main` через `asyncio.run(start_service(...))`
- [ ] tests с реальным `subprocess.Popen` и SIGINT для graceful shutdown (timeout-bound)
- [ ] run tests — must pass before next task

---
### M11 — Conformance Sweep + Documentation

#### Task 49: §17 conformance test mapping

**Files:**
- Create: `tests/conformance/__init__.py`
- Create: `tests/conformance/test_17_1_workflow_config.py`
- Create: `tests/conformance/test_17_2_workspace.py`
- Create: `tests/conformance/test_17_3_tracker.py`
- Create: `tests/conformance/test_17_4_orchestrator.py`
- Create: `tests/conformance/test_17_5_codex.py`
- Create: `tests/conformance/test_17_6_observability.py`
- Create: `tests/conformance/test_17_7_cli.py`
- Create: `docs/conformance.md`

- [ ] для каждого bullet в §17.1–17.7 написать explicit test с docstring `"""Conformance §17.X: <quoted bullet>"""` (некоторые могут переиспользовать unit тесты через `pytest.mark.conformance` marker)
- [ ] write `docs/conformance.md` — таблица `§17.x bullet → test path`
- [ ] cover §17.5 extension bullets для `linear_graphql` (advertised, valid input, GraphQL errors, invalid input, unsupported tool name)
- [ ] verify `pytest -m conformance` выполняется чистым
- [ ] run full pytest — must pass before next task

#### Task 50: Trust posture documentation

**Files:**
- Create: `docs/trust-posture.md`

- [ ] document в `docs/trust-posture.md` per §10.5/§15.1: target environment = trusted single-tenant; `approval_policy=never` + `sandbox=workspace-write`; user-input-required treated as failure; hooks run with full host privilege; rationale; recommended hardening for stricter deployments
- [ ] cross-link из README

#### Task 51: README + WORKFLOW.md sample

**Files:**
- Modify: `README.md`
- Create: `WORKFLOW.md.sample`

- [ ] write README: install via `uv`, configure `LINEAR_API_KEY`, run `river-gang ./WORKFLOW.md`, optional `--port`
- [ ] write `WORKFLOW.md.sample` с realistic prompt + tracker.kind=linear + project_slug placeholder + reasonable defaults
- [ ] document trust posture link
- [ ] verify sample loads cleanly: добавить `tests/test_sample_workflow.py` который грузит `WORKFLOW.md.sample` через workflow loader. Path resolved как `Path(__file__).resolve().parent.parent / "WORKFLOW.md.sample"` (устойчиво к запуску из любого cwd, включая `sandbox/`)

#### Task 52: Verify acceptance criteria

- [ ] verify §18.1 каждый bullet реализован (manual mapping в `docs/conformance.md`)
- [ ] verify §18.2 HTTP server + linear_graphql реализованы
- [ ] verify §17 test matrix полностью green: `uv run pytest -m conformance`
- [ ] verify все §17 bullets начинающиеся с `If ... is implemented` для shipped extensions покрыты тестами
- [ ] run `uv run pytest && uv run mypy src/river_gang && uv run ruff check src tests`
- [ ] verify нет xfail/xpass без TODO

#### Task 53: Final cleanup

- [ ] update README с финальной structure
- [ ] verify CLAUDE.md не нужен (greenfield, README достаточно)
- [ ] move plan to `docs/plans/completed/`
- [ ] `mkdir -p docs/plans/completed && git mv docs/plans/20260428-symphony-river-gang.md docs/plans/completed/`

---
## Post-Completion
*Items requiring manual intervention or external systems — no checkboxes, informational only*

**Manual verification:**
- Real Linear smoke test (Section 17.8 Real Integration Profile) с настоящим `LINEAR_API_KEY` и dedicated test проектом — fetch candidates / refresh / terminal cleanup
- Real `codex app-server` smoke: запуск daemon с настоящим Codex бинарём против реального Linear issue, наблюдение за turn-streaming, token tracking, rate-limit extraction
- Stall timeout end-to-end: имитировать застрявшую сессию (Codex без events) и подтвердить kill+retry
- Dynamic reload: edit `WORKFLOW.md` while daemon running, observe new poll interval applied without restart
- Trust posture validation: запустить агента который пытается писать в `~/test_breakout` — sandbox должен заблокировать (verifies `workspace-write` enforcement)
- HTTP API: `curl /api/v1/state`, `/api/v1/<id>`, `POST /api/v1/refresh` против running daemon

**External system updates:**
- При продакшене — выделенный OS-юзер для daemon, restricted permissions на workspace root (§15.2)
- Logging sink configuration: stderr by default; для production — рассмотреть file/syslog/remote sink через config layer
- Packaging: PyPI release под именем `river_gang` или приватный wheel
- TODO items из §18.2 (persistence retry queue, observability config in workflow, first-class tracker writes, pluggable tracker adapters) — адресовать в follow-up planning sessions
