"""Run the token-floor computation across all fixtures and models.

Usage::

    uv run --no-sync python scripts/run_floor.py            # both models
    uv run --no-sync python scripts/run_floor.py --models openai/gpt-5.4
    uv run --no-sync python scripts/run_floor.py --force
    uv run --no-sync python scripts/run_floor.py --run-id 20260422-190000

Produces ``results/{model_slug}/{run_id}.jsonl`` +
``{run_id}.summary.json`` per model, then prints a final summary table
to stdout (per-model n_cases, total_payload_tokens, mean, median, error
count). Wall-clock target: well under 1 minute when both paths are
offline (no ``ANTHROPIC_API_KEY`` set).
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from pathlib import Path

# Allow running from the repo root without installing the package.
REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fasteditbench.floor_runner import model_slug, run_floor  # noqa: E402

_DEFAULT_MODELS: tuple[str, ...] = (
    "openai/gpt-5.4",
    "anthropic/claude-opus-4.7",
)


def _print_summary_table(out_paths: dict[str, Path]) -> None:
    """Print a compact per-model summary to stdout."""
    print("\n" + "=" * 72)
    print(f"{'model':<35} {'n_ok':>5} {'n_err':>5} {'total':>8} "
          f"{'mean':>7} {'median':>7}")
    print("-" * 72)
    for model_id, jsonl_path in out_paths.items():
        rows: list[dict] = []
        with jsonl_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))

        n_err = sum(1 for r in rows if r.get("error"))
        tokens = [
            r.get("edit_payload_tokens")
            for r in rows
            if r.get("edit_payload_tokens") is not None
        ]
        total = sum(tokens) if tokens else 0
        mean = statistics.fmean(tokens) if tokens else 0.0
        median = statistics.median(tokens) if tokens else 0.0
        n_ok = len(tokens)
        print(
            f"{model_id:<35} {n_ok:>5} {n_err:>5} {total:>8} "
            f"{mean:>7.1f} {median:>7.1f}"
        )
    print("=" * 72)
    for model_id, jsonl_path in out_paths.items():
        summary_path = jsonl_path.with_suffix(".summary.json")
        print(f"  {model_id}")
        print(f"    jsonl   : {jsonl_path}")
        print(f"    summary : {summary_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--models",
        action="append",
        default=None,
        help=(
            "Model id to run (repeatable). Defaults to both: "
            f"{', '.join(_DEFAULT_MODELS)}"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing JSONL instead of resuming.",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Explicit run id (default: current UTC time YYYYMMDD-HHMMSS).",
    )
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=REPO_ROOT / "fixtures",
        help="Fixture root (default: <repo>/fixtures).",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=REPO_ROOT / "results",
        help="Results root (default: <repo>/results).",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=8,
        help="Max threads for the Opus network path (default: 8).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable INFO-level logging.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    models = list(args.models) if args.models else list(_DEFAULT_MODELS)

    out_paths = run_floor(
        models=models,
        fixtures_dir=args.fixtures_dir,
        results_dir=args.results_dir,
        run_id=args.run_id,
        force=args.force,
        max_workers=args.max_workers,
    )

    # Confirm each model got an output. _run_one_model always writes the
    # JSONL, even when no pending fixtures remain (pure resume case).
    for model_id in models:
        slug = model_slug(model_id)
        if model_id not in out_paths:
            print(f"MISSING: no output for {model_id} (slug={slug})",
                  file=sys.stderr)
            return 2

    _print_summary_table(out_paths)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
