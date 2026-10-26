"""Rendering platform texts from templates.

Phase 2 adds per-platform length limits and sentence-aware trimming.
"""

from __future__ import annotations

from ..config import TEMPLATE_FIELDS, TemplateConfig


def render(template: TemplateConfig, platform: str, variables: dict[str, str]) -> dict[str, str]:
    out = {}
    for field in TEMPLATE_FIELDS[platform]:
        value = getattr(template, field)
        out[field] = value.format_map(variables).strip()
    return out
