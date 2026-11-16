# fasteditBench Design — Accuracy + Diff-Quality Benchmark

**Task ID:** fastedit-bench-01
**Milestone:** M0 (design_approval)
**Assertion gated by this doc:** VAL-000
**Project dir:** `/Users/cosimo/Desktop/ContinuousClaudeV4/fasteditBench/`
**Source-of-truth fixtures repo:** `/Users/cosimo/Desktop/FastEdit/`

This document is the spec that downstream milestones (M1–M4) read from. If
anything here is wrong or incomplete, fix it here BEFORE implementation starts.

---

## 1. Purpose & Scope

**Mission.** Measure how well FastEdit applies compact code-edit snippets to
source files. Two dimensions, one subject under test.

- **Dimension A — Accuracy.** Does the adapter produce the expected edited
  code? Measured via two tiers (§5):
  - Tier-1: exact-match after trailing-whitespace normalization.
  - Tier-2: AST-equivalence via tree-sitter (Tier-1 failures re-checked for
    semantic equivalence with different formatting).
- **Dimension B — Diff Quality.** Even when the output is correct, is the
  edit _minimal_? Did the adapter over-edit, or drop surrounding context
  lines it was supposed to preserve? Four metrics (§6): `hunk_count`,
  `loc_required`, `loc_changed`, `minimality_ratio`, `context_lines_preserved`.

**Subject under test (this task).** Only the `FastEditAdapter`. All
comparator adapters (Search/Replace, Unified Diff, Claude Code, Codex) are
explicitly deferred to `fastedit-bench-02`.

