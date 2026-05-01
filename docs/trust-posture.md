# Trust posture

This document defines river-gang's documented trust boundary, approval
posture, and sandbox configuration per SPED §10.5 ("Approval, Tool
Calls, and User Input Policy") and §15.1 ("Trust Boundary
Assumption"). Operators MUST read this before deploying river-gang
into any environment beyond a personal laptop.

## Target environment

river-gang is designed for **trusted, single-tenant deployments**:

- One operator (or one tightly-controlled team) owns both the host and
  the WORKFLOW.md.
- Recommended deployment model: dedicated OS user, dedicated host
  process, no shared filesystem with untrusted accounts.
- The trust boundary is **the host machine**, not internal components.
  Anything that reaches the daemon (workflow file, env vars, tracker
  responses, codex output) is treated as trusted-by-virtue-of-source.

This posture is explicitly NOT suitable for multi-tenant SaaS
deployments without additional isolation work — see "Recommended
hardening" below.

## Approval policy: `never`

river-gang configures the Codex app-server with
`approval_policy = "never"` (SPED §10.5). Concretely:

- Command-execution approval requests are auto-approved by
  `ApprovalHandler` and the session continues without operator
  intervention.
- File-change approval requests are auto-approved.
- Audit pattern: codex 0.125.0+ promotes approvals from notifications
  to JSON-RPC server requests (`applyPatchApproval`,
  `execCommandApproval`, `item/commandExecution/requestApproval`,
  `item/fileChange/requestApproval`,
  `item/permissions/requestApproval`). Each inbound request frame and
  the matching outbound `{id, result: {decision: ...}}` (or
  `{permissions: {}}`) reply are visible in the structured logs at
  DEBUG level — operators capturing the JSON-RPC stream see both halves
  of every approval round-trip.

**Rationale**: river-gang is a daemon-driven unattended workflow.
Pausing for a human operator on every tool call would dominate latency
and defeat the design — issues sit waiting for hours instead of
minutes. The defense-in-depth comes from the workspace sandbox (see
below) and from operator-controlled WORKFLOW.md hooks, not from
per-call gating.

## Sandbox policy: `workspace-write`

river-gang configures the Codex app-server with
`sandbox_policy = "workspace-write"` (SPED §10.5):

- The agent can read and write files only inside the per-issue
  workspace directory (`<workspace.root>/<sanitized-identifier>/`).
- Reads and writes outside that directory rely on the targeted
  protocol's OS-level sandbox enforcement (filesystem ACLs, seccomp,
  etc., depending on Codex build).
- Network egress posture is implementation-defined by the Codex
  app-server build; river-gang itself does not impose extra
  network-level restrictions.

**Path containment is enforced by river-gang** before the agent
launches: `validate_within_root` resolves the per-issue workspace path
against the configured `workspace.root` and rejects any candidate
outside it. The same gate is applied when the agent reports a cwd back
to the orchestrator. This is the §15.2 "Mandatory" baseline — see
[`docs/conformance.md`](conformance.md) §17.2 row "Agent launch uses
the per-issue workspace path as cwd and rejects out-of-root paths".

## User-input-required: never block

Codex 0.125.0+ promoted the legacy `turn_input_required` notification
into the `item/tool/requestUserInput` JSON-RPC server request. When
codex sends one, river-gang's handler immediately replies with an
empty `ToolRequestUserInputResponse` (`{answers: {}}`) — schema-valid
"no answers provided" — so codex unblocks without waiting on a human.
The daemon does not prompt, does not surface the request to a human,
and does not block on input.

The downstream effect is implementation-defined by codex: it typically
proceeds without the requested input or raises its own failure on the
next turn event. river-gang surfaces the inbound request frame and
outbound `{answers: {}}` reply in DEBUG-level structured logs for
audit.

**Rationale**: A daemon cannot reliably reach a human. Anything that
needs human judgement should never have entered the queue —
WORKFLOW.md authors are responsible for shaping prompts so the agent
either solves the task autonomously or fails fast.

This matches the §10.5 example "high-trust behavior" — treating
user-input-required turns as never-block — verbatim.

## Hooks run with daemon privilege

