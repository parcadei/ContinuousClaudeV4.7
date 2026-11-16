"""Compute the calling-agent *tax* per operation-category (bench-02 v4).

This is a drop-in successor to ``compute_tax_per_operation.py`` (v3).
The only behavioural change lives on the FastEdit side of the
comparison: for fixtures where ``target_kind == "replace"`` (i.e.
``modify_inside`` / ``whole_rewrite`` / ``batch_edit`` routed to
``replace=``), the authored snippet's leading signature line is
stripped *before* tokenization.

Why? FastEdit ``0.2.2`` (shipped 2026-04-23) added AST-level auto
preservation of the function signature when the caller uses the
``replace=<name>`` keyword argument. Before 0.2.2 the snippet had to
re-state the signature verbatim as its first line for the merge model
to anchor against. From 0.2.2 onwards that line is redundant — the
calling agent can send just the body change and FastEdit will graft
it under the existing signature.

The v3 script measured snippets in their authored form (signature
included), which baked in the pre-0.2.2 floor (~30% savings on
``modify_inside``). v4 measures what a 0.2.2+ calling agent would
actually emit: the minimal snippet.

The native-edit side is unchanged — the same ±2-line anchor context
rule from v3 still applies. v4 only changes one wire format, not the
comparison framing.

Scope guardrails
----------------

* ``modify_inside`` / ``whole_rewrite``  — strip signature when it
  really is on line 1 of the snippet.
* ``batch_edit``                        — strip only when
  ``target_kind == "replace"`` (the wire format in those cases is
  ``replace=``). A batch that uses ``target_batch`` / no ``replace=``
  is left alone.
* ``insert_after`` / ``delete`` / ``inline_edit`` — never strip. Those
  categories don't use ``replace=`` so 0.2.2's auto-preservation
  doesn't apply.
* If the heuristic fails (signature not on line 1, or first line is a
  decorator / attribute / comment / import), the snippet is left
  untouched and the row is marked ``signature_stripped: false``.

Outputs
-------

* ``results/tax_per_operation/{run_id}.jsonl``
* ``results/tax_per_operation/{run_id}.summary.json``

Per-row fields added over v3:

* ``signature_stripped: bool``
* ``snippet_before_strip_tokens: int``
* ``snippet_after_strip_tokens: int``

Summary-level ``methodology: "v4_minimal_snippets_for_replace"`` is
written so future consumers can tell v3 and v4 runs apart without
comparing timestamps.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import tiktoken

# Make the sibling src/ layout importable when the script is run
# directly (``uv run python scripts/compute_tax_per_operation_v4.py``).
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

# Reuse the v3 helpers — only the FastEdit snippet-prep step differs.
import compute_tax_per_operation as v3  # noqa: E402
from fasteditbench.floor_runner import synthesize_file_path  # noqa: E402
from fasteditbench.runner import load_fixtures  # noqa: E402

logger = logging.getLogger(__name__)

__all__ = [
    "ANCHOR_LINES",
    "METHODOLOGY_TAG",
    "build_fastedit_payload_v4",
    "compute_rows_v4",
    "first_line_is_signature",
    "maybe_strip_signature",
    "run_tax_per_operation_v4",
    "strip_signature_line",
    "summarize_v4",
]

# Re-export for symmetry with v3.
ANCHOR_LINES = v3.ANCHOR_LINES
_ENCODING_NAME = "o200k_base"

# Tag written into the summary so consumers can distinguish v3 and v4
# runs without inferring from timestamps.
METHODOLOGY_TAG = "v4_minimal_snippets_for_replace"

# Keywords that can introduce a function / method signature across the
# 13 supported languages. Used as a shape check for paren-less
# declarations (e.g. Ruby ``def foo``, Elixir ``def foo do``).
_SIGNATURE_KEYWORDS = (
    "def",        # python, ruby, elixir
    "fn",         # rust
    "func",       # go, swift
    "function",   # php, javascript, typescript
    "fun",        # kotlin
)

# Categories whose FastEdit wire format uses ``replace=<name>`` and
# therefore benefit from 0.2.2's signature auto-preservation.
_REPLACE_CATEGORIES = ("modify_inside", "whole_rewrite", "batch_edit")


# ---------------------------------------------------------------------------
# Signature detection + stripping
# ---------------------------------------------------------------------------


def first_line_is_signature(first_line: str, target_name: str) -> bool:
    """Return True when ``first_line`` looks like a signature for
    ``target_name`` in one of the 13 supported languages.

    Heuristic:

    1. ``target_name`` must appear as a whole-word token on the line.
    2. One of the following must also hold:

       * The line contains ``(`` — covers every parenthesised
         declaration (Python ``def name(...)``, Rust ``fn name(...)``,
         Go ``func name(...)``, Java/C#/C/C++ ``T name(...)``,
         TS/JS/PHP ``function name(...)``, Swift ``func name(...)``,
         Kotlin ``fun name(...)``, Ruby ``def name(...)``, Elixir
         ``def name(...)``).
       * OR the line begins with a known signature keyword
         (``def`` / ``fn`` / ``func`` / ``function`` / ``fun``) directly
         followed by ``target_name`` — catches the paren-less Ruby and
         Elixir form (``def foo do`` / ``def foo``).

    Returns False on decorator/attribute lines (``@MainActor``),
    comments, imports, or any first line that doesn't mention the
    target by name.

    The check is intentionally conservative. A false-negative (fail to
    strip a legitimate signature) just means that fixture's
    measurement stays at the v3 floor — under-counting FastEdit's
    savings. A false-positive would over-count and corrupt the
    headline, so the heuristic errs toward skipping.
    """
    if not target_name:
        return False

    # Skip lines that are obviously not a signature. Decorators /
    # attributes / preprocessor directives / plain comments.
    stripped = first_line.lstrip()
    if not stripped:
        return False
    if stripped.startswith(("@", "#", "//", "/*", "--", "<!--", "*")):
        # Python `#` is also a comment; no Python signature starts with
        # `#`, so rejecting here is safe. The `def` signature wouldn't
        # begin with `#`.
        return False

    # Whole-word match on the target name.
    name_re = re.escape(target_name)
    if not re.search(rf"\b{name_re}\b", first_line):
        return False

    # Paren-containing declaration — fast path, covers almost every
    # real fixture.
    if "(" in first_line:
        return True

    # Paren-less signature keyword form (Ruby / Elixir).
    keyword_alt = "|".join(_SIGNATURE_KEYWORDS)
    if re.search(rf"^\s*({keyword_alt})\s+{name_re}\b", first_line):
        return True

    return False


def strip_signature_line(snippet: str) -> str:
    """Return ``snippet`` with its first line removed.

    Preserves the trailing newline structure of the original so the
    joined body indentation looks exactly as a 0.2.2 calling agent
    would send it.
    """
    # ``splitlines(keepends=True)`` so we don't have to reconstruct
    # newline style; drop the first element and join the rest.
    parts = snippet.splitlines(keepends=True)
    if not parts:
        return snippet
    return "".join(parts[1:])


def maybe_strip_signature(
    operation_category: str,
    target_kind: str | None,
    target_name: str | None,
    snippet: str,
) -> tuple[str, bool]:
    """Strip the signature line when the fixture benefits from FastEdit
    0.2.2's auto-preserve behaviour; otherwise return ``snippet``
    unchanged.

    The returned flag is the definitive ``signature_stripped`` field
    for the JSONL row.
    """
    # Only ``replace=`` wire format benefits from auto-preserve.
    if operation_category not in _REPLACE_CATEGORIES:
        return snippet, False
    if target_kind != "replace":
        return snippet, False
    if not target_name:
        return snippet, False

    # Need at least one full line before the body.
    first_newline = snippet.find("\n")
    if first_newline == -1:
        return snippet, False

    first_line = snippet[:first_newline]
    if not first_line_is_signature(first_line, target_name):
        return snippet, False

    return strip_signature_line(snippet), True


# ---------------------------------------------------------------------------
# FastEdit payload — v4 wraps v3 with the strip step
# ---------------------------------------------------------------------------


def build_fastedit_payload_v4(
    operation_category: str,
    file_path: str,
    snippet: str,
    target_kind: str | None,
    target_name: str | None,
) -> tuple[str, str, bool]:
    """Build the serialized FastEdit payload AND return the effective
    snippet plus a ``stripped`` flag.

    Returns ``(payload_json, effective_snippet, stripped)``.
    """
    effective_snippet, stripped = maybe_strip_signature(
        operation_category, target_kind, target_name, snippet
    )
    payload = v3.build_fastedit_payload(
        operation_category,
        file_path,
        effective_snippet,
        target_kind,
        target_name,
    )
    return payload, effective_snippet, stripped


# ---------------------------------------------------------------------------
# Row computation — duplicates the v3 loop but threads through strip
# ---------------------------------------------------------------------------


def compute_rows_v4(fixtures_dir: Path) -> list[dict]:
    """Walk every fixture; compute native + FastEdit v4 token costs.

    Parallels :func:`compute_tax_per_operation.compute_rows` but uses
    the stripped-snippet FastEdit payload when appropriate. Each row
    gains three new fields:

    * ``signature_stripped: bool``
    * ``snippet_before_strip_tokens: int``
    * ``snippet_after_strip_tokens: int``
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

        operation_category = v3.categorize_fixture(
            target_kind, pattern, snippet
        )
        file_path = synthesize_file_path(case_id, language)

        # Native side: unchanged from v3.
        native_payloads = v3.build_native_edit_payloads_with_context(
            file_path, original, expected
        )
        native_tokens = sum(len(enc.encode(p)) for p in native_payloads)
        n_hunks = len(native_payloads)

        # FastEdit side: strip signature if eligible.
        fastedit_payload, effective_snippet, stripped = (
            build_fastedit_payload_v4(
                operation_category,
                file_path,
                snippet,
                target_kind,
                target_name,
            )
        )
        fastedit_tokens = len(enc.encode(fastedit_payload))

        # Stand-alone snippet tokenization — lets readers see how much
        # of the tax came from the signature line vs the structural
        # wrapper.
        snippet_before = len(enc.encode(snippet))
        snippet_after = len(enc.encode(effective_snippet))

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
                "has_marker": v3.has_existing_code_marker(snippet),
                "operation_category": operation_category,
                "file_size_bytes": len(original.encode("utf-8")),
                "n_hunks": n_hunks,
                "native_edit_tokens": native_tokens,
                "fastedit_tokens": fastedit_tokens,
                "savings_tokens": savings_tokens,
                "savings_pct": round(savings_pct, 3),
                # v4 additions
                "signature_stripped": stripped,
                "snippet_before_strip_tokens": snippet_before,
                "snippet_after_strip_tokens": snippet_after,
            }
        )

    return rows


