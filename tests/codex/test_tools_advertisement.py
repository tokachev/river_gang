"""Tests for ``linear_graphql`` tool advertisement + dispatch (SPED §10.5)."""

from __future__ import annotations

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
    RuntimeEvent,
    Session,
    TurnResult,
)
from river_gang.codex.protocol import METHOD_INITIALIZE
from river_gang.tools.linear_graphql import ToolResult
from river_gang.tracker.issue import Issue
from tests.codex.fakes import FakeCodexProcess

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class FakeLinearGraphqlTool:
    """Stand-in for :class:`LinearGraphqlTool` in routing tests."""

    def __init__(
        self, *, return_value: ToolResult | None = None
    ) -> None:
        self.calls: list[Any] = []
        self._return_value = return_value or ToolResult(
            success=True, data={"viewer": {"id": "u-1"}}, errors=None,
            error_message=None,
        )

    async def execute(self, raw_input: Any) -> ToolResult:
        self.calls.append(raw_input)
        return self._return_value


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


def _handshake_responses(
    *, thread_id: str = "th-1", turn_id: str = "tn-1"
) -> list[dict[str, object]]:
    """Codex 0.125.0+ wraps ``thread/start`` / ``turn/start`` results inside
    ``result.thread`` / ``result.turn`` objects."""
    return [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"thread": {"id": thread_id}}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {"turn": {"id": turn_id, "status": "inProgress"}},
        },
    ]


def _session() -> Session:
    return Session(
        thread_id="th-1",
        first_turn_id="tn-0",
        codex_app_server_pid=12345,
        started_at=datetime.now(UTC),
    )


