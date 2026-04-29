"""Tests for :mod:`river_gang.workflow.watcher` (SPED §6.2 dynamic reload)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from river_gang.config.schema import EffectiveConfig
from river_gang.workflow.watcher import (
    LastKnownGoodHolder,
    WorkflowWatcher,
)

_VALID_WORKFLOW = """---
tracker:
  kind: linear
  api_key: lit_secret
  project_slug: river-gang
codex:
  command: codex app-server
---
prompt body
"""

_INVALID_WORKFLOW = """---
tracker:
  kind: linear
  api_key: [unterminated
---
"""

# Resolves successfully but fails ``validate_for_dispatch`` — empty
# api_key is the canonical "looks valid, dispatches to nothing" trap.
_DISPATCH_INVALID_WORKFLOW = """---
tracker:
  kind: linear
  api_key: ""
  project_slug: river-gang
codex:
  command: codex app-server
---
prompt body
"""


# ---------------------------------------------------------------------------
# LastKnownGoodHolder
# ---------------------------------------------------------------------------


async def test_holder_starts_empty() -> None:
    holder: LastKnownGoodHolder[int] = LastKnownGoodHolder()
    assert await holder.get() is None


async def test_holder_set_then_get_returns_same_value() -> None:
    holder: LastKnownGoodHolder[str] = LastKnownGoodHolder()
    await holder.set("a")
    assert await holder.get() == "a"


async def test_holder_swap_does_not_mutate_prior_reference() -> None:
    """Workers in flight captured the old reference — swap MUST NOT touch it."""
    holder: LastKnownGoodHolder[dict[str, int]] = LastKnownGoodHolder()
    first = {"v": 1}
    second = {"v": 2}
    await holder.set(first)
    captured = await holder.get()
    assert captured is first

    await holder.set(second)
    # captured is still the original object, untouched
    assert captured is first
    assert captured == {"v": 1}
    assert await holder.get() is second


async def test_holder_concurrent_writes_serialised() -> None:
    """``asyncio.Lock`` serialises swaps so the final read is deterministic."""
    holder: LastKnownGoodHolder[int] = LastKnownGoodHolder()

    async def writer(n: int) -> None:
        await holder.set(n)

    await asyncio.gather(*(writer(i) for i in range(50)))
    final = await holder.get()
    assert final in range(50)


# ---------------------------------------------------------------------------
# WorkflowWatcher — using injected fake awatch
# ---------------------------------------------------------------------------


def _write_workflow(path: Path, body: str) -> None:
    path.write_text(body)


class _FakeAwatch:
    """Async generator factory that yields pre-recorded change batches.

    Each call returns an async iterator that:
        1. yields each pre-recorded batch in order,
        2. then waits on ``stop_event`` until it fires,
        3. exits cleanly.
    """

    def __init__(self, batches: list[set[tuple[int, str]]]) -> None:
        self._batches = batches
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        *paths: Path | str,
        stop_event: asyncio.Event | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[set[tuple[int, str]]]:
        self.calls.append({"paths": paths, "kwargs": kwargs})

        async def gen() -> AsyncIterator[set[tuple[int, str]]]:
            for batch in self._batches:
                yield batch
                # tiny yield so the watcher loop can run callbacks
                await asyncio.sleep(0)
            if stop_event is not None:
                await stop_event.wait()

        return gen()


async def test_watcher_invokes_callback_on_change(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    _write_workflow(workflow_path, _VALID_WORKFLOW)

    received: list[EffectiveConfig] = []

    def on_reload(cfg: EffectiveConfig) -> None:
        received.append(cfg)

    fake = _FakeAwatch([{(1, str(workflow_path))}])
    watcher = WorkflowWatcher(
        path=workflow_path,
        on_reload=on_reload,
        awatch_factory=fake,
    )

    async with asyncio.timeout(0.5):
        await watcher.start()
        # wait until callback fires or timeout
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.01)
        await watcher.stop()

    assert len(received) == 1
    assert received[0].tracker.kind == "linear"
    assert received[0].tracker.api_key == "lit_secret"


async def test_watcher_invalid_reload_logs_but_keeps_last_good(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    _write_workflow(workflow_path, _VALID_WORKFLOW)

    received: list[EffectiveConfig] = []

    def on_reload(cfg: EffectiveConfig) -> None:
        received.append(cfg)

    # Two batches: first delivers the valid file, then we corrupt and trigger
    # a second batch that must NOT crash the watcher and MUST log an error.
    batches = [
        {(1, str(workflow_path))},
        {(2, str(workflow_path))},
    ]
    fake = _FakeAwatch(batches)
    watcher = WorkflowWatcher(
        path=workflow_path,
        on_reload=on_reload,
        awatch_factory=fake,
    )

    caplog.set_level(logging.ERROR, logger="river_gang.workflow.watcher")

    async with asyncio.timeout(1.0):
        await watcher.start()
        # let first batch flow (success)
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.01)
        # corrupt the file before the second batch is consumed
        _write_workflow(workflow_path, _INVALID_WORKFLOW)
        # second batch needs time to be drained from the fake generator;
        # the generator yields immediately but consumer task needs a turn
        for _ in range(100):
            if any("workflow reload failed" in r.message.lower() for r in caplog.records):
                break
            await asyncio.sleep(0.01)
        await watcher.stop()

    assert len(received) == 1, "second (invalid) reload must NOT call on_reload"
    assert any(
        r.levelno >= logging.ERROR and "workflow reload failed" in r.message.lower()
        for r in caplog.records
    )
    # holder still exposes the last good config
    last_good = await watcher.last_known_good.get()
    assert last_good is not None
    assert last_good.tracker.api_key == "lit_secret"


async def test_watcher_dispatch_invalid_reload_logs_but_keeps_last_good(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Reloads that resolve cleanly but fail ``validate_for_dispatch``
    (empty api_key, dropped project_slug, agent.max_turns=0, ...) MUST
    NOT swap ``last_known_good``. Otherwise the orchestrator silently
    keeps dispatching against a degraded config.
    """
    workflow_path = tmp_path / "WORKFLOW.md"
    _write_workflow(workflow_path, _VALID_WORKFLOW)

    received: list[EffectiveConfig] = []

    def on_reload(cfg: EffectiveConfig) -> None:
        received.append(cfg)

    batches = [
        {(1, str(workflow_path))},
        {(2, str(workflow_path))},
    ]
    fake = _FakeAwatch(batches)
    watcher = WorkflowWatcher(
        path=workflow_path,
        on_reload=on_reload,
        awatch_factory=fake,
    )

    caplog.set_level(logging.ERROR, logger="river_gang.workflow.watcher")

    async with asyncio.timeout(1.0):
        await watcher.start()
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.01)
        # Swap to a workflow that resolves cleanly but FAILS dispatch validation.
        _write_workflow(workflow_path, _DISPATCH_INVALID_WORKFLOW)
        for _ in range(100):
            if any("dispatch validation" in r.message.lower() for r in caplog.records):
                break
            await asyncio.sleep(0.01)
        await watcher.stop()

    assert len(received) == 1, (
        "second (dispatch-invalid) reload must NOT call on_reload"
    )
    assert any(
        r.levelno >= logging.ERROR and "dispatch validation" in r.message.lower()
        for r in caplog.records
    )
    last_good = await watcher.last_known_good.get()
    assert last_good is not None
    assert last_good.tracker.api_key == "lit_secret", (
        "last_known_good must keep the original config when dispatch validation fails"
    )


