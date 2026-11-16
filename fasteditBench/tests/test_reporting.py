"""Tests for the JSONL aggregator (VAL-301).

The summary writer is pure: it consumes a JSONL file and emits a
deterministic JSON summary. These tests build synthetic JSONLs in a
``tmp_path`` and assert hand-computed aggregates.
"""

from __future__ import annotations

import json
from pathlib import Path

from fasteditbench.reporting import aggregate, write_summary


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    """Write ``rows`` as newline-delimited JSON to ``path``.

    Helper used by every test — keeps each test focused on input
    construction and assertions rather than file IO.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _row(
    *,
    language: str,
    tier1: bool,
    tier2: bool,
    minimality: float = 1.0,
    hunks: int = 1,
    context: float = 1.0,
    tokens: int = 0,
    error: str | None = None,
) -> dict:
    """Build a JSONL row matching the runner's CaseRow schema."""
    return {
        "case_id": f"{language}/synth",
        "language": language,
        "pattern": "add_line",
        "tier1_pass": tier1,
        "tier2_pass": tier2,
        "hunk_count": hunks,
        "minimality_ratio": minimality,
        "context_lines_preserved": context,
        "tokens_generated": tokens,
        "error": error,
    }


# ── aggregate: happy path ─────────────────────────────────────────────


def test_aggregate_happy_path(tmp_path: Path) -> None:
    """3 languages x 4 cases: rates and means match hand-computed values.

    Layout (all error=None):
      python: tier1 [T,T,T,F], tier2 [T,T,T,T], minimality [1.0,1.0,1.0,2.0]
      rust:   tier1 [T,F,F,F], tier2 [T,T,F,F], minimality [1.0,1.5,0.5,0.5]
      go:     tier1 [T,T,T,T], tier2 [T,T,T,T], minimality [1.0,1.0,1.0,1.0]
    """
    jsonl = tmp_path / "fastedit" / "20260422-000000.jsonl"
    rows: list[dict] = []
    for t1, t2, m in zip(
        [True, True, True, False],
        [True, True, True, True],
        [1.0, 1.0, 1.0, 2.0],
        strict=True,
    ):
        rows.append(_row(
            language="python", tier1=t1, tier2=t2, minimality=m, tokens=10
        ))
    for t1, t2, m in zip(
        [True, False, False, False],
        [True, True, False, False],
        [1.0, 1.5, 0.5, 0.5],
        strict=True,
    ):
        rows.append(_row(
            language="rust", tier1=t1, tier2=t2, minimality=m, tokens=5
        ))
    for _ in range(4):
        rows.append(_row(
            language="go", tier1=True, tier2=True, minimality=1.0, tokens=0
        ))
    _write_jsonl(jsonl, rows)

    summary = aggregate(jsonl)

    assert summary["adapter"] == "fastedit"
    assert summary["run_id"] == "20260422-000000"
    assert summary["total_cases"] == 12
    assert summary["total_languages"] == 3

    py = summary["per_language"]["python"]
    assert py["n_cases"] == 4
    assert py["n_errors"] == 0
    assert py["tier1_pass_rate"] == 0.75  # 3/4
    assert py["tier2_pass_rate"] == 1.0
    assert abs(py["mean_minimality_ratio"] - 1.25) < 1e-9
    assert py["total_tokens_generated"] == 40

    ru = summary["per_language"]["rust"]
    assert ru["tier1_pass_rate"] == 0.25
    assert ru["tier2_pass_rate"] == 0.5
    assert abs(ru["mean_minimality_ratio"] - 0.875) < 1e-9
    assert ru["total_tokens_generated"] == 20

    go = summary["per_language"]["go"]
    assert go["tier1_pass_rate"] == 1.0
    assert go["tier2_pass_rate"] == 1.0
    assert go["mean_minimality_ratio"] == 1.0

    # Overall: total_tokens = 40 + 20 + 0 = 60; n_errors = 0.
    overall = summary["overall"]
    assert overall["n_cases"] == 12
    assert overall["n_errors"] == 0
    assert overall["total_tokens_generated"] == 60
    # tier1: 3 + 1 + 4 = 8 passes out of 12 -> 8/12 = 0.6666...
    # (micro-average over total n_cases, same result here since n_errors=0)
    assert abs(overall["tier1_pass_rate"] - 8 / 12) < 1e-9


