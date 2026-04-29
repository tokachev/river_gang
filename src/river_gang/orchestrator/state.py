"""Orchestrator runtime state (SPED §4.1.6, §4.1.8, §16.4).

Single authoritative in-memory state owned by the orchestrator's mailbox
dispatcher (single-writer pattern — workers never mutate this directly).
Mutations go through explicit methods on :class:`OrchestratorState`.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from river_gang.codex import RateLimitSnapshot, RuntimeEvent, TokenSnapshot
from river_gang.tracker.issue import Issue

_ZERO_TOKENS = TokenSnapshot(input_tokens=0, output_tokens=0, total_tokens=0)


@dataclass
class RunningEntry:
    """Per-issue live session metadata (§4.1.6 + §16.4 dispatch_issue init).

    ``monitor_handle`` is a placeholder per Implementation-Defined Choice #2:
    the spec mentions a separate monitor task, but this iteration folds the
    monitor into the worker task itself, so this field is always ``None``.
    """

    worker_handle: asyncio.Task[None]
    monitor_handle: None
    identifier: str
    issue: Issue
    session_id: str | None
    last_reported_input_tokens: int
    last_reported_output_tokens: int
    last_reported_total_tokens: int
    started_at: datetime
    last_codex_timestamp: datetime | None
    last_codex_event: str | None
    last_codex_message: str | None
    recent_events: deque[RuntimeEvent]
    last_error: str | None
    restart_count: int
    retry_attempt: int


@dataclass
class OrchestratorState:
    """Authoritative orchestrator state (§4.1.8)."""

    poll_interval_ms: int
    max_concurrent_agents: int
    running: dict[str, RunningEntry] = field(default_factory=dict)
    claimed: set[str] = field(default_factory=set)
    retry_attempts: dict[str, int] = field(default_factory=dict)
    completed: set[str] = field(default_factory=set)
    codex_totals: TokenSnapshot = _ZERO_TOKENS
    codex_rate_limits: RateLimitSnapshot | None = None
    runtime_seconds_total: float = 0.0
    # ``issue_id → identifier`` survives ``remove_running`` so the HTTP
    # detail endpoint (Task 43) can resolve ``GET /api/v1/<identifier>``
    # while the issue is in retry / completed (RetryEntry only carries
    # ``issue_id`` and ``completed`` is just a set of ids).
    identifier_index: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Claim set
    # ------------------------------------------------------------------

    def mark_claimed(self, issue_id: str) -> None:
        self.claimed.add(issue_id)

    def unclaim(self, issue_id: str) -> None:
        self.claimed.discard(issue_id)

    def is_claimed(self, issue_id: str) -> bool:
        return issue_id in self.claimed

    # ------------------------------------------------------------------
    # Running map
    # ------------------------------------------------------------------

    def add_running(self, entry: RunningEntry) -> None:
        self.running[entry.issue.id] = entry
        self.identifier_index[entry.issue.id] = entry.identifier

    def remove_running(self, issue_id: str) -> RunningEntry | None:
        return self.running.pop(issue_id, None)

    # ------------------------------------------------------------------
    # Completion bookkeeping
    # ------------------------------------------------------------------

    def record_completed(self, issue_id: str) -> None:
        self.completed.add(issue_id)

    # ------------------------------------------------------------------
    # Capacity helpers
    # ------------------------------------------------------------------

    def available_slots(self) -> int:
        return self.max_concurrent_agents - len(self.running)

    def count_in_state(self, state_name: str) -> int:
        target = state_name.lower()
        return sum(
            1
            for entry in self.running.values()
            if entry.issue.state.lower() == target
        )

    # ------------------------------------------------------------------
    # Runtime accounting
    # ------------------------------------------------------------------

    def add_runtime_seconds(self, seconds: float) -> None:
        self.runtime_seconds_total += seconds


__all__: list[str] = [
    "OrchestratorState",
    "RunningEntry",
]