**Languages (13).** python, rust, go, typescript, java, kotlin, csharp, c,
cpp, ruby, swift, php, elixir. ≥10 cases per language (§2, VAL-100).
Elixir is a new addition — `tree-sitter-elixir` must be added to
`fasteditBench` deps at M2 (not present in FastEdit's `pyproject.toml`).

**Explicitly out of scope.**
- Speed, latency, throughput, tokens-per-second, TTFT. Dropped from the
  final scope — the benchmark does NOT record wall-clock timings.
- Tier-3 behavioral testing (running the edited code through a test harness).
  Tier-1 + Tier-2 only.
- Comparator adapters (SR / UD / Claude Code / Codex) — deferred to
  `fastedit-bench-02`. The `EditAdapter` Protocol (§4) is designed to
  accommodate them later without rework.
- MCP stdio transport for FastEdit — VAL-300 requires the adapter to use
  the direct Python API (`fastedit.inference.chunked_merge` /
  `deterministic_edit`) to avoid transport overhead and surface raw
  `tokens_generated`.

---

## 2. Fixture Case Schema

Each case is a single YAML file at:

```
fasteditBench/fixtures/{lang}/{case_id}.yaml
```

where `{lang}` is one of the 13 language directory names and `{case_id}`
is `{pattern_slug}_{short_descriptor}` (e.g. `add_guard_empty_list`).

### Fields

| Field             | Type    | Required | Description |
|-------------------|---------|----------|-------------|
| `name`            | string  | yes      | Human-readable case name. Unique within its language. |
| `pattern`         | string  | yes      | One of the 22 pattern slugs in §3. Enforced by `_validate.py`. |
| `language`        | string  | yes      | One of the 13 language slugs. Must match the parent directory. |
| `original_code`   | string (block) | yes | The source code BEFORE the edit. Must parse cleanly under `language`'s tree-sitter grammar. |
| `snippet`         | string (block) | yes | The compact edit snippet (the input an agent would send — context + new lines + optional `... existing code ...` markers). |
| `expected_output` | string (block) | yes | The source code AFTER the edit is applied. Must parse cleanly under `language`'s tree-sitter grammar (VAL-101). |
| `authoring_notes` | string  | no       | Free-form notes: why this case is here, edge cases, what a broken adapter would fail on. Not scored. |

All code-bearing fields use YAML block scalars (`|` literal style) to
preserve whitespace and newlines exactly.

### Worked example — Python (`pattern: add_guard`)

File: `fasteditBench/fixtures/python/add_guard_empty_list.yaml`

```yaml
name: add_guard_empty_list
pattern: add_guard
language: python
original_code: |
  def average(nums):
      total = sum(nums)
      return total / len(nums)
snippet: |
  def average(nums):
      if not nums:
          return 0.0
      total = sum(nums)
      # ... existing code ...
expected_output: |
  def average(nums):
      if not nums:
          return 0.0
      total = sum(nums)
      return total / len(nums)
authoring_notes: |
  Classic guard-clause insertion. The snippet uses the `# ... existing code ...`
  marker to signal preservation of the tail. A correct adapter keeps
  `return total / len(nums)` untouched.
```

### Worked example — Rust (`pattern: add_guard`)

File: `fasteditBench/fixtures/rust/add_guard_zero_div.yaml`

```yaml
name: add_guard_zero_div
pattern: add_guard
language: rust
original_code: |
  fn divide(a: f64, b: f64) -> f64 {
      a / b
  }
snippet: |
  fn divide(a: f64, b: f64) -> f64 {
      if b == 0.0 {
          return f64::NAN;
      }
      a / b
  }
expected_output: |
  fn divide(a: f64, b: f64) -> f64 {
      if b == 0.0 {
          return f64::NAN;
      }
      a / b
  }
authoring_notes: |
  Rust equivalent of p1_js_guard from FastEdit's test corpus. No marker used
  — the snippet shows the full intended function body. Tests the adapter's
  ability to recognize no-marker-needed cases.
```

The schema is language-agnostic: only the `language`, `original_code`, and
`expected_output` change across languages.

---

## 3. Pattern Taxonomy

FastEdit's existing benchmark (`tests/test_deterministic_benchmark.py` at
source-fixtures repo, docstring lines 8–29; case collections P1_CASES
through P22_CASES) defines **exactly 22 patterns**, verified by reading the
file. `fasteditBench` adopts this taxonomy verbatim and documents it in
`fasteditBench/fixtures/PATTERNS.md` (M1 artifact, VAL-102).

| Slug                | Pattern name        | One-sentence definition | Minimal example (pseudo) |
|---------------------|---------------------|-------------------------|--------------------------|
| `add_guard`         | Add guard clause    | Insert an early return / precondition check before existing function body. | `if not x: return 0` before old first line |
| `add_line`          | Add single line     | Insert exactly one new statement between two existing context anchors. | `logger.info(...)` between two surviving lines |
| `change_line`       | Change one line     | Replace exactly one line; surrounding context unchanged. | `timeout=5` → `timeout=30` |
| `wrap_block`        | Wrap block          | Surround existing code with `try/except`, `if`, `with`, etc. via marker. | `try: # ... existing code ... except: ...` |
| `add_at_end`        | Append at end       | Insert new lines after the last pre-existing line of the symbol (marker-guided). | add `logger.info(...)` before final `return` |
| `replace_block`     | Replace a block     | Drop the lines between two anchors and splice new lines in their place. | replace a for-loop with a comprehension |
| `change_signature`  | Change signature    | Modify the function def/signature line; keep body via marker. | `def f(x)` → `def f(x, y=0)` |
| `multi_insert`      | Multi-line insert   | Insert ≥2 new consecutive lines between two anchors. | add 3 middleware setup lines |
| `class_method`      | Class method edit   | Modify one method inside a class; other methods preserved via markers. | change `__init__`, keep `get`/`set` via markers |
| `two_markers`       | Two-marker edit     | Change a middle section; preserve top AND bottom via two separate `... existing code ...` markers. | modify middle 3 steps of an ETL pipeline |
| `delete_lines`      | Delete lines        | Remove lines between anchors (no replacement). | drop `logger.debug(...)` calls |
| `full_replace`      | Full body replace   | Replace entire function body; only signature preserved. | rewrite a loop as `return max(items)` |
| `add_branch`        | Add branch          | Add an `elif` / `else` / `case` to an existing conditional chain. | add `elif score >= 70: return "C"` |
| `extend_literal`    | Extend literal      | Add items to a list / dict / set / tuple literal. | add `".rs", ".go"` to extensions list |
| `add_decorator`     | Add decorator       | Prepend one or more decorators above a function def. | prepend `@lru_cache(maxsize=128)` |
| `rename_variable`   | Rename variable     | Rename a local variable throughout a function body. | `tmp` → `result` everywhere inside `f` |
| `reorder_statements`| Reorder statements  | Swap order of existing statements (no new code). | swap two `connect_*()` calls |
| `extract_variable`  | Extract variable    | Pull a sub-expression into a named local variable. | extract `user.role == "admin"` → `is_admin` |
| `inline_variable`   | Inline variable     | Remove an assignment and inline its value at the usage site. | drop `full_name = ...`; use expression inline |
| `change_expression` | Change expression   | Modify part of a single expression (operator, arg, string). | `requests.get(url)` → `requests.get(url, timeout=30)` |
| `add_parameter`     | Add parameter       | Add a parameter to the signature AND update the body to use it. | `def run_task(name)` → `def run_task(name, verbose=False)` + body ref |
| `remove_parameter`  | Remove parameter    | Remove a parameter from the signature AND remove its usages. | drop `debug=False` param and `if debug: ...` branches |

**Source citation.** `/Users/cosimo/Desktop/FastEdit/tests/test_deterministic_benchmark.py`, patterns P1–P22, docstring lines 8–29, case blocks lines 53–1645.

**Enforcement.** M1 workers must choose `pattern` values from this exact
slug list. `fasteditBench/fixtures/_validate.py` (VAL-102) fails the run if
any case uses a pattern outside this set.

---

## 4. EditAdapter Protocol

The harness is adapter-agnostic. Every adapter under test implements the
same Protocol. This task ships only `FastEditAdapter`; the Protocol is
already shaped for `fastedit-bench-02` comparator adapters.

File: `fasteditBench/src/fasteditbench/adapter.py`

```python
"""EditAdapter protocol — the only surface the harness calls into."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class AdapterResult:
    """The result of applying one edit.

    No timing fields — speed is explicitly out of scope for this benchmark.
    """
    output_text: str
    """The adapter's final edited source code. Must be a complete file /
    symbol body, NOT a diff."""

    raw_response: Any
    """Provider-specific raw payload (dict, SDK object, etc.). Stored for
    debugging and for re-analysis without re-running the adapter. Must be
    JSON-serializable OR explicitly `None`."""

    tokens_generated: int | None
    """Number of output tokens the adapter's underlying model generated.
    `None` when the adapter is deterministic / doesn't invoke a model
    (e.g. NullAdapter, deterministic_edit fast path)."""

    error: str | None
    """Error message if the adapter failed. `None` on success.
    If `error` is set, `output_text` SHOULD be `""`."""


@runtime_checkable
class EditAdapter(Protocol):
    """Every benchmark adapter implements this.

    `apply` is async so the harness can parallelize in the future without
    adapter-level changes. The FastEditAdapter wraps its sync Python API
    call in `asyncio.to_thread`.
    """

    name: str
    """Short identifier used in results paths, e.g. 'fastedit', 'null'."""

    async def apply(
        self,
        original_code: str,
        snippet: str,
        language: str,
        context: dict[str, Any],
    ) -> AdapterResult:
        """Apply the edit snippet to original_code.

        Args:
            original_code: The full source BEFORE the edit.
            snippet: The edit snippet (context + new lines + markers).
            language: One of the 13 language slugs from §1.
            context: Extra per-case hints (e.g. {'case_id': '...',
                'pattern': 'add_guard', 'file_path': '/tmp/...'}). Adapters
                may ignore or use as needed.

        Returns:
            AdapterResult — never raises. All failures surface via
            AdapterResult.error so the harness can continue the run and
            write one row per case.
        """
        ...
```

### Reference fake: `NullAdapter`

Used for unit tests (VAL-203 smoke run) and to prove the Protocol shape.

```python
class NullAdapter:
    name = "null"

    async def apply(self, original_code, snippet, language, context):
        return AdapterResult(
            output_text=original_code,   # identity — always "passes compile"
            raw_response=None,
            tokens_generated=None,
            error=None,
        )
```

### Note on `MergeResult` vs `AdapterResult`

FastEdit's internal `MergeResult` (at
`src/fastedit/inference/merge.py` L18–27) has six fields:
`merged_code`, `parse_valid`, `tokens_generated`, `latency_ms`,
`tokens_per_second`, `ttft_ms`. Of these, **only** `merged_code` (→
`output_text`) and `tokens_generated` carry over to `AdapterResult`.
The three timing fields are deliberately dropped per §1; `parse_valid`
is re-computed by the harness' Tier-2 checker, not trusted from the
adapter.