def test_aggregate_overall_is_micro_average(tmp_path: Path) -> None:
    """Overall tier rates are true micro-averages: total_passes / total_n_cases.

    Construct an asymmetric case where macro- and micro-averages diverge:
      lang-A: 9/10 pass (rate 0.9)
      lang-B: 4/20 pass (rate 0.2)
    Macro = (0.9 + 0.2) / 2 = 0.55
    Micro = (9 + 4) / (10 + 20) = 13/30 ≈ 0.4333...

    The overall rate MUST equal the micro value. Per-language rates
    stay as-is (they're already pass_count / n_cases within a lang).
    """
    jsonl = tmp_path / "fastedit" / "run.jsonl"
    rows: list[dict] = []
    # lang-A: 9 passes, 1 fail (10 cases)
    for _ in range(9):
        rows.append(_row(language="lang-a", tier1=True, tier2=True))
    rows.append(_row(language="lang-a", tier1=False, tier2=False))
    # lang-B: 4 passes, 16 fails (20 cases)
    for _ in range(4):
        rows.append(_row(language="lang-b", tier1=True, tier2=True))
    for _ in range(16):
        rows.append(_row(language="lang-b", tier1=False, tier2=False))
    _write_jsonl(jsonl, rows)

    summary = aggregate(jsonl)

    # Per-language rates stay micro-within-language.
    a = summary["per_language"]["lang-a"]
    assert a["n_cases"] == 10
    assert abs(a["tier1_pass_rate"] - 0.9) < 1e-9
    b = summary["per_language"]["lang-b"]
    assert b["n_cases"] == 20
    assert abs(b["tier1_pass_rate"] - 0.2) < 1e-9

    overall = summary["overall"]
    assert overall["n_cases"] == 30
    # Micro: 13/30, NOT macro (0.9 + 0.2) / 2 = 0.55.
    assert abs(overall["tier1_pass_rate"] - 13 / 30) < 1e-9
    assert abs(overall["tier2_pass_rate"] - 13 / 30) < 1e-9
    # Sanity: not the macro value.
    assert abs(overall["tier1_pass_rate"] - 0.55) > 0.1


def test_aggregate_overall_rate_counts_errors_in_denominator(tmp_path: Path) -> None:
    """Overall denominator = total n_cases (including errors).

    Per-language rates still exclude errors from denominator (existing
    convention). The overall rate uses the full n_cases so that errors
    are visible as failures against the headline number — the true
    "how often does FastEdit produce a correct edit" metric.
    """
    jsonl = tmp_path / "fastedit" / "run.jsonl"
    rows = [
        _row(language="python", tier1=True, tier2=True),
        _row(language="python", tier1=True, tier2=True),
        _row(
            language="python",
            tier1=False,
            tier2=False,
            error="provider timeout",
        ),
        _row(language="rust", tier1=True, tier2=True),
        _row(
            language="rust",
            tier1=False,
            tier2=False,
            error="parse failure",
        ),
    ]
    _write_jsonl(jsonl, rows)

    summary = aggregate(jsonl)

    # Per-language: errors excluded from denominator (existing behavior).
    py = summary["per_language"]["python"]
    assert py["n_cases"] == 3
    assert py["n_errors"] == 1
    assert py["tier1_pass_rate"] == 1.0  # 2 / 2

    ru = summary["per_language"]["rust"]
    assert ru["n_cases"] == 2
    assert ru["n_errors"] == 1
    assert ru["tier1_pass_rate"] == 1.0  # 1 / 1

    # Overall: 3 passes / 5 total cases (errors DO count in denominator).
    overall = summary["overall"]
    assert overall["n_cases"] == 5
    assert overall["n_errors"] == 2
    assert abs(overall["tier1_pass_rate"] - 3 / 5) < 1e-9