def _ack(turn_id: str = "tn-1", *, request_id: int = 1) -> dict[str, object]:
    """Codex 0.125.0+ wraps the new turn id under ``result.turn``."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"turn": {"id": turn_id, "status": "inProgress"}},
    }


def _evt(method: str, **params: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "method": method, "params": dict(params)}


def _turn_completed(
    *, turn_id: str = "tn-1", thread_id: str = "th-1"
) -> dict[str, object]:
    return _evt(
        METHOD_TURN_COMPLETED,
        threadId=thread_id,
        turn={"id": turn_id, "status": "completed", "items": []},
    )


def _tool_call_request(
    *,
    request_id: int,
    tool: str,
    arguments: object,
    call_id: str = "c-1",
    thread_id: str = "th-1",
    turn_id: str = "tn-1",
) -> dict[str, object]:
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


# ---------------------------------------------------------------------------
# Spec shape
# ---------------------------------------------------------------------------


def test_linear_graphql_tool_spec_constants() -> None:
    assert LINEAR_GRAPHQL_TOOL_NAME == "linear_graphql"
    assert LINEAR_GRAPHQL_TOOL_SPEC["name"] == "linear_graphql"
    assert isinstance(LINEAR_GRAPHQL_TOOL_SPEC["description"], str)
    assert LINEAR_GRAPHQL_TOOL_SPEC["description"]
    schema = LINEAR_GRAPHQL_TOOL_SPEC["inputSchema"]
    assert schema["type"] == "object"
    assert "query" in schema["properties"]
    assert "variables" in schema["properties"]
    assert schema["properties"]["query"]["type"] == "string"
    assert schema["properties"]["variables"]["type"] == "object"
    assert "query" in schema.get("required", [])


# ---------------------------------------------------------------------------
# Advertisement: included only for tracker.kind=linear AND tool wired
# ---------------------------------------------------------------------------


async def test_initialize_does_not_carry_tools_field(
    tmp_path: Path,
) -> None:
    """Codex 0.125.0+ schema gap: :file:`InitializeParams.json` and
    :file:`ThreadStartParams.json` define no public field for client-side
    tool advertisement. river-gang therefore omits the field entirely and
    relies on codex being externally configured to dispatch
    ``item/tool/call`` for the agreed tool name (see
    :func:`build_initialize_request` for the gap docstring).
    """
    fake = FakeCodexProcess(_handshake_responses())
    tool = FakeLinearGraphqlTool()
    client = CodexClient(
        process=fake,
        codex_app_server_pid=1,
        linear_graphql_tool=tool,  # type: ignore[arg-type]
    )

    await client.start_session(
        workspace=tmp_path,
        prompt="P",
        issue=_issue(),
        approval_policy="never",
        sandbox_policy="workspace-write",
        read_timeout_ms=5000,
        tracker_kind="linear",
    )

    init_request = next(
        f for f in fake.written_frames if f["method"] == METHOD_INITIALIZE
    )
    assert "tools" not in init_request["params"]


# ---------------------------------------------------------------------------
# Tool dispatch during streaming (codex 0.125.0+ ``item/tool/call`` request)
# ---------------------------------------------------------------------------
#
# DynamicToolCallParams: ``{tool, arguments, callId, threadId, turnId}``
# DynamicToolCallResponse: ``{success, contentItems: [{type:"inputText",text}]}``
# Reply travels as a plain JSON-RPC response frame ``{id, result}`` or
# ``{id, error: {code, message}}`` — there is no separate notification.


def _find_reply(
    frames: list[dict[str, Any]], request_id: int
) -> dict[str, Any]:
    """Locate the JSON-RPC reply frame written for ``request_id``."""
    replies = [
        f for f in frames if f.get("id") == request_id and "method" not in f
    ]
    assert len(replies) == 1, frames
    return replies[0]


async def test_tool_call_routed_to_linear_graphql_tool() -> None:
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        _tool_call_request(
            request_id=42,
            tool="linear_graphql",
            arguments={
                "query": "query Viewer { viewer { id } }",
                "variables": {},
            },
            call_id="call-7",
        ),
        _turn_completed(),
    )
    tool = FakeLinearGraphqlTool(
        return_value=ToolResult(
            success=True,
            data={"viewer": {"id": "u-9"}},
            errors=None,
            error_message=None,
        )
    )
    client = CodexClient(
        process=fake,
        codex_app_server_pid=1,
        linear_graphql_tool=tool,  # type: ignore[arg-type]
    )

    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )

    # Tool was invoked exactly once with the agent-supplied input
    assert len(tool.calls) == 1
    assert tool.calls[0] == {
        "query": "query Viewer { viewer { id } }",
        "variables": {},
    }

    reply = _find_reply(fake.written_frames, 42)
    assert "result" in reply
    result = reply["result"]
    assert result["success"] is True
    assert isinstance(result["contentItems"], list)
    assert result["contentItems"][0]["type"] == "inputText"
    # Successful body is JSON-encoded inside a single text content item.
    assert "viewer" in result["contentItems"][0]["text"]


async def test_tool_call_failure_returns_success_false_response() -> None:
    """Tool *execution* failure is encoded inside the response (success=false)
    rather than as a JSON-RPC error — matches DynamicToolCallResponse schema."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _tool_call_request(
            request_id=51,
            tool="linear_graphql",
            arguments="{ this is broken",
            call_id="call-9",
        ),
        _turn_completed(),
    )
    tool = FakeLinearGraphqlTool(
        return_value=ToolResult(
            success=False,
            data=None,
            errors=None,
            error_message="invalid GraphQL syntax",
        )
    )
    client = CodexClient(
        process=fake,
        codex_app_server_pid=1,
        linear_graphql_tool=tool,  # type: ignore[arg-type]
    )

    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )

    reply = _find_reply(fake.written_frames, 51)
    assert "result" in reply
    assert reply["result"]["success"] is False
    text = reply["result"]["contentItems"][0]["text"]
    assert "invalid GraphQL syntax" in text


