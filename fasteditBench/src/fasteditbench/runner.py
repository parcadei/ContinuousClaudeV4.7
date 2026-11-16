"""Benchmark runner: iterate fixtures, invoke adapter, write JSONL (VAL-203).

This module implements the CLI-facing harness. The public surface is:

- :class:`CaseRow` — one row in the output JSONL file. Its field set is
  FROZEN by ``contract.json`` (VAL-203) and must not drift.
- :func:`load_fixtures` — walk ``fixtures/{lang}/*.yaml`` and yield
  ``(case_id, fixture_dict)`` tuples.
- :func:`run_case` — run a single fixture through an adapter and compute
  tier + diff-quality metrics, returning a :class:`CaseRow`. Adapter
  failures surface via ``CaseRow.error``; this function never raises for
  adapter-side errors.
- :func:`run_all` — run every (optionally language-filtered) fixture and
  write one JSON line per case to
  ``{results_dir}/{adapter_name}/{run_id}.jsonl``.
- :func:`get_adapter` — registry lookup mapping adapter names to
  instances. New adapters (e.g. ``"fastedit"`` in M3) plug in here.

JSONL schema (exact, frozen):

    case_id, language, pattern, tier1_pass, tier2_pass, hunk_count,
    minimality_ratio, context_lines_preserved, tokens_generated, error,
    target_kind, target_name,
    # VAL-101 (bench-02): frontier-LLM comparator fields appended to the
    # schema. Defaults preserve prefix-compatibility with historical
    # FastEdit runs (input_tokens=0, latency=None, etc.).
    input_tokens, output_tokens, latency_ms, tool_call_count, retry_count,
    input_tokens_native, output_tokens_native

Earlier fields are kept in position so the historical markers-only
JSONL remains prefix-compatible for audit comparisons.

No timing fields on the FastEdit side — speed was explicitly out of
scope for bench-01 (see DESIGN.md § 1). ``latency_ms`` is populated
only by the OpenRouter adapter.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from fasteditbench.adapter import AdapterResult, EditAdapter, NullAdapter
from fasteditbench.diff_quality import compute_diff_quality
from fasteditbench.reporting import write_summary
from fasteditbench.tiers import classify

__all__ = [
    "CaseRow",
    "get_adapter",
    "load_fixtures",
    "run_all",
    "run_case",
]


# The 13 target language slugs — same set as fixtures/_validate.py's
# LANGUAGES_ALLOWLIST. Duplicated intentionally rather than imported (the
# validator is a standalone script, not a library).
_LANGUAGES: frozenset[str] = frozenset({
    "python", "rust", "go", "typescript", "java", "kotlin", "csharp",
    "c", "cpp", "ruby", "swift", "php", "elixir",
})


@dataclass
class CaseRow:
    """One row in the JSONL output file.

    Field order matches the contract verbatim. ``tokens_generated`` is
    ``None`` for deterministic adapters (e.g. :class:`NullAdapter`). All
    other fields are always populated, including on the error path where
    tier + metric fields are set to safe sentinels (``False`` / ``0`` /
    ``0.0``).
    """

    case_id: str
    language: str
    pattern: str
    tier1_pass: bool
    tier2_pass: bool
    hunk_count: int
    minimality_ratio: float
    context_lines_preserved: float
    tokens_generated: int | None
    error: str | None
    # MCP-mode target metadata (VAL-305). Appended to the frozen schema
    # so the 20260422-105944 markers-only JSONL remains prefix-compatible
    # for audit comparisons.
    target_kind: str
    target_name: str | None
    # bench-02 VAL-101 extensions. Defaults match the "historical FastEdit
    # run" shape: zero input tokens (FastEdit's merge model only generates
    # the snippet, no re-reading of the file), no latency recorded, no
    # tool calls, no retries. OpenRouter-backed rows populate all of them.
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float | None = None
    tool_call_count: int | None = None
    retry_count: int = 0
    # Native-tokenizer counts (VAL-102 T-003 mitigation). Populated only
    # when OpenRouter-reported counts diverge from a native-tokenizer
    # cross-check by >5% on the model. ``None`` means "no discrepancy
    # observed — trust OpenRouter-reported counts".
    input_tokens_native: int | None = None
    output_tokens_native: int | None = None
    # bench-02 VAL-300 (M3): token-floor comparator fields. Populated by
    # the floor_runner (TokenizerFloorAdapter path). ``None`` on every
    # non-floor row — historical FastEdit + OpenRouter rows both leave
    # these blank. ``edit_payload_tokens`` is the headline floor number
    # (sum across hunks of the serialized tool-call payload tokens).
    # ``tokenizer_used`` is the human-readable tokenizer id (e.g.
    # ``"tiktoken:o200k_base"`` or ``"anthropic.count_tokens"``).
    # ``tokenizer_proxy`` is ``True`` only when the Opus path fell back
    # to ``tiktoken:cl100k_base`` because ``ANTHROPIC_API_KEY`` was
    # unset — disclosed in the VAL-401 report.
    edit_payload_tokens: int | None = None
    tokenizer_used: str | None = None
    tokenizer_proxy: bool | None = None


def load_fixtures(
    fixtures_dir: Path, langs: list[str] | None
) -> Iterable[tuple[str, dict]]:
    """Yield ``(case_id, fixture_dict)`` for each YAML under ``fixtures_dir``.

    Args:
        fixtures_dir: Root containing one subdirectory per language slug.
        langs: Restrict to these languages; ``None`` means all 13.

    Iteration order is deterministic: languages sorted alphabetically,
    then YAML filenames sorted alphabetically within each language.
    ``case_id`` format is ``"{language}/{filename_stem}"`` (e.g.
    ``"python/01-add_guard"``).
    """
    selected = sorted(_LANGUAGES if langs is None else set(langs))
    for lang in selected:
        lang_dir = fixtures_dir / lang
        if not lang_dir.is_dir():
            continue
        for yaml_path in sorted(lang_dir.glob("*.yaml")):
            # Skip helper files (e.g. "_something.yaml") — matches the
            # convention _validate.py uses for its discovery.
            if yaml_path.name.startswith("_"):
                continue
            with yaml_path.open("r", encoding="utf-8") as fh:
                fixture = yaml.safe_load(fh)
            case_id = f"{lang}/{yaml_path.stem}"
            yield case_id, fixture


def _error_row(case_id: str, fixture: dict, error: str) -> CaseRow:
    """Build a CaseRow with safe sentinels for the failure path.

    bench-02 comparator fields default to the historical-FastEdit shape:
    zero input/output tokens, no latency, no tool calls, no retries. The
    OpenRouter adapter overrides these on its error path so that even
    failed API calls carry usage + latency for budget + debugging.
    """
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


async def run_case(
    adapter: EditAdapter, case_id: str, fixture: dict
) -> CaseRow:
    """Run one fixture through ``adapter`` and return the resulting row.

    Any exception raised by the adapter is caught and surfaced via
    ``CaseRow.error``; the function itself never raises for adapter-side
    errors. If ``AdapterResult.error`` is set, tier / metric fields are
    set to safe sentinels and ``error`` is propagated onto the row.
    """
    original_code: str = fixture["original_code"]
    snippet: str = fixture["snippet"]
    language: str = fixture["language"]
    expected_output: str = fixture["expected_output"]
    pattern: str = fixture["pattern"]
    # MCP-mode target metadata (VAL-304). Fixtures annotated by
    # fixtures/_annotate_targets.py always carry these. We default to
    # the markers-only behaviour if a fixture is unannotated (defensive;
    # the validator enforces their presence on the canonical fixtures).
    target_kind: str = fixture.get("target_kind", "none")
    target_name: str | None = fixture.get("target_name")
    # target_batch (M7): when present on the fixture YAML (target_kind='batch'),
    # thread it through to the adapter so it can construct a BatchEdit list.
    target_batch = fixture.get("target_batch")
    context: dict = {
        "target_kind": target_kind,
        "target_name": target_name,
        "target_batch": target_batch,
    }

    try:
        result: AdapterResult = await adapter.apply(
            original_code, snippet, language, context
        )
    except Exception as exc:  # noqa: BLE001 - adapters MUST NOT raise, but we defend
        return _error_row(case_id, fixture, f"{type(exc).__name__}: {exc}")

    if result.error is not None:
        row = _error_row(case_id, fixture, result.error)
        # Preserve tokens_generated from the adapter if it reported any.
        return CaseRow(
            case_id=row.case_id,
            language=row.language,
            pattern=row.pattern,
            tier1_pass=False,
            tier2_pass=False,
            hunk_count=0,
            minimality_ratio=0.0,
            context_lines_preserved=0.0,
            tokens_generated=result.tokens_generated,
            error=result.error,
            target_kind=target_kind,
            target_name=target_name,
        )

    tier1_pass, tier2_pass = classify(
        result.output_text, expected_output, language
    )
    dq = compute_diff_quality(
        original_code, expected_output, result.output_text
    )

    return CaseRow(
        case_id=case_id,
        language=language,
        pattern=pattern,
        tier1_pass=tier1_pass,
        tier2_pass=tier2_pass,
        hunk_count=dq.hunk_count,
        minimality_ratio=dq.minimality_ratio,
        context_lines_preserved=dq.context_lines_preserved,
        tokens_generated=result.tokens_generated,
        error=None,
        target_kind=target_kind,
        target_name=target_name,
    )


def _make_run_id() -> str:
    """Return a UTC timestamp-based run id: ``YYYYMMDD-HHMMSS``."""
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


async def run_all(
    adapter: EditAdapter,
    adapter_name: str,
    fixtures_dir: Path,
    langs: list[str] | None,
    results_dir: Path,
) -> Path:
    """Run every fixture through ``adapter``, writing one JSONL row per case.

    Output path is ``{results_dir}/{adapter_name}/{run_id}.jsonl`` where
    ``run_id`` is a ``YYYYMMDD-HHMMSS`` UTC timestamp. The parent
    directory is created if missing. Returns the JSONL path.
    """
    run_id = _make_run_id()
    out_dir = results_dir / adapter_name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{run_id}.jsonl"

    with out_path.open("w", encoding="utf-8") as fh:
        for case_id, fixture in load_fixtures(fixtures_dir, langs):
            row = await run_case(adapter, case_id, fixture)
            fh.write(json.dumps(asdict(row)) + "\n")

    # Write per-language summary next to the JSONL (VAL-301).
    summary_path = out_dir / f"{run_id}.summary.json"
    write_summary(out_path, summary_path)

    return out_path


def get_adapter(name: str) -> EditAdapter:
    """Return the adapter instance registered under ``name``.

    Raises:
        ValueError: if ``name`` is not a known adapter.

    Future adapters (e.g. ``"fastedit"`` in M3) register here.
    """
    if name == "null":
        return NullAdapter()
    if name == "fastedit":
        # Imported lazily so environments without MLX can still use
        # ``get_adapter("null")`` (e.g. CI smoke tests).
        from fasteditbench.backends.fastedit_adapter import FastEditAdapter
        return FastEditAdapter()
    raise ValueError(f"unknown adapter: {name}")
