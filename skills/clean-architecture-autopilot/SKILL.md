---
name: clean-architecture-autopilot
description: Orchestrator skill that drives the full Clean Architecture pipeline from requirement to accepted code. Manages a 5-phase state machine, dispatches the five role agents, injects the right methodology skill per phase, runs two quality gates (Dependency Rule audit + full architecture review), routes REVISE/FAIL verdicts with bounded feedback loops, and augments each phase with matching "superpowers" skills/agents. Use when the user wants an end-to-end, gated Clean-Architecture-driven build rather than running each agent by hand.
version: 1.0.0
---

# Clean Architecture Autopilot (Orchestrator)

This is the single control entry point that turns the six methodology skills and
five role agents in this repo into an executable, quality-gated pipeline. It owns
the state machine, the artifact contracts between phases, the two gates, the
feedback routing, and the user-decision loop. It also declares, per phase, which
**superpowers** skills/agents to layer on for extra rigor.

Read `pipeline/orchestration.md` for the DAG diagram; this skill is its runnable
specification.

---

## State Machine

```
INIT → P0_RESEARCH? → P1_REQUIREMENTS → P2_DESIGN → G3_DEP_AUDIT ─┐
                                             ▲                     │APPROVED
                                             │REVISE_REQUIRED(≤2)  ▼
                                             └──────────── P4_IMPLEMENT → G5_REVIEW ─┐
                                                                 ▲                    │PASS
                                    code fix│structural fix       │FAIL / CONCERNS    ▼
                                            └────────────────────┘              P6_FINISH → DONE
```

State variables the orchestrator tracks:
`{phase, artifacts{}, gate3_iterations, gate5_iterations, open_questions[], debts[]}`.

Transition rules:
- Enter a phase only when its required input artifact keys exist and validate.
- A gate emits a verdict; the orchestrator routes on verdict, never skips a gate.
- Bounded loops: `gate3_iterations ≤ 2`, `gate5_iterations ≤ 2`. On overflow →
  pause and enter the USER LOOP.

---

## Phase Dispatch Table

For each phase: which role agent to dispatch, which local methodology skill to
inject, and which **superpowers** skill(s)/agent(s) augment it.

### P0 — Research (optional, only if working inside an existing codebase)
- Local skill: —
- Superpowers skill: `find-skills` (discover better-fit skills first), `context7`
  (fetch framework docs only when a tech is already fixed).
- Superpowers agent: **Explore** ("very thorough" for import/dependency mapping),
  **Autopilot Researcher** (existing patterns, sibling naming, API conventions).
- Exit: a `codebase_notes` artifact (existing layers, boundaries already present,
  naming conventions). Skip entirely for greenfield.

### P1 — Requirements → Entities/Use Cases
- Role agent: `agents/requirements-analyst.md`
- Local skill: `use-case-extraction`
- Superpowers skill: **`brainstorming`** (mandatory — explore intent/requirements
  before modeling), **`feature-spec`** (turn fuzzy asks into scoped requirements /
  handle scope & change requests).
- Superpowers agent: general-purpose for parsing large PRDs.
- Exit artifact: `{entities, use_cases, deferred_details, open_questions}`.

### P2 — Layered Design
- Role agent: `agents/architecture-designer.md`
- Local skills: `layer-boundaries`, `component-principles`, `solid-principles`
- Superpowers skill: **`writing-plans`** (structure the design as an executable
  plan with DAG tasks), **`plan-eng-review`** (eng-manager-mode review of the
  architecture/data-flow/edge-cases before it is locked).
- Superpowers agent: **Plan** (software-architect plan), **Autopilot Designer** /
  **Autopilot Planner** (produce DAG task plan with per-task model routing).
- Exit artifact: `{layer_map, ports, boundary_dtos, boundary_choices,
  component_map, directory_tree, design_doc}`.

### G3 — Dependency Rule Audit (GATE)
- Role agent: `agents/dependency-auditor.md`
- Local skills: `dependency-rule`, `component-principles`
- Superpowers skill: **`ast-code-analysis-superpower`** (ast-grep structural rules
  to mechanically detect outward imports / layer-boundary violations / cycles —
  turns the audit from eyeballing into a repeatable scan).