# ── aggregate: error rows excluded from averages ─────────────────────


def test_aggregate_handles_errors(tmp_path: Path) -> None:
    """Rows with error != None count in n_errors and are excluded from rates.

    Convention (documented in reporting.py): error rows don't vote in
    tier rates or metric means; they're counted only in ``n_errors``.
    """
    jsonl = tmp_path / "fastedit" / "run.jsonl"
    rows = [
        _row(language="python", tier1=True, tier2=True, minimality=1.0),
        _row(language="python", tier1=True, tier2=True, minimality=1.0),
        # Error rows: would drag tier1 rate to 0.5 if they counted.
        _row(
            language="python",
            tier1=False,
            tier2=False,
            minimality=0.0,
            hunks=0,
            context=0.0,
            error="provider timeout",
        ),
        _row(
            language="python",
            tier1=False,
            tier2=False,
            minimality=0.0,
            hunks=0,
            context=0.0,
            error="parse_errors_in_merged_output",
        ),
    ]
    _write_jsonl(jsonl, rows)

    summary = aggregate(jsonl)

    py = summary["per_language"]["python"]
    assert py["n_cases"] == 4
    assert py["n_errors"] == 2
    # Rates computed over non-error rows only: 2/2 = 1.0.
    assert py["tier1_pass_rate"] == 1.0
    assert py["tier2_pass_rate"] == 1.0
    # Means exclude error rows: 1.0 avg of two 1.0's.
    assert py["mean_minimality_ratio"] == 1.0


# ── aggregate: empty language (no rows in the JSONL) ─────────────────


def test_aggregate_empty_language(tmp_path: Path) -> None:
    """A language with zero rows is simply absent from per_language.

    Convention: omit (don't emit ``n_cases=0`` stubs). Rationale:
    per_language is the inventory of languages actually observed in
    the JSONL; a missing language signals "no data", which is more
    actionable than a zero-row noise entry.
    """
    jsonl = tmp_path / "fastedit" / "run.jsonl"
    rows = [_row(language="python", tier1=True, tier2=True)]
    _write_jsonl(jsonl, rows)

    summary = aggregate(jsonl)
    assert list(summary["per_language"].keys()) == ["python"]
    assert "rust" not in summary["per_language"]


# ── aggregate: all-error language collapses safely ───────────────────


def test_aggregate_all_errors_language(tmp_path: Path) -> None:
    """When every row in a language errors, rates/means collapse to 0.0 (no NaN).

    This is the path we'll hit for elixir in the real run.
    """
    jsonl = tmp_path / "fastedit" / "run.jsonl"
    rows = [
        _row(
            language="elixir",
            tier1=False,
            tier2=False,
            minimality=0.0,
            hunks=0,
            context=0.0,
            error="elixir not supported by FastEdit (tool unavailable)",
        )
        for _ in range(12)
    ]
    _write_jsonl(jsonl, rows)

    summary = aggregate(jsonl)
    ex = summary["per_language"]["elixir"]
    assert ex["n_cases"] == 12
    assert ex["n_errors"] == 12
    assert ex["tier1_pass_rate"] == 0.0
    assert ex["tier2_pass_rate"] == 0.0
    assert ex["mean_minimality_ratio"] == 0.0


# ── write_summary: IO sanity ──────────────────────────────────────────


def test_write_summary_creates_file(tmp_path: Path) -> None:
    """write_summary writes valid JSON readable round-trip, parent dir created."""
    jsonl = tmp_path / "fastedit" / "run.jsonl"
    _write_jsonl(jsonl, [_row(language="python", tier1=True, tier2=True)])

    summary_path = tmp_path / "fastedit" / "run.summary.json"
    write_summary(jsonl, summary_path)

    assert summary_path.exists()
    parsed = json.loads(summary_path.read_text(encoding="utf-8"))
    assert parsed["total_cases"] == 1
    assert parsed["adapter"] == "fastedit"
    assert parsed["per_language"]["python"]["n_cases"] == 1
