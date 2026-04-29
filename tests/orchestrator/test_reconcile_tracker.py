"""Tests for tracker reconciliation in :mod:`river_gang.orchestrator.reconcile`
(SPED §8.5 Part B)."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime

from river_gang.orchestrator import (
    OrchestratorState,
    ReconcileActions,
    RunningEntry,
    reconcile_running_with_tracker,
)
from river_gang.tracker.issue import Issue

ACTIVE = ("Todo", "In Progress")
TERMINAL = ("Done", "Cancelled", "Canceled", "Duplicate", "Closed")


def _issue(
    *,
    id: str,
    identifier: str | None = None,
    state: str = "In Progress",
    title: str = "title",
) -> Issue:
    return Issue(
        id=id,
        identifier=identifier or f"MT-{id}",
        title=title,
        state=state,
        description=None,
        priority=None,
        branch_name=None,
        url=None,
        labels=(),
        blocked_by=(),
        created_at=None,
        updated_at=None,
    )


def _entry(issue: Issue) -> RunningEntry:
    return RunningEntry(
        worker_handle=None,  # type: ignore[arg-type]
        monitor_handle=None,
        identifier=issue.identifier,
        issue=issue,
        session_id=None,
        last_reported_input_tokens=0,
        last_reported_output_tokens=0,
        last_reported_total_tokens=0,
        started_at=datetime.now(UTC),
        last_codex_timestamp=None,
        last_codex_event=None,
        last_codex_message=None,
        recent_events=deque(maxlen=50),
        last_error=None,
        restart_count=0,
        retry_attempt=0,
    )


def _state_with(*entries: RunningEntry) -> OrchestratorState:
    s = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=10)
    for e in entries:
        s.add_running(e)
    return s


# ---------------------------------------------------------------------------
# Dataclass shape
# ---------------------------------------------------------------------------


def test_reconcile_actions_default_empty() -> None:
    actions = ReconcileActions(
        terminate_with_cleanup=[],
        terminate_without_cleanup=[],
        update_snapshot={},
    )
    assert actions.terminate_with_cleanup == []
    assert actions.terminate_without_cleanup == []
    assert actions.update_snapshot == {}


# ---------------------------------------------------------------------------
# Empty cases
# ---------------------------------------------------------------------------


def test_empty_running_returns_empty_actions() -> None:
    actions = reconcile_running_with_tracker(
        _state_with(),
        refreshed_issues=[_issue(id="a")],
        terminal_states=TERMINAL,
        active_states=ACTIVE,
    )
    assert actions.terminate_with_cleanup == []
    assert actions.terminate_without_cleanup == []
    assert actions.update_snapshot == {}


def test_empty_refreshed_treats_all_running_as_missing() -> None:
    state = _state_with(_entry(_issue(id="a")), _entry(_issue(id="b")))
    actions = reconcile_running_with_tracker(
        state,
        refreshed_issues=[],
        terminal_states=TERMINAL,
        active_states=ACTIVE,
    )
    assert actions.terminate_without_cleanup == ["a", "b"]
    assert actions.terminate_with_cleanup == []
    assert actions.update_snapshot == {}


# ---------------------------------------------------------------------------
# Per-issue transitions
# ---------------------------------------------------------------------------


def test_terminal_state_goes_to_terminate_with_cleanup() -> None:
    state = _state_with(_entry(_issue(id="a", state="In Progress")))
    refreshed = [_issue(id="a", state="Done")]
    actions = reconcile_running_with_tracker(
        state,
        refreshed_issues=refreshed,
        terminal_states=TERMINAL,
        active_states=ACTIVE,
    )
    assert actions.terminate_with_cleanup == ["a"]
    assert actions.terminate_without_cleanup == []
    assert actions.update_snapshot == {}


def test_active_state_goes_to_update_snapshot_with_refreshed_value() -> None:
    state = _state_with(
        _entry(_issue(id="a", state="In Progress", title="old title"))
    )
    refreshed_issue = _issue(id="a", state="In Progress", title="new title")
    actions = reconcile_running_with_tracker(
        state,
        refreshed_issues=[refreshed_issue],
        terminal_states=TERMINAL,
        active_states=ACTIVE,
    )
    assert actions.terminate_with_cleanup == []
    assert actions.terminate_without_cleanup == []
    assert actions.update_snapshot == {"a": refreshed_issue}
    # The snapshot must be the refreshed instance, not the stale one.
    assert actions.update_snapshot["a"].title == "new title"


def test_unexpected_state_goes_to_terminate_without_cleanup() -> None:
    """State left active list but isn't terminal either (e.g. moved to
    'Backlog' or 'Triage'). Preserve workspace for evidence.
    """
    state = _state_with(_entry(_issue(id="a", state="In Progress")))
    refreshed = [_issue(id="a", state="Backlog")]
    actions = reconcile_running_with_tracker(
        state,
        refreshed_issues=refreshed,
        terminal_states=TERMINAL,
        active_states=ACTIVE,
    )
    assert actions.terminate_without_cleanup == ["a"]
    assert actions.terminate_with_cleanup == []
    assert actions.update_snapshot == {}


def test_missing_from_refreshed_goes_to_terminate_without_cleanup() -> None:
    state = _state_with(_entry(_issue(id="a", state="In Progress")))
    actions = reconcile_running_with_tracker(
        state,
        refreshed_issues=[_issue(id="other", state="In Progress")],
        terminal_states=TERMINAL,
        active_states=ACTIVE,
    )
    assert actions.terminate_without_cleanup == ["a"]
    assert actions.terminate_with_cleanup == []
    assert actions.update_snapshot == {}


# ---------------------------------------------------------------------------
# Case-insensitive matching
# ---------------------------------------------------------------------------


def test_case_insensitive_terminal_match() -> None:
    state = _state_with(_entry(_issue(id="a", state="In Progress")))
    actions = reconcile_running_with_tracker(
        state,
        refreshed_issues=[_issue(id="a", state="DONE")],
        terminal_states=TERMINAL,
        active_states=ACTIVE,
    )
    assert actions.terminate_with_cleanup == ["a"]


def test_case_insensitive_active_match() -> None:
    state = _state_with(_entry(_issue(id="a", state="In Progress")))
    refreshed = _issue(id="a", state="in progress")
    actions = reconcile_running_with_tracker(
        state,
        refreshed_issues=[refreshed],
        terminal_states=TERMINAL,
        active_states=ACTIVE,
    )
    assert actions.update_snapshot == {"a": refreshed}


def test_case_insensitive_active_states_input() -> None:
    """Caller-supplied lists may carry any casing — both sides normalize."""
    state = _state_with(_entry(_issue(id="a", state="In Progress")))
    refreshed = _issue(id="a", state="In Progress")
    actions = reconcile_running_with_tracker(
        state,
        refreshed_issues=[refreshed],
        terminal_states=("done", "cancelled"),
        active_states=("TODO", "IN PROGRESS"),
    )
    assert actions.update_snapshot == {"a": refreshed}


# ---------------------------------------------------------------------------
# Mixed multi-entry scenario
# ---------------------------------------------------------------------------


def test_mixed_transitions_populate_all_buckets_in_running_order() -> None:
    state = _state_with(
        _entry(_issue(id="terminal", state="In Progress")),
        _entry(_issue(id="active", state="In Progress", title="old")),
        _entry(_issue(id="unexpected", state="In Progress")),
        _entry(_issue(id="missing", state="In Progress")),
        _entry(_issue(id="terminal2", state="Todo")),
    )
    refreshed_active = _issue(id="active", state="In Progress", title="new")
    refreshed = [
        _issue(id="terminal", state="Done"),
        refreshed_active,
        _issue(id="unexpected", state="Triage"),
        _issue(id="terminal2", state="Cancelled"),
        # 'missing' deliberately absent
        _issue(id="not-in-running", state="In Progress"),  # ignored
    ]
    actions = reconcile_running_with_tracker(
        state,
        refreshed_issues=refreshed,
        terminal_states=TERMINAL,
        active_states=ACTIVE,
    )
    assert actions.terminate_with_cleanup == ["terminal", "terminal2"]
    assert actions.terminate_without_cleanup == ["unexpected", "missing"]
    assert actions.update_snapshot == {"active": refreshed_active}
    assert actions.update_snapshot["active"].title == "new"


def test_function_does_not_mutate_state() -> None:
    """The reconcile helper is pure — it must not touch state.running."""
    original_issue = _issue(id="a", state="In Progress", title="orig")
    state = _state_with(_entry(original_issue))
    refreshed = _issue(id="a", state="In Progress", title="new")
    reconcile_running_with_tracker(
        state,
        refreshed_issues=[refreshed],
        terminal_states=TERMINAL,
        active_states=ACTIVE,
    )
    assert state.running["a"].issue is original_issue
    assert state.running["a"].issue.title == "orig"
