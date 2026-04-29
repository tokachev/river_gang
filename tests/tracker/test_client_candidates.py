"""Tests for :class:`LinearClient.fetch_candidate_issues` (SPED §11.2)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from river_gang.tracker.client import LinearClient
from river_gang.tracker.errors import LinearMissingEndCursor
from river_gang.tracker.linear_transport import LinearTransport
from river_gang.tracker.queries import CANDIDATES_PAGE_SIZE, CANDIDATES_QUERY

ENDPOINT = "https://api.linear.app/graphql"


# ---------------------------------------------------------------------------
# Query string
# ---------------------------------------------------------------------------


def test_candidates_query_filters_project_by_slug_id() -> None:
    assert "project: { slugId: { eq: $projectSlug } }" in CANDIDATES_QUERY


def test_candidates_query_filters_state_by_active_states() -> None:
    assert "state: { name: { in: $activeStates } }" in CANDIDATES_QUERY


def test_candidates_query_uses_first_and_after_arguments() -> None:
    assert "$first" in CANDIDATES_QUERY
    assert "$after" in CANDIDATES_QUERY


def test_candidates_query_requests_required_issue_fields() -> None:
    """Sanity: we don't drop a §4.1.1 field from the projection."""
    for field in (
        "id",
        "identifier",
        "title",
        "description",
        "priority",
        "state",
        "branchName",
        "url",
        "labels",
        "inverseRelations",
        "createdAt",
        "updatedAt",
    ):
        assert field in CANDIDATES_QUERY, f"missing {field!r} in CANDIDATES_QUERY"


def test_candidates_query_requests_pageinfo_cursor_fields() -> None:
    assert "pageInfo" in CANDIDATES_QUERY
    assert "hasNextPage" in CANDIDATES_QUERY
    assert "endCursor" in CANDIDATES_QUERY


def test_candidates_default_page_size_is_50() -> None:
    assert CANDIDATES_PAGE_SIZE == 50


# ---------------------------------------------------------------------------
# Fixtures: Linear-shaped issue payloads
# ---------------------------------------------------------------------------


def _make_issue(identifier: str, *, state: str = "Todo") -> dict[str, Any]:
    return {
        "id": f"uuid-{identifier}",
        "identifier": identifier,
        "title": f"Title {identifier}",
        "description": None,
        "priority": 2,
        "state": {"name": state},
        "branchName": None,
        "url": None,
        "labels": {"nodes": []},
        "inverseRelations": {"nodes": []},
        "createdAt": "2026-04-01T10:00:00.000Z",
        "updatedAt": "2026-04-01T10:00:00.000Z",
    }


def _page(
    *, nodes: list[dict[str, Any]], end_cursor: str | None, has_next: bool
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
# Helpers
# ---------------------------------------------------------------------------


def _client(api_key: str = "lin_test") -> tuple[LinearClient, LinearTransport]:
    transport = LinearTransport(endpoint=ENDPOINT, api_key=api_key)
    client = LinearClient(transport=transport, project_slug="river-gang")
    return client, transport


def _request_body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content.decode("utf-8"))


# ---------------------------------------------------------------------------
# Pagination: 3-page walk
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_candidate_issues_walks_three_pages() -> None:
    captured_bodies: list[dict[str, Any]] = []
    page_iter: Iterator[httpx.Response] = iter(
        [
            httpx.Response(
                200,
                json=_page(
                    nodes=[_make_issue("RG-1"), _make_issue("RG-2")],
                    end_cursor="cursor-1",
                    has_next=True,
                ),
            ),
            httpx.Response(
                200,
                json=_page(
                    nodes=[_make_issue("RG-3"), _make_issue("RG-4")],
                    end_cursor="cursor-2",
                    has_next=True,
                ),
            ),
            httpx.Response(
                200,
                json=_page(
                    nodes=[_make_issue("RG-5")],
                    end_cursor=None,
                    has_next=False,
                ),
            ),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured_bodies.append(_request_body(request))
        return next(page_iter)

    respx.post(ENDPOINT).mock(side_effect=handler)

    client, transport = _client()
    try:
        issues = await client.fetch_candidate_issues(["Todo", "In Progress"])
    finally:
        await transport.aclose()

    # All issues collected, order preserved across pages
    assert [i.identifier for i in issues] == ["RG-1", "RG-2", "RG-3", "RG-4", "RG-5"]

    # Three POSTs, with cursor passed in subsequent requests
    assert len(captured_bodies) == 3
    assert captured_bodies[0]["variables"]["after"] is None
    assert captured_bodies[1]["variables"]["after"] == "cursor-1"
    assert captured_bodies[2]["variables"]["after"] == "cursor-2"

    # Same query string each time; same project_slug + active_states
    for body in captured_bodies:
        assert body["query"] == CANDIDATES_QUERY
        assert body["variables"]["projectSlug"] == "river-gang"
        assert body["variables"]["activeStates"] == ["Todo", "In Progress"]
        assert body["variables"]["first"] == 50


# ---------------------------------------------------------------------------
# Single-page / empty
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_candidate_issues_single_page() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_page(
                nodes=[_make_issue("RG-1")],
                end_cursor=None,
                has_next=False,
            ),
        )
    )

    client, transport = _client()
    try:
        issues = await client.fetch_candidate_issues(["Todo"])
    finally:
        await transport.aclose()

    assert len(issues) == 1
    assert issues[0].identifier == "RG-1"


