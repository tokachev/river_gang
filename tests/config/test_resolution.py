"""Tests for ``$VAR`` indirection and path normalization (SPED §5.3.1, §5.3.3, §6.1)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from river_gang.config.defaults import apply_defaults
from river_gang.config.resolution import (
    normalize_path,
    resolve_and_validate,
    resolve_env_vars,
)

# ---------------------------------------------------------------------------
# resolve_env_vars
# ---------------------------------------------------------------------------


def test_resolve_env_vars_dollar_token_reads_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "lin_secret_123")
    assert resolve_env_vars("$LINEAR_API_KEY") == "lin_secret_123"


def test_resolve_env_vars_missing_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SPED §5.3.1: missing env var → treat as missing → ``None``."""
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    assert resolve_env_vars("$LINEAR_API_KEY") is None


def test_resolve_env_vars_empty_string_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SPED §5.3.1: empty resolution → treat as missing → ``None`` (NOT '')."""
    monkeypatch.setenv("LINEAR_API_KEY", "")
    assert resolve_env_vars("$LINEAR_API_KEY") is None


def test_resolve_env_vars_literal_passes_through() -> None:
    """No leading ``$`` ⇒ value used as-is (literal token)."""
    assert resolve_env_vars("lin_secret_literal") == "lin_secret_literal"


def test_resolve_env_vars_none_passes_through() -> None:
    assert resolve_env_vars(None) is None


def test_resolve_env_vars_empty_input_passes_through() -> None:
    assert resolve_env_vars("") == ""


def test_resolve_env_vars_only_recognises_strict_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``$Lower``, ``$1FOO``, ``${FOO}`` are NOT matched — pattern is
    ``\\$[A-Z_][A-Z0-9_]*`` only."""
    monkeypatch.setenv("foo", "x")
    monkeypatch.setenv("FOO", "y")

    assert resolve_env_vars("$foo") == "$foo"
    assert resolve_env_vars("$1FOO") == "$1FOO"
    assert resolve_env_vars("${FOO}") == "${FOO}"
    assert resolve_env_vars("$FOO bar") == "$FOO bar"  # has trailing chars


def test_resolve_env_vars_underscore_first_char_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("_PRIVATE", "ok")
    assert resolve_env_vars("$_PRIVATE") == "ok"


# ---------------------------------------------------------------------------
# normalize_path
# ---------------------------------------------------------------------------


def test_normalize_path_expands_tilde(tmp_path: Path) -> None:
    home = Path.home()
    out = normalize_path("~/projects/x", base_dir=tmp_path)
    assert out == str(home / "projects" / "x")
    assert os.path.isabs(out)


def test_normalize_path_expands_dollar_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WORKSPACE_HOME", "/srv/ws")
    out = normalize_path("$WORKSPACE_HOME/runs", base_dir=tmp_path)
    assert out == "/srv/ws/runs"


def test_normalize_path_relative_resolves_against_base_dir(tmp_path: Path) -> None:
    out = normalize_path("./ws", base_dir=tmp_path)
    assert out == str(tmp_path / "ws")
    assert os.path.isabs(out)


def test_normalize_path_bare_relative_resolves_against_base_dir(
    tmp_path: Path,
) -> None:
    out = normalize_path("ws/runs", base_dir=tmp_path)
    assert out == str(tmp_path / "ws" / "runs")


def test_normalize_path_absolute_unchanged(tmp_path: Path) -> None:
    out = normalize_path("/absolute/path", base_dir=tmp_path)
    assert out == "/absolute/path"


def test_normalize_path_absolute_with_dollar_var_resolved_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``$VAR`` expands BEFORE absoluteness check; resulting absolute path stays."""
    monkeypatch.setenv("WS_ROOT", "/abs/wsroot")
    out = normalize_path("$WS_ROOT/x", base_dir=tmp_path)
    assert out == "/abs/wsroot/x"


def test_normalize_path_dollar_var_with_relative_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WS_REL", "rel_dir")
    out = normalize_path("$WS_REL/x", base_dir=tmp_path)
    assert out == str(tmp_path / "rel_dir" / "x")


def test_normalize_path_collapses_dotdot(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    out = normalize_path("../sibling", base_dir=nested)
    # ``..`` segment is collapsed lexically (no symlink-following).
    assert out == os.path.normpath(str(nested / ".." / "sibling"))
    assert out == str(tmp_path / "a" / "sibling")


def test_normalize_path_undefined_var_leaves_literal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Undefined ``$VAR`` in a path is left as the literal segment so the
    caller sees an unmistakeably broken path rather than silent ``/x``."""
    monkeypatch.delenv("UNDEFINED_VAR", raising=False)
    out = normalize_path("$UNDEFINED_VAR/x", base_dir=tmp_path)
    assert "$UNDEFINED_VAR" in out


# ---------------------------------------------------------------------------
# resolve_and_validate (top-level integration)
# ---------------------------------------------------------------------------


def test_resolve_and_validate_resolves_tracker_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "lin_secret_xyz")
    raw = {
        "tracker": {
            "kind": "linear",
            "api_key": "$LINEAR_API_KEY",
            "project_slug": "p",
        }
    }
    cfg = resolve_and_validate(raw, workflow_dir=tmp_path)
    assert cfg.tracker.api_key == "lin_secret_xyz"


