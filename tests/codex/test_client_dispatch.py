"""Tests for the incoming-frame dispatcher in :class:`CodexClient`
(plan task 3 — codex 0.125.0+ ServerRequest / ServerNotification split).

The codex 0.125.0+ schema multiplexes three frame shapes on the same channel
between the synchronous turn.start ack and the terminal completion event:

- ``id + method``        → server request   (we MUST reply with ``{id, result}`` or
                                              ``{id, error}``)
- ``method`` only        → server notification (existing event-stream path)
- ``id + result/error``  → response to a request we sent (correlation)

The dispatcher discriminates by frame shape, not by method name, so unknown
ServerRequest variants still get a JSON-RPC ``-32601`` reply instead of being
mis-routed into the notification path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from river_gang.codex.client import (
    METHOD_ITEM_TOOL_CALL,
    METHOD_TURN_COMPLETED,
    CodexClient,
    RuntimeEvent,
    Session,
    TurnResult,
)
from tests.codex.fakes import FakeCodexProcess


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


def _notification(method: str, **params: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method, "params": dict(params)}


def _server_request(
    *, request_id: int, method: str, **params: Any
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": dict(params),
    }


# ---------------------------------------------------------------------------
# Notification path (no id) → existing on_event flow
# ---------------------------------------------------------------------------


async def test_dispatch_notification_routed_to_on_event() -> None:
    """Frames with ``method`` and no ``id`` are notifications — they reach
    the orchestrator's ``on_event`` callback unchanged."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        _notification("agent_message", text="hello"),
        _notification(
            METHOD_TURN_COMPLETED,
            threadId="th-1",
            turn={"id": "tn-1", "status": "completed", "items": []},
        ),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)
    received: list[RuntimeEvent] = []

    result = await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=received.append,
        turn_timeout_ms=5_000,
    )

    assert isinstance(result, TurnResult)
    assert [e.event for e in received] == ["agent_message", METHOD_TURN_COMPLETED]
    # No JSON-RPC reply written for plain notifications.
    written_methods = [f.get("method") for f in fake.written_frames]
    assert written_methods.count("turn/start") == 1  # turn.start request
    # No reply frames carrying the notification's would-be id.
    assert all("id" not in f or f.get("method") for f in fake.written_frames)


# ---------------------------------------------------------------------------
# Server-request path (id + method) → -32601 fallback when no handler
# ---------------------------------------------------------------------------


async def test_dispatch_unknown_server_request_replies_method_not_found() -> None:
    """Unregistered server-request methods receive a JSON-RPC ``-32601``
    error response so codex unblocks instead of hanging until
    ``turn_timeout_ms``."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        _server_request(
            request_id=99,
            method="some/unimplemented/request",
            payload={"k": "v"},
        ),
        _notification(
            METHOD_TURN_COMPLETED,
            threadId="th-1",
            turn={"id": "tn-1", "status": "completed", "items": []},
        ),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)
    received: list[RuntimeEvent] = []

    result = await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=received.append,
        turn_timeout_ms=5_000,
    )

    assert isinstance(result, TurnResult)

    # Find the JSON-RPC error response we wrote back to codex.
    replies = [
        f
        for f in fake.written_frames
        if f.get("id") == 99 and "method" not in f
    ]
    assert len(replies) == 1, fake.written_frames
    reply = replies[0]
    assert reply.get("error", {}).get("code") == -32601
    assert "method not found" in reply["error"]["message"]
    # Message uses bare method name (no embedded {!r} quoting).
    assert "some/unimplemented/request" in reply["error"]["message"]
    assert "'some/unimplemented/request'" not in reply["error"]["message"]
    assert "result" not in reply

    # The unknown server request did NOT leak into the notification stream.
    assert "some/unimplemented/request" not in [e.event for e in received]


async def test_dispatch_server_request_invokes_registered_handler() -> None:
    """A handler registered via :meth:`CodexClient.register_server_request`
    is invoked with the params dict and its return value is wrapped in
    ``{id, result}``."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        _server_request(
            request_id=42,
            method=METHOD_ITEM_TOOL_CALL,
            callId="c-1",
            tool="custom",
            arguments={"k": "v"},
        ),
        _notification(
            METHOD_TURN_COMPLETED,
            threadId="th-1",
            turn={"id": "tn-1", "status": "completed", "items": []},
        ),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)

    handler_calls: list[dict[str, Any]] = []

    async def handler(params: dict[str, Any]) -> dict[str, Any]:
        handler_calls.append(params)
        return {"ok": True, "echo": params.get("arguments")}

    client.register_server_request(METHOD_ITEM_TOOL_CALL, handler)

    received: list[RuntimeEvent] = []
    result = await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=received.append,
        turn_timeout_ms=5_000,
    )

    assert isinstance(result, TurnResult)
    assert handler_calls == [
        {"callId": "c-1", "tool": "custom", "arguments": {"k": "v"}}
    ]
    replies = [
        f
        for f in fake.written_frames
        if f.get("id") == 42 and "method" not in f
    ]
    assert len(replies) == 1, fake.written_frames
    reply = replies[0]
    assert reply.get("result") == {"ok": True, "echo": {"k": "v"}}
    assert "error" not in reply


