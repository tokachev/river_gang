"""Tests for :meth:`CodexClient.stream_turn` and :meth:`stop_session`
(SPED §10.3, §10.4, §10.5, §16.5)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from river_gang.codex.client import (
    METHOD_ITEM_TOOL_CALL,
    METHOD_TURN_COMPLETED,
    CodexClient,
    RuntimeEvent,
    Session,
    TurnResult,
)
from river_gang.codex.errors import (
    PortExit,
    TurnCancelled,
    TurnFailed,
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
    """Synchronous turn/start ack — codex 0.125.0+ wraps id under
    ``result.turn``."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"turn": {"id": turn_id, "status": "inProgress"}},
    }


def _evt(method: str, **params: object) -> dict[str, object]:
    """Notification (no id) carrying ``params``."""
    return {"jsonrpc": "2.0", "method": method, "params": dict(params)}


def _turn_completed(
    *,
    turn_id: str = "tn-1",
    status: str = "completed",
    error_message: str | None = None,
    extra_turn_fields: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build a ``turn/completed`` notification (codex 0.125.0+ shape).

    The single notification carries the full Turn object including
    ``status`` (one of completed/failed/interrupted/inProgress) and an
    optional ``error`` (TurnError) populated only when status=failed.
    """
    turn: dict[str, object] = {
        "id": turn_id,
        "status": status,
        "items": [],
    }
    if error_message is not None:
        turn["error"] = {"message": error_message}
    if extra_turn_fields:
        turn.update(extra_turn_fields)
    return _evt(METHOD_TURN_COMPLETED, threadId="th-1", turn=turn)


# ---------------------------------------------------------------------------
# stream_turn — happy path
# ---------------------------------------------------------------------------


async def test_stream_turn_success_returns_turn_result_succeeded() -> None:
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        _evt("notification", text="thinking"),
        _turn_completed(turn_id="tn-1"),
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
    assert result.completion_event == METHOD_TURN_COMPLETED
    # Both notifications + completion delivered in order
    assert [e.event for e in received] == ["notification", METHOD_TURN_COMPLETED]


async def test_stream_turn_propagates_runtime_event_shape() -> None:
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        _evt("notification", text="hi"),
        _turn_completed(),
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
    fake.queue(_ack(turn_id="tn-1"), _turn_completed())
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
    assert request["params"]["input"] == [
        {"type": "text", "text": "full prompt body"}
    ]


async def test_stream_turn_continuation_writes_guidance_no_prompt() -> None:
    fake = FakeCodexProcess()
    fake.queue(_ack(turn_id="tn-2"), _turn_completed(turn_id="tn-2"))
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
    assert request["params"]["input"] == [
        {"type": "text", "text": "please continue"}
    ]


# ---------------------------------------------------------------------------
# stream_turn — failure classification
# ---------------------------------------------------------------------------


async def test_stream_turn_failed_status_raises_turn_failed_with_message() -> None:
    """Codex 0.125.0+: a single ``turn/completed`` notification with
    ``status=failed`` carries the error message at ``params.turn.error.message``.
    """
    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _turn_completed(status="failed", error_message="agent crashed"),
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


async def test_stream_turn_failed_status_without_error_object_still_raises() -> None:
    """``status=failed`` without an error sub-object still surfaces TurnFailed
    with a default message — never silently succeed."""
    fake = FakeCodexProcess()
    fake.queue(_ack(), _turn_completed(status="failed"))
    client = CodexClient(process=fake, codex_app_server_pid=1)

    with pytest.raises(TurnFailed):
        await client.stream_turn(
            session=_session(),
            prompt="x",
            on_event=lambda _e: None,
            turn_timeout_ms=5000,
        )


async def test_stream_turn_jsonrpc_error_ack_raises_turn_failed() -> None:
    """A JSON-RPC error response to the streaming ``turn/start`` ack must
    surface as :class:`TurnFailed` (not :class:`ResponseError`).

    worker.py only catches TurnFailed/TurnCancelled/TurnTimeout/PortExit on
    the streaming path — a ResponseError leaking out of stream_turn would
    propagate uncaught and break the typed-exit contract.
    """
    fake = FakeCodexProcess()
    fake.queue(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32000, "message": "bad turn"},
        }
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)

    with pytest.raises(TurnFailed) as exc:
        await client.stream_turn(
            session=_session(),
            prompt="x",
            on_event=lambda _e: None,
            turn_timeout_ms=5000,
        )
    assert "bad turn" in str(exc.value)
    assert "-32000" in str(exc.value)


async def test_stream_turn_interrupted_status_raises_turn_cancelled() -> None:
    """``status=interrupted`` (operator/timeout-driven) → TurnCancelled."""
    fake = FakeCodexProcess()
    fake.queue(_ack(), _turn_completed(status="interrupted"))
    client = CodexClient(process=fake, codex_app_server_pid=1)

    with pytest.raises(TurnCancelled):
        await client.stream_turn(
            session=_session(),
            prompt="x",
            on_event=lambda _e: None,
            turn_timeout_ms=5000,
        )


async def test_stream_turn_in_progress_status_continues_streaming() -> None:
    """``status=inProgress`` is a heartbeat — the stream must keep waiting
    for a terminal status, not return early."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _turn_completed(status="inProgress"),
        _turn_completed(status="completed"),
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
    # Both turn/completed events were forwarded to on_event before resolution.
    assert [e.event for e in received] == [METHOD_TURN_COMPLETED, METHOD_TURN_COMPLETED]


async def test_stream_turn_completed_missing_turn_object_raises() -> None:
    """A ``turn/completed`` without the required ``turn`` object is protocol
    drift — surfaces TurnFailed instead of silently succeeding."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        # Notification missing the ``turn`` object entirely.
        _evt(METHOD_TURN_COMPLETED, threadId="th-1"),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)

    with pytest.raises(TurnFailed) as exc:
        await client.stream_turn(
            session=_session(),
            prompt="x",
            on_event=lambda _e: None,
            turn_timeout_ms=5000,
        )
    assert "missing" in str(exc.value).lower()


async def test_stream_turn_completed_unknown_status_raises() -> None:
    """Unknown ``turn.status`` is protocol drift — surfaces TurnFailed
    instead of treating as completed (which would mask schema regressions)."""
    fake = FakeCodexProcess()
    fake.queue(_ack(), _turn_completed(status="bogus_status"))
    client = CodexClient(process=fake, codex_app_server_pid=1)

    with pytest.raises(TurnFailed) as exc:
        await client.stream_turn(
            session=_session(),
            prompt="x",
            on_event=lambda _e: None,
            turn_timeout_ms=5000,
        )
    assert "unknown status" in str(exc.value).lower()
    assert "bogus_status" in str(exc.value)


async def test_stream_turn_top_level_error_notification_raises_turn_failed() -> None:
    """A top-level ``error`` notification with ``willRetry=False`` is a
    session-level fatal — surfaces TurnFailed carrying the error message."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _evt(
            "error",
            threadId="th-1",
            turnId="tn-1",
            willRetry=False,
            error={"message": "session blew up"},
        ),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)

    with pytest.raises(TurnFailed) as exc:
        await client.stream_turn(
            session=_session(),
            prompt="x",
            on_event=lambda _e: None,
            turn_timeout_ms=5000,
        )
    assert "session blew up" in str(exc.value)