---

## 5. Accuracy Tier Algorithms

Both tiers run on every case. Tier-2 only matters when Tier-1 fails;
passing Tier-1 implies passing Tier-2.

### Tier-1: exact_match_normalized

Matches FastEdit's existing `_n()` convention at
`tests/test_deterministic_benchmark.py:39–40`:

```python
def _n(code: str) -> str:
    return "\n".join(line.rstrip() for line in code.splitlines()).strip()
```

**Algorithm.**
1. Split `adapter_output` and `expected_output` on line boundaries.
2. For each line, strip trailing whitespace (`line.rstrip()`).
3. Rejoin with `"\n"`, then `.strip()` the full result (drops leading/trailing blank lines).
4. Return `normalized(adapter_output) == normalized(expected_output)`.

**What this preserves.** Indentation, non-trailing whitespace, blank-line
placement inside the body.

**What this forgives.** Trailing whitespace on any line. Leading/trailing
blank lines around the whole snippet.

### Tier-2: ast_equivalence (tree-sitter)

Only evaluated if Tier-1 fails — otherwise Tier-2 = pass by definition.

**Algorithm.**
1. Parse both `adapter_output` and `expected_output` with the tree-sitter
   grammar for `language`. If either fails to parse, Tier-2 = fail.
2. Walk each tree depth-first; drop nodes whose type is a comment node
   (e.g. `comment`, `line_comment`, `block_comment`, grammar-dependent)
   and nodes that are pure whitespace / newline tokens.
