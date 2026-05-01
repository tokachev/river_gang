"""Tests for :class:`LinearClient` mutations: ``transition_state`` /
``add_comment`` plus the team-scoped ``stateId`` cache.

The transport is stubbed at the :meth:`LinearTransport.execute` boundary
so we exercise the client's GraphQL semantics, mutation result handling,
and cache plumbing without round-tripping through respx.
"""

from __future__ import annotations

from typing import Any

import pytest

from river_gang.tracker.client import LinearClient
from river_gang.tracker.errors import LinearStateNotFound, LinearUnknownPayload
from river_gang.tracker.queries import (
    COMMENT_CREATE_MUTATION,
    ISSUE_UPDATE_STATE_MUTATION,
    STATES_FOR_ISSUE_QUERY,
)


# ---------------------------------------------------------------------------
# Query/mutation strings
# ---------------------------------------------------------------------------


def test_states_for_issue_query_requests_team_states() -> None:
    assert "issue(id: $id)" in STATES_FOR_ISSUE_QUERY
    assert "team" in STATES_FOR_ISSUE_QUERY
    assert "states" in STATES_FOR_ISSUE_QUERY
    assert "nodes" in STATES_FOR_ISSUE_QUERY


def test_issue_update_state_mutation_passes_state_id() -> None:
    assert "issueUpdate" in ISSUE_UPDATE_STATE_MUTATION
    assert "$stateId" in ISSUE_UPDATE_STATE_MUTATION
    assert "stateId: $stateId" in ISSUE_UPDATE_STATE_MUTATION


def test_comment_create_mutation_passes_issue_id_and_body() -> None:
    assert "commentCreate" in COMMENT_CREATE_MUTATION
    assert "$issueId" in COMMENT_CREATE_MUTATION
    assert "$body" in COMMENT_CREATE_MUTATION


# ---------------------------------------------------------------------------
# Stub transport
# ---------------------------------------------------------------------------


