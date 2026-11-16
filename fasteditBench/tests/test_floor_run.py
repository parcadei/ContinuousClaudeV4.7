"""Tests for the floor-mode runner (VAL-300 + VAL-301).

Split:

* Tests 1–4 (VAL-301 invariants): every row has a tokenizer, every
  non-error row has positive payload tokens, per-model totals fall
  within the sanity envelope, proxy-flag consistency.
* Tests 5–6 (VAL-300 resume/force): rerun-without-force skips
  completed fixtures, ``--force`` rewrites from scratch.

Tests 1–4 use a 5-fixture subset (not the full 156) to stay fast while
still exercising both tokenizer backends on real fixture content. Tests
5–6 use a 3-fixture subset for the same reason. ``ANTHROPIC_API_KEY``
is cleared in every test so the Opus path is always the proxy
(cl100k_base) — hermetic, no network.

The ``run_floor()`` call reads fixtures via
:func:`fasteditbench.runner.load_fixtures`, which walks every language
under ``fixtures_dir``. To run against a subset we stage the chosen
fixtures under a fresh ``tmp_path/fixtures/<lang>/*.yaml`` tree.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from fasteditbench.floor_runner import (
    LANGUAGE_EXTENSIONS,
    model_slug,
    run_floor,
    synthesize_file_path,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_ROOT = REPO_ROOT / "fixtures"

_GPT = "openai/gpt-5.4"
_OPUS = "anthropic/claude-opus-4.7"

# Sanity envelope from contract.json VAL-301 (c).
_ENVELOPE_LOW = 10_000
_ENVELOPE_HIGH = 1_000_000


# ---------------------------------------------------------------------------
# Fixture staging helpers
# ---------------------------------------------------------------------------


def _stage_fixtures(tmp_path: Path, selections: list[tuple[str, str]]) -> Path:
    """Copy ``(lang, fixture_stem)`` pairs into a fresh fixtures tree.

    Returns the staged fixtures dir (``tmp_path/fixtures``).
    """
    staged = tmp_path / "fixtures"
    for lang, stem in selections:
        src = FIXTURES_ROOT / lang / f"{stem}.yaml"
        assert src.exists(), f"missing source fixture: {src}"
        dst_dir = staged / lang
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst_dir / src.name)
    return staged


_MINI_MATRIX: list[tuple[str, str]] = [
    ("python", "01-add_guard"),
    ("python", "02-wrap_block"),
    ("rust", "01-add_guard"),
    ("go", "01-add_guard"),
    ("typescript", "01-add-guard-null-user"),
]

_RESUME_MATRIX: list[tuple[str, str]] = [
    ("python", "01-add_guard"),
    ("python", "02-wrap_block"),
    ("python", "03-change_line"),
]


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------------------
# Synthesize-path helpers (pure)
# ---------------------------------------------------------------------------


def test_language_extensions_cover_all_languages():
    """Every benchmark language has a file-extension mapping."""
    expected = {
        "python", "rust", "go", "typescript", "java", "kotlin", "csharp",
        "c", "cpp", "ruby", "swift", "php", "elixir",
    }
    assert expected.issubset(set(LANGUAGE_EXTENSIONS))


def test_synthesize_file_path():
    """case_id stem + language extension -> full representative path."""
    assert synthesize_file_path(
        "python/01-add_guard", "python"
    ) == "python/01-add_guard.py"
    assert synthesize_file_path(
        "rust/01-add_guard", "rust"
    ) == "rust/01-add_guard.rs"
    # Unknown language -> .txt fallback.
    assert synthesize_file_path("martian/01-x", "martian") == "martian/01-x.txt"


def test_model_slug_is_filesystem_safe():
    assert model_slug(_GPT) == "openai_gpt-5.4"
    assert model_slug(_OPUS) == "anthropic_claude-opus-4.7"


# ---------------------------------------------------------------------------
# VAL-301 (invariants) on a mini matrix
# ---------------------------------------------------------------------------


@pytest.fixture
def mini_matrix_run(tmp_path, monkeypatch):
    """Run the 5-fixture × 2-model mini matrix once, return its outputs."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    staged = _stage_fixtures(tmp_path, _MINI_MATRIX)
    out_paths = run_floor(
        models=[_GPT, _OPUS],
        fixtures_dir=staged,
        results_dir=tmp_path / "results",
        run_id="test-mini",
    )
    return {mid: _read_jsonl(p) for mid, p in out_paths.items()}


def test_every_row_has_tokenizer_used(mini_matrix_run):
    """VAL-301 (a): every row carries a non-empty tokenizer_used."""
    for model_id, rows in mini_matrix_run.items():
        assert rows, f"{model_id}: empty output"
        for row in rows:
            assert row.get("tokenizer_used"), (
                f"{model_id}/{row.get('case_id')}: missing tokenizer_used"
            )


def test_every_row_has_positive_payload_tokens(mini_matrix_run):
    """VAL-301 (b): every non-error row has edit_payload_tokens > 0."""
    for model_id, rows in mini_matrix_run.items():
        for row in rows:
            if row.get("error") is not None:
                continue
            tokens = row.get("edit_payload_tokens")
            assert isinstance(tokens, int) and tokens > 0, (
                f"{model_id}/{row.get('case_id')}: non-positive tokens {tokens}"
            )


