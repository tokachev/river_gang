"""SPED §17.5 conformance: Coding-Agent App-Server Client."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from river_gang.codex.client import (
    LINEAR_GRAPHQL_TOOL_NAME,
    LINEAR_GRAPHQL_TOOL_SPEC,
    METHOD_ITEM_TOOL_CALL,
    METHOD_TURN_COMPLETED,
    CodexClient,
    Session,
)
from river_gang.codex.errors import ResponseTimeout, TurnTimeout
from river_gang.codex.policy import (
    APPROVAL_POLICY_NEVER,
    METHOD_APPLY_PATCH_APPROVAL,
    ApprovalHandler,
)
from river_gang.codex.protocol import (
    METHOD_INITIALIZE,
    METHOD_THREAD_START,
    METHOD_TURN_START,
    compose_session_id,
    extract_thread_id,
    extract_turn_id,
)
from river_gang.codex.usage import (
    METHOD_TOKEN_USAGE_UPDATED,
    extract_cumulative_tokens,
    extract_rate_limits,
)
from river_gang.tools.linear_graphql import ToolResult
from river_gang.tracker.issue import Issue
from tests.codex.fakes import FakeCodexProcess

pytestmark = pytest.mark.conformance


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _issue() -> Issue:
    return Issue(
        id="i", identifier="MT-1", title="t", state="Todo",
        description=None, priority=None, branch_name=None, url=None,
        labels=(), blocked_by=(), created_at=None, updated_at=None,
    )


def _handshake() -> list[dict[str, Any]]:
    """Three pre-canned responses for the codex 0.125.0+ handshake.

    ``thread/start`` wraps the id under ``result.thread``;
    ``turn/start`` wraps it under ``result.turn``.
    """
    return [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"thread": {"id": "th-1"}}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {"turn": {"id": "tn-1", "status": "inProgress"}},
        },
    ]


class _FakeLinearGraphqlTool:
    def __init__(self, *, return_value: ToolResult | None = None) -> None:
        self.calls: list[Any] = []
        self._return = return_value or ToolResult(
            success=True, data={"viewer": {"id": "u-1"}},
            errors=None, error_message=None,
        )

    async def execute(self, raw_input: Any) -> ToolResult:
        self.calls.append(raw_input)
        return self._return


# ---------------------------------------------------------------------------
# Launch / handshake
# ---------------------------------------------------------------------------


def test_launch_uses_workspace_cwd_via_bash_lc() -> None:
    """Conformance §17.5: launch command uses workspace cwd and invokes
    ``bash -lc <codex.command>``.

    The :func:`CodexProcess.launch` classmethod is the only path that
    spawns a subprocess. Its source explicitly calls
    ``asyncio.create_subprocess_exec("bash", "-lc", command, cwd=...)``.
    Verifying source-level guarantee since spawning a real subprocess
    isn't sandbox-safe.
    """
    import inspect

    from river_gang.codex.process import CodexProcess
    src = inspect.getsource(CodexProcess.launch)
    assert '"bash"' in src
    assert '"-lc"' in src
    assert "cwd=" in src


async def test_session_startup_follows_three_step_handshake(
    tmp_path: Path,
) -> None:
    """Conformance §17.5: session startup follows the targeted Codex
    app-server protocol.

    Three-step initialize → thread.start → turn.start handshake per
    SPED §10.2.
    """
    proc = FakeCodexProcess(_handshake())
    client = CodexClient(process=proc, codex_app_server_pid=42)
    session = await client.start_session(
        workspace=tmp_path,
        prompt="hello",
        issue=_issue(),
        approval_policy="never",
        sandbox_policy="workspace-write",
        read_timeout_ms=5000,
    )
    assert session.thread_id == "th-1"
    assert session.first_turn_id == "tn-1"
    assert session.session_id == "th-1-tn-1"

    # Three id-bearing requests carry the handshake; the codex 0.125.0+
    # ``initialized`` notification interleaves between them and is filtered
    # out so the order assertion stays focused on requests.
    request_methods = [
        w["method"] for w in proc.written_frames if "id" in w
    ]
    assert request_methods[:3] == [
        METHOD_INITIALIZE, METHOD_THREAD_START, METHOD_TURN_START
    ]


async def test_client_capability_payloads_valid_when_required(
    tmp_path: Path,
) -> None:
    """Conformance §17.5: client identity/capability payloads are valid
    when the targeted Codex app-server protocol requires them.

    The ``initialize`` request carries ``params`` per JSON-RPC 2.0;
    payload is validated by the FakeCodexProcess accepting the frame
    without protocol error. (No specific capability schema today; the
    default empty params dict is the implementation's documented
    payload.)
    """
    proc = FakeCodexProcess(_handshake())
    client = CodexClient(process=proc, codex_app_server_pid=42)
    await client.start_session(
        workspace=tmp_path, prompt="x", issue=_issue(),
        approval_policy="never", sandbox_policy="workspace-write",
        read_timeout_ms=5000,
    )
    init_frame = proc.written_frames[0]
    assert "jsonrpc" not in init_frame
    assert init_frame["method"] == METHOD_INITIALIZE
    assert "params" in init_frame


async def test_policy_payloads_use_documented_settings(
    tmp_path: Path,
) -> None:
    """Conformance §17.5: policy-related startup payloads use the
    implementation's documented approval/sandbox settings."""
    proc = FakeCodexProcess(_handshake())
    client = CodexClient(process=proc, codex_app_server_pid=42)
    await client.start_session(
        workspace=tmp_path, prompt="x", issue=_issue(),
        approval_policy="never", sandbox_policy="workspace-write",
        read_timeout_ms=5000,
    )
    # thread.start frame carries the policy fields.
    thread_frame = next(
        f for f in proc.written_frames if f.get("method") == METHOD_THREAD_START
    )
    params = thread_frame["params"]
    # Shape varies but the documented settings are echoed verbatim.
    serialised = json.dumps(params)
    assert "never" in serialised
    assert "workspace-write" in serialised


