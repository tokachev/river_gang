"""Dispatch preflight validation (SPED §6.3, §5.3.4, §5.3.5).

Run after :func:`river_gang.config.resolution.resolve_and_validate` produces
an :class:`EffectiveConfig`. This layer checks REQUIRED-field presence and
spec-mandated range constraints; it does NOT coerce types (Task 4 owns
coercion) or resolve ``$VAR`` (Task 5 owns resolution).

Result is non-throwing — callers decide whether to abort startup or skip
the current dispatch tick (§6.3 distinguishes these two contexts).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from river_gang.config.schema import EffectiveConfig

SUPPORTED_TRACKER_KINDS: frozenset[str] = frozenset({"linear"})


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)


def validate_for_dispatch(config: EffectiveConfig) -> ValidationResult:
    """Validate ``config`` against §6.3 dispatch preflight rules.

    Errors are accumulated — callers see every problem in one pass instead
    of stopping at the first failure.
    """

    errors: list[str] = []

    kind = config.tracker.kind
    if kind is None or kind == "":
        errors.append("tracker.kind: missing or empty (REQUIRED)")
    elif kind not in SUPPORTED_TRACKER_KINDS:
        supported = ", ".join(sorted(SUPPORTED_TRACKER_KINDS))
        errors.append(
            f"tracker.kind: unsupported value {kind!r} (supported: {supported})"
        )

    api_key = config.tracker.api_key
    if api_key is None or api_key == "":
        errors.append(
            "tracker.api_key: missing after $-resolution (set LINEAR_API_KEY or "
            "provide a literal token)"
        )

    # ``project_slug`` is only REQUIRED for the linear tracker (§6.3, §5.3.1).
    if kind == "linear":
        slug = config.tracker.project_slug
        if slug is None or slug == "":
            errors.append(
                "tracker.project_slug: REQUIRED when tracker.kind=linear"
            )

    if config.codex.command == "":
        errors.append("codex.command: must be a non-empty shell command")

    # §5.3.5 — ``agent.max_turns`` must be a positive integer. Type coercion
    # already happened in Task 4; here we only enforce the positive range.
    if config.agent.max_turns <= 0:
        errors.append(
            f"agent.max_turns: must be a positive integer "
            f"(got {config.agent.max_turns})"
        )

    # §5.3.4 — ``hooks.timeout_ms`` must be a positive integer.
    if config.hooks.timeout_ms <= 0:
        errors.append(
            f"hooks.timeout_ms: must be a positive integer "
            f"(got {config.hooks.timeout_ms})"
        )

    return ValidationResult(ok=not errors, errors=errors)


def format_error_for_operator(result: ValidationResult) -> str:
    """Render a multi-line operator-visible summary.

    Empty string when ``result.ok`` so log sites can guard with ``if msg:``.
    """

    if result.ok:
        return ""

    count = len(result.errors)
    plural = "s" if count != 1 else ""
    header = (
        f"Workflow configuration failed dispatch preflight "
        f"({count} error{plural}):"
    )
    bullets = [f"  - {err}" for err in result.errors]
    return "\n".join([header, *bullets])
