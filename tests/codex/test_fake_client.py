"""Tests for :class:`FakeCodexClient` (test-infrastructure fake)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from river_gang.codex.client import RuntimeEvent, Session, TurnResult
from river_gang.codex.errors import (
    PortExit,
    TurnCancelled,
    TurnFailed,
    TurnInputRequired,
    TurnTimeout,
)
from river_gang.tracker.issue import Issue
from tests.codex.fakes import FakeCodexClient, TurnScenario


def _issue() -> Issue:
    return Issue(
        id="uuid-1",
        identifier="RG-1",
        title="t",
        state="Todo",
        description=None,
        priority=None,
        branch_name=None,
        url=None,
        labels=(),
        blocked_by=(),
        created_at=None,
        updated_at=None,
    )


# ---------------------------------------------------------------------------
# start_session
# ---------------------------------------------------------------------------


async def test_start_session_returns_synthetic_session(tmp_path: Path) -> None:
    fake = FakeCodexClient()
    session = await fake.start_session(
        workspace=tmp_path,
        prompt="P",
        issue=_issue(),
        approval_policy="never",
        sandbox_policy="workspace-write",
        read_timeout_ms=5000,
    )
    assert isinstance(session, Session)
    assert session.thread_id  # non-empty
    assert session.first_turn_id  # non-empty
    assert session.codex_app_server_pid > 0
    assert isinstance(session.started_at, datetime)
    assert session.started_at.tzinfo is not None


async def test_start_session_records_call(tmp_path: Path) -> None:
    fake = FakeCodexClient()
    await fake.start_session(
        workspace=tmp_path,
        prompt="render-prompt",
        issue=_issue(),
        approval_policy="never",
        sandbox_policy="workspace-write",
        read_timeout_ms=5000,
        tracker_kind="linear",
    )

    assert len(fake.calls) == 1
    method, args = fake.calls[0]
    assert method == "start_session"
    assert args["workspace"] == tmp_path
    assert args["prompt"] == "render-prompt"
    assert args["approval_policy"] == "never"
    assert args["sandbox_policy"] == "workspace-write"
    assert args["read_timeout_ms"] == 5000
    assert args["tracker_kind"] == "linear"


async def test_start_session_default_thread_and_turn_ids_match_session_id(
    tmp_path: Path,
) -> None:
    fake = FakeCodexClient(thread_id="th-X", first_turn_id="tn-Y")
    session = await fake.start_session(
        workspace=tmp_path,
        prompt="P",
        issue=_issue(),
        approval_policy="never",
        sandbox_policy="workspace-write",
        read_timeout_ms=5000,
    )
    assert session.thread_id == "th-X"
    assert session.first_turn_id == "tn-Y"
    assert session.session_id == "th-X-tn-Y"


async def test_start_session_failure_injection_raises(tmp_path: Path) -> None:
    fake = FakeCodexClient(start_error=RuntimeError("startup-failed"))
    with pytest.raises(RuntimeError) as exc:
        await fake.start_session(
            workspace=tmp_path,
            prompt="P",
            issue=_issue(),
            approval_policy="never",
            sandbox_policy="workspace-write",
            read_timeout_ms=5000,
        )
    assert "startup-failed" in str(exc.value)
    # Call still recorded for ordering assertions
    assert fake.calls == [
        (
            "start_session",
            {
                "workspace": tmp_path,
                "prompt": "P",
                "issue": _issue(),
                "approval_policy": "never",
                "sandbox_policy": "workspace-write",
                "read_timeout_ms": 5000,
                "tracker_kind": None,
            },
        )
    ]


# ---------------------------------------------------------------------------
# stream_turn — scenario consumption
# ---------------------------------------------------------------------------


def _session() -> Session:
    return Session(
        thread_id="th-1",
        first_turn_id="tn-0",
        codex_app_server_pid=1,
        started_at=datetime.now(UTC),
    )


async def test_stream_turn_scenario_completed_returns_turn_result() -> None:
    fake = FakeCodexClient()
    fake.queue_turn(
        TurnScenario(
            events=[{"event": "notification", "payload": {"text": "thinking"}}],
            outcome="completed",
            completion_payload={"summary": "ok"},
        )
    )

    received: list[RuntimeEvent] = []
    result = await fake.stream_turn(
        session=_session(),
        prompt="x",
        on_event=received.append,
        turn_timeout_ms=5000,
    )
    assert isinstance(result, TurnResult)
    assert result.payload == {"summary": "ok"}
    assert [e.event for e in received] == ["notification", "turn_completed"]


async def test_stream_turn_scenario_failed_raises_turn_failed() -> None:
    fake = FakeCodexClient()
    fake.queue_turn(TurnScenario(events=[], outcome="failed", reason="agent crashed"))
    with pytest.raises(TurnFailed) as exc:
        await fake.stream_turn(
            session=_session(),
            prompt="x",
            on_event=lambda _e: None,
            turn_timeout_ms=5000,
        )
    assert "agent crashed" in str(exc.value)


async def test_stream_turn_scenario_cancelled_raises_turn_cancelled() -> None:
    fake = FakeCodexClient()
    fake.queue_turn(TurnScenario(events=[], outcome="cancelled"))
    with pytest.raises(TurnCancelled):
        await fake.stream_turn(
            session=_session(),
            prompt="x",
            on_event=lambda _e: None,
            turn_timeout_ms=5000,
        )


async def test_stream_turn_scenario_input_required_raises_turn_input_required() -> None:
    fake = FakeCodexClient()
    fake.queue_turn(TurnScenario(events=[], outcome="input_required"))
    with pytest.raises(TurnInputRequired):
        await fake.stream_turn(
            session=_session(),
            prompt="x",
            on_event=lambda _e: None,
            turn_timeout_ms=5000,
        )


async def test_stream_turn_scenario_timeout_raises_turn_timeout() -> None:
    fake = FakeCodexClient()
    fake.queue_turn(TurnScenario(events=[], outcome="timeout"))
    with pytest.raises(TurnTimeout):
        await fake.stream_turn(
            session=_session(),
            prompt="x",
            on_event=lambda _e: None,
            turn_timeout_ms=5000,
        )


async def test_stream_turn_scenario_port_exit_raises() -> None:
    fake = FakeCodexClient()
    fake.queue_turn(TurnScenario(events=[], outcome="port_exit"))
    with pytest.raises(PortExit):
        await fake.stream_turn(
            session=_session(),
            prompt="x",
            on_event=lambda _e: None,
            turn_timeout_ms=5000,
        )


async def test_stream_turn_consumes_one_scenario_per_call() -> None:
    fake = FakeCodexClient()
    fake.queue_turn(TurnScenario(events=[], outcome="completed"))
    fake.queue_turn(TurnScenario(events=[], outcome="completed"))
    fake.queue_turn(TurnScenario(events=[], outcome="failed"))

    await fake.stream_turn(
        session=_session(), prompt="a", on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )
    await fake.stream_turn(
        session=_session(), prompt="b", on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )
    with pytest.raises(TurnFailed):
        await fake.stream_turn(
            session=_session(), prompt="c", on_event=lambda _e: None,
            turn_timeout_ms=5000,
        )


async def test_stream_turn_with_no_queued_scenarios_raises() -> None:
    fake = FakeCodexClient()
    with pytest.raises(AssertionError) as exc:
        await fake.stream_turn(
            session=_session(),
            prompt="x",
            on_event=lambda _e: None,
            turn_timeout_ms=5000,
        )
    assert "scenario" in str(exc.value).lower()


async def test_stream_turn_records_call() -> None:
    fake = FakeCodexClient()
    fake.queue_turn(TurnScenario(events=[], outcome="completed"))
    sess = _session()
    await fake.stream_turn(
        session=sess,
        prompt="continuation guidance",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
        is_first_turn=False,
    )

    assert len(fake.calls) == 1
    method, args = fake.calls[0]
    assert method == "stream_turn"
    assert args["session"] is sess
    assert args["prompt"] == "continuation guidance"
    assert args["turn_timeout_ms"] == 5000
    assert args["is_first_turn"] is False


async def test_stream_turn_event_payloads_become_runtime_events() -> None:
    fake = FakeCodexClient(codex_app_server_pid=42)
    fake.queue_turn(
        TurnScenario(
            events=[
                {"event": "notification", "payload": {"text": "hi"}},
                {
                    "event": "thread/tokenUsage/updated",
                    "payload": {
                        "input_tokens": 10,
                        "output_tokens": 20,
                        "total_tokens": 30,
                    },
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 20,
                        "total_tokens": 30,
                    },
                },
            ],
            outcome="completed",
        )
    )

    received: list[RuntimeEvent] = []
    await fake.stream_turn(
        session=_session(),
        prompt="x",
        on_event=received.append,
        turn_timeout_ms=5000,
    )
    notification = received[0]
    assert notification.event == "notification"
    assert notification.codex_app_server_pid == 42
    assert notification.payload == {"text": "hi"}
    assert notification.usage is None

    token_event = received[1]
    assert token_event.event == "thread/tokenUsage/updated"
    assert token_event.usage == {
        "input_tokens": 10,
        "output_tokens": 20,
        "total_tokens": 30,
    }


# ---------------------------------------------------------------------------
# stop_session
# ---------------------------------------------------------------------------


async def test_stop_session_records_call() -> None:
    fake = FakeCodexClient()
    sess = _session()
    await fake.stop_session(sess, graceful_timeout_s=2.0)
    assert fake.calls[-1] == (
        "stop_session",
        {"session": sess, "graceful_timeout_s": 2.0},
    )


async def test_stop_session_idempotent() -> None:
    fake = FakeCodexClient()
    sess = _session()
    await fake.stop_session(sess)
    await fake.stop_session(sess)
    # Both calls recorded — caller may want to verify the worker actually
    # called stop on every exit branch.
    stop_calls = [c for c in fake.calls if c[0] == "stop_session"]
    assert len(stop_calls) == 2


async def test_stop_session_never_raises_even_if_configured_to_fail() -> None:
    """Mirror the real :meth:`CodexClient.stop_session` contract: never
    raises so the worker's exit branches can call it unconditionally."""
    fake = FakeCodexClient(stop_error=RuntimeError("ignored"))
    await fake.stop_session(_session())  # must not raise


# ---------------------------------------------------------------------------
# .calls preserves cross-method ordering
# ---------------------------------------------------------------------------


async def test_calls_preserves_order_across_lifecycle(tmp_path: Path) -> None:
    fake = FakeCodexClient()
    fake.queue_turn(TurnScenario(events=[], outcome="completed"))

    await fake.start_session(
        workspace=tmp_path,
        prompt="P",
        issue=_issue(),
        approval_policy="never",
        sandbox_policy="workspace-write",
        read_timeout_ms=5000,
    )
    sess = _session()
    await fake.stream_turn(
        session=sess, prompt="x", on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )
    await fake.stop_session(sess)

    methods = [m for m, _ in fake.calls]
    assert methods == ["start_session", "stream_turn", "stop_session"]
