"""Tests for the calling-agent snippet floor computation (bench-02 v2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.compute_snippet_floor import (
    build_snippet_payload,
    compute_rows,
    run_snippet_floor,
    summarize,
)


_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIXTURES_DIR = _REPO_ROOT / "fixtures"


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    """Compute the 156 rows once and share across tests."""
    return compute_rows(_FIXTURES_DIR)


def test_snippet_tokens_positive(rows: list[dict]) -> None:
    """Every fixture must produce a strictly positive snippet token count.

    A zero-token snippet would mean the fixture has no ``snippet`` field
    (fixture bug) or the tokenizer produced an empty encoding (tokenizer
    bug). Either case should fail loud, not silently skew the average.
    """
    assert len(rows) == 156, f"expected 156 fixtures, saw {len(rows)}"
    for r in rows:
        assert r["snippet_tokens"] > 0, (
            f"{r['case_id']}: snippet_tokens must be > 0, got "
            f"{r['snippet_tokens']}"
        )


def test_savings_nonzero_aggregate(rows: list[dict]) -> None:
    """Aggregate savings must be clearly positive.

    Sanity floor: if FastEdit's snippet interface doesn't save the
    calling agent tokens on balance, the report's central claim is
    falsified and we want to know before publishing it.
    """
    total_native = sum(r["native_edit_tokens"] for r in rows)
    total_snippet = sum(r["snippet_tokens"] for r in rows)
    total_savings = total_native - total_snippet
    assert total_savings > 0, (
        f"aggregate savings should be positive; got native={total_native} "
        f"snippet={total_snippet} savings={total_savings}"
    )
    # Also guard against a suspiciously-small or suspiciously-huge ratio;
    # the task spec flags <1.3x or >4x as a likely bug in the logic.
    ratio = total_native / total_snippet
    assert 1.3 <= ratio <= 4.0, (
        f"native/snippet ratio {ratio:.3f} outside expected [1.3, 4.0] band"
    )


def test_per_language_summary_shape(rows: list[dict]) -> None:
    """The summary dict must carry the keys the report will read."""
    summary = summarize(rows)
    assert summary["n_cases"] == 156
    assert summary["tokenizer_used"] == "tiktoken:o200k_base"
    # Top-level required keys
    for key in (
        "totals",
        "per_language",
        "per_pattern",
        "top5_largest_savings",
        "top5_smallest_savings",
    ):
        assert key in summary, f"summary missing key: {key}"
    # Totals keys
    for key in (
        "snippet_tokens",
        "native_edit_tokens",
        "savings_tokens",
        "savings_pct",
        "ratio_native_over_snippet",
    ):
        assert key in summary["totals"], f"totals missing key: {key}"
    # Per-language bucket keys
    assert "python" in summary["per_language"], (
        "python bucket should be present"
    )
    py = summary["per_language"]["python"]
    for key in (
        "n_cases",
        "total_snippet_tokens",
        "total_native_edit_tokens",
        "total_savings_tokens",
        "savings_pct",
        "ratio_native_over_snippet",
    ):
        assert key in py, f"per_language[python] missing key: {key}"
    assert len(summary["top5_largest_savings"]) == 5
    assert len(summary["top5_smallest_savings"]) == 5


def test_snippet_payload_includes_replace_marker(tmp_path: Path) -> None:
    """When target_kind=='replace' the payload carries the replace key."""
    payload = build_snippet_payload(
        file_path="python/x.py",
        snippet="def foo(): pass",
        target_kind="replace",
        target_name="foo",
    )
    obj = json.loads(payload)
    assert obj["name"] == "fast_edit"
    assert obj["arguments"]["replace"] == "foo"
    assert "after" not in obj["arguments"]


def test_run_snippet_floor_writes_both_files(tmp_path: Path) -> None:
    """End-to-end: the runner produces JSONL + summary in the expected layout."""
    jsonl_path, summary_path, summary = run_snippet_floor(
        _FIXTURES_DIR, tmp_path, run_id="test-run"
    )
    assert jsonl_path.exists()
    assert summary_path.exists()
    assert jsonl_path.name == "test-run.jsonl"
    assert summary_path.name == "test-run.summary.json"
    # JSONL is one row per line and parses cleanly.
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 156
    for line in lines:
        row = json.loads(line)
        assert "case_id" in row
        assert "snippet_tokens" in row
    # Summary on disk matches the in-memory object.
    on_disk = json.loads(summary_path.read_text(encoding="utf-8"))
    assert on_disk["n_cases"] == summary["n_cases"]
