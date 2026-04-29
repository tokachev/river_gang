"""Strict prompt rendering (SPED §5.4).

Wraps `python-liquid` with ``StrictUndefined`` so unknown variables and
unknown filters fail rendering — required by §5.4. The :class:`Issue`
dataclass is converted to a plain dict on the fly because python-liquid 2.x
resolves member access via mapping/sequence protocols, not Python attribute
access.

Empty / whitespace-only templates short-circuit to :data:`FALLBACK_PROMPT`
per §5.4 ("If the workflow prompt body is empty, the runtime MAY use a
minimal default prompt"). We do — keeps the orchestrator running when an
operator commits a WORKFLOW.md without a body.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from liquid import Environment, StrictUndefined
from liquid.exceptions import LiquidError, LiquidSyntaxError

from river_gang.prompt.errors import TemplateParseError, TemplateRenderError
from river_gang.tracker.issue import Issue

FALLBACK_PROMPT = "You are working on an issue from Linear."

# One environment shared across the process; constructing a Liquid
# Environment is non-trivial and the configuration is immutable here.
_ENV = Environment(undefined=StrictUndefined)


def render_prompt(
    template: str, *, issue: Issue, attempt: int | None
) -> str:
    """Render ``template`` with ``issue`` + ``attempt`` in strict mode.

    Raises:
        TemplateParseError: ``template`` is not valid Liquid syntax.
        TemplateRenderError: an unknown variable was referenced or an
            unknown filter was applied at render time.
    """
    if template.strip() == "":
        return FALLBACK_PROMPT

    try:
        compiled = _ENV.from_string(template)
    except LiquidSyntaxError as exc:
        raise TemplateParseError(str(exc)) from exc

    context: dict[str, Any] = {
        "issue": dataclasses.asdict(issue),
        "attempt": attempt,
    }

    try:
        return compiled.render(**context)
    except LiquidError as exc:
        raise TemplateRenderError(str(exc)) from exc
