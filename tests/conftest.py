"""Shared pytest fixtures for river_gang tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture()
def tmp_workspace_root(tmp_path: Path) -> Path:
    """Provide an isolated workspace root directory under ``tmp_path``."""

    root = tmp_path / "workspaces"
    root.mkdir(parents=True, exist_ok=True)
    return root


class FrozenClock:
    """Monotonic-ish frozen clock used by tests that need deterministic time.

    Tests advance time explicitly via :meth:`tick`. The clock is intentionally
    minimal — production code receives a callable, not this class.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def tick(self, *, seconds: float = 0.0) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now


@pytest.fixture()
def frozen_clock() -> Iterator[FrozenClock]:
    """Deterministic clock fixture; tests inject ``clock.now`` into code under test."""

    yield FrozenClock()
