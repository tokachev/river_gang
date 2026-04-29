"""Tests for dispatch preflight validation (SPED §6.3, §5.3.4, §5.3.5)."""

from __future__ import annotations

from typing import Any

import pytest

from river_gang.config.defaults import apply_defaults
from river_gang.config.schema import (
    AgentConfig,
    EffectiveConfig,
    HooksConfig,
)
from river_gang.config.validation import (
    ValidationResult,
    format_error_for_operator,
    validate_for_dispatch,
)


def _valid_raw() -> dict[str, Any]:
    """Smallest raw-map that passes preflight."""
    return {
        "tracker": {
            "kind": "linear",
            "api_key": "lit_secret",
            "project_slug": "river-gang",
        },
        "codex": {"command": "codex app-server"},
    }


def _build(raw: dict[str, Any]) -> EffectiveConfig:
    return apply_defaults(raw)


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_valid_config_returns_ok_with_empty_errors() -> None:
    cfg = _build(_valid_raw())
    result = validate_for_dispatch(cfg)
    assert result.ok is True
    assert result.errors == []


def test_validation_result_is_dataclass_with_expected_fields() -> None:
    r = ValidationResult(ok=True, errors=[])
    assert r.ok is True
    assert r.errors == []

    r2 = ValidationResult(ok=False, errors=["a", "b"])
    assert r2.ok is False
    assert r2.errors == ["a", "b"]


# ---------------------------------------------------------------------------
# tracker.kind
# ---------------------------------------------------------------------------


def test_tracker_kind_missing_fails() -> None:
    raw = _valid_raw()
    del raw["tracker"]["kind"]
    cfg = _build(raw)
    result = validate_for_dispatch(cfg)
    assert result.ok is False
    assert any("tracker.kind" in e for e in result.errors)


def test_tracker_kind_unsupported_fails() -> None:
    raw = _valid_raw()
    raw["tracker"]["kind"] = "github"
    cfg = _build(raw)
    result = validate_for_dispatch(cfg)
    assert result.ok is False
    joined = " | ".join(result.errors)
    assert "tracker.kind" in joined
    assert "github" in joined


def test_tracker_kind_empty_string_fails() -> None:
    raw = _valid_raw()
    raw["tracker"]["kind"] = ""
    cfg = _build(raw)
    result = validate_for_dispatch(cfg)
    assert result.ok is False
    assert any("tracker.kind" in e for e in result.errors)


# ---------------------------------------------------------------------------
# tracker.api_key
# ---------------------------------------------------------------------------


def test_tracker_api_key_none_fails() -> None:
    """``None`` is the post-$-resolution "missing" sentinel (§5.3.1)."""
    raw = _valid_raw()
    del raw["tracker"]["api_key"]
    cfg = _build(raw)
    result = validate_for_dispatch(cfg)
    assert result.ok is False
    assert any("tracker.api_key" in e for e in result.errors)


def test_tracker_api_key_empty_string_fails() -> None:
    """Literal empty string is also "missing" for dispatch purposes."""
    raw = _valid_raw()
    raw["tracker"]["api_key"] = ""
    cfg = _build(raw)
    result = validate_for_dispatch(cfg)
    assert result.ok is False
    assert any("tracker.api_key" in e for e in result.errors)


# ---------------------------------------------------------------------------
# tracker.project_slug (REQUIRED only when kind=linear)
# ---------------------------------------------------------------------------


def test_tracker_project_slug_missing_when_linear_fails() -> None:
    raw = _valid_raw()
    del raw["tracker"]["project_slug"]
    cfg = _build(raw)
    result = validate_for_dispatch(cfg)
    assert result.ok is False
    assert any("tracker.project_slug" in e for e in result.errors)


def test_tracker_project_slug_empty_when_linear_fails() -> None:
    raw = _valid_raw()
    raw["tracker"]["project_slug"] = ""
    cfg = _build(raw)
    result = validate_for_dispatch(cfg)
    assert result.ok is False
    assert any("tracker.project_slug" in e for e in result.errors)


# ---------------------------------------------------------------------------
# codex.command
# ---------------------------------------------------------------------------


def test_codex_command_empty_string_fails() -> None:
    raw = _valid_raw()
    raw["codex"]["command"] = ""
    cfg = _build(raw)
    result = validate_for_dispatch(cfg)
    assert result.ok is False
    assert any("codex.command" in e for e in result.errors)


def test_codex_command_default_passes() -> None:
    """Missing ``codex.command`` falls back to default ``codex app-server`` — no error."""
    raw = _valid_raw()
    raw.pop("codex", None)
    cfg = _build(raw)
    result = validate_for_dispatch(cfg)
    assert result.ok is True


