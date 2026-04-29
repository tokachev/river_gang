"""Typed exceptions for the prompt-rendering layer (SPED §5.4)."""

from __future__ import annotations


class PromptError(Exception):
    """Base class for all prompt-rendering failures."""


class TemplateParseError(PromptError):
    """The template body could not be parsed as a Liquid document."""


class TemplateRenderError(PromptError):
    """Render-time failure — strict-undefined hit, unknown filter, etc."""