- Superpowers agent: **Explore** (grep every import against the layer map).
- Verdict: `APPROVED` → P4; `REVISE_REQUIRED` → P2 (increment `gate3_iterations`).

### P4 — Implementation (inside-out, per component)
- Role agent: `agents/clean-implementer.md`
- Local skills: `dependency-rule`, `solid-principles`, `layer-boundaries`
- Superpowers skill: **`test-driven-development`** (write entity/use-case tests
  first — they need no DB/UI), **`executing-plans`** / **`subagent-driven-development`**
  (drive the P2 plan), **`dispatching-parallel-agents`** (fan out per independent
  component — safe because the graph is a DAG), **`using-git-worktrees`** (isolate
  parallel component work), **`systematic-debugging`** / **`investigate`** (root-cause
  on any failure, no fixes without a cause), **`verification-before-completion`**
  (evidence before claiming a layer done).
- Superpowers agent: **Autopilot Implementer** (self-verifying single-task
  implementer with 4-state status), one per DAG task.
- Exit artifact: `{files, tests, status, concerns, debts}` per component.

### G5 — Architecture Review (GATE)
- Role agent: `agents/architecture-reviewer.md`
- Local skill: `architecture-review-checklist` (+ the three deep-dive skills)
- Superpowers skill: **`requesting-code-review`** (frame the review),
  **`ast-code-analysis-superpower`** (re-run structural scans on the actual code),
  **`codex`** (adversarial "try to break it" pass on business rules),
  **`review`** (pre-landing diff review for SQL/side-effect/structural issues).
- Superpowers agent: **Autopilot Code Reviewer** (spec_stage first — dependency
  rule/contracts; quality_stage second — SOLID/tests/security).
- Verdict: `PASS` → P6; `PASS_WITH_CONCERNS` → P6 with logged `debts` (needs user
  sign-off); `FAIL` → route BLOCKERs to P4 (code) or P2 (structural), increment
  `gate5_iterations`.

### P6 — Finish
- Local skill: —
- Superpowers skill: **`receiving-code-review`** (process any human feedback with
  rigor, not blind agreement), **`finishing-a-development-branch`** (merge/PR/cleanup
  decision), optionally **`ship`** if the user wants deploy.
- Exit: accepted, integrated work + a summary of `debts`/follow-ups.

---

## Artifact Contract (must validate at each hand-off)

| Hand-off | Required keys |
|---|---|
| P1→P2 | `entities`, `use_cases`, `deferred_details` |
| P2→G3 | `layer_map`, `ports`, `component_map`, `directory_tree` |
| G3→P4 | verdict `APPROVED` + all P2 artifacts |
| P4→G5 | `files`, `tests`, `status` (per component) |
| G5→P6 | verdict `PASS` or `PASS_WITH_CONCERNS`(+`debts`) |

If a required key is missing or malformed, do NOT advance — return the phase to its
producing agent with a `NEEDS_CONTEXT` note.

---

## USER LOOP — when the orchestrator must pause and ask

1. P1 produced `open_questions` about a business rule / actor.
2. A concrete technology must be chosen (DB/framework/UI) that design kept behind a
   port — surface options, don't decide silently.
3. `gate3_iterations` or `gate5_iterations` exceeded 2 → the axis of change or a
   boundary is genuinely ambiguous; escalate.
4. G5 wants to accept a MAJOR finding as `debt` → require explicit sign-off.

Use one structured multiple-choice question per decision; keep options mutually
exclusive.

---

## Guardrails

- Never skip a gate; never let a gate self-approve without emitting evidence-backed
  findings.
- Superpowers augmentation is *additive* — it never overrides the Dependency Rule
  or the local methodology skills. If a superpowers skill's suggestion conflicts
  with the Dependency Rule, the Dependency Rule wins and the conflict is logged.
