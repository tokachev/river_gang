"""Token accounting + rate-limit extraction (SPED §13.5).

Public entry points:

- :func:`extract_cumulative_tokens` — pulls an absolute :class:`TokenSnapshot`
  from a notification's ``method`` + ``params``, applying §13.5's selection
  rules:

  * ``thread/tokenUsage/updated``                — payload IS the cumulative
  * any event carrying ``total_token_usage``     — that subdict is cumulative
  * ``last_token_usage`` (key or method)         — IGNORE (delta)
  * generic ``usage`` map on an unrelated event  — IGNORE (not cumulative)

- :func:`compute_token_delta` — given the previous and current cumulative
  snapshots, returns the per-event delta with negatives clamped to zero so
  a server-side reset can't corrupt aggregates downstream.

- :func:`extract_rate_limits` — pulls a :class:`RateLimitSnapshot` from any
  payload that carries ``rate_limit`` / ``rateLimit``. Returns ``None`` when
  no rate-limit info is present so callers can short-circuit.

Field-name lookup is lenient (snake_case + camelCase both accepted) per
§13.5 ("extract … leniently from common field names").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Method name signalling the spec-defined cumulative payload (§13.5).
METHOD_TOKEN_USAGE_UPDATED = "thread/tokenUsage/updated"

# Field-name aliases. First hit wins so put the snake_case variant first.
_INPUT_KEYS = ("input_tokens", "inputTokens")
_OUTPUT_KEYS = ("output_tokens", "outputTokens")
_TOTAL_KEYS = ("total_tokens", "totalTokens")
_TOTAL_USAGE_KEYS = ("total_token_usage", "totalTokenUsage")
_LAST_USAGE_KEYS = ("last_token_usage", "lastTokenUsage")
_RATE_LIMIT_KEYS = ("rate_limit", "rateLimit")
_RATE_LIMIT_LIMIT = ("limit",)
_RATE_LIMIT_REMAINING = ("remaining",)
_RATE_LIMIT_RESET = ("reset_at", "resetAt")


@dataclass(frozen=True)
class TokenSnapshot:
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class RateLimitSnapshot:
    limit: int | None
    remaining: int | None
    reset_at: str | None


# ---------------------------------------------------------------------------
# extract_cumulative_tokens
# ---------------------------------------------------------------------------


def extract_cumulative_tokens(
    method: str, payload: dict[str, Any]
) -> TokenSnapshot | None:
    """Return the cumulative :class:`TokenSnapshot` carried by this event,
    or ``None`` if the event doesn't carry one (delta-only, generic usage,
    no token info, malformed values).
    """
    # §13.5: ``last_token_usage`` is a delta — never report as cumulative.
    if method in _LAST_USAGE_KEYS:
        return None

    # 1. Method-level signal — payload itself is the cumulative.
    if method == METHOD_TOKEN_USAGE_UPDATED:
        # Use explicit ``is None`` chaining so an outer payload that legitimately
        # carries an all-zero snapshot is preferred over the nested
        # ``payload["usage"]`` fallback. (Relying on Python truthiness here
        # works only because TokenSnapshot has no ``__bool__`` override —
        # too brittle to depend on.)
        outer = _read_token_object(payload)
        if outer is not None:
            return outer
        nested = payload.get("usage")
        if isinstance(nested, dict):
            return _read_token_object(nested)
        return None

    # 2. Wrapper-level signal — any event with ``total_token_usage`` exposes
    # cumulative totals inside that subdict regardless of the method name.
    wrapper = _first_present(payload, _TOTAL_USAGE_KEYS)
    if isinstance(wrapper, dict):
        return _read_token_object(wrapper)

    # 3. Generic ``usage`` maps on unrelated events are NOT cumulative.
    return None


def _read_token_object(obj: dict[str, Any] | None) -> TokenSnapshot | None:
    if not isinstance(obj, dict):
        return None

    input_present, input_value = _read_int_field(obj, _INPUT_KEYS)
    output_present, output_value = _read_int_field(obj, _OUTPUT_KEYS)
    total_present, total_value = _read_int_field(obj, _TOTAL_KEYS)

    # If a declared field is present but malformed (bool, string, etc.) we
    # reject the snapshot entirely — emitting a half-zero count would be
    # worse than reporting nothing.
    if input_present == "invalid" or output_present == "invalid" or total_present == "invalid":
        return None

    if input_present == "absent" and output_present == "absent" and total_present == "absent":
        return None

    input_tokens = input_value if input_present == "ok" else 0
    output_tokens = output_value if output_present == "ok" else 0
    total_tokens = (
        total_value if total_present == "ok" else input_tokens + output_tokens
    )

    return TokenSnapshot(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _read_int_field(
    obj: dict[str, Any], keys: tuple[str, ...]
) -> tuple[str, int]:
    """Lookup ``obj`` across ``keys`` aliases. Returns ``("ok", value)`` on
    success, ``("absent", 0)`` when no key matched, ``("invalid", 0)`` when
    a key matched but the value cannot be a token count (bool, str, etc.).
    """
    for k in keys:
        if k in obj:
            value = obj[k]
            coerced = _coerce_token_int(value)
            if coerced is None:
                return ("invalid", 0)
            return ("ok", coerced)
    return ("absent", 0)


def _coerce_token_int(value: Any) -> int | None:
    """Strict int extraction: bool is excluded, anything non-int returns None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _first_present(obj: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in obj:
            return obj[k]
    return None


# ---------------------------------------------------------------------------
# compute_token_delta
# ---------------------------------------------------------------------------


def compute_token_delta(
    *, previous: TokenSnapshot | None, current: TokenSnapshot
) -> TokenSnapshot:
    """Per-event delta against the previously-reported cumulative snapshot.

    First report (``previous=None``) is the full snapshot. Negative deltas
    (server-side reset, retry, etc.) clamp to zero so running aggregates
    can't go backwards. Equal snapshots return all-zeros.
    """
    if previous is None:
        return current
    return TokenSnapshot(
        input_tokens=max(current.input_tokens - previous.input_tokens, 0),
        output_tokens=max(current.output_tokens - previous.output_tokens, 0),
        total_tokens=max(current.total_tokens - previous.total_tokens, 0),
    )


# ---------------------------------------------------------------------------
# extract_rate_limits
# ---------------------------------------------------------------------------


def extract_rate_limits(payload: dict[str, Any]) -> RateLimitSnapshot | None:
    """Return :class:`RateLimitSnapshot` if ``payload`` carries a rate-limit
    object, otherwise ``None``.
    """
    raw = _first_present(payload, _RATE_LIMIT_KEYS)
    if not isinstance(raw, dict) or not raw:
        return None

    limit = _coerce_token_int(_first_present(raw, _RATE_LIMIT_LIMIT))
    remaining = _coerce_token_int(_first_present(raw, _RATE_LIMIT_REMAINING))
    reset_value = _first_present(raw, _RATE_LIMIT_RESET)
    reset_at = reset_value if isinstance(reset_value, str) else None

    if limit is None and remaining is None and reset_at is None:
        return None

    return RateLimitSnapshot(limit=limit, remaining=remaining, reset_at=reset_at)


__all__ = [
    "METHOD_TOKEN_USAGE_UPDATED",
    "RateLimitSnapshot",
    "TokenSnapshot",
    "compute_token_delta",
    "extract_cumulative_tokens",
    "extract_rate_limits",
]
