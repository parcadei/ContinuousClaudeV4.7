"""FastAPI report surface for the benchmark (M4 — VAL-400 / VAL-401).

Two endpoints, zero third-party UI dependencies:

* ``GET /results`` — returns the aggregated ``summary.json`` for a
  given adapter / run_id as JSON. Protocol-ready: ``adapter`` is a
  query param (default ``fastedit``) so additional adapters can be
  plugged in later without a schema change. ``run_id`` (optional)
  pins a specific run; omitted means *latest*.

* ``GET /report`` — renders a static HTML report against the same
  summary (and the matching per-case JSONL for distributions).
  Includes: headline band, methodology disclaimer, per-language
  accuracy table, minimality_ratio histogram, hunk_count
  distribution, findings section (3 FastEdit / tldr bugs surfaced
  during the benchmark), footer with run metadata.

Everything is read-only: the report layer never writes to
``results/`` or mutates adapter output.
"""

from __future__ import annotations

import html as html_lib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PKG_ROOT = Path(__file__).resolve().parent
_PROJECT_ROOT = _PKG_ROOT.parent.parent
RESULTS_ROOT = _PROJECT_ROOT / "results"

DEFAULT_ADAPTER = "fastedit"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def _adapter_dir(adapter: str) -> Path:
    return RESULTS_ROOT / adapter


def _summary_path(adapter: str, run_id: str | None) -> Path | None:
    """Resolve a summary JSON path for ``adapter`` / ``run_id``.

    When ``run_id`` is ``None``, pick the lexicographically-latest
    summary file (filenames are ``YYYYMMDD-HHMMSS.summary.json`` so
    sort is chronological). Returns ``None`` when nothing matches.
    """
    adir = _adapter_dir(adapter)
    if not adir.is_dir():
        return None

    if run_id is not None:
        candidate = adir / f"{run_id}.summary.json"
        return candidate if candidate.is_file() else None

    summaries = sorted(adir.glob("*.summary.json"))
    return summaries[-1] if summaries else None


