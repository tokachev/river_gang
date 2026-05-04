"""Normalized Linear comment model + parser."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from river_gang.tracker.errors import IssueMissingRequiredField


@dataclass(frozen=True)
class Comment:
    """Normalized Linear issue comment."""

    id: str
    body: str
    created_at: datetime | None
    updated_at: datetime | None
    user_id: str | None
    user_name: str | None


def parse_comment(payload: dict[str, Any]) -> Comment:
    """Normalize a Linear comment node into :class:`Comment`."""

    return Comment(
        id=_require_str(payload, "id"),
        body=_require_str(payload, "body"),
        created_at=_parse_iso8601(payload.get("createdAt")),
        updated_at=_parse_iso8601(payload.get("updatedAt")),
        user_id=_extract_user_id(payload.get("user")),
        user_name=_extract_user_name(payload.get("user")),
    )


def _require_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or value == "":
        raise IssueMissingRequiredField(
            f"comment payload missing REQUIRED field {key!r}"
        )
    return value


def _extract_user_id(user_obj: Any) -> str | None:
    if not isinstance(user_obj, dict):
        return None
    user_id = user_obj.get("id")
    if isinstance(user_id, str) and user_id != "":
        return user_id
    return None


def _extract_user_name(user_obj: Any) -> str | None:
    if not isinstance(user_obj, dict):
        return None
    for key in ("displayName", "name"):
        name = user_obj.get(key)
        if isinstance(name, str) and name != "":
            return name
    return None


def _parse_iso8601(value: Any) -> datetime | None:
    if not isinstance(value, str) or value == "":
        return None
    candidate = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


__all__ = ["Comment", "parse_comment"]
