"""Tests for :mod:`river_gang.orchestrator.dispatch` (SPED §8.2)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from river_gang.orchestrator import OrchestratorState
from river_gang.orchestrator.dispatch import filter_candidates, sort_for_dispatch
from river_gang.tracker.issue import BlockerRef, Issue

ACTIVE = ("Todo", "In Progress")
TERMINAL = ("Done", "Cancelled", "Canceled", "Duplicate", "Closed")


def _issue(
    *,
    id: str = "id-1",
    identifier: str = "MT-1",
    state: str = "Todo",
    priority: int | None = None,
    created_at: datetime | None = None,
    blocked_by: tuple[BlockerRef, ...] = (),
) -> Issue:
    return Issue(
        id=id,
        identifier=identifier,
        title="t",
        state=state,
        description=None,
        priority=priority,
        branch_name=None,
        url=None,
        labels=(),
        blocked_by=blocked_by,
        created_at=created_at,
        updated_at=None,
    )


def _state(
    *,
    running_ids: tuple[str, ...] = (),
    claimed_ids: tuple[str, ...] = (),
) -> OrchestratorState:
    s = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=10)
    for iid in running_ids:
        # We don't need a real RunningEntry — filter_candidates only looks at
        # state.running keys. Insert a sentinel via raw dict mutation.
        s.running[iid] = None  # type: ignore[assignment]
    for iid in claimed_ids:
        s.mark_claimed(iid)
    return s


# ---------------------------------------------------------------------------
# active_states / terminal_states gates
# ---------------------------------------------------------------------------


def test_state_in_active_states_accepted() -> None:
    issues = [_issue(state="Todo"), _issue(id="b", identifier="MT-2", state="In Progress")]
    out = filter_candidates(
        issues, _state(), active_states=ACTIVE, terminal_states=TERMINAL
    )
    assert {i.id for i in out} == {"id-1", "b"}


def test_state_match_is_case_insensitive() -> None:
    issues = [_issue(state="todo"), _issue(id="b", identifier="MT-2", state="IN PROGRESS")]
    out = filter_candidates(
        issues, _state(), active_states=ACTIVE, terminal_states=TERMINAL
    )
    assert {i.id for i in out} == {"id-1", "b"}


def test_state_not_in_active_or_terminal_excluded() -> None:
    issues = [_issue(state="Triage"), _issue(id="b", identifier="MT-2", state="Backlog")]
    out = filter_candidates(
        issues, _state(), active_states=ACTIVE, terminal_states=TERMINAL
    )
    assert out == []


@pytest.mark.parametrize("state", ["Done", "Cancelled", "Canceled", "Duplicate", "Closed"])
def test_state_in_terminal_states_excluded(state: str) -> None:
    issues = [_issue(state=state)]
    out = filter_candidates(
        issues, _state(), active_states=ACTIVE, terminal_states=TERMINAL
    )
    assert out == []


def test_terminal_match_is_case_insensitive() -> None:
    issues = [_issue(state="DONE")]
    out = filter_candidates(
        issues, _state(), active_states=ACTIVE, terminal_states=TERMINAL
    )
    assert out == []


# ---------------------------------------------------------------------------
# running / claimed gates
# ---------------------------------------------------------------------------


def test_already_in_running_excluded() -> None:
    issues = [_issue(id="x", state="Todo")]
    out = filter_candidates(
        issues, _state(running_ids=("x",)), active_states=ACTIVE, terminal_states=TERMINAL
    )
    assert out == []


def test_in_claimed_excluded() -> None:
    issues = [_issue(id="x", state="Todo")]
    out = filter_candidates(
        issues, _state(claimed_ids=("x",)), active_states=ACTIVE, terminal_states=TERMINAL
    )
    assert out == []


def test_other_running_does_not_affect_candidate() -> None:
    issues = [_issue(id="x", state="Todo")]
    out = filter_candidates(
        issues,
        _state(running_ids=("y",), claimed_ids=("z",)),
        active_states=ACTIVE,
        terminal_states=TERMINAL,
    )
    assert [i.id for i in out] == ["x"]


# ---------------------------------------------------------------------------
# Blocker rule (§8.2)
# ---------------------------------------------------------------------------


def _blocker(state: str | None) -> BlockerRef:
    return BlockerRef(id="blk-1", identifier="MT-BLK", state=state)


@pytest.mark.parametrize("issue_state", ["Todo", "In Progress"])
@pytest.mark.parametrize("blocker_state", ["Todo", "In Progress", "Backlog", "Triage"])
def test_non_terminal_blocker_excludes(issue_state: str, blocker_state: str) -> None:
    issues = [_issue(state=issue_state, blocked_by=(_blocker(blocker_state),))]
    out = filter_candidates(
        issues, _state(), active_states=ACTIVE, terminal_states=TERMINAL
    )
    assert out == []


@pytest.mark.parametrize("blocker_state", ["Done", "Cancelled", "Canceled", "Duplicate", "Closed"])
def test_terminal_blocker_does_not_block(blocker_state: str) -> None:
    issues = [_issue(state="Todo", blocked_by=(_blocker(blocker_state),))]
    out = filter_candidates(
        issues, _state(), active_states=ACTIVE, terminal_states=TERMINAL
    )
    assert [i.id for i in out] == ["id-1"]


def test_terminal_blocker_match_is_case_insensitive() -> None:
    issues = [_issue(state="Todo", blocked_by=(_blocker("done"),))]
    out = filter_candidates(
        issues, _state(), active_states=ACTIVE, terminal_states=TERMINAL
    )
    assert [i.id for i in out] == ["id-1"]


def test_mixed_blockers_any_non_terminal_excludes() -> None:
    issues = [
        _issue(
            state="Todo",
            blocked_by=(_blocker("Done"), _blocker("Todo")),
        )
    ]
    out = filter_candidates(
        issues, _state(), active_states=ACTIVE, terminal_states=TERMINAL
    )
    assert out == []


def test_no_blockers_passes() -> None:
    issues = [_issue(state="Todo", blocked_by=())]
    out = filter_candidates(
        issues, _state(), active_states=ACTIVE, terminal_states=TERMINAL
    )
    assert [i.id for i in out] == ["id-1"]


def test_blocker_with_unknown_state_treated_as_non_terminal() -> None:
    """``BlockerRef.state`` may be None when the tracker omits state info.

    Conservative posture: an unknown blocker state is NOT terminal, so it
    blocks dispatch — better to wait than to dispatch into an unresolved
    dependency.
    """
    issues = [_issue(state="Todo", blocked_by=(_blocker(None),))]
    out = filter_candidates(
        issues, _state(), active_states=ACTIVE, terminal_states=TERMINAL
    )
    assert out == []


# ---------------------------------------------------------------------------
# Sort order
# ---------------------------------------------------------------------------


def test_sort_priority_ascending() -> None:
    issues = [
        _issue(id="a", identifier="MT-A", priority=4),
        _issue(id="b", identifier="MT-B", priority=1),
        _issue(id="c", identifier="MT-C", priority=2),
        _issue(id="d", identifier="MT-D", priority=3),
    ]
    out = sort_for_dispatch(issues)
    assert [i.id for i in out] == ["b", "c", "d", "a"]


def test_sort_priority_none_last() -> None:
    issues = [
        _issue(id="a", identifier="MT-A", priority=None),
        _issue(id="b", identifier="MT-B", priority=2),
        _issue(id="c", identifier="MT-C", priority=None),
        _issue(id="d", identifier="MT-D", priority=4),
    ]
    out = sort_for_dispatch(issues)
    # b(2), d(4), then None-priority issues (a, c) — order between Nones
    # falls back to identifier lex.
    assert [i.id for i in out] == ["b", "d", "a", "c"]


def test_sort_equal_priority_oldest_created_first() -> None:
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = datetime(2026, 4, 1, tzinfo=UTC)
    issues = [
        _issue(id="a", identifier="MT-A", priority=2, created_at=newer),
        _issue(id="b", identifier="MT-B", priority=2, created_at=older),
    ]
    out = sort_for_dispatch(issues)
    assert [i.id for i in out] == ["b", "a"]


def test_sort_equal_priority_equal_time_identifier_lex() -> None:
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    issues = [
        _issue(id="a", identifier="MT-9", priority=1, created_at=ts),
        _issue(id="b", identifier="MT-2", priority=1, created_at=ts),
        _issue(id="c", identifier="MT-12", priority=1, created_at=ts),
    ]
    out = sort_for_dispatch(issues)
    # Lex-sort over identifier strings: "MT-12" < "MT-2" < "MT-9".
    assert [i.identifier for i in out] == ["MT-12", "MT-2", "MT-9"]


def test_sort_created_at_none_sorts_after_dated() -> None:
    """``created_at`` may be None when tracker omitted it. Dated issues
    must come before undated within the same priority bucket — undated
    falls back to identifier tie-break only.
    """
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    issues = [
        _issue(id="a", identifier="MT-A", priority=2, created_at=None),
        _issue(id="b", identifier="MT-B", priority=2, created_at=ts),
    ]
    out = sort_for_dispatch(issues)
    assert [i.id for i in out] == ["b", "a"]


def test_sort_is_stable_across_equal_keys() -> None:
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    issues = [
        _issue(id=f"id-{i}", identifier=f"MT-{i:02d}", priority=2, created_at=ts)
        for i in range(5)
    ]
    out = sort_for_dispatch(issues)
    assert [i.id for i in out] == [f"id-{i}" for i in range(5)]


def test_sort_does_not_mutate_input() -> None:
    issues = [
        _issue(id="a", identifier="MT-A", priority=4),
        _issue(id="b", identifier="MT-B", priority=1),
    ]
    snapshot = list(issues)
    sort_for_dispatch(issues)
    assert issues == snapshot


# ---------------------------------------------------------------------------
# Combined filter + sort
# ---------------------------------------------------------------------------


def test_filter_then_sort_realistic_mix() -> None:
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = datetime(2026, 4, 1, tzinfo=UTC)
    issues = [
        _issue(
            id="running",
            identifier="MT-R",
            state="In Progress",
            priority=1,
            created_at=older,
        ),  # excluded — already running
        _issue(
            id="claimed",
            identifier="MT-C",
            state="Todo",
            priority=1,
            created_at=older,
        ),  # excluded — claimed
        _issue(
            id="done",
            identifier="MT-D",
            state="Done",
            priority=1,
            created_at=older,
        ),  # excluded — terminal
        _issue(
            id="blocked",
            identifier="MT-B",
            state="Todo",
            priority=1,
            created_at=older,
            blocked_by=(_blocker("Todo"),),
        ),  # excluded — non-terminal blocker
        _issue(
            id="ok-low",
            identifier="MT-LO",
            state="Todo",
            priority=4,
            created_at=older,
        ),
        _issue(
            id="ok-high-new",
            identifier="MT-HN",
            state="In Progress",
            priority=1,
            created_at=newer,
        ),
        _issue(
            id="ok-high-old",
            identifier="MT-HO",
            state="In Progress",
            priority=1,
            created_at=older,
        ),
    ]
    state = _state(running_ids=("running",), claimed_ids=("claimed",))
    filtered = filter_candidates(
        issues, state, active_states=ACTIVE, terminal_states=TERMINAL
    )
    assert {i.id for i in filtered} == {"ok-low", "ok-high-new", "ok-high-old"}

    sorted_ = sort_for_dispatch(filtered)
    assert [i.id for i in sorted_] == ["ok-high-old", "ok-high-new", "ok-low"]