3. Serialize each stripped tree to its S-expression form
   (`node.sexp()` or `str(node)` depending on binding).
4. Compare the two S-expression strings with the same trailing-whitespace
   normalization from Tier-1. Tier-2 pass iff they are equal.

**Why this and not structural diffing.** Serialized S-expr comparison
is the smallest thing that catches "same code, different formatting"
(extra blank line, different comment, trailing semicolon stripped)
without re-implementing parser-specific equivalence rules.

**tree-sitter-elixir dependency.** Must be added to
`fasteditBench/pyproject.toml` at M2. FastEdit's upstream
`pyproject.toml` does not ship it. Other 12 languages already covered by
`tree-sitter-language-pack` or FastEdit's current deps.

---

## 6. Diff Quality Formulas

**Copied verbatim from `contract.json.diff_quality_formula` — do not reword.**

- `loc_required` = `num_lines(unified_diff(original_code, expected_output)) with context=0`
- `loc_changed`  = `num_lines(unified_diff(original_code, adapter_output))  with context=0`
- `minimality_ratio` = `loc_changed / max(loc_required, 1)`
- `hunk_count` = `number of @@ hunks in unified_diff(original_code, adapter_output)`
- `context_lines_preserved` = `1 - (lines_unchanged_in_original removed from adapter_output) / total_unchanged_lines`

**Interpretation cheatsheet.**
- `minimality_ratio = 1.0` → adapter changed exactly as many lines as the
  gold diff. Ideal.
