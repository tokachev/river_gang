"""Comment normalization tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from river_gang.tracker.comment import parse_comment
from river_gang.tracker.errors import IssueMissingRequiredField


def test_parse_comment_normalizes_linear_comment_payload() -> None:
    comment = parse_comment(
        {
            "id": "comment-1",
            "body": "Looks good",
            "createdAt": "2026-05-04T12:00:00.000Z",
            "updatedAt": "2026-05-04T12:01:00.000Z",
            "user": {"id": "user-1", "displayName": "Artem", "name": "ignored"},
        }
    )

    assert comment.id == "comment-1"
    assert comment.body == "Looks good"
    assert comment.created_at == datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
    assert comment.updated_at == datetime(2026, 5, 4, 12, 1, tzinfo=UTC)
    assert comment.user_id == "user-1"
    assert comment.user_name == "Artem"


def test_parse_comment_falls_back_to_user_name() -> None:
    comment = parse_comment(
        {"id": "comment-1", "body": "Answer", "user": {"name": "Bot"}}
    )

    assert comment.user_name == "Bot"


@pytest.mark.parametrize("missing", ["id", "body"])
def test_parse_comment_requires_id_and_body(missing: str) -> None:
    payload = {"id": "comment-1", "body": "hello"}
    payload.pop(missing)

    with pytest.raises(IssueMissingRequiredField):
        parse_comment(payload)
