"""Tests for :class:`FakeTracker` (test-infrastructure fake)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from river_gang.tracker.errors import LinearApiRequest, LinearMissingEndCursor
from river_gang.tracker.issue import BlockerRef, Issue
from tests.tracker.fakes import FakeTracker


def _issue(identifier: str, state: str = "Todo") -> Issue:
    return Issue(
        id=f"uuid-{identifier}",
        identifier=identifier,
        title=f"Title {identifier}",
        state=state,
        description=None,
        priority=None,
        branch_name=None,
        url=None,
        labels=(),
        blocked_by=(),
        created_at=datetime(2026, 4, 1, tzinfo=UTC),
        updated_at=datetime(2026, 4, 1, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# Default behaviour
# ---------------------------------------------------------------------------


async def test_default_construction_returns_empty_results() -> None:
    fake = FakeTracker()
    assert await fake.fetch_candidate_issues(["Todo"]) == []
    assert await fake.fetch_issue_states_by_ids(["x"]) == []
    assert await fake.fetch_issues_by_states(["Done"]) == []


async def test_calls_list_records_each_invocation() -> None:
    fake = FakeTracker()
    await fake.fetch_candidate_issues(["Todo", "In Progress"])
    await fake.fetch_issue_states_by_ids(["a", "b"])
    await fake.fetch_issues_by_states(["Done"])

    assert len(fake.calls) == 3
    assert fake.calls[0] == (
        "fetch_candidate_issues",
        {"active_states": ["Todo", "In Progress"]},
    )
    assert fake.calls[1] == (
        "fetch_issue_states_by_ids",
        {"issue_ids": ["a", "b"]},
    )
    assert fake.calls[2] == (
        "fetch_issues_by_states",
        {"state_names": ["Done"]},
    )


async def test_calls_list_preserves_order_across_method_kinds() -> None:
    fake = FakeTracker()
    await fake.fetch_candidate_issues([])
    await fake.fetch_issues_by_states([])
    await fake.fetch_candidate_issues(["A"])
    methods = [m for m, _ in fake.calls]
    assert methods == [
        "fetch_candidate_issues",
        "fetch_issues_by_states",
        "fetch_candidate_issues",
    ]


# ---------------------------------------------------------------------------
# Configured candidates
# ---------------------------------------------------------------------------


async def test_configured_candidates_returned() -> None:
    issues = [_issue("RG-1"), _issue("RG-2")]
    fake = FakeTracker(candidates=issues)
    assert await fake.fetch_candidate_issues(["Todo"]) == issues


async def test_candidates_can_be_mutated_between_calls() -> None:
    fake = FakeTracker(candidates=[_issue("RG-1")])
    first = await fake.fetch_candidate_issues(["Todo"])
    assert [i.identifier for i in first] == ["RG-1"]

    fake.set_candidates([_issue("RG-2"), _issue("RG-3")])
    second = await fake.fetch_candidate_issues(["Todo"])
    assert [i.identifier for i in second] == ["RG-2", "RG-3"]


async def test_candidates_returns_a_copy_callers_cannot_mutate_state() -> None:
    issues = [_issue("RG-1")]
    fake = FakeTracker(candidates=issues)
    result = await fake.fetch_candidate_issues(["Todo"])
    result.clear()
    # internal state still has the issue
    assert len(await fake.fetch_candidate_issues(["Todo"])) == 1


async def test_empty_active_states_short_circuits_to_empty() -> None:
    """Mirror real :class:`LinearClient` short-circuit so orchestrator tests
    don't see different behaviour against the fake."""
    fake = FakeTracker(candidates=[_issue("RG-1")])
    result = await fake.fetch_candidate_issues([])
    assert result == []
    # BUT the call is still recorded for ordering assertions
    assert ("fetch_candidate_issues", {"active_states": []}) in fake.calls


# ---------------------------------------------------------------------------
# state_refreshes map (keyed by issue id → state name)
# ---------------------------------------------------------------------------


async def test_state_refreshes_returns_minimal_issues_for_known_ids() -> None:
    fake = FakeTracker(
        state_refreshes={
            "uuid-RG-1": "In Progress",
            "uuid-RG-2": "Done",
        }
    )
    result = await fake.fetch_issue_states_by_ids(["uuid-RG-1", "uuid-RG-2"])
    by_id = {i.id: i.state for i in result}
    assert by_id == {"uuid-RG-1": "In Progress", "uuid-RG-2": "Done"}


async def test_state_refreshes_omits_unknown_ids() -> None:
    """SPED §17.3: 'Linear server may return fewer issues than requested.'"""
    fake = FakeTracker(state_refreshes={"uuid-1": "Todo"})
    result = await fake.fetch_issue_states_by_ids(["uuid-1", "uuid-missing"])
    assert [i.id for i in result] == ["uuid-1"]


