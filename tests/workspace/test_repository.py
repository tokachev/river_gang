"""Repository-backed workspace tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from river_gang.config.defaults import apply_defaults
from river_gang.config.schema import EffectiveConfig, WorkspaceConfig
from river_gang.workspace.manager import WorkspaceManager, WorkspaceRepositoryFailed


def _make_config(*, workspace_root: Path, repository: str | None) -> EffectiveConfig:
    base = apply_defaults({})
    return EffectiveConfig(
        tracker=base.tracker,
        polling=base.polling,
        workspace=WorkspaceConfig(root=str(workspace_root), repository=repository),
        hooks=base.hooks,
        agent=base.agent,
        codex=base.codex,
    )


class FakeRepositoryPopulator:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, Path]] = []

    async def __call__(self, repository: str, destination: Path) -> None:
        self.calls.append((repository, destination))
        if self.fail:
            raise RuntimeError("clone failed")
        destination.mkdir(parents=True)
        (destination / ".git").mkdir()
        (destination / "README.md").write_text("repo contents\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_ensure_populates_new_workspace_from_configured_repository(tmp_path: Path) -> None:
    populator = FakeRepositoryPopulator()
    cfg = _make_config(
        workspace_root=tmp_path,
        repository="https://github.com/tokachev/river_gang.git",
    )
    mgr = WorkspaceManager(config=cfg, repository_populator=populator)

    result = await mgr.ensure_for_issue("RG-123")

    assert result.created_now is True
    assert result.path == tmp_path / "RG-123"
    assert populator.calls == [
        ("https://github.com/tokachev/river_gang.git", tmp_path / "RG-123")
    ]
    assert (result.path / ".git").is_dir()


@pytest.mark.asyncio
async def test_ensure_reuses_existing_repository_workspace_without_repopulating(
    tmp_path: Path,
) -> None:
    populator = FakeRepositoryPopulator()
    cfg = _make_config(
        workspace_root=tmp_path,
        repository="https://github.com/tokachev/river_gang.git",
    )
    mgr = WorkspaceManager(config=cfg, repository_populator=populator)

    first = await mgr.ensure_for_issue("RG-123")
    second = await mgr.ensure_for_issue("RG-123")

    assert first.created_now is True
    assert second.created_now is False
    assert second.path == first.path
    assert len(populator.calls) == 1


@pytest.mark.asyncio
async def test_repository_population_failure_removes_workspace_and_raises(tmp_path: Path) -> None:
    populator = FakeRepositoryPopulator(fail=True)
    cfg = _make_config(
        workspace_root=tmp_path,
        repository="https://github.com/tokachev/river_gang.git",
    )
    mgr = WorkspaceManager(config=cfg, repository_populator=populator)

    with pytest.raises(WorkspaceRepositoryFailed):
        await mgr.ensure_for_issue("RG-123")

    assert not (tmp_path / "RG-123").exists()
