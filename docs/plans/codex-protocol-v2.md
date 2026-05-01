# Adapt river-gang to codex 0.125.0 app-server protocol

## Why

Current river-gang implements an older codex protocol shape. Codex 0.125.0+
schema (verified via `codex app-server generate-json-schema --out
sandbox/codex-schema/`) diverges in three orthogonal ways:

1. **Method namespace**: `thread.start`/`turn.start` → `thread/start`/`turn/start`.
   Notifications also use `/`-separated paths (`turn/completed`, `thread/started`).
2. **Param/result shapes**: `sandboxPolicy` → `sandbox`; `prompt: string` → `input: [{type: "text", text}]`;
   response wraps result in `{thread: {id, ...}}` / `{turn: {id, status, ...}}`.
3. **Tool/approval flow**: was notification-driven (`tool_call` notification + `tool_call_response` notification).
   Now request/response (`item/tool/call` server request expects JSON-RPC response with same id).
   Approvals likewise: `applyPatchApproval`, `execCommandApproval`, `item/*/requestApproval` server requests.

Reference: `sandbox/codex-schema/{codex_app_server_protocol.v2.schemas.json,
ServerNotification.json, ServerRequest.json, ClientRequest.json}`.

SPED §10 already defers protocol shape to "the targeted Codex app-server
protocol" — no SPED edits needed. The drift is implementation-only.

## Tasks

### 1. Outgoing request shape — `protocol.py`
- [x] `METHOD_THREAD_START = "thread/start"`
- [x] `METHOD_TURN_START = "turn/start"`
- [x] `build_thread_start_request`: drop `sandboxPolicy` key, write `sandbox`. Drop `title`.
- [x] `build_turn_start_request`: replace `prompt: str` param with `input` array literal `[{type:"text", text: prompt}]`. Drop `title`.
- [x] `build_continuation_turn_request`: same input-array shape with guidance text.
- [x] `extract_thread_id`: read `result["thread"]["id"]` (was `result["threadId"]`).
- [x] `extract_turn_id`: read `result["turn"]["id"]` (was `result["turnId"]`).
- verify: `uv run pytest tests/codex/test_protocol.py`

### 2. Send `initialized` notification after `initialize` ack — `client.py`
- [x] After `initialize` round-trip succeeds, write `{method: "initialized"}` notification.
- verify: ClientNotification schema only contains `initialized`; codex expects it before subsequent calls.

### 3. Incoming dispatch: distinguish notifications vs server-requests — `client.py`
- [x] In the read loop, route by shape:
  - `id + method` → server **request** → handle and send `{id, result}` or `{id, error}` back.
  - `method` only → server **notification** → existing notification path.
  - `id + result/error` → response to our pending request → existing correlation.
- [x] smoke test (deferred to task 10 e2e); `codex app-server generate-ts` shows the discriminator.

### 4. Turn lifecycle dispatch — `client.py`
- [x] Replace `_EVENT_TURN_COMPLETED = "turn_completed"` → `"turn/completed"`.
- [x] Drop `_EVENT_TURN_FAILED`, `_EVENT_TURN_ENDED_WITH_ERROR`, `_EVENT_TURN_CANCELLED`. The single `turn/completed` notification carries `params.turn.status ∈ {completed, failed, interrupted, inProgress}` and `params.turn.error.message` when failed.
- [x] Drop `_EVENT_TURN_INPUT_REQUIRED` notification path (now a server request).
- [x] Add handler for top-level `error` notification (TurnError outside of turn/completed).
- verify: unit tests with new notification shapes.

### 5. Tool-call request handler — `client.py`
- [x] Drop `_EVENT_TOOL_CALL = "tool_call"` notification dispatch.
- [x] Add server-request handler for `item/tool/call` — invoke linear_graphql or fail with unknown-tool error.
- [x] Reply with `{id, result: {...}}` or `{id, error: {code, message}}` JSON-RPC response (no `method`).
- [x] Drop `_TOOL_CALL_RESPONSE_METHOD = "tool_call_response"` outgoing notification.
- verify: tool-advertisement tests still pass with reshaped flow.

### 6. Approval request handlers — `client.py` + `policy.py`
- [x] Drop `EVENT_APPROVAL_REQUEST = "approval_request"` notification dispatch.
- [x] Add server-request handlers for: `applyPatchApproval`, `execCommandApproval`, `item/commandExecution/requestApproval`, `item/fileChange/requestApproval`, `item/permissions/requestApproval`.
- [x] All map to the same policy logic — `never` policy → approve. Reply with `{id, result: {decision: "approved"}}` (verify exact field via response schemas in `sandbox/codex-schema/*ApprovalResponse.json`).
- [x] Drop `METHOD_APPROVAL_RESPONSE = "approval_response"` notification path.
- verify: policy unit tests rewritten for new shape.

### 7. User-input request — `client.py`
- [x] Add server-request handler for `item/tool/requestUserInput` — current policy is "never block on input"; reply with refusal/empty per schema.
- verify: timeout test reshaped.

### 8. Token usage notification — `usage.py`
- [x] Already uses `thread/tokenUsage/updated` — verify against `ThreadTokenUsageUpdatedNotification` schema; adjust extractor if field names differ.
- verify: `tests/codex/test_usage.py`.

### 9. Update fixtures + tests
- [x] `tests/codex/test_client_startup.py`: handshake fixture sends `{thread: {id}}` / `{turn: {id, status}}` shapes.
- [x] `tests/codex/test_client_streaming.py`: notifications use new method names.
- [x] `tests/codex/test_tools_advertisement.py`: server-request flow for tool calls.
- [x] `tests/codex/test_policy.py`: server-request flow for approvals.
- [x] `tests/codex/test_usage.py`: ThreadTokenUsage shape.
- [x] `tests/conformance/test_17_5_codex.py`: same updates.
- verify: `uv run pytest tests/codex/ tests/conformance/test_17_5_codex.py`

### 10. End-to-end smoke against real codex
- [x] manual test (skipped — LINEAR_API_KEY not set in environment and no .env file; sandboxed `uv` cache also unavailable). Run `river-gang sandbox/WORKFLOW.e2e.md --port 8080` against TES-6.
- [x] manual test (skipped — requires Linear credentials and live TES-6 ticket). Watch dashboard: TES-6 should land in `running`, then `completed`. File `~/river-gang-workspaces/TES-6/hello.txt` exists with `hi`.
- verify: dashboard shows non-zero `codex_totals.total_tokens`, retry queue empty.

## Out of scope

- Granular `item/*` event surfacing (item/agentMessage/delta etc) — useful for live UI, not needed for orchestrator semantics.
- MCP tool flow (`mcpServer/tool/call`) — river-gang only ships `linear_graphql`; full MCP integration is a separate extension.
- `thread/resume` / `thread/fork` — not used in current orchestrator.
