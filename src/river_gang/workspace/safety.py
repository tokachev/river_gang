"""Workspace key sanitization and path-boundary checks (SPED §9.5).

Implements two of the three §9.5 safety invariants:

- **Invariant 2** (path containment): :func:`validate_within_root` resolves
  both the root and a candidate path with ``Path.resolve(strict=False)`` —
  which follows symlinks — and rejects anything that lands outside the
  resolved root. Symlinks pointing outside the root are therefore detected.
- **Invariant 3** (key sanitization): :func:`sanitize_key` keeps only
  ``[A-Za-z0-9._-]`` and replaces every other character with ``_``.

Invariant 1 (the launcher's ``cwd == workspace_path`` check) lives at the
agent-launch site, not here.
"""

from __future__ import annotations

import re
from pathlib import Path

_ALLOWED_CHAR_RE = re.compile(r"[^A-Za-z0-9._-]")
_RESERVED_KEYS = frozenset({".", ".."})


class WorkspaceSafetyError(Exception):
    """Base class for workspace-safety failures."""


class EmptyWorkspaceKey(WorkspaceSafetyError):  # noqa: N818 -- spec-defined
    """Caller passed an empty identifier into :func:`sanitize_key`."""


class InvalidWorkspaceKey(WorkspaceSafetyError):  # noqa: N818 -- spec-defined
    """Identifier sanitizes to a path-reserved value (``.`` or ``..``)."""


class WorkspaceOutsideRoot(WorkspaceSafetyError):  # noqa: N818 -- spec-defined
    """Resolved candidate path is not contained under the resolved root."""


def sanitize_key(identifier: str) -> str:
    """Return a filesystem-safe workspace directory name (§9.5 invariant 3).

    Each disallowed character maps to a single underscore; consecutive
    disallowed characters therefore produce consecutive underscores. We do
    NOT collapse repeats: distinct inputs must map to distinct outputs to
    avoid accidental directory collisions.

    Raises:
        EmptyWorkspaceKey: ``identifier`` is the empty string.
        InvalidWorkspaceKey: identifier is exactly ``.`` or ``..``. Both
            would resolve to the workspace root or its parent — defensive
            reject here even though :func:`validate_within_root` would also
            block the latter.
    """
    if identifier == "":
        raise EmptyWorkspaceKey("workspace key must be a non-empty string")
    if identifier in _RESERVED_KEYS:
        raise InvalidWorkspaceKey(
            f"workspace key {identifier!r} is reserved (resolves to root or parent)"
        )
    return _ALLOWED_CHAR_RE.sub("_", identifier)


def validate_within_root(root: str | Path, candidate: str | Path) -> Path:
    """Resolve ``candidate`` and require it to live under ``root`` (§9.5 inv. 2).

    Both inputs are normalised via :meth:`Path.resolve(strict=False)` —
    which follows existing symlinks but does not require the target to
    exist. The resolved candidate must equal ``root`` or be ``is_relative_to``
    it; anything else raises :class:`WorkspaceOutsideRoot`.

    Returns the resolved absolute candidate path. Caller can use the return
    value directly without re-resolving.
    """
    resolved_root = Path(root).resolve(strict=False)
    resolved_candidate = Path(candidate).resolve(strict=False)

    # ``is_relative_to`` is the safe boundary check on real Path objects —
    # naive ``str.startswith`` would accept ``/tmp/foobar`` for root ``/tmp/foo``.
    if (
        resolved_candidate != resolved_root
        and not resolved_candidate.is_relative_to(resolved_root)
    ):
        raise WorkspaceOutsideRoot(
            f"candidate path {resolved_candidate!s} is outside workspace root "
            f"{resolved_root!s}"
        )
    return resolved_candidate