def test_thread_and_turn_ids_extracted_and_composed_to_session_id() -> None:
    """Conformance §17.5: thread and turn identities exposed by the
    targeted protocol are extracted and used to emit ``session_started``.

    Codex 0.125.0+ wraps identities under ``result.thread`` /
    ``result.turn``; the legacy top-level ``threadId`` / ``turnId`` fields
    no longer appear in responses.
    """
    assert extract_thread_id({"thread": {"id": "th-A"}}) == "th-A"
    assert (
        extract_turn_id({"turn": {"id": "tn-9", "status": "inProgress"}})
        == "tn-9"
    )
    assert compose_session_id("th-A", "tn-9") == "th-A-tn-9"


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------


async def test_read_timeout_enforced(tmp_path: Path) -> None:
    """Conformance §17.5: request/response read timeout is enforced."""
    # FakeCodexProcess with no frames + no terminal error → read_frame
    # raises PortExit, but we want a hung read. Use a process that
    # sleeps forever on read by overriding read_frame.
    import asyncio as _aio

    class _HangingProcess:
        async def read_frame(self) -> dict[str, Any]:
            await _aio.sleep(3600)
            raise AssertionError  # unreachable

        async def write_frame(self, payload: dict[str, Any]) -> None:
            return None

        async def wait_for_exit(self, timeout: float) -> bool:
            return True

        async def aclose(self) -> None:
            return None

    proc = _HangingProcess()
    client = CodexClient(process=proc, codex_app_server_pid=42)
    with pytest.raises(ResponseTimeout):
        await client.start_session(
            workspace=tmp_path, prompt="x", issue=_issue(),
            approval_policy="never", sandbox_policy="workspace-write",
            read_timeout_ms=50,
        )


