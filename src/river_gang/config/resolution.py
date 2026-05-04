"""``$VAR`` indirection and path normalization (SPED §5.3.1, §5.3.3, §6.1).

Two distinct resolvers, applied to different field categories:

- :func:`resolve_env_vars` — for plain string values that MAY be a literal
  token or a strict ``$VAR`` reference (e.g. ``tracker.api_key``). Pattern is
  ``$[A-Z_][A-Z0-9_]*`` only; ``$lower``, ``${BRACED}``, embedded ``$VAR``,
  and trailing characters are NOT recognised. Missing var or empty resolution
  → ``None`` per §5.3.1 ("treated as missing").

- :func:`normalize_path` — for filesystem path values (``workspace.root``).
  Supports ``~`` home expansion, ``$VAR`` expansion (delegated to
  ``os.path.expandvars`` so embedded forms like ``$WS_HOME/runs`` work), and
  resolves relative paths against ``base_dir`` (the directory containing the
  selected ``WORKFLOW.md`` per §5.3.3). URIs and arbitrary command strings
  are NOT touched (§6.1).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from river_gang.config.defaults import apply_defaults
from river_gang.config.schema import (
    EffectiveConfig,
    TrackerConfig,
    WorkspaceConfig,
)

_DOLLAR_TOKEN_RE = re.compile(r"\$([A-Z_][A-Z0-9_]*)")


def resolve_env_vars(value: str | None) -> str | None:
    """Resolve a strict ``$NAME`` token against ``os.environ``.

    Returns ``None`` when:
        * input is ``None``, or
        * input matches the strict pattern but the env var is missing or
          resolves to an empty string (§5.3.1).

    Otherwise returns the input unchanged (literal pass-through).
    """

    if value is None:
        return None
    match = _DOLLAR_TOKEN_RE.fullmatch(value)
    if match is None:
        return value
    resolved = os.environ.get(match.group(1), "")
    if resolved == "":
        return None
    return resolved


def normalize_path(value: str, *, base_dir: Path) -> str:
    """Expand ``~`` and ``$VAR`` then absolutize against ``base_dir``.

    ``base_dir`` MUST be the directory containing ``WORKFLOW.md`` (§5.3.3).
    Undefined ``$VAR`` references are left as literal text (mirrors
    ``os.path.expandvars`` behaviour) so a misconfigured value surfaces as
    a visibly broken path rather than a silently truncated one.
    """

    expanded = os.path.expanduser(os.path.expandvars(value))
    path = Path(expanded)
    if not path.is_absolute():
        path = base_dir / path
    # ``os.path.normpath`` collapses ``..``/``.`` segments without following
    # symlinks (unlike ``Path.resolve``). On macOS ``/var`` → ``/private/var``
    # via symlink: spec §6.1 only requires absolutization, not realpath.
    return os.path.normpath(str(path))


def resolve_and_validate(
    raw: dict[str, Any], *, workflow_dir: Path
) -> EffectiveConfig:
    """Apply defaults + ``$VAR`` resolution + path normalization.

    This is the single public entry point downstream consumers should use.
    Range/required-field validation lives in
    :mod:`river_gang.config.validation` (Task 6).
    """

    base_cfg = apply_defaults(raw)

    resolved_api_key = resolve_env_vars(base_cfg.tracker.api_key)

    tracker = TrackerConfig(
        kind=base_cfg.tracker.kind,
        endpoint=base_cfg.tracker.endpoint,  # URI, NOT path-normalized (§6.1)
        api_key=resolved_api_key,
        project_slug=base_cfg.tracker.project_slug,
        active_states=base_cfg.tracker.active_states,
        terminal_states=base_cfg.tracker.terminal_states,
        start_state=base_cfg.tracker.start_state,
        success_state=base_cfg.tracker.success_state,
    )

    workspace = WorkspaceConfig(
        root=normalize_path(base_cfg.workspace.root, base_dir=workflow_dir),
        # Git remotes/URLs are identifiers, not filesystem paths; pass through.
        repository=base_cfg.workspace.repository,
    )

    # ``codex.command`` is an arbitrary shell command, NOT a path — pass through.
    return EffectiveConfig(
        tracker=tracker,
        polling=base_cfg.polling,
        workspace=workspace,
        hooks=base_cfg.hooks,
        agent=base_cfg.agent,
        codex=base_cfg.codex,
    )