def test_resolve_and_validate_missing_api_key_var_yields_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    raw = {"tracker": {"kind": "linear", "api_key": "$LINEAR_API_KEY"}}
    cfg = resolve_and_validate(raw, workflow_dir=tmp_path)
    assert cfg.tracker.api_key is None


def test_resolve_and_validate_empty_api_key_var_yields_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "")
    raw = {"tracker": {"kind": "linear", "api_key": "$LINEAR_API_KEY"}}
    cfg = resolve_and_validate(raw, workflow_dir=tmp_path)
    assert cfg.tracker.api_key is None


def test_resolve_and_validate_literal_api_key_passes_through(
    tmp_path: Path,
) -> None:
    raw = {"tracker": {"api_key": "lit_secret"}}
    cfg = resolve_and_validate(raw, workflow_dir=tmp_path)
    assert cfg.tracker.api_key == "lit_secret"


def test_resolve_and_validate_endpoint_is_uri_not_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SPED §6.1: URIs are NOT rewritten by ``normalize_path``.

    Even if the operator set ``tracker.endpoint`` containing a relative-looking
    string, it must be returned verbatim — no base_dir absolutization, no
    ``~`` expansion.
    """
    raw = {"tracker": {"endpoint": "https://api.linear.app/graphql"}}
    cfg = resolve_and_validate(raw, workflow_dir=tmp_path)
    assert cfg.tracker.endpoint == "https://api.linear.app/graphql"

    raw2 = {"tracker": {"endpoint": "~/looks-like-path"}}
    cfg2 = resolve_and_validate(raw2, workflow_dir=tmp_path)
    assert cfg2.tracker.endpoint == "~/looks-like-path"


def test_resolve_and_validate_workspace_root_relative_resolves_against_workflow_dir(
    tmp_path: Path,
) -> None:
    raw = {"workspace": {"root": "ws"}}
    cfg = resolve_and_validate(raw, workflow_dir=tmp_path)
    assert cfg.workspace.root == str(tmp_path / "ws")
    assert os.path.isabs(cfg.workspace.root)


def test_resolve_and_validate_workspace_root_tilde_expands(tmp_path: Path) -> None:
    raw = {"workspace": {"root": "~/symphony_runs"}}
    cfg = resolve_and_validate(raw, workflow_dir=tmp_path)
    assert cfg.workspace.root == str(Path.home() / "symphony_runs")


def test_resolve_and_validate_workspace_root_absolute_unchanged(
    tmp_path: Path,
) -> None:
    raw = {"workspace": {"root": "/var/lib/symphony"}}
    cfg = resolve_and_validate(raw, workflow_dir=tmp_path)
    assert cfg.workspace.root == "/var/lib/symphony"


def test_resolve_and_validate_workspace_root_dollar_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WORKSPACE_HOME", "/srv/ws")
    raw = {"workspace": {"root": "$WORKSPACE_HOME"}}
    cfg = resolve_and_validate(raw, workflow_dir=tmp_path)
    assert cfg.workspace.root == "/srv/ws"


def test_resolve_and_validate_workspace_root_default_unchanged(
    tmp_path: Path,
) -> None:
    """Default ``<system-temp>/symphony_workspaces`` is already absolute and
    must not depend on ``workflow_dir``."""
    cfg = resolve_and_validate({}, workflow_dir=tmp_path)
    assert os.path.isabs(cfg.workspace.root)
    assert cfg.workspace.root.endswith("symphony_workspaces")


def test_resolve_and_validate_codex_command_not_path_normalized(
    tmp_path: Path,
) -> None:
    """Arbitrary shell command strings (codex.command) are NOT rewritten."""
    raw = {"codex": {"command": "codex app-server"}}
    cfg = resolve_and_validate(raw, workflow_dir=tmp_path)
    assert cfg.codex.command == "codex app-server"


def test_resolve_and_validate_returns_effective_config_type(
    tmp_path: Path,
) -> None:
    cfg = resolve_and_validate({}, workflow_dir=tmp_path)
    # same shape as apply_defaults result
    raw_cfg = apply_defaults({})
    assert type(cfg) is type(raw_cfg)


def test_resolve_and_validate_non_dollar_api_key_yields_string(
    tmp_path: Path,
) -> None:
    raw = {"tracker": {"api_key": ""}}
    cfg = resolve_and_validate(raw, workflow_dir=tmp_path)
    # Empty literal (not $VAR) — ``apply_defaults`` already returned ``""``;
    # resolution stage must NOT magically convert literal "" to None. Only
    # ``$VAR`` resolution does that. Validation in Task 6 will reject empty.
    assert cfg.tracker.api_key == ""


def test_resolve_and_validate_does_not_mutate_raw(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "v")
    raw: dict[str, object] = {
        "tracker": {"api_key": "$LINEAR_API_KEY"},
        "workspace": {"root": "ws"},
    }
    snapshot_api = raw["tracker"]["api_key"]  # type: ignore[index]
    snapshot_root = raw["workspace"]["root"]  # type: ignore[index]
    resolve_and_validate(raw, workflow_dir=tmp_path)
    assert raw["tracker"]["api_key"] == snapshot_api  # type: ignore[index]
    assert raw["workspace"]["root"] == snapshot_root  # type: ignore[index]
