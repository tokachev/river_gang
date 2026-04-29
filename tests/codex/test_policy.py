"""Tests for :class:`ApprovalHandler` (SPED §10.5, §15.1)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from river_gang.codex.client import CodexClient, RuntimeEvent, Session
from river_gang.codex.errors import TurnInputRequired
from river_gang.codex.policy import (
    APPROVAL_POLICY_NEVER,
    EVENT_APPROVAL_AUTO_APPROVED,
    EVENT_APPROVAL_DENIED,
    EVENT_APPROVAL_REQUEST,
    METHOD_APPROVAL_RESPONSE,
    SANDBOX_POLICY_WORKSPACE_WRITE,
    ApprovalHandler,
)
from river_gang.codex.protocol import METHOD_THREAD_START, METHOD_TURN_START
from river_gang.tracker.issue import Issue
from tests.codex.fakes import FakeCodexProcess

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------


def test_constants_match_spec_strings() -> None:
    assert APPROVAL_POLICY_NEVER == "never"
    assert SANDBOX_POLICY_WORKSPACE_WRITE == "workspace-write"
    assert EVENT_APPROVAL_REQUEST == "approval_request"
    assert EVENT_APPROVAL_AUTO_APPROVED == "approval_auto_approved"
    assert EVENT_APPROVAL_DENIED == "approval_denied"
    assert METHOD_APPROVAL_RESPONSE == "approval_response"


# ---------------------------------------------------------------------------
# ApprovalHandler — pure builder
# ---------------------------------------------------------------------------


def test_handler_never_policy_emits_approve_response_frame() -> None:
    handler = ApprovalHandler(approval_policy=APPROVAL_POLICY_NEVER)
    response, event = handler.build_response(
        {"approvalId": "ap-1", "kind": "command_execution", "command": "ls"}
    )

    assert response["jsonrpc"] == "2.0"
    assert response["method"] == METHOD_APPROVAL_RESPONSE
    assert "id" not in response  # notification — no id
    assert response["params"]["approvalId"] == "ap-1"
    assert response["params"]["approved"] is True
    assert event is not None
    assert event["event"] == EVENT_APPROVAL_AUTO_APPROVED
    assert event["payload"]["approvalId"] == "ap-1"
    assert event["payload"]["kind"] == "command_execution"


def test_handler_never_policy_works_for_file_change_approvals() -> None:
    handler = ApprovalHandler(approval_policy=APPROVAL_POLICY_NEVER)
    response, event = handler.build_response(
        {"approvalId": "ap-2", "kind": "file_change", "paths": ["a.txt"]}
    )
    assert response["params"]["approved"] is True
    assert event is not None
    assert event["payload"]["kind"] == "file_change"


def test_handler_falls_back_to_deny_on_unknown_policy() -> None:
    """Any policy other than ``never`` denies — defensive default for
    future policies that may want stricter behaviour."""
    handler = ApprovalHandler(approval_policy="manual")
    response, event = handler.build_response({"approvalId": "ap-3"})
    assert response["params"]["approved"] is False
    assert event is not None
    assert event["event"] == EVENT_APPROVAL_DENIED


def test_handler_missing_approval_id_still_produces_response() -> None:
    """Best-effort echo: if the request omits ``approvalId`` we still send
    a response (with None) so the agent doesn't stall waiting forever."""
    handler = ApprovalHandler(approval_policy=APPROVAL_POLICY_NEVER)
    response, event = handler.build_response({"kind": "x"})
    assert response["params"]["approved"] is True
    assert response["params"].get("approvalId") is None


def test_handler_rejects_invalid_policy_at_construction() -> None:
    with pytest.raises(ValueError):
        ApprovalHandler(approval_policy="")


# ---------------------------------------------------------------------------
# Integration: stream_turn auto-approves
# ---------------------------------------------------------------------------


def _session() -> Session:
    return Session(
        thread_id="th-1",
        first_turn_id="tn-0",
        codex_app_server_pid=12345,
        started_at=datetime.now(UTC),
    )


def _ack(turn_id: str = "tn-1", *, request_id: int = 1) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": {"turnId": turn_id}}


def _evt(method: str, **params: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "method": method, "params": dict(params)}


