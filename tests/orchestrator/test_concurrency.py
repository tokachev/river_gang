"""Tests for concurrency-slot helpers in :mod:`river_gang.orchestrator.dispatch`
(SPED §8.3)."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime

from river_gang.orchestrator import OrchestratorState, RunningEntry
from river_gang.orchestrator.dispatch import (
    available_slots_for_dispatch,
    concurrency_check,
)
from river_gang.tracker.issue import Issue


def _issue(*, id: str = "id-1", identifier: str = "MT-1", state: str = "Todo") -> Issue:
    return Issue(
        id=id,
        identifier=identifier,
        title="t",
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


# ---------------------------------------------------------------------------
# available_slots_for_dispatch
# ---------------------------------------------------------------------------


def test_global_limit_exhausted_returns_zero() -> None:
    slots = available_slots_for_dispatch(
        "Todo",
        max_global=3,
        current_running_total=3,
        per_state_map={"todo": 5},
        current_per_state=0,
    )
    assert slots == 0


def test_global_limit_exhausted_negative_clamped_to_zero() -> None:
    """If running somehow exceeds max (e.g. config shrank), we clamp to 0."""
    slots = available_slots_for_dispatch(
        "Todo",
        max_global=3,
        current_running_total=5,
        per_state_map={},
        current_per_state=0,
    )
    assert slots == 0


def test_global_has_room_per_state_explicit_limit_applies() -> None:
    slots = available_slots_for_dispatch(
        "Todo",
        max_global=10,
        current_running_total=2,
        per_state_map={"todo": 2},
        current_per_state=2,
    )
    # global has 8 free, but per-state cap (2) is fully consumed.
    assert slots == 0


def test_global_has_room_per_state_partial() -> None:
    slots = available_slots_for_dispatch(
        "Todo",
        max_global=10,
        current_running_total=2,
        per_state_map={"todo": 5},
        current_per_state=2,
    )
    # global free = 8, per-state free = 3 → min = 3.
    assert slots == 3


def test_per_state_not_in_map_falls_back_to_global() -> None:
    slots = available_slots_for_dispatch(
        "Backlog",
        max_global=4,
        current_running_total=1,
        per_state_map={"todo": 2},
        current_per_state=0,
    )
    # No "backlog" entry → fallback to global cap (4 - 0 = 4); global free = 3.
    assert slots == 3


def test_empty_per_state_map_falls_back_to_global() -> None:
    slots = available_slots_for_dispatch(
        "Todo",
        max_global=5,
        current_running_total=2,
        per_state_map={},
        current_per_state=2,
    )
    # No per-state entries — global free = 3, state free = 5 - 2 = 3 → 3.
    assert slots == 3


def test_per_state_lookup_is_case_insensitive() -> None:
    slots = available_slots_for_dispatch(
        "TODO",
        max_global=10,
        current_running_total=0,
        per_state_map={"todo": 2},
        current_per_state=0,
    )
    assert slots == 2


def test_per_state_lookup_handles_mixed_case_keys() -> None:
    slots = available_slots_for_dispatch(
        "in progress",
        max_global=10,
        current_running_total=0,
        per_state_map={"In Progress": 1},
        current_per_state=0,
    )
    assert slots == 1


def test_per_state_zero_blocks_dispatch() -> None:
    slots = available_slots_for_dispatch(
        "Todo",
        max_global=10,
        current_running_total=0,
        per_state_map={"todo": 0},
        current_per_state=0,
    )
    assert slots == 0


def test_per_state_negative_clamped_to_zero() -> None:
    slots = available_slots_for_dispatch(
        "Todo",
        max_global=10,
        current_running_total=0,
        per_state_map={"todo": 1},
        current_per_state=5,
    )
    assert slots == 0


# ---------------------------------------------------------------------------
# concurrency_check
# ---------------------------------------------------------------------------


def test_concurrency_check_false_when_no_slots() -> None:
    state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=2)
    issue = _issue(id="a", state="Todo")
    state.add_running(_entry(_issue(id="r1", identifier="MT-R1", state="Todo")))
    state.add_running(_entry(_issue(id="r2", identifier="MT-R2", state="Todo")))
    assert concurrency_check(issue, state, per_state_map={}) is False


def test_concurrency_check_true_when_slots_available() -> None:
    state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=3)
    issue = _issue(id="a", state="Todo")
    state.add_running(_entry(_issue(id="r1", identifier="MT-R1", state="Todo")))
    assert concurrency_check(issue, state, per_state_map={}) is True


def test_concurrency_check_per_state_blocks_even_with_global_room() -> None:
    state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=10)
    state.add_running(_entry(_issue(id="r1", identifier="MT-R1", state="In Progress")))
    issue = _issue(id="a", state="In Progress")
    assert concurrency_check(
        issue, state, per_state_map={"in progress": 1}
    ) is False


# ---------------------------------------------------------------------------
# Realistic mixed scenario (per task spec)
# ---------------------------------------------------------------------------


def test_realistic_mixed_scenario() -> None:
    """max=3, running=2 (1 Todo, 1 In Progress), per_state={"todo":2,
    "in progress":1}.

    Expected:
    - Todo: global free=1, state free=1 → 1 slot
    - In Progress: global free=1, state free=0 → 0 slots
    - Backlog (not in map): global free=1, fallback state cap=3 - 0 = 3 → 1 slot
    """
    state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=3)
    state.add_running(_entry(_issue(id="r1", identifier="MT-R1", state="Todo")))
    state.add_running(
        _entry(_issue(id="r2", identifier="MT-R2", state="In Progress"))
    )
    per_state_map = {"todo": 2, "in progress": 1}

    todo_slots = available_slots_for_dispatch(
        "Todo",
        max_global=state.max_concurrent_agents,
        current_running_total=len(state.running),
        per_state_map=per_state_map,
        current_per_state=state.count_in_state("Todo"),
    )
    inprog_slots = available_slots_for_dispatch(
        "In Progress",
        max_global=state.max_concurrent_agents,
        current_running_total=len(state.running),
        per_state_map=per_state_map,
        current_per_state=state.count_in_state("In Progress"),
    )
    backlog_slots = available_slots_for_dispatch(
        "Backlog",
        max_global=state.max_concurrent_agents,
        current_running_total=len(state.running),
        per_state_map=per_state_map,
        current_per_state=state.count_in_state("Backlog"),
    )
    assert todo_slots == 1
    assert inprog_slots == 0
    assert backlog_slots == 1

    # concurrency_check matches the slot result
    assert concurrency_check(
        _issue(id="t", state="Todo"), state, per_state_map=per_state_map
    ) is True
    assert concurrency_check(
        _issue(id="ip", state="In Progress"), state, per_state_map=per_state_map
    ) is False
    assert concurrency_check(
        _issue(id="bl", state="Backlog"), state, per_state_map=per_state_map
    ) is True