class StubTransport:
    """Records executed (query, variables) pairs and returns scripted data."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._responses: list[dict[str, Any]] = []
        self._exceptions: list[BaseException | None] = []

    def push_response(self, data: dict[str, Any]) -> None:
        self._responses.append(data)
        self._exceptions.append(None)

    def push_exception(self, exc: BaseException) -> None:
        self._responses.append({})
        self._exceptions.append(exc)

    async def execute(
        self, query: str, variables: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((query, dict(variables)))
        if not self._responses:
            raise AssertionError(
                "StubTransport.execute called with no scripted response"
            )
        data = self._responses.pop(0)
        exc = self._exceptions.pop(0)
        if exc is not None:
            raise exc
        return data


def _client(transport: StubTransport) -> LinearClient:
    # ``LinearClient.__init__`` accepts any object with ``.execute`` —
    # the type hint says LinearTransport but the call site is duck-typed.
    return LinearClient(transport=transport, project_slug="proj")  # type: ignore[arg-type]


def _states_response(team_id: str = "team-1") -> dict[str, Any]:
    return {
        "issue": {
            "team": {
                "id": team_id,
                "states": {
                    "nodes": [
                        {"id": "st-todo", "name": "Todo"},
                        {"id": "st-progress", "name": "In Progress"},
                        {"id": "st-review", "name": "In Review"},
                        {"id": "st-done", "name": "Done"},
                    ]
                },
            }
        }
    }


# ---------------------------------------------------------------------------
# transition_state happy path
# ---------------------------------------------------------------------------


async def test_transition_state_resolves_id_and_calls_mutation() -> None:
    transport = StubTransport()
    transport.push_response(_states_response())
    transport.push_response({"issueUpdate": {"success": True}})

    client = _client(transport)
    await client.transition_state("iss-1", "In Review")

    assert len(transport.calls) == 2
    states_query, states_vars = transport.calls[0]
    assert states_query == STATES_FOR_ISSUE_QUERY
    assert states_vars == {"id": "iss-1"}

    update_query, update_vars = transport.calls[1]
    assert update_query == ISSUE_UPDATE_STATE_MUTATION
    assert update_vars == {"id": "iss-1", "stateId": "st-review"}


async def test_transition_state_is_case_insensitive_on_name() -> None:
    transport = StubTransport()
    transport.push_response(_states_response())
    transport.push_response({"issueUpdate": {"success": True}})

    client = _client(transport)
    await client.transition_state("iss-1", "in review")  # lowercase

    _, update_vars = transport.calls[1]
    assert update_vars == {"id": "iss-1", "stateId": "st-review"}


# ---------------------------------------------------------------------------
# state-id cache
# ---------------------------------------------------------------------------


async def test_transition_state_cache_skips_second_states_query() -> None:
    transport = StubTransport()
    # First call: states + update.
    transport.push_response(_states_response())
    transport.push_response({"issueUpdate": {"success": True}})
    # Second call should use the cache → only one ``execute`` for the update.
    transport.push_response({"issueUpdate": {"success": True}})

    client = _client(transport)
    await client.transition_state("iss-1", "In Review")
    await client.transition_state("iss-2", "In Progress")

    # Three total executes: states (once) + two issueUpdate mutations.
    assert len(transport.calls) == 3
    queries = [q for q, _ in transport.calls]
    assert queries.count(STATES_FOR_ISSUE_QUERY) == 1
    assert queries.count(ISSUE_UPDATE_STATE_MUTATION) == 2


# ---------------------------------------------------------------------------
# transition_state failure modes
# ---------------------------------------------------------------------------


async def test_transition_state_unknown_state_raises_state_not_found() -> None:
    transport = StubTransport()
    transport.push_response(_states_response())

    client = _client(transport)
    with pytest.raises(LinearStateNotFound) as exc_info:
        await client.transition_state("iss-1", "Doesn't Exist")

    assert exc_info.value.state_name == "Doesn't Exist"
    assert exc_info.value.team_id == "team-1"
    # Only the states lookup happened — issueUpdate was never sent.
    assert [q for q, _ in transport.calls] == [STATES_FOR_ISSUE_QUERY]


async def test_transition_state_success_false_raises_unknown_payload() -> None:
    transport = StubTransport()
    transport.push_response(_states_response())
    transport.push_response({"issueUpdate": {"success": False}})

    client = _client(transport)
    with pytest.raises(LinearUnknownPayload):
        await client.transition_state("iss-1", "In Review")


async def test_transition_state_missing_issueupdate_block_raises() -> None:
    transport = StubTransport()
    transport.push_response(_states_response())
    transport.push_response({"some_other_root": {"success": True}})

    client = _client(transport)
    with pytest.raises(LinearUnknownPayload):
        await client.transition_state("iss-1", "In Review")


async def test_transition_state_propagates_states_lookup_exception() -> None:
    transport = StubTransport()
    transport.push_exception(LinearUnknownPayload("envelope went sideways"))

    client = _client(transport)
    with pytest.raises(LinearUnknownPayload):
        await client.transition_state("iss-1", "In Review")


# ---------------------------------------------------------------------------
# states-for-issue payload validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "broken",
    [
        {"issue": None},
        {"issue": {}},
        {"issue": {"team": None}},
        {"issue": {"team": {"id": None, "states": {"nodes": []}}}},
        {"issue": {"team": {"id": "t-1", "states": None}}},
        {"issue": {"team": {"id": "t-1", "states": {"nodes": "nope"}}}},
    ],
)
async def test_states_for_issue_unexpected_envelope_raises(
    broken: dict[str, Any],
) -> None:
    transport = StubTransport()
    transport.push_response(broken)

    client = _client(transport)
    with pytest.raises(LinearUnknownPayload):
        await client.transition_state("iss-1", "In Review")


# ---------------------------------------------------------------------------
# add_comment
# ---------------------------------------------------------------------------


async def test_add_comment_sends_mutation_with_issue_and_body() -> None:
    transport = StubTransport()
    transport.push_response({"commentCreate": {"success": True}})

    client = _client(transport)
    await client.add_comment("iss-1", "hello world")

    assert len(transport.calls) == 1
    query, variables = transport.calls[0]
    assert query == COMMENT_CREATE_MUTATION
    assert variables == {"issueId": "iss-1", "body": "hello world"}


async def test_add_comment_success_false_raises_unknown_payload() -> None:
    transport = StubTransport()
    transport.push_response({"commentCreate": {"success": False}})

    client = _client(transport)
    with pytest.raises(LinearUnknownPayload):
        await client.add_comment("iss-1", "still here")


async def test_add_comment_missing_block_raises_unknown_payload() -> None:
    transport = StubTransport()
    transport.push_response({"someOtherKey": {"success": True}})

    client = _client(transport)
    with pytest.raises(LinearUnknownPayload):
        await client.add_comment("iss-1", "still here")