`WORKFLOW.md` hooks (`after_create`, `before_run`, `after_run`,
`before_remove`) execute as **arbitrary shell scripts via
`bash -lc`** in the workspace directory, with the **full host
privilege of the river-gang daemon process** (SPED §15.4 "Hook Script
Safety").

Implications:

- Hooks are fully trusted configuration. A malicious hook script can
  do anything the daemon's OS user can do.
- Operators MUST audit every hook script before adding it to
  WORKFLOW.md.
- Hook stdout/stderr are truncated for logs but not redacted.
  Operators MUST avoid printing secrets from hook scripts.
- Hook timeouts (`hooks.timeout_ms`) are enforced so a runaway hook
  cannot hang the orchestrator (SPED §15.4 "Hook timeouts REQUIRED").

## Secret redaction in logs

river-gang's structured-log formatter (SPED §15.3) redacts dictionary
values whose keys match a fixed allowlist before rendering. The
allowlist combines:

- **Exact-match keys** (case-sensitive AND case-insensitive): common
  credential field names — `LINEAR_API_KEY`, `api_key`, `auth_token`,
  `access_token`, `bearer_token`, `refresh_token`, `client_secret`,
  `password`, `secret`, `authorization`.
- **Substring needles** (case-insensitive, applied to `key.lower()`):
  `api_key`, `apikey`, `api-key`, `secret_key`, `secret-key`,
  `secretkey`. These cover the common spelling variants that show up
  in third-party payloads (camelCase, kebab-case, no separator).

Bare `token` is intentionally NOT a substring needle — it would
collide with §13.5 token-accounting fields (`total_tokens`,
`input_tokens`, `last_token_usage` …) that are required to be visible
in dashboards and logs. Add new needles only when their keys reliably
carry credentials and never collide with token-counting fields.

The redaction operates on structured Python values (dicts, lists)
BEFORE serialisation; pre-rendered `key=value` strings are out of
scope. Producers must redact dicts via `redact_secrets(...)` before
handing them to the logger.

## What river-gang does NOT enforce

The following are explicitly **operator responsibilities**, not
daemon-enforced controls:

- **OS-level sandboxing**: river-gang does not chroot itself, does not
  spawn the agent in a separate UID/GID, and does not impose seccomp
  filters. Per-issue workspace isolation is path-level only — see
  "Recommended hardening".
- **Tracker auth secret rotation**: `tracker.api_key` is read once per
  process start (or on workflow reload). The daemon does not refresh
  tokens on a schedule. Operators MUST rotate `LINEAR_API_KEY` per
  their organization's policy and restart the daemon.
- **Hook script review**: as above — operator audit gate.
- **Network egress filtering**: the daemon talks to Linear's GraphQL
  endpoint and to the targeted Codex app-server registry. Restricting
  outbound traffic to those hosts is the deployment infrastructure's
  job (firewall, egress proxy, Tailscale ACL, etc.).
- **Audit log shipping**: structured logs are written to stderr in
  `key=value` format (SPED §13.1). Aggregation, retention, and audit
  review are downstream — Symphony does not provide a built-in audit
  trail backend.

## Recommended hardening for stricter deployments

Operators who need a tighter posture (multi-team host, regulated
data, internet-exposed dashboard, etc.) SHOULD layer the following:

- **Dedicated OS user** for the daemon, with restricted `sudoers`
  entries and no interactive login. Match `workspace.root` ownership
  to the daemon user (mode `0700`).
- **Container or chroot** the daemon so any agent escape is bounded
  to the container filesystem. SPED §15.5 explicitly notes
  "Filesystem isolation: chroot or unionfs-style overlay" as
  RECOMMENDED port-side hardening.
- **Dedicated volume** for `workspace.root` so blast radius of an
  agent that leaks secrets via filesystem is limited (§15.2
  RECOMMENDED).
- **Network egress filtering** at the firewall/egress-proxy layer:
  allowlist only the Linear GraphQL endpoint and the Codex app-server
  registry the daemon needs.
- **HTTP extension binding**: the OPTIONAL HTTP server (`--port`)
  binds to `127.0.0.1` by default. Operators wanting external access
  MUST explicitly choose to expose it via reverse proxy with auth —
  river-gang ships no built-in auth on the dashboard or `/api/v1/*`
  endpoints.
- **Secret rotation cadence**: rotate `LINEAR_API_KEY` and any other
  `$VAR`-resolved secrets per organization policy; restart the daemon
  after rotation.
- **Tool-call audit**: codex 0.125.0+ promotes tool calls to JSON-RPC
  request/response — capture each inbound `item/tool/call` server
  request and the matching outbound `{id, result: {success,
  contentItems}}` reply from the structured logs and ship them to a
  tamper-evident store.
- **`linear_graphql` advertisement gap**: the codex 0.125.0 JSON
  schema defines `DynamicToolSpec` but does not reference it from
  `InitializeParams`, `ThreadStartParams`, or `TurnStartParams`. There
  is no wire mechanism for river-gang to advertise its client-side
  `linear_graphql` tool during the handshake. The `item/tool/call`
  handler is wired and fully exercised by the unit/conformance tests
  (the dispatcher correctly routes calls codex sends), but codex will
  not issue an `item/tool/call` request for this tool unless it has
  been registered externally (e.g. via an MCP server configured by the
  operator). Linear ticket state transitions and failure-context
  comments are now driven from the orchestrator side
  (`LinearClient.transition_state` / `add_comment`, called by the
  per-issue worker on entry and exit) so the dormant tool is no
  longer required for the §16.5 lifecycle. Operators who still want
  the agent to issue ad-hoc Linear queries can register the tool via
  MCP; absent that, the handler stays dormant by design and a debug
  log line surfaces the wiring at session start.
- **Token consumption monitoring**: subscribe to `codex_totals` and
  `rate_limits` from `GET /api/v1/state` to detect runaway agent loops
  and rate-limit exhaustion before they affect production cost or
  availability.

## Cross-references

- SPED §10.5 — Approval, Tool Calls, and User Input Policy.
- SPED §15.1 — Trust Boundary Assumption.
- SPED §15.2 — Filesystem Safety Requirements (mandatory baseline).
- SPED §15.3 — Secret Handling (no logging of tokens).
- SPED §15.4 — Hook Script Safety (hooks fully trusted).
- SPED §15.5 — Harness Hardening Guidance.
- [`docs/conformance.md`](conformance.md) — §17 conformance test
  mapping for each posture-relevant assertion.