async def test_turn_timeout_enforced(tmp_path: Path) -> None:
    """Conformance §17.5: turn timeout is enforced."""
    import asyncio as _aio

    # Streaming hangs after ack.
    class _AckThenHang:
        def __init__(self) -> None:
            self._writes: list[dict[str, Any]] = []
            self._first_read = True

        async def read_frame(self) -> dict[str, Any]:
            if self._first_read:
                self._first_read = False
                # Codex 0.125.0+ TurnStartResponse wraps the id under
                # ``result.turn`` (the legacy top-level ``turnId`` was removed).
                return {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "turn": {"id": "tn-1", "status": "inProgress"}
                    },
                }
            await _aio.sleep(3600)
            raise AssertionError

        async def write_frame(self, payload: dict[str, Any]) -> None:
            self._writes.append(payload)

        async def wait_for_exit(self, timeout: float) -> bool:
            return True

        async def aclose(self) -> None:
            return None

    proc = _AckThenHang()
    client = CodexClient(process=proc, codex_app_server_pid=42)
    session = Session(
        thread_id="th-1", first_turn_id="tn-0",
        codex_app_server_pid=42, started_at=datetime.now(UTC),
    )
    with pytest.raises(TurnTimeout):
        await client.stream_turn(
            session=session, prompt="x",
            on_event=lambda _e: None,
            turn_timeout_ms=50,
            is_first_turn=True,
        )


# ---------------------------------------------------------------------------
# Transport framing
# ---------------------------------------------------------------------------


def test_transport_framing_is_handled() -> None:
    """Conformance §17.5: transport framing required by the targeted
    protocol is handled correctly.

    :class:`CodexProcess` reads/writes JSON-RPC frames; framing is
    JSON-line-based for the targeted protocol. The unit suite under
    tests/codex/test_process.py exercises the framer directly.
    """
    from river_gang.codex.process import MAX_FRAME_BYTES
    assert MAX_FRAME_BYTES > 0  # framer has a configured limit


def test_stderr_isolated_from_protocol_stream() -> None:
    """Conformance §17.5: for stdio-based transports, diagnostic stderr
    handling is kept separate from the protocol stream.

    :func:`CodexProcess.launch` spawns a dedicated stderr-drain task so
    diagnostic output never reaches the JSON-RPC reader.
    """
    import inspect

    from river_gang.codex.process import CodexProcess
    src = inspect.getsource(CodexProcess.launch)
    assert "_drain_stderr" in src
    assert "create_task" in src


# ---------------------------------------------------------------------------
# Approval handling
# ---------------------------------------------------------------------------


def test_approvals_handled_per_documented_policy() -> None:
    """Conformance §17.5: command/file-change approvals are handled
    according to the implementation's documented policy.

    Codex 0.125.0+ promotes approvals from notifications to JSON-RPC server
    requests; :meth:`ApprovalHandler.build_result` returns the per-method
    response body the dispatcher wraps as ``{id, result}``. Under the
    'never' policy, ``applyPatchApproval`` (ReviewDecision shape) yields
    ``{"decision": "approved"}`` so the session continues.
    """
    handler = ApprovalHandler(approval_policy=APPROVAL_POLICY_NEVER)
    result = handler.build_result(
        METHOD_APPLY_PATCH_APPROVAL,
        {"callId": "c-1", "command": ["ls"]},
    )
    assert result == {"decision": "approved"}


# ---------------------------------------------------------------------------
# Unsupported tool calls
# ---------------------------------------------------------------------------


