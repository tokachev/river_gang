"""Tests for state refresh + terminal fetch (SPED §11.1, §11.2, §17.3)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from river_gang.tracker.client import LinearClient
from river_gang.tracker.errors import (
    LinearApiStatus,
    LinearGraphQLErrors,
    LinearMissingEndCursor,
)
from river_gang.tracker.linear_transport import LinearTransport
from river_gang.tracker.queries import (
    STATE_REFRESH_PAGE_SIZE,
    STATE_REFRESH_QUERY,
    TERMINAL_FETCH_QUERY,
)

ENDPOINT = "https://api.linear.app/graphql"


# ---------------------------------------------------------------------------
# Query strings
# ---------------------------------------------------------------------------


def test_state_refresh_query_uses_id_list_variable() -> None:
    """SPED §11.2: refresh query MUST use ``[ID!]`` typing."""
    assert "[ID!]" in STATE_REFRESH_QUERY


def test_state_refresh_query_filters_issues_by_id_in() -> None:
    assert "id: { in: $issueIds }" in STATE_REFRESH_QUERY


def test_state_refresh_query_selects_minimal_fields() -> None:
    """Minimal projection per §17.3 conformance bullet."""
    for field in ("id", "identifier", "title"):
        assert field in STATE_REFRESH_QUERY
    assert "state {" in STATE_REFRESH_QUERY.replace(" ", " ") or "state{" in STATE_REFRESH_QUERY


def test_state_refresh_query_uses_pagination_cursor() -> None:
    assert "$first" in STATE_REFRESH_QUERY
    assert "$after" in STATE_REFRESH_QUERY
    assert "hasNextPage" in STATE_REFRESH_QUERY
    assert "endCursor" in STATE_REFRESH_QUERY


def test_terminal_fetch_query_filters_state_in() -> None:
    assert "state: { name: { in: $stateNames } }" in TERMINAL_FETCH_QUERY


def test_terminal_fetch_query_filters_project_by_slug_id() -> None:
    assert "project: { slugId: { eq: $projectSlug } }" in TERMINAL_FETCH_QUERY


def test_terminal_fetch_query_uses_pagination_cursor() -> None:
    assert "$first" in TERMINAL_FETCH_QUERY
    assert "$after" in TERMINAL_FETCH_QUERY
    assert "hasNextPage" in TERMINAL_FETCH_QUERY
    assert "endCursor" in TERMINAL_FETCH_QUERY


def test_state_refresh_default_page_size_is_50() -> None:
    assert STATE_REFRESH_PAGE_SIZE == 50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client(slug: str = "river-gang") -> tuple[LinearClient, LinearTransport]:
    transport = LinearTransport(endpoint=ENDPOINT, api_key="lin_test")
    client = LinearClient(transport=transport, project_slug=slug)
    return client, transport


def _request_body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content.decode("utf-8"))


def _refresh_node(issue_id: str, identifier: str, state: str) -> dict[str, Any]:
    return {
        "id": issue_id,
        "identifier": identifier,
        "title": f"Title {identifier}",
        "state": {"name": state},
    }


def _full_node(identifier: str, *, state: str) -> dict[str, Any]:
    return {
        "id": f"uuid-{identifier}",
        "identifier": identifier,
        "title": f"Title {identifier}",
        "description": None,
        "priority": None,
        "state": {"name": state},
        "branchName": None,
        "url": None,
        "labels": {"nodes": []},
        "inverseRelations": {"nodes": []},
        "createdAt": "2026-04-01T10:00:00.000Z",
        "updatedAt": "2026-04-01T10:00:00.000Z",
    }


def _refresh_page(
    *,
    nodes: list[dict[str, Any]],
    has_next: bool = False,
    end_cursor: str | None = None,
) -> dict[str, Any]:
    return {
        "data": {
            "issues": {
                "nodes": nodes,
                "pageInfo": {
                    "hasNextPage": has_next,
                    "endCursor": end_cursor,
                },
            }
        }
    }


# ---------------------------------------------------------------------------
# fetch_issue_states_by_ids
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_issue_states_by_ids_empty_skips_api() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"data": {"issues": {}}})
    )

    client, transport = _client()
    try:
        result = await client.fetch_issue_states_by_ids([])
    finally:
        await transport.aclose()

    assert result == []
    assert route.call_count == 0


@respx.mock
async def test_fetch_issue_states_by_ids_passes_ids_in_variables() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(_request_body(request))
        return httpx.Response(
            200,
            json=_refresh_page(
                nodes=[
                    _refresh_node("uuid-1", "RG-1", "In Progress"),
                    _refresh_node("uuid-2", "RG-2", "Done"),
                ]
            ),
        )

    respx.post(ENDPOINT).mock(side_effect=handler)

    client, transport = _client()
    try:
        result = await client.fetch_issue_states_by_ids(["uuid-1", "uuid-2"])
    finally:
        await transport.aclose()

    assert seen["query"] == STATE_REFRESH_QUERY
    assert seen["variables"]["issueIds"] == ["uuid-1", "uuid-2"]
    assert seen["variables"]["first"] == 50
    assert seen["variables"]["after"] is None

    assert [(i.id, i.identifier, i.state) for i in result] == [
        ("uuid-1", "RG-1", "In Progress"),
        ("uuid-2", "RG-2", "Done"),
    ]


@respx.mock
async def test_fetch_issue_states_by_ids_paginates() -> None:
    pages: Iterator[httpx.Response] = iter(
        [
            httpx.Response(
                200,
                json=_refresh_page(
                    nodes=[_refresh_node("u1", "RG-1", "Todo")],
                    has_next=True,
                    end_cursor="cur-1",
                ),
            ),
            httpx.Response(
                200,
                json=_refresh_page(
                    nodes=[_refresh_node("u2", "RG-2", "Done")],
                ),
            ),
        ]
    )

    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(_request_body(request))
        return next(pages)

    respx.post(ENDPOINT).mock(side_effect=handler)

    client, transport = _client()
    try:
        result = await client.fetch_issue_states_by_ids(["u1", "u2"])
    finally:
        await transport.aclose()

    assert [i.identifier for i in result] == ["RG-1", "RG-2"]
    assert captured[0]["variables"]["after"] is None
    assert captured[1]["variables"]["after"] == "cur-1"


@respx.mock
async def test_fetch_issue_states_by_ids_missing_end_cursor_raises() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_refresh_page(
                nodes=[_refresh_node("u1", "RG-1", "Todo")],
                has_next=True,
                end_cursor=None,
            ),
        )
    )

    client, transport = _client()
    try:
        with pytest.raises(LinearMissingEndCursor):
            await client.fetch_issue_states_by_ids(["u1"])
    finally:
        await transport.aclose()


@respx.mock
async def test_fetch_issue_states_by_ids_partial_response_returns_only_known() -> None:
    """If Linear returns fewer issues than requested (e.g. one was deleted),
    we just return what we got — caller infers absence by id-set diff."""
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_refresh_page(
                nodes=[_refresh_node("u1", "RG-1", "Done")],
            ),
        )
    )

    client, transport = _client()
    try:
        result = await client.fetch_issue_states_by_ids(["u1", "u2-missing"])
    finally:
        await transport.aclose()

    assert [i.id for i in result] == ["u1"]


@respx.mock
async def test_fetch_issue_states_by_ids_propagates_error_status() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(503, text="upstream is down")
    )

    client, transport = _client()
    try:
        with pytest.raises(LinearApiStatus) as exc:
            await client.fetch_issue_states_by_ids(["u1"])
    finally:
        await transport.aclose()
    assert exc.value.status == 503


@respx.mock
async def test_fetch_issue_states_by_ids_propagates_graphql_errors() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={"errors": [{"message": "Argument 'filter' was rejected"}]},
        )
    )

    client, transport = _client()
    try:
        with pytest.raises(LinearGraphQLErrors):
            await client.fetch_issue_states_by_ids(["u1"])
    finally:
        await transport.aclose()


# ---------------------------------------------------------------------------
# fetch_issues_by_states
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_issues_by_states_empty_skips_api() -> None:
    """SPED §17.3: empty list returns ``[]`` without HTTP call."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"data": {"issues": {}}})
    )

    client, transport = _client()
    try:
        result = await client.fetch_issues_by_states([])
    finally:
        await transport.aclose()

    assert result == []
    assert route.call_count == 0