# ---------------------------------------------------------------------------
# Summarization
# ---------------------------------------------------------------------------


def summarize_v4(rows: list[dict], run_id: str) -> dict:
    """v4 summary — same shape as v3 plus a ``methodology`` tag and a
    ``signature_strip_stats`` block (overall + per-language +
    per-category counts of fixtures where the signature line was
    stripped)."""
    summary = v3.summarize(rows, run_id)

    # Overall + per-language + per-category strip counts.
    strip_by_lang: dict[str, int] = defaultdict(int)
    strip_by_cat: dict[str, int] = defaultdict(int)
    n_by_lang: dict[str, int] = defaultdict(int)
    n_by_cat: dict[str, int] = defaultdict(int)
    total_stripped = 0

    for r in rows:
        lang = r["language"]
        cat = r["operation_category"]
        n_by_lang[lang] += 1
        n_by_cat[cat] += 1
        if r.get("signature_stripped"):
            total_stripped += 1
            strip_by_lang[lang] += 1
            strip_by_cat[cat] += 1

    summary["methodology"] = METHODOLOGY_TAG
    summary["signature_strip_stats"] = {
        "total_stripped": total_stripped,
        "total_fixtures": len(rows),
        "per_language": {
            lang: {
                "stripped": strip_by_lang.get(lang, 0),
                "total": n_by_lang[lang],
            }
            for lang in sorted(n_by_lang)
        },
        "per_category": {
            cat: {
                "stripped": strip_by_cat.get(cat, 0),
                "total": n_by_cat[cat],
            }
            for cat in sorted(n_by_cat)
        },
    }
    return summary


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_tax_per_operation_v4(
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

    rows = compute_rows_v4(fixtures_dir)
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    summary = summarize_v4(rows, run_id)
    with summary_path.open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)

    return jsonl_path, summary_path, summary