async def test_tool_call_graphql_errors_preserved_in_content_text() -> None:
    """SPED §10.5: GraphQL body MUST be preserved on tool failure. With the
    new request/response flow the body is JSON-encoded inside the single
    inputText content item the schema permits."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _tool_call_request(
            request_id=60,
            tool="linear_graphql",
            arguments={"query": "{ viewer { id } }"},
            call_id="call-1",
        ),
        _turn_completed(),
    )
    tool = FakeLinearGraphqlTool(
        return_value=ToolResult(
            success=False,
            data={"viewer": None},
            errors=[{"message": "permission denied"}],
            error_message="GraphQL errors",
        )
    )
    client = CodexClient(
        process=fake,
        codex_app_server_pid=1,
        linear_graphql_tool=tool,  # type: ignore[arg-type]
    )

    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )

    reply = _find_reply(fake.written_frames, 60)
    assert reply["result"]["success"] is False
    text = reply["result"]["contentItems"][0]["text"]
    assert "GraphQL errors" in text
    assert "permission denied" in text
    # Partial data is preserved alongside the errors list.
    assert "viewer" in text


async def test_unknown_tool_replies_with_success_false() -> None:
    """Unknown tool name → DynamicToolCallResponse with ``success=false``
    and an ``unsupported_tool: ...`` content item. The schema reserves
    JSON-RPC errors for protocol-level failures, not tool-level ones."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _tool_call_request(
            request_id=71,
            tool="random_tool",
            arguments={},
            call_id="x-1",
        ),
        _turn_completed(),
    )
    tool = FakeLinearGraphqlTool()
    client = CodexClient(
        process=fake,
        codex_app_server_pid=1,
        linear_graphql_tool=tool,  # type: ignore[arg-type]
    )

    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )

    reply = _find_reply(fake.written_frames, 71)
    assert "result" in reply
    assert reply["result"]["success"] is False
    assert "unsupported_tool" in reply["result"]["contentItems"][0]["text"]
    # Linear tool was NOT invoked
    assert tool.calls == []


async def test_tool_call_session_continues_after_dispatch() -> None:
    """Even after a tool call, ``stream_turn`` continues until turn/completed."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _tool_call_request(
            request_id=80,
            tool="linear_graphql",
            arguments={"query": "{ x }"},
            call_id="c1",
        ),
        _evt("notification", text="agent reasoning"),
        _turn_completed(),
    )
    tool = FakeLinearGraphqlTool()
    client = CodexClient(
        process=fake,
        codex_app_server_pid=1,
        linear_graphql_tool=tool,  # type: ignore[arg-type]
    )

    received: list[RuntimeEvent] = []
    result = await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=received.append,
        turn_timeout_ms=5000,
    )
    assert isinstance(result, TurnResult)
    # tool was invoked, agent_message was forwarded, completion arrived
    assert len(tool.calls) == 1
    assert any(e.event == "notification" for e in received)


async def test_tool_call_when_no_linear_tool_wired_replies_with_success_false() -> None:
    """Even ``tool=linear_graphql`` is auto-failed when no tool instance
    is wired into the client — encoded as success=false in the response
    (no JSON-RPC error)."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _tool_call_request(
            request_id=90,
            tool="linear_graphql",
            arguments={"query": "{ x }"},
            call_id="c1",
        ),
        _turn_completed(),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)  # no tool

    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )

    reply = _find_reply(fake.written_frames, 90)
    assert "result" in reply
    assert reply["result"]["success"] is False
    assert "unsupported_tool" in reply["result"]["contentItems"][0]["text"]


