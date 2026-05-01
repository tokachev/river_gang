"""Tests for :class:`ApprovalHandler` (SPED §10.5, §15.1).

Codex 0.125.0+ promotes approvals from notifications to JSON-RPC server
requests. There are five approval methods, with two response shapes:

- ``decision: "approved"`` — ``applyPatchApproval``, ``execCommandApproval``
- ``decision: "accept"``   — ``item/commandExecution/requestApproval``,
                              ``item/fileChange/requestApproval``
- ``permissions: {...}``    — ``item/permissions/requestApproval``

This module covers both the pure-builder surface (``build_result``) and the
end-to-end wire shape (``stream_turn`` answers each method with a JSON-RPC
``{id, result: {...}}`` response — never a notification).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from river_gang.codex.client import METHOD_TURN_COMPLETED, CodexClient, Session
from river_gang.codex.policy import (
    APPROVAL_METHODS,
    APPROVAL_POLICY_NEVER,
    METHOD_APPLY_PATCH_APPROVAL,
    METHOD_EXEC_COMMAND_APPROVAL,
    METHOD_ITEM_COMMAND_EXECUTION_REQUEST_APPROVAL,
    METHOD_ITEM_FILE_CHANGE_REQUEST_APPROVAL,
    METHOD_ITEM_PERMISSIONS_REQUEST_APPROVAL,
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
    assert METHOD_APPLY_PATCH_APPROVAL == "applyPatchApproval"
    assert METHOD_EXEC_COMMAND_APPROVAL == "execCommandApproval"
    assert (
        METHOD_ITEM_COMMAND_EXECUTION_REQUEST_APPROVAL
        == "item/commandExecution/requestApproval"
    )
    assert (
        METHOD_ITEM_FILE_CHANGE_REQUEST_APPROVAL
        == "item/fileChange/requestApproval"
    )
    assert (
        METHOD_ITEM_PERMISSIONS_REQUEST_APPROVAL
        == "item/permissions/requestApproval"
    )
    assert set(APPROVAL_METHODS) == {
        METHOD_APPLY_PATCH_APPROVAL,
        METHOD_EXEC_COMMAND_APPROVAL,
        METHOD_ITEM_COMMAND_EXECUTION_REQUEST_APPROVAL,
        METHOD_ITEM_FILE_CHANGE_REQUEST_APPROVAL,
        METHOD_ITEM_PERMISSIONS_REQUEST_APPROVAL,
    }


# ---------------------------------------------------------------------------
# ApprovalHandler — pure builder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method",
    [METHOD_APPLY_PATCH_APPROVAL, METHOD_EXEC_COMMAND_APPROVAL],
)
def test_handler_never_policy_returns_review_decision_approved(
    method: str,
) -> None:
    """ApplyPatchApproval / ExecCommandApproval use the ReviewDecision enum
    where 'approved' is the success literal."""
    handler = ApprovalHandler(approval_policy=APPROVAL_POLICY_NEVER)
    result = handler.build_result(method, {"command": ["ls"]})
    assert result == {"decision": "approved"}


@pytest.mark.parametrize(
    "method",
    [
        METHOD_ITEM_COMMAND_EXECUTION_REQUEST_APPROVAL,
        METHOD_ITEM_FILE_CHANGE_REQUEST_APPROVAL,
    ],
)
def test_handler_never_policy_returns_item_decision_accept(method: str) -> None:
    """Item-namespace approvals use {Command,File}ChangeApprovalDecision
    where the success literal is 'accept' (different vocabulary from
    ReviewDecision — both schemas live in sandbox/codex-schema/)."""
    handler = ApprovalHandler(approval_policy=APPROVAL_POLICY_NEVER)
    result = handler.build_result(method, {"itemId": "i-1"})
    assert result == {"decision": "accept"}


def test_handler_never_policy_returns_permissions_grant() -> None:
    """PermissionsRequestApprovalResponse has no ``decision`` field — its
    only required field is ``permissions`` (a GrantedPermissionProfile).

    Empty profile = "no additional permissions granted"; the OS sandbox
    already covers our workspace-write surface.
    """
    handler = ApprovalHandler(approval_policy=APPROVAL_POLICY_NEVER)
    result = handler.build_result(
        METHOD_ITEM_PERMISSIONS_REQUEST_APPROVAL,
        {"itemId": "i-1", "permissions": {}, "cwd": "/tmp"},
    )
    assert "permissions" in result
    assert isinstance(result["permissions"], dict)


@pytest.mark.parametrize(
    "method,expected_denial",
    [
        (METHOD_APPLY_PATCH_APPROVAL, "denied"),
        (METHOD_EXEC_COMMAND_APPROVAL, "denied"),
        (METHOD_ITEM_COMMAND_EXECUTION_REQUEST_APPROVAL, "decline"),
        (METHOD_ITEM_FILE_CHANGE_REQUEST_APPROVAL, "decline"),
    ],
)
def test_handler_unknown_policy_falls_back_to_deny(
    method: str, expected_denial: str
) -> None:
    """Any policy other than 'never' denies — defensive default. Per-method
    denial vocabulary follows the matching response schema."""
    handler = ApprovalHandler(approval_policy="manual")
    result = handler.build_result(method, {})
    assert result == {"decision": expected_denial}


def test_handler_rejects_invalid_policy_at_construction() -> None:
    with pytest.raises(ValueError):
        ApprovalHandler(approval_policy="")


def test_handler_unknown_method_raises() -> None:
    handler = ApprovalHandler(approval_policy=APPROVAL_POLICY_NEVER)
    with pytest.raises(ValueError, match="unknown approval method"):
        handler.build_result("not/a/real/approval", {})


# ---------------------------------------------------------------------------
# Integration: stream_turn replies as JSON-RPC response, not notification
# ---------------------------------------------------------------------------


def _session() -> Session:
    return Session(
        thread_id="th-1",
        first_turn_id="tn-0",
        codex_app_server_pid=12345,
        started_at=datetime.now(UTC),
    )


def _ack(turn_id: str = "tn-1", *, request_id: int = 1) -> dict[str, Any]:
    """Synchronous turn/start ack — codex 0.125.0+ wraps the id under
    ``result.turn``."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"turn": {"id": turn_id, "status": "inProgress"}},
    }


