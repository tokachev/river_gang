"""Tests for :func:`should_terminate_for_stall` (SPED §16.5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from river_gang.codex.stall import should_terminate_for_stall


def _at(seconds: float) -> datetime:
    """Build a UTC datetime ``seconds`` after a fixed reference instant."""
    return datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# Below threshold → False
# ---------------------------------------------------------------------------


def test_below_threshold_with_last_event() -> None:
    started = _at(0)
    last_event = _at(5)
    now = _at(8)  # 3000ms since last event, below 5000ms timeout
    assert should_terminate_for_stall(
        now=now, last_event_at=last_event, started_at=started,
        stall_timeout_ms=5000,
    ) is False


def test_below_threshold_using_started_at_when_no_event() -> None:
    started = _at(0)
    now = _at(2)  # 2000ms since started, below 5000ms
    assert should_terminate_for_stall(
        now=now, last_event_at=None, started_at=started,
        stall_timeout_ms=5000,
    ) is False


# ---------------------------------------------------------------------------
# Boundary: elapsed exactly equal → True
# ---------------------------------------------------------------------------


def test_exactly_at_threshold_returns_true() -> None:
    """``elapsed >= timeout`` — equality terminates."""
    started = _at(0)
    last_event = _at(0)
    now = _at(5)  # exactly 5000ms
    assert should_terminate_for_stall(
        now=now, last_event_at=last_event, started_at=started,
        stall_timeout_ms=5000,
    ) is True


def test_exactly_at_threshold_using_started_at() -> None:
    started = _at(0)
    now = _at(5)
    assert should_terminate_for_stall(
        now=now, last_event_at=None, started_at=started,
        stall_timeout_ms=5000,
    ) is True


# ---------------------------------------------------------------------------
# Above threshold → True
# ---------------------------------------------------------------------------


def test_above_threshold_with_last_event() -> None:
    started = _at(0)
    last_event = _at(10)
    now = _at(20)  # 10000ms > 5000ms
    assert should_terminate_for_stall(
        now=now, last_event_at=last_event, started_at=started,
        stall_timeout_ms=5000,
    ) is True


def test_above_threshold_with_no_last_event() -> None:
    started = _at(0)
    now = _at(60)  # 60000ms since start, way over 5000ms
    assert should_terminate_for_stall(
        now=now, last_event_at=None, started_at=started,
        stall_timeout_ms=5000,
    ) is True


# ---------------------------------------------------------------------------
# Disabled: stall_timeout_ms == 0 or negative
# ---------------------------------------------------------------------------


def test_zero_timeout_disables_stall_detection() -> None:
    """``stall_timeout_ms=0`` is the documented "disabled" sentinel."""
    started = _at(0)
    now = _at(3600)  # one hour later — would normally be a clear stall
    assert should_terminate_for_stall(
        now=now, last_event_at=None, started_at=started,
        stall_timeout_ms=0,
    ) is False


@pytest.mark.parametrize("bad", [-1, -1000, -300_000])
def test_negative_timeout_disables_stall_detection(bad: int) -> None:
    """Negative timeouts treated as disabled, defensive against config typos."""
    started = _at(0)
    now = _at(60)
    assert should_terminate_for_stall(
        now=now, last_event_at=None, started_at=started,
        stall_timeout_ms=bad,
    ) is False


def test_zero_timeout_with_last_event_still_disabled() -> None:
    started = _at(0)
    last_event = _at(10)
    now = _at(3600)
    assert should_terminate_for_stall(
        now=now, last_event_at=last_event, started_at=started,
        stall_timeout_ms=0,
    ) is False


# ---------------------------------------------------------------------------
# Last-event preferred over started-at
# ---------------------------------------------------------------------------


def test_last_event_resets_the_clock() -> None:
    """Started 1h ago but last event 1s ago → not stalled."""
    started = _at(0)
    last_event = _at(3599)
    now = _at(3600)  # 1 second after the latest event
    assert should_terminate_for_stall(
        now=now, last_event_at=last_event, started_at=started,
        stall_timeout_ms=5000,
    ) is False


def test_last_event_in_future_is_not_stalled() -> None:
    """Defensive: clock skew that puts last_event slightly after now should
    NOT spuriously trigger termination — the elapsed delta clamps at zero."""
    started = _at(0)
    last_event = _at(11)
    now = _at(10)  # last event is "after" now (1s of skew)
    assert should_terminate_for_stall(
        now=now, last_event_at=last_event, started_at=started,
        stall_timeout_ms=5000,
    ) is False


# ---------------------------------------------------------------------------
# Sub-second resolution
# ---------------------------------------------------------------------------


def test_sub_second_threshold() -> None:
    started = _at(0)
    now = started + timedelta(milliseconds=499)
    assert should_terminate_for_stall(
        now=now, last_event_at=None, started_at=started,
        stall_timeout_ms=500,
    ) is False
    now2 = started + timedelta(milliseconds=500)
    assert should_terminate_for_stall(
        now=now2, last_event_at=None, started_at=started,
        stall_timeout_ms=500,
    ) is True


# ---------------------------------------------------------------------------
# Timezone handling
# ---------------------------------------------------------------------------


def test_utc_aware_inputs_are_accepted() -> None:
    started = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    now = datetime(2026, 4, 1, 12, 0, 10, tzinfo=UTC)
    assert should_terminate_for_stall(
        now=now, last_event_at=None, started_at=started,
        stall_timeout_ms=5000,
    ) is True


def test_mixed_timezones_compared_correctly() -> None:
    """Aware-aware comparison across different offsets MUST work — both
    sides normalise to UTC under the hood (datetime arithmetic does this)."""
    eastern = timezone(timedelta(hours=-5))
    started = datetime(2026, 4, 1, 7, 0, 0, tzinfo=eastern)  # 12:00 UTC
    now = datetime(2026, 4, 1, 12, 0, 10, tzinfo=UTC)
    assert should_terminate_for_stall(
        now=now, last_event_at=None, started_at=started,
        stall_timeout_ms=5000,
    ) is True


def test_naive_now_raises_type_error() -> None:
    """Mixing naive + aware datetimes is undefined — raise loudly so a bug
    in the caller doesn't silently produce wrong stall decisions."""
    started = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    now = datetime(2026, 4, 1, 12, 0, 10)  # naive
    with pytest.raises(TypeError):
        should_terminate_for_stall(
            now=now, last_event_at=None, started_at=started,
            stall_timeout_ms=5000,
        )


def test_naive_started_at_raises_type_error() -> None:
    started = datetime(2026, 4, 1, 12, 0, 0)  # naive
    now = datetime(2026, 4, 1, 12, 0, 10, tzinfo=UTC)
    with pytest.raises(TypeError):
        should_terminate_for_stall(
            now=now, last_event_at=None, started_at=started,
            stall_timeout_ms=5000,
        )


def test_naive_last_event_at_raises_type_error() -> None:
    started = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    last_event = datetime(2026, 4, 1, 12, 0, 5)  # naive
    now = datetime(2026, 4, 1, 12, 0, 10, tzinfo=UTC)
    with pytest.raises(TypeError):
        should_terminate_for_stall(
            now=now, last_event_at=last_event, started_at=started,
            stall_timeout_ms=5000,
        )


# ---------------------------------------------------------------------------
# Re-export sanity
# ---------------------------------------------------------------------------


def test_function_is_importable_from_package_root() -> None:
    from river_gang.codex import should_terminate_for_stall as exported

    assert exported is should_terminate_for_stall
