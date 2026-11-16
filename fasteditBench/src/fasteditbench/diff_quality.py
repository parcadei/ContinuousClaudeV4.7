"""Diff-quality metrics for the FastEdit benchmark harness (VAL-202).

Formulas (verbatim from ``contract.json.diff_quality_formula`` and
``DESIGN.md`` section 6):

- ``loc_required``           = ``num_lines(unified_diff(original_code, expected_output)) with context=0``
- ``loc_changed``            = ``num_lines(unified_diff(original_code, adapter_output))  with context=0``
- ``minimality_ratio``       = ``loc_changed / max(loc_required, 1)``
- ``hunk_count``             = ``number of @@ hunks in unified_diff(original_code, adapter_output)``
- ``context_lines_preserved`` = ``1 - (lines_unchanged_in_original removed from adapter_output) / total_unchanged_lines``

Conventions chosen for this implementation
------------------------------------------

The design doc explicitly calls out that the M2 worker must pick ONE
convention for ``num_lines`` and apply it uniformly to both numerator
and denominator so the ratio is well-defined. The choices made here and
used throughout this module are:

1. ``num_lines`` EXCLUDES the two unified-diff file-header lines
   (``--- ...`` and ``+++ ...``). Those lines are invariant for every
   non-empty diff and therefore carry no signal about "how much changed".
   Everything else produced by :func:`difflib.unified_diff` — i.e. the
   ``@@`` hunk headers and the removed (``-``) / added (``+``) body
   lines — is counted. This convention is captured in the module-level
   constant :data:`_NUM_LINES_EXCLUDE_FILE_HEADERS` so it is visible at
   a glance.

2. Zero context (``n=0``) is used for every unified-diff call so that
   the counts reflect only lines that actually changed.

3. ``context_lines_preserved`` edge case: when ``original_code`` and
   ``expected_output`` share no lines at all (full replacement, so
   ``total_unchanged_lines == 0``), we define
   ``context_lines_preserved = 1.0`` by vacuous truth — there is no
   context to preserve, so nothing can be dropped.

4. ``minimality_ratio`` uses ``max(loc_required, 1)`` in the denominator
   exactly as the contract specifies; this avoids ``ZeroDivisionError``
   for the degenerate "no-op edit" case where ``original_code`` equals
   ``expected_output``.

The implementation is pure-stdlib (``difflib`` only) and fully
deterministic.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

__all__ = ["DiffQuality", "compute_diff_quality"]

# Convention flag: whether ``_num_lines`` excludes the ``---`` / ``+++``
# file-header lines emitted by ``difflib.unified_diff``. Flipping this
# would require a matching change to every test in
# ``tests/test_diff_quality.py``. See the module docstring for rationale.
_NUM_LINES_EXCLUDE_FILE_HEADERS: bool = True


@dataclass(frozen=True)
class DiffQuality:
    """Bundle of the five diff-quality metrics for a single case.

    All fields follow the formulas listed in the module docstring.

    Attributes:
        loc_required: Lines in ``unified_diff(original, expected, n=0)``
            under the module's ``num_lines`` convention.
        loc_changed: Lines in ``unified_diff(original, adapter, n=0)``
            under the same convention.
        minimality_ratio: ``loc_changed / max(loc_required, 1)``.
        hunk_count: Number of ``@@`` hunk headers in the adapter diff.
        context_lines_preserved: Fraction (``0.0``–``1.0``) of originally
            unchanged lines that survive in the adapter output. ``1.0``
            when there were no unchanged lines to preserve (vacuous).
    """

    loc_required: int
    loc_changed: int
    minimality_ratio: float
    hunk_count: int
    context_lines_preserved: float


def _unified_diff_zero_context(a: str, b: str) -> list[str]:
    """Return the zero-context unified diff between ``a`` and ``b``.

    Splits the inputs with ``keepends=True`` so ``difflib`` can emit
    properly newline-terminated output, runs
    :func:`difflib.unified_diff` with ``n=0``, and realizes the iterator
    to a list so callers can iterate multiple times.
    """

    a_lines = a.splitlines(keepends=True)
    b_lines = b.splitlines(keepends=True)
    return list(difflib.unified_diff(a_lines, b_lines, n=0))


def _num_lines(diff_lines: list[str]) -> int:
    """Count lines in a unified-diff output under the module convention.

    Excludes the ``---`` and ``+++`` file-header lines (see
    :data:`_NUM_LINES_EXCLUDE_FILE_HEADERS` and the module docstring).
    Includes ``@@`` hunk headers and every removed / added body line.
    """

    if not _NUM_LINES_EXCLUDE_FILE_HEADERS:
        return len(diff_lines)  # pragma: no cover - convention is fixed

    count = 0
    for line in diff_lines:
        if line.startswith("--- ") or line.startswith("+++ "):
            continue
        count += 1
    return count


def compute_diff_quality(
    original_code: str,
    expected_output: str,
    adapter_output: str,
) -> DiffQuality:
    """Compute all five diff-quality metrics for a single fixture case.

    Args:
        original_code: The pre-edit source.
        expected_output: The gold post-edit source (used to derive
            ``loc_required`` and the set of originally unchanged lines).
        adapter_output: What the adapter under test produced.

    Returns:
        A :class:`DiffQuality` with all five metrics populated under the
        conventions documented in the module docstring.
    """

    gold_diff = _unified_diff_zero_context(original_code, expected_output)
    adapter_diff = _unified_diff_zero_context(original_code, adapter_output)

    loc_required = _num_lines(gold_diff)
    loc_changed = _num_lines(adapter_diff)

    minimality_ratio = float(loc_changed) / float(max(loc_required, 1))

    hunk_count = sum(1 for line in adapter_diff if line.startswith("@@"))

    # ``context_lines_preserved``: of the lines that were supposed to be
    # kept verbatim (present in both original and expected), how many
    # survive in the adapter output?
    original_line_set = set(original_code.splitlines())
    expected_line_set = set(expected_output.splitlines())
    adapter_line_set = set(adapter_output.splitlines())

    unchanged_lines = original_line_set & expected_line_set
    if not unchanged_lines:
        # Full replacement — nothing to preserve, vacuously 1.0.
        context_lines_preserved = 1.0
    else:
        removed_count = len(unchanged_lines - adapter_line_set)
        context_lines_preserved = 1.0 - (removed_count / len(unchanged_lines))

    return DiffQuality(
        loc_required=loc_required,
        loc_changed=loc_changed,
        minimality_ratio=minimality_ratio,
        hunk_count=hunk_count,
        context_lines_preserved=context_lines_preserved,
    )