async def test_tool_call_timeout_replies_with_tool_timeout_text() -> None:
    """If the tool execution exceeds ``read_timeout_ms``, the handler
    returns success=false with a ``tool_timeout`` content text — encoded
    inside the response shape, never a JSON-RPC error."""
    import asyncio as _asyncio

    class HangingTool:
        async def execute(self, _raw: Any) -> Any:
            await _asyncio.sleep(10)  # never returns within the test budget
            return None

    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _tool_call_request(
            request_id=110,
            tool="linear_graphql",
            arguments={"query": "{ x }"},
            call_id="c-t",
        ),
        _turn_completed(),
    )
    client = CodexClient(
        process=fake,
        codex_app_server_pid=1,
        linear_graphql_tool=HangingTool(),  # type: ignore[arg-type]
    )
    # start_session sets _read_timeout_ms; here we set it directly to a
    # tiny budget via the public start_session contract isn't appropriate,
    # so we set the protected field — locked-in by the contract that the
    # field is the per-call cap (see CodexClient.start_session).
    client._read_timeout_ms = 50  # ms  # noqa: SLF001

    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )

    reply = _find_reply(fake.written_frames, 110)
    assert "result" in reply
    assert reply["result"]["success"] is False
    assert reply["result"]["contentItems"][0]["text"] == "tool_timeout"


async def test_tool_call_handler_exception_replies_with_success_false() -> None:
    """A tool-execution exception (not a ``ToolResult(success=False)``)
    is encoded inside the DynamicToolCallResponse as success=false with
    the exception text, so codex unblocks immediately without a JSON-RPC
    error."""

    class ExplodingTool:
        async def execute(self, _raw: Any) -> Any:
            raise RuntimeError("tool boom")

    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _tool_call_request(
            request_id=100,
            tool="linear_graphql",
            arguments={"query": "{ x }"},
            call_id="c1",
        ),
        _turn_completed(),
    )
    client = CodexClient(
        process=fake,
        codex_app_server_pid=1,
        linear_graphql_tool=ExplodingTool(),  # type: ignore[arg-type]
    )

    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )

    reply = _find_reply(fake.written_frames, 100)
    assert "result" in reply
    assert reply["result"]["success"] is False
    assert "tool boom" in reply["result"]["contentItems"][0]["text"]


@pytest.mark.parametrize("kind", ["", None, "Linear", "LINEAR"])
async def test_tracker_kind_strict_match_only_lowercase_linear(
    tmp_path: Path, kind: object
) -> None:
    """Advertisement requires exactly ``tracker_kind="linear"``."""
    fake = FakeCodexProcess(_handshake_responses())
    tool = FakeLinearGraphqlTool()
    client = CodexClient(
        process=fake,
        codex_app_server_pid=1,
        linear_graphql_tool=tool,  # type: ignore[arg-type]
    )

    await client.start_session(
        workspace=tmp_path,
        prompt="P",
        issue=_issue(),
        approval_policy="never",
        sandbox_policy="workspace-write",
        read_timeout_ms=5000,
        tracker_kind=kind,  # type: ignore[arg-type]
    )

    init_request = next(
        f for f in fake.written_frames if f["method"] == METHOD_INITIALIZE
    )
    assert "tools" not in init_request["params"]


# ---------------------------------------------------------------------------
# Tool dispatch: string arguments parsing + tool-internal TimeoutError
# vs wait_for timeout + turn_timeout_ms fallback
# ---------------------------------------------------------------------------


async def test_tool_call_string_arguments_parsed_as_json() -> None:
    """``DynamicToolCallParams.arguments`` is schema-typed ``true`` (any).
    Some codex builds serialise the value as a JSON-encoded string; the
    handler MUST :func:`json.loads` strings before passing them to the
    tool layer so the tool always sees a structured object when codex
    meant one."""
    tool = FakeLinearGraphqlTool()
    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _tool_call_request(
            request_id=200,
            tool="linear_graphql",
            arguments='{"query": "{ x }", "variables": {"k": 1}}',
            call_id="c-str",
        ),
        _turn_completed(),
    )
    client = CodexClient(
        process=fake,
        codex_app_server_pid=1,
        linear_graphql_tool=tool,  # type: ignore[arg-type]
    )

    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )

    assert tool.calls == [{"query": "{ x }", "variables": {"k": 1}}]