async def test_stream_turn_top_level_error_with_will_retry_true_continues() -> None:
    """``ErrorNotification.willRetry=true`` means codex intends to retry the
    turn itself. Aborting on every error frame would race the retry and
    surface a spurious failure — surface as a notification beat (delivered
    via on_event) and keep streaming until codex emits the next
    ``turn/completed``."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _evt(
            "error",
            threadId="th-1",
            turnId="tn-1",
            willRetry=True,
            error={"message": "transient — retrying"},
        ),
        # Codex retries and ultimately succeeds.
        _turn_completed(turn_id="tn-1", status="completed"),
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
    # The error notification was still delivered to on_event (audit trail).
    assert any(e.event == "error" for e in received)


async def test_stream_turn_top_level_error_with_will_retry_absent_fails_closed() -> None:
    """If ``willRetry`` is absent (schema requires it but defensive against
    drift), fail closed: treat as a non-retryable error and raise
    TurnFailed. Silently continuing on a malformed error frame would mask
    schema regressions in production."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _evt(
            "error",
            threadId="th-1",
            turnId="tn-1",
            error={"message": "no willRetry field"},
            # NOTE: no willRetry key.
        ),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)

    with pytest.raises(TurnFailed) as exc:
        await client.stream_turn(
            session=_session(),
            prompt="x",
            on_event=lambda _e: None,
            turn_timeout_ms=5000,
        )
    assert "no willRetry field" in str(exc.value)


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
        _turn_completed(turn_id="tn-1"),
        _ack(turn_id="tn-2", request_id=2),
        _turn_completed(turn_id="tn-2"),
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


