"""FastAPI app factory for the OPTIONAL HTTP extension (SPED §13.7).

This module owns only the app object — uvicorn lifecycle lives in
:mod:`river_gang.http.server`. Routes are added incrementally by
Tasks 42-46; this scaffold just wires the orchestrator-driven providers
onto ``app.state`` so route handlers can read fresh state on every
request without holding a reference to the orchestrator itself.

Provider indirection (``state_provider`` / ``retry_queue_provider``)
exists for two reasons:

1. **Test-injection** — tests can swap the provider with a closure that
   returns a hand-built :class:`OrchestratorState` without standing up
   a real orchestrator.
2. **Single-writer safety** — handlers should treat the values they
   read as read-only snapshots. Providers stay simple ``() -> T``
   callables so any future upgrade (per-request snapshot via
   :func:`build_snapshot`) is a one-line change.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from river_gang.http.routes_dashboard import router as dashboard_router
from river_gang.http.routes_issue import router as issue_router
from river_gang.http.routes_refresh import router as refresh_router
from river_gang.http.routes_state import router as state_router
from river_gang.orchestrator.loop import Orchestrator
from river_gang.orchestrator.retry import RetryQueue
from river_gang.orchestrator.state import OrchestratorState

# Default error-code slug per HTTP status (§13.7.2 design notes). Statuses
# without a dedicated slug fall back to ``"http_error"``.
_DEFAULT_ERROR_CODES: dict[int, str] = {
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
}

StateProvider = Callable[[], OrchestratorState]
RetryQueueProvider = Callable[[], RetryQueue]
# Mailbox is duck-typed (real :class:`Mailbox` or test-only recorder) so
# the provider returns ``Any`` — the only contract is ``async send(msg)``.
MailboxProvider = Callable[[], Any]


def create_app(
    orchestrator: Orchestrator,
    *,
    state_provider: StateProvider | None = None,
    retry_queue_provider: RetryQueueProvider | None = None,
    mailbox_provider: MailboxProvider | None = None,
) -> FastAPI:
    """Build a :class:`FastAPI` app wired to the given orchestrator.

    Default providers wrap ``orchestrator.state`` /
    ``orchestrator.retry_queue`` / ``orchestrator.mailbox`` — pass
    explicit callables to override for tests or for setups that swap
    the orchestrator hot-path.

    The returned app has a single ``GET /healthz`` route used as a
    liveness probe by tests and external watchdogs. Real observability
    routes land in Tasks 42-46.
    """
    app = FastAPI(title="symphony", version="0.1.0")

    app.state.state_provider = state_provider or (lambda: orchestrator.state)
    app.state.retry_queue_provider = retry_queue_provider or (
        lambda: orchestrator.retry_queue
    )
    app.state.mailbox_provider = mailbox_provider or (
        lambda: orchestrator.mailbox
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True}

    app.include_router(state_router)
    app.include_router(refresh_router)
    # Detail route registered AFTER literal /api/v1/state and /api/v1/refresh
    # so those paths take precedence over the catch-all ``/{identifier}``.
    app.include_router(issue_router)
    # Dashboard at root — orthogonal to /api/v1/* so registration order
    # is irrelevant, but keep it last for symmetry with public surface.
    app.include_router(dashboard_router)

    # Global error-envelope wrapping per §13.7.2 design notes.
    app.add_exception_handler(
        StarletteHTTPException, _http_exception_handler
    )
    app.add_exception_handler(
        RequestValidationError, _validation_exception_handler
    )

    return app


# ---------------------------------------------------------------------------
# Exception handlers (module-level so tests can unit-call them directly)
# ---------------------------------------------------------------------------


async def _http_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """Wrap any ``HTTPException`` in the §13.7.2 error envelope.

    Two passthrough rules so route handlers can pre-shape responses:

    1. ``exc.detail`` already a dict carrying ``code`` + ``message`` →
       use it verbatim. Routes raising ``HTTPException`` with a custom
       envelope detail keep their specific code (e.g. issue endpoint's
       ``"issue_not_found"``).
    2. Otherwise → derive ``code`` from
       :data:`_DEFAULT_ERROR_CODES` and synthesise a human-readable
       ``message`` for 404/405; fall back to ``str(exc.detail)``
       otherwise.

    The original status code and any ``Allow`` / ``WWW-Authenticate``
    headers from the source exception propagate through.
    """
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101 -- registered handler
    detail = exc.detail
    headers = exc.headers or {}

    if (
        isinstance(detail, dict)
        and isinstance(detail.get("code"), str)
        and isinstance(detail.get("message"), str)
    ):
        envelope = {"error": {"code": detail["code"], "message": detail["message"]}}
        return JSONResponse(
            status_code=exc.status_code, content=envelope, headers=headers
        )

    code = _DEFAULT_ERROR_CODES.get(exc.status_code, "http_error")
    message = _format_default_message(exc, request)
    envelope = {"error": {"code": code, "message": message}}
    return JSONResponse(
        status_code=exc.status_code, content=envelope, headers=headers
    )


def _format_default_message(
    exc: StarletteHTTPException, request: Request
) -> str:
    """Build a useful ``message`` string for non-envelope HTTPExceptions."""
    if exc.status_code == 404:
        return f"no route for {request.method} {request.url.path}"
    if exc.status_code == 405:
        return f"method {request.method} not allowed on {request.url.path}"
    if isinstance(exc.detail, str) and exc.detail:
        return exc.detail
    return str(exc.detail) if exc.detail is not None else "http error"


async def _validation_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """Map ``RequestValidationError`` (FastAPI's 422) to ``400 invalid_request``.

    The plan calls for a uniform 400 envelope so clients don't have to
    branch on FastAPI's distinction between "request shape rejected"
    (422) and "request semantics rejected" (typically 400). The first
    validation error message is surfaced verbatim so debugging stays
    actionable.
    """
    assert isinstance(exc, RequestValidationError)  # noqa: S101 -- registered handler
    errors = exc.errors()
    first = errors[0] if errors else {}
    message_part = str(first.get("msg", "")).strip() or "request validation failed"
    loc = first.get("loc") or ()
    if loc:
        loc_str = ".".join(str(x) for x in loc)
        message = f"{loc_str}: {message_part}"
    else:
        message = message_part
    _ = request  # unused but required by FastAPI handler signature
    return JSONResponse(
        status_code=400,
        content={"error": {"code": "invalid_request", "message": message}},
    )


__all__ = [
    "MailboxProvider",
    "RetryQueueProvider",
    "StateProvider",
    "create_app",
]