def _server_request(
    *, request_id: int, method: str, **params: Any
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": dict(params),
    }


def _completion(turn_id: str = "tn-1") -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": METHOD_TURN_COMPLETED,
        "params": {
            "threadId": "th-1",
            "turn": {"id": turn_id, "status": "completed", "items": []},
        },
    }


@pytest.mark.parametrize(
    "method,expected_result",
    [
        (METHOD_APPLY_PATCH_APPROVAL, {"decision": "approved"}),
        (METHOD_EXEC_COMMAND_APPROVAL, {"decision": "approved"}),
        (
            METHOD_ITEM_COMMAND_EXECUTION_REQUEST_APPROVAL,
            {"decision": "accept"},
        ),
        (METHOD_ITEM_FILE_CHANGE_REQUEST_APPROVAL, {"decision": "accept"}),
    ],
)
async def test_stream_turn_answers_decision_approval_methods(
    method: str, expected_result: dict[str, Any]
) -> None:
    """Each decision-shaped approval method gets a JSON-RPC ``{id, result}``
    reply carrying the policy decision — NOT a notification."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        _server_request(request_id=99, method=method, callId="c-1"),
        _completion(turn_id="tn-1"),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)

    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )

    replies = [
        f for f in fake.written_frames
        if f.get("id") == 99 and "method" not in f
    ]
    assert len(replies) == 1, fake.written_frames
    reply = replies[0]
    assert reply == {"id": 99, "result": expected_result}
    # Wire-shape guarantee: response, not notification.
    assert "method" not in reply
    assert "result" in reply
    assert "id" in reply


async def test_stream_turn_answers_permissions_approval() -> None:
    """``item/permissions/requestApproval`` uses the permissions-grant
    response shape, not a decision string."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        _server_request(
            request_id=77,
            method=METHOD_ITEM_PERMISSIONS_REQUEST_APPROVAL,
            itemId="i-1",
            permissions={},
            cwd="/tmp",
            threadId="th-1",
            turnId="tn-1",
        ),
        _completion(turn_id="tn-1"),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)

    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )

    replies = [
        f for f in fake.written_frames
        if f.get("id") == 77 and "method" not in f
    ]
    assert len(replies) == 1
    reply = replies[0]
    assert reply["id"] == 77
    assert "result" in reply
    assert "permissions" in reply["result"]
    assert "decision" not in reply["result"]


async def test_stream_turn_approval_reply_is_response_not_notification() -> None:
    """Regression guard: legacy code emitted a ``method=approval_response``
    notification (no id). Codex 0.125.0+ requires a JSON-RPC response
    with the request id and no ``method`` field."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        _server_request(
            request_id=55,
            method=METHOD_APPLY_PATCH_APPROVAL,
            callId="c-1",
        ),
        _completion(turn_id="tn-1"),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)

    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )

    # No frame should carry ``method=approval_response`` — that legacy
    # notification path no longer exists.
    assert not any(
        f.get("method") == "approval_response" for f in fake.written_frames
    )

    # The reply for id=55 is wrapped as ``{id, result: {decision: ...}}``.
    reply = next(
        f for f in fake.written_frames
        if f.get("id") == 55 and "method" not in f
    )
    assert reply["result"] == {"decision": "approved"}


async def test_stream_turn_under_manual_policy_replies_with_denial() -> None:
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        _server_request(
            request_id=11,
            method=METHOD_APPLY_PATCH_APPROVAL,
        ),
        _completion(turn_id="tn-1"),
    )
    client = CodexClient(
        process=fake,
        codex_app_server_pid=1,
        approval_handler=ApprovalHandler(approval_policy="manual"),
    )

    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )

    reply = next(
        f for f in fake.written_frames
        if f.get("id") == 11 and "method" not in f
    )
    assert reply["result"] == {"decision": "denied"}


# ---------------------------------------------------------------------------
# Startup payload: policies travel into thread.start
# ---------------------------------------------------------------------------


def _handshake_responses(
    *, thread_id: str = "th-1", turn_id: str = "tn-1"
) -> list[dict[str, Any]]:
    return [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"thread": {"id": thread_id}},
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {"turn": {"id": turn_id, "status": "inProgress"}},
        },
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
    # Codex 0.125.0+ renamed ``sandboxPolicy`` to ``sandbox`` (Task 1).
    assert thread_start["params"]["sandbox"] == SANDBOX_POLICY_WORKSPACE_WRITE
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
    assert "sandbox" not in turn_start["params"]
    assert "sandboxPolicy" not in turn_start["params"]
