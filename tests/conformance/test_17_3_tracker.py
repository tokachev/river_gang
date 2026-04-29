"""SPED §17.3 conformance: Issue Tracker Client."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from river_gang.tracker.client import LinearClient
from river_gang.tracker.errors import (
    LinearApiRequest,
    LinearApiStatus,
    LinearError,
    LinearGraphQLErrors,
    LinearUnknownPayload,
)
from river_gang.tracker.issue import parse_issue
from river_gang.tracker.linear_transport import LinearTransport
from river_gang.tracker.queries import (
    CANDIDATES_QUERY,
    STATE_REFRESH_QUERY,
)

pytestmark = pytest.mark.conformance

ENDPOINT = "https://api.linear.app/graphql"


def _client(*, page_overrides: dict[str, Any] | None = None) -> LinearClient:
    transport = LinearTransport(endpoint=ENDPOINT, api_key="lit_test")
    return LinearClient(transport=transport, project_slug="rg")


def _issue_node(identifier: str, *, state: str = "Todo") -> dict[str, Any]:
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
        "createdAt": "2026-01-01T00:00:00.000Z",
        "updatedAt": "2026-01-02T00:00:00.000Z",
    }


def _payload(nodes: list[dict[str, Any]], *, has_next: bool = False) -> dict[str, Any]:
    return {
        "data": {
            "issues": {
                "nodes": nodes,
                "pageInfo": {
                    "hasNextPage": has_next,
                    "endCursor": "cursor-x" if has_next else None,
                },
            }
        }
    }


# ---------------------------------------------------------------------------
# Candidate fetch uses active states + project slug
# ---------------------------------------------------------------------------


@respx.mock
async def test_candidate_fetch_uses_active_states_and_project_slug() -> None:
    """Conformance §17.3: candidate issue fetch uses active states and
    project slug."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        body = json.loads(request.content)
        captured["variables"] = body["variables"]
        return httpx.Response(200, json=_payload([]))

    respx.post(ENDPOINT).mock(side_effect=handler)
    client = _client()
    await client.fetch_candidate_issues(["Todo", "In Progress"])

    assert captured["variables"]["projectSlug"] == "rg"
    assert captured["variables"]["activeStates"] == ["Todo", "In Progress"]


def test_candidates_query_uses_slug_id_filter() -> None:
    """Conformance §17.3: Linear query uses the specified project filter
    field (``slugId``)."""
    assert "slugId" in CANDIDATES_QUERY


# ---------------------------------------------------------------------------
# Empty fetch_issues_by_states([]) — short-circuit
# ---------------------------------------------------------------------------


async def test_empty_fetch_issues_by_states_no_api_call() -> None:
    """Conformance §17.3: empty ``fetch_issues_by_states([])`` returns
    empty without API call."""
    # No respx.mock — if the client tried to hit the network the test
    # would raise (no mock configured). Empty input must short-circuit.
    client = _client()
    result = await client.fetch_issues_by_states([])
    assert result == []


# ---------------------------------------------------------------------------
# Pagination preserves order
# ---------------------------------------------------------------------------


@respx.mock
async def test_pagination_preserves_order() -> None:
    """Conformance §17.3: pagination preserves order across multiple pages."""
    page1 = _payload([_issue_node("MT-A"), _issue_node("MT-B")], has_next=True)
    page2 = _payload([_issue_node("MT-C")], has_next=False)

    responses = iter([
        httpx.Response(200, json=page1),
        httpx.Response(200, json=page2),
    ])
    respx.post(ENDPOINT).mock(side_effect=lambda _r: next(responses))

    client = _client()
    issues = await client.fetch_candidate_issues(["Todo"])
    assert [i.identifier for i in issues] == ["MT-A", "MT-B", "MT-C"]


# ---------------------------------------------------------------------------
# Blocker normalization — only ``type=blocks`` from inverseRelations
# ---------------------------------------------------------------------------


def test_blockers_normalized_from_inverse_relations_blocks() -> None:
    """Conformance §17.3: blockers are normalized from inverse relations
    of type ``blocks``."""
    payload = {
        "id": "u-1",
        "identifier": "MT-1",
        "title": "t",
        "state": {"name": "Todo"},
        "inverseRelations": {
            "nodes": [
                {
                    "type": "blocks",
                    "issue": {"id": "b1", "identifier": "B-1", "state": {"name": "Done"}},
                },
                {
                    "type": "duplicate_of",  # ignored
                    "issue": {"id": "b2", "identifier": "B-2", "state": {"name": "Done"}},
                },
            ]
        },
    }
    issue = parse_issue(payload)
    assert len(issue.blocked_by) == 1
    assert issue.blocked_by[0].identifier == "B-1"


# ---------------------------------------------------------------------------
# Labels lowercased
# ---------------------------------------------------------------------------


def test_labels_normalized_to_lowercase() -> None:
    """Conformance §17.3: labels are normalized to lowercase."""
    payload = {
        "id": "u-1",
        "identifier": "MT-1",
        "title": "t",
        "state": {"name": "Todo"},
        "labels": {"nodes": [{"name": "Backend"}, {"name": "URGENT"}]},
    }
    issue = parse_issue(payload)
    assert issue.labels == ("backend", "urgent")


# ---------------------------------------------------------------------------
# Issue state refresh — minimal projection
# ---------------------------------------------------------------------------


@respx.mock
async def test_state_refresh_returns_minimal_normalized_issues() -> None:
    """Conformance §17.3: issue state refresh by ID returns minimal
    normalized issues."""
    minimal = {
        "id": "uuid-1", "identifier": "MT-1", "title": "T",
        "state": {"name": "Done"},
    }
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_payload([minimal]))
    )
    client = _client()
    rows = await client.fetch_issue_states_by_ids(["uuid-1"])
    assert len(rows) == 1
    assert rows[0].state == "Done"
    assert rows[0].labels == ()  # minimal projection


def test_state_refresh_query_uses_id_bang_array() -> None:
    """Conformance §17.3: issue state refresh query uses GraphQL ID typing
    (``[ID!]``) as specified in Section 11.2."""
    assert "$issueIds: [ID!]" in STATE_REFRESH_QUERY


# ---------------------------------------------------------------------------
# Error mapping — request, non-200, GraphQL errors, malformed payload
# ---------------------------------------------------------------------------


@respx.mock
async def test_error_mapping_request_error() -> None:
    """Conformance §17.3: error mapping for request errors."""
    respx.post(ENDPOINT).mock(side_effect=httpx.ConnectError("dns"))
    client = _client()
    with pytest.raises(LinearApiRequest):
        await client.fetch_candidate_issues(["Todo"])


@respx.mock
async def test_error_mapping_non_200_status() -> None:
    """Conformance §17.3: error mapping for non-200."""
    respx.post(ENDPOINT).mock(return_value=httpx.Response(500, text="oops"))
    client = _client()
    with pytest.raises(LinearApiStatus):
        await client.fetch_candidate_issues(["Todo"])


@respx.mock
async def test_error_mapping_graphql_errors_in_body() -> None:
    """Conformance §17.3: error mapping for GraphQL errors."""
    body = {"errors": [{"message": "bad query"}]}
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=body))
    client = _client()
    with pytest.raises(LinearGraphQLErrors):
        await client.fetch_candidate_issues(["Todo"])


@respx.mock
async def test_error_mapping_malformed_payload() -> None:
    """Conformance §17.3: error mapping for malformed payloads."""
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )
    client = _client()
    with pytest.raises((LinearUnknownPayload, LinearError)):
        await client.fetch_candidate_issues(["Todo"])
