"""Tests for the global error envelope + 405/404/422→400 wrapper (§13.7.2)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError

from river_gang.config.defaults import apply_defaults
from river_gang.http import create_app
from river_gang.orchestrator import (
    Mailbox,
    Orchestrator,
    OrchestratorState,
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


def _client_for(orch: Orchestrator) -> httpx.AsyncClient:
    app = create_app(orch)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# 405 — POST on a GET-only route → envelope + Allow header
# ---------------------------------------------------------------------------


async def test_post_on_state_returns_405_envelope(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    async with _client_for(orch) as client:
        response = await client.post("/api/v1/state", json={})

    assert response.status_code == 405
    body = response.json()
    assert body == {
        "error": {
            "code": "method_not_allowed",
            "message": "method POST not allowed on /api/v1/state",
        }
    }
    # Allow header preserved per HTTP spec.
    assert "GET" in response.headers.get("allow", "")


async def test_get_on_refresh_returns_405_envelope(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    async with _client_for(orch) as client:
        response = await client.get("/api/v1/refresh")

    assert response.status_code == 405
    body = response.json()
    assert body["error"]["code"] == "method_not_allowed"
    assert "GET" in body["error"]["message"]
    assert "POST" in response.headers.get("allow", "")


async def test_post_on_root_dashboard_returns_405_envelope(
    tmp_path: Path,
) -> None:
    orch = _make_orchestrator(tmp_path)
    async with _client_for(orch) as client:
        response = await client.post("/", json={})

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


# ---------------------------------------------------------------------------
# 404 — unknown route → envelope
# ---------------------------------------------------------------------------


async def test_unknown_route_returns_404_envelope(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    async with _client_for(orch) as client:
        response = await client.get("/totally/bogus/path")

    assert response.status_code == 404
    body = response.json()
    assert body == {
        "error": {
            "code": "not_found",
            "message": "no route for GET /totally/bogus/path",
        }
    }


async def test_unknown_api_v1_route_returns_404_envelope(
    tmp_path: Path,
) -> None:
    """``/api/v1/<unknown>`` falls into the issue catch-all → its own
    'issue_not_found' envelope (specific code preserved)."""
    orch = _make_orchestrator(tmp_path)
    async with _client_for(orch) as client:
        response = await client.get("/api/v1/MT-XYZ-not-tracked")

    assert response.status_code == 404
    body = response.json()
    # Specific code from the issue endpoint preserved.
    assert body["error"]["code"] == "issue_not_found"
    assert "MT-XYZ-not-tracked" in body["error"]["message"]


# ---------------------------------------------------------------------------
# 422 → 400 invalid_request
# ---------------------------------------------------------------------------


async def test_validation_error_returns_400_invalid_request_envelope(
    tmp_path: Path,
) -> None:
    """Mount a route on the orchestrator's app that triggers
    RequestValidationError so the global handler kicks in.

    No production route currently does Pydantic body validation, so we
    add an ad-hoc test-only route to drive the handler. It exercises
    the same wrapper the production code paths will use as soon as
    body-bound endpoints exist.
    """
    from pydantic import BaseModel

    orch = _make_orchestrator(tmp_path)
    app = create_app(orch)

    class _Body(BaseModel):
        n: int

    @app.post("/_test_validate")
    async def _v(body: _Body) -> dict[str, int]:
        return {"n": body.n}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.post("/_test_validate", json={"n": "not-an-int"})

    # 422 from FastAPI is rewritten to 400 with our envelope.
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert isinstance(body["error"]["message"], str)
    assert body["error"]["message"]  # non-empty


# ---------------------------------------------------------------------------
# Backward-compat: existing issue-not-found envelope unchanged.
# ---------------------------------------------------------------------------


async def test_issue_not_found_envelope_unchanged(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    async with _client_for(orch) as client:
        response = await client.get("/api/v1/MT-Ghost")

    assert response.status_code == 404
    body = response.json()
    assert body == {
        "error": {
            "code": "issue_not_found",
            "message": "identifier=MT-Ghost not tracked",
        }
    }


# ---------------------------------------------------------------------------
# Content-Type
# ---------------------------------------------------------------------------


async def test_error_envelope_content_type_is_json(tmp_path: Path) -> None:
    orch = _make_orchestrator(tmp_path)
    async with _client_for(orch) as client:
        response_404 = await client.get("/totally/bogus")
        response_405 = await client.post("/api/v1/state", json={})

    assert "application/json" in response_404.headers["content-type"]
    assert "application/json" in response_405.headers["content-type"]


# ---------------------------------------------------------------------------
# Status-code → default error code mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (401, "unauthorized"),
        (403, "forbidden"),
        (418, "http_error"),  # arbitrary non-mapped status
    ],
)
async def test_default_code_mapping(
    tmp_path: Path, status: int, expected_code: str
) -> None:
    """Status codes without a dedicated slug fall back to a default."""
    orch = _make_orchestrator(tmp_path)
    app = create_app(orch)

    @app.get(f"/_raise_{status}")
    async def _r() -> None:
        raise HTTPException(status_code=status, detail="forced")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.get(f"/_raise_{status}")

    assert response.status_code == status
    assert response.json()["error"]["code"] == expected_code


# ---------------------------------------------------------------------------
# Custom HTTPException with detail dict carrying a code is preserved.
# ---------------------------------------------------------------------------


async def test_http_exception_with_envelope_detail_passthrough(
    tmp_path: Path,
) -> None:
    """Routes that raise HTTPException with detail={'code': ..., 'message': ...}
    keep their specific code rather than getting overwritten."""
    orch = _make_orchestrator(tmp_path)
    app = create_app(orch)

    @app.get("/_custom_envelope")
    async def _r() -> None:
        raise HTTPException(
            status_code=409,
            detail={"code": "conflict_specific", "message": "the thing"},
        )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.get("/_custom_envelope")

    assert response.status_code == 409
    body = response.json()
    assert body == {
        "error": {"code": "conflict_specific", "message": "the thing"}
    }


# ---------------------------------------------------------------------------
# Direct unit test of the validation-error handler — synthesise one without
# requiring a live route. Belt-and-braces around the in-process FastAPI test.
# ---------------------------------------------------------------------------


def test_validation_handler_unit() -> None:
    """The handler must produce the envelope from a fabricated
    :class:`RequestValidationError` — guards against future changes
    that bypass the live ASGI route used in
    :func:`test_validation_error_returns_400_invalid_request_envelope`.
    """
    from river_gang.http.app import _validation_exception_handler

    fake_app = FastAPI()  # only used to build a Request stub; never served
    errs = [
        {"loc": ("body", "n"), "msg": "value is not a valid integer", "type": "x"}
    ]
    exc = RequestValidationError(errs)

    # The handler signature is ``(request, exc) -> Response``. We can pass
    # ``None`` for request because the handler doesn't introspect it.
    # ``asyncio.run`` over ``get_event_loop().run_until_complete`` so this
    # works regardless of whether pytest-asyncio left a closed loop in TLS.
    response = asyncio.run(
        _validation_exception_handler(None, exc)  # type: ignore[arg-type]
    )

    assert response.status_code == 400
    import json
    body = json.loads(response.body)
    assert body["error"]["code"] == "invalid_request"
    assert "value is not a valid integer" in body["error"]["message"]
    _ = fake_app  # silence unused
