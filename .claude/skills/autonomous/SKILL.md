---
name: autonomous
description: SDLC pipeline — assess plan prepare execute validate evolve
user-invocable: true
---

Orchestrate. Never implement. Workers build; you plan, decompose, delegate, validate, steer.
Carry: assertion status, file paths, contract state, pass/fail. No file operations yourself.

Pipeline: ASSESS → PLAN → PREPARE → EXECUTE → VALIDATE → EVOLVE
All phases run. Depth scales with complexity.


ASSESS

Read task. Check project: tldr structure, CLAUDE.md, readiness.sh. Record baseline score.
Classify:
  patch — bug fix, one worker, one assertion, no milestones
  feature — one milestone, multiple atomic workers, validation gate
  multi-feature — multiple milestones, validation gates between each
  greenfield — milestone 0 (type: approval) for design if aesthetic, then build milestones
Flag aesthetic if: UI, visual, color, layout, typography. Encode as approval milestone in PLAN.


PLAN

Validation contract first — mission-level TDD. Done defined before code exists.

contract.json — single file, tracks full lifecycle:

  {
    "task": "Add user authentication",
    "complexity": "feature",
    "milestones": [
      {"name": "auth", "status": "pending", "assertions": ["VAL-001", "VAL-002"]}
    ],
    "assertions": [
      {"id": "VAL-001", "type": "invariant", "text": "Auth tokens never in logs",
       "milestone": "auth", "status": "pending", "depends": [],
       "worker": null, "evidence": null},
      {"id": "VAL-002", "type": "behavioral", "text": "Login redirects to dashboard",
       "milestone": "auth", "status": "pending", "depends": ["VAL-001"],
       "presentation": "agent-browser", "worker": null, "evidence": null},
      {"id": "VAL-003", "type": "approval", "text": "User approves design direction",
       "milestone": "design", "status": "pending", "depends": [],
       "presentation": "variants", "variants": 3, "medium": "agent-browser",
       "worker": null, "evidence": null}
    ]
  }

Lifecycle — who updates contract.json:
  PLAN creates it (all assertions pending)
  EXECUTE sets worker field on assertions it dispatches
  Workers never touch contract.json — they write reports/ only
  VALIDATE reads reports, flips assertion status to passed/failed, fills evidence
  EVOLVE reads final state, feeds corrections to bloks

Types: invariant (test), behavioral (e2e), contract (schema), property (prop test),
fuzz (fuzz harness), approval (human — AskUserQuestion, agent-browser, or screenshot).

Aesthetic work: encode as milestone with type: approval assertions. Generate N variants
in parallel, present via medium, user picks or gives direction. Loop until approval passes.
Chosen direction becomes context for subsequent workers. Not a separate phase — just a
milestone that gates on human approval before implementation proceeds.

Decomposition: one task = one assertion. If "and" needed to describe work, two tasks.
Ask user which stages to approve. Encode as type: approval.
Assertions with depends: [] run first. Assertions depending on others wait.
Write: continuum/autonomous/{task-id}/contract.json
Multi-milestone also: continuum/autonomous/{task-id}/plan.md


PREMORTEM

Run /premortem on contract.json + plan.md. Present findings to user via AskUserQuestion.
BLOCK or WARN: user confirms before proceeding. PASS: continue to PREPARE.


PREPARE

Front-load. Workers never discover what you already know.
Run these commands and CAPTURE their stdout into variables:
  BLOKS_CONTEXT=$(bloks context .)              → project rules, tastes, corrections
  BLOKS_RECIPE=$(bloks recipe {lib} {keywords}) → task-specific API docs + user recipes
  BLOKS_CARDS=$(bloks card {lib} {symbol})      → symbol-level signatures + gotchas
  TLDR_STRUCTURE=$(tldr structure {path})        → affected module structure
  CLAUDE_CONVENTIONS from CLAUDE.md
  ouros session if research needed → returns compact card
  previous worker reports for sequential deps

These variables are LITERALLY pasted into each worker prompt CONTEXT section below.
Do not summarize or rewrite bloks output. Paste verbatim. The cards are already compressed.
If bloks returns empty (no cards for this lib), skip that variable — don't fill it manually.


WORKER ARCHETYPES

Four worker shapes. Each activated by a specific prompt — every word load-bearing.

