"""Back-fill bench-02 CaseRow fields onto historical FastEdit runs (VAL-101).

Historical FastEdit JSONL rows (from bench-01) predate the
frontier-LLM comparator schema extensions. Side-by-side comparison in
the bench-02 report requires every row to carry:

- input_tokens (default 0 — FastEdit's merge model only consumes the
  snippet; the host-side prompt tokens are negligible and not metered)
- output_tokens (default = the existing tokens_generated value)
- latency_ms (default None — speed was explicitly out-of-scope for
  bench-01, see DESIGN.md § 1)
- tool_call_count (default None — FastEdit uses no tool calls)
- retry_count (default 0)
- input_tokens_native / output_tokens_native (default None — no
  cross-tokenizer check was performed on bench-01)

This script rewrites each ``results/fastedit/*.jsonl`` to
``*.migrated.jsonl`` (non-destructive — originals untouched) and
regenerates the ``*.migrated.summary.json`` via
:func:`fasteditbench.reporting.aggregate`. A migrated run whose
tier1_pass_rate does not round-trip to the original rate is a hard
error: the script exits non-zero rather than silently produce a broken
summary.

Run with no arguments to migrate every ``results/fastedit/*.jsonl``,
or pass explicit paths to restrict the migration. Example::

    uv run python scripts/migrate_historical_runs.py \\
        results/fastedit/20260422-181127.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running from the repo root without installing the package.
REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fasteditbench.reporting import aggregate, write_summary  # noqa: E402

# Field -> default for bench-02 extensions. Keeping this as a module
# constant makes the "what's new" set self-documenting.
_NEW_FIELD_DEFAULTS: dict[str, object] = {
    "input_tokens": 0,
    "output_tokens": 0,
    "latency_ms": None,
    "tool_call_count": None,
    "retry_count": 0,
    "input_tokens_native": None,
    "output_tokens_native": None,
    # bench-02 VAL-300 (M3): token-floor comparator fields. Historical
    # FastEdit rows didn't compute a per-fixture floor, so these stay
    # None on back-fill.
    "edit_payload_tokens": None,
    "tokenizer_used": None,
    "tokenizer_proxy": None,
}


def migrate_row(row: dict) -> dict:
    """Return a new row with bench-02 fields back-filled.

    ``output_tokens`` inherits from the legacy ``tokens_generated``
    (FastEdit's only token metric is merge-model output). All other new
    fields use the module-level defaults. Existing fields are preserved
    verbatim so the migrated row remains a strict superset.
    """
    migrated = dict(row)
    # Inherit output_tokens from legacy tokens_generated when the row
    # carries one. None/missing -> 0 (consistent with historical
    # null-safe default).
    tg = row.get("tokens_generated")
    default_output = int(tg) if isinstance(tg, (int, float)) else 0
    for field, default in _NEW_FIELD_DEFAULTS.items():
        if field in migrated:
            # Already migrated (idempotent). Skip.
            continue
        if field == "output_tokens":
            migrated[field] = default_output
        else:
            migrated[field] = default
    return migrated


def migrate_file(jsonl_path: Path) -> Path:
    """Read ``jsonl_path``, write ``*.migrated.jsonl`` next to it.

    Returns the path to the migrated JSONL. Existing migrated files are
    overwritten (the migration is deterministic + idempotent, so a
    re-run is safe).
    """
    out_path = jsonl_path.with_suffix(".migrated.jsonl")
    with jsonl_path.open("r", encoding="utf-8") as src_fh, out_path.open(
        "w", encoding="utf-8"
    ) as dst_fh:
        for line in src_fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            migrated = migrate_row(row)
            dst_fh.write(json.dumps(migrated) + "\n")
    return out_path


def verify_roundtrip(
    original_jsonl: Path, migrated_jsonl: Path
) -> tuple[float, float]:
    """Aggregate both files and assert tier1 rates match within float eps.

    Returns ``(orig_tier1, migrated_tier1)``.
    """
    orig_summary = aggregate(original_jsonl)
    migrated_summary = aggregate(migrated_jsonl)
    orig_rate = orig_summary["overall"]["tier1_pass_rate"]
    migrated_rate = migrated_summary["overall"]["tier1_pass_rate"]
    if abs(orig_rate - migrated_rate) > 1e-9:
        raise SystemExit(
            f"Tier-1 rate drifted during migration: "
            f"{orig_rate} -> {migrated_rate} (file: {original_jsonl})"
        )
    return orig_rate, migrated_rate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help=(
            "Specific JSONL files to migrate. If omitted, every "
            "results/fastedit/*.jsonl (excluding *.migrated.jsonl) is "
            "migrated."
        ),
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=REPO_ROOT / "results",
        help="Root results directory (default: <repo>/results).",
    )
    args = parser.parse_args(argv)

    if args.paths:
        targets = [Path(p) for p in args.paths]
    else:
        fastedit_dir = args.results_dir / "fastedit"
        targets = sorted(
            p
            for p in fastedit_dir.glob("*.jsonl")
            if not p.name.endswith(".migrated.jsonl")
        )

    if not targets:
        print("No JSONL files to migrate.", flush=True)
        return 0

    for src in targets:
        if not src.exists():
            print(f"skip (missing): {src}", flush=True)
            continue
        migrated = migrate_file(src)
        # Regenerate summary for the migrated JSONL. The original
        # summary is left in place so audits can diff old vs new.
        migrated_summary_path = migrated.with_suffix(".summary.json")
        write_summary(migrated, migrated_summary_path)
        orig_rate, migrated_rate = verify_roundtrip(src, migrated)
        print(
            f"migrated: {src.name} -> {migrated.name} "
            f"(tier1: {orig_rate:.4f} -> {migrated_rate:.4f})",
            flush=True,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