async def test_dispatch_server_request_handler_exception_replies_error() -> None:
    """If a registered handler raises, the dispatcher MUST still send back
    a JSON-RPC ``-32603`` (Internal error) so codex unblocks. The session
    keeps streaming."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        _server_request(request_id=7, method="some/registered/method"),
        _notification(
            METHOD_TURN_COMPLETED,
            threadId="th-1",
            turn={"id": "tn-1", "status": "completed", "items": []},
        ),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)

    async def boom(_params: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("handler boom")

    client.register_server_request("some/registered/method", boom)

    received: list[RuntimeEvent] = []
    result = await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=received.append,
        turn_timeout_ms=5_000,
    )

    assert isinstance(result, TurnResult)
    replies = [
        f
        for f in fake.written_frames
        if f.get("id") == 7 and "method" not in f
    ]
    assert len(replies) == 1
    reply = replies[0]
    assert "error" in reply
    # Locks the -32603 distinction from -32601 (method-not-found).
    assert reply["error"]["code"] == -32603
    assert "handler boom" in reply["error"]["message"]


# ---------------------------------------------------------------------------
# item/tool/requestUserInput → empty-answers refusal per policy (task 7)
# ---------------------------------------------------------------------------


async def test_dispatch_request_user_input_replies_with_empty_answers() -> None:
    """``item/tool/requestUserInput`` is the codex 0.125.0+ EXPERIMENTAL
    server request that replaces the legacy ``turn_input_required``
    notification. river-gang's documented trust posture is "never block on
    operator input" — the handler must reply with a
    :file:`ToolRequestUserInputResponse` body whose ``answers`` map is
    empty so codex unblocks immediately without an operator-in-the-loop.
    """
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        _server_request(
            request_id=55,
            method="item/tool/requestUserInput",
            itemId="it-1",
            threadId="th-1",
            turnId="tn-1",
            questions=[
                {
                    "id": "q-1",
                    "header": "Confirm proceed",
                    "question": "Continue?",
                }
            ],
        ),
        _notification(
            METHOD_TURN_COMPLETED,
            threadId="th-1",
            turn={"id": "tn-1", "status": "completed", "items": []},
        ),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)
    received: list[RuntimeEvent] = []

    result = await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=received.append,
        turn_timeout_ms=5_000,
    )

    assert isinstance(result, TurnResult)

    # Reply: JSON-RPC ``{id, result: {answers: {}}}`` — no ``method``.
    replies = [
        f
        for f in fake.written_frames
        if f.get("id") == 55 and "method" not in f
    ]
    assert len(replies) == 1, fake.written_frames
    reply = replies[0]
    assert "error" not in reply
    # Schema (ToolRequestUserInputResponse) requires the ``answers`` field;
    # an empty map satisfies the schema and signals "no answers provided".
    assert reply.get("result") == {"answers": {}}

    # The request did NOT leak into the notification stream.
    assert "item/tool/requestUserInput" not in [e.event for e in received]


# ---------------------------------------------------------------------------
# mcpServer/elicitation/request → decline per policy (no MCP-server hang)
# ---------------------------------------------------------------------------


async def test_dispatch_mcp_elicitation_request_replies_decline() -> None:
    """When an MCP server attached to codex raises an
    ``elicitation/create`` request (form/url popup for the user), codex
    forwards it as ``mcpServer/elicitation/request`` and blocks until we
    reply with one of ``accept|decline|cancel``. Without a handler, codex
    eventually fails the turn — this was the root cause of the e2e
    ``max_turns`` loop against codex 0.125.0. Per the never-block-on-input
    policy the handler must reply with ``{action: "decline"}`` so the MCP
    server unblocks immediately without operator input.
    """
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        _server_request(
            request_id=77,
            method="mcpServer/elicitation/request",
            serverName="test-mcp",
            threadId="th-1",
            turnId="tn-1",
            mode="form",
            message="Please confirm",
            requestedSchema={"type": "object", "properties": {}},
        ),
        _notification(
            METHOD_TURN_COMPLETED,
            threadId="th-1",
            turn={"id": "tn-1", "status": "completed", "items": []},
        ),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)
    received: list[RuntimeEvent] = []

    result = await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=received.append,
        turn_timeout_ms=5_000,
    )

    assert isinstance(result, TurnResult)

    replies = [
        f
        for f in fake.written_frames
        if f.get("id") == 77 and "method" not in f
    ]
    assert len(replies) == 1, fake.written_frames
    reply = replies[0]
    assert "error" not in reply
    assert reply.get("result") == {"action": "decline"}

    # Does not leak into the notification stream.
    assert "mcpServer/elicitation/request" not in [e.event for e in received]


# ---------------------------------------------------------------------------
# Response path (id + result/error, no method) → ignored, no reply
# ---------------------------------------------------------------------------


async def test_dispatch_stray_response_frame_is_ignored() -> None:
    """A frame with ``id`` and ``result`` but no ``method`` is a response to
    a prior request. We tolerate it (log + skip) rather than treat it as a
    notification or as a server request."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        # Stray response — id + result, no method.
        {"jsonrpc": "2.0", "id": 12345, "result": {"some": "thing"}},
        _notification(
            METHOD_TURN_COMPLETED,
            threadId="th-1",
            turn={"id": "tn-1", "status": "completed", "items": []},
        ),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)
    received: list[RuntimeEvent] = []

    result = await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=received.append,
        turn_timeout_ms=5_000,
    )

    assert isinstance(result, TurnResult)
    # Stray id was NOT echoed back as a server-request reply.
    written_ids = [f.get("id") for f in fake.written_frames if "id" in f]
    assert 12345 not in written_ids
    # The stray response did NOT leak into the event stream as a notification.
    assert all(e.event != "" for e in received)
    assert "result" not in [e.event for e in received]


