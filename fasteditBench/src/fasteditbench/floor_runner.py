"""Floor-mode runner: 156 fixtures × N models → JSONL + summary (VAL-300).

A parallel runner to :mod:`fasteditbench.runner` that does NOT go through
the :class:`EditAdapter` pipeline — it calls
:meth:`fasteditbench.backends.tokenizer_floor_adapter.TokenizerFloorAdapter.count_floor`
directly. The output JSONL uses the same :class:`CaseRow` shape used by
FastEdit / OpenRouter runs so the downstream comparator report (M4) can
merge them side-by-side.

Floor-mode semantics:

* ``tier1_pass = tier2_pass = True`` — the floor assumes a correctly-
  produced edit (the most charitable interpretation of LLM cost).
* ``minimality_ratio = 1.0`` — one tool-call per hunk, no waste.
* ``hunk_count`` — ``FloorResult.n_hunks``.
* ``output_tokens = tokens_generated = edit_payload_tokens`` — the
  headline floor number.
* ``input_tokens = 0`` — not measured in floor-mode.
* ``latency_ms = None`` — N/A (offline computation).
* ``tool_call_count = 1`` — one ``edit`` tool call per fixture.
* ``retry_count = 0`` — no retries in floor-mode.

On ANY exception extracting/counting a fixture (identical original/expected,
malformed YAML, tokenizer error), a CaseRow is logged with ``tier1=False``,
``tier2=False``, and ``error="<exception class>: <message>"``. The matrix
continues — one bad fixture does not abort the run.

Idempotency + resume: if ``{run_id}.jsonl`` already exists, rows with a
matching ``case_id`` are preserved and only missing fixtures are
appended. ``force=True`` overwrites the entire file.

Performance: GPT path is fully offline (tiktoken o200k_base) — ~ms per
fixture. Opus path with ``tokenizer_proxy=True`` (cl100k_base) is also
offline. When ``ANTHROPIC_API_KEY`` is present the Opus path makes a
network call per fixture (~200ms each); in that case we parallelize
with ``concurrent.futures.ThreadPoolExecutor(max_workers=8)``. The full
312-computation matrix completes in under a minute on the offline
paths.
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from fasteditbench.backends.tokenizer_floor_adapter import (
    TokenizerFloorAdapter,
)
from fasteditbench.reporting import write_summary
from fasteditbench.runner import CaseRow, load_fixtures

__all__ = [
    "LANGUAGE_EXTENSIONS",
    "model_slug",
    "run_floor",
    "synthesize_file_path",
]

logger = logging.getLogger(__name__)


# Conventional file-extension mapping per language. The file_path is
# embedded verbatim in the serialized tool-call payload — it doesn't
# need to resolve on disk, just be representative of what a model would
# emit for that language.
LANGUAGE_EXTENSIONS: dict[str, str] = {
    "python": "py",
    "rust": "rs",
    "go": "go",
    "typescript": "ts",
    "java": "java",
    "kotlin": "kt",
    "csharp": "cs",
    "c": "c",
    "cpp": "cpp",
    "ruby": "rb",
    "swift": "swift",
    "php": "php",
    "elixir": "ex",
}


def model_slug(model_id: str) -> str:
    """Turn ``"openai/gpt-5.4"`` into ``"openai_gpt-5.4"`` (filesystem-safe)."""
    return model_id.replace("/", "_")


def synthesize_file_path(case_id: str, language: str) -> str:
    """Build a representative file_path for the serialized tool-call.

    ``case_id`` already has the form ``"{language}/{stem}"`` (see
    :func:`fasteditbench.runner.load_fixtures`); we just append the
    conventional extension for the language. Unknown languages fall
    back to ``txt``.
    """
    ext = LANGUAGE_EXTENSIONS.get(language, "txt")
    return f"{case_id}.{ext}"


def _make_run_id() -> str:
    """Return a UTC timestamp-based run id: ``YYYYMMDD-HHMMSS``."""
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def _load_existing_case_ids(out_path: Path) -> tuple[list[dict], set[str]]:
    """Load already-written rows from ``out_path``, if present.

    Returns ``(existing_rows_as_dicts, set_of_case_ids)``. When the
    file doesn't exist, both are empty. Malformed lines are skipped
    with a warning rather than aborting — a partial-write from a
    crashed prior run should not block a rerun.
    """
    if not out_path.exists():
        return [], set()

    rows: list[dict] = []
    case_ids: set[str] = set()
    with out_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(
                    "skipping malformed line in %s during resume", out_path
                )
                continue
            rows.append(row)
            cid = row.get("case_id")
            if isinstance(cid, str):
                case_ids.add(cid)
    return rows, case_ids


def _success_row(
    case_id: str,
    fixture: dict,
    floor_tokens: int,
    tokenizer_used: str,
    tokenizer_proxy: bool,
    n_hunks: int,
) -> CaseRow:
    """Build the floor-mode success row for one (fixture, model) pair."""
    return CaseRow(
        case_id=case_id,
        language=str(fixture.get("language", "")),
        pattern=str(fixture.get("pattern", "")),
        tier1_pass=True,
        tier2_pass=True,
        hunk_count=int(n_hunks),
        minimality_ratio=1.0,
        context_lines_preserved=1.0,
        tokens_generated=int(floor_tokens),
        error=None,
        target_kind=str(fixture.get("target_kind", "none")),
        target_name=fixture.get("target_name"),
        input_tokens=0,
        output_tokens=int(floor_tokens),
        latency_ms=None,
        tool_call_count=1,
        retry_count=0,
        input_tokens_native=None,
        output_tokens_native=None,
        edit_payload_tokens=int(floor_tokens),
        tokenizer_used=tokenizer_used,
        tokenizer_proxy=tokenizer_proxy,
    )


def _floor_error_row(case_id: str, fixture: dict, error: str) -> CaseRow:
    """Build a floor-mode error row with safe sentinels."""
    return CaseRow(
        case_id=case_id,
        language=str(fixture.get("language", "")),
        pattern=str(fixture.get("pattern", "")),
        tier1_pass=False,
        tier2_pass=False,
        hunk_count=0,
        minimality_ratio=0.0,
        context_lines_preserved=0.0,
        tokens_generated=None,
        error=error,
        target_kind=str(fixture.get("target_kind", "none")),
        target_name=fixture.get("target_name"),
        input_tokens=0,
        output_tokens=0,
        latency_ms=None,
        tool_call_count=None,
        retry_count=0,
        input_tokens_native=None,
        output_tokens_native=None,
        edit_payload_tokens=None,
        tokenizer_used=None,
        tokenizer_proxy=None,
    )


def _compute_one(
    adapter: TokenizerFloorAdapter,
    model_id: str,
    case_id: str,
    fixture: dict,
) -> CaseRow:
    """Compute the floor for one (fixture, model). Never raises."""
    try:
        original = fixture["original_code"]
        expected = fixture["expected_output"]
        language = str(fixture.get("language", ""))
        file_path = synthesize_file_path(case_id, language)
        result = adapter.count_floor(
            original_code=original,
            expected_output=expected,
            file_path=file_path,
            model_id=model_id,
        )
    except Exception as exc:  # noqa: BLE001 - capture all failure modes
        return _floor_error_row(
            case_id, fixture, f"{type(exc).__name__}: {exc}"
        )

    return _success_row(
        case_id=case_id,
        fixture=fixture,
        floor_tokens=result.edit_payload_tokens,
        tokenizer_used=result.tokenizer_used,
        tokenizer_proxy=result.tokenizer_proxy,
        n_hunks=result.n_hunks,
    )


def _run_one_model(
    model_id: str,
    adapter: TokenizerFloorAdapter,
    fixtures: list[tuple[str, dict]],
    out_path: Path,
    *,
    force: bool,
    max_workers: int,
) -> Path:
    """Execute the floor matrix for a single model, writing JSONL + summary."""
    if force and out_path.exists():
        out_path.unlink()

    existing_rows, existing_ids = _load_existing_case_ids(out_path)
    pending = [(cid, fx) for cid, fx in fixtures if cid not in existing_ids]

    if not pending:
        logger.info(
            "[%s] all %d fixtures already present; nothing to do",
            model_id,
            len(existing_rows),
        )
    else:
        logger.info(
            "[%s] running %d fixtures (%d already present)",
            model_id,
            len(pending),
            len(existing_rows),
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Decide whether to parallelize. Network path (live Opus
    # count_tokens) benefits; offline paths do not.
    is_network_path = (
        model_id == "anthropic/claude-opus-4.7"
        and bool(os.environ.get("ANTHROPIC_API_KEY"))
    )

    new_rows: list[CaseRow]
    if is_network_path and max_workers > 1 and pending:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            # Preserve fixture order via list(map(...)) over the executor.
            new_rows = list(
                pool.map(
                    lambda args: _compute_one(adapter, model_id, *args),
                    pending,
                )
            )
    else:
        new_rows = [
            _compute_one(adapter, model_id, cid, fx) for cid, fx in pending
        ]

    # Append-mode write: existing rows first (unchanged), then new rows.
    # Rewriting the whole file keeps ordering deterministic and matches
    # the force-rewrite path.
    with out_path.open("w", encoding="utf-8") as fh:
        for row in existing_rows:
            fh.write(json.dumps(row) + "\n")
        for row in new_rows:
            fh.write(json.dumps(asdict(row)) + "\n")

    summary_path = out_path.with_suffix(".summary.json")
    write_summary(out_path, summary_path)

    return out_path


def run_floor(
    models: list[str],
    fixtures_dir: Path | None = None,
    results_dir: Path | None = None,
    run_id: str | None = None,
    *,
    force: bool = False,
    max_workers: int = 8,
) -> dict[str, Path]:
    """Run the floor matrix across ``models`` and write per-model outputs.

    Args:
        models: OpenRouter-style model ids to run, e.g.
            ``["openai/gpt-5.4", "anthropic/claude-opus-4.7"]``.
        fixtures_dir: Root of the fixture tree (defaults to
            ``<repo>/fixtures``).
        results_dir: Root of the results tree (defaults to
            ``<repo>/results``).
        run_id: Optional explicit run id; defaults to the current UTC
            time formatted as ``YYYYMMDD-HHMMSS``.
        force: If True, overwrite existing ``{run_id}.jsonl`` rather
            than resume.
        max_workers: Max threads for the live Opus network path. Ignored
            on offline paths.

    Returns:
        ``{model_id: jsonl_path}`` for each model that was run.
    """
    if not models:
        raise ValueError("at least one model id is required")

    if fixtures_dir is None:
        fixtures_dir = Path("fixtures")
    if results_dir is None:
        results_dir = Path("results")
    fixtures_dir = Path(fixtures_dir)
    results_dir = Path(results_dir)

    if run_id is None:
        run_id = _make_run_id()

    # One adapter shared across models — tokenizer init is the expensive
    # part and the adapter holds independent backends per model id.
    enable_opus = "anthropic/claude-opus-4.7" in models
    enable_gpt = "openai/gpt-5.4" in models
    if not (enable_opus or enable_gpt):
        raise ValueError(
            "no supported models requested; "
            "expected one of: openai/gpt-5.4, anthropic/claude-opus-4.7"
        )
    adapter = TokenizerFloorAdapter(
        enable_opus=enable_opus, enable_gpt=enable_gpt
    )

    # Materialize fixture list once so both models see identical inputs.
    fixtures: list[tuple[str, dict]] = list(
        load_fixtures(fixtures_dir, langs=None)
    )

    out_paths: dict[str, Path] = {}
    for model_id in models:
        slug = model_slug(model_id)
        out_path = results_dir / slug / f"{run_id}.jsonl"
        _run_one_model(
            model_id=model_id,
            adapter=adapter,
            fixtures=fixtures,
            out_path=out_path,
            force=force,
            max_workers=max_workers,
        )
        out_paths[model_id] = out_path

    return out_paths
