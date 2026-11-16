"""Compute the calling-agent *tax* per operation-category (bench-02 v6).

Identical measurement methodology to v5.1. The only change is the fixture
set: bench-02 Phase 2 added 20 realistic-size fixtures on top of the
original 156 (see commit 345649b), bringing the total to 176.

The point of v6 is to run the same v5.1 measurement on the enlarged
fixture set so the headline savings reflect production-scale edits, not
just precision-test micro-edits. v5.1 landed at 49.4% headline on 156
fixtures; size-stratified analysis showed fixtures with native_tokens
>= 120 were already hitting 57%+. v6 is expected to rise into the
55-60% band.

Outputs
-------

* ``results/tax_per_operation/{run_id}.jsonl``
* ``results/tax_per_operation/{run_id}.summary.json``

Summary-level ``methodology: "v6_176_fixtures_fastedit_0_2_5"``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import compute_tax_per_operation_v5 as v5  # noqa: E402
import compute_tax_per_operation_v5_1 as v5_1  # noqa: E402

METHODOLOGY_TAG = "v6_176_fixtures_fastedit_0_2_5"

__all__ = [
    "METHODOLOGY_TAG",
    "main",
]


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint — patch in v5.1 minimizer, override methodology tag."""
    parser = argparse.ArgumentParser(
        description=(
            "Compute calling-agent tax per operation category — v6 "
            "(176 fixtures, v5.1 minimizer, FastEdit 0.2.5)."
        )
    )
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=_REPO_ROOT / "fixtures",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=_REPO_ROOT / "results",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
    )
    args = parser.parse_args(argv)

    # Patch in v5.1 minimizer.
    v5.minimize_modify_inside_snippet = v5_1.minimize_modify_inside_snippet_v5_1

    # Override methodology tag to v6.
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
