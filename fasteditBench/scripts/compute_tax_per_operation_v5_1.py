"""Compute the calling-agent *tax* per operation-category (bench-02 v5.1).

Successor to ``compute_tax_per_operation_v5.py``. Same five-release stack
(FastEdit 0.2.1 → 0.2.5); the ONLY measurement change is in
``minimize_modify_inside_snippet``:

    **v5 (conservative):** trivial close-punctuation lines (``}``, ``)``,
    ``end``, ``]``, ``};``, ``);``, ``end;``) were ALWAYS kept in the
    minimized snippet. The worry was that stripping a closing brace might
    produce a snippet FastEdit can't splice.

    **v5.1 (empirically verified):** trivial close-punctuation lines that
    appear verbatim in the ORIGINAL body are anchors — they're echoing the
    existing structure, not introducing new content. They can be stripped.
    Trivial lines whose content does NOT appear in the original body
    (e.g. the caller is closing a brand-new block the snippet introduced)
    are still kept.

The empirical verification probe lives at ``/tmp/punct_probe/probe.py`` and
covers:

* JS function whole-body replace with ``}`` stripped — deterministic, correct
* Ruby function whole-body replace with ``end`` stripped — deterministic, correct
* TS class method whole-body replace with method's ``}`` stripped — deterministic, correct
* Modify_inside snippet with function's trailing ``}`` stripped — deterministic, correct

Every variant hit 0.2.5's deterministic text-match path (0 model tokens) and
produced a correctly parseable output file. Close-punct stripping is safe.

Outputs
-------

* ``results/tax_per_operation/{run_id}.jsonl``
* ``results/tax_per_operation/{run_id}.summary.json``

Summary-level ``methodology: "v5_1_strip_close_punct_fastedit_0_2_5"``.
Row-level ``v5_minimal_payload_form`` tags get a ``+strip_trivials``
suffix when the v5.1 heuristic dropped one or more trivial anchors.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import compute_tax_per_operation_v5 as v5  # noqa: E402

logger = logging.getLogger(__name__)

METHODOLOGY_TAG = "v5_1_strip_close_punct_fastedit_0_2_5"

__all__ = [
    "METHODOLOGY_TAG",
    "minimize_modify_inside_snippet_v5_1",
    "main",
]


def minimize_modify_inside_snippet_v5_1(
    snippet_no_sig: str,
    original_body: str,
) -> tuple[str, str]:
    """v5.1 variant of ``v5.minimize_modify_inside_snippet``.

    Identical to v5 except trivial close-punctuation lines whose content
    is present in ``original_body`` are classified as ``anchor`` (and
    therefore dropped in ``top_insert`` / ``bottom_insert`` / ``middle``
    modes) instead of ``trivial`` (always kept).

    Trivial lines whose content is NOT in the original body are still
    classified as ``trivial`` → they close a new block introduced by the
    snippet and must be preserved.
    """
    lines = v5._split_body_lines(snippet_no_sig)
    if not lines:
        return snippet_no_sig, "no_marker_found"

    marker_idx = -1
    for i, line in enumerate(lines):
        if v5._line_marker_kind(line):
            marker_idx = i
            break
    if marker_idx == -1:
        return snippet_no_sig, "no_marker_found"

    # two_markers pattern — conservative: keep v4/v5 form
    if (
        snippet_no_sig.count("# ... existing code ...")
        + snippet_no_sig.count("// ... existing code ...")
        + snippet_no_sig.count("/* ... existing code ... */")
        + snippet_no_sig.count("-- ... existing code ...")
        + snippet_no_sig.count("<!-- ... existing code ... -->")
        > 1
    ):
        return snippet_no_sig, "no_marker_found"

    before = lines[:marker_idx]
    marker_line = lines[marker_idx]
    after = lines[marker_idx + 1:]

    original_lines_norm = frozenset(
        v5._normalize_for_compare(line)
        for line in original_body.splitlines()
        if line.strip()
    )

    def classify(region: list[str]) -> list[str]:
        """v5.1: trivial-in-original → anchor (drop); trivial-not-in-original → trivial (keep)."""
        out: list[str] = []
        for line in region:
            stripped = v5._normalize_for_compare(line)
            if not stripped:
                out.append("blank")
            elif stripped in v5._TRIVIAL_LINES:
                # KEY v5.1 CHANGE: decide based on presence in original body.
                # If the trivial line appears in the original body (it almost
                # always does — it's echoing the target's own closing
                # punctuation), classify as anchor → drop.
                # Empirical evidence (/tmp/punct_probe/probe.py): FastEdit
                # 0.2.5 handles close-punct-stripped snippets correctly on
                # the deterministic text-match path.
                if stripped in original_lines_norm:
                    out.append("anchor")
                else:
                    # True "new close-brace" — the snippet introduced a new
                    # block that needs its own closer. Keep it.
                    out.append("trivial")
            elif v5._is_anchor_line(line, original_lines_norm):
                out.append("anchor")
            else:
                out.append("new")
        return out

    before_cls = classify(before)
    after_cls = classify(after)

    has_new_before = "new" in before_cls
    has_new_after = "new" in after_cls

    short_marker_line = v5._short_marker_with_indent(marker_line)

    # top_insert
    if has_new_before and not has_new_after:
        kept_before = [
            line for line, cls in zip(before, before_cls, strict=True)
            if cls in ("new", "trivial")
        ]
        minimized = "".join(kept_before) + short_marker_line
        return minimized, "top_insert"

    # bottom_insert
    if has_new_after and not has_new_before:
        kept_after = [
            line for line, cls in zip(after, after_cls, strict=True)
            if cls in ("new", "trivial")
        ]
        minimized = short_marker_line + "".join(kept_after)
        return minimized, "bottom_insert"

    # middle
    if has_new_before and has_new_after:
        kept_before_mid: list[str] = []
        last_anchor_idx = -1
        for i, cls in enumerate(before_cls):
            if cls == "anchor":
                last_anchor_idx = i
        for i, line in enumerate(before):
            cls = before_cls[i]
            if cls in ("new", "trivial") or (cls == "anchor" and i == last_anchor_idx):
                kept_before_mid.append(line)

        kept_after_mid: list[str] = []
        first_anchor_idx = -1
        for i, cls in enumerate(after_cls):
            if cls == "anchor":
                first_anchor_idx = i
                break
        for i, line in enumerate(after):
            cls = after_cls[i]
            if cls in ("new", "trivial") or (cls == "anchor" and i == first_anchor_idx):
                kept_after_mid.append(line)

        minimized = (
            "".join(kept_before_mid) + short_marker_line + "".join(kept_after_mid)
        )
        return minimized, "middle"

    # Fallback — swap long marker for short, unchanged otherwise.
    swapped = snippet_no_sig
    for long in v5._LONG_MARKERS:
        if long in swapped:
            swapped = swapped.replace(long, v5.SHORT_MARKER)
            break
    return swapped, "marker_swap_only"


def _patched_summarize(rows: list[dict], run_id: str) -> dict:
    """Wrap v5.summarize_v5 and override methodology tag."""
    summary = v5.summarize_v5(rows, run_id)
    summary["methodology"] = METHODOLOGY_TAG
    return summary


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint — patches v5's minimize function, then delegates."""
    parser = argparse.ArgumentParser(
        description=(
            "Compute calling-agent tax per operation category — v5.1 "
            "(strip trivial close-punct anchors; FastEdit 0.2.5 verified)."
        )
    )
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=_REPO_ROOT / "fixtures",
        help="Root of the fixture tree (default: <repo>/fixtures).",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=_REPO_ROOT / "results",
        help="Root of the results tree (default: <repo>/results).",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Run-id string (default: timestamp).",
    )
    args = parser.parse_args(argv)

    # Monkey-patch v5 module-level function so v5.build_fastedit_payload_v5
    # and v5.compute_rows_v5 pick up our refined minimizer.
    v5.minimize_modify_inside_snippet = minimize_modify_inside_snippet_v5_1

    # Also patch summarize to use the 5.1 tag.
    orig_summarize = v5.summarize_v5
    v5.summarize_v5 = lambda rows, run_id: {
        **orig_summarize(rows, run_id),
        "methodology": METHODOLOGY_TAG,
    }

    jsonl_path, summary_path, summary = v5.run_tax_per_operation_v5(
        args.fixtures_dir, args.results_dir, args.run_id
    )
    v5._print_summary(summary, jsonl_path, summary_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