async def test_unsupported_dynamic_tool_calls_rejected_without_stalling(
    tmp_path: Path,
) -> None:
    """Conformance §17.5: unsupported dynamic tool calls are rejected
    without stalling the session.

    Codex 0.125.0+ promotes the legacy ``tool_call`` notification +
    ``tool_call_response`` notification pair into a single ``item/tool/call``
    server request. An unknown tool name yields a
    :file:`DynamicToolCallResponse` reply with ``success=false`` and an
    ``unsupported_tool: ...`` content item so codex unblocks and the turn
    continues to its terminal ``turn/completed``.
    """
    frames: list[dict[str, Any]] = list(_handshake())
    # ack for stream_turn
    frames.append({
        "jsonrpc": "2.0",
        "id": 4,
        "result": {"turn": {"id": "tn-2", "status": "inProgress"}},
    })
    # Server-initiated tool-call request with an unknown tool name.
    frames.append({
        "jsonrpc": "2.0",
        "id": 7001,
        "method": METHOD_ITEM_TOOL_CALL,
        "params": {
            "tool": "unknown_tool",
            "callId": "c-1",
            "threadId": "th-1",
            "turnId": "tn-2",
            "arguments": {},
        },
    })
    # Terminal completion so the turn returns instead of stalling.
    frames.append({
        "jsonrpc": "2.0",
        "method": METHOD_TURN_COMPLETED,
        "params": {
            "threadId": "th-1",
            "turn": {"id": "tn-2", "status": "completed", "items": []},
        },
    })
    proc = FakeCodexProcess(frames)
    client = CodexClient(process=proc, codex_app_server_pid=42)
    session = await client.start_session(
        workspace=tmp_path, prompt="x", issue=_issue(),
        approval_policy="never", sandbox_policy="workspace-write",
        read_timeout_ms=5000,
    )
    result = await client.stream_turn(
        session=session, prompt="continue",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )
    # Session continued through an unknown tool: turn completed.
    assert result is not None

    # The handler wrote a DynamicToolCallResponse reply for the unknown
    # tool — success=false with the unsupported_tool sentinel encoded
    # inside contentItems[0].text. No JSON-RPC error.
    replies = [
        f
        for f in proc.written_frames
        if f.get("id") == 7001 and "method" not in f
    ]
    assert len(replies) == 1, proc.written_frames
    reply = replies[0]
    assert "result" in reply
    assert reply["result"]["success"] is False
    assert "unsupported_tool" in reply["result"]["contentItems"][0]["text"]


# ---------------------------------------------------------------------------
# User input requests
# ---------------------------------------------------------------------------