- `minimality_ratio > 1.0` → over-editing. Adapter touched lines it should
  have left alone.
- `minimality_ratio < 1.0` → under-editing. Adapter missed lines that
  needed changes (usually correlates with Tier-1/Tier-2 failure).
- `hunk_count` tracks edit locality — a 1-line conceptual change that
  produces 3 hunks suggests the adapter is shuffling unrelated regions.
- `context_lines_preserved = 1.0` → no surrounding unchanged lines were
  dropped. Anything below 1.0 means the adapter deleted lines it was
  supposed to keep (worst-case failure mode for marker-based snippets).

### Worked example

Original (`original_code`, 3 lines):

```
def f(x):
    y = x + 1
    return y
```

Gold edit (`expected_output`, changes one line):

```
def f(x):
    y = x * 2
    return y
```

`unified_diff(original, expected, context=0)` → 4 lines total (1 `@@`
header + 1 removed + 1 added = body; the hunk header is the 4th).
So `loc_required ≈ 4` (the exact constant depends on whether the
implementation counts the `---`/`+++` file header; the M2 worker picks
ONE convention and applies it uniformly to both numerator and denominator
so the ratio is well-defined).

- **Adapter A** outputs exactly the gold: `loc_changed = 4`.
  `minimality_ratio = 4 / max(4, 1) = 1.0`. Hunk count = 1.
  `context_lines_preserved = 1.0`. Ideal.
- **Adapter B** correctly changes the middle line but ALSO rewrites
  `return y` to `return y  # computed`. Diff now has 2 removed + 2 added
  in one hunk. `loc_changed = 6`. `minimality_ratio = 6 / 4 = 1.5`.
  Hunk count = 1. Tier-1 fails (comment added); Tier-2 might pass since
  comments are stripped — but diff quality flags over-editing regardless.
- **Adapter C** wipes the whole body and writes a completely different
  2-line impl. Diff has 2 removed + 2 added. `loc_changed = 6`.
  `minimality_ratio = 6 / 4 = 1.5`. Hunk count = 1. Accuracy = fail.

---

## 7. JSONL Result Schema

### Per-case row (`results/{adapter}/{run_id}.jsonl` — one JSON object per line)

| Field                      | Type              | Description |
|----------------------------|-------------------|-------------|
| `run_id`                   | string            | UTC-ISO-ish id, e.g. `20260422T1430Z-a7b2`. Same across all rows of a single run. |
| `adapter`                  | string            | Adapter `name` from §4 (e.g. `"fastedit"`, `"null"`). |
| `case_id`                  | string            | Fixture file stem (e.g. `add_guard_empty_list`). |
| `language`                 | string            | One of the 13 language slugs. |
| `pattern`                  | string            | One of the 22 pattern slugs from §3. |
| `tier1_pass`               | bool              | Exact-match after trailing-WS strip (§5). |
| `tier2_pass`               | bool              | AST equivalence (§5). Always true if `tier1_pass`. |
| `hunk_count`               | int               | §6. |
| `loc_required`             | int               | §6. From gold diff. |
| `loc_changed`              | int               | §6. From adapter diff. |
| `minimality_ratio`         | float             | §6. |
| `context_lines_preserved`  | float             | §6. In `[0, 1]`. |
| `tokens_generated`         | int \| null       | From `AdapterResult.tokens_generated`. Null for deterministic adapters. |
| `error`                    | string \| null    | From `AdapterResult.error`. Null on success. |
| `timestamp`                | string (ISO 8601, UTC, `Z` suffix) | Per-row timestamp (when the case completed). |

No `latency_ms` / `ttft_ms` / `tokens_per_second` — speed is out of scope.

### Summary JSON (`results/{adapter}/{run_id}.summary.json`)

Aggregate written by the runner at the end of a full run (VAL-301).

