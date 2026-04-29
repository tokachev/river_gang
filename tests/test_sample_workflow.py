"""Smoke tests for ``WORKFLOW.md.sample`` — the operator-facing template
used as a starting point in the README quick-start (Task 51).

Locks the sample's structural validity so ``cp WORKFLOW.md.sample
WORKFLOW.md`` always yields a parseable file. Resolves the path via
``__file__`` so the suite is cwd-independent (works under ``uv run
pytest`` from any directory).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from river_gang.config.defaults import apply_defaults
from river_gang.config.resolution import resolve_and_validate
from river_gang.config.validation import validate_for_dispatch
from river_gang.prompt import render_prompt
from river_gang.tracker.issue import parse_issue
from river_gang.workflow.loader import load_workflow

SAMPLE_PATH = Path(__file__).resolve().parent.parent / "WORKFLOW.md.sample"


def test_sample_workflow_file_exists() -> None:
    assert SAMPLE_PATH.is_file(), f"sample missing at {SAMPLE_PATH}"


def test_sample_workflow_loads_without_error() -> None:
    """``load_workflow`` parses the YAML front matter without raising."""
    wd = load_workflow(SAMPLE_PATH)
    assert wd.config  # non-empty front matter
    assert wd.prompt_template  # non-empty body


def test_sample_apply_defaults_succeeds() -> None:
    """``apply_defaults`` coerces the sample into an :class:`EffectiveConfig`."""
    wd = load_workflow(SAMPLE_PATH)
    cfg = apply_defaults(wd.config)
    # Tracker section pulled through.
    assert cfg.tracker.kind == "linear"
    # Polling override surfaced.
    assert cfg.polling.interval_ms == 30000


def test_sample_resolve_and_validate_returns_effective_config() -> None:
    """``resolve_and_validate`` accepts the sample regardless of whether
    ``$LINEAR_API_KEY`` is set in the environment.

    ``$VAR`` indirection resolves to ``None`` when the env var is unset
    (SPED §5.3.1) — that is OK at this layer; the dispatch preflight in
    :func:`validate_for_dispatch` is the gate that rejects the missing
    key.
    """
    wd = load_workflow(SAMPLE_PATH)
    cfg = resolve_and_validate(wd.config, workflow_dir=SAMPLE_PATH.parent)
    # Path expansion took effect — workspace.root no longer starts with ``~``.
    assert not cfg.workspace.root.startswith("~")


def test_sample_validates_for_dispatch_when_api_key_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``$LINEAR_API_KEY`` set, the sample passes dispatch preflight.

    The sample ships with ``project_slug: my-project-slug-here`` so the
    REQUIRED slug is non-empty even before the operator edits it.
    """
    monkeypatch.setenv("LINEAR_API_KEY", "lit_dummy_for_test")
    wd = load_workflow(SAMPLE_PATH)
    cfg = resolve_and_validate(wd.config, workflow_dir=SAMPLE_PATH.parent)
    result = validate_for_dispatch(cfg)
    assert result.ok, result.errors


def test_sample_prompt_template_renders_against_synthetic_issue() -> None:
    """The Liquid template body uses only published Issue / attempt fields."""
    wd = load_workflow(SAMPLE_PATH)
    issue = parse_issue({
        "id": "uuid-1",
        "identifier": "MT-1",
        "title": "implement tests",
        "state": {"name": "In Progress"},
        "description": "do the thing",
        "priority": 2,
        "labels": {"nodes": [{"name": "Backend"}, {"name": "URGENT"}]},
        "inverseRelations": {"nodes": []},
    })
    rendered = render_prompt(wd.prompt_template, issue=issue, attempt=1)
    assert "MT-1" in rendered
    assert "implement tests" in rendered
    assert "In Progress" in rendered
    assert "do the thing" in rendered
    # Labels rendered through {% for %}; lowercased per parse_issue.
    assert "backend" in rendered
    assert "urgent" in rendered
    # ``attempt > 1`` branch suppressed on attempt 1.
    assert "attempt 1" not in rendered.lower() or "this is attempt" not in rendered.lower()


def test_sample_prompt_template_renders_attempt_branch() -> None:
    """``{% if attempt > 1 %}`` branch fires on retries."""
    wd = load_workflow(SAMPLE_PATH)
    issue = parse_issue({
        "id": "uuid-2",
        "identifier": "MT-2",
        "title": "retry me",
        "state": {"name": "Todo"},
    })
    rendered = render_prompt(wd.prompt_template, issue=issue, attempt=3)
    assert "attempt 3" in rendered.lower()
