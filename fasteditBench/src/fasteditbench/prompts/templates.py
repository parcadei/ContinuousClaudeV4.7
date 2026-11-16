"""Per-model prompt templates (VAL-102.5 decision: per_model_registry).

Pilot result (10 fixtures, tier-1 pass counts out of 10):

+---------------------+------------+------------+--------------+
| model               | template A | template B | variance_pp  |
+=====================+============+============+==============+
| anthropic/opus-4.7  |      8     |      9     |     10.0     |
| openai/gpt-5.4      |      8     |      7     |     10.0     |
+---------------------+------------+------------+--------------+

Both variances > 5pp, so per-model assignment wins:
- Anthropic models -> Template B (Markdown-style, slightly better on opus)
- OpenAI models -> Template A (XML-style, slightly better on gpt)
- Everything else (Kimi, GLM, future models) -> Template A as default.

Evidence: ``continuum/autonomous/fastedit-bench-02/validation/template-pilot.json``
"""

from __future__ import annotations

from collections.abc import Callable

# ── Template A: Anthropic XML-style ──────────────────────────────────

TEMPLATE_A_SYSTEM = (
    "You are a code editor. Apply the requested edit to the file using the `edit` tool.\n"
    "Emit one or more `edit` tool calls. Do not emit code fences, explanations, or chain-of-thought — just the tool calls.\n"
    "The `old_string` must match the file content EXACTLY (including whitespace and indentation)."
)


def template_a_user_fmt(file_path: str, snippet: str, original_code: str) -> str:
    """Format the user message for Template A (XML-style)."""
    return (
        f"Apply this edit to `{file_path}`:\n\n"
        f"<snippet>\n{snippet}\n</snippet>\n\n"
        f"Here is the current file content:\n\n"
        f"<file>\n{original_code}\n</file>"
    )


# ── Template B: OpenAI Markdown-style ────────────────────────────────

TEMPLATE_B_SYSTEM = (
    "You are a code editor. Use the `edit` tool to apply edits to the file below. "
    "Make one or more `edit` calls. Each call replaces `old_string` with `new_string`. "
    "Do not include explanations or preambles in your response — only tool calls. "
    "The `old_string` must match the file exactly, including whitespace."
)


def template_b_user_fmt(file_path: str, snippet: str, original_code: str) -> str:
    """Format the user message for Template B (Markdown-style)."""
    return (
        f"## File to edit\n"
        f"Path: {file_path}\n\n"
        f"```\n{original_code}\n```\n\n"
        f"## Edit instruction\n"
        f"Apply this snippet "
        f"(the `# ... existing code ...` markers indicate preserved regions):\n\n"
        f"```\n{snippet}\n```"
    )


# Public uppercase aliases — used as "template slots" in the registry.
# Keeping snake_case definitions above makes ruff N802 happy; these
# module-level names preserve the contract-style naming.
TEMPLATE_A_USER_FMT = template_a_user_fmt
TEMPLATE_B_USER_FMT = template_b_user_fmt


# ── Registry ─────────────────────────────────────────────────────────

UserFormatter = Callable[[str, str, str], str]


def get_template(model_id: str) -> tuple[str, UserFormatter]:
    """Return ``(system_prompt, user_formatter)`` for ``model_id``.

    Assignment:
        - ``anthropic/*`` -> Template B
        - ``openai/*``    -> Template A
        - everything else -> Template A (default)

    Args:
        model_id: OpenRouter model id, e.g. ``"anthropic/claude-opus-4.7"``.
    """
    if model_id.startswith("anthropic/"):
        return (TEMPLATE_B_SYSTEM, template_b_user_fmt)
    # OpenAI and everything else fall back to Template A
    return (TEMPLATE_A_SYSTEM, template_a_user_fmt)


def render_user(
    model_id: str, file_path: str, snippet: str, original_code: str
) -> str:
    """Convenience: render the user message using the model's template."""
    _, user_fmt = get_template(model_id)
    return user_fmt(file_path, snippet, original_code)
