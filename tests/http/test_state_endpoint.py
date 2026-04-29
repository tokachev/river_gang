"""Tests for ``GET /api/v1/state`` (SPED §13.7.2)."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from river_gang.codex import RateLimitSnapshot, RuntimeEvent, TokenSnapshot
from river_gang.config.defaults import apply_defaults
from river_gang.http import create_app
from river_gang.orchestrator import (
    Mailbox,
    Orchestrator,
    OrchestratorState,
    RetryQueue,
    RunningEntry,
)
from river_gang.tracker.issue import Issue
from tests.codex.fakes import FakeCodexClient
from tests.tracker.fakes import FakeTracker
from tests.workspace.fakes import FakeWorkspaceManager


def _issue(
    *,
    id: str = "iss-1",
    identifier: str = "MT-1",
    state: str = "In Progress",
    title: str = "title",
    priority: int | None = 2,
) -> Issue:
    return Issue(
        id=id,
        identifier=identifier,
        title=title,
        state=state,
        description=None,
        priority=priority,
        branch_name=None,
        url=None,
        labels=(),
        blocked_by=(),
        created_at=None,
        updated_at=None,
    )


def _runtime_event(name: str) -> RuntimeEvent:
    return RuntimeEvent(
        event=name,
        timestamp=datetime.now(UTC),
        codex_app_server_pid=42,
        payload={},
        usage=None,
    )


def _running_entry(
    *,
    issue: Issue | None = None,
    session_id: str | None = None,
    started_at: datetime | None = None,
    last_codex_event: str | None = None,
    last_codex_timestamp: datetime | None = None,
    last_in: int = 0,
    last_out: int = 0,
    last_total: int = 0,
    events: list[RuntimeEvent] | None = None,
    last_error: str | None = None,
    restart_count: int = 0,
) -> RunningEntry:
    issue = issue or _issue()
    rec: deque[RuntimeEvent] = deque(maxlen=50)
    if events is not None:
        rec.extend(events)
    return RunningEntry(
        worker_handle=None,  # type: ignore[arg-type]
        monitor_handle=None,
        identifier=issue.identifier,
        issue=issue,
        session_id=session_id,
        last_reported_input_tokens=last_in,
        last_reported_output_tokens=last_out,
        last_reported_total_tokens=last_total,
        started_at=started_at or datetime.now(UTC),
        last_codex_timestamp=last_codex_timestamp,
        last_codex_event=last_codex_event,
        last_codex_message=None,
        recent_events=rec,
        last_error=last_error,
        restart_count=restart_count,
        retry_attempt=0,
    )


def _no_op(_id: str) -> None:
    return None


def _make_orchestrator(tmp_path: Path) -> Orchestrator:
    config = apply_defaults({
        "tracker": {"kind": "linear", "active_states": ["Todo"]},
    })
    state = OrchestratorState(
        poll_interval_ms=config.polling.interval_ms,
        max_concurrent_agents=config.agent.max_concurrent_agents,
    )
    return Orchestrator(
        state=state,
        mailbox=Mailbox(),
        retry_queue=RetryQueue(loop=asyncio.get_running_loop()),
        tracker=FakeTracker(),
        codex_client=FakeCodexClient(),
        workspace_manager=FakeWorkspaceManager(root_path=tmp_path),
        prompt_template="x",
        config=config,
        workflow_loader=lambda: config,
    )


async def _client(orch: Orchestrator) -> httpx.AsyncClient:
    app = create_app(orch)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------


async def test_empty_state_returns_zero_counts(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    async with await _client(orch) as client:
        response = await client.get("/api/v1/state")

    assert response.status_code == 200
    body = response.json()
    assert body["counts"] == {"running": 0, "retrying": 0, "completed": 0}
    assert body["running"] == []
    assert body["retrying"] == []
    assert "generated_at" in body
    assert body["codex_totals"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "seconds_running": 0.0,
    }
    assert body["rate_limits"] is None


# ---------------------------------------------------------------------------
# Populated running entry — full row JSON shape
# ---------------------------------------------------------------------------


async def test_running_entry_serialized_with_all_fields(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    issue = _issue(
        id="abc123",
        identifier="MT-649",
        state="In Progress",
        title="Implement feature",
        priority=1,
    )
    started = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)
    last_evt_ts = datetime(2026, 4, 28, 12, 0, 30, tzinfo=UTC)
    entry = _running_entry(
        issue=issue,
        session_id="thread-1-turn-1",
        started_at=started,
        last_codex_event="turn_completed",
        last_codex_timestamp=last_evt_ts,
        last_in=1200, last_out=800, last_total=2000,
        events=[_runtime_event("turn_started"), _runtime_event("agent_message")],
    )
    orch.state.add_running(entry)

    async with await _client(orch) as client:
        response = await client.get("/api/v1/state")

    assert response.status_code == 200
    body = response.json()
    assert body["counts"]["running"] == 1
    row = body["running"][0]
    assert row["issue_id"] == "abc123"
    assert row["identifier"] == "MT-649"
    assert row["title"] == "Implement feature"
    assert row["state"] == "In Progress"
    assert row["priority"] == 1
    assert row["session_id"] == "thread-1-turn-1"
    assert row["last_codex_event"] == "turn_completed"
    assert row["turn_count"] == 1
    assert row["last_reported_input_tokens"] == 1200
    assert row["last_reported_output_tokens"] == 800
    assert row["last_reported_total_tokens"] == 2000
    assert row["last_error"] is None
    assert row["restart_count"] == 0
    # Datetime fields ISO-formatted.
    assert row["started_at"].startswith("2026-04-28T12:00:00")
    assert row["last_codex_timestamp"].startswith("2026-04-28T12:00:30")


async def test_turn_count_reflects_recent_events(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    entry = _running_entry(
        events=[
            _runtime_event("turn_started"),
            _runtime_event("agent_message"),
            _runtime_event("turn.start"),
            _runtime_event("turn_completed"),
            _runtime_event("turn_started"),
        ]
    )
    orch.state.add_running(entry)

    async with await _client(orch) as client:
        response = await client.get("/api/v1/state")
    assert response.json()["running"][0]["turn_count"] == 3


# ---------------------------------------------------------------------------
# Retry rows
# ---------------------------------------------------------------------------


async def test_retry_row_serialized(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    orch.retry_queue.schedule(
        issue_id="def456",
        attempt=3,
        kind="failure",
        max_cap_ms=300_000,
        on_fire=_no_op,
        last_error="no available orchestrator slots",
    )

    async with await _client(orch) as client:
        response = await client.get("/api/v1/state")

    body = response.json()
    assert body["counts"]["retrying"] == 1
    row = body["retrying"][0]
    assert row["issue_id"] == "def456"
    assert row["identifier"] is None
    assert row["attempt"] == 3
    assert row["kind"] == "failure"
    assert row["last_error"] == "no available orchestrator slots"
    assert "fire_at" in row
    orch.retry_queue.cancel("def456")


async def test_retry_identifier_pulled_from_running(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    orch.state.add_running(
        _running_entry(issue=_issue(id="known", identifier="MT-Known"))
    )
    orch.retry_queue.schedule(
        issue_id="known", attempt=1, kind="continuation",
        max_cap_ms=300_000, on_fire=_no_op,
    )

    async with await _client(orch) as client:
        response = await client.get("/api/v1/state")
    assert response.json()["retrying"][0]["identifier"] == "MT-Known"
    orch.retry_queue.cancel("known")


# ---------------------------------------------------------------------------
# codex_totals + rate_limits
# ---------------------------------------------------------------------------


async def test_codex_totals_reflect_state_and_active_runtime(
    tmp_path: Path,
) -> None:
    orch = _make_orchestrator(tmp_path)
    orch.state.codex_totals = TokenSnapshot(5000, 2400, 7400)
    orch.state.add_runtime_seconds(100.0)
    orch.state.add_running(
        _running_entry(started_at=datetime.now(UTC) - timedelta(seconds=20))
    )

    async with await _client(orch) as client:
        response = await client.get("/api/v1/state")

    body = response.json()
    totals = body["codex_totals"]
    assert totals["input_tokens"] == 5000
    assert totals["output_tokens"] == 2400
    assert totals["total_tokens"] == 7400
    # 100 ended + ~20 active (allow slack for jitter).
    assert totals["seconds_running"] >= 119.0
    assert totals["seconds_running"] < 130.0


async def test_rate_limits_serialized_when_set(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    orch.state.codex_rate_limits = RateLimitSnapshot(
        limit=1000, remaining=250, reset_at="2026-04-28T13:00:00Z"
    )
    async with await _client(orch) as client:
        response = await client.get("/api/v1/state")

    rl = response.json()["rate_limits"]
    assert rl == {
        "limit": 1000,
        "remaining": 250,
        "reset_at": "2026-04-28T13:00:00Z",
    }


# ---------------------------------------------------------------------------
# Method + content-type negative cases
# ---------------------------------------------------------------------------


async def test_post_returns_405(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    async with await _client(orch) as client:
        response = await client.post("/api/v1/state", json={})
    assert response.status_code == 405


async def test_response_content_type_is_json(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    async with await _client(orch) as client:
        response = await client.get("/api/v1/state")
    assert "application/json" in response.headers["content-type"]


# ---------------------------------------------------------------------------
# Shape conformance — every §13.7.2 top-level key present
# ---------------------------------------------------------------------------


async def test_response_shape_matches_spec_example(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    orch.state.add_running(_running_entry())
    orch.retry_queue.schedule(
        issue_id="rt", attempt=2, kind="failure",
        max_cap_ms=300_000, on_fire=_no_op, last_error="x",
    )

    async with await _client(orch) as client:
        response = await client.get("/api/v1/state")
    body = response.json()

    for key in (
        "generated_at", "counts", "running", "retrying",
        "codex_totals", "rate_limits",
    ):
        assert key in body, body
    for key in ("running", "retrying", "completed"):
        assert key in body["counts"]
    for key in ("input_tokens", "output_tokens", "total_tokens", "seconds_running"):
        assert key in body["codex_totals"]
    orch.retry_queue.cancel("rt")
