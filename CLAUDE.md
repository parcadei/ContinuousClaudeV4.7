# Continuous Claude v4.7

Autonomous SDLC pipeline for Claude Code. 9 skills, 2 agents, 5 hooks.

## Skills

| Skill | Trigger | Role |
|-------|---------|------|
| `/autonomous` | Bounded implementation tasks | ASSESS → PLAN → PREMORTEM → PREPARE → EXECUTE → VALIDATE → EVOLVE |
| `/autonomous-research` | Open-ended research | Looping research pipeline via Ouros — EVOLVE loops back to PLAN |
| `/research` | Quick exploration | Single-pass Ouros REPL exploration |
| `/premortem` | Before implementation | Failure analysis gate — first-principles risk check |
| `/bootup` | Session start | Assess readiness, route to research/autonomous/review |
| `/review` | Code review | Structural + semantic review |
| `/create-handoff` | Session end | Serialize context for next session |
| `/resume-handoff` | Session start | Resume from handoff document |
| `/upgrade-harness` | Extend Ouros | Add new external functions to Ouros sandbox |

## Agents

| Agent | Role |
|-------|------|
| `worker` | Executes atomic tasks — full autonomy over implementation |
| `oracle` | External research — docs, APIs, best practices |

## Hooks (all .mjs — cross-platform)

| Hook | Event | What it does |
|------|-------|-------------|
| `status.mjs` | statusLine | Context %, git info, goal from handoffs |
| `tldr-read.mjs` | PreToolUse:Read | Injects structural nav map for large code files, truncates |
| `post-edit-diagnostics.mjs` | PostToolUse:Edit\|Write\|MultiEdit\|Update | Type errors + lint after edits |
| `pre-compact.mjs` | PreCompact | Auto-handoff before context compaction |
| `auto-handoff-stop.mjs` | Stop | Blocks at 85% context to force handoff |

## External Tools (optional, on PATH)

| Tool | What it does |
|------|-------------|
| [bloks](https://github.com/parcadei/bloks) | Library knowledge cards — API docs, taste, corrections |
| [tldr](https://github.com/parcadei/tldr-code) | Token-efficient code analysis (AST, call graphs, diagnostics) |
| [ouros](https://github.com/parcadei/ouros) | Sandboxed Python REPL with fork/save/resume |
| [fastedit](https://github.com/parcadei/fastedit) | Fast code editing via merge model (`pip install fastedits`) |

## Scripts

- `scripts/readiness.sh` — assess project health (27 criteria, 5 levels)
- `scripts/readiness-fix.sh` — auto-remediate readiness gaps

## Tool Bridges (in tools/)

- `ouros_harness.py` — Ouros REPL bridge with exa_search, nia_search, llm_call, agent_call
- `exa_search.py` — web search (requires EXA_API_KEY in .env)
- `nia_docs.py` — documentation search (requires NIA_API_KEY in .env)

## Architecture

Skills orchestrate — they never implement directly. Workers build. Bloks cards carry knowledge between sessions. Handoffs carry session state.

**Enforcement hierarchy:** lint rule > type system > formatter > pre-commit hook > CI check > CLAUDE.md (last resort). Deterministic enforcement always preferred over probabilistic instructions.

**Knowledge flow:** PREPARE consumes bloks cards → workers execute → EVOLVE produces new cards via `bloks new rule`. Cards score through ack/nack.