@respx.mock
async def test_fetch_issues_by_states_passes_state_names_and_slug() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(_request_body(request))
        return httpx.Response(
            200,
            json=_refresh_page(nodes=[_full_node("RG-1", state="Done")]),
        )

    respx.post(ENDPOINT).mock(side_effect=handler)

    client, transport = _client(slug="river-gang")
    try:
        result = await client.fetch_issues_by_states(["Done", "Cancelled"])
    finally:
        await transport.aclose()

    assert seen["query"] == TERMINAL_FETCH_QUERY
    assert seen["variables"]["projectSlug"] == "river-gang"
    assert seen["variables"]["stateNames"] == ["Done", "Cancelled"]
    assert seen["variables"]["first"] == 50
    assert seen["variables"]["after"] is None
    assert [i.identifier for i in result] == ["RG-1"]
    assert result[0].state == "Done"


@respx.mock
async def test_fetch_issues_by_states_paginates() -> None:
    pages: Iterator[httpx.Response] = iter(
        [
            httpx.Response(
                200,
                json=_refresh_page(
                    nodes=[_full_node("RG-1", state="Done")],
                    has_next=True,
                    end_cursor="cur-1",
                ),
            ),
            httpx.Response(
                200,
                json=_refresh_page(
                    nodes=[_full_node("RG-2", state="Cancelled")],
                ),
            ),
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return next(pages)

    respx.post(ENDPOINT).mock(side_effect=handler)

    client, transport = _client()
    try:
        result = await client.fetch_issues_by_states(["Done", "Cancelled"])
    finally:
        await transport.aclose()

    assert [i.identifier for i in result] == ["RG-1", "RG-2"]


@respx.mock
async def test_fetch_issues_by_states_missing_end_cursor_raises() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_refresh_page(
                nodes=[_full_node("RG-1", state="Done")],
                has_next=True,
                end_cursor=None,
            ),
        )
    )

    client, transport = _client()
    try:
        with pytest.raises(LinearMissingEndCursor):
            await client.fetch_issues_by_states(["Done"])
    finally:
        await transport.aclose()


@respx.mock
async def test_fetch_issues_by_states_propagates_error_status() -> None:
    respx.post(ENDPOINT).mock(return_value=httpx.Response(500, text="boom"))

    client, transport = _client()
    try:
        with pytest.raises(LinearApiStatus) as exc:
            await client.fetch_issues_by_states(["Done"])
    finally:
        await transport.aclose()
    assert exc.value.status == 500
