"""Tests for ``POST /api/v1/refresh`` (SPED §13.7.2)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from river_gang.config.defaults import apply_defaults
from river_gang.http import create_app
from river_gang.orchestrator import (
    Mailbox,
    Orchestrator,
    OrchestratorState,
    PollTick,
    RetryQueue,
)
from tests.codex.fakes import FakeCodexClient
from tests.tracker.fakes import FakeTracker
from tests.workspace.fakes import FakeWorkspaceManager


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


class _RecordingMailbox:
    """Records messages without actually wiring asyncio queues."""

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, msg: Any) -> None:
        self.sent.append(msg)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_post_refresh_returns_202_with_envelope(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    app = create_app(orch)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.post("/api/v1/refresh")

    assert response.status_code == 202
    body = response.json()
    assert body["queued"] is True
    assert body["coalesced"] is False
    assert body["operations"] == ["poll", "reconcile"]
    assert "requested_at" in body


async def test_post_refresh_sends_poll_tick_to_mailbox(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    recording = _RecordingMailbox()
    app = create_app(orch, mailbox_provider=lambda: recording)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        await client.post("/api/v1/refresh")

    assert len(recording.sent) == 1
    assert isinstance(recording.sent[0], PollTick)


async def test_default_mailbox_provider_wraps_orchestrator_mailbox(
    tmp_path: Path,
) -> None:
    orch = _make_orchestrator(tmp_path)
    app = create_app(orch)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.post("/api/v1/refresh")

    assert response.status_code == 202
    # Real Mailbox: PollTick landed and is dequeueable.
    msg = await asyncio.wait_for(orch.mailbox.recv(), timeout=0.5)
    assert isinstance(msg, PollTick)


# ---------------------------------------------------------------------------
# Repeat calls — no coalescing, independent timestamps
# ---------------------------------------------------------------------------


async def test_two_rapid_posts_both_queue_without_coalescing(
    tmp_path: Path,
) -> None:
    orch = _make_orchestrator(tmp_path)
    recording = _RecordingMailbox()
    app = create_app(orch, mailbox_provider=lambda: recording)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        r1 = await client.post("/api/v1/refresh")
        r2 = await client.post("/api/v1/refresh")

    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r1.json()["queued"] is True
    assert r2.json()["queued"] is True
    assert r1.json()["coalesced"] is False
    assert r2.json()["coalesced"] is False
    # Both timestamps present (datetime serialisation may render them
    # equal at sub-millisecond resolution — just assert both populated).
    assert r1.json()["requested_at"]
    assert r2.json()["requested_at"]
    # Two PollTick messages enqueued.
    assert len(recording.sent) == 2
    assert all(isinstance(m, PollTick) for m in recording.sent)


# ---------------------------------------------------------------------------
# Body acceptance — empty + JSON body both work, body ignored
# ---------------------------------------------------------------------------


async def test_post_refresh_with_json_body_still_succeeds(
    tmp_path: Path,
) -> None:
    orch = _make_orchestrator(tmp_path)
    app = create_app(orch)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.post("/api/v1/refresh", json={"ignored": True})

    assert response.status_code == 202


async def test_post_refresh_with_empty_body_succeeds(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    app = create_app(orch)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        # No body at all.
        response = await client.post("/api/v1/refresh", content=b"")
    assert response.status_code == 202


# ---------------------------------------------------------------------------
# Method negative case
# ---------------------------------------------------------------------------


async def test_get_refresh_returns_405(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    app = create_app(orch)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/refresh")

    assert response.status_code == 405


# ---------------------------------------------------------------------------
# Content-Type
# ---------------------------------------------------------------------------


async def test_response_content_type_is_json(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    app = create_app(orch)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.post("/api/v1/refresh")

    assert "application/json" in response.headers["content-type"]


# ---------------------------------------------------------------------------
# Provider override sanity — explicit mailbox_provider reaches handler
# ---------------------------------------------------------------------------


async def test_explicit_mailbox_provider_overrides_default(
    tmp_path: Path,
) -> None:
    orch = _make_orchestrator(tmp_path)
    other = _RecordingMailbox()
    app = create_app(orch, mailbox_provider=lambda: other)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        await client.post("/api/v1/refresh")

    # Default orchestrator.mailbox must not have received the tick.
    assert orch.mailbox.qsize() == 0
    # The explicit provider's recording mailbox got it.
    assert len(other.sent) == 1
    assert isinstance(other.sent[0], PollTick)
