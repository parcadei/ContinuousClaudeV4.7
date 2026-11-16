"""Tests for ``scripts/compute_tax_per_operation.py``.

Covers:

* Categorization correctness across every real fixture (no Unknowns).
* Realistic ±2-line anchor context in generated ``old_string`` hunks.
* Minimal tax on ``after=`` kwarg fixtures (synthetic — the real
  fixture set has none).
* Minimal tax on ``delete`` fixtures — just the symbol name on the
  wire.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# The script is under ``scripts/`` (not a package) — load it via path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "src"))

import compute_tax_per_operation as ctpo  # noqa: E402


# ---------------------------------------------------------------------------
# Categorization
# ---------------------------------------------------------------------------


def test_categorization_covers_all_target_kinds() -> None:
    """Every fixture must get one of the six defined categories."""
    from fasteditbench.runner import load_fixtures

    allowed = {
        "modify_inside",
        "whole_rewrite",
        "insert_after",
        "inline_edit",
        "batch_edit",
        "delete",
    }
    n = 0
    for _case_id, fixture in load_fixtures(_REPO_ROOT / "fixtures", langs=None):
        cat = ctpo.categorize_fixture(
            fixture.get("target_kind"),
            str(fixture.get("pattern", "")),
            str(fixture.get("snippet", "")),
        )
        assert cat in allowed, f"bad category {cat} for fixture"
        n += 1
    assert n == 156, f"expected 156 fixtures, got {n}"


def test_delete_pattern_takes_precedence() -> None:
    """A fixture with ``pattern: delete_lines`` is classified ``delete``
    even if ``target_kind == 'replace'``."""
    cat = ctpo.categorize_fixture(
        target_kind="replace",
        pattern="delete_lines",
        snippet="func Foo() { /* ... */ }",
    )
    assert cat == "delete"


def test_marker_detection_across_comment_styles() -> None:
    """Marker detector recognizes ``#``, ``//``, ``/* */``, ``--``, and
    HTML-comment variants."""
    assert ctpo.has_existing_code_marker("a\n# ... existing code ...\nb")
    assert ctpo.has_existing_code_marker("a\n// ... existing code ...\nb")
    assert ctpo.has_existing_code_marker(
        "a\n/* ... existing code ... */\nb"
    )
    assert ctpo.has_existing_code_marker("a\n-- ... existing code ...\nb")
    assert ctpo.has_existing_code_marker(
        "a\n<!-- ... existing code ... -->\nb"
    )
    assert not ctpo.has_existing_code_marker("a\nprint('hello')\nb")


def test_replace_with_marker_is_modify_inside() -> None:
    cat = ctpo.categorize_fixture(
        target_kind="replace",
        pattern="add_guard",
        snippet="def foo():\n    x = 1\n    # ... existing code ...\n",
    )
    assert cat == "modify_inside"


def test_replace_without_marker_is_whole_rewrite() -> None:
    cat = ctpo.categorize_fixture(
        target_kind="replace",
        pattern="full_replace",
        snippet="def foo():\n    return 1\n",
    )
    assert cat == "whole_rewrite"


# ---------------------------------------------------------------------------
# Anchor-context extraction
# ---------------------------------------------------------------------------


def test_anchor_context_is_two_lines() -> None:
    """For a tiny fixture with a 1-line change in the middle of 5 lines,
    the extracted ``old_string`` must include ±2 surrounding lines
    (when available)."""
    original = "A\nB\nC\nD\nE\n"
    expected = "A\nB\nCHANGED\nD\nE\n"
    hunks = ctpo.extract_hunks_with_context(original, expected, n_context=2)
    assert len(hunks) == 1
    old, new = hunks[0]
    # Expect A + B + C + D + E = whole file (2 ctx above + change + 2 ctx below).
    assert old.splitlines() == ["A", "B", "C", "D", "E"]
    assert new.splitlines() == ["A", "B", "CHANGED", "D", "E"]


def test_anchor_context_clips_at_boundaries() -> None:
    """When the change is at the start of file, context above is clipped
    to 0 (no lines available) and below still gets up to n_context."""
    original = "X\nB\nC\nD\nE\n"
    expected = "Y\nB\nC\nD\nE\n"
    hunks = ctpo.extract_hunks_with_context(original, expected, n_context=2)
    assert len(hunks) == 1
    old, new = hunks[0]
    # Expect X + B + C (no above-context; 2 lines below).
    assert old.splitlines() == ["X", "B", "C"]
    assert new.splitlines() == ["Y", "B", "C"]


def test_anchor_context_merges_adjacent_hunks() -> None:
    """Two separate changes within 2×n_context lines collapse into one
    hunk (what a real agent would do — one tool call spanning both)."""
    original = "A\nB\nC\nD\nE\nF\nG\n"
    # Change B and F — separated by 3 equal lines (C, D, E); with n=2 the
    # windows [A-D] and [D-G] overlap at D → merged.
    expected = "A\nBB\nC\nD\nE\nFF\nG\n"
    hunks = ctpo.extract_hunks_with_context(original, expected, n_context=2)
    assert len(hunks) == 1
    old, new = hunks[0]
    assert "BB" in new and "FF" in new


def test_anchor_context_keeps_separate_hunks_when_far_apart() -> None:
    """Two changes far apart stay as two hunks."""
    lines = [f"L{i}\n" for i in range(20)]
    original = "".join(lines)
    # Change L2 and L17 — windows don't overlap.
    mod_lines = lines.copy()
    mod_lines[2] = "CHANGED2\n"
    mod_lines[17] = "CHANGED17\n"
    expected = "".join(mod_lines)
    hunks = ctpo.extract_hunks_with_context(
        original, expected, n_context=2
    )
    assert len(hunks) == 2


def test_anchor_context_identical_inputs_raises() -> None:
    import pytest

    with pytest.raises(ValueError):
        ctpo.extract_hunks_with_context("same\n", "same\n", n_context=2)


# ---------------------------------------------------------------------------
# FastEdit payload construction
# ---------------------------------------------------------------------------


def test_after_kwarg_has_minimal_tax() -> None:
    """An ``insert_after`` fixture's FastEdit payload is just file_path +
    edit_snippet + after kwarg — no ``old_string`` equivalent."""
    payload = ctpo.build_fastedit_payload(
        operation_category="insert_after",
        file_path="foo.py",
        snippet="def new_fn():\n    return 1\n",
        target_kind="after",
        target_name="existing_fn",
    )
    obj = json.loads(payload)
    assert obj["name"] == "fast_edit"
    assert obj["arguments"]["after"] == "existing_fn"
    assert "replace" not in obj["arguments"]
    # No old_string anywhere on the wire.
    assert "old_string" not in payload


def test_delete_payload_is_symbol_name() -> None:
    """``fast_delete`` payload carries only file_path + symbol — the
    smallest possible tool call for removing code."""
    payload = ctpo.build_fastedit_payload(
        operation_category="delete",
        file_path="foo.go",
        snippet="func Run() error {}",  # irrelevant for delete
        target_kind="replace",
        target_name="Run",
    )
    obj = json.loads(payload)
    assert obj["name"] == "fast_delete"
    assert obj["arguments"]["symbol"] == "Run"
    assert "edit_snippet" not in obj["arguments"]
    assert "old_string" not in payload


def test_inline_edit_has_no_structural_kwarg() -> None:
    """``inline_edit`` (target_kind=none) emits no replace/after kwarg."""
    payload = ctpo.build_fastedit_payload(
        operation_category="inline_edit",
        file_path="foo.py",
        snippet="class Foo:\n    ...\n# ... existing code ...\n",
        target_kind="none",
        target_name=None,
    )
    obj = json.loads(payload)
    assert obj["name"] == "fast_edit"
    assert "replace" not in obj["arguments"]
    assert "after" not in obj["arguments"]


def test_modify_inside_carries_replace_kwarg() -> None:
    payload = ctpo.build_fastedit_payload(
        operation_category="modify_inside",
        file_path="foo.py",
        snippet="def foo():\n    x=1\n    # ... existing code ...\n",
        target_kind="replace",
        target_name="foo",
    )
    obj = json.loads(payload)
    assert obj["arguments"]["replace"] == "foo"


# ---------------------------------------------------------------------------
# End-to-end smoke test
# ---------------------------------------------------------------------------


def test_end_to_end_on_retry_batch_fixture() -> None:
    """The walkthrough fixture ``python/01-add_guard`` must classify as
    ``modify_inside`` and show positive savings."""
    from fasteditbench.runner import load_fixtures

    fixtures = dict(load_fixtures(_REPO_ROOT / "fixtures", langs=["python"]))
    assert "python/01-add_guard" in fixtures
    rows = ctpo.compute_rows(_REPO_ROOT / "fixtures")
    [row] = [r for r in rows if r["case_id"] == "python/01-add_guard"]
    assert row["operation_category"] == "modify_inside"
    assert row["has_marker"] is True
    assert row["native_edit_tokens"] > 0
    assert row["fastedit_tokens"] > 0
    assert row["savings_tokens"] == (
        row["native_edit_tokens"] - row["fastedit_tokens"]
    )
