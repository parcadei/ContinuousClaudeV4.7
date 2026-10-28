---
name: autonomous-research
description: Looping research pipeline — hypotheses deepen via Ouros sessions, artifacts flow back
user-invocable: true
---

Orchestrate. Never research directly. Workers research inside Ouros; you plan, decompose, delegate, synthesize.
Carry: hypothesis status, confidence scores, iteration number, ouros session names. No web searches yourself.
Workers share state through Ouros sessions — not through your context window.

Pipeline: ASSESS → PLAN → PREPARE → EXECUTE → VALIDATE → EVOLVE → (loop to PLAN)
Terminates when: all hypotheses confidence >= 0.8, context budget >= 75%, or user stops.


ASSESS

Read research question. Classify scope:
  focused — single question, 1-3 hypotheses, 2-3 iterations
  exploratory — broad topic, 5+ hypotheses, open-ended
  comparative — N alternatives to evaluate, structured comparison
Estimate iteration budget: focused ~3, exploratory ~6, comparative ~4.
Check available tools: ouros harness, exa, nia, bloks.
Create ouros session name: {topic-slug} (e.g., "attention-scale", "auth-patterns").


PLAN

Hypothesis contract first — what would "understanding this" look like?

research_contract.json:

  {
    "question": "How do attention patterns change with model scale?",
    "scope": "exploratory",
    "iteration": 1,
    "ouros_session": "attention-scale",
    "hypotheses": [
      {"id": "H-001", "text": "Attention head specialization increases with scale",
       "confidence": 0, "status": "pending", "sources": 0,
       "depends": [], "worker": null}
    ],
    "accumulated_findings": [],
    "iterations_completed": 0
  }

Confidence: 0 (unknown) → 0.3 (speculative) → 0.6 (supported) → 0.9 (well-established).
Status: pending, researching, supported, refuted, inconclusive, superseded.

On iteration 2+: read EVOLVE output. Add new hypotheses from workers' new_questions.
Supersede refuted hypotheses. Carry all findings forward.


PREMORTEM

Bias check:
  confirmation bias — only searching for supporting evidence?
  source bias — over-relying on one source type?
  blind spots — obvious sub-questions not covered?
Present to user. BLOCK on blind spots, WARN on bias, PASS otherwise.


PREPARE

Front-load what's already known:

  BLOKS_CONTEXT=$(bloks context .)
  BLOKS_CARDS=$(bloks search {topic keywords})
  PRIOR_VARS=$(python tools/ouros_harness.py --session {topic} --list-vars)  # iteration 2+

On iteration 1: mostly empty. Workers discover.
On iteration 2+: Ouros session has accumulated variables from prior iterations.
Workers --load the session and build on prior state.


EXECUTE

Workers research INSIDE Ouros. Same contract pattern as /autonomous — worker does the work,
orchestrator just provides hypothesis + context + where to report.

Worker prompt — structured JSON, same shape as /autonomous:

  {
    "role": "research",
    "hypothesis": {"id": "H-001", "text": "Attention head specialization increases with scale"},
    "context": {
      "bloks_context": "{verbatim bloks output}",
      "bloks_cards": [],
      "prior_findings": "{accumulated_findings from research_contract.json}",
      "available_tools": "exa_search, nia_universal, nia_search, nia_web, llm_call, agent_call, read_file, write_file"
    },
    "bounds": {
      "ouros_session": "attention-scale",
      "ouros_storage": "continuum/research/attention-scale",
      "ouros_load": false,
      "ouros_fork": "attention-scale-H001",
      "max_sources": 5,
      "source_types": ["arxiv", "official_docs", "reference_impl"],
      "artifact_path": "continuum/research/attention-scale/artifacts/H-001-iter1.md"
    },
    "output": "continuum/research/attention-scale/reports/H-001-iter1.json"
  }

bounds.ouros_load: true on iteration 2+ (worker resumes session, sees prior variables).
bounds.ouros_fork: set when parallel workers need independent variable space.

Worker flow (the worker decides HOW to research — orchestrator just says WHAT):
  1. Write a Python research program (worker chooses search queries, synthesis approach)
  2. Run it: python tools/ouros_harness.py --file /tmp/{program}.py --session {session} --storage {storage}
     Add --load if bounds.ouros_load is true. Add --fork {name} if bounds.ouros_fork is set.
  3. Inside Ouros: exa_search, nia_search, llm_call etc. — results in REPL heap, not in context
  4. Program writes compact artifact to bounds.artifact_path
  5. Worker reads artifact, writes report JSON to output path

