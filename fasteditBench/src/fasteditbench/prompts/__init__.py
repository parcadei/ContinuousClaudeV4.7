"""Prompt templates for the edit-tool adapter (VAL-102.5).

Exposes :func:`get_template` which returns ``(system_prompt, user_formatter)``
for a given model id. The active registry is determined by the VAL-102.5
pilot result; see ``continuum/autonomous/fastedit-bench-02/validation/template-pilot.json``.
"""

from fasteditbench.prompts.templates import (
    TEMPLATE_A_SYSTEM,
    TEMPLATE_A_USER_FMT,
    TEMPLATE_B_SYSTEM,
    TEMPLATE_B_USER_FMT,
    get_template,
    render_user,
)

__all__ = [
    "TEMPLATE_A_SYSTEM",
    "TEMPLATE_A_USER_FMT",
    "TEMPLATE_B_SYSTEM",
    "TEMPLATE_B_USER_FMT",
    "get_template",
    "render_user",
]
