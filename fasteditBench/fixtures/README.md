# Fixtures

Gold-output fixture cases for `fastedit-bench-01`.

Each `*.yaml` file under a language subdirectory is one benchmark case
(original code + edit snippet + expected output), drawn from the 22-pattern
taxonomy in [`PATTERNS.md`](./PATTERNS.md).

## Directory layout

```
fixtures/
  PATTERNS.md        # 22-pattern taxonomy — authoritative
  _validate.py       # VAL-101 + VAL-102 enforcement
  README.md          # this file
  python/            # ≥10 *.yaml cases
  rust/
  go/
  typescript/
  java/
  kotlin/
  csharp/
  c/
  cpp/
  ruby/
  swift/
  php/
  elixir/
```

Exactly 13 language directories. Each case lives under the directory
matching its `language:` field — `_validate.py` enforces this.

## Adding a new case

1. Choose the target language directory (e.g. `rust/`).
2. Pick a pattern slug from [`PATTERNS.md`](./PATTERNS.md) (one of the 22).
3. Name the file `{pattern_slug}_{short_descriptor}.yaml`
   (e.g. `add_guard_zero_div.yaml`). Stems must be unique within the
   language directory.
4. Fill in the template below, using YAML block scalars (`|`) for all
   code-bearing fields so whitespace and newlines are preserved verbatim.

### Template

```yaml
name: <unique_case_id_within_language>
pattern: <one_of_the_22_slugs>
language: <one_of_13_language_slugs>
original_code: |
  # source code BEFORE the edit
snippet: |
  # the compact edit snippet an agent would send
  # (context anchors + new lines + optional "... existing code ..." markers)
expected_output: |
  # source code AFTER the edit — must parse cleanly under <language>'s
  # tree-sitter grammar (VAL-101)
authoring_notes: |
  # optional free-form: why this case is here, edge cases, what a broken
  # adapter would fail on. Not scored.
```

See `DESIGN.md` §2 for the full schema and worked examples in Python and
Rust.

## Running the validator

From the `fasteditBench/` project root:

```bash
uv run python fixtures/_validate.py fixtures/
```

Exit codes:

- `0` — all cases passed (deferred cases, e.g. Elixir without its grammar,
  are tolerated by default)
- `1` — one or more cases failed schema / taxonomy / parse checks
- `2` — validator self-test failed (bug in `_validate.py`, not in your case)

Other modes:

```bash
# Run the internal self-tests
uv run python fixtures/_validate.py --self-test

# Treat deferred cases (missing tree-sitter grammar) as failures
uv run python fixtures/_validate.py fixtures/ --strict
```

## Elixir note

`tree-sitter-elixir` is not yet pinned in `pyproject.toml` — per
`DESIGN.md` §11, M2 adds that dep. Until then, Elixir cases are reported
as **deferred** (parse-check skipped with a warning), not failed.

## Pattern taxonomy

See [`PATTERNS.md`](./PATTERNS.md) for the full 22-pattern list, one
section per pattern with a worked example. The allowlist enforced by
`_validate.py` is a hard-coded `frozenset` at the top of that script — see
the PATTERNS.md § "Adding a new pattern" for how to extend the taxonomy
safely.
