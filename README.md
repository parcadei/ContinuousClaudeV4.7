# Continuous Claude v4.7

Autonomous software development pipeline for Claude Code. Skills orchestrate; workers build; the project improves with every task.

## What's in here

```
.claude/
  skills/          9 workflow skills
  agents/          worker + oracle
  hooks/           5 hooks (.mjs — cross-platform)
  settings.json    hook wiring + env config
scripts/
  readiness.sh     assess project health (27 criteria, 5 levels)
  readiness-fix.sh auto-remediate gaps
tools/
  ouros_harness.py sandboxed Python REPL with external function bridge
  exa_search.py    web search bridge (requires EXA_API_KEY)
  nia_docs.py      documentation search bridge (requires NIA_API_KEY)
```

## Skills

| Skill | What it does |
|-------|-------------|
| `/autonomous` | Full SDLC pipeline: assess, plan, premortem, prepare, execute, validate, evolve |
| `/autonomous-research` | Looping research pipeline — hypotheses deepen via Ouros sessions |
| `/research` | Open-ended exploration via Ouros REPL with persistent state |
| `/premortem` | Failure analysis gate — first-principles retrospective before implementation |
| `/bootup` | Assess project readiness, route to research/autonomous/review |
| `/review` | Structural + semantic code review |
| `/create-handoff` | Serialize session context for transfer |
| `/resume-handoff` | Resume an autonomous session from handoff |
| `/upgrade-harness` | Add new external functions to the Ouros sandbox |

## Hooks

All hooks are plain `.mjs` (ES modules). No build step, no dependencies — Node.js is guaranteed wherever Claude Code runs.

| Hook | Event | Purpose |
|------|-------|---------|
| `status.mjs` | statusLine | Shows context %, git branch, staged/unstaged counts, goal from handoffs |
| `tldr-read.mjs` | PreToolUse:Read | Injects structural nav map for large code files, truncates to save tokens |
| `post-edit-diagnostics.mjs` | PostToolUse | Runs type checker + linter after edits for immediate feedback |
| `pre-compact.mjs` | PreCompact | Writes auto-handoff YAML before context compaction |
| `auto-handoff-stop.mjs` | Stop | Blocks at 85% context usage to force handoff before data loss |

Hooks that use `tldr` (tldr-read, post-edit-diagnostics) fall through silently if tldr is not installed.

## Dependencies

### Required

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — the runtime

### Recommended (on PATH)

