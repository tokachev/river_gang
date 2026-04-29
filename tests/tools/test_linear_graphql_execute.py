"""Tests for :class:`LinearGraphqlTool.execute` (SPED §10.5 result semantics)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from river_gang.tools.linear_graphql import (
    LinearGraphqlTool,
    ToolResult,
)
from river_gang.tracker.errors import MissingTrackerApiKey
from river_gang.tracker.linear_transport import LinearTransport

ENDPOINT = "https://api.linear.app/graphql"


def _client() -> tuple[LinearGraphqlTool, LinearTransport]:
    transport = LinearTransport(endpoint=ENDPOINT, api_key="lin_test")
    tool = LinearGraphqlTool(transport=transport)
    return tool, transport


def _request_body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content.decode("utf-8"))


# ---------------------------------------------------------------------------
# ToolResult dataclass shape
# ---------------------------------------------------------------------------


def test_tool_result_is_frozen_dataclass() -> None:
    r = ToolResult(success=True, data={"x": 1}, errors=None, error_message=None)
    with pytest.raises(Exception):
        r.success = False  # type: ignore[misc]


def test_tool_result_success_factory_shape() -> None:
    r = ToolResult(success=True, data={"viewer": {"id": 1}}, errors=None, error_message=None)
    assert r.success is True
    assert r.data == {"viewer": {"id": 1}}
    assert r.errors is None
    assert r.error_message is None


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@respx.mock
async def test_execute_success_returns_data() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(_request_body(request))
        return httpx.Response(
            200, json={"data": {"viewer": {"id": "u-1"}}}
        )

    respx.post(ENDPOINT).mock(side_effect=handler)

    tool, transport = _client()
    try:
        result = await tool.execute(
            {"query": "query Viewer { viewer { id } }", "variables": {"x": 1}}
        )
    finally:
        await transport.aclose()

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert result.data == {"viewer": {"id": "u-1"}}
    assert result.errors is None
    assert result.error_message is None

    assert seen["query"] == "query Viewer { viewer { id } }"
    assert seen["variables"] == {"x": 1}


@respx.mock
async def test_execute_with_raw_string_shorthand() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"data": {"ok": True}})
    )

    tool, transport = _client()
    try:
        result = await tool.execute("{ viewer { id } }")
    finally:
        await transport.aclose()

    assert result.success is True
    assert result.data == {"ok": True}


@respx.mock
async def test_execute_passes_variables_through() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(_request_body(request))
        return httpx.Response(200, json={"data": {"ok": True}})

    respx.post(ENDPOINT).mock(side_effect=handler)

    tool, transport = _client()
    try:
        await tool.execute(
            {
                "query": "query Q($a: Int!, $b: String!) { x }",
                "variables": {"a": 42, "b": "hello"},
            }
        )
    finally:
        await transport.aclose()

    assert seen["variables"] == {"a": 42, "b": "hello"}


@respx.mock
async def test_execute_default_empty_variables_sent_as_object() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(_request_body(request))
        return httpx.Response(200, json={"data": {"ok": True}})

    respx.post(ENDPOINT).mock(side_effect=handler)

    tool, transport = _client()
    try:
        await tool.execute({"query": "{ viewer { id } }"})
    finally:
        await transport.aclose()

    assert seen["variables"] == {}


# ---------------------------------------------------------------------------
# GraphQL errors — body preserved per §10.5
# ---------------------------------------------------------------------------


@respx.mock
async def test_execute_graphql_errors_only_returns_failure_with_errors_list() -> None:
    payload = {
        "errors": [
            {"message": "Field 'viewer' is forbidden", "extensions": {"code": "X"}},
            {"message": "Second"},
        ]
    }
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=payload))

    tool, transport = _client()
    try:
        result = await tool.execute({"query": "{ viewer { id } }"})
    finally:
        await transport.aclose()

    assert result.success is False
    assert result.errors is not None
    assert len(result.errors) == 2
    assert result.errors[0]["message"] == "Field 'viewer' is forbidden"
    # No partial data on this response → data is None
    assert result.data is None
    assert result.error_message is not None
    assert "graphql" in result.error_message.lower()


@respx.mock
async def test_execute_graphql_errors_with_partial_data_preserves_body() -> None:
    """SPED §10.5: GraphQL response body MUST be preserved when errors fire."""
    payload = {
        "data": {"viewer": None, "team": {"id": "t-1"}},
        "errors": [{"message": "permission denied on viewer"}],
    }
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=payload))

    tool, transport = _client()
    try:
        result = await tool.execute({"query": "{ viewer { id } team { id } }"})
    finally:
        await transport.aclose()

    assert result.success is False
    assert result.data == {"viewer": None, "team": {"id": "t-1"}}
    assert result.errors is not None
    assert len(result.errors) == 1


# ---------------------------------------------------------------------------
# Transport failures
# ---------------------------------------------------------------------------


@respx.mock
async def test_execute_non_200_returns_failure_with_status_in_message() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(503, text="upstream down")
    )

    tool, transport = _client()
    try:
        result = await tool.execute({"query": "{ viewer { id } }"})
    finally:
        await transport.aclose()

    assert result.success is False
    assert result.error_message is not None
    assert "503" in result.error_message
    assert result.data is None
    assert result.errors is None


@respx.mock
async def test_execute_401_unauthorized_returns_failure() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(401, json={"message": "Bad token"})
    )

    tool, transport = _client()
    try:
        result = await tool.execute({"query": "{ viewer { id } }"})
    finally:
        await transport.aclose()

    assert result.success is False
    assert result.error_message is not None
    assert "401" in result.error_message


@respx.mock
async def test_execute_transport_connection_error_returns_failure() -> None:
    respx.post(ENDPOINT).mock(side_effect=httpx.ConnectError("boom"))

    tool, transport = _client()
    try:
        result = await tool.execute({"query": "{ viewer { id } }"})
    finally:
        await transport.aclose()

    assert result.success is False
    assert result.error_message is not None
    assert result.data is None
    assert result.errors is None


@respx.mock
async def test_execute_timeout_returns_failure() -> None:
    respx.post(ENDPOINT).mock(side_effect=httpx.TimeoutException("slow"))

    tool, transport = _client()
    try:
        result = await tool.execute({"query": "{ viewer { id } }"})
    finally:
        await transport.aclose()

    assert result.success is False
    assert result.error_message is not None


@respx.mock
async def test_execute_malformed_json_response_returns_failure() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            content=b"<html>not json</html>",
            headers={"content-type": "application/json"},
        )
    )

    tool, transport = _client()
    try:
        result = await tool.execute({"query": "{ viewer { id } }"})
    finally:
        await transport.aclose()

    assert result.success is False
    assert result.error_message is not None


# ---------------------------------------------------------------------------
# Input validation failures (no API call should fire)
# ---------------------------------------------------------------------------


@respx.mock
async def test_execute_empty_query_returns_failure_without_api_call() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"data": {"ok": True}})
    )

    tool, transport = _client()
    try:
        result = await tool.execute({"query": ""})
    finally:
        await transport.aclose()

    assert result.success is False
    assert result.error_message is not None
    assert "query" in result.error_message.lower()
    assert route.call_count == 0


@respx.mock
async def test_execute_multiple_operations_returns_failure_without_api_call() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"data": {"ok": True}})
    )

    tool, transport = _client()
    try:
        result = await tool.execute({"query": "query A { x } mutation B { y }"})
    finally:
        await transport.aclose()

    assert result.success is False
    assert result.error_message is not None
    assert "operation" in result.error_message.lower() or "2" in result.error_message
    assert route.call_count == 0


@respx.mock
async def test_execute_invalid_graphql_returns_failure_without_api_call() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"data": {"ok": True}})
    )

    tool, transport = _client()
    try:
        result = await tool.execute({"query": "this is not graphql {"})
    finally:
        await transport.aclose()

    assert result.success is False
    assert route.call_count == 0


@respx.mock
async def test_execute_non_dict_variables_returns_failure_without_api_call() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"data": {"ok": True}})
    )

    tool, transport = _client()
    try:
        result = await tool.execute(
            {"query": "{ viewer { id } }", "variables": [1, 2, 3]}
        )
    finally:
        await transport.aclose()

    assert result.success is False
    assert "variables" in result.error_message.lower()  # type: ignore[union-attr]
    assert route.call_count == 0


# ---------------------------------------------------------------------------
# Missing auth — surface as failure without crashing
# ---------------------------------------------------------------------------


async def test_execute_handles_missing_api_key_at_runtime() -> None:
    """Even though :class:`LinearTransport` rejects empty api_key at
    construction, model the case where a stale/closed transport raises
    ``MissingTrackerApiKey`` mid-call. The tool MUST surface it as
    success=False rather than propagate."""

    class StubMissingAuthTransport:
        async def execute(
            self, query: str, variables: dict[str, Any]
        ) -> dict[str, Any]:
            raise MissingTrackerApiKey("api key was rotated out")

    tool = LinearGraphqlTool(transport=StubMissingAuthTransport())  # type: ignore[arg-type]
    result = await tool.execute({"query": "{ viewer { id } }"})

    assert result.success is False
    assert result.error_message is not None
    assert "api key" in result.error_message.lower()


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


@respx.mock
async def test_execute_mutation_works() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200, json={"data": {"updateIssue": {"success": True}}}
        )
    )

    tool, transport = _client()
    try:
        result = await tool.execute(
            {
                "query": "mutation U($id: String!) { updateIssue(id: $id) { success } }",
                "variables": {"id": "i-1"},
            }
        )
    finally:
        await transport.aclose()

    assert result.success is True
    assert result.data == {"updateIssue": {"success": True}}


# ---------------------------------------------------------------------------
# Reuse contract: the tool MUST NOT instantiate a new transport
# ---------------------------------------------------------------------------


def test_tool_holds_reference_to_supplied_transport() -> None:
    transport = LinearTransport(endpoint=ENDPOINT, api_key="lin_test")
    tool = LinearGraphqlTool(transport=transport)
    assert tool.transport is transport