def _tool_call_request(
    *,
    request_id: int,
    tool: str,
    arguments: object,
    call_id: str = "c-1",
) -> dict[str, object]:
    """Build a codex 0.125.0+ ``item/tool/call`` server-request frame."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": METHOD_ITEM_TOOL_CALL,
        "params": {
            "tool": tool,
            "arguments": arguments,
            "callId": call_id,
            "threadId": "th-1",
            "turnId": "tn-1",
        },
    }


async def test_stream_turn_unsupported_tool_call_replies_with_success_false() -> None:
    """SPED §10.5: unknown dynamic tool returns a DynamicToolCallResponse
    with ``success=false`` and an ``unsupported_tool: ...`` content item;
    the session does NOT crash and codex unblocks immediately."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        _tool_call_request(
            request_id=42,
            tool="bogus_tool",
            arguments={"x": 1},
            call_id="call-7",
        ),
        _turn_completed(),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)

    result = await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )

    assert isinstance(result, TurnResult)
    replies = [
        f
        for f in fake.written_frames
        if f.get("id") == 42 and "method" not in f
    ]
    assert len(replies) == 1
    reply = replies[0]
    assert "result" in reply
    assert reply["result"]["success"] is False
    assert "unsupported_tool" in reply["result"]["contentItems"][0]["text"]


async def test_stream_turn_wired_linear_tool_replies_with_success() -> None:
    """When ``linear_graphql_tool`` is wired, an ``item/tool/call`` request
    for ``linear_graphql`` is dispatched to the tool — the JSON-RPC reply is
    a SUCCESS shape (``result.success=True``), NOT an unknown-tool error.
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
        _tool_call_request(
            request_id=51,
            tool="linear_graphql",
            arguments={},
            call_id="c1",
        ),
        _turn_completed(),
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

    replies = [
        f
        for f in fake.written_frames
        if f.get("id") == 51 and "method" not in f
    ]
    assert len(replies) == 1
    reply = replies[0]
    assert "result" in reply
    assert reply["result"]["success"] is True
    assert isinstance(reply["result"]["contentItems"], list)


# ---------------------------------------------------------------------------
# stop_session
# ---------------------------------------------------------------------------


async def test_stop_session_does_not_write_shutdown_notification() -> None:
    """ClientNotification (codex 0.125.0+ schema) only allows ``initialized``;
    no ``shutdown`` notification exists. ``stop_session`` relies on OS-level
    termination via ``aclose`` and MUST NOT emit a non-schema frame."""
    fake = FakeCodexProcess()
    client = CodexClient(process=fake, codex_app_server_pid=1)

    await client.stop_session(_session(), graceful_timeout_s=1.0)

    methods = [f.get("method") for f in fake.written_frames]
    assert "shutdown" not in methods
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
        _turn_completed(
            turn_id="tn-1",
            extra_turn_fields={"durationMs": 1234},
        ),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)
    result = await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )
    # turn/completed payload carries the full Turn object; durationMs is
    # threaded through ``params.turn``.
    assert result.payload["turn"]["id"] == "tn-1"
    assert result.payload["turn"]["status"] == "completed"
    assert result.payload["turn"]["durationMs"] == 1234


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
    assert result.completion_event == METHOD_TURN_COMPLETED