- Keep concretions wired only in `main` even when `ship`/deploy skills run.
- Parallel fan-out (P4) only after G3 `APPROVED` — a non-DAG graph must not be
  parallelized.

## Orchestrator Output (running log)

```
{
  phase, verdicts:{g3, g5},
  artifacts_index:{p1,p2,p4},
  injections:{ methodology_skills[], superpowers_used[], agents_dispatched[] },
  loops:{gate3_iterations, gate5_iterations},
  debts[], open_questions[], next_action
}
```

## Run Log & Audit Trail (MANDATORY — persist to disk)

The orchestrator MUST persist a durable, append-only trail for every run so the
work can later be audited and the pipeline tuned. Logging is not optional and must
never be overwritten.

### Directory layout (one folder per run, centralized under .cc-skill/)
```
.cc-skill/                            # 统一日志根目录（项目根下；只追加，不覆盖）
  <task-slug>/                        # 每个任务一个子目录，名字=任务简要介绍(slug)
                                      #   e.g. place-order / add-refund-flow / user-signup
    run.jsonl                        # append-only event log (one JSON per line)
    manifest.json                    # run header: id, task_title, start/end, requirement digest, config
    artifacts/
      p1-requirements.json           # snapshot of each phase's exit artifact
      p2-design.json
      g3-audit.json                  # gate verdict + violations + evidence
      p4-<component>.json            # one per implemented component
      g5-review.json                 # gate verdict + findings
    summary.md                       # human-readable recap generated at DONE
```
Naming rules for `<task-slug>`:
- Derive from the task's brief description: lowercase, hyphen-separated, ASCII where
  possible (e.g. "Place Order" → `place-order`); keep it short (≤ 40 chars).
- The full human title goes into `manifest.json.task_title`; the slug is only the
  folder name.
- Collision handling: if `.cc-skill/<slug>/` already exists, append a short suffix
  `-<YYYYMMDD-HHMMSS>` to keep runs distinct (never overwrite a prior run's folder).
- Default root: `.cc-skill/` at the project root. If the project is read-only, fall
  back to the user's output folder and record the resolved absolute path in
  `manifest.json`.

### run.jsonl event schema (append one line per event)
```
{ "ts":"ISO-8601", "run_id":"...", "seq":N, "phase":"P2|G3|...",
  "event":"phase_enter|phase_exit|agent_dispatch|skill_inject|
           superpower_used|gate_verdict|loop_increment|user_loop|
           conflict_logged|artifact_written|error",
  "agent":"...", "skills":[...], "superpowers":[...],
  "verdict":"APPROVED|REVISE_REQUIRED|PASS|PASS_WITH_CONCERNS|FAIL|null",
  "detail":{...}, "duration_ms":N }
```
Rules:
- Emit `phase_enter`/`phase_exit` around every phase; `gate_verdict` at each gate;
  `loop_increment` whenever `gate3/5_iterations` rises; `user_loop` on every pause;
  `conflict_logged` whenever a superpowers suggestion is overridden by the
  Dependency Rule.
- Never mutate a prior line — the log is append-only (use `>>`, not rewrite).
- Redact secrets: never write API keys/tokens/passwords into the log or artifacts.

### summary.md (written at DONE — the tuning artifact)
Must include: final verdicts; `gate3_iterations` / `gate5_iterations` (how many
rounds each gate needed); the list of superpowers actually used vs skipped; all
`debts` accepted with sign-off; all `open_questions` and how they were resolved;
per-phase wall-clock; and a short "what to tune next time" note (e.g. a gate that
looped repeatedly signals an ambiguous boundary upstream).

### How to use it for review & tuning
- **核对 (audit)**: replay `.cc-skill/<task-slug>/run.jsonl` to see exactly which
  agent/skill ran when, every gate verdict, and every user decision — a full
  provenance trail.
- **调优 (tune)**: aggregate `summary.md` across all task subfolders under
  `.cc-skill/` to spot patterns — gates that loop often (design smell), superpowers
  that never help (drop them), phases that dominate wall-clock (parallelize or route
  to a cheaper model).