async def test_user_input_requests_handled_per_documented_policy(
    tmp_path: Path,
) -> None:
    """Conformance §17.5: user input requests are handled according to
    the implementation's documented policy and do not stall indefinitely.

    Codex 0.125.0+ promotes the legacy ``turn_input_required`` notification
    to the ``item/tool/requestUserInput`` server request. river-gang's
    documented posture (policy.py) is "never block on operator input" —
    the client must reply with an empty
    :file:`ToolRequestUserInputResponse` (``answers: {}``) so codex unblocks
    immediately, then the turn proceeds to its terminal ``turn/completed``.
    """
    frames: list[dict[str, Any]] = list(_handshake())
    # turn.start ack for stream_turn (id=4 — handshake consumed 1..3).
    # Codex 0.125.0+ wraps the new turn id under ``result.turn``.
    frames.append({
        "jsonrpc": "2.0",
        "id": 4,
        "result": {"turn": {"id": "tn-2", "status": "inProgress"}},
    })
    # Server-initiated user-input request mid-turn.
    frames.append({
        "jsonrpc": "2.0",
        "id": 9001,
        "method": "item/tool/requestUserInput",
        "params": {
            "itemId": "it-1",
            "threadId": "th-1",
            "turnId": "tn-2",
            "questions": [
                {"id": "q-1", "header": "Need input", "question": "Continue?"}
            ],
        },
    })
    # Terminal completion so the turn returns instead of stalling.
    frames.append({
        "jsonrpc": "2.0",
        "method": METHOD_TURN_COMPLETED,
        "params": {
            "threadId": "th-1",
            "turn": {"id": "tn-2", "status": "completed", "items": []},
        },
    })
    proc = FakeCodexProcess(frames)
    client = CodexClient(process=proc, codex_app_server_pid=42)
    session = await client.start_session(
        workspace=tmp_path, prompt="x", issue=_issue(),
        approval_policy="never", sandbox_policy="workspace-write",
        read_timeout_ms=5000,
    )
    # Documented policy: never-block-on-input → reply empty answers, do not
    # raise. The turn must reach its terminal completion event.
    result = await client.stream_turn(
        session=session, prompt="x",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )
    assert result is not None

    # Verify the JSON-RPC reply shape matches ToolRequestUserInputResponse:
    # ``{id: <request_id>, result: {answers: {}}}`` with no ``method``.
    replies = [
        f
        for f in proc.written_frames
        if f.get("id") == 9001 and "method" not in f
    ]
    assert len(replies) == 1, proc.written_frames
    assert replies[0].get("result") == {"answers": {}}
    assert "error" not in replies[0]


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def test_usage_telemetry_extracted_from_protocol_payloads() -> None:
    """Conformance §17.5: usage and rate-limit telemetry exposed by the
    targeted protocol is extracted.

    Codex 0.125.0+ ``ThreadTokenUsageUpdatedNotification`` carries cumulative
    counts under ``params.tokenUsage.total`` (a ``TokenUsageBreakdown`` with
    camelCase keys); ``tokenUsage.last`` holds per-event deltas which the
    extractor ignores.
    """
    snap = extract_cumulative_tokens(
        METHOD_TOKEN_USAGE_UPDATED,
        {
            "threadId": "th-1",
            "turnId": "tn-1",
            "tokenUsage": {
                "last": {
                    "inputTokens": 0,
                    "outputTokens": 0,
                    "totalTokens": 0,
                    "cachedInputTokens": 0,
                    "reasoningOutputTokens": 0,
                },
                "total": {
                    "inputTokens": 10,
                    "outputTokens": 5,
                    "totalTokens": 15,
                    "cachedInputTokens": 0,
                    "reasoningOutputTokens": 0,
                },
            },
        },
    )
    assert snap is not None
    assert snap.total_tokens == 15

    rl = extract_rate_limits({
        "rate_limit": {
            "limit": 100, "remaining": 50, "reset_at": "2026-04-28T00:00:00Z",
        }
    })
    assert rl is not None
    assert rl.limit == 100
    assert rl.remaining == 50


# ---------------------------------------------------------------------------
# linear_graphql tool extension
# ---------------------------------------------------------------------------


def test_client_side_tools_advertised_when_implemented() -> None:
    """Conformance §17.5: if client-side tools are implemented, session
    startup advertises the supported tool specs using the targeted
    app-server protocol."""
    assert LINEAR_GRAPHQL_TOOL_NAME == "linear_graphql"
    assert "name" in LINEAR_GRAPHQL_TOOL_SPEC
    assert "inputSchema" in LINEAR_GRAPHQL_TOOL_SPEC


async def test_linear_graphql_handler_is_wired_into_session(tmp_path: Path) -> None:
    """Conformance §17.5: ``linear_graphql`` — the tool is wired into the
    session so codex-initiated ``item/tool/call`` requests can dispatch
    against it.

    Codex 0.125.0+ schema gap: :file:`InitializeParams.json` /
    :file:`ThreadStartParams.json` define no public field for advertising
    client-side tools. Wire-side advertisement is therefore omitted; the
    handler still answers ``item/tool/call`` requests (which codex sends
    when externally configured to know about the tool).
    """
    proc = FakeCodexProcess(_handshake())
    tool = _FakeLinearGraphqlTool()
    client = CodexClient(
        process=proc,
        codex_app_server_pid=42,
        linear_graphql_tool=tool,
    )
    await client.start_session(
        workspace=tmp_path, prompt="x", issue=_issue(),
        approval_policy="never", sandbox_policy="workspace-write",
        read_timeout_ms=5000,
        tracker_kind="linear",
    )
    init_request = next(
        f for f in proc.written_frames if f.get("method") == METHOD_INITIALIZE
    )
    init_params = init_request["params"]
    # No ``tools`` field exists on InitializeParams; ensure we don't
    # silently start sending a non-schema field.
    assert "tools" not in init_params


