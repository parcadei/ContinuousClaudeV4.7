"""Tests for the CLI runner, JSONL writer, and adapter registry (VAL-203)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from fasteditbench.adapter import AdapterResult, NullAdapter
from fasteditbench.runner import (
    CaseRow,
    get_adapter,
    load_fixtures,
    run_all,
    run_case,
)

# Repo root — the package is installed from here via `uv sync`, so tests
# can invoke the CLI as a subprocess.
REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "fixtures"

# Expected JSONL field set — frozen by contract.json VAL-203. If this set
# ever drifts, the downstream report generator will break silently.
EXPECTED_FIELDS: frozenset[str] = frozenset({
    "case_id",
    "language",
    "pattern",
    "tier1_pass",
    "tier2_pass",
    "hunk_count",
    "minimality_ratio",
    "context_lines_preserved",
    "tokens_generated",
    "error",
    # VAL-305: MCP-mode target metadata appended to the schema.
    "target_kind",
    "target_name",
    # bench-02 VAL-101: frontier-LLM comparator fields appended. Defaults
    # preserve prefix-compatibility with historical FastEdit runs.
    "input_tokens",
    "output_tokens",
    "latency_ms",
    "tool_call_count",
    "retry_count",
    "input_tokens_native",
    "output_tokens_native",
    # bench-02 VAL-300 (M3): token-floor comparator fields. Populated by
    # the floor_runner only; all other adapters leave them as None.
    "edit_payload_tokens",
    "tokenizer_used",
    "tokenizer_proxy",
})

# Total fixture count, derived by filesystem inventory:
# c=12, cpp=12, csharp=12, elixir=12, go=12, java=12, kotlin=12, php=13,
# python=12, ruby=13, rust=10, swift=12, typescript=12 → 156.
TOTAL_FIXTURES = 156


# ── CaseRow schema ────────────────────────────────────────────────────


def test_case_row_schema() -> None:
    """CaseRow.asdict() keys match EXACTLY the 10 contract field names."""
    row = CaseRow(
        case_id="python/01-add_guard",
        language="python",
        pattern="add_guard",
        tier1_pass=True,
        tier2_pass=True,
        hunk_count=1,
        minimality_ratio=1.0,
        context_lines_preserved=1.0,
        tokens_generated=0,
        error=None,
        target_kind="replace",
        target_name="retry_batch",
    )
    keys = set(dataclasses.asdict(row).keys())
    assert keys == EXPECTED_FIELDS


# ── run_case ──────────────────────────────────────────────────────────


def test_run_case_error_path() -> None:
    """An adapter that raises inside apply produces a sentinel row, no uncaught exc."""

    class BrokenAdapter:
        name = "broken"

        async def apply(
            self,
            original_code: str,
            snippet: str,
            language: str,
            context: dict[str, Any],
        ) -> AdapterResult:
            raise RuntimeError("kaboom")

    fixture = {
        "name": "boom",
        "pattern": "add_guard",
        "language": "python",
        "original_code": "def f(): pass\n",
        "snippet": "# snippet\n",
        "expected_output": "def f(): return 1\n",
    }

    row = asyncio.run(run_case(BrokenAdapter(), "python/boom", fixture))

    assert row.error is not None
    assert "kaboom" in row.error
    assert row.tier1_pass is False
    assert row.tier2_pass is False
    assert row.hunk_count == 0
    assert row.minimality_ratio == 0.0
    assert row.context_lines_preserved == 0.0
    assert row.tokens_generated is None
    assert row.language == "python"
    assert row.pattern == "add_guard"


def test_run_case_adapter_result_error_surfaces() -> None:
    """An adapter returning AdapterResult(error=...) surfaces the error on the row."""

    class ErroringAdapter:
        name = "erroring"

        async def apply(
            self,
            original_code: str,
            snippet: str,
            language: str,
            context: dict[str, Any],
        ) -> AdapterResult:
            return AdapterResult(output_text="", error="provider timeout", tokens_generated=7)

    fixture = {
        "name": "t",
        "pattern": "add_line",
        "language": "python",
        "original_code": "x = 1\n",
        "snippet": "y = 2\n",
        "expected_output": "x = 1\ny = 2\n",
    }

    row = asyncio.run(run_case(ErroringAdapter(), "python/t", fixture))
    assert row.error == "provider timeout"
    assert row.tier1_pass is False
    assert row.tokens_generated == 7


# ── load_fixtures ─────────────────────────────────────────────────────


def test_load_fixtures_all_languages() -> None:
    """load_fixtures(None) yields all 156 fixtures across 13 languages."""
    cases = list(load_fixtures(FIXTURES_DIR, None))
    assert len(cases) == TOTAL_FIXTURES
    # case_id format: "{lang}/{stem}"
    for case_id, fixture in cases:
        assert "/" in case_id
        lang, stem = case_id.split("/", 1)
        assert fixture["language"] == lang
        assert stem  # non-empty


def test_load_fixtures_single_language() -> None:
    """Filter by one language."""
    cases = list(load_fixtures(FIXTURES_DIR, ["python"]))
    # Python dir has 12 fixtures per the inventory.
    assert len(cases) == 12
    for case_id, fixture in cases:
        assert case_id.startswith("python/")
        assert fixture["language"] == "python"


# ── run_all ───────────────────────────────────────────────────────────


def test_run_all_null_adapter_smoke(tmp_path: Path) -> None:
    """SMOKE: NullAdapter over all 13 langs produces valid JSONL with 156 rows.

    NOTE: This proves the pipeline works end-to-end. It does NOT expect
    NullAdapter to pass tier-1 (it returns original_code, never expected).
    """
    adapter = NullAdapter()
    out_path = asyncio.run(
        run_all(
            adapter=adapter,
            adapter_name="null",
            fixtures_dir=FIXTURES_DIR,
            langs=None,
            results_dir=tmp_path,
        )
    )

    assert out_path.exists()
    assert out_path.parent.name == "null"
    assert out_path.suffix == ".jsonl"

    lines = out_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == TOTAL_FIXTURES

    for line in lines:
        obj = json.loads(line)
        assert set(obj.keys()) == EXPECTED_FIELDS
        # NullAdapter returns original != expected, so tier-1 is False.
        assert obj["tier1_pass"] is False
        # tokens_generated is 0 for NullAdapter, not None.
        assert obj["tokens_generated"] == 0
        # No error on the happy path.
        assert obj["error"] is None


def test_run_all_single_language(tmp_path: Path) -> None:
    """Filter by one language — only Python rows are written."""
    out_path = asyncio.run(
        run_all(
            adapter=NullAdapter(),
            adapter_name="null",
            fixtures_dir=FIXTURES_DIR,
            langs=["python"],
            results_dir=tmp_path,
        )
    )

    lines = out_path.read_text(encoding="utf-8").splitlines()
    # Python fixture dir has 12 YAML files.
    assert len(lines) == 12
    for line in lines:
        obj = json.loads(line)
        assert obj["language"] == "python"
        assert obj["case_id"].startswith("python/")


def test_run_all_run_id_format(tmp_path: Path) -> None:
    """Output filename matches the YYYYMMDD-HHMMSS run_id format."""
    out_path = asyncio.run(
        run_all(
            adapter=NullAdapter(),
            adapter_name="null",
            fixtures_dir=FIXTURES_DIR,
            langs=["python"],
            results_dir=tmp_path,
        )
    )
    stem = out_path.stem
    # YYYYMMDD-HHMMSS — 15 chars, digit-dash-digit layout.
    assert len(stem) == 15
    assert stem[8] == "-"
    assert stem[:8].isdigit()
    assert stem[9:].isdigit()


# ── get_adapter registry ──────────────────────────────────────────────


def test_get_adapter_null() -> None:
    """Registry returns a NullAdapter instance for name='null'."""
    adapter = get_adapter("null")
    assert isinstance(adapter, NullAdapter)


def test_get_adapter_unknown_raises() -> None:
    """Unknown adapter name raises ValueError with a clear message."""
    with pytest.raises(ValueError, match="unknown adapter"):
        get_adapter("nonexistent")


# ── CLI via `python -m fasteditbench` ─────────────────────────────────


def test_cli_run_null_all(tmp_path: Path) -> None:
    """`python -m fasteditbench run --adapter null --lang all ...` exits 0 with 156 rows."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "fasteditbench",
            "run",
            "--adapter",
            "null",
            "--lang",
            "all",
            "--fixtures-dir",
            str(FIXTURES_DIR),
            "--results-dir",
            str(tmp_path),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )

    # Stdout should be the JSONL path.
    out_path = Path(result.stdout.strip())
    assert out_path.exists()
    assert out_path.parent.name == "null"

    lines = out_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == TOTAL_FIXTURES


def test_cli_run_unknown_adapter(tmp_path: Path) -> None:
    """Unknown adapter exits non-zero with 'unknown adapter' on stderr."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "fasteditbench",
            "run",
            "--adapter",
            "nonexistent",
            "--lang",
            "python",
            "--fixtures-dir",
            str(FIXTURES_DIR),
            "--results-dir",
            str(tmp_path),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "unknown adapter" in result.stderr
