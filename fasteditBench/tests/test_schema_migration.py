"""Tests for the bench-02 historical-run migration (VAL-101).

These tests verify that:

1. :func:`migrate_row` idempotently adds every bench-02 field with the
   documented defaults, including the legacy ``tokens_generated`` ->
   ``output_tokens`` inheritance.
2. The canonical bench-01 run ``results/fastedit/20260422-181127.jsonl``
   survives a round-trip through the migration with
   ``tier1_pass_rate == 1.0`` preserved.
3. The migrated summary exposes the new aggregate fields
   (``mean_input_tokens``, ``mean_output_tokens``, ``mean_latency_ms``,
   ``mean_tool_call_count``, ``mean_retry_count``, ``total_cost_usd``)
   with nullable-safe values.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from migrate_historical_runs import (  # noqa: E402
    _NEW_FIELD_DEFAULTS,
    migrate_file,
    migrate_row,
)

from fasteditbench.reporting import aggregate  # noqa: E402


def _legacy_row() -> dict:
    """A minimal bench-01 row as it exists on disk today."""
    return {
        "case_id": "python/01-add_guard",
        "language": "python",
        "pattern": "add_guard",
        "tier1_pass": True,
        "tier2_pass": True,
        "hunk_count": 1,
        "minimality_ratio": 1.0,
        "context_lines_preserved": 1.0,
        "tokens_generated": 42,
        "error": None,
        "target_kind": "replace",
        "target_name": "greet",
    }


def test_migrate_row_adds_all_new_fields():
    """Every key in ``_NEW_FIELD_DEFAULTS`` lands on the migrated row."""
    migrated = migrate_row(_legacy_row())
    for key in _NEW_FIELD_DEFAULTS:
        assert key in migrated, f"missing: {key}"


def test_migrate_row_types():
    """Migrated fields have the documented types (int / None-or-int / etc.)."""
    migrated = migrate_row(_legacy_row())

    # input_tokens + retry_count are integers with default 0.
    assert isinstance(migrated["input_tokens"], int)
    assert migrated["input_tokens"] == 0
    assert isinstance(migrated["retry_count"], int)
    assert migrated["retry_count"] == 0

    # output_tokens inherits from tokens_generated (42 in the fixture).
    assert migrated["output_tokens"] == 42

    # Nullable fields default to None for historical rows.
    assert migrated["latency_ms"] is None
    assert migrated["tool_call_count"] is None
    assert migrated["input_tokens_native"] is None
    assert migrated["output_tokens_native"] is None


def test_migrate_row_preserves_existing_fields():
    """Migration doesn't mutate existing field values."""
    row = _legacy_row()
    migrated = migrate_row(row)
    for key, val in row.items():
        assert migrated[key] == val


def test_migrate_row_idempotent():
    """Running migrate_row twice produces the same dict."""
    once = migrate_row(_legacy_row())
    twice = migrate_row(once)
    assert once == twice


def test_migrate_row_tokens_generated_missing_defaults_zero():
    """A row lacking ``tokens_generated`` yields output_tokens=0, not error."""
    row = _legacy_row()
    row.pop("tokens_generated")
    migrated = migrate_row(row)
    assert migrated["output_tokens"] == 0


def test_canonical_bench01_run_migrates_with_tier1_preserved(tmp_path):
    """The canonical FastEdit bench-01 run keeps tier1=1.0 after migration."""
    src = REPO_ROOT / "results" / "fastedit" / "20260422-181127.jsonl"
    if not src.exists():
        pytest.skip(f"canonical run not present at {src}")

    # Copy into tmp_path so we don't clobber the committed migrated file.
    staged = tmp_path / src.name
    staged.write_bytes(src.read_bytes())

    migrated = migrate_file(staged)
    assert migrated.exists()

    orig_summary = aggregate(staged)
    migrated_summary = aggregate(migrated)

    assert orig_summary["overall"]["tier1_pass_rate"] == 1.0
    assert migrated_summary["overall"]["tier1_pass_rate"] == 1.0
    assert migrated_summary["overall"]["tier2_pass_rate"] == 1.0
    assert migrated_summary["overall"]["n_cases"] == 156
    assert migrated_summary["overall"]["n_errors"] == 0


def test_migrated_summary_exposes_new_aggregates(tmp_path):
    """The migrated summary carries the bench-02 aggregate fields."""
    src = REPO_ROOT / "results" / "fastedit" / "20260422-181127.jsonl"
    if not src.exists():
        pytest.skip(f"canonical run not present at {src}")

    staged = tmp_path / src.name
    staged.write_bytes(src.read_bytes())
    migrated = migrate_file(staged)

    summary = aggregate(migrated)
    overall = summary["overall"]

    # mean_input_tokens is 0.0 (all migrated FastEdit rows default to 0).
    assert overall["mean_input_tokens"] == 0.0
    # mean_output_tokens equals mean tokens_generated (~ 12.67 for this run).
    assert overall["mean_output_tokens"] == pytest.approx(
        1977 / 156, rel=1e-6
    )
    # latency and tool_call_count are None — no data on historical runs.
    assert overall["mean_latency_ms"] is None
    assert overall["mean_tool_call_count"] is None
    # retry_count defaults to 0 everywhere, so mean is 0.0 (not None).
    assert overall["mean_retry_count"] == 0.0
    # No cost recorded on historical runs.
    assert overall["total_cost_usd"] == 0.0


def test_migrated_jsonl_lines_roundtrip_json(tmp_path):
    """Every line in the migrated JSONL is parseable JSON with the new keys."""
    src = REPO_ROOT / "results" / "fastedit" / "20260422-181127.jsonl"
    if not src.exists():
        pytest.skip(f"canonical run not present at {src}")

    staged = tmp_path / src.name
    staged.write_bytes(src.read_bytes())
    migrated = migrate_file(staged)

    with migrated.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            for key in _NEW_FIELD_DEFAULTS:
                assert key in row