async def test_state_refreshes_can_be_mutated() -> None:
    fake = FakeTracker()
    fake.set_state_refreshes({"uuid-1": "Done"})
    result = await fake.fetch_issue_states_by_ids(["uuid-1"])
    assert result[0].state == "Done"


# ---------------------------------------------------------------------------
# terminal_by_state map (state name → list[Issue])
# ---------------------------------------------------------------------------


async def test_terminal_by_state_returns_matching_issues() -> None:
    done = [_issue("RG-1", state="Done"), _issue("RG-2", state="Done")]
    cancelled = [_issue("RG-3", state="Cancelled")]
    fake = FakeTracker(
        terminal_by_state={"Done": done, "Cancelled": cancelled}
    )
    result = await fake.fetch_issues_by_states(["Done", "Cancelled"])
    identifiers = sorted(i.identifier for i in result)
    assert identifiers == ["RG-1", "RG-2", "RG-3"]


async def test_terminal_by_state_filters_by_requested_states_only() -> None:
    fake = FakeTracker(
        terminal_by_state={
            "Done": [_issue("RG-1", state="Done")],
            "Cancelled": [_issue("RG-2", state="Cancelled")],
        }
    )
    result = await fake.fetch_issues_by_states(["Done"])
    assert [i.identifier for i in result] == ["RG-1"]


async def test_terminal_by_state_unknown_state_returns_empty() -> None:
    fake = FakeTracker(terminal_by_state={"Done": [_issue("RG-1", state="Done")]})
    result = await fake.fetch_issues_by_states(["Cancelled"])
    assert result == []


# ---------------------------------------------------------------------------
# Failure injection
# ---------------------------------------------------------------------------


async def test_inject_candidate_failure_raises_on_next_call() -> None:
    fake = FakeTracker(candidates=[_issue("RG-1")])
    fake.fail_next_candidates(LinearApiRequest("simulated transport error"))
    with pytest.raises(LinearApiRequest):
        await fake.fetch_candidate_issues(["Todo"])
    # subsequent call recovers (one-shot failure)
    result = await fake.fetch_candidate_issues(["Todo"])
    assert [i.identifier for i in result] == ["RG-1"]


async def test_inject_state_refresh_failure_raises() -> None:
    fake = FakeTracker(state_refreshes={"uuid-1": "Todo"})
    fake.fail_next_state_refreshes(LinearMissingEndCursor("missing cursor"))
    with pytest.raises(LinearMissingEndCursor):
        await fake.fetch_issue_states_by_ids(["uuid-1"])
    # next call recovers
    assert (await fake.fetch_issue_states_by_ids(["uuid-1"]))[0].state == "Todo"


async def test_inject_terminal_failure_raises() -> None:
    fake = FakeTracker(terminal_by_state={"Done": [_issue("RG-1", state="Done")]})
    fake.fail_next_terminal(LinearApiRequest("simulated"))
    with pytest.raises(LinearApiRequest):
        await fake.fetch_issues_by_states(["Done"])


async def test_failure_injection_records_call_before_raising() -> None:
    """Test ordering still holds even when a call fails — the fake records
    the call BEFORE raising so assertions on ``.calls`` still see it."""
    fake = FakeTracker()
    fake.fail_next_candidates(LinearApiRequest("boom"))
    with pytest.raises(LinearApiRequest):
        await fake.fetch_candidate_issues(["Todo"])
    assert fake.calls == [
        ("fetch_candidate_issues", {"active_states": ["Todo"]}),
    ]


# ---------------------------------------------------------------------------
# Blockers passthrough
# ---------------------------------------------------------------------------


async def test_blockers_preserved_on_returned_issues() -> None:
    """The fake doesn't synthesize blockers — it returns the configured Issue
    verbatim including any blocked_by tuple supplied by the test."""
    issue_with_blockers = Issue(
        id="uuid-1",
        identifier="RG-1",
        title="t",
        state="Todo",
        description=None,
        priority=None,
        branch_name=None,
        url=None,
        labels=("bug",),
        blocked_by=(BlockerRef(id="b1", identifier="RG-99", state="In Progress"),),
        created_at=None,
        updated_at=None,
    )
    fake = FakeTracker(candidates=[issue_with_blockers])
    result = await fake.fetch_candidate_issues(["Todo"])
    assert result[0].blocked_by == (
        BlockerRef(id="b1", identifier="RG-99", state="In Progress"),
    )
    assert result[0].labels == ("bug",)
