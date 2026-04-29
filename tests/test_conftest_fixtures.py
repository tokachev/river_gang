"""Self-tests for shared fixtures in :mod:`tests.conftest`."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from tests.conftest import FrozenClock


def test_frozen_clock_tick_advances_seconds() -> None:
    clock = FrozenClock(start=datetime(2026, 1, 1, tzinfo=UTC))
    initial = clock.now()
    after = clock.tick(seconds=5)
    assert (after - initial).total_seconds() == 5.0
    assert clock.now() == after


def test_frozen_clock_tick_zero_is_noop() -> None:
    clock = FrozenClock()
    before = clock.now()
    after = clock.tick()
    assert before == after


def test_tmp_workspace_root_fixture_creates_dir(tmp_workspace_root: Path) -> None:
    assert tmp_workspace_root.is_dir()
    assert tmp_workspace_root.name == "workspaces"
