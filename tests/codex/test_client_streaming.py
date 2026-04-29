"""Tests for :meth:`CodexClient.stream_turn` and :meth:`stop_session`
(SPED §10.3, §10.4, §10.5, §16.5)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from river_gang.codex.client import CodexClient, RuntimeEvent, Session, TurnResult
from river_gang.codex.errors import (
    PortExit,
    TurnCancelled,
    TurnFailed,
    TurnInputRequired,
    TurnTimeout,
)
from river_gang.codex.protocol import (
    METHOD_TURN_START,
)
from tests.codex.fakes import FakeCodexProcess

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _session() -> Session:
    return Session(
        thread_id="th-1",
        first_turn_id="tn-0",
        codex_app_server_pid=12345,
        started_at=datetime.now(UTC),
    )


def _ack(turn_id: str = "tn-1", *, request_id: int = 1) -> dict[str, object]:
    """Synchronous turn.start ack — JSON-RPC response with id."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"turnId": turn_id},
    }


def _evt(method: str, **params: object) -> dict[str, object]:
    """Notification (no id) carrying ``params``."""
    return {"jsonrpc": "2.0", "method": method, "params": dict(params)}


# ---------------------------------------------------------------------------
# stream_turn — happy path
# ---------------------------------------------------------------------------


async def test_stream_turn_success_returns_turn_result_succeeded() -> None:
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        _evt("notification", text="thinking"),
        _evt("turn_completed", summary="all done"),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)
    received: list[RuntimeEvent] = []

    result = await client.stream_turn(
        session=_session(),
        prompt="continue",
        on_event=received.append,
        turn_timeout_ms=5000,
    )

    assert isinstance(result, TurnResult)
    assert result.turn_id == "tn-1"
    assert result.completion_event == "turn_completed"
    assert result.payload == {"summary": "all done"}
    # Both notifications + completion delivered in order
    assert [e.event for e in received] == ["notification", "turn_completed"]


async def test_stream_turn_propagates_runtime_event_shape() -> None:
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        _evt("notification", text="hi"),
        _evt("turn_completed"),
    )
    client = CodexClient(process=fake, codex_app_server_pid=999)
    received: list[RuntimeEvent] = []
    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=received.append,
        turn_timeout_ms=5000,
    )

    first = received[0]
    assert first.event == "notification"
    assert first.codex_app_server_pid == 999
    assert isinstance(first.timestamp, datetime)
    assert first.timestamp.tzinfo is not None
    assert first.payload == {"text": "hi"}
    assert first.usage is None  # populated in Task 19


async def test_stream_turn_writes_first_turn_request_with_prompt() -> None:
    fake = FakeCodexProcess()
    fake.queue(_ack(turn_id="tn-1"), _evt("turn_completed"))
    client = CodexClient(process=fake, codex_app_server_pid=1)

    await client.stream_turn(
        session=_session(),
        prompt="full prompt body",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
        is_first_turn=True,
    )

    request = fake.written_frames[0]
    assert request["method"] == METHOD_TURN_START
    assert request["params"]["threadId"] == "th-1"
    assert request["params"]["prompt"] == "full prompt body"
    assert "guidance" not in request["params"]


async def test_stream_turn_continuation_writes_guidance_no_prompt() -> None:
    fake = FakeCodexProcess()
    fake.queue(_ack(turn_id="tn-2"), _evt("turn_completed"))
    client = CodexClient(process=fake, codex_app_server_pid=1)

    await client.stream_turn(
        session=_session(),
        prompt="please continue",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
        is_first_turn=False,
    )

    request = fake.written_frames[0]
    assert request["method"] == METHOD_TURN_START
    assert request["params"]["guidance"] == "please continue"
    assert "prompt" not in request["params"]


# ---------------------------------------------------------------------------
# stream_turn — failure classification
# ---------------------------------------------------------------------------


async def test_stream_turn_failure_event_raises_turn_failed() -> None:
    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _evt("turn_failed", reason="agent crashed"),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)

    with pytest.raises(TurnFailed) as exc:
        await client.stream_turn(
            session=_session(),
            prompt="x",
            on_event=lambda _e: None,
            turn_timeout_ms=5000,
        )
    assert "agent crashed" in str(exc.value)


async def test_stream_turn_ended_with_error_also_raises_turn_failed() -> None:
    fake = FakeCodexProcess()
    fake.queue(_ack(), _evt("turn_ended_with_error", code="x"))
    client = CodexClient(process=fake, codex_app_server_pid=1)

    with pytest.raises(TurnFailed):
        await client.stream_turn(
            session=_session(),
            prompt="x",
            on_event=lambda _e: None,
            turn_timeout_ms=5000,
        )


async def test_stream_turn_cancelled_event_raises_turn_cancelled() -> None:
    fake = FakeCodexProcess()
    fake.queue(_ack(), _evt("turn_cancelled", by="operator"))
    client = CodexClient(process=fake, codex_app_server_pid=1)

    with pytest.raises(TurnCancelled):
        await client.stream_turn(
            session=_session(),
            prompt="x",
            on_event=lambda _e: None,
            turn_timeout_ms=5000,
        )