The worker is an agent with full autonomy over research strategy. It writes the program,
picks the queries, chooses which tools to use, decides how to synthesize.
The orchestrator only controls: which hypothesis, which session, where to report.

Sharing between workers (same iteration):
  Fork: --fork {topic}-{hypothesis-id}. Independent variable space, no cross-contamination.

Sharing between iterations:
  Load: --load resumes base session. Iteration 2 workers see iteration 1 variables.
  Accumulated findings grow in the REPL heap — zero orchestrator tokens.

Report — uniform JSON, same principle as /autonomous (every field filled):

  {
    "hypothesis": "H-001",
    "result": "supported | refuted | inconclusive",
    "artifact_path": "continuum/research/attention-scale/artifacts/H-001-iter1.md",
    "source_count": 5,
    "synthesis_preview": "3 papers support specialization increasing with scale...",
    "confidence_delta": +0.3,
    "new_questions": ["Does this hold for MoE architectures?"],
    "bloks_written": ["bloks new rule \"transformers: attention heads specialize linearly with log(params)\" --tags transformers"],
    "corrections": [],
    "ouros_vars_created": ["papers_H001", "synthesis_H001", "sources_H001"]
  }

synthesis_preview ~300 chars. Full synthesis in the artifact file.
ouros_vars_created tells the next iteration what variables are available to --load.
The orchestrator carries forward: artifact_path, confidence, new_questions. Not raw findings.


VALIDATE

After each iteration's workers complete:
  artifact check: do artifact files exist at reported paths?
  confidence calibration: read artifacts, is confidence justified by evidence?
  consistency: do findings from parallel workers contradict?
  coverage: what % of hypothesis space is at confidence >= 0.6?

Update research_contract.json: hypothesis status, confidence, source counts.
Write: continuum/research/{topic}/validation/iteration-{N}.json


EVOLVE — THE LOOP

  1. Aggregate worker reports (compact — just paths, confidence, new questions).
  2. Update accumulated_findings in research_contract.json.
  3. Score progress: % of hypotheses at confidence >= 0.6.
  4. Generate new hypotheses from workers' new_questions.
  5. Mark superseded hypotheses.
  6. Write discoveries to bloks: bloks new rule "{finding}" --tags {topic}
  7. Bloks ack/nack on cards consumed in PREPARE.

Decision gate:
  ALL hypotheses confidence >= 0.8 → SYNTHESIZE FINAL
  Context budget >= 75% → SYNTHESIZE FINAL (save room for handoff)
  User says stop → SYNTHESIZE FINAL
  Otherwise → increment iteration, loop to PLAN with new hypotheses

SYNTHESIZE FINAL:
  Load ouros session: --list-vars to see all accumulated research variables.
  Write final synthesis program that reads all findings_* vars and produces:
    continuum/research/{topic}/findings.md — telegraphic artifact
  Structure: question, findings per hypothesis, confidence, open questions, sources.
  This is what /autonomous consumes, handoffs reference, user reads.
  Persist: research_contract.json has full state for /resume_handoff.


STATE

  continuum/research/{topic}/
    research_contract.json    hypotheses + lifecycle + iteration history
    findings.md               final synthesis (what others consume)
    artifacts/                per-hypothesis per-iteration evidence files
    reports/                  worker reports (compact JSON)
    validation/               iteration validation results
    ouros session state       REPL heap (all raw data, persisted by harness)


RESUME

Read research_contract.json. Show: hypotheses N/total, avg confidence, iterations done.
Load ouros session: --list-vars. Show accumulated variable count.
Continue from last EVOLVE decision gate.


RULES

Never research directly — workers run Ouros programs
Ouros is the research medium — results stay in heap, only artifacts cross back
Workers share via sessions — --load for sequential, --fork for parallel
Hypotheses before search — know what you're looking for
Primary sources — papers, docs, repos. Not blogs, not LLM summaries.
Confidence is earned — evidence count + quality justifies the score
Knowledge accumulates — Ouros session grows, orchestrator context stays flat
Each iteration deeper not wider — wider = new hypotheses, deeper = more evidence
Write to bloks — future sessions benefit: bloks new rule "{finding}" --tags {topic}
Budget-aware — synthesize at 75% context, hard stop at 85%
Max iterations: focused 5, exploratory 10, comparative 7. Then force-synthesize.