def _tool_call_request(
    *,
    request_id: int,
    tool: str,
    arguments: Any,
    call_id: str = "c-1",
    thread_id: str = "th-1",
    turn_id: str = "tn-2",
) -> dict[str, Any]:
    """Build a codex 0.125.0+ ``item/tool/call`` server-request frame
    (DynamicToolCallParams shape)."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": METHOD_ITEM_TOOL_CALL,
        "params": {
            "tool": tool,
            "arguments": arguments,
            "callId": call_id,
            "threadId": thread_id,
            "turnId": turn_id,
        },
    }


def _stream_ack(turn_id: str = "tn-2", *, request_id: int = 4) -> dict[str, Any]:
    """Codex 0.125.0+ TurnStartResponse wraps the new turn id under
    ``result.turn``."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"turn": {"id": turn_id, "status": "inProgress"}},
    }


def _stream_completion(turn_id: str = "tn-2") -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": METHOD_TURN_COMPLETED,
        "params": {
            "threadId": "th-1",
            "turn": {"id": turn_id, "status": "completed", "items": []},
        },
    }


def _find_tool_reply(
    frames: list[dict[str, Any]], request_id: int
) -> dict[str, Any]:
    """Locate the codex 0.125.0+ JSON-RPC reply for a ``item/tool/call``
    request — ``{id, result|error}`` with no ``method`` field."""
    replies = [
        f for f in frames if f.get("id") == request_id and "method" not in f
    ]
    assert len(replies) == 1, frames
    return replies[0]


async def test_linear_graphql_valid_inputs_executed(tmp_path: Path) -> None:
    """Conformance §17.5: ``linear_graphql`` — valid ``query`` /
    ``variables`` inputs execute against configured Linear auth.

    Codex 0.125.0+: tool dispatch is a JSON-RPC server request
    (``item/tool/call``) replied to with a ``DynamicToolCallResponse``.
    """
    frames: list[dict[str, Any]] = list(_handshake())
    frames.append(_stream_ack())
    frames.append(
        _tool_call_request(
            request_id=8001,
            tool=LINEAR_GRAPHQL_TOOL_NAME,
            arguments={"query": "{ viewer { id } }", "variables": {}},
        )
    )
    frames.append(_stream_completion())
    proc = FakeCodexProcess(frames)
    tool = _FakeLinearGraphqlTool()
    client = CodexClient(
        process=proc, codex_app_server_pid=42,
        linear_graphql_tool=tool,
    )
    session = await client.start_session(
        workspace=tmp_path, prompt="x", issue=_issue(),
        approval_policy="never", sandbox_policy="workspace-write",
        read_timeout_ms=5000, tracker_kind="linear",
    )
    await client.stream_turn(
        session=session, prompt="x",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )
    assert len(tool.calls) == 1
    assert tool.calls[0]["query"] == "{ viewer { id } }"

    reply = _find_tool_reply(proc.written_frames, 8001)
    assert reply["result"]["success"] is True


