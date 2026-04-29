"""HTML dashboard at ``GET /`` (SPED §13.7.1).

Server-rendered Jinja2 view of the same :class:`Snapshot` powering
``GET /api/v1/state``. No external CSS/JS — single-file template with
inline styles so the dashboard works without static-file plumbing.

Sections:

- **Active sessions** — running rows.
- **Retry queue** — pending retries.
- **Token consumption** — codex_totals + rate_limits when present.

Empty-state placeholders ("No active sessions." / "Retry queue empty.")
land when the corresponding row list is empty so the page reads cleanly
on a fresh orchestrator.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, PackageLoader, select_autoescape

from river_gang.observability.snapshot import build_snapshot
from river_gang.orchestrator.retry import RetryQueue
from river_gang.orchestrator.state import OrchestratorState

# Jinja2 environment is module-level — a single Environment is the
# documented way to avoid per-request loader/parse overhead. Autoescape
# is on for any extension that produces HTML so dynamic identifier /
# title strings can't smuggle markup.
_env = Environment(
    loader=PackageLoader("river_gang.http", "templates"),
    autoescape=select_autoescape(["html", "xml"]),
)

router = APIRouter(tags=["dashboard"])


@router.get("/", response_class=HTMLResponse)
async def get_dashboard(request: Request) -> HTMLResponse:
    """Render the runtime dashboard."""
    state: OrchestratorState = request.app.state.state_provider()
    retry_queue: RetryQueue = request.app.state.retry_queue_provider()
    snapshot = build_snapshot(state, retry_queue=retry_queue, now=datetime.now(UTC))

    template = _env.get_template("dashboard.html")
    rendered = template.render(snapshot=snapshot)
    return HTMLResponse(content=rendered, status_code=200)


__all__ = ["router"]