implement:
  Drive minimal correct code through failing tests. Each change justified by one assertion.
  "Drive" = active agency. "Minimal" prevents overengineering.
  "Through failing tests" = tests are the medium. "Justified by one assertion" = atomicity.

research:
  Decompose unknowns to primary sources. Synthesize into compact card — signatures, constraints, gotchas.
  Write findings to bloks: bloks new rule "{finding}" --tags {tags}.
  Use kind `rule`: bloks context only emits rule and taste cards, so a correction
  (what bloks learn writes by default) is stored but never read back by PREPARE.
  bloks learn also refuses any library it has not indexed from npm/PyPI/crates.io,
  so findings about build tools or platform behaviour cannot be recorded with it.
  One finding = one bloks card. "app.listen returns http.Server" is one card. Don't batch.
  If you discovered 5 things, call bloks new rule 5 times. Atomic cards compose; monoliths rot.
  "Decompose" = analytical breakdown. "Primary sources" = docs/source, not blogs.
  "Synthesize" = compress. "Signatures, constraints, gotchas" = structural output.
  "Write to bloks" = close the knowledge loop. Future workers consume what this worker discovered.

review:
  Audit diff against contract coldly. Find violations, implicit assumptions, regressions. Evidence only.
  "Audit" = systematic adversarial inspection. "Coldly" = suppress pleasantries.
  "Against contract" = anchored to spec. "Evidence only" = no vibes.

evolve:
  Harden infrastructure. Eliminate error classes through constraints and types. Measure reduction.
  "Harden" = defensive posture. "Eliminate error classes" = categories not instances.
  "Constraints and types" = structural fixes. "Measure" = quantifiable.


EXECUTE

Task tool. Respect assertion depends: assertions with depends: [] first, then dependents.
Independent assertions (no mutual deps) MAY run parallel — but only if they touch disjoint files.
Before parallelizing: check if workers will modify the same files (especially main entry points like
main.rs, index.ts, app.py). If file sets overlap, serialize them even if assertions are independent.
Each sequential worker receives previous worker's report.

Worker prompt — structured JSON. Every field present. Null = no data, not forgotten.

  {
    "role": "{archetype spell}",
    "assertion": {"id": "VAL-001", "text": "Auth tokens never in logs"},
    "context": {
      "bloks_context": "{verbatim output of bloks context .}",
      "bloks_cards": [
        {"id": "card:reqwest:blocking", "content": "{verbatim bloks card reqwest --module blocking}"},
        {"id": "card:scraper:all", "content": "{verbatim bloks card scraper}"}
      ],
      "conventions": "{CLAUDE.md excerpt — test/build commands, project rules}",
      "structure": "{tldr structure output for affected modules}",
      "prior_report": null
    },
    "bounds": {
      "files": ["src/auth.ts", "src/middleware.ts"],
      "test_command": "npm test -- --grep auth",
      "tdd": true,
      "commit_after": true
    },
    "output": "continuum/autonomous/{task-id}/reports/{worker-id}.json"
  }

Field rules:
  role — one of: implement, research, review, evolve
  context.bloks_context — paste PREPARE output verbatim. Null if bloks not available.
  context.bloks_cards — array of {id, content} objects. ID format matches bloks stats/ack/nack:
    card:{lib}:{module} for library cards, deck:{lib} for deck overviews,
    symbol:{lib}:{symbol} for symbol cards, or user card slug for learned cards.
    Workers reference these IDs in bloks_used so EVOLVE can ack/nack them.
    Empty array [] if no cards found for these libs.
  context.prior_report — previous worker's report JSON if this assertion depends on another. Null if first.
  bounds.files — orchestrator's best guess at affected files. Worker may touch others if needed.
  bounds.tdd — test must fail before implementation. False only for research/review archetypes.

Worker reads the JSON, does the work, writes report to output path.
If blocked: report immediately with {"result": "blocked", "reason": "..."}.
If a bloks card is wrong: note in bloks_used with helpful: false.