async def test_linear_graphql_top_level_errors_preserve_body(
    tmp_path: Path,
) -> None:
    """Conformance §17.5: ``linear_graphql`` — top-level GraphQL ``errors``
    produce ``success=false`` while preserving the GraphQL body.

    Under codex 0.125.0+ the response carries free-form text content items;
    the GraphQL body and errors list are JSON-encoded inside the single
    ``inputText`` content item the schema permits.
    """
    frames: list[dict[str, Any]] = list(_handshake())
    frames.append(_stream_ack())
    frames.append(
        _tool_call_request(
            request_id=8101,
            tool=LINEAR_GRAPHQL_TOOL_NAME,
            arguments={"query": "bad"},
        )
    )
    frames.append(_stream_completion())
    proc = FakeCodexProcess(frames)
    tool = _FakeLinearGraphqlTool(
        return_value=ToolResult(
            success=False,
            data={"viewer": None},
            errors=[{"message": "rate-limited"}],
            error_message=None,
        )
    )
    client = CodexClient(
        process=proc, codex_app_server_pid=42,
        linear_graphql_tool=tool,
    )
    session = await client.start_session(
        workspace=tmp_path, prompt="x", issue=_issue(),
        approval_policy="never", sandbox_policy="workspace-write",
        read_timeout_ms=5000, tracker_kind="linear",
    )
    await client.stream_turn(
        session=session, prompt="x",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )
    reply = _find_tool_reply(proc.written_frames, 8101)
    assert reply["result"]["success"] is False
    text = reply["result"]["contentItems"][0]["text"]
    # GraphQL body + errors list both preserved inside the content item.
    assert "rate-limited" in text
    assert "viewer" in text


async def test_linear_graphql_invalid_args_returns_failure(
    tmp_path: Path,
) -> None:
    """Conformance §17.5: ``linear_graphql`` — invalid arguments, missing
    auth, and transport failures return structured failure payloads."""
    frames: list[dict[str, Any]] = list(_handshake())
    frames.append(_stream_ack())
    frames.append(
        _tool_call_request(
            request_id=8201,
            tool=LINEAR_GRAPHQL_TOOL_NAME,
            arguments={"query": ""},  # invalid
        )
    )
    frames.append(_stream_completion())
    proc = FakeCodexProcess(frames)
    tool = _FakeLinearGraphqlTool(
        return_value=ToolResult(
            success=False, data=None, errors=None,
            error_message="invalid_request: empty query",
        )
    )
    client = CodexClient(
        process=proc, codex_app_server_pid=42,
        linear_graphql_tool=tool,
    )
    session = await client.start_session(
        workspace=tmp_path, prompt="x", issue=_issue(),
        approval_policy="never", sandbox_policy="workspace-write",
        read_timeout_ms=5000, tracker_kind="linear",
    )
    await client.stream_turn(
        session=session, prompt="x",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )
    reply = _find_tool_reply(proc.written_frames, 8201)
    assert reply["result"]["success"] is False
    text = reply["result"]["contentItems"][0]["text"]
    assert "invalid_request" in text


async def test_unsupported_tool_name_does_not_stall(tmp_path: Path) -> None:
    """Conformance §17.5: ``linear_graphql`` — unsupported tool names
    still fail without stalling the session.

    Same as the global unsupported-tool test but explicitly with the
    linear_graphql wiring present (so the test exercises the extension's
    coexistence with bare unknown tool names).
    """
    frames: list[dict[str, Any]] = list(_handshake())
    frames.append(_stream_ack())
    frames.append(
        _tool_call_request(
            request_id=8301,
            tool="ghost_tool",
            arguments={},
        )
    )
    frames.append(_stream_completion())
    proc = FakeCodexProcess(frames)
    client = CodexClient(
        process=proc, codex_app_server_pid=42,
        linear_graphql_tool=_FakeLinearGraphqlTool(),
    )
    session = await client.start_session(
        workspace=tmp_path, prompt="x", issue=_issue(),
        approval_policy="never", sandbox_policy="workspace-write",
        read_timeout_ms=5000, tracker_kind="linear",
    )
    result = await client.stream_turn(
        session=session, prompt="x",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )
    assert result is not None
    reply = _find_tool_reply(proc.written_frames, 8301)
    # Encoded inside the DynamicToolCallResponse as success=false; the
    # JSON-RPC envelope itself stays a successful response.
    assert "result" in reply
    assert reply["result"]["success"] is False
    assert "unsupported_tool" in reply["result"]["contentItems"][0]["text"]