async def test_watcher_stop_returns_cleanly_with_no_changes(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    _write_workflow(workflow_path, _VALID_WORKFLOW)

    fake = _FakeAwatch([])  # no events at all
    watcher = WorkflowWatcher(
        path=workflow_path,
        on_reload=lambda _cfg: None,
        awatch_factory=fake,
    )

    await watcher.start()
    # idempotent stop
    await watcher.stop()
    await watcher.stop()


async def test_watcher_debounce_param_passed_to_awatch(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    _write_workflow(workflow_path, _VALID_WORKFLOW)

    fake = _FakeAwatch([])
    watcher = WorkflowWatcher(
        path=workflow_path,
        on_reload=lambda _cfg: None,
        awatch_factory=fake,
        debounce_ms=200,
    )
    await watcher.start()
    await watcher.stop()

    assert fake.calls, "awatch must have been invoked at least once"
    assert fake.calls[0]["kwargs"].get("debounce") == 200


async def test_watcher_coalesces_rapid_changes_within_one_batch(
    tmp_path: Path,
) -> None:
    """Multiple changes inside one debounced batch produce ONE callback."""
    workflow_path = tmp_path / "WORKFLOW.md"
    _write_workflow(workflow_path, _VALID_WORKFLOW)

    received: list[EffectiveConfig] = []

    def on_reload(cfg: EffectiveConfig) -> None:
        received.append(cfg)

    # one batch with many events for the same path — must collapse to 1 reload
    fake = _FakeAwatch(
        [
            {
                (1, str(workflow_path)),
                (2, str(workflow_path)),
                (3, str(workflow_path)),
            }
        ]
    )
    watcher = WorkflowWatcher(
        path=workflow_path,
        on_reload=on_reload,
        awatch_factory=fake,
    )

    async with asyncio.timeout(0.5):
        await watcher.start()
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.01)
        await watcher.stop()

    assert len(received) == 1


async def test_watcher_ignores_changes_for_other_paths(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    _write_workflow(workflow_path, _VALID_WORKFLOW)

    received: list[EffectiveConfig] = []

    def on_reload(cfg: EffectiveConfig) -> None:
        received.append(cfg)

    other = tmp_path / "OTHER.md"
    fake = _FakeAwatch([{(1, str(other))}])
    watcher = WorkflowWatcher(
        path=workflow_path,
        on_reload=on_reload,
        awatch_factory=fake,
    )
    async with asyncio.timeout(0.3):
        await watcher.start()
        await asyncio.sleep(0.05)
        await watcher.stop()

    assert received == []


async def test_watcher_in_flight_reference_unaffected_by_reload(
    tmp_path: Path,
) -> None:
    """SPED §6.2: reload MUST NOT restart in-flight workers.

    We model an in-flight worker as code that captured the previous holder
    reference. A subsequent reload-driven swap must not mutate that reference.
    """
    workflow_path = tmp_path / "WORKFLOW.md"
    _write_workflow(workflow_path, _VALID_WORKFLOW)

    received: list[EffectiveConfig] = []

    def on_reload(cfg: EffectiveConfig) -> None:
        received.append(cfg)

    fake = _FakeAwatch(
        [
            {(1, str(workflow_path))},
            {(2, str(workflow_path))},
        ]
    )
    watcher = WorkflowWatcher(
        path=workflow_path,
        on_reload=on_reload,
        awatch_factory=fake,
    )

    async with asyncio.timeout(1.0):
        await watcher.start()
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.01)
        # capture a reference exactly like a worker would
        in_flight_snapshot = received[0]
        # rewrite WORKFLOW.md with different api_key, then let next batch drive
        _write_workflow(
            workflow_path,
            _VALID_WORKFLOW.replace("lit_secret", "rotated_secret"),
        )
        for _ in range(100):
            if len(received) >= 2:
                break
            await asyncio.sleep(0.01)
        await watcher.stop()

    assert len(received) == 2
    assert received[0] is in_flight_snapshot
    # original snapshot still has the old api_key (frozen dataclass — safe)
    assert in_flight_snapshot.tracker.api_key == "lit_secret"
    assert received[1].tracker.api_key == "rotated_secret"


# ---------------------------------------------------------------------------
# Real watchfiles integration (≤500ms callback latency)
# ---------------------------------------------------------------------------


async def _probe_real_awatch_works(tmp_path: Path) -> bool:
    """Sandboxed CI runners may block ``watchfiles``' Rust notify backend
    *and* polling. Quickly probe and skip the integration test if so."""
    from watchfiles import awatch

    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    probe_file = probe_dir / "x"
    probe_file.write_text("0")

    async def trigger() -> None:
        await asyncio.sleep(0.05)
        probe_file.write_text("1")

    async def listen() -> bool:
        async for _changes in awatch(probe_dir, debounce=20):
            return True
        return False

    try:
        async with asyncio.timeout(0.6):
            done, _pending = await asyncio.wait(
                {asyncio.create_task(listen()), asyncio.create_task(trigger())},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in done:
                result = t.result()
                if isinstance(result, bool) and result:
                    return True
    except TimeoutError:
        return False
    return False


async def test_watcher_real_awatch_callback_within_500ms(tmp_path: Path) -> None:
    if not await _probe_real_awatch_works(tmp_path):
        pytest.skip(
            "watchfiles cannot observe FS events in this sandbox — "
            "integration coverage falls back to the injected-awatch tests"
        )

    workflow_path = tmp_path / "WORKFLOW.md"
    _write_workflow(workflow_path, _VALID_WORKFLOW)

    received: list[EffectiveConfig] = []
    event = asyncio.Event()

    def on_reload(cfg: EffectiveConfig) -> None:
        received.append(cfg)
        event.set()

    watcher = WorkflowWatcher(
        path=workflow_path,
        on_reload=on_reload,
        debounce_ms=50,
    )
    await watcher.start()
    try:
        await asyncio.sleep(0.05)
        workflow_path.write_text(
            _VALID_WORKFLOW.replace("lit_secret", "fresh_secret")
        )
        async with asyncio.timeout(0.5):
            await event.wait()
    finally:
        await watcher.stop()

    assert received
    assert received[-1].tracker.api_key == "fresh_secret"