async def test_stream_turn_auto_approves_under_never_policy() -> None:
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        _evt(EVENT_APPROVAL_REQUEST, approvalId="ap-9", kind="command_execution"),
        _evt("turn_completed"),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)
    received: list[RuntimeEvent] = []

    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=received.append,
        turn_timeout_ms=5000,
    )

    # Approval response was written back to the agent.
    response_frames = [
        f for f in fake.written_frames if f.get("method") == METHOD_APPROVAL_RESPONSE
    ]
    assert len(response_frames) == 1
    assert response_frames[0]["params"]["approvalId"] == "ap-9"
    assert response_frames[0]["params"]["approved"] is True

    # Operator-visible event is the *auto_approved* signal, not the raw request.
    auto_events = [
        e for e in received if e.event == EVENT_APPROVAL_AUTO_APPROVED
    ]
    assert len(auto_events) == 1
    assert auto_events[0].payload["approvalId"] == "ap-9"
    raw_events = [e for e in received if e.event == EVENT_APPROVAL_REQUEST]
    assert raw_events == []


async def test_stream_turn_emits_denied_event_when_policy_denies() -> None:
    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _evt(EVENT_APPROVAL_REQUEST, approvalId="ap-1"),
        _evt("turn_completed"),
    )
    client = CodexClient(
        process=fake,
        codex_app_server_pid=1,
        approval_handler=ApprovalHandler(approval_policy="manual"),
    )
    received: list[RuntimeEvent] = []
    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=received.append,
        turn_timeout_ms=5000,
    )

    response = next(
        f for f in fake.written_frames if f.get("method") == METHOD_APPROVAL_RESPONSE
    )
    assert response["params"]["approved"] is False
    assert any(e.event == EVENT_APPROVAL_DENIED for e in received)


async def test_stream_turn_user_input_required_raises_under_never_policy() -> None:
    """SPED §10.5 high-trust posture: user input request = hard failure.

    This duplicates a Task 18 case but lives here too because the policy
    docstring is what makes the behaviour load-bearing — if the trust
    posture ever changes, this test changes with it.
    """
    fake = FakeCodexProcess()
    fake.queue(_ack(), _evt("turn_input_required", prompt="?"))
    client = CodexClient(process=fake, codex_app_server_pid=1)

    with pytest.raises(TurnInputRequired):
        await client.stream_turn(
            session=_session(),
            prompt="x",
            on_event=lambda _e: None,
            turn_timeout_ms=5000,
        )


# ---------------------------------------------------------------------------
# Startup payload: policies travel into thread.start
# ---------------------------------------------------------------------------


def _handshake_responses(
    *, thread_id: str = "th-1", turn_id: str = "tn-1"
) -> list[dict[str, object]]:
    return [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"threadId": thread_id}},
        {"jsonrpc": "2.0", "id": 3, "result": {"turnId": turn_id}},
    ]


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


async def test_thread_start_carries_workspace_write_sandbox_policy(
    tmp_path: object,
) -> None:
    fake = FakeCodexProcess(_handshake_responses())
    client = CodexClient(process=fake, codex_app_server_pid=1)
    await client.start_session(
        workspace=tmp_path,  # type: ignore[arg-type]
        prompt="P",
        issue=_issue(),
        approval_policy=APPROVAL_POLICY_NEVER,
        sandbox_policy=SANDBOX_POLICY_WORKSPACE_WRITE,
        read_timeout_ms=5000,
    )

    thread_start = next(
        f for f in fake.written_frames if f.get("method") == METHOD_THREAD_START
    )
    assert thread_start["params"]["sandboxPolicy"] == SANDBOX_POLICY_WORKSPACE_WRITE
    assert thread_start["params"]["approvalPolicy"] == APPROVAL_POLICY_NEVER


async def test_first_turn_does_not_resend_policies(tmp_path: object) -> None:
    """``turn.start`` is scoped to the thread that already binds policies —
    the first turn payload must NOT redundantly carry approval/sandbox.
    """
    fake = FakeCodexProcess(_handshake_responses())
    client = CodexClient(process=fake, codex_app_server_pid=1)
    await client.start_session(
        workspace=tmp_path,  # type: ignore[arg-type]
        prompt="P",
        issue=_issue(),
        approval_policy=APPROVAL_POLICY_NEVER,
        sandbox_policy=SANDBOX_POLICY_WORKSPACE_WRITE,
        read_timeout_ms=5000,
    )
    turn_start = next(
        f for f in fake.written_frames if f.get("method") == METHOD_TURN_START
    )
    assert "approvalPolicy" not in turn_start["params"]
    assert "sandboxPolicy" not in turn_start["params"]