| Tool | Install | What it does |
|------|---------|-------------|
| [bloks](https://github.com/parcadei/bloks) | `cargo install bloks` | Library knowledge cards — API docs, taste, corrections |
| [tldr](https://github.com/parcadei/tldr-code) | `cargo install --git https://github.com/parcadei/tldr-code tldr-cli` | Token-efficient code analysis (AST, call graphs, impact, diagnostics) |
| [ouros](https://github.com/parcadei/ouros) | `python -m pip install ouros` | Sandboxed Python REPL with fork/save/resume |
| [fastedit](https://github.com/parcadei/fastedit) | `pip install fastedits` | Fast code editing via merge model — 10x fewer tokens per edit |

Two notes on those installs. `tldr` is published from the `tldr-cli` workspace member rather than
as a `tldr-code` crate, and its releases carry no Windows asset, so the git build above is the only
path on Windows. `ouros` is a Python module, not a cargo binary — `tools/ouros_harness.py` imports
it directly — and PyPI ships wheels for CPython 3.10 through 3.13 only, so install it into an
interpreter in that range.

Install it with `python -m pip`, not a bare `pip`, and use the same interpreter that runs the
harness. A bare `pip` can belong to a different interpreter than `python`, in which case the install
succeeds and `python tools/ouros_harness.py` still fails on `import ouros`. Where several versions
are present, name one on both sides — on Windows `py -3.13 -m pip install ouros` alongside
`py -3.13 tools/ouros_harness.py`; elsewhere `python3.13 -m pip install ouros`. A 3.14 default
interpreter has no wheel and will fail regardless.

### Optional (API keys in .env)

```bash
cp .env.example .env
# Add your keys:
# EXA_API_KEY=     — for web search via exa_search.py
# NIA_API_KEY=     — for documentation search via nia_docs.py
```

Python dependencies for tools that use API keys:
```bash
pip install -r tools/requirements.txt  # aiohttp
```

### FastEdit setup

A PreToolUse hook redirects the built-in `Edit` tool to [FastEdit](https://github.com/parcadei/fastedit) when available. If FastEdit isn't installed, Edit falls through normally. Model weights on [HuggingFace](https://huggingface.co/continuous-lab/FastEdit).

```bash
pip install fastedits[mlx]
fastedit pull                # downloads MLX 8-bit model (~1.7GB) from HuggingFace
fastedit read src/app.py     # verify it works — shows file structure
fastedit search "auth" src/  # search codebase for symbols
```

The PyPI package is `fastedits` but the CLI entry point is `fastedit`. To choose a different model format:

```bash
fastedit pull              # MLX 8-bit (default, Apple Silicon)
fastedit pull --model bf16 # BF16 safetensors (GPU serving)
```

To point at a custom model path: `export FASTEDIT_MODEL_PATH=/path/to/your/model`. Resolution order: `FASTEDIT_MODEL_PATH` env var → `./models/` → `~/.cache/fastedit/models/` → auto-download.

MCP server config — add to `~/.claude.json` (or use `claude mcp add`):

```json
{
  "mcpServers": {
    "fastedit": {
      "command": "python3",
      "args": ["-m", "fastedit.mcp_server"]
    }
  }
}
```

This exposes 10 tools in Claude Code automatically: `fast_edit`, `fast_batch_edit`, `fast_read`, `fast_search`, `fast_delete`, `fast_move`, `fast_rename`, `fast_diff`, `fast_undo`, `fast_multi_edit`.

## Quick start

### Option A: Add to an existing project

Copy `.claude/`, `scripts/`, and `tools/` into your project root. The skills, hooks, and agents will be available immediately.

### Option B: Try it standalone

```bash
git clone https://github.com/parcadei/ContinuousClaudeV4.7.git
cd your-project
CLAUDE_CONFIG_DIR=/path/to/ContinuousClaudeV4.7 claude
```

This tells Claude Code to use CCv4's `.claude/` directory (skills, hooks, agents, settings) while working in your project.

### Then

1. Run `/bootup` to assess readiness and route to a workflow
2. Or jump straight to `/autonomous` with a task description
3. Or `/autonomous-research` for open-ended research

## How it works

**The loop:** Research explores unknowns. Autonomous plans and builds. Workers execute atomic tasks. Validation gates each milestone. Evolve aggregates findings, recommends lint rules and tool configs, and feeds corrections back to bloks cards. The project gets better with every run.

**Knowledge flow:** Bloks cards carry API knowledge, taste, and corrections. Cards are consumed during PREPARE (injected into worker prompts) and produced during EVOLVE (from worker findings). Cards score through ack/nack — useful cards surface, bad cards get revised or retired.

**Research flow:** `/autonomous-research` runs a looping pipeline where workers research inside Ouros. Results persist in the REPL heap (zero orchestrator tokens). Workers share state via Ouros sessions (`--load` for sequential, `--fork` for parallel). Only compact artifacts cross back to the orchestrator.

**Enforcement hierarchy:** When workers report conventions, evolve maps them to the strongest deterministic enforcement: lint rule > type system > formatter > pre-commit hook > CI check > CLAUDE.md (last resort). Probabilistic instructions are always the fallback, never the first choice.

## Project structure (created by autonomous)

```
continuum/
  autonomous/{task-id}/
    contract.json    assertions + lifecycle state
    plan.md          milestones (multi-feature+)
    reports/         worker reports (uniform JSON)
    validation/      milestone results
  research/{topic}/
    research_contract.json   hypotheses + lifecycle + iteration history
    findings.md              final synthesis
    artifacts/               per-hypothesis per-iteration evidence
    reports/                 worker reports (compact JSON)
```

## License

MIT
