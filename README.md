# river-gang

A long-running daemon that polls Linear for issues, claims one per
available slot, prepares an isolated per-issue workspace, runs a
`codex app-server` agent against it, streams events, and reconciles
state. Implements **Symphony Service Specification v1** (`SPED.md`)
plus two OPTIONAL extensions: an HTTP server and the `linear_graphql`
client-side tool.

Requires **Python 3.11+** and **codex >= 0.125.0** (the app-server
protocol used by river-gang switched its method namespace and request
shapes in 0.125.0; older codex builds are not compatible).

## Install

This project is managed with [`uv`](https://github.com/astral-sh/uv).

```bash
uv sync                       # install runtime + dev deps into .venv/
uv pip install -e .           # editable install (optional)
```

The console script `river-gang` is installed as a project entry point;
`python -m river_gang` works as well.

## Configure

1. Set `LINEAR_API_KEY` in your environment. river-gang reads it once
   at startup via `$VAR` indirection — your `WORKFLOW.md` must
   reference it as `api_key: $LINEAR_API_KEY` (see
   [`WORKFLOW.md.sample`](WORKFLOW.md.sample) for the canonical form).
   Rotation requires a daemon restart.
2. Copy the sample workflow:

   ```bash
   cp WORKFLOW.md.sample WORKFLOW.md
   ```

3. Edit `tracker.project_slug` to your Linear project slug. Other
   fields have working defaults; tweak them as needed.

The sample's prompt template uses Liquid syntax with the published
`issue` + `attempt` variables. See `WORKFLOW.md.sample` for the full
working example.

## Run

```bash
river-gang                              # uses ./WORKFLOW.md
river-gang ./path/to/WORKFLOW.md        # explicit path
river-gang ./WORKFLOW.md --port 8080    # with HTTP dashboard + JSON API
```

`--port N` enables the OPTIONAL HTTP extension on `127.0.0.1:N`. The
daemon binds to loopback only — operators wanting external access MUST
front it with a reverse proxy that adds auth (river-gang ships none).
**Without `--port` the dashboard and `/api/v1/*` endpoints are
unreachable** — the daemon runs CLI-only.

The daemon shuts down cleanly on `SIGINT` / `SIGTERM` and exits 0.
Validation failures (missing API key, unsupported tracker kind, etc.)
exit 1 with an operator-visible error written to stderr. Unexpected
exceptions exit 2.

## Quick start

```bash
uv sync
cp WORKFLOW.md.sample WORKFLOW.md && $EDITOR WORKFLOW.md   # set project_slug
export LINEAR_API_KEY=lit_xxx
river-gang ./WORKFLOW.md --port 8080
# → dashboard at http://127.0.0.1:8080/
```

## HTTP API

When started with `--port N`:

- `GET /` — server-rendered HTML dashboard (active sessions, retry
  queue, token consumption).
- `GET /api/v1/state` — full runtime snapshot as JSON
  (running/retrying/completed counts, codex_totals, rate_limits).
- `GET /api/v1/{identifier}` — per-issue debug detail (running block
  with session_id + turn_count, retry block, recent_events).
- `POST /api/v1/refresh` — manually enqueue a poll-and-reconcile tick;
  responds `202 Accepted` with `{queued, coalesced, requested_at,
  operations}`.
- `GET /healthz` — liveness probe (`{"ok": true}`).

Errors return a JSON envelope: `{"error": {"code": "<slug>", "message":
"<text>"}}` for 404 / 405 / validation (422 → 400) per SPED §13.7.2.

## Project structure

```
src/river_gang/
  cli.py              # argparse + main() entry
  __main__.py         # python -m river_gang
  config/             # YAML coercion, defaults, $VAR + ~ resolution, dispatch validation
  workflow/           # WORKFLOW.md loader + watchfiles-driven hot-reload
  tracker/            # Linear GraphQL client (httpx + respx)
  workspace/          # per-issue workspace mgmt + safety + hooks runner
  prompt/             # Liquid template renderer
  codex/              # JSON-RPC app-server client + protocol + token/usage extractors
  tools/              # linear_graphql client-side tool extension
  orchestrator/       # state, mailbox, dispatch, reconcile, retry, lifecycle, loop, startup
  observability/      # structured logging + runtime snapshot builder
  http/               # FastAPI app + uvicorn lifecycle + /api/v1/* + dashboard
```

## Security & Trust Posture

river-gang targets **trusted, single-tenant deployments** with auto-
approved tool calls and a `workspace-write` sandbox. Read
[`docs/trust-posture.md`](docs/trust-posture.md) before deploying — it
covers the approval policy, hook trust model, and recommended
hardening for stricter environments.

## Conformance

Each bullet of SPED §17.1–§17.7 maps to an explicit test in
`tests/conformance/`. Run the conformance subset only:

```bash
uv run pytest -m conformance
```

See [`docs/conformance.md`](docs/conformance.md) for the full bullet →
test::name mapping table and the list of explicitly-deferred fields.

## Development

```bash
uv run pytest                         # full suite (conformance + unit + integration)
uv run pytest -m conformance          # conformance subset only
uv run mypy src/river_gang            # strict type-check
uv run ruff check src tests           # lint
```

Sandbox-aware skips: a few real-TCP and real-filesystem-watch tests
skip when running in a sandbox that denies `bind(127.0.0.1, 0)` or
`watchfiles` events. ASGI in-process and injected-awatch tests cover
the same code paths in those environments.

Package import name: `river_gang`. Distribution name: `river-gang`.
