"""Tests for the per-model prompt template registry (VAL-102.5)."""
from __future__ import annotations

from fasteditbench.prompts import (
    TEMPLATE_A_SYSTEM,
    TEMPLATE_B_SYSTEM,
    get_template,
    render_user,
)


def test_anthropic_uses_template_b():
    system, user_fmt = get_template("anthropic/claude-opus-4.7")
    assert system == TEMPLATE_B_SYSTEM
    rendered = user_fmt("foo.py", "snip", "orig")
    assert "## File to edit" in rendered
    assert "Path: foo.py" in rendered


def test_openai_uses_template_a():
    system, user_fmt = get_template("openai/gpt-5.4")
    assert system == TEMPLATE_A_SYSTEM
    rendered = user_fmt("foo.py", "snip", "orig")
    assert "<file>" in rendered
    assert "<snippet>" in rendered


def test_other_models_default_to_a():
    # Kimi, GLM, future providers — default to Template A.
    for mid in (
        "moonshotai/kimi-k2.6",
        "z-ai/glm-5.1",
        "meta-llama/llama-4",
        "google/gemini-2.5",
    ):
        system, user_fmt = get_template(mid)
        assert system == TEMPLATE_A_SYSTEM
        assert "<file>" in user_fmt("f.py", "s", "o")


def test_render_user_shortcut():
    out_anth = render_user("anthropic/claude-opus-4.7", "f.py", "S", "O")
    out_oai = render_user("openai/gpt-5.4", "f.py", "S", "O")
    assert "## File to edit" in out_anth
    assert "<file>" in out_oai


def test_template_a_embeds_inputs_verbatim():
    user = render_user("openai/gpt-5.4", "a.rs", "SNIP-X", "ORIG-Y")
    assert "SNIP-X" in user
    assert "ORIG-Y" in user
    assert "a.rs" in user


def test_template_b_embeds_inputs_verbatim():
    user = render_user("anthropic/claude-opus-4.7", "a.rs", "SNIP-X", "ORIG-Y")
    assert "SNIP-X" in user
    assert "ORIG-Y" in user
    assert "a.rs" in user
