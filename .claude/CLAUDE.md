# Continuous Claude v4

Autonomous software development pipeline. Skills orchestrate; workers build; the project improves with every task.

## Skills

| Skill | What it does |
|-------|-------------|
| `/autonomous` | Full SDLC: assess, plan, premortem, prepare, execute, validate, evolve |
| `/research` | Open-ended exploration via Ouros REPL with persistent state |
| `/premortem` | Failure analysis gate — risk identification before implementation |
| `/bootup` | Assess project readiness, route to research/autonomous/review |
| `/review` | Structural + semantic code review |
| `/create-handoff` | Serialize session context for transfer |
| `/resume-handoff` | Resume autonomous session from handoff |
| `/upgrade-harness` | Add new external functions to the Ouros sandbox |

## Agents

| Agent | Role | Model |
|-------|------|-------|
| worker | Implementation — executes one bounded step, reads task state, does work, updates state | inherited |
| oracle | External research — web search, docs, package analysis via Ouros sandbox | sonnet |

## Hooks

Three hooks form a context safety loop (wired in `.claude/settings.json`):

| Hook | Event | What it does |
|------|-------|-------------|
| `status.py` | statusLine | Status line: context %, git info, goal from latest handoff |
| `pre-compact.py` | PreCompact | Parses transcript, writes auto-handoff before compaction |
| `auto-handoff-stop.py` | Stop | Blocks at 85% context, suggests `/create_handoff` |

Flow: status tracks context % (writes to temp file) -> stop reads it and blocks at threshold -> pre-compact saves session state before compaction.

## Sandbox

Python sandbox (ouros) for executing code with built-in tool access. Use for research, codebase exploration, data processing, or multi-step operations.

```bash
# From file (preferred — no quoting issues)
tools/ouros_harness.py --file /tmp/script.py

# With session persistence (variables survive across executions)
tools/ouros_harness.py --file /tmp/script.py --session my-task --storage continuum/research/my-task/ouros

# Load a previous session and continue
tools/ouros_harness.py --file /tmp/script.py --session my-task --load

# Inspect session state
tools/ouros_harness.py --session my-task --list-vars
tools/ouros_harness.py --session my-task --get-var results
```

### Available functions

Call `nia_help()` inside the sandbox for full details. Summary:

#### Research (use for context blocks and external docs)

| Function | What it does |
|----------|-------------|
| `research_package(pkg, version, registry)` | ALL-IN-ONE: nia + exa, filters noise, returns compact structured data |
| `nia_search(query)` | Search indexed docs/repos |
| `nia_universal(query, limit=10)` | Search all 10k+ public indexed sources |
| `nia_web(query)` | Web search via Nia |
| `nia_package(pkg, query, registry)` | Semantic search within a package's source code |
| `nia_package_grep(pkg, pattern, registry)` | Regex search within a package's source code |
| `exa_search(query, num_results=5)` | Semantic web search via Exa AI |

Registries: `npm`, `py_pi`, `crates_io`, `go_modules`.

#### Filesystem & codebase

| Function | What it does |
|----------|-------------|
| `read_file(path)` | Read a file from the host filesystem |
| `write_file(path, content)` | Write a file to allowed directories |
| `glob_files(pattern)` | Find files matching a pattern |
| `run_command(cmd)` | Run an allowed shell command |

### Token efficiency

The sandbox processes data internally — only `print()` output enters the agent's context. Always filter, truncate, and structure results inside the script.

### Context blocks

Package documentation is handled by `bloks` — run `bloks card {lib} {symbol}` for API context.

```python
# Research a package (all-in-one, ~1K tokens output)
data = research_package("express", version="5.1.0", registry="npm")

# Individual functions for targeted lookups
nia_package_grep("express", "deprecat", registry="npm")
nia_package("fastapi", "dependency injection", registry="py_pi")
```

### Security

Sandbox enforces deny-by-default: `read_file` only reads project + `/tmp/ouros`, `write_file` only writes to `/tmp/ouros-sandbox-output`, `run_command` only allows safe commands (`tldr`, `grep`, `rg`, `git log/diff/show/blame`, `wc`, `echo`).

## Editing

A PreToolUse hook redirects the built-in `Edit` tool to FastEdit when available (`pip install fastedits[mcp]`). FastEdit uses a 1.7B merge model for 10x fewer tokens per edit. If FastEdit isn't installed, Edit falls through normally.

Prefer FastEdit tools directly when available: `fast_edit`, `fast_batch_edit`, `fast_read`, `fast_search`, `fast_delete`, `fast_move`, `fast_rename`, `fast_diff`, `fast_undo`.

## External tools

| Tool | What it does |
|------|-------------|
| [bloks](https://github.com/parcadei/bloks) | Library knowledge cards — API docs, taste, corrections |
| [tldr](https://github.com/parcadei/tldr) | Token-efficient code analysis (AST, call graphs, impact) |
| [ouros](https://github.com/parcadei/ouros) | Sandboxed Python REPL with fork/save/resume |
| [fastedit](https://github.com/parcadei/fastedit) | Fast code editing via 1.7B merge model (MCP server) |

## Project structure (created at runtime)

```
continuum/
  autonomous/{task-id}/
    contract.json    assertions + lifecycle state
    plan.md          milestones (multi-feature+)
    reports/         worker reports (uniform JSON)
    validation/      milestone results
  research/{topic}/
    findings.md      telegraphic artifact
thoughts/
  shared/handoffs/   session handoffs (manual + auto-generated)
```
