"""Service startup orchestration + graceful shutdown (SPED §16.1, IDC #7).

:func:`start_service` is the single entry point that wires every layer
together and runs the orchestrator until SIGINT/SIGTERM (or an injected
``shutdown_event`` for tests). It returns a process exit code so the
service binary can ``sys.exit(asyncio.run(start_service(...)))``.

Startup sequence (matches the §16.1 algorithm):

1. ``configure_logging()`` — first, so subsequent validation failures
   are operator-visible.
2. Observability outputs — covered by step 1 today; placeholder for
   future structured sinks.
3. Workflow watcher — :class:`WorkflowWatcher` started so reload events
   land before the first tick.
4. Initialize state — load workflow, ``resolve_and_validate``, build
   :class:`OrchestratorState`, :class:`Mailbox`, :class:`RetryQueue`.
5. ``validate_for_dispatch`` — failure → log
   :func:`format_error_for_operator` + return ``1`` (no exception leak).
6. Startup terminal workspace cleanup (§8.6) — best-effort; tracker
   failure → log warning + continue.
7. Build clients via injectable factories so tests can drop in fakes.
8. Build :class:`Orchestrator`.
9. Optionally build HTTP app.
10. ``schedule_initial_tick``.
11. Install signal handlers; ``await orchestrator.run()``.
12. Graceful shutdown (IDC #7): cancel next tick → stop HTTP →
    cancel retry timers → cancel + await workers with bounded grace
    → stop workflow watcher → return 0.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Callable
from pathlib import Path
from typing import Any

from river_gang.config.resolution import resolve_and_validate
from river_gang.config.schema import EffectiveConfig
from river_gang.config.validation import (
    format_error_for_operator,
    validate_for_dispatch,
)
from river_gang.observability.logging import configure_logging
from river_gang.orchestrator.loop import Orchestrator
from river_gang.orchestrator.mailbox import Mailbox, Shutdown
from river_gang.orchestrator.retry import RetryQueue
from river_gang.orchestrator.state import OrchestratorState
from river_gang.workflow.loader import load_workflow
from river_gang.workflow.watcher import WorkflowWatcher

logger = logging.getLogger(__name__)


SHUTDOWN_GRACE_MS = 30_000

_DEFAULT_EXIT_OK = 0
_DEFAULT_EXIT_FAIL = 1


def fail_startup(reason: str) -> int:
    """Log an operator-visible startup error and return a nonzero exit code."""
    logger.error("startup failed: %s", reason)
    return _DEFAULT_EXIT_FAIL


# ---------------------------------------------------------------------------
# Factory typedefs (injectable for tests)
# ---------------------------------------------------------------------------

TrackerFactory = Callable[[EffectiveConfig], Any]
CodexClientFactory = Callable[[EffectiveConfig], Any]
WorkspaceManagerFactory = Callable[[EffectiveConfig], Any]
HttpAppFactory = Callable[[OrchestratorState], Any]
StateHook = Callable[[OrchestratorState], None]


def _default_tracker_factory(config: EffectiveConfig) -> Any:
    """Build a real :class:`LinearClient` from config."""
    from river_gang.tracker.client import LinearClient
    from river_gang.tracker.linear_transport import LinearTransport

    transport = LinearTransport(
        endpoint=config.tracker.endpoint or "https://api.linear.app/graphql",
        api_key=config.tracker.api_key,
    )
    return LinearClient(
        transport=transport,
        project_slug=config.tracker.project_slug or "",
    )


def _default_workspace_manager_factory(config: EffectiveConfig) -> Any:
    from river_gang.workspace.manager import WorkspaceManager

    return WorkspaceManager(config=config)


def _default_codex_client_factory(_config: EffectiveConfig) -> Any:
    """Startup-time factory hook for the legacy singleton :class:`CodexClient`
    injection seam (used by tests that pre-build a single fake client).

    Production wiring does NOT build a singleton at startup: subprocess
    lifecycle is bound to a single Codex session per §10.1, so a fresh
    :class:`CodexClient` is constructed for every worker attempt by
    :func:`build_default_per_worker_codex_factory`. Returning ``None``
    here is the documented signal that triggers ``start_service`` to
    fall back to the per-worker factory path; tests that want the
    singleton path inject their own ``codex_client_factory`` and bypass
    this default entirely.
    """
    return None


def build_default_per_worker_codex_factory(
    *,
    transport: Any,
    project_slug: str,
) -> Callable[[Path, EffectiveConfig], Any]:
    """Return a :class:`CodexClientPerWorkerFactory` that spawns a real
    Codex subprocess + :class:`CodexClient` for every attempt.

    Args:
        transport: configured :class:`LinearTransport` reused across
            attempts so the agent's :class:`LinearGraphqlTool` calls go
            through the orchestrator's authenticated transport (no second
            credential).
        project_slug: needed to validate the transport's binding; when
            empty no :class:`LinearGraphqlTool` is wired (agent loses the
            tool but the run still proceeds).

    The returned factory:

    1. Launches ``codex.command`` via :class:`CodexProcess.launch` with
       the per-issue workspace as ``cwd`` and ``workspace.root`` as the
       containment root (rejects attempts to ``cd`` out of the sandbox).
    2. Builds a :class:`CodexClient` wired to the live transport-backed
       :class:`LinearGraphqlTool` (when ``project_slug`` is non-empty).
    3. Returns the client; the worker calls ``start_session`` on it.
    """
    # Local imports keep ``startup.py`` cheap to import for callers that
    # don't need the codex extension.
    from typing import cast

    from river_gang.codex.client import (
        CodexClient,
        _LinearGraphqlToolLike,
        _ProcessLike,
    )
    from river_gang.codex.process import CodexProcess
    from river_gang.tools.linear_graphql import LinearGraphqlTool

    async def _factory(workspace_path: Path, config: EffectiveConfig) -> Any:
        workspace_root = Path(config.workspace.root)
        process = await CodexProcess.launch(
            config.codex.command,
            cwd=workspace_path,
            workspace_root=workspace_root,
        )
        linear_tool: LinearGraphqlTool | None = None
        if project_slug:
            linear_tool = LinearGraphqlTool(transport=transport)
        # CodexProcess + LinearGraphqlTool are structural matches for the
        # client's protocols; cast crosses the nominal boundary because
        # the protocols are private (``_ProcessLike`` / ``_LinearGraphqlToolLike``).
        return CodexClient(
            process=cast(_ProcessLike, process),
            codex_app_server_pid=process.pid,
            linear_graphql_tool=cast(_LinearGraphqlToolLike | None, linear_tool),
        )

    return _factory


# ---------------------------------------------------------------------------
# start_service
# ---------------------------------------------------------------------------


async def start_service(
    *,
    workflow_path: Path,
    install_signal_handlers: bool = True,
    shutdown_event: asyncio.Event | None = None,
    shutdown_grace_ms: int = SHUTDOWN_GRACE_MS,
    tracker_factory: TrackerFactory | None = None,
    codex_client_factory: CodexClientFactory | None = None,
    workspace_manager_factory: WorkspaceManagerFactory | None = None,
    http_app_factory: HttpAppFactory | None = None,
    awatch_factory: Any | None = None,
    state_hook: StateHook | None = None,
    port: int | None = None,
) -> int:
    """Start the orchestrator service. Returns process exit code.

    ``port`` enables the OPTIONAL HTTP extension (§13.7): when set,
    :func:`river_gang.http.app.create_app` builds a FastAPI app and
    :func:`river_gang.http.server.start_server` binds uvicorn on
    ``127.0.0.1:<port>``. ``None`` keeps the HTTP path inert. When
    ``http_app_factory`` is also provided, the factory takes precedence
    — tests inject custom factories to verify wiring without binding a
    real socket.
    """
    # 1. Configure logging FIRST so validation failures are visible.
    configure_logging()

    # 2. Observability outputs — covered by configure_logging today.

    # 3. Load + validate the initial workflow before starting the watcher,
    #    so a malformed file fails fast with a clean exit code.
    try:
        wd = load_workflow(workflow_path)
        config = resolve_and_validate(wd.config, workflow_dir=workflow_path.parent)
    except Exception as exc:  # noqa: BLE001 -- bad workflow → operator-visible exit
        return fail_startup(f"failed to load workflow {workflow_path}: {exc}")

    prompt_template = wd.prompt_template

    # 5. Validation preflight (run before building heavy clients so an
    #    invalid config fails fast).
    validation = validate_for_dispatch(config)
    if not validation.ok:
        message = format_error_for_operator(validation)
        logger.error("%s", message)
        return _DEFAULT_EXIT_FAIL

    # 4. Build mailbox + retry queue + state.
    mailbox = Mailbox()
    loop = asyncio.get_running_loop()
    retry_queue = RetryQueue(loop=loop)
    state = OrchestratorState(
        poll_interval_ms=config.polling.interval_ms,
        max_concurrent_agents=config.agent.max_concurrent_agents,
    )

    # Test seam: let tests pre-populate state.running before the run loop
    # so graceful-shutdown paths can be exercised in isolation.
    if state_hook is not None:
        state_hook(state)

    # 7. Build clients via factories.
    tracker_factory = tracker_factory or _default_tracker_factory
    codex_client_factory = codex_client_factory or _default_codex_client_factory
    workspace_manager_factory = (
        workspace_manager_factory or _default_workspace_manager_factory
    )
    tracker = tracker_factory(config)
    codex_client = codex_client_factory(config)
    workspace_manager = workspace_manager_factory(config)

    # When the test/operator-supplied factory yielded ``None``, fall back to
    # the per-worker default that spawns a fresh :class:`CodexClient` (with
    # its own ``codex app-server`` subprocess) per attempt — which is the
    # production-correct lifecycle per §10.1. ``getattr`` on the tracker
    # keeps the path tolerant of fakes that don't expose ``_transport``.
    per_worker_codex_factory: Any | None = None
    if codex_client is None:
        transport = getattr(tracker, "_transport", None)
        if transport is not None:
            per_worker_codex_factory = build_default_per_worker_codex_factory(
                transport=transport,
                project_slug=config.tracker.project_slug or "",
            )

    # 6. Startup terminal cleanup (best-effort, §8.6).
    await _startup_terminal_cleanup(
        tracker=tracker,
        workspace_manager=workspace_manager,
        terminal_states=list(config.tracker.terminal_states),
    )

    # 3 (cont.). Workflow watcher — started after state is built so the
    # on_reload callback can post into the live mailbox.
    watcher = _build_watcher(
        path=workflow_path,
        mailbox=mailbox,
        awatch_factory=awatch_factory,
    )
    await watcher.start()
    # Seed last_known_good with the initial config so the orchestrator's
    # defensive reload sees a value on the very first tick.
    await watcher.last_known_good.set(config)

    # 8. Build orchestrator.
    orchestrator = Orchestrator(
        state=state,
        mailbox=mailbox,
        retry_queue=retry_queue,
        tracker=tracker,
        codex_client=codex_client,
        workspace_manager=workspace_manager,
        prompt_template=prompt_template,
        config=config,
        workflow_loader=lambda: _sync_holder_get(watcher.last_known_good),
        codex_client_factory=per_worker_codex_factory,
    )

    # 9. Optional HTTP app (§13.7).
    #    Precedence: explicit ``http_app_factory`` (tests, custom wiring)
    #    over the default ``port`` driven branch. The default branch
    #    builds the production FastAPI app via ``create_app`` and binds
    #    uvicorn on ``127.0.0.1:<port>`` — a 0 here means "ephemeral".
    http_handle: Any = None
    if http_app_factory is not None:
        http_handle = http_app_factory(state)
    elif port is not None:
        http_handle = await _start_default_http_server(
            orchestrator=orchestrator, port=port
        )

    # 11. Install signal handlers.
    signal_handlers_installed = False
    if install_signal_handlers:
        signal_handlers_installed = _install_signal_handlers(
            loop=loop, mailbox=mailbox
        )

    # 11 (cont.). Pump shutdown_event → mailbox in the background, if any.
    shutdown_pump_task: asyncio.Task[None] | None = None
    if shutdown_event is not None:
        shutdown_pump_task = asyncio.create_task(
            _pump_shutdown_event(shutdown_event, mailbox),
            name="startup-shutdown-pump",
        )

    # 10. Schedule first tick.
    orchestrator.schedule_initial_tick()

    # 11 (cont.). Run the dispatcher loop.
    try:
        await orchestrator.run()
    finally:
        # 12. Graceful shutdown sequence.
        if signal_handlers_installed:
            _remove_signal_handlers(loop)
        if shutdown_pump_task is not None and not shutdown_pump_task.done():
            shutdown_pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await shutdown_pump_task

        await _graceful_shutdown(
            orchestrator=orchestrator,
            retry_queue=retry_queue,
            watcher=watcher,
            http_handle=http_handle,
            shutdown_grace_ms=shutdown_grace_ms,
        )

    return _DEFAULT_EXIT_OK


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _sync_holder_get(holder: Any) -> EffectiveConfig | None:
    """Read :class:`LastKnownGoodHolder` synchronously from inside a tick.

    Delegates to :meth:`LastKnownGoodHolder.peek`, which is the documented
    sync fast-path: writes go through ``await set()`` and the single
    attribute read is atomic under the GIL.
    """
    value: EffectiveConfig | None = holder.peek()
    return value


def _build_watcher(
    *,
    path: Path,
    mailbox: Mailbox,
    awatch_factory: Any | None,
) -> WorkflowWatcher:
    from river_gang.orchestrator.mailbox import ConfigReloaded

    def _on_reload(cfg: EffectiveConfig) -> None:
        # The watcher's reload callback is sync; ``send_nowait`` honours
        # that contract while keeping the public mailbox surface in use.
        mailbox.send_nowait(ConfigReloaded(config=cfg))

    return WorkflowWatcher(
        path=path,
        on_reload=_on_reload,
        awatch_factory=awatch_factory,
    )


async def _pump_shutdown_event(
    event: asyncio.Event, mailbox: Mailbox
) -> None:
    """Wait for the test-injected shutdown_event then post :class:`Shutdown`."""
    try:
        await event.wait()
    except asyncio.CancelledError:
        return
    mailbox.send_nowait(Shutdown())


def _install_signal_handlers(
    *, loop: asyncio.AbstractEventLoop, mailbox: Mailbox
) -> bool:
    """Install SIGINT/SIGTERM → post :class:`Shutdown`. POSIX-only."""

    def _post_shutdown() -> None:
        mailbox.send_nowait(Shutdown())

    installed = False
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _post_shutdown)
            installed = True
        except NotImplementedError:
            # Windows / non-POSIX: skip — caller will rely on
            # KeyboardInterrupt or shutdown_event instead.
            return False
    return installed


def _remove_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.remove_signal_handler(sig)


# ---------------------------------------------------------------------------
# Startup terminal cleanup (§8.6)
# ---------------------------------------------------------------------------


async def _startup_terminal_cleanup(
    *,
    tracker: Any,
    workspace_manager: Any,
    terminal_states: list[str],
) -> None:
    """Best-effort §8.6 cleanup. Tracker / workspace failures are logged
    and swallowed so they don't block startup.
    """
    if not terminal_states:
        return

    fetch = getattr(tracker, "fetch_issues_by_states", None)
    if fetch is None:
        logger.warning(
            "tracker has no fetch_issues_by_states — skipping startup "
            "terminal cleanup"
        )
        return

    try:
        terminal_issues = await fetch(terminal_states)
    except Exception as exc:  # noqa: BLE001 -- §8.6 best-effort
        logger.warning(
            "startup terminal cleanup: fetch_issues_by_states failed — "
            "continuing without cleanup: %s",
            exc,
        )
        return

    for issue in terminal_issues:
        identifier = getattr(issue, "identifier", None)
        if not identifier:
            continue
        try:
            await workspace_manager.cleanup_for_issue(identifier)
        except Exception as exc:  # noqa: BLE001 -- §8.6 best-effort
            logger.warning(
                "startup terminal cleanup: cleanup_for_issue %s raised — "
                "continuing: %s",
                identifier,
                exc,
            )


# ---------------------------------------------------------------------------
# Graceful shutdown (IDC #7)
# ---------------------------------------------------------------------------


async def _graceful_shutdown(
    *,
    orchestrator: Orchestrator,
    retry_queue: RetryQueue,
    watcher: WorkflowWatcher,
    http_handle: Any,
    shutdown_grace_ms: int,
) -> None:
    """Bounded teardown sequence per IDC #7."""
    # Stop accepting new ticks (idempotent — orchestrator.run already did this).
    orchestrator.cancel_next_tick()

    # Stop HTTP server if one was started.
    await _stop_http(http_handle)

    # Cancel all retry timers — none of them should fire post-shutdown.
    retry_queue.cancel_all()

    # Cancel + await running workers with bounded grace.
    await _drain_workers(orchestrator, shutdown_grace_ms=shutdown_grace_ms)

    # Stop watcher.
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await watcher.stop()


