# fastedit-bench-02 — Token-floor comparator

## Goal

Answer: **on the same 156 fixtures where FastEdit hits 100% / 1,977 tokens, what is the theoretical minimum output tokens that frontier LLMs (Opus 4.7, GPT-5.4) would emit to produce the same edits via a native `edit` tool call?**

Scope narrowed 2026-04-22 from the original 4-model real-API matrix to a 2-model tokenization-only floor. Rationale in `contract.json#scope_note`.

## Baseline to beat (or fail to beat)

From fastedit-bench-01 final run `results/fastedit/20260422-181127`:
- Tier-1: **100.0%** (156/156)
- Tier-2: **100.0%**
- Errors: **0**
- Total model tokens: **1,977**
- Zero-token (pure AST) cases: **120 / 156** (77%)
- Mean latency (estimated): ~50ms per case (local MLX inference)

## Comparators

2 frontier models, measured by native tokenizer only:

| OpenRouter ID | Short | Tokenizer | Mode |
|---|---|---|---|
| `anthropic/claude-opus-4.7` | opus | `anthropic.count_tokens` (free API call) | network, 0 cost |
| `openai/gpt-5.4` | gpt | `tiktoken.get_encoding('o200k_base')` | offline |

Dropped: `moonshotai/kimi-k2.6`, `z-ai/glm-5.1` — no easily-accessible offline tokenizer. Possible follow-up if the floor comparison motivates it.

## Measurement

For each (fixture, model) pair:

```python
payload = json.dumps({
  "name": "edit",
  "arguments": {
    "file_path": fixture.path,
    "old_string": fixture.old_snippet,  # derived from original
    "new_string": fixture.new_snippet,  # derived from expected
  }
}, separators=(",", ":"))

edit_payload_tokens = tokenizer.count(payload) + wrapper_overhead_constant
```

Where `wrapper_overhead_constant` is measured once per model at adapter init (tokens of empty `{"name":"edit","arguments":{"file_path":"","old_string":"","new_string":""}}` minus the tokens the arguments themselves contribute).

### What this floor represents

The most charitable interpretation of output cost for a frontier model doing this edit:
- Zero chain-of-thought
- Zero preamble ("Sure, I'll edit that file...")
- Zero retries
- Exactly one tool call per fixture
- Perfect payload on the first try

Real-world output would be ≥ this floor. Typical inflation factor from published tool-use benchmarks: 1.1–1.5× on Anthropic/OpenAI. Not measured here.

### What this floor does NOT represent

- Accuracy (can the model actually produce this edit?)
- Latency
- Input token cost (the whole file is in the prompt in a real run)
- Retry cost, CoT cost
- Cost in USD

### Extracting `old_string` / `new_string` from fixtures

Fixtures carry `original` and `expected` full-file content, plus a `snippet` that a real edit tool would match. For the floor, we need the minimal SEARCH/REPLACE pair that would transform `original` into `expected`.

Approach:
1. Diff `original` vs `expected` → find changed region(s).
2. Extract a minimal context-preserving `old_string` (the exact source region) and `new_string` (the replacement).
3. If the fixture has multiple hunks, emit one tool-call payload per hunk and sum.

This logic lives in `fixture_payload_extractor.py` and is tested against hand-annotated pairs.

## Milestones

| M | Name | Scope |
|---|---|---|
| M0 | design_approval | ✅ done — user approved 2026-04-22 |
| M1 | infra | ✅ done (now dormant) — OpenRouter client, prompt templates, smoke/pilot kept for future |
| M2 | tokenizer_adapter | `TokenizerFloorAdapter` with 2 backends; fixture→payload extractor; tests |
| M3 | floor_run | 156 × 2 = 312 computations; produces per-model JSONL + summaries |
| M4 | report | `/compare` page; side-by-side table; final approval |

## Schema extensions to `CaseRow` (delta from M1)

New fields (nullable for backward-compat):
- `edit_payload_tokens: int`
- `tokenizer_used: str`
- `tokenizer_proxy: bool` (only true if Opus fell back to tiktoken cl100k_base due to missing ANTHROPIC_API_KEY)