```json
{
  "run_id": "20260422T1430Z-a7b2",
  "adapter": "fastedit",
  "timestamp_start": "2026-04-22T14:30:11Z",
  "timestamp_end":   "2026-04-22T14:42:03Z",
  "total_cases": 137,
  "overall": {
    "tier1_pass_rate": 0.89,
    "tier2_pass_rate": 0.94,
    "mean_minimality_ratio": 1.08,
    "median_minimality_ratio": 1.00,
    "mean_hunk_count": 1.2,
    "mean_context_lines_preserved": 0.98,
    "total_tokens_generated": 48213,
    "error_count": 2
  },
  "per_language": {
    "python":  { "cases": 12, "tier1_pass_rate": 1.00, "tier2_pass_rate": 1.00, "mean_minimality_ratio": 1.00, "median_minimality_ratio": 1.00, "mean_hunk_count": 1.1, "mean_context_lines_preserved": 1.00, "total_tokens_generated": 3140, "error_count": 0 },
    "rust":    { "cases": 10, "tier1_pass_rate": 0.80, "tier2_pass_rate": 0.90, "mean_minimality_ratio": 1.15, "median_minimality_ratio": 1.00, "mean_hunk_count": 1.3, "mean_context_lines_preserved": 0.97, "total_tokens_generated": 4210, "error_count": 1 }
  }
}
```

All 13 languages appear in `per_language` (keys present even if some
languages have all-pass or all-fail).

---

## 8. Report Layout

FastAPI app at `fasteditBench/src/fasteditbench/report_app.py`.

### Endpoints

- **`GET /results?adapter=<name>&run_id=<id>`** → JSON
  Returns the contents of the most recent `{adapter}/{run_id}.summary.json`
  (or the specified `run_id`). 404 if no runs exist for that adapter.
  Protocol-ready: `adapter` is a query param even though only `fastedit` is
  populated this task. Accepts `adapter=fastedit` (and in the future `sr`,
  `ud`, `claude_code`, `codex`).
- **`GET /report?adapter=<name>&run_id=<id>`** → HTML
  Static-rendered HTML page. Columns + histogram described below.

### Accuracy table (rendered on `/report`)

Columns, in order:

| Column            | Source                              |
|-------------------|-------------------------------------|
| Language          | `per_language` key                  |
| Cases             | `per_language[lang].cases`          |
| Tier-1 pass rate  | `per_language[lang].tier1_pass_rate` rendered as `%` |
| Tier-2 pass rate  | `per_language[lang].tier2_pass_rate` rendered as `%` |
| Mean minimality   | `per_language[lang].mean_minimality_ratio` to 2 dp |
| Mean hunks        | `per_language[lang].mean_hunk_count` to 1 dp |
| Ctx preserved     | `per_language[lang].mean_context_lines_preserved` as `%` |
| Errors            | `per_language[lang].error_count`    |

Mock rendering (13 language rows + 1 total row):

```
| Language   | Cases | Tier-1 | Tier-2 | Min.ratio | Hunks | Ctx | Err |
|------------|------:|-------:|-------:|----------:|------:|----:|----:|
| python     |    12 |   100% |   100% |      1.00 |   1.1 |100% |   0 |
| rust       |    10 |    80% |    90% |      1.15 |   1.3 | 97% |   1 |
| go         |    10 |    90% |    90% |      1.00 |   1.0 |100% |   0 |
| typescript |    11 |    91% |    91% |      1.05 |   1.2 | 99% |   0 |
| ...        |   ... |   ...  |   ...  |       ... |   ... | ... | ... |
| ALL        |   137 |    89% |    94% |      1.08 |   1.2 | 98% |   2 |
```

### Diff-quality section

Two visualizations, both server-rendered inline (SVG or pre-binned histogram
rendered via static HTML bars — no JS charting dep required):

1. **Minimality ratio histogram** — x-axis bucketed by ratio:
   `[0, 0.5)`, `[0.5, 0.9)`, `[0.9, 1.1]`, `(1.1, 1.5]`, `(1.5, 2]`, `> 2`.
   y-axis = case count. One bar per bucket, labeled.