def test_sum_within_sanity_envelope(tmp_path, monkeypatch):
    """VAL-301 (c): full 156-fixture total per model is in [10k, 1M].

    Uses the canonical fixtures tree (not the mini matrix) to match the
    envelope from the contract verbatim.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out_paths = run_floor(
        models=[_GPT, _OPUS],
        fixtures_dir=FIXTURES_ROOT,
        results_dir=tmp_path / "results",
        run_id="test-envelope",
    )
    for model_id, path in out_paths.items():
        rows = _read_jsonl(path)
        total = sum(
            r.get("edit_payload_tokens") or 0
            for r in rows
            if r.get("error") is None
        )
        assert _ENVELOPE_LOW <= total <= _ENVELOPE_HIGH, (
            f"{model_id}: total {total} outside envelope "
            f"[{_ENVELOPE_LOW}, {_ENVELOPE_HIGH}]"
        )


def test_no_unexpected_proxy(mini_matrix_run):
    """VAL-301 (d): tokenizer_proxy flag must match ANTHROPIC_API_KEY state.

    With the key absent (monkeypatched off in the fixture setup), every
    Opus row must carry ``tokenizer_proxy=True``. GPT rows always have
    ``tokenizer_proxy=False``. If a future CI env ships a real key, the
    other branch asserts no Opus row used the proxy.
    """
    key_present = bool(os.environ.get("ANTHROPIC_API_KEY"))

    for row in mini_matrix_run[_GPT]:
        assert row.get("tokenizer_proxy") is False, (
            f"GPT row {row.get('case_id')} unexpectedly flagged proxy"
        )

    for row in mini_matrix_run[_OPUS]:
        if row.get("error") is not None:
            continue
        if key_present:
            assert row.get("tokenizer_proxy") is False, (
                f"Opus row {row.get('case_id')} used proxy "
                f"despite ANTHROPIC_API_KEY set"
            )
        else:
            assert row.get("tokenizer_proxy") is True, (
                f"Opus row {row.get('case_id')} missing proxy flag "
                f"despite ANTHROPIC_API_KEY unset"
            )


# ---------------------------------------------------------------------------
# VAL-300 (resume / force)
# ---------------------------------------------------------------------------


def test_resume_skips_completed(tmp_path, monkeypatch):
    """A rerun without --force preserves existing rows and only adds new ones."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # First, stage only the first fixture and run.
    staged_partial = _stage_fixtures(tmp_path, _RESUME_MATRIX[:1])
    results_dir = tmp_path / "results"
    run_floor(
        models=[_GPT],
        fixtures_dir=staged_partial,
        results_dir=results_dir,
        run_id="test-resume",
    )

    jsonl_path = results_dir / model_slug(_GPT) / "test-resume.jsonl"
    assert jsonl_path.exists()

    first_rows = _read_jsonl(jsonl_path)
    assert len(first_rows) == 1
    first_cid = first_rows[0]["case_id"]

    # Replace staged tree with the full 3-fixture set and rerun (no --force).
    # Use a SECOND staging dir so the first one stays a clean "partial" input.
    staged_full = _stage_fixtures(tmp_path / "full", _RESUME_MATRIX)
    # Mirror the pre-existing JSONL into the new results layout — since
    # results_dir is the same, no migration needed; resume just reads it.
    run_floor(
        models=[_GPT],
        fixtures_dir=staged_full,
        results_dir=results_dir,
        run_id="test-resume",
    )

    second_rows = _read_jsonl(jsonl_path)
    cids = [r["case_id"] for r in second_rows]

    # No duplicates.
    assert len(cids) == len(set(cids)), f"duplicates in {cids}"
    # All 3 fixtures now present.
    assert len(second_rows) == 3
    # The original row survived verbatim.
    preserved = next(r for r in second_rows if r["case_id"] == first_cid)
    first = first_rows[0]
    for key in ("edit_payload_tokens", "tokenizer_used", "hunk_count"):
        assert preserved.get(key) == first.get(key), (
            f"row {first_cid} drifted on resume: {key}"
        )


def test_force_overwrites(tmp_path, monkeypatch):
    """``--force`` rewrites the JSONL from scratch — old rows don't leak in."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # First run produces 1 fixture.
    staged_partial = _stage_fixtures(tmp_path, _RESUME_MATRIX[:1])
    results_dir = tmp_path / "results"
    run_floor(
        models=[_GPT],
        fixtures_dir=staged_partial,
        results_dir=results_dir,
        run_id="test-force",
    )

    jsonl_path = results_dir / model_slug(_GPT) / "test-force.jsonl"
    first_rows = _read_jsonl(jsonl_path)
    assert len(first_rows) == 1

    # Rerun with --force against a DIFFERENT fixture. If force is wrong
    # and the old row leaks through, we'd see 2 rows with different ids.
    staged_alt = _stage_fixtures(tmp_path / "alt", _RESUME_MATRIX[1:2])
    run_floor(
        models=[_GPT],
        fixtures_dir=staged_alt,
        results_dir=results_dir,
        run_id="test-force",
        force=True,
    )

    second_rows = _read_jsonl(jsonl_path)
    assert len(second_rows) == 1, (
        f"expected exactly 1 row after --force, got {len(second_rows)}"
    )
    assert second_rows[0]["case_id"] != first_rows[0]["case_id"], (
        "force did not overwrite — old row still present"
    )
