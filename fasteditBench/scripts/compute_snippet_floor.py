"""Compute the calling-agent FastEdit snippet token cost (bench-02 v2).

This script answers the *apples-to-apples* question the first bench-02
report got wrong:

    "How many output tokens does the CALLING AGENT emit to invoke each
     edit tool? Native edit vs FastEdit's snippet interface?"

For every fixture we build both tool-call payloads:

* Native edit  — ``{file_path, old_string, new_string}`` (matches M2's
  :class:`~fasteditbench.backends.tokenizer_floor_adapter.TokenizerFloorAdapter`
  serialization exactly, so the per-row ``native_edit_tokens`` total
  matches the VAL-300 floor run to within rounding).
* FastEdit snippet — ``{file_path, snippet, [after|replace]}`` where
  ``snippet`` is the fixture's verbatim ``snippet`` field (the code
  snippet with ``# ... existing code ...`` markers that FastEdit's MCP
  surface accepts).

Both payloads are serialized via ``json.dumps(..., separators=(",", ":"))``
and tokenized with ``tiktoken.get_encoding("o200k_base")`` — the same
encoding M3 used for the GPT floor. The direct comparison is fair
because both sides measure the calling agent's output cost with the
same tokenizer.

File-path synthesis reuses
:func:`fasteditbench.floor_runner.synthesize_file_path` so the
``file_path`` fields in both payloads are byte-for-byte identical to the
VAL-300 run, keeping the native-edit totals directly comparable to
``results/openai_gpt-5.4/20260422-213958.summary.json``.

Outputs
-------

* ``results/fastedit_snippet/{run_id}.jsonl`` — one row per fixture with
  fields: ``case_id, language, pattern, file_size_bytes, snippet_tokens,
  native_edit_tokens, savings_tokens, savings_pct``.
* ``results/fastedit_snippet/{run_id}.summary.json`` — totals, per-language
  breakdown, per-pattern breakdown, top-5 largest savings, top-5 smallest
  (including any negative-savings rows).

A summary table is printed to stdout on completion.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import tiktoken

# Make the sibling src/ layout importable when the script is run
# directly (``uv run python scripts/compute_snippet_floor.py``).
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from fasteditbench.backends.tokenizer_floor_adapter import (  # noqa: E402
    _serialize_tool_call,
    extract_edit_payloads,
)
from fasteditbench.floor_runner import synthesize_file_path  # noqa: E402
from fasteditbench.runner import load_fixtures  # noqa: E402

logger = logging.getLogger(__name__)

__all__ = [
    "SnippetRow",
    "build_snippet_payload",
    "compute_rows",
    "main",
    "run_snippet_floor",
    "summarize",
]

# tiktoken encoding — same as GPT floor for apples-to-apples comparison.
_ENCODING_NAME = "o200k_base"


def build_snippet_payload(
    file_path: str,
    snippet: str,
    target_kind: str | None,
    target_name: str | None,
) -> str:
    """Serialize a FastEdit tool-call exactly as a calling agent emits it.

    Shape mirrors the ``fast_edit`` MCP tool surface:

        {
          "name": "fast_edit",
          "arguments": {
            "file_path": "...",
            "snippet": "...",
            # only present when the fixture declares a targeted edit:
            "replace": "<name>"  # when target_kind == "replace"
            "after":   "<name>"  # when target_kind == "after"
          }
        }

    JSON is emitted with no whitespace (``separators=(",", ":")``) to
    match the native-edit payload's serialization choice.
    """
    arguments: dict[str, str] = {
        "file_path": file_path,
        "snippet": snippet,
    }
    if target_kind == "replace" and target_name:
        arguments["replace"] = target_name
    elif target_kind == "after" and target_name:
        arguments["after"] = target_name
    return json.dumps(
        {"name": "fast_edit", "arguments": arguments},
        separators=(",", ":"),
    )


def _native_edit_tokens(
    enc: tiktoken.Encoding,
    file_path: str,
    original: str,
    expected: str,
) -> int:
    """Sum native-edit payload tokens across all hunks for one fixture.

    Replicates :meth:`TokenizerFloorAdapter.count_floor` on the GPT path
    (o200k_base) without bringing in the Opus / proxy branches.
    """
    total = 0
    for old, new in extract_edit_payloads(original, expected):
        payload = _serialize_tool_call(file_path, old, new)
        total += len(enc.encode(payload))
    return total


class SnippetRow(dict):
    """Typed-ish row dict for the output JSONL.

    Subclasses ``dict`` so ``json.dumps`` works without an adapter.
    """


def compute_rows(fixtures_dir: Path) -> list[SnippetRow]:
    """Iterate every fixture and compute both token costs per row.

    Args:
        fixtures_dir: Root of ``fixtures/{lang}/*.yaml``.

    Returns:
        One :class:`SnippetRow` per fixture, in the deterministic order
        produced by :func:`fasteditbench.runner.load_fixtures`.
    """
    enc = tiktoken.get_encoding(_ENCODING_NAME)
    rows: list[SnippetRow] = []

    for case_id, fixture in load_fixtures(fixtures_dir, langs=None):
        language = str(fixture.get("language", ""))
        pattern = str(fixture.get("pattern", ""))
        snippet = str(fixture.get("snippet", ""))
        original = str(fixture.get("original_code", ""))
        expected = str(fixture.get("expected_output", ""))
        target_kind = fixture.get("target_kind")
        target_name = fixture.get("target_name")

        file_path = synthesize_file_path(case_id, language)

        snippet_payload = build_snippet_payload(
            file_path,
            snippet,
            target_kind if isinstance(target_kind, str) else None,
            target_name if isinstance(target_name, str) else None,
        )
        snippet_tokens = len(enc.encode(snippet_payload))
        native_tokens = _native_edit_tokens(
            enc, file_path, original, expected
        )
        savings = native_tokens - snippet_tokens
        savings_pct = (
            (savings / native_tokens * 100.0) if native_tokens else 0.0
        )

        rows.append(
            SnippetRow(
                case_id=case_id,
                language=language,
                pattern=pattern,
                file_size_bytes=len(original.encode("utf-8")),
                snippet_tokens=snippet_tokens,
                native_edit_tokens=native_tokens,
                savings_tokens=savings,
                savings_pct=round(savings_pct, 3),
            )
        )

    return rows


def summarize(rows: list[SnippetRow]) -> dict:
    """Build the summary dict for ``{run_id}.summary.json``.

    Includes totals, overall savings percentage, per-language breakdown,
    per-pattern breakdown, and the top-5 largest + top-5 smallest
    savings rows (smallest list includes any negative-savings cases).
    """
    n = len(rows)
    total_snippet = sum(r["snippet_tokens"] for r in rows)
    total_native = sum(r["native_edit_tokens"] for r in rows)
    total_savings = total_native - total_snippet
    savings_pct = (
        (total_savings / total_native * 100.0) if total_native else 0.0
    )
    ratio = (total_native / total_snippet) if total_snippet else 0.0

    per_language: dict[str, dict] = defaultdict(
        lambda: {
            "n_cases": 0,
            "total_snippet_tokens": 0,
            "total_native_edit_tokens": 0,
            "total_savings_tokens": 0,
        }
    )
    for r in rows:
        bucket = per_language[r["language"]]
        bucket["n_cases"] += 1
        bucket["total_snippet_tokens"] += r["snippet_tokens"]
        bucket["total_native_edit_tokens"] += r["native_edit_tokens"]
        bucket["total_savings_tokens"] += r["savings_tokens"]
    for bucket in per_language.values():
        nat = bucket["total_native_edit_tokens"]
        sni = bucket["total_snippet_tokens"]
        bucket["savings_pct"] = round(
            (bucket["total_savings_tokens"] / nat * 100.0) if nat else 0.0,
            3,
        )
        bucket["ratio_native_over_snippet"] = round(
            (nat / sni) if sni else 0.0, 3
        )

    per_pattern: dict[str, dict] = defaultdict(
        lambda: {
            "n_cases": 0,
            "total_snippet_tokens": 0,
            "total_native_edit_tokens": 0,
            "total_savings_tokens": 0,
        }
    )
    for r in rows:
        bucket = per_pattern[r["pattern"]]
        bucket["n_cases"] += 1
        bucket["total_snippet_tokens"] += r["snippet_tokens"]
        bucket["total_native_edit_tokens"] += r["native_edit_tokens"]
        bucket["total_savings_tokens"] += r["savings_tokens"]
    for bucket in per_pattern.values():
        nat = bucket["total_native_edit_tokens"]
        bucket["savings_pct"] = round(
            (bucket["total_savings_tokens"] / nat * 100.0) if nat else 0.0,
            3,
        )

    sorted_by_savings = sorted(
        rows, key=lambda r: r["savings_tokens"], reverse=True
    )
    top5_largest = sorted_by_savings[:5]
    top5_smallest = sorted_by_savings[-5:][::-1]  # ascending, worst first

    return {
        "tokenizer_used": f"tiktoken:{_ENCODING_NAME}",
        "n_cases": n,
        "totals": {
            "snippet_tokens": total_snippet,
            "native_edit_tokens": total_native,
            "savings_tokens": total_savings,
            "savings_pct": round(savings_pct, 3),
            "ratio_native_over_snippet": round(ratio, 3),
        },
        "per_language": dict(per_language),
        "per_pattern": dict(per_pattern),
        "top5_largest_savings": top5_largest,
        "top5_smallest_savings": top5_smallest,
    }


def run_snippet_floor(
    fixtures_dir: Path,
    results_dir: Path,
    run_id: str | None = None,
) -> tuple[Path, Path, dict]:
    """Compute rows, write JSONL + summary, return paths and summary."""
    if run_id is None:
        run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")

    out_dir = results_dir / "fastedit_snippet"
    out_dir.mkdir(parents=True, exist_ok=True)

    jsonl_path = out_dir / f"{run_id}.jsonl"
    summary_path = out_dir / f"{run_id}.summary.json"

    rows = compute_rows(fixtures_dir)

    with jsonl_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    summary = summarize(rows)
    with summary_path.open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)

    return jsonl_path, summary_path, summary


def _print_summary(summary: dict, jsonl_path: Path, summary_path: Path) -> None:
    """Print a human-readable summary table to stdout."""
    totals = summary["totals"]
    print("=" * 72)
    print("bench-02 calling-agent FastEdit snippet floor")
    print("=" * 72)
    print(f"  fixtures:            {summary['n_cases']}")
    print(f"  tokenizer:           {summary['tokenizer_used']}")
    print(f"  native edit tokens:  {totals['native_edit_tokens']:>8}")
    print(f"  snippet tokens:      {totals['snippet_tokens']:>8}")
    print(f"  savings tokens:      {totals['savings_tokens']:>8}")
    print(f"  savings pct:         {totals['savings_pct']:>7.2f}%")
    print(f"  ratio (native/snip): {totals['ratio_native_over_snippet']:>7.3f}x")
    print()
    print("per language (sorted by savings pct desc):")
    sorted_langs = sorted(
        summary["per_language"].items(),
        key=lambda kv: kv[1]["savings_pct"],
        reverse=True,
    )
    print(f"  {'lang':<12} {'n':>3} {'native':>8} {'snippet':>8} "
          f"{'saved':>8} {'%':>7}")
    for lang, b in sorted_langs:
        print(
            f"  {lang:<12} {b['n_cases']:>3} "
            f"{b['total_native_edit_tokens']:>8} "
            f"{b['total_snippet_tokens']:>8} "
            f"{b['total_savings_tokens']:>8} "
            f"{b['savings_pct']:>6.2f}%"
        )
    print()
    print("top 5 largest savings:")
    for r in summary["top5_largest_savings"]:
        print(
            f"  +{r['savings_tokens']:>5} tok ({r['savings_pct']:>6.2f}%)  "
            f"{r['case_id']:<35} [{r['pattern']}]"
        )
    print()
    print("top 5 smallest savings (incl. negatives):")
    for r in summary["top5_smallest_savings"]:
        print(
            f"  {r['savings_tokens']:>+6} tok ({r['savings_pct']:>+7.2f}%)  "
            f"{r['case_id']:<35} [{r['pattern']}]"
        )
    print()
    print(f"wrote: {jsonl_path}")
    print(f"wrote: {summary_path}")


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description=(
            "Compute the calling-agent FastEdit snippet token cost "
            "across every bench-02 fixture and emit JSONL + summary."
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
        help="Explicit run id; default is the current UTC timestamp.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    jsonl_path, summary_path, summary = run_snippet_floor(
        args.fixtures_dir, args.results_dir, args.run_id
    )
    _print_summary(summary, jsonl_path, summary_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
