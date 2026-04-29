"""Tests for ``GET /api/v1/{identifier}`` (SPED §13.7.2)."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import httpx

from river_gang.codex import RuntimeEvent
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


def _runtime_event(
    name: str, *, ts: datetime | None = None
) -> RuntimeEvent:
    return RuntimeEvent(
        event=name,
        timestamp=ts or datetime.now(UTC),
        codex_app_server_pid=42,
        payload={"k": "v"},
        usage=None,
    )


def _running_entry(
    *,
    issue: Issue | None = None,
    session_id: str | None = None,
    started_at: datetime | None = None,
    last_codex_event: str | None = None,
    last_codex_timestamp: datetime | None = None,
    last_codex_message: str | None = None,
    last_error: str | None = None,
    restart_count: int = 0,
    last_in: int = 0,
    last_out: int = 0,
    last_total: int = 0,
    events: list[RuntimeEvent] | None = None,
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
        last_codex_message=last_codex_message,
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
# Running issue
# ---------------------------------------------------------------------------


async def test_running_issue_returns_running_block(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    started = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)
    last_evt_ts = datetime(2026, 4, 28, 12, 0, 30, tzinfo=UTC)
    issue = _issue(
        id="abc123", identifier="MT-649", state="In Progress",
        title="Implement feature", priority=1,
    )
    entry = _running_entry(
        issue=issue,
        session_id="thread-1-turn-1",
        started_at=started,
        last_codex_event="turn_completed",
        last_codex_timestamp=last_evt_ts,
        last_codex_message="ok",
        last_in=1200, last_out=800, last_total=2000,
        restart_count=2,
        events=[
            _runtime_event("turn_started"),
            _runtime_event("turn.start"),
            _runtime_event("agent_message"),
        ],
    )
    orch.state.add_running(entry)

    async with await _client(orch) as client:
        response = await client.get("/api/v1/MT-649")

    assert response.status_code == 200
    body = response.json()
    assert body["issue_id"] == "abc123"
    assert body["identifier"] == "MT-649"
    assert body["title"] == "Implement feature"
    assert body["state"] == "In Progress"
    assert body["status"] == "running"

    running = body["running"]
    assert running is not None
    assert running["session_id"] == "thread-1-turn-1"
    assert running["turn_count"] == 2  # turn_started + turn.start
    assert running["state"] == "In Progress"
    assert running["started_at"].startswith("2026-04-28T12:00:00")
    assert running["last_codex_event"] == "turn_completed"
    assert running["last_codex_timestamp"].startswith("2026-04-28T12:00:30")
    assert running["last_codex_message"] == "ok"
    assert running["tokens"] == {
        "input_tokens": 1200, "output_tokens": 800, "total_tokens": 2000,
    }

    assert body["retry"] is None
    assert body["attempts"]["restart_count"] == 2
    assert body["attempts"]["current_retry_attempt"] is None
    assert body["last_error"] is None
    assert body["tracked"] == {}
    assert body["logs"]["codex_session_logs"] == []
    # recent_events serialized.
    assert len(body["recent_events"]) == 3
    assert body["recent_events"][0]["event"] == "turn_started"
    assert body["recent_events"][0]["payload"] == {"k": "v"}


async def test_running_issue_last_error_propagated(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    entry = _running_entry(last_error="something exploded")
    orch.state.add_running(entry)

    async with await _client(orch) as client:
        response = await client.get("/api/v1/MT-1")

    assert response.status_code == 200
    assert response.json()["last_error"] == "something exploded"


async def test_recent_events_capped_to_50_via_deque(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    events = [_runtime_event(f"evt-{i}") for i in range(60)]
    entry = _running_entry(events=events)
    orch.state.add_running(entry)

    async with await _client(orch) as client:
        response = await client.get("/api/v1/MT-1")

    body = response.json()
    assert len(body["recent_events"]) == 50
    # Oldest dropped: first kept = evt-10.
    assert body["recent_events"][0]["event"] == "evt-10"
    assert body["recent_events"][-1]["event"] == "evt-59"


# ---------------------------------------------------------------------------
# Retrying issue (no running entry)
# ---------------------------------------------------------------------------


async def test_retrying_issue_returns_retry_block(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    # Establish identifier mapping by running once, then removing.
    orch.state.add_running(
        _running_entry(issue=_issue(id="r1", identifier="MT-Retry"))
    )
    orch.state.remove_running("r1")
    orch.retry_queue.schedule(
        issue_id="r1", attempt=3, kind="failure",
        max_cap_ms=300_000, on_fire=_no_op, last_error="transient err",
    )

    async with await _client(orch) as client:
        response = await client.get("/api/v1/MT-Retry")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "retrying"
    assert body["issue_id"] == "r1"
    assert body["identifier"] == "MT-Retry"
    assert body["running"] is None

    retry = body["retry"]
    assert retry is not None
    assert retry["attempt"] == 3
    assert retry["kind"] == "failure"
    assert retry["last_error"] == "transient err"
    assert "fire_at" in retry
    assert body["attempts"]["current_retry_attempt"] == 3

    orch.retry_queue.cancel("r1")


# ---------------------------------------------------------------------------
# Completed issue (in identifier_index + completed, not running, not retry)
# ---------------------------------------------------------------------------


async def test_completed_issue_status(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    orch.state.add_running(
        _running_entry(issue=_issue(id="d1", identifier="MT-Done"))
    )
    orch.state.remove_running("d1")
    orch.state.record_completed("d1")

    async with await _client(orch) as client:
        response = await client.get("/api/v1/MT-Done")

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "completed"
    assert body["issue_id"] == "d1"
    assert body["identifier"] == "MT-Done"
    assert body["running"] is None
    assert body["retry"] is None


# ---------------------------------------------------------------------------
# 404 cases — never seen + JSON envelope
# ---------------------------------------------------------------------------


async def test_unknown_identifier_returns_404_envelope(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    async with await _client(orch) as client:
        response = await client.get("/api/v1/MT-999")

    assert response.status_code == 404
    body = response.json()
    assert body == {
        "error": {
            "code": "issue_not_found",
            "message": "identifier=MT-999 not tracked",
        }
    }


# ---------------------------------------------------------------------------
# Case-sensitivity policy: identifiers are case-sensitive.
# ---------------------------------------------------------------------------


async def test_identifier_lookup_is_case_sensitive(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    orch.state.add_running(
        _running_entry(issue=_issue(id="x", identifier="MT-1"))
    )

    async with await _client(orch) as client:
        # Wrong case → 404.
        response = await client.get("/api/v1/mt-1")
        # Exact case → 200.
        response_ok = await client.get("/api/v1/MT-1")

    assert response.status_code == 404
    assert response_ok.status_code == 200


# ---------------------------------------------------------------------------
# Method negative case
# ---------------------------------------------------------------------------


async def test_post_returns_405(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    orch.state.add_running(
        _running_entry(issue=_issue(id="x", identifier="MT-1"))
    )
    async with await _client(orch) as client:
        response = await client.post("/api/v1/MT-1", json={})
    assert response.status_code == 405


# ---------------------------------------------------------------------------
# tracked + logs deferred fields
# ---------------------------------------------------------------------------


async def test_tracked_is_empty_dict_and_logs_empty_list(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    orch.state.add_running(_running_entry())
    async with await _client(orch) as client:
        response = await client.get("/api/v1/MT-1")
    body = response.json()
    assert body["tracked"] == {}
    assert body["logs"] == {"codex_session_logs": []}


# ---------------------------------------------------------------------------
# Running takes precedence over retry when both present.
# ---------------------------------------------------------------------------


async def test_running_precedence_over_retry(tmp_path: Path) -> None:
    """Active worker + scheduled continuation retry → status='running'."""
    orch = _make_orchestrator(tmp_path)
    orch.state.add_running(
        _running_entry(issue=_issue(id="dual", identifier="MT-Dual"))
    )
    orch.retry_queue.schedule(
        issue_id="dual", attempt=2, kind="continuation",
        max_cap_ms=300_000, on_fire=_no_op,
    )

    async with await _client(orch) as client:
        response = await client.get("/api/v1/MT-Dual")

    body = response.json()
    assert body["status"] == "running"
    assert body["running"] is not None
    assert body["retry"] is None
    orch.retry_queue.cancel("dual")


# ---------------------------------------------------------------------------
# Recent events serialization shape
# ---------------------------------------------------------------------------


async def test_recent_events_field_shape(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    ts = datetime(2026, 4, 28, 12, 0, 30, tzinfo=UTC)
    events = [_runtime_event("agent_message", ts=ts)]
    orch.state.add_running(_running_entry(events=events))

    async with await _client(orch) as client:
        response = await client.get("/api/v1/MT-1")

    evt = response.json()["recent_events"][0]
    for key in ("event", "timestamp", "codex_app_server_pid", "payload"):
        assert key in evt, evt
    assert evt["event"] == "agent_message"
    assert evt["timestamp"].startswith("2026-04-28T12:00:30")
    assert evt["codex_app_server_pid"] == 42
    assert evt["payload"] == {"k": "v"}