@respx.mock
async def test_fetch_candidate_issues_empty_first_page_returns_empty_list() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_page(nodes=[], end_cursor=None, has_next=False),
        )
    )

    client, transport = _client()
    try:
        issues = await client.fetch_candidate_issues(["Todo"])
    finally:
        await transport.aclose()

    assert issues == []
    assert route.call_count == 1


@respx.mock
async def test_fetch_candidate_issues_empty_active_states_skips_api_call() -> None:
    """SPED §17.2: ``fetch_issues_by_states([])`` returns empty without API call."""
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"data": {"issues": {}}})
    )

    client, transport = _client()
    try:
        issues = await client.fetch_candidate_issues([])
    finally:
        await transport.aclose()

    assert issues == []
    assert route.call_count == 0


# ---------------------------------------------------------------------------
# Pagination integrity
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_candidate_issues_missing_end_cursor_raises() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_page(
                nodes=[_make_issue("RG-1")],
                end_cursor=None,
                has_next=True,  # has next page but cursor is missing -- corrupt
            ),
        )
    )

    client, transport = _client()
    try:
        with pytest.raises(LinearMissingEndCursor):
            await client.fetch_candidate_issues(["Todo"])
    finally:
        await transport.aclose()


@respx.mock
async def test_fetch_candidate_issues_empty_string_cursor_treated_as_missing() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_page(
                nodes=[_make_issue("RG-1")],
                end_cursor="",
                has_next=True,
            ),
        )
    )

    client, transport = _client()
    try:
        with pytest.raises(LinearMissingEndCursor):
            await client.fetch_candidate_issues(["Todo"])
    finally:
        await transport.aclose()


# ---------------------------------------------------------------------------
# Variables shape
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_candidate_issues_propagates_project_slug() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(_request_body(request))
        return httpx.Response(
            200,
            json=_page(nodes=[], end_cursor=None, has_next=False),
        )

    respx.post(ENDPOINT).mock(side_effect=handler)

    transport = LinearTransport(endpoint=ENDPOINT, api_key="lin_test")
    client = LinearClient(transport=transport, project_slug="custom-slug")
    try:
        await client.fetch_candidate_issues(["Todo"])
    finally:
        await transport.aclose()

    assert seen["variables"]["projectSlug"] == "custom-slug"


@respx.mock
async def test_fetch_candidate_issues_uses_first_50_page_size() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(_request_body(request))
        return httpx.Response(
            200,
            json=_page(nodes=[], end_cursor=None, has_next=False),
        )

    respx.post(ENDPOINT).mock(side_effect=handler)

    client, transport = _client()
    try:
        await client.fetch_candidate_issues(["Todo"])
    finally:
        await transport.aclose()

    assert seen["variables"]["first"] == 50


# ---------------------------------------------------------------------------
# Order preservation (sortable result)
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_candidate_issues_preserves_node_order_within_page() -> None:
    nodes = [_make_issue(f"RG-{n}") for n in (10, 3, 1, 7, 5)]
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_page(nodes=nodes, end_cursor=None, has_next=False),
        )
    )

    client, transport = _client()
    try:
        issues = await client.fetch_candidate_issues(["Todo"])
    finally:
        await transport.aclose()

    assert [i.identifier for i in issues] == ["RG-10", "RG-3", "RG-1", "RG-7", "RG-5"]


# ---------------------------------------------------------------------------
# Construction guards
# ---------------------------------------------------------------------------


def test_linear_client_requires_non_empty_project_slug() -> None:
    transport = LinearTransport(endpoint=ENDPOINT, api_key="lin_test")
    try:
        with pytest.raises(Exception) as exc_info:
            LinearClient(transport=transport, project_slug="")
        assert "project_slug" in str(exc_info.value).lower()
    finally:
        # cannot await aclose synchronously here; rely on AsyncClient finalisation
        pass