# ---------------------------------------------------------------------------
# tracker.endpoint default
# ---------------------------------------------------------------------------


def test_tracker_endpoint_missing_uses_default_no_error() -> None:
    raw = _valid_raw()
    raw["tracker"].pop("endpoint", None)
    cfg = _build(raw)
    result = validate_for_dispatch(cfg)
    assert result.ok is True
    assert cfg.tracker.endpoint == "https://api.linear.app/graphql"


# ---------------------------------------------------------------------------
# agent.max_turns (§5.3.5 — positive integer)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_value", [0, -1, -100])
def test_agent_max_turns_non_positive_fails(bad_value: int) -> None:
    cfg = _build(_valid_raw())
    bad = EffectiveConfig(
        tracker=cfg.tracker,
        polling=cfg.polling,
        workspace=cfg.workspace,
        hooks=cfg.hooks,
        agent=AgentConfig(
            max_concurrent_agents=cfg.agent.max_concurrent_agents,
            max_turns=bad_value,
            max_retry_backoff_ms=cfg.agent.max_retry_backoff_ms,
            max_concurrent_agents_by_state=cfg.agent.max_concurrent_agents_by_state,
        ),
        codex=cfg.codex,
    )
    result = validate_for_dispatch(bad)
    assert result.ok is False
    assert any("agent.max_turns" in e for e in result.errors)


def test_agent_max_turns_positive_default_passes() -> None:
    cfg = _build(_valid_raw())
    assert cfg.agent.max_turns == 20
    assert validate_for_dispatch(cfg).ok is True


# ---------------------------------------------------------------------------
# hooks.timeout_ms (§5.3.4 — positive integer)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_value", [0, -1, -1000])
def test_hooks_timeout_ms_non_positive_fails(bad_value: int) -> None:
    cfg = _build(_valid_raw())
    bad = EffectiveConfig(
        tracker=cfg.tracker,
        polling=cfg.polling,
        workspace=cfg.workspace,
        hooks=HooksConfig(
            after_create=cfg.hooks.after_create,
            before_run=cfg.hooks.before_run,
            after_run=cfg.hooks.after_run,
            before_remove=cfg.hooks.before_remove,
            timeout_ms=bad_value,
        ),
        agent=cfg.agent,
        codex=cfg.codex,
    )
    result = validate_for_dispatch(bad)
    assert result.ok is False
    assert any("hooks.timeout_ms" in e for e in result.errors)


def test_hooks_timeout_ms_positive_default_passes() -> None:
    cfg = _build(_valid_raw())
    assert cfg.hooks.timeout_ms == 60000
    assert validate_for_dispatch(cfg).ok is True


# ---------------------------------------------------------------------------
# multiple errors aggregate
# ---------------------------------------------------------------------------


def test_multiple_errors_are_collected() -> None:
    # kind=linear triggers the project_slug check too; everything else is
    # left blank to surface every other rule simultaneously.
    cfg = _build({"tracker": {"kind": "linear"}, "codex": {"command": ""}})
    result = validate_for_dispatch(cfg)
    assert result.ok is False
    # tracker.api_key, tracker.project_slug, codex.command at minimum
    assert len(result.errors) >= 3
    joined = " | ".join(result.errors)
    assert "tracker.api_key" in joined
    assert "tracker.project_slug" in joined
    assert "codex.command" in joined


# ---------------------------------------------------------------------------
# format_error_for_operator
# ---------------------------------------------------------------------------


def test_format_error_for_operator_when_ok() -> None:
    out = format_error_for_operator(ValidationResult(ok=True, errors=[]))
    assert out == ""


def test_format_error_for_operator_single_error() -> None:
    res = ValidationResult(ok=False, errors=["tracker.kind: missing or empty"])
    out = format_error_for_operator(res)
    assert "tracker.kind" in out
    assert "missing or empty" in out


def test_format_error_for_operator_multiple_errors_one_per_line() -> None:
    res = ValidationResult(
        ok=False,
        errors=[
            "tracker.kind: missing or empty",
            "tracker.api_key: missing after $-resolution",
        ],
    )
    out = format_error_for_operator(res)
    lines = out.splitlines()
    # at least one summary line + one line per error
    error_lines = [ln for ln in lines if "tracker." in ln]
    assert len(error_lines) == 2


def test_format_error_for_operator_includes_summary_count() -> None:
    res = ValidationResult(
        ok=False,
        errors=["a: x", "b: y", "c: z"],
    )
    out = format_error_for_operator(res)
    assert "3" in out  # count appears somewhere in summary
