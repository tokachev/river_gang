"""Tests for :func:`render_prompt` (SPED §5.4)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from river_gang.prompt.errors import (
    PromptError,
    TemplateParseError,
    TemplateRenderError,
)
from river_gang.prompt.render import FALLBACK_PROMPT, render_prompt
from river_gang.tracker.issue import BlockerRef, Issue


def _issue(**overrides: object) -> Issue:
    base = {
        "id": "uuid-1",
        "identifier": "RG-1",
        "title": "Implement feature",
        "state": "Todo",
        "description": "Some description.",
        "priority": 2,
        "branch_name": "feature/rg-1",
        "url": "https://linear.app/x/RG-1",
        "labels": ("bug", "core"),
        "blocked_by": (
            BlockerRef(id="b1", identifier="RG-99", state="In Progress"),
            BlockerRef(id="b2", identifier="RG-100", state="Done"),
        ),
        "created_at": datetime(2026, 4, 1, 10, 30, tzinfo=UTC),
        "updated_at": datetime(2026, 4, 2, 11, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return Issue(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Variable access (§5.4 input variables)
# ---------------------------------------------------------------------------


def test_render_basic_variables() -> None:
    out = render_prompt(
        "Issue {{ issue.identifier }}: {{ issue.title }}\nState={{ issue.state }}",
        issue=_issue(),
        attempt=None,
    )
    assert "Issue RG-1: Implement feature" in out
    assert "State=Todo" in out


def test_render_description_url_branch_priority() -> None:
    out = render_prompt(
        "desc={{ issue.description }} url={{ issue.url }} "
        "branch={{ issue.branch_name }} prio={{ issue.priority }}",
        issue=_issue(),
        attempt=None,
    )
    assert "desc=Some description." in out
    assert "url=https://linear.app/x/RG-1" in out
    assert "branch=feature/rg-1" in out
    assert "prio=2" in out


def test_render_optional_fields_as_nil_when_none() -> None:
    issue = _issue(description=None, branch_name=None, url=None, priority=None)
    out = render_prompt(
        "desc=[{{ issue.description }}] branch=[{{ issue.branch_name }}] "
        "url=[{{ issue.url }}] prio=[{{ issue.priority }}]",
        issue=issue,
        attempt=None,
    )
    assert "desc=[]" in out
    assert "branch=[]" in out
    assert "url=[]" in out
    assert "prio=[]" in out


def test_render_iterates_labels() -> None:
    out = render_prompt(
        "{% for l in issue.labels %}<{{ l }}>{% endfor %}",
        issue=_issue(),
        attempt=None,
    )
    assert out == "<bug><core>"


def test_render_iterates_blockers_with_nested_fields() -> None:
    out = render_prompt(
        "{% for b in issue.blocked_by %}{{ b.identifier }}={{ b.state }};{% endfor %}",
        issue=_issue(),
        attempt=None,
    )
    assert out == "RG-99=In Progress;RG-100=Done;"


def test_render_empty_labels_iterable_is_safe() -> None:
    out = render_prompt(
        "x={% for l in issue.labels %}{{ l }}{% endfor %}.",
        issue=_issue(labels=()),
        attempt=None,
    )
    assert out == "x=."


def test_render_empty_blocked_by_iterable_is_safe() -> None:
    out = render_prompt(
        "blockers=[{% for b in issue.blocked_by %}{{ b.identifier }}{% endfor %}]",
        issue=_issue(blocked_by=()),
        attempt=None,
    )
    assert out == "blockers=[]"


# ---------------------------------------------------------------------------
# attempt variable
# ---------------------------------------------------------------------------


def test_render_attempt_none_renders_as_empty() -> None:
    out = render_prompt("a={{ attempt }}!", issue=_issue(), attempt=None)
    assert out == "a=!"


@pytest.mark.parametrize("n", [1, 2, 7, 100])
def test_render_attempt_int_interpolated(n: int) -> None:
    out = render_prompt("attempt={{ attempt }}", issue=_issue(), attempt=n)
    assert out == f"attempt={n}"


def test_render_attempt_can_be_compared_in_conditional() -> None:
    template = (
        "{% if attempt %}retry #{{ attempt }}{% else %}first run{% endif %}"
    )
    assert (
        render_prompt(template, issue=_issue(), attempt=None) == "first run"
    )
    assert (
        render_prompt(template, issue=_issue(), attempt=3) == "retry #3"
    )


# ---------------------------------------------------------------------------
# Strict-undefined and unknown filter
# ---------------------------------------------------------------------------


def test_render_unknown_top_level_variable_raises() -> None:
    with pytest.raises(TemplateRenderError):
        render_prompt("{{ unknown_var }}", issue=_issue(), attempt=None)


def test_render_unknown_issue_attribute_raises() -> None:
    with pytest.raises(TemplateRenderError):
        render_prompt("{{ issue.does_not_exist }}", issue=_issue(), attempt=None)


def test_render_unknown_filter_raises() -> None:
    with pytest.raises(TemplateRenderError):
        render_prompt(
            "{{ issue.title | bogus_filter }}", issue=_issue(), attempt=None
        )


def test_render_template_render_error_is_prompt_error_subclass() -> None:
    assert issubclass(TemplateRenderError, PromptError)
    assert issubclass(TemplateParseError, PromptError)


# ---------------------------------------------------------------------------
# Parse errors
# ---------------------------------------------------------------------------


def test_render_bad_syntax_raises_template_parse_error() -> None:
    with pytest.raises(TemplateParseError):
        render_prompt("{% if %}", issue=_issue(), attempt=None)


def test_render_unbalanced_tag_raises_template_parse_error() -> None:
    with pytest.raises(TemplateParseError):
        render_prompt(
            "{% for x in issue.labels %}{{ x }}", issue=_issue(), attempt=None
        )


# ---------------------------------------------------------------------------
# Fallback prompt (§5.4)
# ---------------------------------------------------------------------------


def test_render_empty_template_returns_fallback() -> None:
    out = render_prompt("", issue=_issue(), attempt=None)
    assert out == FALLBACK_PROMPT
    assert out == "You are working on an issue from Linear."


def test_render_whitespace_only_template_returns_fallback() -> None:
    assert render_prompt("   ", issue=_issue(), attempt=None) == FALLBACK_PROMPT
    assert render_prompt("\n\n", issue=_issue(), attempt=None) == FALLBACK_PROMPT
    assert (
        render_prompt("\t\r\n  ", issue=_issue(), attempt=None) == FALLBACK_PROMPT
    )


def test_render_fallback_does_not_use_template_engine() -> None:
    """Fallback path must not be subject to strict-undefined checks itself —
    even an obviously-broken template like ``""`` returns the fallback,
    not a TemplateRenderError."""
    # Already covered indirectly above; assert again that no exception leaks.
    render_prompt("", issue=_issue(), attempt=None)


# ---------------------------------------------------------------------------
# Real WORKFLOW.md-style example
# ---------------------------------------------------------------------------


def test_render_realistic_workflow_template() -> None:
    template = (
        "Work on {{ issue.identifier }}: {{ issue.title }}\n"
        "State: {{ issue.state }}\n"
        "Labels: {% for l in issue.labels %}{{ l }} {% endfor %}\n"
        "{% if issue.description %}Description: {{ issue.description }}\n{% endif %}"
        "Attempt: {% if attempt %}{{ attempt }}{% else %}first{% endif %}\n"
    )
    out = render_prompt(template, issue=_issue(), attempt=2)
    assert "Work on RG-1: Implement feature" in out
    assert "Labels: bug core " in out
    assert "Description: Some description." in out
    assert "Attempt: 2" in out


def test_render_realistic_workflow_template_first_attempt() -> None:
    template = "Attempt: {% if attempt %}{{ attempt }}{% else %}first{% endif %}"
    out = render_prompt(template, issue=_issue(), attempt=None)
    assert out == "Attempt: first"


# ---------------------------------------------------------------------------
# Nice-to-have: timestamps still accessible
# ---------------------------------------------------------------------------


def test_render_includes_timestamps() -> None:
    out = render_prompt(
        "created={{ issue.created_at }} updated={{ issue.updated_at }}",
        issue=_issue(),
        attempt=None,
    )
    assert "2026-04-01" in out
    assert "2026-04-02" in out
