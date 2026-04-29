"""Tests for ``linear_graphql`` tool advertisement + dispatch (SPED §10.5)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from river_gang.codex.client import (
    LINEAR_GRAPHQL_TOOL_NAME,
    LINEAR_GRAPHQL_TOOL_SPEC,
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
    return [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"threadId": thread_id}},
        {"jsonrpc": "2.0", "id": 3, "result": {"turnId": turn_id}},
    ]


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


async def test_initialize_advertises_linear_graphql_when_tracker_is_linear(
    tmp_path: Path,
) -> None:
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
    tools = init_request["params"].get("tools")
    assert isinstance(tools, list)
    assert len(tools) == 1
    assert tools[0]["name"] == "linear_graphql"
    assert tools[0] == LINEAR_GRAPHQL_TOOL_SPEC


async def test_initialize_does_not_advertise_when_tracker_is_not_linear(
    tmp_path: Path,
) -> None:
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
        tracker_kind="github",
    )

    init_request = next(
        f for f in fake.written_frames if f["method"] == METHOD_INITIALIZE
    )
    assert "tools" not in init_request["params"]


async def test_initialize_does_not_advertise_when_tool_not_wired(
    tmp_path: Path,
) -> None:
    """``tracker_kind=linear`` alone is insufficient — the tool instance
    must also be supplied."""
    fake = FakeCodexProcess(_handshake_responses())
    client = CodexClient(process=fake, codex_app_server_pid=1)

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


async def test_tracker_kind_defaults_to_none_no_tools_advertised(
    tmp_path: Path,
) -> None:
    """Backward compat: existing call sites without ``tracker_kind`` work."""
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
    )

    init_request = next(
        f for f in fake.written_frames if f["method"] == METHOD_INITIALIZE
    )
    assert "tools" not in init_request["params"]


# ---------------------------------------------------------------------------
# Tool dispatch during streaming
# ---------------------------------------------------------------------------


async def test_tool_call_routed_to_linear_graphql_tool() -> None:
    fake = FakeCodexProcess()
    fake.queue(
        _ack(turn_id="tn-1"),
        _evt(
            "tool_call",
            callId="call-7",
            toolName="linear_graphql",
            arguments={
                "query": "query Viewer { viewer { id } }",
                "variables": {},
            },
        ),
        _evt("turn_completed"),
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
    received: list[RuntimeEvent] = []

    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=received.append,
        turn_timeout_ms=5000,
    )

    # Tool was invoked exactly once with the agent-supplied input
    assert len(tool.calls) == 1
    assert tool.calls[0] == {
        "query": "query Viewer { viewer { id } }",
        "variables": {},
    }

    # tool_call_response frame written back
    response = next(
        f for f in fake.written_frames if f.get("method") == "tool_call_response"
    )
    assert response["params"]["callId"] == "call-7"
    assert response["params"]["ok"] is True
    assert response["params"]["result"] == {"viewer": {"id": "u-9"}}


async def test_tool_call_failure_routed_with_failure_response() -> None:
    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _evt(
            "tool_call",
            callId="call-9",
            toolName="linear_graphql",
            arguments="{ this is broken",
        ),
        _evt("turn_completed"),
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

    response = next(
        f for f in fake.written_frames if f.get("method") == "tool_call_response"
    )
    assert response["params"]["callId"] == "call-9"
    assert response["params"]["ok"] is False
    assert response["params"]["error"] == "invalid GraphQL syntax"


async def test_tool_call_graphql_errors_response_includes_errors_list() -> None:
    """SPED §10.5: GraphQL body MUST be preserved on tool failure."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _evt(
            "tool_call",
            callId="call-1",
            toolName="linear_graphql",
            arguments={"query": "{ viewer { id } }"},
        ),
        _evt("turn_completed"),
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

    response = next(
        f for f in fake.written_frames if f.get("method") == "tool_call_response"
    )
    assert response["params"]["ok"] is False
    assert response["params"]["error"] == "GraphQL errors"
    # Body preserved alongside the errors list and the partial data
    assert response["params"]["result"] == {"viewer": None}
    assert response["params"]["errors"] == [{"message": "permission denied"}]


async def test_unknown_tool_still_falls_back_to_unsupported_response() -> None:
    """The Task 18 ``unsupported_tool_call`` path still applies for tools
    not registered with the client."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _evt("tool_call", callId="x-1", toolName="random_tool"),
        _evt("turn_completed"),
    )
    tool = FakeLinearGraphqlTool()
    client = CodexClient(
        process=fake,
        codex_app_server_pid=1,
        linear_graphql_tool=tool,  # type: ignore[arg-type]
    )

    received: list[RuntimeEvent] = []
    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=received.append,
        turn_timeout_ms=5000,
    )

    response = next(
        f for f in fake.written_frames if f.get("method") == "tool_call_response"
    )
    assert response["params"]["callId"] == "x-1"
    assert response["params"]["ok"] is False
    assert "unsupported_tool" in response["params"]["error"]
    # Linear tool was NOT invoked
    assert tool.calls == []
    # And an unsupported_tool_call event was emitted for observability
    assert any(e.event == "unsupported_tool_call" for e in received)


async def test_tool_call_response_session_continues_after_dispatch() -> None:
    """Even after a tool call, ``stream_turn`` continues until turn_completed."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _evt(
            "tool_call",
            callId="c1",
            toolName="linear_graphql",
            arguments={"query": "{ x }"},
        ),
        _evt("notification", text="agent reasoning"),
        _evt("turn_completed"),
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


async def test_tool_call_when_no_linear_tool_wired_falls_back_to_unsupported() -> None:
    """Even ``toolName=linear_graphql`` is auto-failed when no tool instance
    is wired into the client — the routing depends on the runtime config."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _evt(
            "tool_call",
            callId="c1",
            toolName="linear_graphql",
            arguments={"query": "{ x }"},
        ),
        _evt("turn_completed"),
    )
    client = CodexClient(process=fake, codex_app_server_pid=1)  # no tool

    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=lambda _e: None,
        turn_timeout_ms=5000,
    )

    response = next(
        f for f in fake.written_frames if f.get("method") == "tool_call_response"
    )
    assert response["params"]["ok"] is False
    assert "unsupported_tool" in response["params"]["error"]


async def test_tool_call_emits_observability_event() -> None:
    """When the tool runs, on_event should see a ``tool_call_completed``
    event so the orchestrator can surface tool activity to operators."""
    fake = FakeCodexProcess()
    fake.queue(
        _ack(),
        _evt(
            "tool_call",
            callId="c1",
            toolName="linear_graphql",
            arguments={"query": "{ x }"},
        ),
        _evt("turn_completed"),
    )
    tool = FakeLinearGraphqlTool()
    client = CodexClient(
        process=fake,
        codex_app_server_pid=1,
        linear_graphql_tool=tool,  # type: ignore[arg-type]
    )

    received: list[RuntimeEvent] = []
    await client.stream_turn(
        session=_session(),
        prompt="x",
        on_event=received.append,
        turn_timeout_ms=5000,
    )
    tool_events = [e for e in received if e.event == "tool_call_completed"]
    assert len(tool_events) == 1
    assert tool_events[0].payload["toolName"] == "linear_graphql"
    assert tool_events[0].payload["callId"] == "c1"
    assert tool_events[0].payload["ok"] is True


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