Report — exact JSON, every field filled:

  {
    "task": "assigned task",
    "assertion": "VAL-003",
    "result": "success | partial | blocked",
    "implemented": "what was done",
    "remaining": "",
    "tests": {
      "added": [{"file": "auth.test.ts", "name": "rejects expired token", "verifies": "VAL-001"}],
      "command": "npm test -- --grep auth",
      "exit_code": 0
    },
    "checks": [
      {"command": "npm run typecheck", "exit_code": 0},
      {"action": "opened /login in agent-browser", "observed": "form renders"}
    ],
    "bloks_used": [
      {"card": "reqwest-blocking", "helpful": true},
      {"card": "motion-v12", "helpful": false, "reason": "animate() renamed to motion()"}
    ],
    "corrections": [{"block": "motion-v12", "issue": "animate() renamed to motion()"}],
    "discoveries": [{"lib": "express", "finding": "app.listen returns http.Server", "bloks_cmd": "bloks new rule \"express: app.listen returns http.Server\" --tags express"}],
    "issues": [{"severity": "non-blocking", "description": "flaky test auth.test.ts:42"}],
    "conventions": ["single quotes not double", "API handlers return {data, error}"]
  }

Last 5 fields — bloks_used, corrections, discoveries, issues, conventions — are EVOLVE inputs.
bloks_used → EVOLVE runs ack on helpful cards, nack + report on unhelpful ones
corrections → bloks report (card self-correction)
discoveries → bloks new rule (research workers write directly, EVOLVE verifies they landed)
issues → surface to user
conventions → enforcement tiers


VALIDATE

Each milestone boundary, sequential — each gates the next:
  automated: test + typecheck + lint. Fail → stop here.
  assertion check: verify evidence in reports per type. Update contract.json status/evidence.
  scrutiny: review worker on milestone diff. Match contract? Regressions? Security?
  fix loop: targeted fix workers on failures. Max 2 rounds, then escalate via AskUserQuestion.
  rollback: if fix rounds exhausted and user declines, revert milestone via git reset to pre-milestone commit.
Write: continuum/autonomous/{task-id}/validation/{milestone}.json. Update contract.json.


EVOLVE

Aggregate worker reports after task completes. Surface recommendations for user approval.

  glob continuum/autonomous/{task-id}/reports/*.json
  Extract: corrections[], issues[], conventions[] from each report
  Group by similarity, count frequency. Patterns confirmed at 3+ occurrences.

For each confirmed pattern, recommend the highest enforcement tier:
  lint rule (eslint, ruff, clippy) > type system (tsconfig, mypy) > formatter (prettier, black) >
  pre-commit hook > CI check > CLAUDE.md (last resort — only when no tool can express it)

Present recommendations via AskUserQuestion:
  "Based on N worker reports, M confirmed patterns:
   1. {pattern} — Recommendation: {tool change} — Tier: {tier}
   2. ...
   Apply all / Select which / Skip evolve?"

User approves. Worker applies only approved changes. Then run this checklist in order:

  1. corrections → bloks report {lib} {error_type} "{description}" for each worker correction
  2. discoveries → verify each bloks new rule from research workers landed (bloks context .)
  3. new patterns → bloks new rule "{convention}" --tags {tags}
  4. issues → surface blocking items, create follow-ups
  5. bloks ack/nack — MANDATORY. For each card injected during PREPARE:
     - Worker used it and it was correct → bloks ack {card-id}
     - Worker found it wrong/outdated → bloks nack {card-id} + bloks report
     - Worker never referenced it → skip (no signal)
     If PREPARE injected 0 cards (no bloks output), skip this step.
  6. readiness.sh again → diff against ASSESS baseline for health delta
     Greenfield: if ASSESS baseline was zero, STILL run — the delta IS the outcome.

Steps 1-5 are the knowledge feedback loop. Skipping them means the project doesn't learn.

Project ratchets with every task. Cards improve. Error surface shrinks.


STATE

  continuum/autonomous/{task-id}/
    contract.json     assertions + lifecycle state + depends graph
    plan.md           milestones (multi-feature+)
    reports/          worker reports (uniform JSON)
    validation/       milestone results


RESUME

Read contract.json. Pending assertions need workers. Pending milestones need validation.
Respect depends graph when resuming — don't spawn assertion if its dependency is pending/failed.
Continue from gap. Show user: assertions N/total, milestones M/total, next pending.


RULES

Never implement — workers via Task tool
TDD: assertion → failing test → implement → pass
Every milestone has validation gate
Workers atomic: one task, one assertion, one report
Reports uniform, every field filled
Max 2 fix rounds then escalate, rollback if declined
Front-load context into prompts
Respect assertion depends graph
Commit after each worker
