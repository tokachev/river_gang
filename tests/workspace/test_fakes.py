"""Tests for :class:`FakeWorkspaceManager` (test-infrastructure fake)."""

from __future__ import annotations

from pathlib import Path

import pytest

from river_gang.workspace.hooks import HookResult
from river_gang.workspace.manager import EnsureResult, WorkspaceHookFailed
from tests.workspace.fakes import FakeWorkspaceManager

# ---------------------------------------------------------------------------
# path_for_issue + sanitization
# ---------------------------------------------------------------------------


def test_path_for_issue_under_root() -> None:
    fake = FakeWorkspaceManager(root_path=Path("/synthetic/ws"))
    assert fake.path_for_issue("RG-1") == Path("/synthetic/ws/RG-1")


def test_path_for_issue_sanitizes_disallowed_chars() -> None:
    fake = FakeWorkspaceManager(root_path=Path("/synthetic/ws"))
    assert fake.path_for_issue("issue/RG-1 weird") == Path(
        "/synthetic/ws/issue_RG-1_weird"
    )


def test_path_for_issue_rejects_empty_identifier() -> None:
    fake = FakeWorkspaceManager(root_path=Path("/synthetic/ws"))
    with pytest.raises(Exception):
        fake.path_for_issue("")


def test_path_for_issue_rejects_dotdot() -> None:
    fake = FakeWorkspaceManager(root_path=Path("/synthetic/ws"))
    with pytest.raises(Exception):
        fake.path_for_issue("..")


# ---------------------------------------------------------------------------
# ensure_for_issue — first call vs reuse
# ---------------------------------------------------------------------------


async def test_ensure_first_call_returns_created_now_true() -> None:
    fake = FakeWorkspaceManager(root_path=Path("/synthetic/ws"))
    result = await fake.ensure_for_issue("RG-1")
    assert isinstance(result, EnsureResult)
    assert result.created_now is True
    assert result.path == Path("/synthetic/ws/RG-1")


async def test_ensure_second_call_returns_created_now_false() -> None:
    fake = FakeWorkspaceManager(root_path=Path("/synthetic/ws"))
    first = await fake.ensure_for_issue("RG-1")
    second = await fake.ensure_for_issue("RG-1")
    assert first.created_now is True
    assert second.created_now is False
    assert second.path == first.path


async def test_ensure_does_not_touch_real_filesystem(tmp_path: Path) -> None:
    """The synthetic root must NOT exist on disk — bookkeeping only."""
    root = tmp_path / "ghost"
    fake = FakeWorkspaceManager(root_path=root)
    result = await fake.ensure_for_issue("RG-1")
    # Path returned is purely synthetic — no mkdir happened
    assert not root.exists()
    assert not result.path.exists()


async def test_ensure_different_identifiers_create_distinct_paths() -> None:
    fake = FakeWorkspaceManager(root_path=Path("/synthetic/ws"))
    a = await fake.ensure_for_issue("RG-1")
    b = await fake.ensure_for_issue("RG-2")
    assert a.path != b.path
    assert a.created_now is True
    assert b.created_now is True


# ---------------------------------------------------------------------------
# ensure_for_issue — hook outcome injection
# ---------------------------------------------------------------------------


async def test_ensure_after_create_failure_raises_and_unsets_created() -> None:
    fake = FakeWorkspaceManager(
        root_path=Path("/synthetic/ws"),
        after_create_results={
            "RG-1": HookResult(
                exit_code=1,
                stdout=b"",
                stderr=b"boom",
                duration_ms=5,
                timed_out=False,
                is_skipped=False,
            )
        },
    )
    with pytest.raises(WorkspaceHookFailed):
        await fake.ensure_for_issue("RG-1")
    # After failure, the issue is unmarked so a retry can attempt creation
    # again with created_now=True (matches real WorkspaceManager).
    fake.set_after_create_result("RG-1", None)  # remove the failure
    second = await fake.ensure_for_issue("RG-1")
    assert second.created_now is True


async def test_ensure_after_create_success_does_not_raise() -> None:
    fake = FakeWorkspaceManager(
        root_path=Path("/synthetic/ws"),
        after_create_results={
            "RG-1": HookResult(
                exit_code=0,
                stdout=b"",
                stderr=b"",
                duration_ms=1,
                timed_out=False,
                is_skipped=False,
            )
        },
    )
    result = await fake.ensure_for_issue("RG-1")
    assert result.created_now is True


# ---------------------------------------------------------------------------
# cleanup_for_issue
# ---------------------------------------------------------------------------


async def test_cleanup_after_ensure_succeeds() -> None:
    fake = FakeWorkspaceManager(root_path=Path("/synthetic/ws"))
    await fake.ensure_for_issue("RG-1")
    await fake.cleanup_for_issue("RG-1")
    # After cleanup, the next ensure should report created_now=True again
    result = await fake.ensure_for_issue("RG-1")
    assert result.created_now is True


async def test_cleanup_missing_issue_is_noop() -> None:
    fake = FakeWorkspaceManager(root_path=Path("/synthetic/ws"))
    # cleanup without prior ensure must not raise
    await fake.cleanup_for_issue("RG-99")


async def test_cleanup_does_not_touch_real_filesystem(tmp_path: Path) -> None:
    fake = FakeWorkspaceManager(root_path=tmp_path / "ghost")
    await fake.ensure_for_issue("RG-1")
    await fake.cleanup_for_issue("RG-1")
    assert not (tmp_path / "ghost").exists()


# ---------------------------------------------------------------------------
# .calls bookkeeping
# ---------------------------------------------------------------------------


async def test_calls_records_ensure_and_cleanup_in_order() -> None:
    fake = FakeWorkspaceManager(root_path=Path("/synthetic/ws"))
    await fake.ensure_for_issue("RG-1")
    await fake.cleanup_for_issue("RG-1")
    await fake.ensure_for_issue("RG-2")

    methods = [m for m, _ in fake.calls]
    assert methods == ["ensure_for_issue", "cleanup_for_issue", "ensure_for_issue"]
    assert fake.calls[0] == ("ensure_for_issue", {"identifier": "RG-1"})
    assert fake.calls[1] == ("cleanup_for_issue", {"identifier": "RG-1"})
    assert fake.calls[2] == ("ensure_for_issue", {"identifier": "RG-2"})


async def test_calls_records_failed_ensure() -> None:
    fake = FakeWorkspaceManager(
        root_path=Path("/synthetic/ws"),
        after_create_results={
            "RG-1": HookResult(
                exit_code=1, stdout=b"", stderr=b"x", duration_ms=1,
                timed_out=False, is_skipped=False,
            )
        },
    )
    with pytest.raises(WorkspaceHookFailed):
        await fake.ensure_for_issue("RG-1")
    # Call still recorded for ordering assertions
    assert fake.calls == [("ensure_for_issue", {"identifier": "RG-1"})]
