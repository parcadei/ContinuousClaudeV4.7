"""Per-language aggregation over the JSONL output (VAL-301).

The runner produces one JSON line per case. For the final report we
need per-language summaries (pass rates, mean metrics, error counts)
plus a grand total. This module computes those summaries from the
JSONL without re-running the benchmark.

Summary schema (written next to the JSONL as ``{run_id}.summary.json``)::

    {
      "adapter":        str,
      "run_id":         str,
      "total_cases":    int,
      "total_languages": int,
      "per_language": {
        "<lang>": {
          "n_cases":                   int,
          "n_errors":                  int,
          "tier1_pass_rate":           float,  # 0.0–1.0, excludes errors
          "tier2_pass_rate":           float,
          "mean_minimality_ratio":     float,
          "median_minimality_ratio":   float,
          "mean_hunk_count":           float,
          "mean_context_lines_preserved": float,
          "total_tokens_generated":    int
        },
        ...
      },
      "overall": { same fields, aggregated across all languages }
    }

Conventions:

* Per-language rates (``per_language[lang].tier*_pass_rate``) are
  computed over non-error rows only: rows with ``error != null`` are
  excluded from the denominator. Rationale: don't punish a language
  for provider/infra errors outside its scope.
* Overall rates (``overall.tier*_pass_rate``) are **true
  micro-averages**: total_passes / total_n_cases, INCLUDING error
  rows in the denominator. Rationale: the headline number should
  reflect "how often does the adapter produce a correct edit over
  the whole benchmark" — errors are failures from the user's view.
* Means (minimality, hunk count, context preserved) always exclude
  error rows since there's no sensible value for an errored row.
* ``n_errors`` makes the failure count visible in the summary.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

__all__ = ["aggregate", "write_summary"]


def _init_lang_accum() -> dict[str, Any]:
    """Build a fresh accumulator dict for one language (or 'overall').

    bench-02 (VAL-101) extends this with LLM-comparator accumulators:
    input/output token means, latency mean, tool-call-count mean, retry
    count mean, and cumulative cost. The underscore-prefixed lists hold
    raw values so :func:`_finalize` can compute both means safely (None
    entries are skipped per-field). ``total_cost_usd`` is a scalar since
    it's a straight sum.
    """
    return {
        "n_cases": 0,
        "n_errors": 0,
        "_tier1_passes": 0,
        "_tier2_passes": 0,
        "_minimality_ratios": [],
        "_hunk_counts": [],
        "_context_preserved": [],
        "total_tokens_generated": 0,
        # bench-02 VAL-101: frontier-LLM comparator accumulators.
        "_input_tokens": [],
        "_output_tokens": [],
        "_latency_ms": [],
        "_tool_call_counts": [],
        "_retry_counts": [],
        "total_cost_usd": 0.0,
    }


def _finalize(accum: dict[str, Any], *, rate_over_total: bool = False) -> dict[str, Any]:
    """Collapse a per-language accumulator into its public-shape dict.

    Division is guarded: when no rows qualify for the rate denominator,
    rates and means collapse to 0.0 (not NaN, not KeyError).
    ``n_cases`` counts ALL rows (including error rows); ``n_errors``
    is the subset.

    Args:
        accum: The accumulator dict built by :func:`_init_lang_accum`.
        rate_over_total: When False (per-language default), tier rates
            are computed over non-error rows only — errors are excluded
            from the denominator. When True (overall rollup), tier
            rates are true micro-averages: total_passes / total_n_cases,
            so errors count against the headline number. Means
            (minimality, hunks, context) always exclude error rows on
            both paths since there's no sensible value for an errored
            row.
    """
    n_total = accum["n_cases"]
    n_err = accum["n_errors"]
    n_ok = n_total - n_err

    rate_denom = n_total if rate_over_total else n_ok

    if rate_denom > 0:
        tier1_rate = accum["_tier1_passes"] / rate_denom
        tier2_rate = accum["_tier2_passes"] / rate_denom
    else:
        tier1_rate = 0.0
        tier2_rate = 0.0

    if n_ok > 0:
        mean_min = statistics.fmean(accum["_minimality_ratios"])
        median_min = statistics.median(accum["_minimality_ratios"])
        mean_hunks = statistics.fmean(accum["_hunk_counts"])
        mean_ctx = statistics.fmean(accum["_context_preserved"])
    else:
        mean_min = 0.0
        median_min = 0.0
        mean_hunks = 0.0
        mean_ctx = 0.0

    # bench-02 VAL-101: nullable-safe means over LLM-comparator fields.
    # None entries are skipped per-field; an all-None list collapses to
    # None (not 0.0) so a historical FastEdit summary doesn't
    # misleadingly report "mean_latency_ms: 0.0" when no latency was
    # recorded. Per-field independence is important: a FastEdit run has
    # input_tokens=0 across the board but latency_ms=None throughout.
    mean_input_tokens = _safe_mean(accum["_input_tokens"])
    mean_output_tokens = _safe_mean(accum["_output_tokens"])
    mean_latency_ms = _safe_mean(accum["_latency_ms"])
    mean_tool_call_count = _safe_mean(accum["_tool_call_counts"])
    mean_retry_count = _safe_mean(accum["_retry_counts"])

    return {
        "n_cases": n_total,
        "n_errors": n_err,
        "tier1_pass_rate": tier1_rate,
        "tier2_pass_rate": tier2_rate,
        "mean_minimality_ratio": mean_min,
        "median_minimality_ratio": median_min,
        "mean_hunk_count": mean_hunks,
        "mean_context_lines_preserved": mean_ctx,
        "total_tokens_generated": accum["total_tokens_generated"],
        # bench-02 VAL-101 additions.
        "mean_input_tokens": mean_input_tokens,
        "mean_output_tokens": mean_output_tokens,
        "mean_latency_ms": mean_latency_ms,
        "mean_tool_call_count": mean_tool_call_count,
        "mean_retry_count": mean_retry_count,
        "total_cost_usd": round(accum["total_cost_usd"], 6),
    }


def _safe_mean(values: list) -> float | None:
    """Compute the mean of non-None values, or None if all entries are None.

    Mirrors ``statistics.fmean`` semantics for the non-empty case but
    tolerates ``None`` entries (skipped). Returns ``None`` (not 0.0) when
    no numeric values are present — important so historical runs that
    didn't record a field are flagged as "no data" rather than "zero".
    """
    numeric = [v for v in values if v is not None]
    if not numeric:
        return None
    return statistics.fmean(numeric)


def aggregate(jsonl_path: Path) -> dict[str, Any]:
    """Read a benchmark JSONL file and return the summary dict.

    Args:
        jsonl_path: Path to ``{adapter}/{run_id}.jsonl`` as written by
            :func:`fasteditbench.runner.run_all`.

    Returns:
        A dict matching the schema documented at module level. The
        ``adapter`` and ``run_id`` fields are inferred from the path:
        ``results/{adapter}/{run_id}.jsonl``.

    Raises:
        FileNotFoundError: if the JSONL path doesn't exist.
        json.JSONDecodeError: if any line is not valid JSON.
    """
    jsonl_path = Path(jsonl_path)

    adapter = jsonl_path.parent.name
    run_id = jsonl_path.stem

    per_lang: dict[str, dict[str, Any]] = defaultdict(_init_lang_accum)
    overall = _init_lang_accum()

    with jsonl_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)

            lang = row.get("language") or "<unknown>"
            is_error = row.get("error") is not None
            tokens = row.get("tokens_generated")
            tokens = int(tokens) if isinstance(tokens, (int, float)) else 0

            # bench-02 VAL-101: pull comparator fields up-front so both
            # error and success paths can record them. ``None`` propagates
            # through so historical FastEdit rows (which don't carry
            # latency_ms) stay as None in the mean computation. Cost is
            # summed unconditionally — errored API calls still cost.
            in_tok = row.get("input_tokens")
            out_tok = row.get("output_tokens")
            latency = row.get("latency_ms")
            tool_calls = row.get("tool_call_count")
            retries = row.get("retry_count")
            cost = row.get("cost_usd")

            for bucket in (per_lang[lang], overall):
                bucket["n_cases"] += 1
                bucket["total_tokens_generated"] += tokens
                if isinstance(cost, (int, float)):
                    bucket["total_cost_usd"] += float(cost)
                # Track per-field means on ALL rows (not just success).
                # An errored API call still burned tokens + latency and
                # must show up in cost/latency aggregates; otherwise a
                # model with high error rates would look falsely cheap.
                bucket["_input_tokens"].append(
                    int(in_tok) if isinstance(in_tok, (int, float)) else None
                )
                bucket["_output_tokens"].append(
                    int(out_tok) if isinstance(out_tok, (int, float)) else None
                )
                bucket["_latency_ms"].append(
                    float(latency)
                    if isinstance(latency, (int, float))
                    else None
                )
                bucket["_tool_call_counts"].append(
                    int(tool_calls)
                    if isinstance(tool_calls, (int, float))
                    else None
                )
                bucket["_retry_counts"].append(
                    int(retries) if isinstance(retries, (int, float)) else None
                )
                if is_error:
                    bucket["n_errors"] += 1
                    continue
                if row.get("tier1_pass"):
                    bucket["_tier1_passes"] += 1
                if row.get("tier2_pass"):
                    bucket["_tier2_passes"] += 1
                bucket["_minimality_ratios"].append(
                    float(row.get("minimality_ratio", 0.0))
                )
                bucket["_hunk_counts"].append(
                    float(row.get("hunk_count", 0))
                )
                bucket["_context_preserved"].append(
                    float(row.get("context_lines_preserved", 0.0))
                )

    return {
        "adapter": adapter,
        "run_id": run_id,
        "total_cases": overall["n_cases"],
        "total_languages": len(per_lang),
        "per_language": {
            lang: _finalize(accum) for lang, accum in sorted(per_lang.items())
        },
        # Overall rates are micro-averages over total n_cases (errors
        # count against the headline number). Per-language rates stay
        # as pass_count / n_non_error so individual languages aren't
        # double-punished for provider/infra errors outside their scope.
        "overall": _finalize(overall, rate_over_total=True),
    }


def write_summary(jsonl_path: Path, summary_path: Path) -> None:
    """Aggregate ``jsonl_path`` and write the result as JSON to ``summary_path``.

    The parent directory is created if missing. The file is written
    atomically-enough for single-writer benchmarks: we open with
    ``"w"`` and rely on the OS to replace the contents in one call.
    """
    jsonl_path = Path(jsonl_path)
    summary_path = Path(summary_path)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    summary = aggregate(jsonl_path)
    with summary_path.open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
        fh.write("\n")
