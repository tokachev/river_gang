"""Tests for workspace key sanitization and path safety (SPED §9.5)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from river_gang.workspace.safety import (
    EmptyWorkspaceKey,
    InvalidWorkspaceKey,
    WorkspaceOutsideRoot,
    sanitize_key,
    validate_within_root,
)

# ---------------------------------------------------------------------------
# sanitize_key — invariant 3: only [A-Za-z0-9._-] allowed
# ---------------------------------------------------------------------------


def test_sanitize_key_passthrough_alnum_dash() -> None:
    assert sanitize_key("ABC-123") == "ABC-123"


def test_sanitize_key_replaces_slashes_backslashes_and_spaces() -> None:
    assert sanitize_key("a/b\\c d") == "a_b_c_d"


def test_sanitize_key_keeps_dot_and_underscore() -> None:
    assert sanitize_key("v1.2_alpha-3") == "v1.2_alpha-3"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("foo bar", "foo_bar"),
        ("a:b", "a_b"),
        ("a;b", "a_b"),
        ("a*b", "a_b"),
        ("a?b", "a_b"),
        ('a"b', "a_b"),
        ("a<b>", "a_b_"),
        ("a|b", "a_b"),
        ("a$b", "a_b"),
        ("a%20b", "a_20b"),
        ("issue/RG-1", "issue_RG-1"),
        ("../etc/passwd", ".._etc_passwd"),
        ("☃snowman", "_snowman"),
        ("\x00null", "_null"),
    ],
)
def test_sanitize_key_replaces_disallowed_with_underscore(
    raw: str, expected: str
) -> None:
    assert sanitize_key(raw) == expected


def test_sanitize_key_empty_raises() -> None:
    with pytest.raises(EmptyWorkspaceKey):
        sanitize_key("")


def test_sanitize_key_rejects_dot_and_dotdot() -> None:
    """A workspace key of ``.`` or ``..`` would resolve to the workspace
    root or its parent — defensive reject at sanitize time even though
    ``validate_within_root`` would also catch it."""
    with pytest.raises(InvalidWorkspaceKey):
        sanitize_key(".")
    with pytest.raises(InvalidWorkspaceKey):
        sanitize_key("..")


def test_sanitize_key_returns_str_not_path() -> None:
    assert isinstance(sanitize_key("RG-1"), str)


def test_sanitize_key_does_not_collapse_repeated_underscores() -> None:
    """Each disallowed char becomes one underscore — the function does not
    deduplicate consecutive underscores so distinct inputs map to distinct
    outputs (no accidental collisions)."""
    assert sanitize_key("a  b") == "a__b"
    assert sanitize_key("a   b") == "a___b"


# ---------------------------------------------------------------------------
# validate_within_root — invariant 2
# ---------------------------------------------------------------------------


def test_validate_within_root_accepts_path_inside_root(tmp_path: Path) -> None:
    candidate = tmp_path / "RG-1"
    out = validate_within_root(tmp_path, candidate)
    assert out == tmp_path.resolve() / "RG-1"


def test_validate_within_root_accepts_nested_path(tmp_path: Path) -> None:
    candidate = tmp_path / "deeply" / "nested" / "RG-1"
    out = validate_within_root(tmp_path, candidate)
    assert out.is_relative_to(tmp_path.resolve())


def test_validate_within_root_accepts_root_itself(tmp_path: Path) -> None:
    out = validate_within_root(tmp_path, tmp_path)
    assert out == tmp_path.resolve()


def test_validate_within_root_rejects_dotdot_traversal(tmp_path: Path) -> None:
    candidate = tmp_path / ".." / "etc" / "passwd"
    with pytest.raises(WorkspaceOutsideRoot):
        validate_within_root(tmp_path, candidate)


def test_validate_within_root_rejects_double_dotdot(tmp_path: Path) -> None:
    candidate = tmp_path / ".." / ".." / "etc"
    with pytest.raises(WorkspaceOutsideRoot):
        validate_within_root(tmp_path, candidate)


def test_validate_within_root_rejects_absolute_outside_root(
    tmp_path: Path,
) -> None:
    with pytest.raises(WorkspaceOutsideRoot):
        validate_within_root(tmp_path, Path("/etc/passwd"))


def test_validate_within_root_rejects_sibling_dir(tmp_path: Path) -> None:
    sibling = tmp_path.parent / "OTHER"
    with pytest.raises(WorkspaceOutsideRoot):
        validate_within_root(tmp_path, sibling)


def test_validate_within_root_accepts_string_inputs(tmp_path: Path) -> None:
    out = validate_within_root(str(tmp_path), str(tmp_path / "RG-1"))
    assert isinstance(out, Path)
    assert out == tmp_path.resolve() / "RG-1"


def test_validate_within_root_returns_resolved_absolute_path(
    tmp_path: Path,
) -> None:
    out = validate_within_root(tmp_path, tmp_path / "RG-1")
    assert out.is_absolute()


def test_validate_within_root_handles_nonexistent_candidate(
    tmp_path: Path,
) -> None:
    """``Path.resolve(strict=False)`` works on nonexistent paths."""
    out = validate_within_root(tmp_path, tmp_path / "does-not-exist-yet")
    assert out == tmp_path.resolve() / "does-not-exist-yet"


def test_validate_within_root_rejects_when_root_contains_root_substring(
    tmp_path: Path,
) -> None:
    """``/tmp/foo`` MUST NOT be accepted as a child of ``/tmp/foobar``.

    Naive string-prefix checks would let ``/tmp/foobar123`` pass for root
    ``/tmp/foo`` — guard against that.
    """
    root = tmp_path / "foo"
    root.mkdir()
    sibling = tmp_path / "foobar"
    sibling.mkdir()
    with pytest.raises(WorkspaceOutsideRoot):
        validate_within_root(root, sibling)


# ---------------------------------------------------------------------------
# Symlink behaviour
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlinks")
def test_validate_within_root_rejects_symlink_pointing_outside(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    link = root / "escape"
    os.symlink(outside, link)

    with pytest.raises(WorkspaceOutsideRoot):
        validate_within_root(root, link)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlinks")
def test_validate_within_root_accepts_symlink_pointing_inside(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    target = root / "real"
    target.mkdir()
    link = root / "alias"
    os.symlink(target, link)

    out = validate_within_root(root, link)
    assert out == target.resolve()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlinks")
def test_validate_within_root_root_is_a_symlink(tmp_path: Path) -> None:
    """When the supplied root itself is a symlink, both sides resolve and
    the boundary check still works."""
    real_root = tmp_path / "real_root"
    real_root.mkdir()
    link_root = tmp_path / "link_root"
    os.symlink(real_root, link_root)

    out = validate_within_root(link_root, link_root / "RG-1")
    assert out == real_root.resolve() / "RG-1"


# ---------------------------------------------------------------------------
# Round-trip: sanitize then validate
# ---------------------------------------------------------------------------


def test_sanitize_then_validate_round_trip(tmp_path: Path) -> None:
    key = sanitize_key("Issue/RG-42")
    candidate = tmp_path / key
    out = validate_within_root(tmp_path, candidate)
    assert out == tmp_path.resolve() / "Issue_RG-42"