async def test_stream_turn_user_input_required_raises_turn_input_required() -> None:
    """SPED §10.5 high-trust posture: user-input-required = hard failure."""
    fake = FakeCodexProcess()
    fake.queue(_ack(), _evt("turn_input_required", question="?"))
    client = CodexClient(process=fake, codex_app_server_pid=1)

    with pytest.raises(TurnInputRequired):
        await client.stream_turn(
            session=_session(),
            prompt="x",
            on_event=lambda _e: None,
            turn_timeout_ms=5000,
        )


async def test_stream_turn_subprocess_exit_during_stream_raises_port_exit() -> None:
    fake = FakeCodexProcess()
    fake.queue(_ack())  # ack but no completion event; FakeCodex EOF -> PortExit
    client = CodexClient(process=fake, codex_app_server_pid=1)

    with pytest.raises(PortExit):
        await client.stream_turn(
            session=_session(),
            prompt="x",
            on_event=lambda _e: None,
            turn_timeout_ms=5000,
        )


async def test_stream_turn_timeout_raises_turn_timeout() -> None:
    """Stream that never produces a completion event must surface
    :class:`TurnTimeout` after ``turn_timeout_ms``."""

    class StallingFake(FakeCodexProcess):
        def __init__(self) -> None:
            super().__init__()
            self._first_call = True

        async def read_frame(self) -> dict[str, object]:
            if self._first_call:
                self._first_call = False
                return _ack()
            await asyncio.sleep(60)
            raise AssertionError("unreachable")

    fake = StallingFake()
    client = CodexClient(process=fake, codex_app_server_pid=1)

    with pytest.raises(TurnTimeout):
        await client.stream_turn(
            session=_session(),
            prompt="x",
            on_event=lambda _e: None,
            turn_timeout_ms=50,
        )


# ---------------------------------------------------------------------------
# Continuation: same session, multiple turns
# ---------------------------------------------------------------------------


async def test_stream_turn_continuation_after_success_reuses_session() -> None:
    fake = FakeCodexProcess()
    # Two complete turn cycles back-to-back.
    fake.queue(
        _ack(turn_id="tn-1", request_id=1),
        _evt("turn_completed", n=1),
        _ack(turn_id="tn-2", request_id=2),
        _evt("turn_completed", n=2),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)
    sess = _session()

    r1 = await client.stream_turn(
        session=sess, prompt="first", on_event=lambda _e: None,
        turn_timeout_ms=5000, is_first_turn=False,
    )
    r2 = await client.stream_turn(
        session=sess, prompt="continue", on_event=lambda _e: None,
        turn_timeout_ms=5000, is_first_turn=False,
    )

    assert r1.turn_id == "tn-1"
    assert r2.turn_id == "tn-2"
    # Both rounds emit a single turn.start request with monotonic ids
    request_ids = [f["id"] for f in fake.written_frames]
    assert request_ids == [1, 2]


# ---------------------------------------------------------------------------
# Unsupported tool call — write failure, keep streaming
# ---------------------------------------------------------------------------


async def test_stream_turn_unsupported_tool_call_writes_failure_and_continues() -> None:
    """SPED §10.5: unknown dynamic tool returns a tool failure response,
    session does NOT crash."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        _evt(
            "tool_call",
            callId="call-7",
            toolName="bogus_tool",
            arguments={"x": 1},
        ),
        _evt("turn_completed"),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)
    received: list[RuntimeEvent] = []

    result = await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=received.append,
        turn_timeout_ms=5000,
    )

    assert isinstance(result, TurnResult)
    # We wrote: turn.start request + a tool_call_response failure frame.
    methods = [f["method"] for f in fake.written_frames]
    assert "tool_call_response" in methods
    failure_frame = next(
        f for f in fake.written_frames if f["method"] == "tool_call_response"
    )
    assert failure_frame["params"]["callId"] == "call-7"
    assert failure_frame["params"]["ok"] is False
    assert "unsupported_tool" in failure_frame["params"]["error"]
    # The unsupported_tool_call event was forwarded to on_event for observability.
    assert any(e.event == "unsupported_tool_call" for e in received)


async def test_stream_turn_unsupported_tool_call_without_call_id_still_replies() -> None:
    """An unsupported tool_call with NO ``callId`` must still get a
    ``tool_call_response`` so the agent's pending-tool-call entry can
    unblock — otherwise the session hangs forever."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        # Note: intentionally no callId in params
        _evt("tool_call", toolName="bogus_tool", arguments={"x": 1}),
        _evt("turn_completed"),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)
    received: list[RuntimeEvent] = []

    result = await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=received.append,
        turn_timeout_ms=5000,
    )

    assert isinstance(result, TurnResult)
    response_frames = [
        f for f in fake.written_frames if f["method"] == "tool_call_response"
    ]
    assert len(response_frames) == 1
    params = response_frames[0]["params"]
    # callId surfaces as None when the original event omitted it.
    assert params["callId"] is None
    assert params["ok"] is False
    assert "unsupported_tool" in params["error"]