# ---------------------------------------------------------------------------
# Server-request validation: -32602 invalid params + malformed id drop
# ---------------------------------------------------------------------------


async def test_dispatch_server_request_non_dict_params_replies_invalid_params(
    caplog: Any,
) -> None:
    """Schema-typed params is always an object on every codex 0.125.0+
    ServerRequest. A non-dict params is wire-malformed — the dispatcher MUST
    reply with JSON-RPC ``-32602`` (Invalid params), NOT silently coerce to
    ``{}`` (which for approval methods would auto-approve a malformed
    request)."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        # Server-request frame with params=str (schema violation).
        {
            "jsonrpc": "2.0",
            "id": 71,
            "method": "applyPatchApproval",
            "params": "not-a-dict",
        },
        _notification(
            METHOD_TURN_COMPLETED,
            threadId="th-1",
            turn={"id": "tn-1", "status": "completed", "items": []},
        ),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)

    result = await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=lambda _e: None,
        turn_timeout_ms=5_000,
    )
    assert isinstance(result, TurnResult)

    replies = [
        f
        for f in fake.written_frames
        if f.get("id") == 71 and "method" not in f
    ]
    assert len(replies) == 1, fake.written_frames
    reply = replies[0]
    assert reply.get("error", {}).get("code") == -32602
    # Crucially: NO ``result`` body — the approval was NOT auto-approved
    # despite the policy being "never" (which would normally produce
    # ``{decision: "approved"}`` for applyPatchApproval).
    assert "result" not in reply


async def test_dispatch_server_request_malformed_id_is_dropped(
    caplog: Any,
) -> None:
    """A request frame with an id that's neither int nor str (e.g. a list)
    cannot be routed back to codex by JSON-RPC — the dispatcher MUST log
    and drop it instead of writing a malformed reply that compounds the
    drift."""
    import logging

    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        # Malformed id (list).
        {
            "jsonrpc": "2.0",
            "id": ["bogus", "id"],
            "method": "applyPatchApproval",
            "params": {"command": "ls"},
        },
        _notification(
            METHOD_TURN_COMPLETED,
            threadId="th-1",
            turn={"id": "tn-1", "status": "completed", "items": []},
        ),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)

    with caplog.at_level(logging.WARNING, logger="river_gang.codex.client"):
        result = await client.stream_turn(
            session=_session(),
            prompt="x",
            on_event=lambda _e: None,
            turn_timeout_ms=5_000,
        )
    assert isinstance(result, TurnResult)

    # No reply whatsoever for the malformed id.
    replies = [
        f
        for f in fake.written_frames
        if f.get("id") == ["bogus", "id"] and "method" not in f
    ]
    assert replies == []
    # Nothing carrying a list id leaked back at all.
    assert all(
        not isinstance(f.get("id"), list) for f in fake.written_frames
    )
    assert any(
        "malformed id" in rec.message for rec in caplog.records
    ), caplog.records
