"""Normalized :class:`Issue` model + parser (SPED §4.1.1, §11.3).

:func:`parse_issue` accepts a raw Linear GraphQL response node and returns a
frozen :class:`Issue`. Required-field guard raises
:class:`river_gang.tracker.errors.IssueMissingRequiredField` so downstream
eligibility logic can assume populated REQUIRED scalars.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from river_gang.tracker.errors import IssueMissingRequiredField

_BLOCKS_RELATION = "blocks"


@dataclass(frozen=True)
class BlockerRef:
    """Inverse-relation blocker pointer (§4.1.1)."""

    id: str | None
    identifier: str | None
    state: str | None


@dataclass(frozen=True)
class Issue:
    """Normalized issue record (§4.1.1)."""

    id: str
    identifier: str
    title: str
    state: str
    description: str | None
    priority: int | None
    branch_name: str | None
    url: str | None
    labels: tuple[str, ...]
    blocked_by: tuple[BlockerRef, ...]
    created_at: datetime | None
    updated_at: datetime | None


def parse_issue(payload: dict[str, Any]) -> Issue:
    """Normalize a Linear issue node into :class:`Issue`.

    Raises:
        IssueMissingRequiredField: any of ``id``/``identifier``/``title``/
            ``state.name`` is missing, ``None``, or empty.
    """

    issue_id = _require_str(payload, "id")
    identifier = _require_str(payload, "identifier")
    title = _require_str(payload, "title")
    state = _require_state_name(payload)

    return Issue(
        id=issue_id,
        identifier=identifier,
        title=title,
        state=state,
        description=_optional_str(payload.get("description")),
        priority=_coerce_priority(payload.get("priority")),
        branch_name=_optional_str(payload.get("branchName")),
        url=_optional_str(payload.get("url")),
        labels=_extract_labels(payload.get("labels")),
        blocked_by=_extract_blockers(payload.get("inverseRelations")),
        created_at=_parse_iso8601(payload.get("createdAt")),
        updated_at=_parse_iso8601(payload.get("updatedAt")),
    )


# ---------------------------------------------------------------------------
# Required-field helpers
# ---------------------------------------------------------------------------


def _require_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or value == "":
        raise IssueMissingRequiredField(
            f"issue payload missing REQUIRED field {key!r}"
        )
    return value


def _require_state_name(payload: dict[str, Any]) -> str:
    state_obj = payload.get("state")
    if not isinstance(state_obj, dict):
        raise IssueMissingRequiredField(
            "issue payload missing REQUIRED field 'state'"
        )
    name = state_obj.get("name")
    if not isinstance(name, str) or name == "":
        raise IssueMissingRequiredField(
            "issue payload missing REQUIRED field 'state.name'"
        )
    return name


# ---------------------------------------------------------------------------
# Optional-field helpers
# ---------------------------------------------------------------------------


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value != "":
        return value
    return None


def _coerce_priority(value: Any) -> int | None:
    """SPED §11.3: integer only, non-int (incl. float and bool) → None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _extract_labels(labels_obj: Any) -> tuple[str, ...]:
    if not isinstance(labels_obj, dict):
        return ()
    nodes = labels_obj.get("nodes")
    if not isinstance(nodes, list):
        return ()
    out: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        name = node.get("name")
        if isinstance(name, str) and name != "":
            out.append(name.lower())
    return tuple(out)


def _extract_blockers(inverse_obj: Any) -> tuple[BlockerRef, ...]:
    """Pick up nodes where ``type == "blocks"`` per §11.3.

    Each blocker ref carries the *blocking* issue's ``id``/``identifier``/
    ``state.name`` (or ``None`` if absent). State is preserved verbatim —
    terminal-state filtering happens in eligibility logic, not here.
    """
    if not isinstance(inverse_obj, dict):
        return ()
    nodes = inverse_obj.get("nodes")
    if not isinstance(nodes, list):
        return ()

    out: list[BlockerRef] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("type") != _BLOCKS_RELATION:
            continue
        blocker_issue = node.get("issue")
        if not isinstance(blocker_issue, dict):
            continue
        out.append(
            BlockerRef(
                id=_optional_str(blocker_issue.get("id")),
                identifier=_optional_str(blocker_issue.get("identifier")),
                state=_extract_state_name(blocker_issue.get("state")),
            )
        )
    return tuple(out)


def _extract_state_name(state_obj: Any) -> str | None:
    if not isinstance(state_obj, dict):
        return None
    name = state_obj.get("name")
    if isinstance(name, str) and name != "":
        return name
    return None


def _parse_iso8601(value: Any) -> datetime | None:
    if not isinstance(value, str) or value == "":
        return None
    # ``datetime.fromisoformat`` in 3.11+ accepts the trailing ``Z`` only via
    # explicit replacement; keep the substitution minimal so we don't mask
    # legitimately malformed timestamps.
    candidate = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None
