"""SPED §17.6 conformance: Observability."""

from __future__ import annotations

import logging
from io import StringIO

import pytest

from river_gang.codex import TokenSnapshot
from river_gang.config.defaults import apply_defaults
from river_gang.config.validation import (
    format_error_for_operator,
    validate_for_dispatch,
)
from river_gang.observability.logging import (
    configure_logging,
    issue_id_var,
    set_log_context,
)
from river_gang.observability.snapshot import build_snapshot
from river_gang.orchestrator import OrchestratorState, RetryQueue

pytestmark = pytest.mark.conformance


@pytest.fixture(autouse=True)
def _restore_root_logger():
    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level
    yield
    for h in list(root.handlers):
        if h not in saved:
            root.removeHandler(h)
    root.setLevel(saved_level)


# ---------------------------------------------------------------------------
# Validation failures operator-visible
# ---------------------------------------------------------------------------


def test_validation_failures_operator_visible() -> None:
    """Conformance §17.6: validation failures are operator-visible."""
    bad = apply_defaults({})  # missing tracker.kind + api_key
    result = validate_for_dispatch(bad)
    assert not result.ok
    formatted = format_error_for_operator(result)
    # Multi-line operator summary mentions the failing fields.
    assert "tracker.kind" in formatted
    assert "tracker.api_key" in formatted


# ---------------------------------------------------------------------------
# Structured logging carries issue/session context
# ---------------------------------------------------------------------------


def test_structured_logging_carries_issue_session_context() -> None:
    """Conformance §17.6: structured logging includes issue/session context
    fields."""
    buffer = StringIO()
    configure_logging(level=logging.INFO, stream=buffer)
    log = logging.getLogger("conformance.test")
    with set_log_context(
        issue_id="iid-1",
        issue_identifier="MT-1",
        session_id="th-1-tn-1",
    ):
        log.info("worker step")
    output = buffer.getvalue()
    assert "issue_id=iid-1" in output
    assert "issue_identifier=MT-1" in output
    assert "session_id=th-1-tn-1" in output


# ---------------------------------------------------------------------------
# Logging sink failures don't crash orchestration
# ---------------------------------------------------------------------------


def test_logging_sink_failure_does_not_crash_orchestration() -> None:
    """Conformance §17.6: logging sink failures do not crash
    orchestration."""
    # configure_logging installs a single handler. If the underlying
    # write raises, Python's logging module swallows it via
    # ``logging.raiseExceptions = False`` semantics by default in
    # production (we don't flip it). Here we install a broken sink and
    # confirm log calls don't propagate.
    class _BrokenStream:
        def write(self, _data: str) -> int:
            raise OSError("disk full")

        def flush(self) -> None:
            pass

    configure_logging(level=logging.INFO, stream=_BrokenStream())  # type: ignore[arg-type]
    log = logging.getLogger("conformance.broken-sink")
    # Must not raise.
    log.info("anything")
    log.error("ooh")


# ---------------------------------------------------------------------------
# Token + rate-limit aggregation correct across repeated updates
# ---------------------------------------------------------------------------


async def test_token_rate_limit_aggregation_correct_across_updates() -> None:
    """Conformance §17.6: token/rate-limit aggregation remains correct
    across repeated agent updates."""
    import asyncio
    from collections import deque
    from datetime import UTC, datetime

    from river_gang.codex import RateLimitSnapshot, RuntimeEvent
    from river_gang.orchestrator import CodexUpdate, RunningEntry
    from river_gang.orchestrator.lifecycle import on_codex_update
    from river_gang.tracker.issue import Issue

    state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=2)
    issue = Issue(
        id="i", identifier="MT-1", title="t", state="In Progress",
        description=None, priority=None, branch_name=None, url=None,
        labels=(), blocked_by=(), created_at=None, updated_at=None,
    )
    entry = RunningEntry(
        worker_handle=None,  # type: ignore[arg-type]
        monitor_handle=None,
        identifier="MT-1", issue=issue, session_id=None,
        last_reported_input_tokens=0, last_reported_output_tokens=0,
        last_reported_total_tokens=0,
        started_at=datetime.now(UTC),
        last_codex_timestamp=None, last_codex_event=None, last_codex_message=None,
        recent_events=deque(maxlen=50),
        last_error=None, restart_count=0, retry_attempt=0,
    )
    state.add_running(entry)

    def _evt(usage: dict[str, int] | None = None,
             rl: dict | None = None) -> RuntimeEvent:
        payload: dict = {}
        if rl is not None:
            payload["rate_limit"] = rl
        return RuntimeEvent(
            event="agent_message",
            timestamp=datetime.now(UTC),
            codex_app_server_pid=42,
            payload=payload,
            usage=usage,
        )

    # Two cumulative updates: 100→130. Globals reflect 130 absolute.
    on_codex_update(state, message=CodexUpdate(
        issue_id="i", event=_evt(usage={
            "input_tokens": 50, "output_tokens": 50, "total_tokens": 100,
        })
    ))
    on_codex_update(state, message=CodexUpdate(
        issue_id="i", event=_evt(usage={
            "input_tokens": 70, "output_tokens": 60, "total_tokens": 130,
        })
    ))
    assert state.codex_totals == TokenSnapshot(70, 60, 130)

    # Rate limits: most recent payload wins; absent payload doesn't null prior.
    on_codex_update(state, message=CodexUpdate(
        issue_id="i", event=_evt(rl={
            "limit": 1000, "remaining": 200, "reset_at": "2026-01-01T00:00:00Z",
        })
    ))
    assert state.codex_rate_limits == RateLimitSnapshot(
        limit=1000, remaining=200, reset_at="2026-01-01T00:00:00Z",
    )
    on_codex_update(state, message=CodexUpdate(
        issue_id="i", event=_evt()  # no rate_limit on this event
    ))
    # Rate limits preserved.
    assert state.codex_rate_limits is not None
    _ = asyncio  # silence unused


# ---------------------------------------------------------------------------
# Optional human-readable status surface
# ---------------------------------------------------------------------------


async def test_status_surface_driven_from_state_no_correctness_impact(
    tmp_path,  # noqa: ANN001
) -> None:
    """Conformance §17.6: if a human-readable status surface is
    implemented, it is driven from orchestrator state and does not
    affect correctness."""
    import asyncio as _aio
    from datetime import datetime as _dt

    rq = RetryQueue(loop=_aio.get_running_loop())
    state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=2)
    snap = build_snapshot(state, retry_queue=rq, now=_dt.now())
    # Purely projective: no IO, no mutation observable on the inputs.
    assert state.running == {}
    assert snap.counts == {"running": 0, "retrying": 0, "completed": 0}


def test_humanized_summaries_dont_change_orchestrator_behavior() -> None:
    """Conformance §17.6: if humanized event summaries are implemented,
    they cover key wrapper/agent event classes without changing
    orchestrator behavior.

    Humanized strings are NOT implemented in this iteration — orchestrator
    decisions are made on the raw RuntimeEvent.event names (turn_completed,
    turn_failed, turn_input_required, etc.). This test documents the
    deferral.
    """
    # Marker: explicitly NOT changing orchestrator behavior because
    # there are no humanizers. The on_codex_update handler reads only
    # raw event/payload fields.
    _ = issue_id_var  # silence unused
