"""Linear comment fetch tests."""

from __future__ import annotations

from typing import Any

import pytest

from river_gang.tracker.client import LinearClient
from river_gang.tracker.errors import LinearMissingEndCursor, LinearUnknownPayload
from river_gang.tracker.queries import ISSUE_COMMENTS_QUERY


class FakeTransport:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((query, dict(variables)))
        if not self._responses:
            raise AssertionError("unexpected execute call")
        return self._responses.pop(0)


def test_issue_comments_query_requests_required_fields() -> None:
    assert "issue(id: $id)" in ISSUE_COMMENTS_QUERY
    assert "comments(first: $first, after: $after)" in ISSUE_COMMENTS_QUERY
    for field in ["id", "body", "createdAt", "updatedAt", "user"]:
        assert field in ISSUE_COMMENTS_QUERY
    assert "pageInfo" in ISSUE_COMMENTS_QUERY
    assert "endCursor" in ISSUE_COMMENTS_QUERY


async def test_fetch_comments_single_page() -> None:
    transport = FakeTransport(
        responses=[
            {
                "issue": {
                    "comments": {
                        "nodes": [
                            {
                                "id": "comment-1",
                                "body": "answer",
                                "user": {"id": "user-1", "name": "Artem"},
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        ]
    )
    client = LinearClient(transport=transport, project_slug="proj")

    comments = await client.fetch_comments("issue-1")

    assert [c.id for c in comments] == ["comment-1"]
    assert comments[0].body == "answer"
    assert transport.calls[0][0] == ISSUE_COMMENTS_QUERY
    assert transport.calls[0][1]["id"] == "issue-1"


async def test_fetch_comments_paginates() -> None:
    transport = FakeTransport(
        responses=[
            {
                "issue": {
                    "comments": {
                        "nodes": [{"id": "c1", "body": "one"}],
                        "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                    }
                }
            },
            {
                "issue": {
                    "comments": {
                        "nodes": [{"id": "c2", "body": "two"}],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            },
        ]
    )
    client = LinearClient(transport=transport, project_slug="proj")

    comments = await client.fetch_comments("issue-1")

    assert [c.id for c in comments] == ["c1", "c2"]
    assert transport.calls[1][1]["after"] == "cursor-1"


async def test_fetch_comments_missing_cursor_raises() -> None:
    transport = FakeTransport(
        responses=[
            {
                "issue": {
                    "comments": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": True, "endCursor": ""},
                    }
                }
            }
        ]
    )
    client = LinearClient(transport=transport, project_slug="proj")

    with pytest.raises(LinearMissingEndCursor):
        await client.fetch_comments("issue-1")


async def test_fetch_comments_malformed_envelope_raises() -> None:
    transport = FakeTransport(responses=[{"issue": None}])
    client = LinearClient(transport=transport, project_slug="proj")

    with pytest.raises(LinearUnknownPayload):
        await client.fetch_comments("issue-1")
