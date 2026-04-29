"""Tests for WORKFLOW.md loader (SPED §5.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from river_gang.workflow.errors import (
    FrontMatterNotAMap,
    MissingWorkflowFile,
    WorkflowParseError,
)
from river_gang.workflow.loader import WorkflowDefinition, load_workflow

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_load_workflow_missing_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "WORKFLOW.md"
    with pytest.raises(MissingWorkflowFile) as excinfo:
        load_workflow(missing)
    assert str(missing) in str(excinfo.value)


def test_load_workflow_with_front_matter_returns_config_and_body() -> None:
    result = load_workflow(FIXTURES_DIR / "valid_with_fm.md")

    assert isinstance(result, WorkflowDefinition)
    assert result.config["tracker"]["kind"] == "linear"
    assert result.config["tracker"]["api_key"] == "$LINEAR_API_KEY"
    assert result.config["tracker"]["project_slug"] == "river-gang"
    assert result.config["agent"]["max_concurrent_agents"] == 5
    assert result.prompt_template.startswith("# Workflow Prompt")
    assert "Process the issue" in result.prompt_template
    assert not result.prompt_template.endswith("\n")
    assert not result.prompt_template.startswith("\n")


def test_load_workflow_without_front_matter_treats_full_file_as_body() -> None:
    result = load_workflow(FIXTURES_DIR / "valid_no_fm.md")

    assert result.config == {}
    assert result.prompt_template.startswith("# Plain Prompt")
    assert "prompt body" in result.prompt_template


def test_load_workflow_invalid_yaml_raises_parse_error() -> None:
    with pytest.raises(WorkflowParseError):
        load_workflow(FIXTURES_DIR / "invalid_yaml.md")


def test_load_workflow_scalar_yaml_raises_front_matter_not_a_map() -> None:
    with pytest.raises(FrontMatterNotAMap):
        load_workflow(FIXTURES_DIR / "non_map_yaml.md")


def test_load_workflow_unterminated_front_matter_raises_parse_error(
    tmp_path: Path,
) -> None:
    """File starts with `---` but has no closing `---` → explicit error.

    Plan choice §34.4: explicit error beats silent fall-through to body-only.
    """
    path = tmp_path / "WORKFLOW.md"
    path.write_text("---\ntracker:\n  kind: linear\nbody without closing fence\n")

    with pytest.raises(WorkflowParseError) as excinfo:
        load_workflow(path)
    assert "closing" in str(excinfo.value).lower() or "front matter" in str(
        excinfo.value
    ).lower()


def test_load_workflow_trims_trailing_whitespace_from_body(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    path.write_text("---\ntracker:\n  kind: linear\n---\nBody text.\n\n\n   \n")

    result = load_workflow(path)
    assert result.prompt_template == "Body text."


def test_load_workflow_trims_leading_whitespace_from_body(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    path.write_text("---\ntracker:\n  kind: linear\n---\n\n\nBody text.\n")

    result = load_workflow(path)
    assert result.prompt_template == "Body text."


def test_load_workflow_empty_body_after_front_matter(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    path.write_text("---\ntracker:\n  kind: linear\n---\n")

    result = load_workflow(path)
    assert result.config["tracker"]["kind"] == "linear"
    assert result.prompt_template == ""


def test_load_workflow_empty_front_matter_yields_empty_config(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    path.write_text("---\n---\nBody only.\n")

    result = load_workflow(path)
    assert result.config == {}
    assert result.prompt_template == "Body only."


def test_load_workflow_only_dashes_on_first_line_count_as_front_matter(
    tmp_path: Path,
) -> None:
    """First line must be exactly `---` to enter front-matter mode.

    Lines like `--- something` or `----` are NOT front-matter delimiters.
    """
    path = tmp_path / "WORKFLOW.md"
    path.write_text("--- something\nrest of body\n")

    result = load_workflow(path)
    assert result.config == {}
    assert result.prompt_template.startswith("--- something")


def test_workflow_definition_is_dataclass_with_expected_fields() -> None:
    wd = WorkflowDefinition(config={"a": 1}, prompt_template="hi")
    assert wd.config == {"a": 1}
    assert wd.prompt_template == "hi"
