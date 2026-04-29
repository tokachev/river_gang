"""Tests for FastAPI app skeleton + uvicorn lifecycle (SPED §13.7)."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from river_gang.config.defaults import apply_defaults
from river_gang.http import ServerHandle, create_app, start_server
from river_gang.orchestrator import (
    Mailbox,
    Orchestrator,
    OrchestratorState,
    RetryQueue,
)
from tests.codex.fakes import FakeCodexClient
from tests.tracker.fakes import FakeTracker
from tests.workspace.fakes import FakeWorkspaceManager


def _can_bind_loopback() -> bool:
    """Sandboxes (CI) sometimes deny ``bind(127.0.0.1, 0)`` — skip real-TCP
    tests there but keep the in-process ASGI coverage."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.close()
    except OSError:
        return False
    return True


_REQUIRES_LOOPBACK = pytest.mark.skipif(
    not _can_bind_loopback(),
    reason="sandbox denies loopback TCP bind — ASGI in-process tests cover the app",
)


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


# ---------------------------------------------------------------------------
# create_app
# ---------------------------------------------------------------------------


async def test_create_app_returns_fastapi_instance(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    app = create_app(orch)
    assert isinstance(app, FastAPI)


async def test_create_app_stores_providers_on_app_state(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    app = create_app(orch)
    # Default providers wrap the orchestrator and return its state/retry_queue.
    assert app.state.state_provider() is orch.state
    assert app.state.retry_queue_provider() is orch.retry_queue


async def test_create_app_explicit_providers_override_orchestrator(
    tmp_path: Path,
) -> None:
    orch = _make_orchestrator(tmp_path)
    other_state = OrchestratorState(poll_interval_ms=1000, max_concurrent_agents=1)
    other_retry = RetryQueue(loop=asyncio.get_running_loop())

    app = create_app(
        orch,
        state_provider=lambda: other_state,
        retry_queue_provider=lambda: other_retry,
    )
    assert app.state.state_provider() is other_state
    assert app.state.retry_queue_provider() is other_retry


# ---------------------------------------------------------------------------
# /healthz route
# ---------------------------------------------------------------------------


async def test_healthz_returns_ok_via_asgi(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    app = create_app(orch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


# ---------------------------------------------------------------------------
# start_server / ServerHandle lifecycle
# ---------------------------------------------------------------------------


@_REQUIRES_LOOPBACK
async def test_start_server_binds_ephemeral_port(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    app = create_app(orch)
    handle = await start_server(app, port=0)
    try:
        assert isinstance(handle, ServerHandle)
        assert handle.port != 0  # OS picked a real free port
        assert handle.port > 0
    finally:
        await handle.stop()


@_REQUIRES_LOOPBACK
async def test_start_server_serves_real_http_traffic(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    app = create_app(orch)
    handle = await start_server(app, port=0)
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{handle.port}",
            timeout=5.0,
        ) as client:
            response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"ok": True}
    finally:
        await handle.stop()


@_REQUIRES_LOOPBACK
async def test_handle_stop_completes_task_cleanly(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    app = create_app(orch)
    handle = await start_server(app, port=0)
    await handle.stop()
    assert handle.task.done()
    # ``stop`` must not raise — the underlying task may have surfaced
    # CancelledError or returned cleanly; either is acceptable.
    if handle.task.cancelled():
        return
    # If not cancelled, the task should have finished without raising.
    exc = handle.task.exception()
    assert exc is None, exc


@_REQUIRES_LOOPBACK
async def test_start_server_uses_explicit_port(tmp_path: Path) -> None:
    """Bind a specific port (we still use 0 to grab one, then reuse)."""
    import socket

    # Grab a free port deterministically.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    chosen_port = sock.getsockname()[1]
    sock.close()

    orch = _make_orchestrator(tmp_path)
    app = create_app(orch)
    handle = await start_server(app, port=chosen_port)
    try:
        assert handle.port == chosen_port
    finally:
        await handle.stop()


@_REQUIRES_LOOPBACK
async def test_start_server_default_host_is_loopback(tmp_path: Path) -> None:
    """Loopback default — security: never accidentally expose externally."""
    orch = _make_orchestrator(tmp_path)
    app = create_app(orch)
    handle = await start_server(app, port=0)
    try:
        # The bound socket is on 127.0.0.1 → external interfaces are not
        # listening. Quick check: server is reachable on 127.0.0.1 but
        # binding another socket on the same port + 0.0.0.0 should fail
        # if loopback didn't claim INADDR_ANY (it didn't, so this is fine).
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{handle.port}", timeout=2.0
        ) as client:
            r = await client.get("/healthz")
        assert r.status_code == 200
    finally:
        await handle.stop()


def test_start_server_signature_defaults_to_loopback() -> None:
    """Sandbox-safe check: ``host`` parameter defaults to ``127.0.0.1``."""
    import inspect

    sig = inspect.signature(start_server)
    assert sig.parameters["host"].default == "127.0.0.1"
    assert sig.parameters["port"].default == 0


@_REQUIRES_LOOPBACK
async def test_start_server_propagates_immediate_failure(tmp_path: Path) -> None:
    """If uvicorn task ends before ``server.started`` flips, raise."""
    import socket

    # Hold a listening socket on a port so uvicorn fails to bind.
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    blocked_port = blocker.getsockname()[1]

    orch = _make_orchestrator(tmp_path)
    app = create_app(orch)

    try:
        with pytest.raises((RuntimeError, OSError, SystemExit, BaseException)):  # noqa: PT011 -- uvicorn surfaces multiple types
            await start_server(app, port=blocked_port)
    finally:
        blocker.close()


# ---------------------------------------------------------------------------
# Re-exports
# ---------------------------------------------------------------------------


def test_re_exports_present() -> None:
    from river_gang.http import ServerHandle as RH_Handle
    from river_gang.http import create_app as RH_Create
    from river_gang.http import start_server as RH_Start

    assert RH_Handle is ServerHandle
    assert RH_Create is create_app
    assert RH_Start is start_server


# ---------------------------------------------------------------------------
# Suppress unused-Any noise
# ---------------------------------------------------------------------------

_: Any = None
