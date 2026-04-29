"""Tests for :mod:`river_gang.orchestrator.retry` (SPED §8.4)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from river_gang.orchestrator import (
    CONTINUATION_DELAY_MS,
    RetryEntry,
    RetryQueue,
    compute_backoff_ms,
)

# ---------------------------------------------------------------------------
# compute_backoff_ms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [
        (1, 10_000),
        (2, 20_000),
        (3, 40_000),
        (4, 80_000),
        (5, 160_000),
    ],
)
def test_backoff_uncapped(attempt: int, expected: int) -> None:
    assert compute_backoff_ms(attempt, max_cap_ms=300_000) == expected


def test_backoff_capped_at_max() -> None:
    # 10000 * 2^9 = 5_120_000; cap clamps it.
    assert compute_backoff_ms(10, max_cap_ms=300_000) == 300_000


def test_backoff_cap_smaller_than_first() -> None:
    """Tiny caps clamp from attempt=1 onwards."""
    assert compute_backoff_ms(1, max_cap_ms=5_000) == 5_000
    assert compute_backoff_ms(5, max_cap_ms=5_000) == 5_000


def test_backoff_attempt_zero_raises() -> None:
    with pytest.raises(ValueError):
        compute_backoff_ms(0, max_cap_ms=300_000)


def test_backoff_attempt_negative_raises() -> None:
    with pytest.raises(ValueError):
        compute_backoff_ms(-1, max_cap_ms=300_000)


# ---------------------------------------------------------------------------
# RetryEntry shape
# ---------------------------------------------------------------------------


def test_retry_entry_is_frozen() -> None:
    entry = RetryEntry(
        issue_id="x",
        attempt=1,
        kind="failure",
        scheduled_at=datetime.now(UTC),
        fire_at=datetime.now(UTC),
        timer_handle=None,
        last_error=None,
    )
    with pytest.raises(Exception):
        entry.attempt = 2  # type: ignore[misc]


def test_retry_entry_holds_last_error() -> None:
    entry = RetryEntry(
        issue_id="x",
        attempt=2,
        kind="failure",
        scheduled_at=datetime.now(UTC),
        fire_at=datetime.now(UTC),
        timer_handle=None,
        last_error="boom",
    )
    assert entry.last_error == "boom"


# ---------------------------------------------------------------------------
# RetryQueue.schedule — delay computation per kind
# ---------------------------------------------------------------------------


async def test_schedule_continuation_uses_fixed_1000ms_regardless_of_attempt() -> None:
    loop = asyncio.get_running_loop()
    fake_loop = MagicMock(spec=asyncio.AbstractEventLoop)
    fake_loop.time.return_value = loop.time()
    fake_loop.call_later.return_value = MagicMock(spec=asyncio.TimerHandle)

    rq = RetryQueue(loop=fake_loop)
    on_fire = MagicMock()
    rq.schedule(
        issue_id="x",
        attempt=5,
        kind="continuation",
        max_cap_ms=300_000,
        on_fire=on_fire,
    )
    fake_loop.call_later.assert_called_once()
    delay_arg = fake_loop.call_later.call_args.args[0]
    assert delay_arg == pytest.approx(CONTINUATION_DELAY_MS / 1000.0)


async def test_schedule_failure_uses_backoff() -> None:
    loop = asyncio.get_running_loop()
    fake_loop = MagicMock(spec=asyncio.AbstractEventLoop)
    fake_loop.time.return_value = loop.time()
    fake_loop.call_later.return_value = MagicMock(spec=asyncio.TimerHandle)

    rq = RetryQueue(loop=fake_loop)
    rq.schedule(
        issue_id="x",
        attempt=2,
        kind="failure",
        max_cap_ms=300_000,
        on_fire=lambda _id: None,
    )
    delay_arg = fake_loop.call_later.call_args.args[0]
    # 10000 * 2^(2-1) = 20_000ms = 20s.
    assert delay_arg == pytest.approx(20.0)


async def test_schedule_returns_entry_with_expected_fields() -> None:
    loop = asyncio.get_running_loop()
    fake_loop = MagicMock(spec=asyncio.AbstractEventLoop)
    fake_loop.time.return_value = loop.time()
    fake_loop.call_later.return_value = MagicMock(spec=asyncio.TimerHandle)

    rq = RetryQueue(loop=fake_loop)
    entry = rq.schedule(
        issue_id="x",
        attempt=3,
        kind="failure",
        max_cap_ms=300_000,
        on_fire=lambda _id: None,
        last_error="prev failure",
    )
    assert entry.issue_id == "x"
    assert entry.attempt == 3
    assert entry.kind == "failure"
    assert entry.last_error == "prev failure"
    assert entry.scheduled_at <= entry.fire_at
    assert entry.timer_handle is fake_loop.call_later.return_value
    # 40_000ms after scheduled.
    delta = entry.fire_at - entry.scheduled_at
    assert delta.total_seconds() == pytest.approx(40.0)


async def test_schedule_inserts_into_queue() -> None:
    loop = asyncio.get_running_loop()
    fake_loop = MagicMock(spec=asyncio.AbstractEventLoop)
    fake_loop.time.return_value = loop.time()
    fake_loop.call_later.return_value = MagicMock(spec=asyncio.TimerHandle)

    rq = RetryQueue(loop=fake_loop)
    assert "x" not in rq
    entry = rq.schedule(
        issue_id="x",
        attempt=1,
        kind="failure",
        max_cap_ms=300_000,
        on_fire=lambda _id: None,
    )
    assert "x" in rq
    assert len(rq) == 1
    assert rq.get("x") is entry


# ---------------------------------------------------------------------------
# Re-scheduling supersedes existing timer
# ---------------------------------------------------------------------------


async def test_reschedule_cancels_old_timer() -> None:
    loop = asyncio.get_running_loop()
    fake_loop = MagicMock(spec=asyncio.AbstractEventLoop)
    fake_loop.time.return_value = loop.time()
    handle1 = MagicMock(spec=asyncio.TimerHandle)
    handle2 = MagicMock(spec=asyncio.TimerHandle)
    fake_loop.call_later.side_effect = [handle1, handle2]

    rq = RetryQueue(loop=fake_loop)
    rq.schedule(
        issue_id="x",
        attempt=1,
        kind="failure",
        max_cap_ms=300_000,
        on_fire=lambda _id: None,
    )
    rq.schedule(
        issue_id="x",
        attempt=2,
        kind="failure",
        max_cap_ms=300_000,
        on_fire=lambda _id: None,
    )
    handle1.cancel.assert_called_once()
    handle2.cancel.assert_not_called()
    assert len(rq) == 1
    assert rq.get("x") is not None
    assert rq.get("x").timer_handle is handle2  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# cancel / cancel_all / pop
# ---------------------------------------------------------------------------


async def test_cancel_removes_entry_and_cancels_timer() -> None:
    loop = asyncio.get_running_loop()
    fake_loop = MagicMock(spec=asyncio.AbstractEventLoop)
    fake_loop.time.return_value = loop.time()
    handle = MagicMock(spec=asyncio.TimerHandle)
    fake_loop.call_later.return_value = handle

    rq = RetryQueue(loop=fake_loop)
    rq.schedule(
        issue_id="x",
        attempt=1,
        kind="failure",
        max_cap_ms=300_000,
        on_fire=lambda _id: None,
    )
    removed = rq.cancel("x")
    assert removed is not None
    assert removed.issue_id == "x"
    handle.cancel.assert_called_once()
    assert "x" not in rq
    assert len(rq) == 0


async def test_cancel_missing_returns_none() -> None:
    fake_loop = MagicMock(spec=asyncio.AbstractEventLoop)
    rq = RetryQueue(loop=fake_loop)
    assert rq.cancel("missing") is None


async def test_cancel_all_clears_all_entries() -> None:
    fake_loop = MagicMock(spec=asyncio.AbstractEventLoop)
    fake_loop.time.return_value = 0.0
    handles = [MagicMock(spec=asyncio.TimerHandle) for _ in range(3)]
    fake_loop.call_later.side_effect = handles

    rq = RetryQueue(loop=fake_loop)
    for i, iid in enumerate(("a", "b", "c"), start=1):
        rq.schedule(
            issue_id=iid,
            attempt=i,
            kind="failure",
            max_cap_ms=300_000,
            on_fire=lambda _id: None,
        )
    assert len(rq) == 3
    rq.cancel_all()
    for h in handles:
        h.cancel.assert_called_once()
    assert len(rq) == 0


async def test_pop_returns_entry_without_canceling_timer() -> None:
    fake_loop = MagicMock(spec=asyncio.AbstractEventLoop)
    fake_loop.time.return_value = 0.0
    handle = MagicMock(spec=asyncio.TimerHandle)
    fake_loop.call_later.return_value = handle

    rq = RetryQueue(loop=fake_loop)
    rq.schedule(
        issue_id="x",
        attempt=1,
        kind="failure",
        max_cap_ms=300_000,
        on_fire=lambda _id: None,
    )
    popped = rq.pop("x")
    assert popped is not None
    assert popped.issue_id == "x"
    handle.cancel.assert_not_called()
    assert "x" not in rq


async def test_pop_missing_returns_none() -> None:
    fake_loop = MagicMock(spec=asyncio.AbstractEventLoop)
    rq = RetryQueue(loop=fake_loop)
    assert rq.pop("missing") is None


# ---------------------------------------------------------------------------
# Real-loop integration — on_fire actually invoked
# ---------------------------------------------------------------------------


async def test_real_loop_fires_callback_after_delay() -> None:
    """Schedule with the real running loop and a continuation kind (1000ms),
    but override delay via a near-zero failure max_cap so the test is fast.

    We use ``kind="failure"`` with ``max_cap_ms=50`` so backoff clamps to 50ms.
    """
    loop = asyncio.get_running_loop()
    rq = RetryQueue(loop=loop)
    fired: list[str] = []

    def on_fire(issue_id: str) -> None:
        fired.append(issue_id)

    rq.schedule(
        issue_id="x",
        attempt=5,
        kind="failure",
        max_cap_ms=50,  # 50ms cap → fires fast
        on_fire=on_fire,
    )
    await asyncio.sleep(0.15)
    assert fired == ["x"]


async def test_canceled_timer_does_not_fire() -> None:
    loop = asyncio.get_running_loop()
    rq = RetryQueue(loop=loop)
    fired: list[str] = []

    rq.schedule(
        issue_id="x",
        attempt=5,
        kind="failure",
        max_cap_ms=50,
        on_fire=fired.append,
    )
    rq.cancel("x")
    await asyncio.sleep(0.15)
    assert fired == []
