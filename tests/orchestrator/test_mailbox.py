"""Tests for :mod:`river_gang.orchestrator.mailbox` (SPED §7 single-writer)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import assert_type

import pytest

from river_gang.codex import RuntimeEvent
from river_gang.config import EffectiveConfig, apply_defaults
from river_gang.orchestrator import (
    CodexUpdate,
    ConfigReloaded,
    Mailbox,
    OrchestratorMessage,
    PollTick,
    RetryTimerFired,
    Shutdown,
    WorkerExit,
)


def _make_runtime_event(seq: int = 0) -> RuntimeEvent:
    return RuntimeEvent(
        event="agent_message",
        timestamp=datetime.now(UTC),
        codex_app_server_pid=42,
        payload={"seq": seq},
    )


def _make_effective_config() -> EffectiveConfig:
    return apply_defaults({})


# ---------------------------------------------------------------------------
# Message dataclass shapes
# ---------------------------------------------------------------------------


def test_poll_tick_is_frozen() -> None:
    msg = PollTick()
    with pytest.raises(Exception):
        msg.foo = 1  # type: ignore[attr-defined]


def test_shutdown_is_frozen() -> None:
    msg = Shutdown()
    with pytest.raises(Exception):
        msg.foo = 1  # type: ignore[attr-defined]


def test_worker_exit_fields() -> None:
    msg = WorkerExit(
        issue_id="abc",
        reason="normal",
        ok=True,
        runtime_seconds=12.5,
    )
    assert msg.issue_id == "abc"
    assert msg.reason == "normal"
    assert msg.ok is True
    assert msg.runtime_seconds == 12.5
    assert msg.last_error is None


def test_worker_exit_carries_last_error() -> None:
    msg = WorkerExit(
        issue_id="abc",
        reason="stall_detected",
        ok=False,
        runtime_seconds=3.0,
        last_error="no events for 600s",
    )
    assert msg.last_error == "no events for 600s"
    assert msg.ok is False


def test_codex_update_fields() -> None:
    evt = _make_runtime_event()
    msg = CodexUpdate(issue_id="abc", event=evt)
    assert msg.issue_id == "abc"
    assert msg.event is evt


def test_retry_timer_fired_fields() -> None:
    msg = RetryTimerFired(issue_id="abc")
    assert msg.issue_id == "abc"


def test_config_reloaded_fields() -> None:
    cfg = _make_effective_config()
    msg = ConfigReloaded(config=cfg)
    assert msg.config is cfg


def test_messages_are_frozen() -> None:
    msg = WorkerExit(issue_id="x", reason="r", ok=True, runtime_seconds=0.0)
    with pytest.raises(Exception):
        msg.issue_id = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Round-trip per message type
# ---------------------------------------------------------------------------


async def test_round_trip_poll_tick() -> None:
    mb: Mailbox = Mailbox()
    msg = PollTick()
    await mb.send(msg)
    out = await mb.recv()
    assert out is msg


async def test_round_trip_shutdown() -> None:
    mb = Mailbox()
    msg = Shutdown()
    await mb.send(msg)
    assert await mb.recv() is msg


async def test_round_trip_worker_exit() -> None:
    mb = Mailbox()
    msg = WorkerExit(issue_id="abc", reason="normal", ok=True, runtime_seconds=1.0)
    await mb.send(msg)
    assert await mb.recv() is msg


async def test_round_trip_codex_update() -> None:
    mb = Mailbox()
    msg = CodexUpdate(issue_id="abc", event=_make_runtime_event())
    await mb.send(msg)
    assert await mb.recv() is msg


async def test_round_trip_retry_timer_fired() -> None:
    mb = Mailbox()
    msg = RetryTimerFired(issue_id="abc")
    await mb.send(msg)
    assert await mb.recv() is msg


async def test_round_trip_config_reloaded() -> None:
    mb = Mailbox()
    msg = ConfigReloaded(config=_make_effective_config())
    await mb.send(msg)
    assert await mb.recv() is msg


# ---------------------------------------------------------------------------
# FIFO ordering — single producer
# ---------------------------------------------------------------------------


async def test_fifo_ordering_single_producer() -> None:
    mb = Mailbox()
    sent: list[OrchestratorMessage] = [
        WorkerExit(issue_id=f"id-{i}", reason="normal", ok=True, runtime_seconds=float(i))
        for i in range(100)
    ]
    for m in sent:
        await mb.send(m)
    assert mb.qsize() == 100
    received = [await mb.recv() for _ in range(100)]
    assert received == sent
    assert mb.qsize() == 0


# ---------------------------------------------------------------------------
# Concurrency — 100 producers via gather
# ---------------------------------------------------------------------------


async def test_hundred_producers_no_message_lost() -> None:
    mb = Mailbox()
    n_producers = 100

    async def produce(seq: int) -> None:
        await mb.send(
            WorkerExit(
                issue_id=f"prod-{seq}",
                reason="normal",
                ok=True,
                runtime_seconds=0.0,
            )
        )

    await asyncio.gather(*(produce(i) for i in range(n_producers)))
    assert mb.qsize() == n_producers

    received: list[OrchestratorMessage] = [await mb.recv() for _ in range(n_producers)]
    assert {
        m.issue_id for m in received if isinstance(m, WorkerExit)
    } == {f"prod-{i}" for i in range(n_producers)}


async def test_per_producer_ordering_preserved() -> None:
    """Each producer sends 5 sequential messages with monotonic seq IDs.

    Per-producer ordering must hold: dequeued WorkerExit.runtime_seconds
    values for a given prefix must appear in ascending order.
    """
    mb = Mailbox()
    n_producers = 20
    per_producer = 5

    async def produce(prefix: int) -> None:
        for i in range(per_producer):
            await mb.send(
                WorkerExit(
                    issue_id=f"p{prefix}",
                    reason="normal",
                    ok=True,
                    runtime_seconds=float(i),
                )
            )

    await asyncio.gather(*(produce(p) for p in range(n_producers)))

    received: list[WorkerExit] = []
    for _ in range(n_producers * per_producer):
        msg = await mb.recv()
        assert isinstance(msg, WorkerExit)
        received.append(msg)

    by_prefix: dict[str, list[float]] = {}
    for m in received:
        by_prefix.setdefault(m.issue_id, []).append(m.runtime_seconds)
    assert len(by_prefix) == n_producers
    for prefix, runtimes in by_prefix.items():
        assert runtimes == [float(i) for i in range(per_producer)], prefix


# ---------------------------------------------------------------------------
# Discriminated dispatch — match/case post-dequeue
# ---------------------------------------------------------------------------


async def test_match_dispatch_after_dequeue() -> None:
    """match/case narrows OrchestratorMessage to its variant."""
    mb = Mailbox()
    await mb.send(PollTick())
    await mb.send(WorkerExit(issue_id="x", reason="r", ok=True, runtime_seconds=1.0))
    await mb.send(CodexUpdate(issue_id="x", event=_make_runtime_event()))
    await mb.send(RetryTimerFired(issue_id="x"))
    await mb.send(ConfigReloaded(config=_make_effective_config()))
    await mb.send(Shutdown())

    seen: list[str] = []
    for _ in range(6):
        msg = await mb.recv()
        match msg:
            case PollTick():
                seen.append("poll")
            case WorkerExit(issue_id=iid, ok=ok):
                assert iid == "x"
                assert ok is True
                seen.append("exit")
            case CodexUpdate(issue_id=iid, event=evt):
                assert iid == "x"
                assert_type(evt, RuntimeEvent)
                seen.append("codex")
            case RetryTimerFired(issue_id=iid):
                assert iid == "x"
                seen.append("retry")
            case ConfigReloaded(config=cfg):
                assert isinstance(cfg, EffectiveConfig)
                seen.append("config")
            case Shutdown():
                seen.append("shutdown")
    assert seen == ["poll", "exit", "codex", "retry", "config", "shutdown"]


# ---------------------------------------------------------------------------
# recv blocks until send
# ---------------------------------------------------------------------------


async def test_recv_blocks_on_empty_queue() -> None:
    mb = Mailbox()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(mb.recv(), timeout=0.05)


async def test_recv_resumes_after_send() -> None:
    mb = Mailbox()
    sent = PollTick()

    async def delayed_send() -> None:
        await asyncio.sleep(0.01)
        await mb.send(sent)

    received, _ = await asyncio.gather(mb.recv(), delayed_send())
    assert received is sent
