"""WORKFLOW.md loader (SPED §5.2).

Parses an optional YAML front-matter block delimited by ``---`` lines and
returns the remaining Markdown body as the prompt template. The first line
must be exactly ``---`` (no trailing characters) to enter front-matter mode;
otherwise the entire file is treated as the prompt body.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from river_gang.workflow.errors import (
    FrontMatterNotAMap,
    MissingWorkflowFile,
    WorkflowParseError,
)

_FRONT_MATTER_FENCE = "---"


@dataclass(frozen=True)
class WorkflowDefinition:
    """Parsed WORKFLOW.md payload (SPED §4.1.2)."""

    config: dict[str, Any]
    prompt_template: str


def load_workflow(path: Path) -> WorkflowDefinition:
    """Load a WORKFLOW.md file from ``path``.

    Raises:
        MissingWorkflowFile: file does not exist or cannot be read.
        WorkflowParseError: YAML front matter is malformed, or the file opens
            with ``---`` but never closes the front-matter block.
        FrontMatterNotAMap: YAML decodes to a non-map root (scalar, list, ...).
    """

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise MissingWorkflowFile(str(path)) from exc
    except OSError as exc:
        raise MissingWorkflowFile(f"{path}: {exc}") from exc

    front_matter_text, body = _split_front_matter(text)

    config: dict[str, Any] = (
        {} if front_matter_text is None else _parse_front_matter(front_matter_text)
    )

    return WorkflowDefinition(config=config, prompt_template=body.strip())


def _split_front_matter(text: str) -> tuple[str | None, str]:
    """Return ``(front_matter_text or None, body_text)``.

    Front matter is recognised only when the first line is exactly ``---``.
    The closing fence is the next line equal to ``---``. Missing closing
    fence raises ``WorkflowParseError`` (plan §34.4: explicit error beats
    silent fall-through to body-only).
    """

    lines = text.splitlines(keepends=True)
    if not lines:
        return None, ""

    first_line_stripped = lines[0].rstrip("\r\n")
    if first_line_stripped != _FRONT_MATTER_FENCE:
        return None, text

    for idx in range(1, len(lines)):
        if lines[idx].rstrip("\r\n") == _FRONT_MATTER_FENCE:
            front_matter = "".join(lines[1:idx])
            body = "".join(lines[idx + 1 :])
            return front_matter, body

    raise WorkflowParseError(
        "WORKFLOW.md begins with '---' but is missing the closing front-matter fence"
    )


def _parse_front_matter(front_matter_text: str) -> dict[str, Any]:
    """Decode ``front_matter_text`` as YAML and require a map at the root."""

    try:
        decoded = yaml.safe_load(front_matter_text)
    except yaml.YAMLError as exc:
        raise WorkflowParseError(f"invalid YAML front matter: {exc}") from exc

    if decoded is None:
        return {}
    if not isinstance(decoded, dict):
        raise FrontMatterNotAMap(
            f"YAML front matter must decode to a map, got {type(decoded).__name__}"
        )
    return decoded
