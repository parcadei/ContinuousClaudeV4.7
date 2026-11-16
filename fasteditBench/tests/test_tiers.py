"""Tests for Tier-1 (exact-match normalized) and Tier-2 (AST-equivalence) checkers.

Satisfies VAL-201 from fastedit-bench-01/contract.json:
a hand-built 9-case sample set covering (exact × same-ast × sem-diff) ×
(3 languages).
"""

from __future__ import annotations

import pytest

from fasteditbench import tiers
from fasteditbench.tiers import (
    _normalize,
    classify,
    tier1_exact_match_normalized,
    tier2_ast_equivalent,
)

# ── 9-case sample matrix ──────────────────────────────────────────────
#
# Each row: (case_id, language, adapter_output, expected_output,
#            expected_tier1, expected_tier2).
#
# Three buckets, three languages, nine rows total.

_MATRIX: list[tuple[str, str, str, str, bool, bool]] = [
    # ── EXACT (3): tier1 passes → tier2 short-circuits to True ──
    (
        "exact-python",
        "python",
        "def f(x):\n    return x + 1\n",
        "def f(x):\n    return x + 1\n",
        True,
        True,
    ),
    (
        "exact-rust",
        "rust",
        "fn main() {\n    let x = 42;\n}\n",
        "fn main() {\n    let x = 42;\n}\n",
        True,
        True,
    ),
    (
        "exact-typescript",
        "typescript",
        "const a: number = 1;\n",
        "const a: number = 1;\n",
        True,
        True,
    ),
    # ── SAME-AST (3): tier1 fails, tier2 passes ──
    (
        # Different comment contents — Tier-2 strips comment nodes.
        "same-ast-python-comment",
        "python",
        "# old comment\ndef f(x):\n    return x + 1\n",
        "# new comment describing behavior\ndef f(x):\n    return x + 1\n",
        False,
        True,
    ),
    (
        # Extra blank line inside function body — tree-sitter skips
        # whitespace so both produce the same AST.
        "same-ast-rust-blank-line",
        "rust",
        'fn greet() {\n    let msg = "hi";\n    println!("{}", msg);\n}\n',
        'fn greet() {\n    let msg = "hi";\n\n    println!("{}", msg);\n}\n',
        False,
        True,
    ),
    (
        # Trailing statement-terminator semicolon vs none — the `;`
        # anonymous token is skipped by Tier-2.
        "same-ast-typescript-semicolon",
        "typescript",
        "const a: number = 1;\n",
        "const a: number = 1\n",
        False,
        True,
    ),
    # ── SEM-DIFF (3): tier1 fails, tier2 fails ──
    (
        # Different variable name — identifier text differs.
        "sem-diff-python-varname",
        "python",
        "def f():\n    total = 0\n    return total\n",
        "def f():\n    count = 0\n    return count\n",
        False,
        False,
    ),
    (
        # Different literal value — integer text differs.
        "sem-diff-rust-literal",
        "rust",
        "fn limit() -> i32 { 100 }\n",
        "fn limit() -> i32 { 200 }\n",
        False,
        False,
    ),
    (
        # Inverted condition — anonymous operator token differs.
        "sem-diff-typescript-operator",
        "typescript",
        "function pick(a: number, b: number) { return a > b ? a : b; }\n",
        "function pick(a: number, b: number) { return a < b ? a : b; }\n",
        False,
        False,
    ),
]


@pytest.mark.parametrize(
    ("case_id", "language", "adapter_output", "expected_output", "want_t1", "want_t2"),
    [(c[0], c[1], c[2], c[3], c[4], c[5]) for c in _MATRIX],
    ids=[c[0] for c in _MATRIX],
)
def test_classify_matrix(
    case_id: str,
    language: str,
    adapter_output: str,
    expected_output: str,
    want_t1: bool,
    want_t2: bool,
) -> None:
    """Each of the 9 hand-built cases classifies as expected."""
    t1, t2 = classify(adapter_output, expected_output, language)
    assert t1 is want_t1, f"{case_id}: tier1 expected {want_t1}, got {t1}"
    assert t2 is want_t2, f"{case_id}: tier2 expected {want_t2}, got {t2}"


# ── Auxiliary unit tests ──────────────────────────────────────────────


def test_normalize_drops_trailing_whitespace() -> None:
    """_normalize rstrips each line and strips overall blank lines."""
    src = "a = 1   \n  b = 2\t\n"
    got = _normalize(src)
    assert got == "a = 1\n  b = 2"


def test_normalize_strips_leading_and_trailing_blank_lines() -> None:
    src = "\n\nx = 1\n\n\n"
    assert _normalize(src) == "x = 1"


def test_tier1_forgives_trailing_blank_lines() -> None:
    """Leading/trailing blank lines around the whole snippet are forgiven."""
    assert tier1_exact_match_normalized("x = 1\n", "x = 1\n\n\n") is True


def test_tier1_rejects_internal_whitespace_differences() -> None:
    """Tier-1 preserves non-trailing whitespace: `x=1` != `x = 1`."""
    assert tier1_exact_match_normalized("x=1", "x = 1") is False


def test_tier2_unsupported_language_raises() -> None:
    """Misuse is loud: unknown language raises ValueError."""
    with pytest.raises(ValueError, match="unsupported language"):
        tier2_ast_equivalent("x", "x", "cobol")


def test_classify_short_circuits_tier2_when_tier1_passes(monkeypatch) -> None:
    """When Tier-1 passes, Tier-2 is not invoked — even if it would raise."""

    def boom(*_args, **_kwargs):  # pragma: no cover - asserted via short-circuit
        raise RuntimeError("tier2 must not be called when tier1 passes")

    monkeypatch.setattr(tiers, "tier2_ast_equivalent", boom)

    t1, t2 = classify("x = 1\n", "x = 1\n", "python")
    assert (t1, t2) == (True, True)


def test_tier2_unparseable_returns_false() -> None:
    """If the source has parse errors, Tier-2 returns False, not raises."""
    # Rust parser on grossly malformed input.
    broken = "fn f( {"
    ok = "fn f() {}\n"
    assert tier2_ast_equivalent(broken, ok, "rust") is False