2. **Hunk-count distribution** — x-axis = integer hunk count `1..6+`;
   y-axis = case count.

### Methodology section

The full text of §9 below, rendered verbatim, placed ABOVE the accuracy
table so readers see it before the numbers.

---

## 9. Methodology Disclaimer

**The following text MUST appear verbatim on the HTML report (§8) at M4 to
satisfy VAL-402 and premortem risk E-001 (in-distribution bias).**

> **Methodology.** Benchmark fixtures are drawn from the 22 edit patterns
> FastEdit is designed to handle (`add_guard`, `wrap_block`, `replace_fn`,
> etc.). Results characterize performance on this in-distribution task;
> performance on arbitrary out-of-distribution edits may differ. Fixture
> coverage: 13 languages × ≥10 cases each.

Placement: top of the HTML report, above the accuracy table, in a visually
distinct panel (bordered/shaded) — readers must not be able to read the
headline accuracy numbers without first seeing this disclaimer.

---

## 10. Milestone → Design Section Map

Downstream workers should cite the sections they implement against:

| Milestone | Worker reads | What the worker builds |
|-----------|--------------|------------------------|
| M1 fixtures (VAL-100, 101, 102) | §2 (schema), §3 (taxonomy) | `fasteditBench/fixtures/{lang}/*.yaml`; `PATTERNS.md`; `_validate.py` |
| M2 harness_core (VAL-200, 201, 202, 203) | §4 (Protocol), §5 (tier checkers), §6 (diff metrics), §7 (JSONL schema) | `src/fasteditbench/adapter.py`; tier checkers; diff metrics module; `fasteditbench run` CLI |
| M3 fastedit_adapter (VAL-299, 300, 301) | §4 (Protocol contract) | `FastEditAdapter` wiring `fastedit.inference.chunked_merge` + `deterministic_edit`; VAL-299 health gate `GET {api_base}/v1/models` BEFORE VAL-301 run |
| M4 report (VAL-400, 401, 402) | §7 (result + summary schemas), §8 (endpoints + layout), §9 (disclaimer verbatim) | FastAPI `GET /results`, `GET /report` with accuracy table + diff histograms + methodology panel |

---

## 11. Open Questions

**Q1 — `num_lines(unified_diff(...))` exact constant.**
The §6 formulas reference `num_lines(unified_diff(original, other, context=0))`.
Python's `difflib.unified_diff` emits 2 header lines (`---`/`+++`) plus
per-hunk `@@` headers plus `+`/`-` body lines. As long as the M2 worker
uses the SAME counting convention for both `loc_required` and `loc_changed`
(they appear symmetrically in the ratio), the ratio is well-defined and
meaningful regardless of whether the 2 file-header lines are included or
excluded. M2 MUST document the chosen convention in the diff-metrics
module docstring and MUST ensure VAL-202's synthetic-diff unit tests lock
it in. Recommended: exclude the 2 file-header lines and count `@@` + `+` +
`-` lines only — but either choice is acceptable if applied uniformly.

**Q2 — Tier-2 comment-node type names per language.**
Tree-sitter grammars use slightly different type names for comments
(`comment`, `line_comment`, `block_comment`, `documentation_comment`,
etc.). M2 worker must either (a) use a language→comment-types map, or
(b) strip all nodes whose type contains the substring `"comment"`. Option
(b) is simpler and almost certainly adequate for the 13 languages in
scope; M2 worker to choose and justify in the module docstring.

**Q3 — Elixir fixture authoring.**
tree-sitter-elixir is not in FastEdit's existing dep set. M2 adds the
dep. M1 fixture authors for Elixir must verify `expected_output` parses
under the version pinned by M2 before writing fixtures — otherwise VAL-101
fails at fixture-validation time. If blocked, Elixir fixture authoring can
be last among the 13 languages; it does not block other-language work.

No other gaps identified at design time.