async def test_tool_call_unparseable_string_arguments_passed_through() -> None:
    """If the JSON-encoded-string form fails to parse, the raw string is
    passed through to the tool — the tool layer can decide what to do
    (validate, reject, etc.). The handler must not swallow the call."""
    tool = FakeLinearGraphqlTool()
    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _tool_call_request(
            request_id=201,
            tool="linear_graphql",
            arguments="not valid json {{{",
            call_id="c-bad",
        ),
        _turn_completed(),
    )
    client = CodexClient(
        process=fake,
        codex_app_server_pid=1,
        linear_graphql_tool=tool,  # type: ignore[arg-type]
    )

    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )

    assert tool.calls == ["not valid json {{{"]


async def test_tool_internal_timeout_error_reported_as_tool_error_not_tool_timeout() -> None:
    """A :class:`TimeoutError` raised from inside the tool (e.g. an HTTP
    transport timeout) is NOT a wait_for trip and MUST surface via the
    generic ``tool_error: ...`` envelope. Conflating it with the wait_for
    ``tool_timeout`` label hides which layer actually timed out."""

    class TransportTimeoutTool:
        async def execute(self, _raw: Any) -> Any:
            raise TimeoutError("HTTP transport timeout")

    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _tool_call_request(
            request_id=202,
            tool="linear_graphql",
            arguments={"query": "{ x }"},
            call_id="c-tx",
        ),
        _turn_completed(),
    )
    client = CodexClient(
        process=fake,
        codex_app_server_pid=1,
        linear_graphql_tool=TransportTimeoutTool(),  # type: ignore[arg-type]
    )
    # Generous read timeout so the wait_for never trips — the tool raises
    # before that.
    client._read_timeout_ms = 5000  # noqa: SLF001

    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )

    reply = _find_reply(fake.written_frames, 202)
    text = reply["result"]["contentItems"][0]["text"]
    assert reply["result"]["success"] is False
    assert text != "tool_timeout"
    assert text.startswith("tool_error:")
    assert "HTTP transport timeout" in text


async def test_tool_call_falls_back_to_turn_timeout_when_read_timeout_unset() -> None:
    """When ``stream_turn`` is invoked outside ``start_session`` (so
    ``_read_timeout_ms`` is None), the tool dispatch MUST still be bounded
    — the handler clamps to ``turn_timeout_ms`` as the per-call cap so a
    hanging tool cannot escape every wall-clock guard.

    Verify by inspecting the deadline argument passed to
    :func:`asyncio.timeout` rather than by racing the outer stream_turn
    timeout (the outer wait_for shares the same deadline; it will trip
    first regardless of the inner cap and surface :class:`TurnTimeout`
    instead of a ``tool_timeout`` reply).
    """
    import asyncio as _asyncio

    captured_deadlines: list[float | None] = []
    real_timeout = _asyncio.timeout

    def spying_timeout(delay: float | None) -> Any:
        captured_deadlines.append(delay)
        return real_timeout(delay)

    tool = FakeLinearGraphqlTool()
    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _tool_call_request(
            request_id=203,
            tool="linear_graphql",
            arguments={"query": "{ x }"},
            call_id="c-fallback",
        ),
        _turn_completed(),
    )
    client = CodexClient(
        process=fake,
        codex_app_server_pid=1,
        linear_graphql_tool=tool,  # type: ignore[arg-type]
    )
    # Crucial: do NOT set _read_timeout_ms — this exercises the fallback.
    assert client._read_timeout_ms is None  # noqa: SLF001

    import unittest.mock as _mock

    with _mock.patch(
        "river_gang.codex.client.asyncio.timeout",
        side_effect=spying_timeout,
    ):
        await client.stream_turn(
            session=_session(),
            prompt="x",
            on_event=lambda _e: None,
            turn_timeout_ms=4321,
        )

    # The handler's asyncio.timeout(...) must have been called with a
    # deadline derived from turn_timeout_ms (4.321s) — proving the
    # fallback fires.
    assert 4.321 in captured_deadlines, captured_deadlines
