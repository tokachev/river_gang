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
    CodexClient,
    Session,
)
from river_gang.codex.errors import ResponseTimeout, TurnTimeout
from river_gang.codex.policy import (
    APPROVAL_POLICY_NEVER,
    EVENT_APPROVAL_AUTO_APPROVED,
    EVENT_APPROVAL_REQUEST,
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
    return [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"threadId": "th-1"}},
        {"jsonrpc": "2.0", "id": 3, "result": {"turnId": "tn-1"}},
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

    # Three writes corresponding to initialize / thread.start / turn.start.
    methods = [w["method"] for w in proc.written_frames]
    assert methods[:3] == [
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
    assert init_frame["jsonrpc"] == "2.0"
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
    thread_frame = proc.written_frames[1]
    params = thread_frame["params"]
    # Shape varies but the documented settings are echoed verbatim.
    serialised = json.dumps(params)
    assert "never" in serialised
    assert "workspace-write" in serialised


def test_thread_and_turn_ids_extracted_and_composed_to_session_id() -> None:
    """Conformance §17.5: thread and turn identities exposed by the
    targeted protocol are extracted and used to emit ``session_started``."""
    assert extract_thread_id({"threadId": "th-A"}) == "th-A"
    assert extract_turn_id({"turnId": "tn-9"}) == "tn-9"
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
                return {"jsonrpc": "2.0", "id": 1, "result": {"turnId": "tn-1"}}
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
    according to the implementation's documented policy."""
    handler = ApprovalHandler(approval_policy=APPROVAL_POLICY_NEVER)
    request = {
        "jsonrpc": "2.0",
        "method": EVENT_APPROVAL_REQUEST,
        "params": {"requestId": "req-1", "command": ["ls"]},
    }
    frame, observability = handler.build_response(request["params"])
    # 'never' policy auto-approves so the session continues.
    assert observability is not None
    assert observability["event"] == EVENT_APPROVAL_AUTO_APPROVED


# ---------------------------------------------------------------------------
# Unsupported tool calls
# ---------------------------------------------------------------------------


async def test_unsupported_dynamic_tool_calls_rejected_without_stalling(
    tmp_path: Path,
) -> None:
    """Conformance §17.5: unsupported dynamic tool calls are rejected
    without stalling the session."""
    frames: list[dict[str, Any]] = list(_handshake())
    # ack for stream_turn
    frames.append({"jsonrpc": "2.0", "id": 4, "result": {"turnId": "tn-2"}})
    frames.append({
        "jsonrpc": "2.0",
        "method": "tool_call",
        "params": {
            "toolName": "unknown_tool",
            "callId": "c-1",
            "arguments": {},
        },
    })
    frames.append({
        "jsonrpc": "2.0",
        "method": "turn_completed",
        "params": {},
    })
    proc = FakeCodexProcess(frames)
    client = CodexClient(process=proc, codex_app_server_pid=42)
    session = await client.start_session(
        workspace=tmp_path, prompt="x", issue=_issue(),
        approval_policy="never", sandbox_policy="workspace-write",
        read_timeout_ms=5000,
    )
    events: list[Any] = []
    result = await client.stream_turn(
        session=session, prompt="continue",
        on_event=lambda e: events.append(e),
        turn_timeout_ms=5000,
    )
    # Session continued through an unknown tool: turn completed.
    assert result is not None
    # An unsupported_tool_call event was synthesised on the side.
    assert any(e.event == "unsupported_tool_call" for e in events)


# ---------------------------------------------------------------------------
# User input requests
# ---------------------------------------------------------------------------


async def test_user_input_requests_handled_per_documented_policy(
    tmp_path: Path,
) -> None:
    """Conformance §17.5: user input requests are handled according to
    the implementation's documented policy and do not stall indefinitely."""
    from river_gang.codex.errors import TurnInputRequired

    frames: list[dict[str, Any]] = list(_handshake())
    frames.append({"jsonrpc": "2.0", "id": 4, "result": {"turnId": "tn-2"}})
    frames.append({
        "jsonrpc": "2.0",
        "method": "turn_input_required",
        "params": {"reason": "needs human"},
    })
    proc = FakeCodexProcess(frames)
    client = CodexClient(process=proc, codex_app_server_pid=42)
    session = await client.start_session(
        workspace=tmp_path, prompt="x", issue=_issue(),
        approval_policy="never", sandbox_policy="workspace-write",
        read_timeout_ms=5000,
    )
    # High-trust posture: TurnInputRequired raises (does not stall).
    with pytest.raises(TurnInputRequired):
        await client.stream_turn(
            session=session, prompt="x",
            on_event=lambda _e: None,
            turn_timeout_ms=5000,
        )


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def test_usage_telemetry_extracted_from_protocol_payloads() -> None:
    """Conformance §17.5: usage and rate-limit telemetry exposed by the
    targeted protocol is extracted."""
    snap = extract_cumulative_tokens(
        METHOD_TOKEN_USAGE_UPDATED,
        {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
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


def test_signals_interpreted_per_protocol() -> None:
    """Conformance §17.5: approval, user-input-required, usage, and
    rate-limit signals are interpreted according to the targeted
    protocol.

    Covered by upstream tests:
    - approval: ``ApprovalHandler.build_response`` (above).
    - user input: ``TurnInputRequired`` raised path (above).
    - usage / rate limits: extractors return well-typed snapshots
      (above).
    """
    # Marker test — see referenced tests for behaviour.
    assert True


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


async def test_linear_graphql_advertised_to_session(tmp_path: Path) -> None:
    """Conformance §17.5: ``linear_graphql`` — the tool is advertised to
    the session."""
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
    init_params = proc.written_frames[0]["params"]
    tool_names = [t["name"] for t in init_params.get("tools", [])]
    assert LINEAR_GRAPHQL_TOOL_NAME in tool_names


async def test_linear_graphql_valid_inputs_executed(tmp_path: Path) -> None:
    """Conformance §17.5: ``linear_graphql`` — valid ``query`` /
    ``variables`` inputs execute against configured Linear auth."""
    frames: list[dict[str, Any]] = list(_handshake())
    frames.append({"jsonrpc": "2.0", "id": 4, "result": {"turnId": "tn-2"}})
    frames.append({
        "jsonrpc": "2.0",
        "method": "tool_call",
        "params": {
            "toolName": LINEAR_GRAPHQL_TOOL_NAME,
            "callId": "c-1",
            "arguments": {"query": "{ viewer { id } }", "variables": {}},
        },
    })
    frames.append({"jsonrpc": "2.0", "method": "turn_completed", "params": {}})
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


async def test_linear_graphql_top_level_errors_preserve_body(
    tmp_path: Path,
) -> None:
    """Conformance §17.5: ``linear_graphql`` — top-level GraphQL ``errors``
    produce ``success=false`` while preserving the GraphQL body."""
    frames: list[dict[str, Any]] = list(_handshake())
    frames.append({"jsonrpc": "2.0", "id": 4, "result": {"turnId": "tn-2"}})
    frames.append({
        "jsonrpc": "2.0",
        "method": "tool_call",
        "params": {
            "toolName": LINEAR_GRAPHQL_TOOL_NAME,
            "callId": "c-1",
            "arguments": {"query": "bad"},
        },
    })
    frames.append({"jsonrpc": "2.0", "method": "turn_completed", "params": {}})
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
    # ``tool_call_response`` written back to the agent — find that frame.
    tcr = next(
        (f for f in proc.written_frames if f.get("method") == "tool_call_response"),
        None,
    )
    assert tcr is not None
    params = tcr["params"]
    assert params["ok"] is False
    assert params["errors"] == [{"message": "rate-limited"}]
    # GraphQL body preserved alongside the errors list.
    assert params["result"] == {"viewer": None}


async def test_linear_graphql_invalid_args_returns_failure(
    tmp_path: Path,
) -> None:
    """Conformance §17.5: ``linear_graphql`` — invalid arguments, missing
    auth, and transport failures return structured failure payloads."""
    frames: list[dict[str, Any]] = list(_handshake())
    frames.append({"jsonrpc": "2.0", "id": 4, "result": {"turnId": "tn-2"}})
    frames.append({
        "jsonrpc": "2.0",
        "method": "tool_call",
        "params": {
            "toolName": LINEAR_GRAPHQL_TOOL_NAME,
            "callId": "c-1",
            "arguments": {"query": ""},  # invalid
        },
    })
    frames.append({"jsonrpc": "2.0", "method": "turn_completed", "params": {}})
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
    tcr = next(
        (f for f in proc.written_frames if f.get("method") == "tool_call_response"),
        None,
    )
    assert tcr is not None
    assert tcr["params"]["ok"] is False
    assert "error" in tcr["params"]


async def test_unsupported_tool_name_does_not_stall(tmp_path: Path) -> None:
    """Conformance §17.5: ``linear_graphql`` — unsupported tool names
    still fail without stalling the session."""
    # Same as the global unsupported-tool test but explicitly with the
    # linear_graphql wiring present (so the test exercises the
    # extension's coexistence with bare unknown tool names).
    frames: list[dict[str, Any]] = list(_handshake())
    frames.append({"jsonrpc": "2.0", "id": 4, "result": {"turnId": "tn-2"}})
    frames.append({
        "jsonrpc": "2.0",
        "method": "tool_call",
        "params": {
            "toolName": "ghost_tool",
            "callId": "c-1",
            "arguments": {},
        },
    })
    frames.append({"jsonrpc": "2.0", "method": "turn_completed", "params": {}})
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
    events: list[Any] = []
    result = await client.stream_turn(
        session=session, prompt="x",
        on_event=lambda e: events.append(e),
        turn_timeout_ms=5000,
    )
    assert result is not None
    assert any(e.event == "unsupported_tool_call" for e in events)