async def test_stream_turn_wired_linear_tool_does_not_emit_unsupported_failure() -> None:
    """When ``linear_graphql_tool`` is wired (Task 24), ``tool_call`` for
    ``linear_graphql`` is dispatched to the tool — the response frame is
    a SUCCESS shape (``ok=True``), NOT an ``unsupported_tool`` failure.
    """
    from river_gang.tools.linear_graphql import ToolResult

    class _NoopTool:
        async def execute(self, raw_input: object) -> ToolResult:
            return ToolResult(
                success=True, data={"viewer": None}, errors=None, error_message=None
            )

    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _evt("tool_call", callId="c1", toolName="linear_graphql", arguments={}),
        _evt("turn_completed"),
    )
    client = CodexClient(
        process=fake,
        codex_app_server_pid=1,
        linear_graphql_tool=_NoopTool(),  # type: ignore[arg-type]
    )
    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )

    response = next(
        f for f in fake.written_frames if f.get("method") == "tool_call_response"
    )
    assert response["params"]["ok"] is True
    assert response["params"].get("error") is None
    assert "unsupported_tool" not in str(response["params"])


# ---------------------------------------------------------------------------
# stop_session
# ---------------------------------------------------------------------------


async def test_stop_session_writes_shutdown_notification() -> None:
    fake = FakeCodexProcess()
    client = CodexClient(process=fake, codex_app_server_pid=1)

    await client.stop_session(_session(), graceful_timeout_s=1.0)

    methods = [f["method"] for f in fake.written_frames]
    assert "shutdown" in methods
    shutdown_frame = next(f for f in fake.written_frames if f["method"] == "shutdown")
    # shutdown is a notification — no id in the JSON-RPC envelope
    assert "id" not in shutdown_frame
    assert fake.closed is True


async def test_stop_session_clean_exit_no_force_kill() -> None:
    """Subprocess exits cleanly during graceful window → no force-kill needed."""
    fake = FakeCodexProcess(exit_code=0)
    client = CodexClient(process=fake, codex_app_server_pid=1)

    await client.stop_session(_session(), graceful_timeout_s=2.0)

    assert fake.closed is True
    assert fake.kill_called is False  # surfaced by the fake


async def test_stop_session_forced_kill_when_graceful_ignored() -> None:
    """Subprocess ignores graceful → stop_session escalates to terminate/kill."""

    class StubbornFake(FakeCodexProcess):
        def __init__(self) -> None:
            super().__init__()
            self.kill_called = False

        async def wait_for_exit(self, timeout: float) -> bool:
            # Always pretend graceful window expired.
            return False

        async def aclose(self) -> None:
            self.kill_called = True
            await super().aclose()

    fake = StubbornFake()
    client = CodexClient(process=fake, codex_app_server_pid=1)

    await client.stop_session(_session(), graceful_timeout_s=0.1)

    assert fake.kill_called is True


async def test_stop_session_idempotent() -> None:
    fake = FakeCodexProcess()
    client = CodexClient(process=fake, codex_app_server_pid=1)

    await client.stop_session(_session())
    await client.stop_session(_session())

    # Second call must not crash even though process is already closed.
    assert fake.closed is True


async def test_stop_session_swallows_write_errors() -> None:
    """If shutdown notification can't be sent (broken pipe), ``stop_session``
    must still close the process — never let a stop bubble an error."""

    class BrokenWriteFake(FakeCodexProcess):
        async def write_frame(self, payload: dict[str, object]) -> None:
            raise BrokenPipeError("pipe closed")

    fake = BrokenWriteFake()
    client = CodexClient(process=fake, codex_app_server_pid=1)

    # Must not raise.
    await client.stop_session(_session(), graceful_timeout_s=0.1)
    assert fake.closed is True


# ---------------------------------------------------------------------------
# stream_turn read timeout vs turn timeout
# ---------------------------------------------------------------------------


async def test_stream_turn_response_includes_completion_payload() -> None:
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        _evt("turn_completed", summary="ok", duration_ms=1234),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)
    result = await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )
    assert result.payload == {"summary": "ok", "duration_ms": 1234}


# ---------------------------------------------------------------------------
# RuntimeEvent dataclass shape
# ---------------------------------------------------------------------------


def test_runtime_event_is_frozen_dataclass() -> None:
    e = RuntimeEvent(
        event="x",
        timestamp=datetime.now(UTC),
        codex_app_server_pid=1,
        payload={"a": 1},
        usage=None,
    )
    with pytest.raises(Exception):
        e.event = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TurnResult dataclass shape
# ---------------------------------------------------------------------------


def test_turn_result_succeeded_factory() -> None:
    result = TurnResult.succeeded(turn_id="tn-1", payload={"k": 1})
    assert isinstance(result, TurnResult)
    assert result.turn_id == "tn-1"
    assert result.payload == {"k": 1}
    assert result.completion_event == "turn_completed"
