"""Reconciliation helpers (SPED §8.5).

Part A — stall detection: :func:`detect_stalls` scans the running map and
flags issue ids whose workers have been silent past ``stall_timeout_ms``.
Pure function over :class:`OrchestratorState`; no IO, no mutation. The
orchestrator's mailbox dispatcher decides what to do with the returned
ids (terminate worker, schedule retry, etc.).

Part B — tracker reconciliation: :func:`reconcile_running_with_tracker`
classifies each running entry against a freshly-fetched issue snapshot
into one of three buckets — ``terminate_with_cleanup`` (issue went
terminal), ``terminate_without_cleanup`` (issue moved to a state that's
neither active nor terminal, or vanished from the tracker — preserve the
workspace as evidence), or ``update_snapshot`` (still active — refresh
the running entry's :class:`Issue` snapshot). Refresh-failure handling
lives at the call site; this function operates on the data it gets.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from river_gang.codex.stall import should_terminate_for_stall
from river_gang.orchestrator.state import OrchestratorState
from river_gang.tracker.issue import Issue


def detect_stalls(
    state: OrchestratorState,
    *,
    now: datetime,
    stall_timeout_ms: int,
) -> list[str]:
    """Return ids of running entries that have stalled.

    ``stall_timeout_ms <= 0`` disables detection (returns ``[]``). Order
    follows ``state.running`` insertion order.
    """
    if stall_timeout_ms <= 0:
        return []

    out: list[str] = []
    for issue_id, entry in state.running.items():
        if should_terminate_for_stall(
            now=now,
            last_event_at=entry.last_codex_timestamp,
            started_at=entry.started_at,
            stall_timeout_ms=stall_timeout_ms,
        ):
            out.append(issue_id)
    return out


@dataclass(frozen=True)
class ReconcileActions:
    """Outcome of :func:`reconcile_running_with_tracker` (§8.5 Part B).

    ``terminate_with_cleanup`` — issue went terminal: stop session AND
    cleanup workspace. ``terminate_without_cleanup`` — issue moved out of
    both the active and terminal lists, or disappeared from the tracker:
    stop session and preserve the workspace for inspection.
    ``update_snapshot`` — issue is still active; replace the running
    entry's stale :class:`Issue` with the refreshed value so subsequent
    eligibility checks see current title/state/blockers.
    """

    terminate_with_cleanup: list[str] = field(default_factory=list)
    terminate_without_cleanup: list[str] = field(default_factory=list)
    update_snapshot: dict[str, Issue] = field(default_factory=dict)


def reconcile_running_with_tracker(
    state: OrchestratorState,
    *,
    refreshed_issues: list[Issue],
    terminal_states: Iterable[str],
    active_states: Iterable[str],
) -> ReconcileActions:
    """Classify each running entry against ``refreshed_issues`` (§8.5 B).

    Iterates ``state.running`` in insertion order so caller-visible action
    lists are deterministic. The function is pure — it does not touch
    ``state.running``.
    """
    refreshed_by_id = {iss.id: iss for iss in refreshed_issues}
    terminal_norm = {s.lower() for s in terminal_states}
    active_norm = {s.lower() for s in active_states}

    actions = ReconcileActions()
    for issue_id in state.running:
        refreshed = refreshed_by_id.get(issue_id)
        if refreshed is None:
            actions.terminate_without_cleanup.append(issue_id)
            continue

        new_state = refreshed.state.lower()
        if new_state in terminal_norm:
            actions.terminate_with_cleanup.append(issue_id)
        elif new_state in active_norm:
            actions.update_snapshot[issue_id] = refreshed
        else:
            actions.terminate_without_cleanup.append(issue_id)

    return actions


__all__ = [
    "ReconcileActions",
    "detect_stalls",
    "reconcile_running_with_tracker",
]
