"""Unit tests for :mod:`fasteditbench.diff_quality` (VAL-202).

Each test maps back to a contract phrase from ``contract.json`` /
``DESIGN.md`` section 6. Synthetic code snippets are intentionally
realistic (small Python functions mirroring the DESIGN.md worked
example) so the unified diffs are non-trivial.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from fasteditbench.diff_quality import DiffQuality, compute_diff_quality

# --- Reusable fixtures -------------------------------------------------------

ORIGINAL = """def f(x):
    y = x + 1
    return y
"""

# Gold edit: change the middle line only (matches DESIGN.md §6 example).
EXPECTED = """def f(x):
    y = x * 2
    return y
"""

# Over-editing: also rewrites the ``return`` line with a comment.
OVER_EDIT = """def f(x):
    y = x * 2
    return y  # computed
"""

# Under-editing: leaves the middle line unchanged; only adds a trailing
# comment to the return. Fewer lines touched vs the gold.
# Gold touches the ``y = x + 1`` line; this edit doesn't — so the
# adapter's "changed" count can still be lower than gold when the
# adapter does a strictly smaller change.
UNDER_EDIT = ORIGINAL  # identity is the tightest "under-edit" (0 changes).


# --- Identity edit -> ratio == 0 --------------------------------------------


def test_identity_edit_ratio_is_zero() -> None:
    """Contract phrase: 'identity edit -> ratio=0'.

    adapter_output == original_code (adapter did nothing) while the gold
    demands changes. loc_changed must be 0, loc_required > 0, ratio 0.0.
    """
    dq = compute_diff_quality(ORIGINAL, EXPECTED, adapter_output=ORIGINAL)
    assert dq.loc_changed == 0
    assert dq.loc_required > 0
    assert dq.minimality_ratio == 0.0


# --- Perfect minimal edit -> ratio == 1 -------------------------------------


def test_perfect_minimal_edit_ratio_is_one() -> None:
    """Contract phrase: 'perfect minimal edit -> ratio=1'."""
    dq = compute_diff_quality(ORIGINAL, EXPECTED, adapter_output=EXPECTED)
    assert dq.loc_changed == dq.loc_required
    assert dq.minimality_ratio == 1.0


# --- Over-editing -> ratio > 1 ----------------------------------------------


def test_over_editing_ratio_gt_one() -> None:
    """Contract phrase: 'over-editing -> ratio>1'.

    Adapter makes the intended change AND an extra unrequested change.
    """
    dq = compute_diff_quality(ORIGINAL, EXPECTED, adapter_output=OVER_EDIT)
    assert dq.minimality_ratio > 1.0


# --- Under-editing -> ratio < 1 ---------------------------------------------


def test_under_editing_ratio_lt_one() -> None:
    """Contract phrase: 'under-editing -> ratio<1'.

    Adapter did strictly fewer changes than the gold required. The
    identity case (no-op) is the tightest under-edit.
    """
    # Construct a case where the gold wants TWO lines changed but the
    # adapter only changes ONE.
    original = "a = 1\nb = 2\nc = 3\n"
    expected = "a = 10\nb = 20\nc = 3\n"  # two lines changed
    adapter = "a = 10\nb = 2\nc = 3\n"  # only one line changed
    dq = compute_diff_quality(original, expected, adapter_output=adapter)
    assert 0.0 < dq.minimality_ratio < 1.0


# --- Hunk counting ----------------------------------------------------------


def test_hunk_count_single_hunk() -> None:
    """One contiguous change -> hunk_count == 1."""
    dq = compute_diff_quality(ORIGINAL, EXPECTED, adapter_output=EXPECTED)
    assert dq.hunk_count == 1


def test_hunk_count_multiple_hunks() -> None:
    """Two non-adjacent changes -> hunk_count == 2."""
    original = (
        "def f():\n"
        "    return 1\n"
        "\n"
        "def g():\n"
        "    return 2\n"
        "\n"
        "def h():\n"
        "    return 3\n"
    )
    # Change f's body AND h's body, leave g alone -> two hunks.
    adapter = (
        "def f():\n"
        "    return 111\n"
        "\n"
        "def g():\n"
        "    return 2\n"
        "\n"
        "def h():\n"
        "    return 333\n"
    )
    dq = compute_diff_quality(original, original, adapter_output=adapter)
    assert dq.hunk_count == 2


# --- context_lines_preserved -------------------------------------------------


def test_context_lines_preserved_full() -> None:
    """Adapter keeps all surrounding lines -> context_lines_preserved == 1.0."""
    dq = compute_diff_quality(ORIGINAL, EXPECTED, adapter_output=EXPECTED)
    assert dq.context_lines_preserved == 1.0


def test_context_lines_preserved_partial() -> None:
    """Adapter deletes one surrounding line it should have kept.

    ``context_lines_preserved`` must be strictly between 0 and 1.
    """
    original = "line_a\nTARGET\nline_b\nline_c\n"
    expected = "line_a\nTARGET_EDITED\nline_b\nline_c\n"
    # Adapter edited the target AND accidentally dropped line_c.
    adapter = "line_a\nTARGET_EDITED\nline_b\n"
    dq = compute_diff_quality(original, expected, adapter_output=adapter)
    assert 0.0 < dq.context_lines_preserved < 1.0


def test_context_lines_preserved_full_replacement_edge() -> None:
    """Original and expected share no lines -> 1.0 by vacuous truth."""
    original = "alpha\nbeta\ngamma\n"
    expected = "one\ntwo\nthree\n"
    adapter = "one\ntwo\nthree\n"
    dq = compute_diff_quality(original, expected, adapter_output=adapter)
    assert dq.context_lines_preserved == 1.0


# --- max(loc_required, 1) floor ---------------------------------------------


def test_loc_required_empty_original_uses_floor_one() -> None:
    """No diff at all (original == expected == adapter) -> ratio 0.0, no crash.

    Documents the ``max(loc_required, 1)`` floor in the denominator.
    """
    code = "print('hi')\n"
    dq = compute_diff_quality(code, code, adapter_output=code)
    assert dq.loc_required == 0
    assert dq.loc_changed == 0
    # 0 / max(0, 1) == 0.0 -- no ZeroDivisionError.
    assert dq.minimality_ratio == 0.0


# --- Dataclass-level guarantees ---------------------------------------------


def test_dataclass_is_frozen() -> None:
    """``DiffQuality`` is frozen; mutation raises FrozenInstanceError."""
    dq = compute_diff_quality(ORIGINAL, EXPECTED, adapter_output=EXPECTED)
    with pytest.raises(FrozenInstanceError):
        dq.loc_changed = 99  # type: ignore[misc]


def test_returned_types() -> None:
    """Integer fields are ``int``; ratio fields are ``float``."""
    dq = compute_diff_quality(ORIGINAL, EXPECTED, adapter_output=EXPECTED)
    assert isinstance(dq, DiffQuality)
    assert isinstance(dq.loc_required, int)
    assert isinstance(dq.loc_changed, int)
    assert isinstance(dq.hunk_count, int)
    assert isinstance(dq.minimality_ratio, float)
    assert isinstance(dq.context_lines_preserved, float)
