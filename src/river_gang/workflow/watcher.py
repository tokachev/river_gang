"""Dynamic WORKFLOW.md reload watcher (SPED §6.2).

Wraps :func:`watchfiles.awatch` in an asyncio-friendly facade.

Reload pipeline on every debounced change batch:

1. ``load_workflow(path)`` → raw config map + prompt template
2. ``resolve_and_validate(raw, workflow_dir=...)`` → :class:`EffectiveConfig`
3. ``on_reload(cfg)`` callback + atomic swap into :class:`LastKnownGoodHolder`

Failure of any step is logged at ``ERROR`` (operator-visible) and the holder
keeps its previous value — in-flight workers retain their already-captured
config reference, satisfying §6.2's "do not restart in-flight sessions" rule.

Public surface:

- :class:`LastKnownGoodHolder` — atomic ``set``/``get`` of an immutable value.
- :class:`WorkflowWatcher` — start/stop background watch task; exposes the
  holder so callers can read the current config at any time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Generic, TypeVar, cast

from watchfiles import awatch as _default_awatch

from river_gang.config.resolution import resolve_and_validate
from river_gang.config.schema import EffectiveConfig
from river_gang.config.validation import (
    format_error_for_operator,
    validate_for_dispatch,
)
from river_gang.workflow.loader import load_workflow

logger = logging.getLogger(__name__)


T = TypeVar("T")


class LastKnownGoodHolder(Generic[T]):
    """Atomic single-slot holder for the last successfully-loaded value.

    The held value is treated as immutable (we hand out the reference as-is);
    consumers that captured a prior reference are unaffected by later swaps.
    """

    def __init__(self) -> None:
        self._value: T | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> T | None:
        async with self._lock:
            return self._value

    def peek(self) -> T | None:
        """Synchronous, lock-free read of the current value.

        Safe because writes are serialised through ``await set()`` and a
        single attribute read is atomic under the GIL — consumers that need
        to read the holder from inside a sync callback (timer/dispatcher
        fast paths) avoid the async-lock acquisition this way.
        """
        return self._value

    async def set(self, value: T) -> None:
        async with self._lock:
            self._value = value


# Signature is intentionally loose: ``watchfiles.awatch`` has many optional
# kwargs we don't care about, and tests inject a stub with a different
# ``Change`` enum representation. The runtime contract is just "kwargs in,
# async iterator of changes out".
_AwatchFactory = Callable[..., AsyncIterator[set[tuple[Any, str]]]]


class WorkflowWatcher:
    """Watch a single ``WORKFLOW.md`` file and re-run the config pipeline.

    Args:
        path: absolute or relative path to ``WORKFLOW.md``.
        on_reload: synchronous callback invoked with the new
            :class:`EffectiveConfig` after a successful reload.
        debounce_ms: passed to ``watchfiles.awatch`` as the debounce window.
        awatch_factory: injectable seam for tests; defaults to
            :func:`watchfiles.awatch`.
    """

    def __init__(
        self,
        *,
        path: Path,
        on_reload: Callable[[EffectiveConfig], None],
        debounce_ms: int = 200,
        awatch_factory: _AwatchFactory | None = None,
    ) -> None:
        self._path = path
        self._on_reload = on_reload
        self._debounce_ms = debounce_ms
        self._awatch: _AwatchFactory = awatch_factory or cast(
            _AwatchFactory, _default_awatch
        )
        self._stop_event: asyncio.Event = asyncio.Event()
        self._started_event: asyncio.Event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

        self.last_known_good: LastKnownGoodHolder[EffectiveConfig] = (
            LastKnownGoodHolder()
        )

    @property
    def path(self) -> Path:
        return self._path

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event = asyncio.Event()
        self._started_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="workflow-watcher")
        # Block until the underlying awatch iterator is constructed so callers
        # can rely on "after start() returns, file events are being observed".
        await self._started_event.wait()

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        task = self._task
        self._task = None
        task.cancel()
        # Cancelled task may surface CancelledError or any unhandled error
        # from the watch loop; neither is actionable for the caller of stop().
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _run(self) -> None:
        target = str(self._path)
        try:
            iterator = self._awatch(
                self._path.parent,
                stop_event=self._stop_event,
                debounce=self._debounce_ms,
            )
        except Exception:  # noqa: BLE001 -- surface as crash log, unblock start()
            logger.exception("workflow watcher failed to initialise")
            self._started_event.set()
            return

        self._started_event.set()
        try:
            async for changes in iterator:
                if self._stop_event.is_set():
                    break
                if not any(changed_path == target for _kind, changed_path in changes):
                    continue
                await self._reload_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- log-and-continue is the contract
            logger.exception("workflow watcher loop crashed")

    async def _reload_once(self) -> None:
        try:
            wd = load_workflow(self._path)
            cfg = resolve_and_validate(wd.config, workflow_dir=self._path.parent)
        except Exception as exc:  # noqa: BLE001 -- §6.2 keep last-good
            logger.error(
                "workflow reload failed; keeping last known good config: %s",
                exc,
            )
            return

        # §6.2 dispatch-readiness gate: an empty api_key, missing
        # project_slug, or agent.max_turns=0 must not silently slip into
        # the live config. Re-run the same gate startup uses.
        dispatch_check = validate_for_dispatch(cfg)
        if not dispatch_check.ok:
            logger.error(
                "workflow reload failed dispatch validation; "
                "keeping last known good config:\n%s",
                format_error_for_operator(dispatch_check),
            )
            return

        await self.last_known_good.set(cfg)
        try:
            self._on_reload(cfg)
        except Exception:  # noqa: BLE001
            logger.exception("workflow on_reload callback raised")