async def _start_default_http_server(
    *, orchestrator: Orchestrator, port: int
) -> Any:
    """Build the production FastAPI app + bind uvicorn on loopback.

    Loopback-only by design (§13.7): operators must opt-in to external
    exposure with explicit infrastructure (reverse proxy, Tailscale,
    etc.). Returns the :class:`ServerHandle` from
    :func:`river_gang.http.server.start_server`.
    """
    # Local imports keep ``startup.py`` cheap to import for callers that
    # don't need the HTTP extension (CLI-only mode).
    from river_gang.http.app import create_app
    from river_gang.http.server import start_server

    app = create_app(orchestrator)
    return await start_server(app, host="127.0.0.1", port=port)


async def _stop_http(http_handle: Any) -> None:
    """Best-effort stop of the optional HTTP server.

    Recognises three shapes:

    1. Anything with an ``async stop()`` method (production
       :class:`ServerHandle` and any test fake mirroring its surface).
    2. Legacy uvicorn-like objects with ``should_exit`` + ``serve_task``.
    3. Anything else → no-op.
    """
    if http_handle is None:
        return

    stop_method = getattr(http_handle, "stop", None)
    if callable(stop_method):
        with contextlib.suppress(asyncio.CancelledError, Exception):
            result = stop_method()
            if asyncio.iscoroutine(result):
                await result
        return

    if hasattr(http_handle, "should_exit"):
        with contextlib.suppress(Exception):
            http_handle.should_exit = True
    serve_task = getattr(http_handle, "serve_task", None)
    if isinstance(serve_task, asyncio.Task):
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(serve_task, timeout=5.0)


async def _drain_workers(
    orchestrator: Orchestrator, *, shutdown_grace_ms: int
) -> None:
    """Cancel all running worker tasks and await with bounded grace."""
    entries = list(orchestrator.state.running.values())
    if not entries:
        return

    grace_s = max(shutdown_grace_ms, 0) / 1000.0

    for entry in entries:
        if not entry.worker_handle.done():
            entry.worker_handle.cancel()

    pending = [
        entry.worker_handle
        for entry in entries
        if not entry.worker_handle.done()
    ]
    if not pending:
        return

    done, still_pending = await asyncio.wait(pending, timeout=grace_s)
    if still_pending:
        logger.warning(
            "shutdown grace %dms expired with %d worker(s) still running — "
            "force-cancelling",
            shutdown_grace_ms,
            len(still_pending),
        )
        for task in still_pending:
            task.cancel()
        # Brief secondary wait so the second cancel can land before we
        # return control to the event loop's teardown.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait(still_pending, timeout=1.0)

    # Surface any exceptions for observability — they're already logged
    # by the worker, so we just swallow here.
    for task in done:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()


__all__ = [
    "SHUTDOWN_GRACE_MS",
    "fail_startup",
    "start_service",
]
