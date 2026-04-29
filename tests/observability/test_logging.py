"""Tests for ``river_gang.observability.logging``.

Covers SPED §13.1 (key=value formatting + REQUIRED context fields), §13.2 (sink
failure resilience — stdlib contract is preserved), and §15.3 (secret
redaction).
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator

import pytest

from river_gang.observability.logging import (
    configure_logging,
    issue_id_var,
    issue_identifier_var,
    redact_secrets,
    session_id_var,
)


@pytest.fixture()
def stream() -> io.StringIO:
    return io.StringIO()


@pytest.fixture(autouse=True)
def _reset_root_logger() -> Iterator[None]:
    """Snapshot/restore root logger handlers around each test.

    ``var.set(None)`` at fixture entry forces a known-clean starting state
    regardless of prior test pollution; the matching ``var.reset(token)``
    on teardown pops that frame so anything below it survives unchanged.
    """

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    issue_id_token = issue_id_var.set(None)
    issue_identifier_token = issue_identifier_var.set(None)
    session_id_token = session_id_var.set(None)
    try:
        yield
    finally:
        root.handlers = saved_handlers
        root.level = saved_level
        issue_id_var.reset(issue_id_token)
        issue_identifier_var.reset(issue_identifier_token)
        session_id_var.reset(session_id_token)


def test_configure_logging_sets_root_level_and_key_value_formatter(
    stream: io.StringIO,
) -> None:
    configure_logging(level=logging.INFO, stream=stream)
    root = logging.getLogger()
    assert root.level == logging.INFO

    logger = logging.getLogger("river_gang.test")
    logger.info("workflow_started")

    line = stream.getvalue().strip()
    # key=value phrasing: level=..., logger=..., msg=...
    assert "level=INFO" in line
    assert "logger=river_gang.test" in line
    assert "msg=workflow_started" in line


def test_context_vars_attached_when_set(stream: io.StringIO) -> None:
    configure_logging(level=logging.INFO, stream=stream)
    issue_id_var.set("abc-123")
    issue_identifier_var.set("MT-649")
    session_id_var.set("sess-1")

    logging.getLogger("river_gang.worker").info("issue_claimed")

    line = stream.getvalue()
    assert "issue_id=abc-123" in line
    assert "issue_identifier=MT-649" in line
    assert "session_id=sess-1" in line


def test_context_vars_absent_when_not_set(stream: io.StringIO) -> None:
    configure_logging(level=logging.INFO, stream=stream)
    logging.getLogger("river_gang.worker").info("startup")

    line = stream.getvalue()
    assert "issue_id=" not in line
    assert "issue_identifier=" not in line
    assert "session_id=" not in line


def test_partial_context_vars(stream: io.StringIO) -> None:
    configure_logging(level=logging.INFO, stream=stream)
    issue_identifier_var.set("MT-1")
    logging.getLogger("river_gang.worker").info("polled")
    line = stream.getvalue()
    assert "issue_identifier=MT-1" in line
    assert "issue_id=" not in line
    assert "session_id=" not in line


def test_value_with_space_is_quoted(stream: io.StringIO) -> None:
    configure_logging(level=logging.INFO, stream=stream)
    issue_identifier_var.set("rate limit hit")
    logging.getLogger("river_gang.worker").info("issue_failed")
    line = stream.getvalue()
    assert 'issue_identifier="rate limit hit"' in line


def test_value_with_newline_is_escaped_no_log_injection(stream: io.StringIO) -> None:
    """Control chars must be quoted+escaped to prevent log injection."""

    configure_logging(level=logging.INFO, stream=stream)
    issue_identifier_var.set("a\nlevel=ERROR injected")
    logging.getLogger("river_gang.worker").info("issue_failed")
    raw = stream.getvalue()
    # The newline is rendered as the literal two-char sequence ``\n``,
    # not as a real 0x0A — so the forged second log line cannot exist.
    assert 'issue_identifier="a\\nlevel=ERROR injected"' in raw
    # The actual byte 0x0A only appears as the StreamHandler terminator.
    body, _, trailing = raw.rpartition("\n")
    assert "\n" not in body
    assert trailing == ""


def test_format_value_escapes_cr_and_tab() -> None:
    from river_gang.observability.logging import _format_value

    assert _format_value("a\nb") == '"a\\nb"'
    assert _format_value("a\rb") == '"a\\rb"'
    assert _format_value("a\tb") == '"a\\tb"'


def test_sink_failure_does_not_raise(stream: io.StringIO) -> None:
    """SPED §13.2 + stdlib contract: a failing stream sink does not bubble.

    This locks in stdlib's default behavior (``Handler.handleError`` swallows
    exceptions at runtime) — i.e. our configuration does not undo that shield.
    Multi-sink fallback (the §13.2 SHOULD-clause) is out of scope for M0.
    """

    class ExplodingStream:
        def write(self, _data: str) -> int:
            raise RuntimeError("disk full")

        def flush(self) -> None:  # pragma: no cover - not reached
            pass

    configure_logging(level=logging.INFO, stream=ExplodingStream())  # type: ignore[arg-type]
    # Must not raise.
    logging.getLogger("river_gang").info("still_alive")


def test_configure_logging_idempotent(stream: io.StringIO) -> None:
    first = io.StringIO()
    second = io.StringIO()
    configure_logging(level=logging.INFO, stream=first)
    configure_logging(level=logging.INFO, stream=second)
    logging.getLogger("river_gang").info("once")
    # Second configure replaces the first sink: only the second receives.
    assert "msg=once" in second.getvalue()
    assert "msg=once" not in first.getvalue()
    assert second.getvalue().count("msg=once") == 1


# --- redact_secrets ---------------------------------------------------------


def test_redact_secrets_redacts_linear_api_key() -> None:
    out = redact_secrets({"LINEAR_API_KEY": "lin_api_xxx", "ok": "value"})
    assert out["LINEAR_API_KEY"] == "***"
    assert out["ok"] == "value"


def test_redact_secrets_redacts_token_and_api_key_substrings() -> None:
    out = redact_secrets(
        {
            "auth_token": "abcd",
            "service_api_key": "k",
            "tracker_api_KEY_extra": "k2",
            "harmless": "v",
        }
    )
    assert out["auth_token"] == "***"
    assert out["service_api_key"] == "***"
    assert out["tracker_api_KEY_extra"] == "***"
    assert out["harmless"] == "v"


def test_redact_secrets_token_accounting_keys_not_redacted() -> None:
    """SPED §13.5 token-accounting fields must NOT collide with §15.3 secrets."""

    out = redact_secrets(
        {
            "total_tokens": 1234,
            "token_count": 99,
            "last_token_usage": {"input_tokens": 10, "output_tokens": 20},
            "input_tokens": 10,
            "output_tokens": 20,
        }
    )
    assert out["total_tokens"] == 1234
    assert out["token_count"] == 99
    assert out["last_token_usage"]["input_tokens"] == 10
    assert out["last_token_usage"]["output_tokens"] == 20
    assert out["input_tokens"] == 10
    assert out["output_tokens"] == 20


def test_redact_secrets_extra_secret_keys() -> None:
    out = redact_secrets(
        {
            "password": "p",
            "secret": "s",
            "authorization": "Bearer xyz",
            "client_secret": "cs",
            "refresh_token": "rt",
            "bearer_token": "bt",
            "access_token": "at",
        }
    )
    assert out["password"] == "***"
    assert out["secret"] == "***"
    assert out["authorization"] == "***"
    assert out["client_secret"] == "***"
    assert out["refresh_token"] == "***"
    assert out["bearer_token"] == "***"
    assert out["access_token"] == "***"


def test_redact_secrets_nested_dict() -> None:
    out = redact_secrets(
        {
            "tracker": {"LINEAR_API_KEY": "secret", "endpoint": "https://x"},
            "agent": {"opts": {"api_key": "k"}},
        }
    )
    assert out["tracker"]["LINEAR_API_KEY"] == "***"
    assert out["tracker"]["endpoint"] == "https://x"
    assert out["agent"]["opts"]["api_key"] == "***"


def test_redact_secrets_passthrough_for_strings() -> None:
    # Pre-rendered strings are out of scope: redact dicts BEFORE serialising.
    assert redact_secrets("hello world") == "hello world"
    assert redact_secrets("api_key=xxx") == "api_key=xxx"


def test_redact_secrets_does_not_mutate_input() -> None:
    src = {"LINEAR_API_KEY": "x", "nested": {"api_key": "y"}}
    redact_secrets(src)
    assert src["LINEAR_API_KEY"] == "x"
    assert src["nested"]["api_key"] == "y"


def test_redact_secrets_lists_in_dict() -> None:
    out = redact_secrets({"creds": [{"api_key": "k"}, {"ok": "v"}]})
    assert out["creds"][0]["api_key"] == "***"
    assert out["creds"][1]["ok"] == "v"


def test_redact_secrets_empty_string_and_none_values() -> None:
    """Lock in: secret keys with empty/None values still render as ``***``."""

    out = redact_secrets({"api_key": "", "LINEAR_API_KEY": None, "ok": ""})
    assert out["api_key"] == "***"
    assert out["LINEAR_API_KEY"] == "***"
    assert out["ok"] == ""


def test_redact_secrets_handles_cyclic_dict() -> None:
    """Cyclic dicts must not blow the stack and must not leak the un-redacted
    original via the cycle position."""

    d: dict[str, object] = {"api_key": "k", "ok": "v"}
    d["self"] = d
    out = redact_secrets(d)  # must not raise RecursionError
    assert isinstance(out, dict)
    assert out["api_key"] == "***"
    assert out["ok"] == "v"
    # Cycle position replaced with sentinel — no un-redacted reference reachable.
    assert out["self"] == "<cycle>"
    # Secret keys at the cycle target are NOT exposed via the returned tree:
    # ``out["self"]`` is a plain string, not the original dict carrying ``api_key``.
    assert not isinstance(out["self"], dict)


def test_redact_secrets_handles_cyclic_list() -> None:
    """Cyclic lists must not blow the stack and must not leak via the cycle
    position."""

    inner: list[object] = [{"api_key": "k"}]
    inner.append(inner)
    out = redact_secrets({"items": inner})  # must not raise RecursionError
    assert isinstance(out, dict)
    items = out["items"]
    assert isinstance(items, list)
    assert items[0]["api_key"] == "***"
    # The self-reference slot is replaced with the sentinel, not the original list.
    assert items[1] == "<cycle>"
    assert not isinstance(items[1], list)


def test_redact_secrets_dag_dict_shared_subdict() -> None:
    """DAG: same dict referenced under two keys must be redacted in BOTH branches."""

    shared = {"api_key": "LEAK", "ok": "v"}
    out = redact_secrets({"a": shared, "b": shared})
    assert out["a"]["api_key"] == "***"
    assert out["b"]["api_key"] == "***"
    assert out["a"]["ok"] == "v"
    assert out["b"]["ok"] == "v"


def test_redact_secrets_dag_list_shared_subdict() -> None:
    """DAG: same dict referenced from two list positions must be redacted in both."""

    shared = {"api_key": "LEAK"}
    out = redact_secrets([shared, shared])
    assert isinstance(out, list)
    assert out[0]["api_key"] == "***"
    assert out[1]["api_key"] == "***"


def test_redact_secrets_dag_mixed_dict_and_list() -> None:
    """DAG: shared sub-dict reachable via both a dict key and a list slot."""

    shared = {"api_key": "LEAK", "ok": "v"}
    out = redact_secrets({"direct": shared, "via_list": [shared, {"other": shared}]})
    assert out["direct"]["api_key"] == "***"
    assert out["via_list"][0]["api_key"] == "***"
    assert out["via_list"][1]["other"]["api_key"] == "***"


def test_context_filter_overrides_extra_with_contextvar(stream: io.StringIO) -> None:
    """SPED §13.1: contextvar wins over caller-supplied ``extra``."""

    configure_logging(level=logging.INFO, stream=stream)
    issue_id_var.set("ABC")
    logging.getLogger("river_gang.worker").info("x", extra={"issue_id": "WRONG"})
    line = stream.getvalue()
    assert "issue_id=ABC" in line
    assert "WRONG" not in line


def test_context_filter_suppresses_extra_when_contextvar_unset(
    stream: io.StringIO,
) -> None:
    """SPED §13.1: reserved names from ``extra`` are stripped when contextvar is unset."""

    configure_logging(level=logging.INFO, stream=stream)
    logging.getLogger("river_gang.worker").info(
        "x", extra={"issue_id": "LEAKED_FROM_EXTRA"}
    )
    line = stream.getvalue()
    assert "LEAKED_FROM_EXTRA" not in line
    assert "issue_id=" not in line
