"""Tests for :func:`parse_issue` (SPED §4.1.1, §11.3)."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from river_gang.tracker.errors import IssueMissingRequiredField
from river_gang.tracker.issue import BlockerRef, Issue, parse_issue

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Dataclass shape
# ---------------------------------------------------------------------------


def test_issue_is_frozen_dataclass() -> None:
    assert dataclasses.is_dataclass(Issue)
    assert Issue.__dataclass_params__.frozen is True


def test_blocker_ref_is_frozen_dataclass() -> None:
    assert dataclasses.is_dataclass(BlockerRef)
    assert BlockerRef.__dataclass_params__.frozen is True


# ---------------------------------------------------------------------------
# Happy-path parses
# ---------------------------------------------------------------------------


def test_parse_basic_issue() -> None:
    issue = parse_issue(_load("issue_basic.json"))

    assert issue.id == "issue-uuid-1"
    assert issue.identifier == "RG-1"
    assert issue.title == "Implement workflow loader"
    assert issue.description == "Parse WORKFLOW.md front matter and body."
    assert issue.priority == 2
    assert issue.state == "Todo"
    assert issue.branch_name == "rg-1-implement-workflow-loader"
    assert issue.url == "https://linear.app/river-gang/issue/RG-1"
    assert issue.labels == ()
    assert issue.blocked_by == ()
    assert issue.created_at == datetime(2026, 4, 1, 10, 30, 0, tzinfo=UTC)
    assert issue.updated_at == datetime(2026, 4, 2, 11, 45, 30, tzinfo=UTC)


def test_parse_issue_with_labels_lowercases_them() -> None:
    issue = parse_issue(_load("issue_with_labels.json"))
    # Spec §11.3: labels lowercased; order from input preserved.
    assert issue.labels == ("bug", "backend", "good-first-issue")


def test_parse_issue_no_priority_yields_none() -> None:
    issue = parse_issue(_load("issue_no_priority.json"))
    assert issue.priority is None


def test_parse_issue_state_extracted_from_nested_object() -> None:
    issue = parse_issue(_load("issue_basic.json"))
    assert issue.state == "Todo"


# ---------------------------------------------------------------------------
# Blockers
# ---------------------------------------------------------------------------


def test_parse_issue_extracts_blockers_from_inverse_relations() -> None:
    issue = parse_issue(_load("issue_with_blockers.json"))

    assert len(issue.blocked_by) == 2
    assert issue.blocked_by[0] == BlockerRef(
        id="blocker-uuid-1", identifier="RG-99", state="In Progress"
    )
    assert issue.blocked_by[1] == BlockerRef(
        id="blocker-uuid-2", identifier="RG-100", state="Done"
    )


def test_parse_issue_blockers_filter_only_blocks_relation_type() -> None:
    """Inverse relations of type ``duplicate`` MUST NOT appear in blocked_by."""
    issue = parse_issue(_load("issue_with_blockers.json"))
    blocker_ids = {b.id for b in issue.blocked_by}
    assert "dupe-uuid" not in blocker_ids


def test_parse_issue_terminal_blocker_state_preserved() -> None:
    """SPED §4.1.1: blocker state is reported as-is. Terminal-state filtering
    happens later in eligibility logic, NOT at parse time."""
    issue = parse_issue(_load("issue_with_blockers.json"))
    states = [b.state for b in issue.blocked_by]
    # Includes the terminal "Done" — parse_issue must not drop it.
    assert "Done" in states
    assert "In Progress" in states


def test_parse_issue_blocker_with_partial_fields_allows_nulls() -> None:
    payload = _valid_payload()
    payload["inverseRelations"] = {
        "nodes": [
            {"type": "blocks", "issue": {"id": None, "identifier": None, "state": None}}
        ]
    }
    issue = parse_issue(payload)
    assert issue.blocked_by == (BlockerRef(id=None, identifier=None, state=None),)


def test_parse_issue_blocker_state_is_object() -> None:
    """``state`` on a blocker may arrive as ``{"name": "X"}`` or as null."""
    payload = _valid_payload()
    payload["inverseRelations"] = {
        "nodes": [
            {
                "type": "blocks",
                "issue": {
                    "id": "b1",
                    "identifier": "RG-100",
                    "state": {"name": "Cancelled"},
                },
            }
        ]
    }
    issue = parse_issue(payload)
    assert issue.blocked_by == (
        BlockerRef(id="b1", identifier="RG-100", state="Cancelled"),
    )


def test_parse_issue_no_inverse_relations_yields_empty_blocked_by() -> None:
    payload = _valid_payload()
    payload["inverseRelations"] = {"nodes": []}
    issue = parse_issue(payload)
    assert issue.blocked_by == ()


def test_parse_issue_missing_inverse_relations_key_yields_empty_blocked_by() -> None:
    payload = _valid_payload()
    payload.pop("inverseRelations", None)
    issue = parse_issue(payload)
    assert issue.blocked_by == ()


# ---------------------------------------------------------------------------
# Optional-field fallbacks
# ---------------------------------------------------------------------------


def test_parse_issue_missing_optional_fields_yield_none() -> None:
    payload = {
        "id": "i",
        "identifier": "RG-9",
        "title": "T",
        "state": {"name": "Todo"},
    }
    issue = parse_issue(payload)
    assert issue.description is None
    assert issue.priority is None
    assert issue.branch_name is None
    assert issue.url is None
    assert issue.labels == ()
    assert issue.blocked_by == ()
    assert issue.created_at is None
    assert issue.updated_at is None


def test_parse_issue_description_populated_when_present() -> None:
    payload = _valid_payload()
    payload["description"] = "hello"
    issue = parse_issue(payload)
    assert issue.description == "hello"


def test_parse_issue_explicit_null_description_yields_none() -> None:
    payload = _valid_payload()
    payload["description"] = None
    issue = parse_issue(payload)
    assert issue.description is None


# ---------------------------------------------------------------------------
# Priority edge cases (§11.3: non-int -> null)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["high", 1.5, [1], {"x": 1}])
def test_parse_issue_non_int_priority_becomes_none(bad: Any) -> None:
    payload = _valid_payload()
    payload["priority"] = bad
    issue = parse_issue(payload)
    assert issue.priority is None


def test_parse_issue_bool_priority_becomes_none() -> None:
    """``True``/``False`` are int subclasses but never a real priority."""
    payload = _valid_payload()
    payload["priority"] = True
    issue = parse_issue(payload)
    assert issue.priority is None


@pytest.mark.parametrize("good", [0, 1, 2, 3, 4])
def test_parse_issue_int_priority_preserved(good: int) -> None:
    payload = _valid_payload()
    payload["priority"] = good
    issue = parse_issue(payload)
    assert issue.priority == good


# ---------------------------------------------------------------------------
# Timestamp parsing (ISO-8601 incl. trailing 'Z')
# ---------------------------------------------------------------------------


def test_parse_issue_iso8601_with_z_suffix() -> None:
    payload = _valid_payload()
    payload["createdAt"] = "2026-04-01T10:30:00Z"
    payload["updatedAt"] = "2026-04-02T11:45:30Z"
    issue = parse_issue(payload)
    assert issue.created_at == datetime(2026, 4, 1, 10, 30, 0, tzinfo=UTC)
    assert issue.updated_at == datetime(2026, 4, 2, 11, 45, 30, tzinfo=UTC)


def test_parse_issue_iso8601_with_offset() -> None:
    payload = _valid_payload()
    payload["createdAt"] = "2026-04-01T13:30:00+03:00"
    issue = parse_issue(payload)
    assert issue.created_at is not None
    assert issue.created_at.utcoffset() is not None


def test_parse_issue_invalid_timestamp_yields_none() -> None:
    payload = _valid_payload()
    payload["createdAt"] = "not-a-timestamp"
    payload["updatedAt"] = ""
    issue = parse_issue(payload)
    assert issue.created_at is None
    assert issue.updated_at is None


def test_parse_issue_null_timestamps_yield_none() -> None:
    payload = _valid_payload()
    payload["createdAt"] = None
    payload["updatedAt"] = None
    issue = parse_issue(payload)
    assert issue.created_at is None
    assert issue.updated_at is None


# ---------------------------------------------------------------------------
# Required-field guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing_field", ["id", "identifier", "title"])
def test_parse_issue_missing_required_scalar_raises(missing_field: str) -> None:
    payload = _valid_payload()
    del payload[missing_field]
    with pytest.raises(IssueMissingRequiredField) as exc_info:
        parse_issue(payload)
    assert missing_field in str(exc_info.value)


def test_parse_issue_missing_state_raises() -> None:
    payload = _valid_payload()
    del payload["state"]
    with pytest.raises(IssueMissingRequiredField) as exc_info:
        parse_issue(payload)
    assert "state" in str(exc_info.value)


def test_parse_issue_state_with_no_name_raises() -> None:
    payload = _valid_payload()
    payload["state"] = {}
    with pytest.raises(IssueMissingRequiredField):
        parse_issue(payload)


@pytest.mark.parametrize("bad", ["", None])
def test_parse_issue_empty_required_string_raises(bad: Any) -> None:
    payload = _valid_payload()
    payload["title"] = bad
    with pytest.raises(IssueMissingRequiredField):
        parse_issue(payload)


# ---------------------------------------------------------------------------
# Helper fixtures
# ---------------------------------------------------------------------------


def _valid_payload() -> dict[str, Any]:
    """Minimum-shape valid Linear issue payload for permutation tests."""
    return {
        "id": "i",
        "identifier": "RG-9",
        "title": "T",
        "state": {"name": "Todo"},
        "labels": {"nodes": []},
        "inverseRelations": {"nodes": []},
    }
