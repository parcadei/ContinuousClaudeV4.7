---
description: Exploratory research via Ouros RLM — token-efficient reasoning with persistent REPL state
---

Use when you need to explore, reason through an idea, or research something you don't already know the answer to. Unlike `/autonomous` (which drives toward known goals), `/research` is for open-ended investigation where the destination isn't clear yet.

Ouros is a sandboxed Python REPL that persists variables across executions, supports fork/save/resume, and bridges to external functions (exa search, nia docs, llm_call, agent_call). Context lives in the REPL heap — not in your conversation window — so you can reason over large amounts of data without burning tokens.

**Harness location:** `tools/ouros_harness.py`

**Core commands:**

```bash
# Execute research code in a named session
python tools/ouros_harness.py --file /tmp/research.py \
  --session {topic} --storage continuum/research/{topic}/ouros

# Resume a previous session (loads saved state, then runs new code)
python tools/ouros_harness.py --file /tmp/next-step.py \
  --session {topic} --storage continuum/research/{topic}/ouros --load

# Fork a session for parallel exploration
python tools/ouros_harness.py --session {topic} \
  --storage continuum/research/{topic}/ouros --fork {topic}-branch-a

# Inspect session state
python tools/ouros_harness.py --session {topic} \
  --storage continuum/research/{topic}/ouros --list-vars

# Extract a variable as JSON
python tools/ouros_harness.py --session {topic} \
  --storage continuum/research/{topic}/ouros --get-var findings
```

**Available functions inside the REPL:**

Research: `exa_search(query, num_results, with_text, domains, start_date)`, `nia_search(query, repositories, data_sources)`, `nia_universal(query, limit)`, `nia_web(query)`, `nia_package(package, query, registry)`, `nia_package_grep(package, pattern, registry)`, `research_package(package, version, registry)`, `nia_help()`.

RLM recursive calls: `llm_call(prompt, model, backend, max_tokens, system, temperature)` — stateless LM sub-query. `agent_call(prompt, agent, model, max_turns, timeout, cwd, isolated)` — headless agent with full tool access.

Filesystem: `read_file(path)`, `write_file(path, content)`, `glob_files(pattern, path)`, `run_command(cmd, timeout)`.

**How to use this skill:**

Write a Python program that decomposes the research question, calls external functions, stores intermediate results as variables, and produces a telegraphic artifact as final output. The program runs inside Ouros — all variables persist in the session and can be inspected, forked, or resumed later.

The key RLM property: sub-agents (via `agent_call`) and LM queries (via `llm_call`) are functions called from within the REPL. Results flow back into the program as return values, not as context injected into your conversation. This means you can orchestrate multiple agents, aggregate their outputs, reason over the combined data — all without any of it touching your token budget.

**Example research program:**

```python
# Research: What auth patterns does this codebase use?

# 1. Gather data from multiple sources
codebase_auth = agent_call(
    "Search this codebase for authentication patterns. "
    "List every auth-related file with a one-line summary. "
    "Include: middleware, token validation, session handling, OAuth.",
    agent="claude-code", model="sonnet", max_turns=5
)

best_practices = exa_search(
    "modern authentication patterns 2026 JWT OAuth session",
    num_results=5, with_text=True
)

docs = nia_search("authentication middleware best practices")

# 2. All results are now REPL variables — no tokens spent in conversation

# 3. Synthesize via LLM sub-call (only sees relevant slices)
synthesis = llm_call(
    f"Given these codebase auth patterns:\n{codebase_auth}\n\n"
    f"And these best practices:\n{docs.get('answer', '')}\n\n"
    "Write a telegraphic assessment: what's good, what's missing, "
    "what should change. Be specific with file:line references.",
    model="claude-sonnet-4-6", backend="anthropic", max_tokens=2000
)

# 4. Store as artifact
findings = {
    "question": "What auth patterns does this codebase use?",
    "synthesis": synthesis,
    "sources": {
        "codebase": codebase_auth[:500],
        "exa_results": len(best_practices.get("results", [])),
        "nia_answer": docs.get("answer", "")[:300],
    },
}

# 5. Write telegraphic artifact for /autonomous or handoff consumption
write_file("continuum/research/auth-patterns/findings.md", f"""# Auth Patterns Research

{synthesis}

## Sources
- Codebase scan: {len(codebase_auth)} chars of analysis
- Exa: {len(best_practices.get('results', []))} results
- Nia docs: indexed answer available
""")

print(synthesis)
```

**Output convention:** Every research session should produce a telegraphic artifact at `continuum/research/{topic}/findings.md`. This is what `/autonomous` reads as input context, what handoffs reference, and what the user sees. The full REPL state (all variables, raw API responses, intermediate reasoning) persists in the session and can be resumed or forked — but only the artifact crosses back into the conversation.

**Storage layout:**
```
continuum/research/
  {topic}/
    findings.md          ← telegraphic artifact (what others consume)
    ouros/               ← serialized REPL state (.bin files, one per session)
```
Pass `--storage continuum/research/{topic}/ouros` so REPL `.bin` files stay in their own subdir and don't clutter the artifact folder.

**When to fork:** Fork when you want to explore two hypotheses in parallel without them contaminating each other's variable space. Each fork gets an independent copy of all accumulated state.

**When to resume:** Resume when you got initial results and want to go deeper on one thread, or when a previous research session produced partial findings you want to extend.

**Relationship to other skills:** `/research` explores and produces artifacts. `/autonomous` consumes those artifacts as milestone inputs. `/create-handoff` references them for context transfer. They compose but don't overlap — research finds the path, autonomous walks it.