Existing M1 fields reused:
- `input_tokens` — set to 0 in floor-mode (not measured)
- `output_tokens` — set to `edit_payload_tokens` in floor-mode
- `latency_ms` — null in floor-mode
- `tool_call_count` — 1 in floor-mode (always single payload per fixture per model; multi-hunk fixtures sum-before-report)
- `retry_count` — 0 in floor-mode

## Expected headline outcome

| Model | Total payload tokens (floor) | Ratio vs FastEdit 1,977 |
|---|---|---|
| FastEdit (baseline) | 1,977 | 1× |
| Opus 4.7 (floor) | TBD (est 15–40k) | ~8–20× |
| GPT-5.4 (floor) | TBD (est 15–40k) | ~8–20× |

Estimates assume average `old_string + new_string` of ~400 bytes ≈ 100 tokens per fixture, × 156 fixtures ≈ 15,600 tokens per model. Wide range because multi-line function replacements will dominate.

## Cost envelope

$0. Opus uses `messages.count_tokens` (free endpoint). GPT uses offline tiktoken.

## Dormant infra (retained, not used)

Built during M1 before the scope narrow. Kept in repo:

- `src/fasteditbench/backends/openrouter_client.py`
- `src/fasteditbench/prompts/templates.py`
- `scripts/smoke_test_models.py`, `scripts/template_pilot.py`
- `tests/test_openrouter_client.py`, `tests/test_prompts.py`

If a follow-up task wants real-edit measurement (capability + retry + inflation factor), this infra is ready to re-activate.

## Risks

- Fixture payload extraction ambiguity: a fixture with multiple non-adjacent edits could be expressed as 1, 2, or N tool-calls. We pick the minimal-hunks interpretation and document it.
- `anthropic.count_tokens` may rate-limit on 156 sequential calls. Mitigation: async with concurrency cap, retry on 429.
- Tiktoken o200k_base is the encoding published for GPT-4o family; GPT-5.4 may use a different encoding. Mitigation: document the encoding used; re-run trivially if a better one is later published.
- The floor understates LLM cost in reality. Disclosed in the report (E-004).

## Non-goals

- No accuracy measurement (the floor assumes success)
- No latency measurement
- No USD cost measurement (floor has no API spend)
- No new fixtures
- No Kimi/GLM (dropped from scope)
- No template A/B work (not applicable in floor-mode)

## v3 addendum — calling-agent tax framing (2026-04-23)

The v2 comparator produced a misleading 0.992x snippet-vs-native ratio
because (a) M3's native-edit payload used straw-man `difflib` pairs with
`old_string` as tight as 1 line and (b) the snippet side included
whole-function rewrites where neither tool has a structural advantage.

v3 reframes the headline as the **calling-agent tax** — tokens the agent
emits on top of the new code it writes anyway:

- Native tax = `old_string` (anchor) + JSON wrapper.
- FastEdit tax = signature anchor + `# ... existing code ...` marker, or
  nothing (for `after=` / `fast_delete`).

Fixtures are categorized per operation: `modify_inside` (replace +
marker), `whole_rewrite` (replace, no marker — **excluded from
headline**), `insert_after`, `delete`, `inline_edit`, `batch_edit`.
Native-edit side uses realistic ±2-line anchor context.

| Category       | n  | Savings% |
|----------------|---:|---------:|
| delete         |  3 |   79.8%  |
| inline_edit    | 21 |   37.8%  |
| batch_edit     | 10 |   31.9%  |
| modify_inside  | 34 |   29.9%  |
| whole_rewrite  | 88 |   31.9%  (excluded from headline)   |

**Headline (excl. whole_rewrite):** 68 fixtures, native 6,882 tok,
FastEdit 4,465 tok, **35.1% savings**, 1.541× ratio.

Pipeline:
- `scripts/compute_tax_per_operation.py` — computes per-fixture rows.
- `tests/test_tax_per_operation.py` — 15 tests covering categorization,
  ±2-line anchor context, and per-category payload minimality.
- `results/tax_per_operation/{run_id}.*` — JSONL + summary outputs.
- `results/compare/bench-02-report.html` — v3 report page (rewritten).

The v2 comparator scripts, data, and the `openrouter_client` / prompt
template / smoke-test infra remain in the repo. v3 supersedes the
report page but does not delete v2 data.
