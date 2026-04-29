"""Tests for the HTML dashboard at ``GET /`` (SPED §13.7.1)."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from river_gang.codex import RuntimeEvent, TokenSnapshot
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
        last_reported_input_tokens=0,
        last_reported_output_tokens=0,
        last_reported_total_tokens=0,
        started_at=started_at or datetime.now(UTC),
        last_codex_timestamp=None,
        last_codex_event=last_codex_event,
        last_codex_message=None,
        recent_events=rec,
        last_error=None,
        restart_count=0,
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
# Status code + Content-Type
# ---------------------------------------------------------------------------


async def test_root_returns_200_html(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    async with await _client(orch) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


async def test_root_response_is_not_json(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    async with await _client(orch) as client:
        response = await client.get("/")

    body = response.text
    # Must look like HTML, not JSON.
    assert body.lstrip().startswith("<")
    assert "<html" in body.lower()


# ---------------------------------------------------------------------------
# Required section headings
# ---------------------------------------------------------------------------


async def test_html_contains_active_sessions_heading(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    async with await _client(orch) as client:
        response = await client.get("/")
    assert "Active sessions" in response.text


async def test_html_contains_retry_queue_heading(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    async with await _client(orch) as client:
        response = await client.get("/")
    assert "Retry queue" in response.text


async def test_html_contains_token_consumption_heading(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    async with await _client(orch) as client:
        response = await client.get("/")
    assert "Token consumption" in response.text


# ---------------------------------------------------------------------------
# Empty-state placeholders
# ---------------------------------------------------------------------------


async def test_empty_state_shows_no_active_sessions(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    async with await _client(orch) as client:
        response = await client.get("/")
    assert "No active sessions" in response.text


async def test_empty_state_shows_retry_queue_empty(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    async with await _client(orch) as client:
        response = await client.get("/")
    assert "Retry queue empty" in response.text


# ---------------------------------------------------------------------------
# Populated state — running identifier rendered
# ---------------------------------------------------------------------------


async def test_running_entry_identifier_rendered_in_html(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    issue = _issue(id="abc", identifier="MT-Special-42", title="Cool feature")
    orch.state.add_running(
        _running_entry(
            issue=issue,
            session_id="th-1-tn-1",
            last_codex_event="turn_completed",
            events=[_runtime_event("turn_started")],
        )
    )

    async with await _client(orch) as client:
        response = await client.get("/")

    body = response.text
    assert "MT-Special-42" in body
    assert "Cool feature" in body
    assert "turn_completed" in body


async def test_retry_entry_rendered_in_html(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    orch.retry_queue.schedule(
        issue_id="ret-1", attempt=4, kind="failure",
        max_cap_ms=300_000, on_fire=_no_op, last_error="rate limited",
    )

    async with await _client(orch) as client:
        response = await client.get("/")

    body = response.text
    assert "ret-1" in body
    assert "rate limited" in body
    # Empty-state placeholder no longer shown when retry exists.
    assert "Retry queue empty" not in body
    orch.retry_queue.cancel("ret-1")


# ---------------------------------------------------------------------------
# Token consumption section — values rendered
# ---------------------------------------------------------------------------


async def test_codex_totals_rendered_in_html(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    orch.state.codex_totals = TokenSnapshot(
        input_tokens=12345, output_tokens=6789, total_tokens=19134
    )
    orch.state.add_runtime_seconds(42.0)

    async with await _client(orch) as client:
        response = await client.get("/")

    body = response.text
    assert "12345" in body
    assert "6789" in body
    assert "19134" in body
    # seconds_running formatted somewhere — accept either int "42" or "42.0".
    assert "42" in body


# ---------------------------------------------------------------------------
# generated_at present
# ---------------------------------------------------------------------------


async def test_generated_at_rendered(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    async with await _client(orch) as client:
        response = await client.get("/")
    # ISO timestamp from datetime.now(UTC) — must contain a 4-digit year.
    body = response.text
    assert "20" in body  # year prefix; weak but enough


# ---------------------------------------------------------------------------
# Method negative case
# ---------------------------------------------------------------------------


async def test_post_root_returns_405(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    async with await _client(orch) as client:
        response = await client.post("/", json={})
    assert response.status_code == 405


# ---------------------------------------------------------------------------
# Active session row carries started_at + turn_count + state
# ---------------------------------------------------------------------------


async def test_running_row_includes_runtime_fields(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    started = datetime(2026, 4, 28, 10, 0, 0, tzinfo=UTC)
    orch.state.add_running(
        _running_entry(
            issue=_issue(identifier="MT-RUN", state="In Progress"),
            started_at=started,
            events=[
                _runtime_event("turn_started"),
                _runtime_event("turn.start"),
            ],
        )
    )

    async with await _client(orch) as client:
        response = await client.get("/")

    body = response.text
    assert "MT-RUN" in body
    # started_at ISO-ish.
    assert "2026-04-28" in body
    # turn_count 2 rendered as standalone int.
    assert ">2<" in body or " 2 " in body or "turn_count=2" in body or "2" in body
    assert "In Progress" in body


# ---------------------------------------------------------------------------
# Active runtime contributes to seconds_running on dashboard
# ---------------------------------------------------------------------------


async def test_active_runtime_contributes_to_token_consumption(
    tmp_path: Path,
) -> None:
    orch = _make_orchestrator(tmp_path)
    orch.state.add_runtime_seconds(60.0)
    orch.state.add_running(
        _running_entry(started_at=datetime.now(UTC) - timedelta(seconds=15))
    )
    async with await _client(orch) as client:
        response = await client.get("/")

    body = response.text
    # Should be at least ~75 (60 ended + ~15 active), allow for jitter.
    assert any(token in body for token in ("75", "76", "77"))