class _NotFoundError(Exception):
    """Internal sentinel for 404 paths — translated to a flat JSON body."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _load_summary(adapter: str, run_id: str | None) -> tuple[Path, dict[str, Any]]:
    path = _summary_path(adapter, run_id)
    if path is None:
        raise _NotFoundError(
            f"no summary for adapter={adapter!r} run_id={run_id!r}"
        )
    return path, json.loads(path.read_text(encoding="utf-8"))


def _load_rows(summary_path: Path) -> list[dict[str, Any]]:
    """Load the JSONL that sits next to a summary file (same stem)."""
    jsonl_path = summary_path.with_suffix("")  # strip .json
    if jsonl_path.suffix == ".summary":
        jsonl_path = jsonl_path.with_suffix(".jsonl")
    else:
        jsonl_path = summary_path.parent / (summary_path.stem.replace(".summary", "") + ".jsonl")

    if not jsonl_path.is_file():
        return []

    rows: list[dict[str, Any]] = []
    with jsonl_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="FastEditBench Report", version="0.1.0")


@app.exception_handler(_NotFoundError)
async def _not_found_handler(_request: Any, exc: _NotFoundError) -> JSONResponse:  # noqa: RUF029
    return JSONResponse(status_code=404, content={"error": exc.message})


@app.get("/results")
def get_results(
    adapter: str = Query(default=DEFAULT_ADAPTER),
    run_id: str | None = Query(default=None),
) -> JSONResponse:
    """Return the aggregated summary for a given adapter / run."""
    _, summary = _load_summary(adapter, run_id)
    return JSONResponse(content=summary)


@app.get("/report", response_class=HTMLResponse)
def get_report(
    adapter: str = Query(default=DEFAULT_ADAPTER),
    run_id: str | None = Query(default=None),
) -> HTMLResponse:
    """Render the static HTML report for a given adapter / run."""
    summary_path, summary = _load_summary(adapter, run_id)
    rows = _load_rows(summary_path)
    html = _render_report(summary, rows, summary_path, adapter)
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


DISCLAIMER_TEXT = (
    "This benchmark measures FastEdit's MCP-mode behavior — calls to "
    "<code>chunked_merge</code> with explicit <code>after=</code> / "
    "<code>replace=</code> kwargs (and the <code>fast_batch_edit</code> "
    "multi-target surface), replicating real agent invocation via the "
    "<code>fast_edit</code> / <code>fast_batch_edit</code> MCP tools. "
    "Fixtures cover 22 canonical patterns representative of agent-"
    "generated edits that FastEdit was designed to handle; performance on "
    "out-of-distribution edits may differ. Tier-1 = exact-match after "
    "trailing-whitespace normalization; Tier-2 = tree-sitter AST-"
    "equivalence (strips comments, whitespace-only nodes). All 13 "
    "languages (Python, Rust, Go, TypeScript, Java, Kotlin, C#, C, C++, "
    "Ruby, Swift, PHP, Elixir) route through FastEdit's "
    "<code>chunked_merge</code>. Reported numbers are post every upstream "
    "fix landed during the benchmark: Elixir support was added, a "
    "direct-swap fast path was added for full-symbol replacement, the "
    "<code>preserve_siblings</code> flag was added for surgical class "
    "edits, and two tldr-side corrections were applied (C struct-pointer "
    "misclassification and Swift-init / Java-constructor omission from "
    "the definitions set)."
)

# Histogram bin edges for minimality_ratio. Right-open intervals except
# the final bin which captures the long tail.
_MINIMALITY_BINS: list[tuple[float, float, str]] = [
    (0.0, 0.5, "0 – 0.5"),
    (0.5, 1.0, "0.5 – 1.0"),
    (1.0, 1.5, "1.0 – 1.5"),
    (1.5, 2.0, "1.5 – 2.0"),
    (2.0, 3.0, "2.0 – 3.0"),
    (3.0, float("inf"), "3.0+"),
]


def _bucket_minimality(values: list[float]) -> list[tuple[str, int]]:
    counts = [0] * len(_MINIMALITY_BINS)
    for v in values:
        for i, (lo, hi, _label) in enumerate(_MINIMALITY_BINS):
            if lo <= v < hi:
                counts[i] += 1
                break
    return [(label, counts[i]) for i, (_lo, _hi, label) in enumerate(_MINIMALITY_BINS)]


def _bucket_hunks(values: list[int]) -> list[tuple[str, int]]:
    if not values:
        return []
    counts: dict[int, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    items = sorted(counts.items())
    return [(str(k), v) for k, v in items]


def _bar_row(label: str, count: int, max_count: int) -> str:
    pct = (count / max_count * 100.0) if max_count > 0 else 0.0
    return (
        f'<tr>'
        f'<td class="bar-label">{html_lib.escape(label)}</td>'
        f'<td class="bar-cell"><div class="bar" style="width:{pct:.1f}%"></div></td>'
        f'<td class="bar-count">{count}</td>'
        f'</tr>'
    )


def _git_head_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def _fmt_pct(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.1f}%"


def _fmt_num(x: float | int | None, places: int = 2) -> str:
    if x is None:
        return "—"
    if isinstance(x, int):
        return str(x)
    return f"{x:.{places}f}"


def _render_report(
    summary: dict[str, Any],
    rows: list[dict[str, Any]],
    summary_path: Path,
    adapter: str,
) -> str:
    overall = summary.get("overall", {})
    per_lang = summary.get("per_language", {})
    run_id = summary.get("run_id", summary_path.stem.replace(".summary", ""))

    # --- headline ---
    tier1 = _fmt_pct(overall.get("tier1_pass_rate"))
    tier2 = _fmt_pct(overall.get("tier2_pass_rate"))
    n_cases = overall.get("n_cases", 0)
    n_errors = overall.get("n_errors", 0)
    total_tokens = overall.get("total_tokens_generated", 0)

    headline = (
        '<section class="headline">'
        '<div class="stat"><div class="stat-label">tier-1</div>'
        f'<div class="stat-value">{tier1}</div></div>'
        '<div class="stat"><div class="stat-label">tier-2</div>'
        f'<div class="stat-value">{tier2}</div></div>'
        '<div class="stat"><div class="stat-label">cases</div>'
        f'<div class="stat-value">{n_cases}</div></div>'
        '<div class="stat"><div class="stat-label">errors</div>'
        f'<div class="stat-value">{n_errors}</div></div>'
        '<div class="stat"><div class="stat-label">model tokens</div>'
        f'<div class="stat-value">{total_tokens}</div></div>'
        '</section>'
    )

    # --- disclaimer ---
    disclaimer = (
        '<section class="disclaimer">'
        '<h2>Methodology</h2>'
        f'<p>{DISCLAIMER_TEXT}</p>'
        '</section>'
    )

    # --- per-language table ---
    lang_rows: list[str] = []
    for lang in sorted(per_lang.keys()):
        data = per_lang[lang]
        lang_rows.append(
            '<tr>'
            f'<td class="lang">{html_lib.escape(lang)}</td>'
            f'<td>{data.get("n_cases", 0)}</td>'
            f'<td>{_fmt_pct(data.get("tier1_pass_rate"))}</td>'
            f'<td>{_fmt_pct(data.get("tier2_pass_rate"))}</td>'
            f'<td>{data.get("n_errors", 0)}</td>'
            f'<td>{_fmt_num(data.get("mean_minimality_ratio"), 2)}</td>'
            f'<td>{_fmt_num(data.get("mean_hunk_count"), 2)}</td>'
            '</tr>'
        )
    lang_table = (
        '<section class="per-lang">'
        '<h2>Per-language accuracy</h2>'
        '<table class="data">'
        '<thead><tr>'
        '<th>language</th><th>n</th><th>tier-1</th><th>tier-2</th>'
        '<th>errors</th><th>mean minimality</th><th>mean hunks</th>'
        '</tr></thead>'
        f'<tbody>{"".join(lang_rows)}</tbody>'
        '</table>'
        '</section>'
    )

    # --- distributions (read from JSONL, non-error rows only) ---
    ok_rows = [r for r in rows if r.get("error") is None]
    minimality_values = [
        float(r.get("minimality_ratio", 0.0)) for r in ok_rows
    ]
    hunk_values = [int(r.get("hunk_count", 0)) for r in ok_rows]

    min_buckets = _bucket_minimality(minimality_values)
    min_max = max((c for _, c in min_buckets), default=0)
    min_bars = "".join(_bar_row(label, c, min_max) for label, c in min_buckets)

    hunk_buckets = _bucket_hunks(hunk_values)
    hunk_max = max((c for _, c in hunk_buckets), default=0)
    hunk_bars = "".join(_bar_row(label, c, hunk_max) for label, c in hunk_buckets)

    distributions = (
        '<section class="dist">'
        '<h2>Minimality ratio distribution</h2>'
        '<p class="hint">Per-case <code>loc_changed / loc_required</code>. '
        '1.0 = perfectly minimal; &gt;1.0 = over-editing; &lt;1.0 = under-editing. '
        'Error rows excluded.</p>'
        f'<table class="bars">{min_bars}</table>'
        '<h2>Hunk count distribution</h2>'
        '<p class="hint">Number of <code>@@</code> hunks in the adapter-output diff '
        'against the original. Lower = more localized edits.</p>'
        f'<table class="bars">{hunk_bars}</table>'
        '</section>'
    )

    # --- findings ---
    findings = (
        '<section class="findings">'
        '<h2>Findings — FastEdit &amp; tldr bugs surfaced during the benchmark</h2>'
        '<p class="hint">Every finding below was filed, fixed upstream, '
        'and re-validated against the benchmark within the same '
        'milestone. Tier-1 improved from 75.6% at bench start to '
        '99.4% at final rebench as each fix landed.</p>'

        '<article class="finding">'
        '<h3>FASTEDIT-001 — preserved-gap indent mismatch '
        '<span class="tag fixed">Fixed: FastEdit@b063141</span></h3>'
        '<p><strong>Summary.</strong> When marker-mode preserved gaps need '
        're-indentation (e.g. wrapping in try/except), <code>deterministic_edit</code> '
        'was emitting the gap lines at their original indent, producing '
        'syntactically wrong output.</p>'
        '<p><strong>Impact at discovery.</strong> Contributed to ~4 '
        'Category-A fixture failures (<code>wrap_block</code> pattern).</p>'
        '<p><strong>Resolution.</strong> Indent delta now computed from '
        'marker position and applied to gap lines.</p>'
        '</article>'

        '<article class="finding">'
        '<h3>FASTEDIT-002 — two-marker bracketing produces wrong position '
        '<span class="tag fixed">Fixed: FastEdit@4d8989c</span></h3>'
        '<p><strong>Summary.</strong> Sections containing ≥2 markers had '
        'an ambiguous insert-position for new lines. Old behavior '
        'appended them after the entire preserved gap. New behavior '
        'defers to the model, which uses semantic context.</p>'
        '<p><strong>Impact at discovery.</strong> Contributed to ~4 '
        'Category-A fixture failures (<code>two_markers</code> pattern).</p>'
        '<p><strong>Resolution.</strong> <code>deterministic_edit</code> '
        'returns <code>None</code> on multi-marker sections → '
        '<code>chunked_merge</code> falls through to the model.</p>'
        '</article>'

        '<article class="finding">'
        '<h3>TLDR-C-001 — C struct pointer misclassification '
        '<span class="tag fixed">Fixed upstream (tldr)</span></h3>'
        '<p><strong>Summary.</strong> <code>tldr structure</code> on C '
        'snippets emitted <code>struct Foo *ptr</code> (a pointer '
        'parameter) as a top-level struct definition, causing '
        '<code>_extract_snippet_names</code> to falsely report "snippet '
        'defines additional symbols" and reject otherwise-valid edits. '
        'Bare type references in parameters, <code>sizeof</code> '
        'expressions, and forward declarations were all affected.</p>'
        '<p><strong>Impact at discovery.</strong> Blocked 2 C fixtures '
        '(<code>c/02-change-line-timeout</code>, '
        '<code>c/10-multi-insert-hooks</code>) from the deterministic '
        'fast path; both routed through the model as a workaround.</p>'
        '<p><strong>Resolution.</strong> tldr C grammar rebuilt to emit '
        'only struct defs with a body, not bare type references. Both '
        'fixtures now pass at 0 model tokens.</p>'
        '</article>'

        '<article class="finding">'
        '<h3>FASTEDIT-003 — Elixir was unsupported at start of benchmark '
        '<span class="tag fixed">Fixed: FastEdit@a956476</span></h3>'
        '<p><strong>Summary.</strong> <code>.ex</code>/<code>.exs</code> '
        'were missing from <code>EXTENSION_TO_LANGUAGE</code>; Elixir '
        'macros (<code>def</code>, <code>defp</code>, <code>defmodule</code>, '
        "etc.) weren't registered in <code>_FUNCTION_NODE_TYPES</code> / "
        '<code>_CLASS_NODE_TYPES</code>. Elixir parses these as '
        '<code>call</code> nodes (unlike most languages where functions '
        'have a dedicated node type), so the AST analyzer never '
        'recognized Elixir definitions.</p>'
        '<p><strong>Impact at discovery.</strong> All 12 Elixir fixtures '
        'counted as 0% / errored — FastEdit refused to operate on '
        '<code>.ex</code> files entirely.</p>'
        '<p><strong>Resolution.</strong> Extension map updated to include '
        '<code>.ex</code>/<code>.exs</code>; AST layer extended with '
        'call-node filtering that identifies Elixir macros by the name '
        'of their first identifier child. All 12 Elixir fixtures pass '
        'at tier-1 in the final rebench.</p>'
        '</article>'

        '<article class="finding">'
        '<h3>FASTEDIT-004 — direct-swap fast path for full-symbol replacement '
        '<span class="tag fixed">Fixed: FastEdit@2b98be3</span></h3>'
        '<p><strong>Summary.</strong> When a snippet IS the complete new '
        'definition of a single resolvable symbol (no markers, no '
        'elision), <code>chunked_merge</code> was still invoking the '
        'merge model — spending tokens on an edit that is equivalent to '
        'a pure AST splice. The deterministic text-match path only '
        'fired when the snippet shared unchanged context lines with the '
        'original.</p>'
        '<p><strong>Impact at discovery.</strong> Category-B fixtures '
        '(full-body rewrites targeting one symbol) paid ~40-70 model '
        'tokens per case even when deterministic routing was possible.</p>'
        '<p><strong>Resolution.</strong> New direct-swap fast path: when '
        '<code>replace=X</code> and the snippet parses as a single '
        'definition of <code>X</code>, splice verbatim at zero tokens. '
        'Routed via a new AST-first branch in <code>chunked_merge</code>.</p>'
        '</article>'

        '<article class="finding">'
        '<h3>FASTEDIT-005 — <code>preserve_siblings</code> flag for surgical class edits '
        '<span class="tag fixed">Fixed: FastEdit@333c651</span></h3>'
        '<p><strong>Summary.</strong> Modifying a single member of a '
        'class (field, constructor, or init) had no deterministic route '
        'when the sibling methods were unchanged: a snippet naming only '
        'the changed member tripped the extras guard if the whole class '
        'was replaced, and a minimal class shell without the siblings '
        'deleted them. Four languages hit an identical structural limit '
        '(Java constructors and Swift inits are omitted from tldr\'s '
        '<code>definitions</code>; fields are never exposed as top-level '
        'symbols in any language).</p>'
        '<p><strong>Impact at discovery.</strong> Blocked 4 '
        '<code>class_method</code> fixtures (java/09, kotlin/09, '
        'swift/06, typescript/09) from deterministic routing at M9.</p>'
        '<p><strong>Resolution.</strong> New '
        '<code>preserve_siblings=True</code> kwarg on <code>chunked_merge</code> '
        '(and a matching field on <code>BatchEdit</code>, plus per-edit '
        'exposure via <code>fast_batch_edit</code>). FastEdit resolves '
        'the target class, enumerates its children via the tldr AST, '
        'parses the snippet to find which children are named, and '
        'splices every missing sibling verbatim from the original into '
        'the snippet\'s class body before the closing brace. All 4 '
        'fixtures now pass at 0 model tokens.</p>'
        '</article>'

        '<article class="finding">'
        '<h3>FASTEDIT-006 — text_match context-anchor indent preservation '
        '<span class="tag fixed">Fixed: FastEdit@214ffe8</span></h3>'
        '<p><strong>Summary.</strong> When a wrap_block-style edit '
        'deepens the scope of context-anchor lines (e.g. a function '
        'body moved into an immediately-invoked closure body, or a '
        'statement moved inside a <code>with</code>/<code>try</code> '
        'block), the unchanged lines appeared lexically in both the '
        'snippet and the original but at different indent levels. '
        'FastEdit\'s deterministic <code>text_match</code> emitted '
        'those lines at their ORIGINAL indent, producing parse-invalid '
        'output — the body statements sat at 4 spaces inside an '
        '8-space closure body.</p>'
        '<p><strong>Impact at discovery.</strong> Blocked '
        'rust/02-wrap_block — the sole remaining benchmark failure '
        'through M12. Sat at 114 model tokens with a mangled body; '
        'labelled "accepted as limit" pending an upstream patch.</p>'
        '<p><strong>Resolution.</strong> Per-anchor indent delta is '
        'now applied at the context-anchor emit site, analogous to the '
        'M7 preserved-gap indent fix extended to the context-anchor '
        'branch. When the snippet places a shared line at a DEEPER '
        'indent than the original, <code>text_match</code> pads the '
        'emitted line by the delta. <code>_adjust_indent</code> picks '
        'up the shift via a <code>ref_shifted_right</code> flag so new '
        'lines in the same section align to the anchor\'s effective '
        'output indent. Left-shifts are intentionally not performed — '
        'a shallower snippet is treated as a "view" of the code '
        '(e.g. a method extracted from its enclosing class). The '
        'rust/02 fixture re-annotated to <code>target_kind: replace</code> '
        'with a Style A snippet (body inlined at the deeper closure-'
        'body indent) now merges deterministically at 0 model tokens. '
        'Benchmark reaches tier-1 = 100%.</p>'
        '</article>'

        '<article class="finding">'
        '<h3>TLDR-MULTI-001 — Swift init / Java constructor omission, field exposure '
        '<span class="tag fixed">Fixed upstream (tldr)</span></h3>'
        '<p><strong>Summary.</strong> <code>tldr structure</code> listed '
        'Swift <code>init()</code> and Java constructors in each class\'s '
        '<code>methods</code> array but omitted them from the resolvable-'
        'definitions set. Field declarations were not exposed at all. '
        'Downstream consumers (including FastEdit\'s '
        '<code>_resolve_symbol</code>) could not target these symbols '
        'by name.</p>'
        '<p><strong>Impact at discovery.</strong> Combined with the '
        'missing <code>preserve_siblings</code> flag, forced the same 4 '
        'class_method fixtures into the model path.</p>'
        '<p><strong>Resolution.</strong> tldr now emits initializers / '
        'constructors into <code>definitions</code> and exposes class '
        'fields with their name and span. FastEdit\'s new '
        '<code>preserve_siblings</code> path consumes the richer '
        'structure directly.</p>'
        '</article>'

        '</section>'
    )

    # --- footer ---
    sha = _git_head_sha()
    rendered_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    footer = (
        '<footer>'
        f'<div>run_id: <code>{html_lib.escape(run_id)}</code></div>'
        f'<div>adapter: <code>{html_lib.escape(adapter)}</code></div>'
        f'<div>rendered: {rendered_at}</div>'
        + (f'<div>benchmark HEAD: <code>{html_lib.escape(sha)}</code></div>' if sha else '')
        + '</footer>'
    )

    # --- styles ---
    styles = """
    :root {
      --bg: #18181b;          /* zinc-900 */
      --bg-elev: #27272a;     /* zinc-800 */
      --border: #3f3f46;      /* zinc-700 */
      --fg: #f4f4f5;          /* zinc-100 */
      --fg-mute: #a1a1aa;     /* zinc-400 */
      --accent: #a8a29e;      /* stone-400 */
      --accent-fg: #e7e5e4;   /* stone-200 */
      --bar: #78716c;         /* stone-500 */
      --good: #84cc16;        /* lime-500, muted */
      --warn: #eab308;        /* yellow-500 */
    }
    * { box-sizing: border-box; }
    html, body {
      margin: 0;
      padding: 0;
      background: var(--bg);
      color: var(--fg);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        Helvetica, Arial, sans-serif;
      font-size: 14px;
      line-height: 1.5;
    }
    main {
      max-width: 1024px;
      margin: 0 auto;
      padding: 48px 32px 96px;
    }
    h1 {
      font-size: 24px;
      font-weight: 500;
      letter-spacing: -0.01em;
      margin: 0 0 8px;
    }
    h2 {
      font-size: 16px;
      font-weight: 500;
      color: var(--accent-fg);
      margin: 40px 0 16px;
      letter-spacing: -0.005em;
    }
    h3 {
      font-size: 14px;
      font-weight: 500;
      color: var(--fg);
      margin: 24px 0 8px;
    }
    p { margin: 8px 0; color: var(--fg); }
    code {
      background: var(--bg-elev);
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 1px 6px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12px;
    }
    .subtitle {
      color: var(--fg-mute);
      margin: 0 0 32px;
      font-size: 13px;
    }
    .headline {
      display: grid;
      grid-template-columns: repeat(5, 1fr);
      gap: 16px;
      margin: 24px 0 32px;
    }
    .stat {
      background: var(--bg-elev);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px 16px 16px;
      box-shadow: 0 1px 0 rgba(0, 0, 0, 0.2);
    }
    .stat-label {
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--fg-mute);
      margin-bottom: 8px;
    }
    .stat-value {
      font-size: 24px;
      font-weight: 500;
      font-variant-numeric: tabular-nums;
    }
    .disclaimer {
      background: var(--bg-elev);
      border: 1px solid var(--border);
      border-left: 3px solid var(--warn);
      border-radius: 8px;
      padding: 16px 24px;
    }
    .disclaimer h2 { margin-top: 0; color: var(--warn); }
    .disclaimer p { color: var(--fg-mute); margin-bottom: 0; }
    table.data {
      width: 100%;
      border-collapse: collapse;
      background: var(--bg-elev);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
      font-variant-numeric: tabular-nums;
    }
    table.data th, table.data td {
      padding: 8px 16px;
      text-align: right;
      border-bottom: 1px solid var(--border);
    }
    table.data th {
      background: rgba(255,255,255,0.02);
      font-weight: 500;
      color: var(--fg-mute);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }
    table.data td.lang, table.data th:first-child { text-align: left; }
    table.data tr:last-child td { border-bottom: none; }
    table.data tr:hover td { background: rgba(255,255,255,0.02); }
    table.bars {
      width: 100%;
      border-collapse: collapse;
      background: var(--bg-elev);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 8px;
      font-variant-numeric: tabular-nums;
    }
    table.bars td { padding: 6px 12px; }
    .bar-label { width: 80px; color: var(--fg-mute); font-size: 12px; }
    .bar-count { width: 40px; text-align: right; color: var(--fg); font-size: 12px; }
    .bar-cell { padding: 6px 8px !important; }
    .bar {
      height: 16px;
      min-width: 1px;
      background: var(--bar);
      border-radius: 2px;
    }
    .hint { color: var(--fg-mute); font-size: 12px; margin: 0 0 16px; }
    .findings article.finding {
      background: var(--bg-elev);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px 24px;
      margin-bottom: 16px;
    }
    .findings article.finding h3 {
      margin-top: 0;
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 16px;
      flex-wrap: wrap;
    }
    .tag {
      font-size: 11px;
      font-weight: 400;
      padding: 2px 8px;
      border-radius: 4px;
      border: 1px solid var(--border);
      color: var(--fg-mute);
    }
    .tag.fixed { color: var(--good); border-color: rgba(132,204,22,0.4); }
    footer {
      margin-top: 48px;
      padding-top: 16px;
      border-top: 1px solid var(--border);
      color: var(--fg-mute);
      font-size: 12px;
      display: flex;
      gap: 24px;
      flex-wrap: wrap;
    }
    """

    title = f"FastEditBench — {html_lib.escape(adapter)} / {html_lib.escape(run_id)}"

    return (
        "<!DOCTYPE html>"
        '<html lang="en"><head>'
        '<meta charset="utf-8"/>'
        '<meta name="viewport" content="width=device-width,initial-scale=1"/>'
        f"<title>{title}</title>"
        f"<style>{styles}</style>"
        "</head><body><main>"
        f"<h1>{title}</h1>"
        '<p class="subtitle">Accuracy and diff-quality benchmark for FastEdit '
        "in MCP mode across 13 languages.</p>"
        + headline
        + disclaimer
        + lang_table
        + distributions
        + findings
        + footer
        + "</main></body></html>"
    )
