"""Apply SPED §6.4 defaults and coerce types into :class:`EffectiveConfig`.

Validation of ranges (e.g. ``max_turns > 0``, required ``tracker.kind``) is
NOT performed here — that lives in the dispatch preflight (Task 6). This
module only:

- supplies defaults from §6.4,
- coerces stringified integers (``"30000"`` → ``30000``),
- normalises ``agent.max_concurrent_agents_by_state`` keys to lowercase and
  drops invalid entries (§5.3.5),
- silently ignores unknown top-level / nested keys for forward compat (§5.3).

Invalid types that cannot be safely coerced surface as
:class:`ConfigCoercionError` so the operator gets an explicit failure rather
than a silent fallback to defaults.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from river_gang.config.schema import (
    AgentConfig,
    CodexConfig,
    EffectiveConfig,
    HooksConfig,
    PollingConfig,
    TrackerConfig,
    WorkspaceConfig,
)


class ConfigCoercionError(ValueError):
    """Raised when a config value cannot be coerced to its declared type."""


# SPED §6.4 defaults.
DEFAULT_TRACKER_ENDPOINT = "https://api.linear.app/graphql"
DEFAULT_ACTIVE_STATES: tuple[str, ...] = ("Todo", "In Progress")
DEFAULT_TERMINAL_STATES: tuple[str, ...] = (
    "Closed",
    "Cancelled",
    "Canceled",
    "Duplicate",
    "Done",
)
DEFAULT_POLLING_INTERVAL_MS = 30000
DEFAULT_HOOKS_TIMEOUT_MS = 60000
DEFAULT_MAX_CONCURRENT_AGENTS = 10
DEFAULT_MAX_TURNS = 20
DEFAULT_MAX_RETRY_BACKOFF_MS = 300000
DEFAULT_CODEX_COMMAND = "codex app-server"
DEFAULT_CODEX_TURN_TIMEOUT_MS = 3600000
DEFAULT_CODEX_READ_TIMEOUT_MS = 5000
DEFAULT_CODEX_STALL_TIMEOUT_MS = 300000


def _default_workspace_root() -> str:
    return str(Path(tempfile.gettempdir()) / "symphony_workspaces")


def _require_map(value: Any, *, path: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigCoercionError(
            f"{path}: expected a map/object, got {type(value).__name__}"
        )
    return value


def _coerce_int(value: Any, *, path: str) -> int:
    # ``bool`` is a subclass of ``int`` — reject explicitly so ``True`` does
    # not silently become ``1`` for a numeric setting.
    if isinstance(value, bool):
        raise ConfigCoercionError(f"{path}: expected int, got bool")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        try:
            return int(stripped)
        except ValueError as exc:
            raise ConfigCoercionError(
                f"{path}: cannot coerce {value!r} to int"
            ) from exc
    raise ConfigCoercionError(
        f"{path}: cannot coerce {type(value).__name__} to int"
    )


def _coerce_optional_str(value: Any, *, path: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise ConfigCoercionError(
        f"{path}: expected string or null, got {type(value).__name__}"
    )


def _coerce_required_str(value: Any, *, path: str) -> str:
    if not isinstance(value, str):
        raise ConfigCoercionError(
            f"{path}: expected string, got {type(value).__name__}"
        )
    return value


def _coerce_str_tuple(value: Any, *, path: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ConfigCoercionError(
            f"{path}: expected list of strings, got {type(value).__name__}"
        )
    out: list[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str):
            raise ConfigCoercionError(
                f"{path}[{idx}]: expected string, got {type(item).__name__}"
            )
        out.append(item)
    return tuple(out)


def _normalize_agents_by_state(value: Any, *, path: str) -> dict[str, int]:
    """Lowercase keys, drop invalid (non-positive / non-int) entries.

    Per SPED §5.3.5: invalid entries are silently ignored — they never raise.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigCoercionError(
            f"{path}: expected a map/object, got {type(value).__name__}"
        )
    out: dict[str, int] = {}
    for raw_key, raw_val in value.items():
        if not isinstance(raw_key, str):
            continue
        # bool is int subclass — explicitly excluded
        if isinstance(raw_val, bool) or not isinstance(raw_val, int):
            continue
        if raw_val <= 0:
            continue
        out[raw_key.lower()] = raw_val
    return out


def _build_tracker(raw: dict[str, Any]) -> TrackerConfig:
    section = _require_map(raw.get("tracker"), path="tracker")
    return TrackerConfig(
        kind=_coerce_optional_str(section.get("kind"), path="tracker.kind"),
        endpoint=_coerce_optional_str(
            section.get("endpoint", DEFAULT_TRACKER_ENDPOINT),
            path="tracker.endpoint",
        ),
        api_key=_coerce_optional_str(
            section.get("api_key"), path="tracker.api_key"
        ),
        project_slug=_coerce_optional_str(
            section.get("project_slug"), path="tracker.project_slug"
        ),
        active_states=_coerce_str_tuple(
            section.get("active_states", list(DEFAULT_ACTIVE_STATES)),
            path="tracker.active_states",
        ),
        terminal_states=_coerce_str_tuple(
            section.get("terminal_states", list(DEFAULT_TERMINAL_STATES)),
            path="tracker.terminal_states",
        ),
    )


