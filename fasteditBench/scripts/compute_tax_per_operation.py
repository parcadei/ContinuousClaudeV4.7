"""Compute the calling-agent *tax* per operation-category (bench-02 v3).

This script replaces the naive ``compute_snippet_floor`` which compared
apples to oranges: on one side a minimal ``(old_string, new_string)``
pair derived from ``difflib.SequenceMatcher`` (as tight as one line);
on the other side a full FastEdit snippet including signature anchor.
That produced a misleading 0.992x ratio — snippet *slightly more*
expensive than native — because the native side was a straw-man floor
no real model could actually emit.

This rewrite reframes the metric as the *calling-agent tax* — the
output tokens the agent spends *on top of* the actual new code it
would write anyway:

* Native edit tax = ``old_string`` (anchor) + JSON wrapper.
* FastEdit tax = signature anchor + ``# ... existing code ...`` marker
  (for ``replace=`` with marker), nothing for ``after=`` inserts, or
  just the symbol name for ``fast_delete``.

We make the native side REALISTIC by surrounding each changed region
with **2 lines of context** on each side — what a real model would
emit so its ``old_string`` uniquely matches in the file. And we
*categorize* each fixture so the headline can exclude cases where
neither tool has a structural advantage (``whole_rewrite``).

Operation categories
--------------------

``modify_inside``
    ``target_kind='replace'`` AND snippet contains an "existing code"
    marker — the most common case; FastEdit emits signature + marker
    instead of the whole body.

``whole_rewrite``
    ``target_kind='replace'`` AND no marker — both tools emit the full
    new function. **Excluded from the headline ratio** — no structural
    savings possible.

``insert_after``
    ``target_kind='after'`` — FastEdit pays zero anchoring tax (the
    ``after=`` kwarg is structural); native edit still needs an anchor.

``delete``
    ``pattern`` contains ``delete`` or ``remove`` — FastEdit uses
    ``fast_delete(symbol=name)`` which is just the symbol name.

``inline_edit``
    ``target_kind='none'`` — the snippet IS the edit; no structural
    kwarg overhead on either side. Savings small or zero; documented.

``batch_edit``
    ``target_kind='batch'`` — FastEdit packs N edits per target_batch;
    native edit still needs N tool calls.

Outputs
-------

* ``results/tax_per_operation/{run_id}.jsonl`` — per-fixture rows.
* ``results/tax_per_operation/{run_id}.summary.json`` — aggregates.

A table is printed to stdout.
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import tiktoken

# Make the sibling src/ layout importable when the script is run
# directly (``uv run python scripts/compute_tax_per_operation.py``).
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from fasteditbench.floor_runner import synthesize_file_path  # noqa: E402
from fasteditbench.runner import load_fixtures  # noqa: E402

logger = logging.getLogger(__name__)

__all__ = [
    "ANCHOR_LINES",
    "build_fastedit_payload",
    "build_native_edit_payloads_with_context",
    "categorize_fixture",
    "compute_rows",
    "extract_hunks_with_context",
    "has_existing_code_marker",
    "main",
    "run_tax_per_operation",
    "summarize",
]

# Tokenizer — keep consistent with M3 GPT floor.
_ENCODING_NAME = "o200k_base"

# Lines of equal context grafted onto each side of every changed hunk.
# 2 is the defensible middle ground per the task spec: more context
# widens FastEdit's lead further; less produces old_strings too short
# for a real model to match uniquely.
ANCHOR_LINES = 2

# Marker substrings that signal "preserve existing body" in a snippet.
# FastEdit accepts both Python-style '# ...' and JS/C-style '// ...'
# prefixes for the hint.
_MARKER_SUBSTRINGS = (
    "# ... existing code ...",
    "// ... existing code ...",
    "/* ... existing code ... */",
    "-- ... existing code ...",     # sql, lua
    "<!-- ... existing code ... -->",  # markup
)


# ---------------------------------------------------------------------------
# Categorization
# ---------------------------------------------------------------------------


def has_existing_code_marker(snippet: str) -> bool:
    """Return True if ``snippet`` contains any FastEdit preservation marker.

    Recognizes comment-prefix variants used across the fixture languages.
    """
    return any(marker in snippet for marker in _MARKER_SUBSTRINGS)


def categorize_fixture(
    target_kind: str | None,
    pattern: str,
    snippet: str,
) -> str:
    """Classify a fixture into one of six operation categories.

    ``delete`` is reserved for whole-symbol removal (``target_kind=delete``).
    ``delete_lines`` fixtures that remove lines from within a function
    body are classified by their structural shape — ``fast_delete`` would
    remove the entire function, which is not what the fixture expects.
    """
    if target_kind == "delete":
        return "delete"
    if target_kind == "after":
        return "insert_after"
    if target_kind == "batch":
        return "batch_edit"
    if target_kind == "none":
        return "inline_edit"
    if target_kind == "replace":
        if has_existing_code_marker(snippet):
            return "modify_inside"
        return "whole_rewrite"
    # Defensive fallback — no observed fixtures hit this branch.
    return "inline_edit"


# ---------------------------------------------------------------------------
# Realistic native-edit payload with 2-line anchor context
# ---------------------------------------------------------------------------


def extract_hunks_with_context(
    original: str,
    expected: str,
    n_context: int = ANCHOR_LINES,
) -> list[tuple[str, str]]:
    """Derive ``(old_string, new_string)`` hunks with ``n_context`` equal-lines
    on each side of every changed region.

    This replaces the straw-man M3 extraction which grafted only a
    single anchor line (or none for pure replaces). Real models can't
    reliably emit a 1-line ``old_string`` — it wouldn't uniquely match
    in most files — so the M3 floor was unfair to native edit.

    Algorithm:
        1. Split both texts by line (keepends).
        2. Run ``difflib.SequenceMatcher`` to get opcodes.
        3. For every non-equal opcode, compute the context-extended
           old/new ranges by grabbing up to ``n_context`` lines from the
           preceding and following equal opcodes.
        4. Merge adjacent hunks whose context windows overlap (so two
           changes separated by < ``2 * n_context`` lines collapse into
           one larger hunk — what a real model would do).

    Raises:
        ValueError: if ``original == expected`` — same-state fixture
            means empty payload, always a fixture bug.
    """
    if original == expected:
        raise ValueError("identical fixture — payload is empty")

    orig_lines = original.splitlines(keepends=True)
    exp_lines = expected.splitlines(keepends=True)
    sm = difflib.SequenceMatcher(a=orig_lines, b=exp_lines, autojunk=False)
    opcodes = sm.get_opcodes()

    # First pass: compute context-extended (i1, i2, j1, j2) per change.
    extended: list[tuple[int, int, int, int]] = []
    for idx, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag == "equal":
            continue
        # Look backwards for up to n_context lines of equal context.
        before = 0
        if idx > 0 and opcodes[idx - 1][0] == "equal":
            pi1, pi2 = opcodes[idx - 1][1], opcodes[idx - 1][2]
            before = min(n_context, pi2 - pi1)
        # Look forwards for up to n_context lines of equal context.
        after = 0
        if idx + 1 < len(opcodes) and opcodes[idx + 1][0] == "equal":
            ni1, ni2 = opcodes[idx + 1][1], opcodes[idx + 1][2]
            after = min(n_context, ni2 - ni1)
        new_i1 = i1 - before
        new_i2 = i2 + after
        new_j1 = j1 - before
        new_j2 = j2 + after
        extended.append((new_i1, new_i2, new_j1, new_j2))

    if not extended:
        # Should never happen given the identical-check above but keep
        # the invariant visible to future maintainers.
        raise ValueError("no non-equal opcodes but original != expected")

    # Merge overlapping / adjacent hunks (windows touch or intersect).
    merged: list[tuple[int, int, int, int]] = [extended[0]]
    for i1, i2, j1, j2 in extended[1:]:
        pi1, pi2, pj1, pj2 = merged[-1]
        if i1 <= pi2:  # overlap or adjacent on the original side
            merged[-1] = (pi1, max(pi2, i2), pj1, max(pj2, j2))
        else:
            merged.append((i1, i2, j1, j2))

    hunks: list[tuple[str, str]] = []
    for i1, i2, j1, j2 in merged:
        old_block = "".join(orig_lines[i1:i2])
        new_block = "".join(exp_lines[j1:j2])
        hunks.append((old_block, new_block))
    return hunks


def _serialize_native_edit(file_path: str, old: str, new: str) -> str:
    """Serialize one ``edit`` tool call exactly as a calling agent emits it.

    Byte-compatible with ``TokenizerFloorAdapter._serialize_tool_call`` —
    key order and separators match so direct cross-comparisons with the
    VAL-300 floor run don't drift on serialization choices.
    """
    return json.dumps(
        {
            "name": "edit",
            "arguments": {
                "file_path": file_path,
                "old_string": old,
                "new_string": new,
            },
        },
        separators=(",", ":"),
    )


def build_native_edit_payloads_with_context(
    file_path: str,
    original: str,
    expected: str,
) -> list[str]:
    """Return one serialized ``edit`` tool call per context-extended hunk."""
    return [
        _serialize_native_edit(file_path, old, new)
        for old, new in extract_hunks_with_context(original, expected)
    ]


# ---------------------------------------------------------------------------
# FastEdit payload
# ---------------------------------------------------------------------------


def build_fastedit_payload(
    operation_category: str,
    file_path: str,
    snippet: str,
    target_kind: str | None,
    target_name: str | None,
) -> str:
    """Serialize the minimal FastEdit tool call for this fixture.

    Shape follows the ``fastedit`` MCP surface:

    * ``delete`` → ``fast_delete(file_path, symbol)`` — just the name.
    * ``insert_after`` → ``fast_edit(file_path, edit_snippet, after=name)``.
    * ``modify_inside`` / ``whole_rewrite`` →
      ``fast_edit(file_path, edit_snippet, replace=name)``.
    * ``batch_edit`` → ``fast_edit(file_path, edit_snippet, replace=name)``
      (single-tool-call batch; FastEdit resolves siblings internally).
    * ``inline_edit`` → ``fast_edit(file_path, edit_snippet)`` — no kwarg.
    """
    if operation_category == "delete":
        return json.dumps(
            {
                "name": "fast_delete",
                "arguments": {
                    "file_path": file_path,
                    "symbol": target_name or "",
                },
            },
            separators=(",", ":"),
        )

    arguments: dict[str, str] = {
        "file_path": file_path,
        "edit_snippet": snippet,
    }
    if operation_category == "insert_after" and target_name:
        arguments["after"] = target_name
    elif operation_category in ("modify_inside", "whole_rewrite", "batch_edit"):
        if target_kind == "replace" and target_name:
            arguments["replace"] = target_name
        elif target_kind == "batch" and target_name:
            # batch fixtures sometimes set target_name=null and use
            # target_batch; we fall through to no-kwarg in that case.
            arguments["replace"] = target_name
    # inline_edit → no structural kwarg
    return json.dumps(
        {"name": "fast_edit", "arguments": arguments},
        separators=(",", ":"),
    )


# ---------------------------------------------------------------------------
# Row computation
# ---------------------------------------------------------------------------


def compute_rows(fixtures_dir: Path) -> list[dict]:
    """Walk every fixture; compute native + FastEdit token costs.

    Returns one dict per fixture with all fields needed for JSONL + the
    summary aggregates. Order matches :func:`load_fixtures`.
    """
    enc = tiktoken.get_encoding(_ENCODING_NAME)
    rows: list[dict] = []

    for case_id, fixture in load_fixtures(fixtures_dir, langs=None):
        language = str(fixture.get("language", ""))
        pattern = str(fixture.get("pattern", ""))
        snippet = str(fixture.get("snippet", ""))
        original = str(fixture.get("original_code", ""))
        expected = str(fixture.get("expected_output", ""))
        target_kind = fixture.get("target_kind")
        if not isinstance(target_kind, str):
            target_kind = None
        target_name = fixture.get("target_name")
        if not isinstance(target_name, str):
            target_name = None

        operation_category = categorize_fixture(target_kind, pattern, snippet)
        file_path = synthesize_file_path(case_id, language)

        native_payloads = build_native_edit_payloads_with_context(
            file_path, original, expected
        )
        native_tokens = sum(len(enc.encode(p)) for p in native_payloads)
        n_hunks = len(native_payloads)

        fastedit_payload = build_fastedit_payload(
            operation_category,
            file_path,
            snippet,
            target_kind,
            target_name,
        )
        fastedit_tokens = len(enc.encode(fastedit_payload))

        savings_tokens = native_tokens - fastedit_tokens
        savings_pct = (
            (savings_tokens / native_tokens * 100.0) if native_tokens else 0.0
        )

        rows.append(
            {
                "case_id": case_id,
                "language": language,
                "pattern": pattern,
                "target_kind": target_kind,
                "has_marker": has_existing_code_marker(snippet),
                "operation_category": operation_category,
                "file_size_bytes": len(original.encode("utf-8")),
                "n_hunks": n_hunks,
                "native_edit_tokens": native_tokens,
                "fastedit_tokens": fastedit_tokens,
                "savings_tokens": savings_tokens,
                "savings_pct": round(savings_pct, 3),
            }
        )

    return rows


# ---------------------------------------------------------------------------
# Summarization
# ---------------------------------------------------------------------------


def _bucket_new() -> dict:
    return {
        "n": 0,
        "native_total": 0,
        "fastedit_total": 0,
        "savings_total": 0,
    }


def _bucket_finalize(b: dict) -> None:
    nat = b["native_total"]
    sav = b["savings_total"]
    b["savings_pct"] = round((sav / nat * 100.0) if nat else 0.0, 3)
    b["ratio_native_over_fastedit"] = round(
        (nat / b["fastedit_total"]) if b["fastedit_total"] else 0.0, 3
    )
    b["native_avg"] = round((nat / b["n"]) if b["n"] else 0.0, 2)
    b["fastedit_avg"] = round(
        (b["fastedit_total"] / b["n"]) if b["n"] else 0.0, 2
    )


def summarize(rows: list[dict], run_id: str) -> dict:
    """Aggregate rows into per-category + per-language + headline summaries."""
    per_cat: dict[str, dict] = defaultdict(_bucket_new)
    per_lang: dict[str, dict] = defaultdict(_bucket_new)
    per_lang_ex_whole: dict[str, dict] = defaultdict(_bucket_new)

    total_native = 0
    total_fastedit = 0
    headline_native = 0
    headline_fastedit = 0
    headline_n = 0

    for r in rows:
        c = r["operation_category"]
        lang = r["language"]
        nat = r["native_edit_tokens"]
        fe = r["fastedit_tokens"]
        sav = r["savings_tokens"]
        for bucket in (per_cat[c], per_lang[lang]):
            bucket["n"] += 1
            bucket["native_total"] += nat
            bucket["fastedit_total"] += fe
            bucket["savings_total"] += sav
        total_native += nat
        total_fastedit += fe
        if c != "whole_rewrite":
            headline_native += nat
            headline_fastedit += fe
            headline_n += 1
            b2 = per_lang_ex_whole[lang]
            b2["n"] += 1
            b2["native_total"] += nat
            b2["fastedit_total"] += fe
            b2["savings_total"] += sav

    for b in per_cat.values():
        _bucket_finalize(b)
    for b in per_lang.values():
        _bucket_finalize(b)
    for b in per_lang_ex_whole.values():
        _bucket_finalize(b)

    headline_savings = headline_native - headline_fastedit
    headline_pct = (
        (headline_savings / headline_native * 100.0) if headline_native else 0.0
    )
    headline_ratio = (
        (headline_native / headline_fastedit) if headline_fastedit else 0.0
    )

    return {
        "run_id": run_id,
        "tokenizer_used": f"tiktoken:{_ENCODING_NAME}",
        "anchor_context_lines": ANCHOR_LINES,
        "n_fixtures": len(rows),
        "totals": {
            "native_total": total_native,
            "fastedit_total": total_fastedit,
            "savings_total": total_native - total_fastedit,
            "savings_pct": round(
                ((total_native - total_fastedit) / total_native * 100.0)
                if total_native
                else 0.0,
                3,
            ),
        },
        "operation_categories": dict(per_cat),
        "headline_excluding_whole_rewrite": {
            "n_fixtures": headline_n,
            "native_total": headline_native,
            "fastedit_total": headline_fastedit,
            "savings_tokens": headline_savings,
            "savings_pct": round(headline_pct, 3),
            "savings_ratio": round(headline_ratio, 3),
        },
        "per_language": dict(per_lang),
        "per_language_excluding_whole_rewrite": dict(per_lang_ex_whole),
    }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_tax_per_operation(
    fixtures_dir: Path,
    results_dir: Path,
    run_id: str | None = None,
) -> tuple[Path, Path, dict]:
    """Compute rows, write JSONL + summary, return paths and summary."""
    if run_id is None:
        run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")

    out_dir = results_dir / "tax_per_operation"
    out_dir.mkdir(parents=True, exist_ok=True)

    jsonl_path = out_dir / f"{run_id}.jsonl"
    summary_path = out_dir / f"{run_id}.summary.json"

    rows = compute_rows(fixtures_dir)
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    summary = summarize(rows, run_id)
    with summary_path.open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)

    return jsonl_path, summary_path, summary


def _print_summary(summary: dict, jsonl_path: Path, summary_path: Path) -> None:
    """Print a human-readable table to stdout."""
    totals = summary["totals"]
    hl = summary["headline_excluding_whole_rewrite"]
    print("=" * 78)
    print("bench-02 v3 — calling-agent tax per operation category")
    print("=" * 78)
    print(f"  fixtures:        {summary['n_fixtures']}")
    print(f"  tokenizer:       {summary['tokenizer_used']}")
    print(f"  anchor context:  ±{summary['anchor_context_lines']} lines")
    print()
    print("OVERALL")
    print(f"  native total:    {totals['native_total']:>7}")
    print(f"  fastedit total:  {totals['fastedit_total']:>7}")
    print(f"  savings:         {totals['savings_total']:>7} "
          f"({totals['savings_pct']:>6.2f}%)")
    print()
    print("HEADLINE (excluding whole_rewrite)")
    print(f"  n:               {hl['n_fixtures']:>7}")
    print(f"  native total:    {hl['native_total']:>7}")
    print(f"  fastedit total:  {hl['fastedit_total']:>7}")
    print(f"  savings:         {hl['savings_tokens']:>7} "
          f"({hl['savings_pct']:>6.2f}%)")
    print(f"  ratio n/fe:      {hl['savings_ratio']:>7.3f}x")
    print()
    print("PER OPERATION CATEGORY (sorted by savings_pct desc)")
    print(f"  {'category':<16} {'n':>3} {'native':>8} {'fastedit':>9} "
          f"{'saved':>8} {'%':>7}")
    sorted_cats = sorted(
        summary["operation_categories"].items(),
        key=lambda kv: kv[1]["savings_pct"],
        reverse=True,
    )
    for cat, b in sorted_cats:
        print(
            f"  {cat:<16} {b['n']:>3} "
            f"{b['native_total']:>8} "
            f"{b['fastedit_total']:>9} "
            f"{b['savings_total']:>8} "
            f"{b['savings_pct']:>6.2f}%"
        )
    print()
    print("PER LANGUAGE — excluding whole_rewrite (sorted by savings_pct desc)")
    print(f"  {'lang':<12} {'n':>3} {'native':>8} {'fastedit':>9} "
          f"{'saved':>8} {'%':>7}")
    sorted_langs = sorted(
        summary["per_language_excluding_whole_rewrite"].items(),
        key=lambda kv: kv[1]["savings_pct"],
        reverse=True,
    )
    for lang, b in sorted_langs:
        print(
            f"  {lang:<12} {b['n']:>3} "
            f"{b['native_total']:>8} "
            f"{b['fastedit_total']:>9} "
            f"{b['savings_total']:>8} "
            f"{b['savings_pct']:>6.2f}%"
        )
    print()
    print(f"wrote: {jsonl_path}")
    print(f"wrote: {summary_path}")


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description=(
            "Compute calling-agent tax per operation category across "
            "every bench-02 fixture — realistic 2-line anchor context."
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

    jsonl_path, summary_path, summary = run_tax_per_operation(
        args.fixtures_dir, args.results_dir, args.run_id
    )
    _print_summary(summary, jsonl_path, summary_path)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