def _print_summary(summary: dict, jsonl_path: Path, summary_path: Path) -> None:
    """Print a v4 table to stdout (mirrors v3 format + strip stats)."""
    totals = summary["totals"]
    hl = summary["headline_excluding_whole_rewrite"]
    strip_stats = summary.get("signature_strip_stats", {})

    print("=" * 78)
    print("bench-02 v4 — calling-agent tax per operation category")
    print("                (FastEdit 0.2.2 minimal-snippet form)")
    print("=" * 78)
    print(f"  fixtures:        {summary['n_fixtures']}")
    print(f"  tokenizer:       {summary['tokenizer_used']}")
    print(f"  anchor context:  ±{summary['anchor_context_lines']} lines")
    print(f"  methodology:     {summary.get('methodology', '(unset)')}")
    print(
        f"  signatures stripped: "
        f"{strip_stats.get('total_stripped', 0)} / "
        f"{strip_stats.get('total_fixtures', 0)}"
    )
    print()
    print("OVERALL")
    print(f"  native total:    {totals['native_total']:>7}")
    print(f"  fastedit total:  {totals['fastedit_total']:>7}")
    print(
        f"  savings:         {totals['savings_total']:>7} "
        f"({totals['savings_pct']:>6.2f}%)"
    )
    print()
    print("HEADLINE (excluding whole_rewrite)")
    print(f"  n:               {hl['n_fixtures']:>7}")
    print(f"  native total:    {hl['native_total']:>7}")
    print(f"  fastedit total:  {hl['fastedit_total']:>7}")
    print(
        f"  savings:         {hl['savings_tokens']:>7} "
        f"({hl['savings_pct']:>6.2f}%)"
    )
    print(f"  ratio n/fe:      {hl['savings_ratio']:>7.3f}x")
    print()
    print("PER OPERATION CATEGORY (sorted by savings_pct desc)")
    print(
        f"  {'category':<16} {'n':>3} {'native':>8} {'fastedit':>9} "
        f"{'saved':>8} {'%':>7}"
    )
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
    print("SIGNATURE STRIP — per category")
    for cat, info in sorted(
        strip_stats.get("per_category", {}).items(),
        key=lambda kv: kv[0],
    ):
        print(
            f"  {cat:<16} {info['stripped']:>3} / {info['total']:>3}"
        )
    print()
    print("SIGNATURE STRIP — per language")
    for lang, info in sorted(
        strip_stats.get("per_language", {}).items(),
        key=lambda kv: kv[0],
    ):
        print(
            f"  {lang:<12} {info['stripped']:>3} / {info['total']:>3}"
        )
    print()
    print(f"wrote: {jsonl_path}")
    print(f"wrote: {summary_path}")


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description=(
            "Compute calling-agent tax per operation category across "
            "every bench-02 fixture — v4 with FastEdit 0.2.2 minimal "
            "snippets."
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

    jsonl_path, summary_path, summary = run_tax_per_operation_v4(
        args.fixtures_dir, args.results_dir, args.run_id
    )
    _print_summary(summary, jsonl_path, summary_path)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