def _build_polling(raw: dict[str, Any]) -> PollingConfig:
    section = _require_map(raw.get("polling"), path="polling")
    return PollingConfig(
        interval_ms=_coerce_int(
            section.get("interval_ms", DEFAULT_POLLING_INTERVAL_MS),
            path="polling.interval_ms",
        ),
    )


def _build_workspace(raw: dict[str, Any]) -> WorkspaceConfig:
    section = _require_map(raw.get("workspace"), path="workspace")
    root_value = section.get("root", _default_workspace_root())
    root = _coerce_required_str(root_value, path="workspace.root")
    return WorkspaceConfig(root=root)


def _build_hooks(raw: dict[str, Any]) -> HooksConfig:
    section = _require_map(raw.get("hooks"), path="hooks")
    return HooksConfig(
        after_create=_coerce_optional_str(
            section.get("after_create"), path="hooks.after_create"
        ),
        before_run=_coerce_optional_str(
            section.get("before_run"), path="hooks.before_run"
        ),
        after_run=_coerce_optional_str(
            section.get("after_run"), path="hooks.after_run"
        ),
        before_remove=_coerce_optional_str(
            section.get("before_remove"), path="hooks.before_remove"
        ),
        timeout_ms=_coerce_int(
            section.get("timeout_ms", DEFAULT_HOOKS_TIMEOUT_MS),
            path="hooks.timeout_ms",
        ),
    )


def _build_agent(raw: dict[str, Any]) -> AgentConfig:
    section = _require_map(raw.get("agent"), path="agent")
    return AgentConfig(
        max_concurrent_agents=_coerce_int(
            section.get("max_concurrent_agents", DEFAULT_MAX_CONCURRENT_AGENTS),
            path="agent.max_concurrent_agents",
        ),
        max_turns=_coerce_int(
            section.get("max_turns", DEFAULT_MAX_TURNS),
            path="agent.max_turns",
        ),
        max_retry_backoff_ms=_coerce_int(
            section.get("max_retry_backoff_ms", DEFAULT_MAX_RETRY_BACKOFF_MS),
            path="agent.max_retry_backoff_ms",
        ),
        max_concurrent_agents_by_state=_normalize_agents_by_state(
            section.get("max_concurrent_agents_by_state"),
            path="agent.max_concurrent_agents_by_state",
        ),
    )


def _build_codex(raw: dict[str, Any]) -> CodexConfig:
    section = _require_map(raw.get("codex"), path="codex")
    return CodexConfig(
        command=_coerce_required_str(
            section.get("command", DEFAULT_CODEX_COMMAND), path="codex.command"
        ),
        approval_policy=_coerce_optional_str(
            section.get("approval_policy"), path="codex.approval_policy"
        ),
        thread_sandbox=_coerce_optional_str(
            section.get("thread_sandbox"), path="codex.thread_sandbox"
        ),
        turn_sandbox_policy=_coerce_optional_str(
            section.get("turn_sandbox_policy"), path="codex.turn_sandbox_policy"
        ),
        turn_timeout_ms=_coerce_int(
            section.get("turn_timeout_ms", DEFAULT_CODEX_TURN_TIMEOUT_MS),
            path="codex.turn_timeout_ms",
        ),
        read_timeout_ms=_coerce_int(
            section.get("read_timeout_ms", DEFAULT_CODEX_READ_TIMEOUT_MS),
            path="codex.read_timeout_ms",
        ),
        stall_timeout_ms=_coerce_int(
            section.get("stall_timeout_ms", DEFAULT_CODEX_STALL_TIMEOUT_MS),
            path="codex.stall_timeout_ms",
        ),
    )


def apply_defaults(raw: dict[str, Any]) -> EffectiveConfig:
    """Build an :class:`EffectiveConfig` from a raw front-matter map.

    Unknown top-level and nested keys are ignored (forward compatibility,
    SPED §5.3). Input ``raw`` is not mutated.
    """

    if not isinstance(raw, dict):
        raise ConfigCoercionError(
            f"workflow config: expected a map/object, got {type(raw).__name__}"
        )

    return EffectiveConfig(
        tracker=_build_tracker(raw),
        polling=_build_polling(raw),
        workspace=_build_workspace(raw),
        hooks=_build_hooks(raw),
        agent=_build_agent(raw),
        codex=_build_codex(raw),
    )
