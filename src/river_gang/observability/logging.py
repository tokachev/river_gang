"""Structured logging foundation (SPED §13.1, §13.2, §15.3).

Logs use stable ``key=value`` formatting. REQUIRED issue/session context fields
are injected automatically from :mod:`contextvars` — there are no ``with_*``
helpers; callers set the relevant ``ContextVar`` directly.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import IO, Any, TypeVar, cast

# REQUIRED context fields per SPED §13.1. ``None`` means "not set" → omitted
# from the rendered log line.
issue_id_var: ContextVar[str | None] = ContextVar("issue_id", default=None)
issue_identifier_var: ContextVar[str | None] = ContextVar("issue_identifier", default=None)
session_id_var: ContextVar[str | None] = ContextVar("session_id", default=None)

# Pre-built (name, var) pairs consumed by ``_ContextFilter.filter``. Hoisted to
# module scope so the tuple is allocated once, not per-record. The contextvars
# above must already be defined at this point.
_CTX_PAIRS: tuple[tuple[str, ContextVar[str | None]], ...] = (
    ("issue_id", issue_id_var),
    ("issue_identifier", issue_identifier_var),
    ("session_id", session_id_var),
)


# Keys whose values must never be logged. SPED §15.3.
#
# Exact-match list of known secret field names. Substring matching is reserved
# for ``api_key`` only — bare ``token`` collides with §13.5 token-accounting
# fields (``total_tokens``, ``input_tokens``, ``last_token_usage`` …).
_SECRET_EXACT: frozenset[str] = frozenset(
    {
        "linear_api_key",
        "api_key",
        "auth_token",
        "access_token",
        "bearer_token",
        "refresh_token",
        "client_secret",
        "password",
        "secret",
        "authorization",
    }
)
# Substring needles compared against ``key.lower()`` — an exhaustive
# allowlist of common API/secret-key spelling variants. Bare ``token``
# is intentionally excluded because it collides with §13.5
# token-accounting fields (``total_tokens``, ``input_tokens``,
# ``last_token_usage`` …); add only forms whose keys reliably carry
# credentials. See docs/trust-posture.md for the full policy.
_SECRET_SUBSTRINGS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "api-key",
    "secret_key",
    "secret-key",
    "secretkey",
)
_SECRET_PLACEHOLDER = "***"


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _SECRET_EXACT:
        return True
    return any(needle in lowered for needle in _SECRET_SUBSTRINGS)


T = TypeVar("T")

_CYCLE_SENTINEL = "<cycle>"


def redact_secrets(value: T) -> T:
    """Redact secret-keyed values inside dicts and lists.

    Operates on structured Python values only. Pre-rendered ``key=value``
    strings are out of scope: callers must redact dicts BEFORE serialisation.

    Cyclic dict/list structures are detected via ``id()`` tracking; on a
    cycle hit the position is replaced with the string sentinel ``"<cycle>"``
    so the un-redacted reference is never reachable through the returned tree.
    """

    return cast(T, _redact(value, set()))


def _redact(value: Any, seen: set[int]) -> Any:
    if not isinstance(value, dict | list):
        return value
    if id(value) in seen:
        return _CYCLE_SENTINEL
    seen.add(id(value))
    try:
        if isinstance(value, dict):
            return {
                k: (
                    _SECRET_PLACEHOLDER
                    if isinstance(k, str) and _is_secret_key(k)
                    else _redact(v, seen)
                )
                for k, v in value.items()
            }
        return [_redact(item, seen) for item in value]
    finally:
        seen.discard(id(value))


class _ContextFilter(logging.Filter):
    """Inject ``contextvars`` values onto every record under fixed names."""

    def filter(self, record: logging.LogRecord) -> bool:
        # SPED §13.1: the contextvar is authoritative — overwrite any
        # caller-supplied ``extra={"issue_id": ...}`` so log lines reflect the
        # real worker context. When the contextvar is unset, strip any
        # caller-supplied value for the reserved name so it cannot leak.
        for name, var in _CTX_PAIRS:
            current = var.get()
            if current is not None:
                setattr(record, name, current)
            elif hasattr(record, name):
                delattr(record, name)
        return True


def _format_value(value: Any) -> str:
    text = str(value)
    needs_quote = any(c == " " or c == '"' or ord(c) < 32 for c in text)
    if needs_quote:
        escaped = (
            text.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'
    return text


class _KeyValueFormatter(logging.Formatter):
    """Emit a stable ``key=value`` line per SPED §13.1.

    Fixed columns: ``level``, ``logger``, ``msg``, plus the three §13.1 context
    fields when set. Arbitrary ``extra={}`` fields are intentionally NOT
    rendered in M0 — there is no producer yet, and a real consumer should
    drive an explicit allowlist + secret-key redaction at that point.
    """

    def format(self, record: logging.LogRecord) -> str:
        parts: list[str] = [
            f"level={record.levelname}",
            f"logger={record.name}",
            f"msg={_format_value(record.getMessage())}",
        ]

        for ctx_key, _ in _CTX_PAIRS:
            ctx_val = getattr(record, ctx_key, None)
            if ctx_val is not None:
                parts.append(f"{ctx_key}={_format_value(ctx_val)}")

        line = " ".join(parts)
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


@contextmanager
def set_log_context(
    *,
    issue_id: str | None = None,
    issue_identifier: str | None = None,
    session_id: str | None = None,
) -> Iterator[None]:
    """Set §13.1 log context fields for the wrapped block.

    Only ``ContextVar`` slots whose argument is non-``None`` are updated;
    the others remain at their inherited value. On exit, every set token
    is reset in reverse order so nested calls restore the outer scope
    cleanly.
    """
    tokens: list[tuple[ContextVar[str | None], Token[str | None]]] = []
    if issue_id is not None:
        tokens.append((issue_id_var, issue_id_var.set(issue_id)))
    if issue_identifier is not None:
        tokens.append(
            (issue_identifier_var, issue_identifier_var.set(issue_identifier))
        )
    if session_id is not None:
        tokens.append((session_id_var, session_id_var.set(session_id)))
    try:
        yield
    finally:
        # Reset in reverse so nested set/reset pairs unwind cleanly.
        for var, token in reversed(tokens):
            var.reset(token)


_CONFIGURED_MARKER = "_river_gang_configured"


def configure_logging(
    *,
    level: int = logging.INFO,
    stream: IO[str] | None = None,
) -> None:
    """Install a single key=value stream handler on the root logger.

    Idempotent — repeated calls replace the prior river_gang handler.
    """

    root = logging.getLogger()
    root.setLevel(level)

    # Drop any handler this function previously installed so re-configuration
    # does not duplicate output.
    for handler in list(root.handlers):
        if getattr(handler, _CONFIGURED_MARKER, False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(_KeyValueFormatter())
    handler.addFilter(_ContextFilter())
    setattr(handler, _CONFIGURED_MARKER, True)
    root.addHandler(handler)
