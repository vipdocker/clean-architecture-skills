---
name: clean-architecture-autopilot
description: Orchestrator skill that drives the full Clean Architecture pipeline from requirement to accepted code. Manages a 5-phase state machine, dispatches the five role agents, injects the right methodology skill per phase, runs two quality gates (Dependency Rule audit + full architecture review), routes REVISE/FAIL verdicts with bounded feedback loops, and augments each phase with matching "superpowers" skills/agents. Use when the user wants an end-to-end, gated Clean-Architecture-driven build rather than running each agent by hand. Not for applying a single methodology skill in isolation (use that skill directly), for retrospectively tuning a finished run (use ca-process-tuning), or for reviewing code without building it (use ca-architecture-review-checklist).
---
<!-- clean-architecture system v1.13.0 -->

# Clean Architecture Autopilot (Orchestrator)

This is the single control entry point that turns the six methodology skills and
five role agents in this repo into an executable, quality-gated pipeline. It owns
the state machine, the artifact contracts between phases, the two gates, the
feedback routing, and the user-decision loop. It also declares, per phase, which
**superpowers** skills/agents to layer on for extra rigor.

Read `pipeline/orchestration.md` for the DAG diagram; this skill is its runnable
specification.

---

## Step 0 — Bootstrap (RUN THIS FIRST, before P0/P1 — non-negotiable)

Logging is a **mechanical command**, not a thing to remember. The FIRST action of
this skill, before any analysis, is to create the run folder and checkpoint by
running the bundled helper. If you skip this, no `.cc-skill/` will exist and the
run is non-resumable — so do it immediately.

```bash
# resolve <project_dir> (the repo you are building in) and a short task title
python3 "$(dirname "$0")/scripts/cc_log.py" init \
  --root "<project_dir>" --title "<one-line task title>"
# → prints the run dir, e.g. <project_dir>/.cc-skill/<task-slug>/
```
(The skill lives at `~/.qoderwork/skills/clean-architecture-autopilot/`; the helper
is at `scripts/cc_log.py` there. Use that absolute path if `$0` is unavailable.)

Then, at **every** phase enter/exit and **every** gate verdict, call:
```bash
python3 .../scripts/cc_log.py event --root "<project_dir>" --slug "<task-slug>" \
  --phase P2 --event phase_enter --agent ca-architecture-designer \
  --skills ca-layer-boundaries,ca-component-principles --status in_progress
python3 .../scripts/cc_log.py event --root "<project_dir>" --slug "<task-slug>" \
  --phase G3 --event gate_verdict --verdict APPROVED --status in_progress
```
This writes `state.json` (current position, overwritten) AND appends `run.jsonl`
(history) in one atomic call. At DONE, call `cc_log.py summary --body-file <md>`.

**Enforcement rule:** you may not enter a phase whose `phase_enter` event has not
been logged, and you may not advance past a gate whose `gate_verdict` has not been
logged. If `.cc-skill/<slug>/` does not exist when you reach P1, STOP and run
`init` first. Snapshots of each phase's exit artifact go under
`.cc-skill/<slug>/artifacts/` (write them with your normal file tools).

**The latch is now mechanical, not just prose.** `cc_log.py event` refuses to
record `phase_enter P4` unless `gate_verdicts.g3` is `APPROVED` (P6 likewise
needs a passing `g5`; a recorded REVISE/FAIL does **not** open the gate) and
exits 2 without writing anything. A deliberate bypass needs `--force`, which
appends a MAJOR `process_violation` event plus a `debts` entry before the
`phase_enter` — so skipping a gate is possible but never silent. This exists
because the first production run entered P4 with `g3` still null and it surfaced
only when the user thought to ask.

**Gate verdicts go stale — but scoped re-entry avoids full invalidation.**
Re-entering P2 voids `g3`; re-entering P4 **with `--scope`** does NOT void the
full `g5` verdict — it marks the specific components as needing delta review while
preserving the prior verdict for unaffected components. Only a **full** P4
re-entry (no scope, meaning all components are re-implemented) voids `g5` entirely.
`cc_log.py` handles both cases: scoped re-entry logs a `gate_scope_narrowed` event
(listing affected components); unscoped re-entry logs `gate_invalidated` as before.
**P6 entry gate latch (mechanically enforced by `cc_log.py`):**
P6 `phase_enter` requires ALL three conditions:
1. `gate_verdicts.g5` ∈ {`PASS`, `PASS_WITH_CONCERNS`}
2. `g5_delta_scopes` is empty (absent or `[]`)
3. `state.debts` is empty (`PASS_WITH_CONCERNS` debts were signed off by the user)

If condition 1 passes but condition 2 or 3 fails, `cc_log.py` refuses the transition
with exit 2 — exactly as it does for a missing verdict. `--force` can bypass (with
a logged `process_violation`). Condition 2 mechanically prevents the "run 2"
pattern where a scoped re-entry preserves the old verdict and P6 sails through
without the promised delta review ever running; condition 3 stops P6 before finish
work begins instead of discovering at `phase_exit` that nobody authorized the debt.

From run 2's lesson: P4 was re-entered after G5 passed, the promised delta review
never happened, and P6 closed on a verdict that predated ~2300 new lines. The
scoped mechanism + P6 latch prevents this by tracking exactly which components need
re-review and refusing to advance until they are cleared.

**Closing a phase is now as gated as opening one (run 3).** That run closed P6 at
01:34, then a live-verification FAIL reopened P4 twice and a *second* `P6
phase_exit` was appended with no matching entry — while `completed_phases` still
listed P6 from the first closure. Four latches now cover the exit side:

1. **`phase_exit` needs an open `phase_enter`.** Exits may not outnumber entries;
   a second closure after corrective work means the re-entry is what was missing.
2. **Re-entering P2/P4 retracts downstream phases** from `completed_phases` and logs
   `phase_reopened`. Voiding the gate verdict alone left the phase ledger claiming
   the run had finished.
3. **P6 may not close over unsigned debts.** `PASS_WITH_CONCERNS` means "passing,
   with debts the user accepts"; the sign-off is recorded with a `debt_signoff`
   event, which moves `state.debts` into `state.debts_signed_off`. Run 3 closed P6
   twice with two debts outstanding and `state.debts` empty.
4. **`PASS_WITH_CONCERNS` must name its debts** in `--detail`
   (`debts_awaiting_signoff[]`). A PWC that names nothing is indistinguishable from
   PASS and skips the sign-off silently.

**state.json is maintained, not a snapshot.** It used to be written at `init` and
then only `current_phase` / `phase_status` / `gate_verdicts` / `completed_phases`
were ever touched, so run 3 finished with `p4_components: []` after 11
`component_done` events, `artifact_pointers: {}` after 5 artifacts, `debts: []`
beside two unsigned debts, `loops.gate5_iterations: 0` after two real G5 rounds, and
`next_action: "run P0/P1"` — the exact string a post-compression resume would have
acted on. `cc_log.py` now folds every event into the matching field, which also
makes the documented loop cap (2 per gate) enforceable: a third `loop_increment`
is refused and points at the user escalation instead.

**Unlogged gaps are annotated.** Run 3 has 6h18m of silence between two events with
no marker, which left P6's `duration_ms` reading 398 minutes of "work". A gap over
30 minutes now appends a `pause` event, and phase durations subtract recorded
pauses. Long pauses are legitimate; unmeasurable ones are not.

**The user loop was the last unlatched event — and the debt sign-off was
self-issuable.** Every neighbour had a mechanical precondition while `user_loop`
required nothing at all, which is why the three runs each handled questions
differently and the best of them never recurred: run 1 asked a question the user
cancelled with "继续"; run 2 batched 4 questions and resolved 3 more off disk,
recording that in an ad-hoc `adopted_without_asking` field this repo never defined;
run 3 asked nothing across 48 events. Worse, `debt_signoff` cleared
`state.debts` unconditionally — no preceding question, no record of what the user
said — so the v1.5.0 "P6 may not close over unsigned debts" latch actually verified
that *a sign-off event existed*, not that anyone agreed. An agent could sign its
own MAJOR findings and walk through P6. Both are now latched: see the USER LOOP
section for the trigger taxonomy, the 4-question batch cap, the
`resolution_attempted[]` requirement on information gaps, and the exact sign-off
chain: an ask after the current PWC names the current debts; the sign-off repeats
that debt list, quotes the answer, and references the ask's `seq`. The logger proves
that order and identity of subject, not cryptographic authorship — the orchestrator
must never fabricate the quoted answer.

### If `cc_log.py` itself cannot run (fallback — this happens)

The script needs a working shell. When the shell is unavailable, **hand-write the
same files with your normal file tools** rather than skipping the log:

1. First, append one `superpower_unavailable` event naming `scripts/cc_log.py`.
2. `run.jsonl` — append-only. Never edit or reorder an existing line; keep `seq`
   monotonic by reading the last line first.
3. `state.json` — rewrite the whole file each time.
4. **You now own the latch the script would have enforced**: before writing
   `phase_enter P4`, read `gate_verdicts.g3`; if it is null, write the
   `process_violation` event yourself instead of quietly proceeding. The same
   applies to `debt_signoff` — confirm the preceding `user_loop` follows the
   current PWC and names the exact `state.debts`; repeat that list, reference the
   ask's `user_loop_seq`, and record the user's `answer`, or you are signing their
   name for them.

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
`{phase, artifacts{}, gate3_iterations, gate5_iterations, open_questions[], debts[],
question_ledger{asked, self_resolved}}`.

Transition rules:
- Enter a phase only when its required input artifact keys exist and validate.
- A gate emits a verdict; the orchestrator routes on verdict, never skips a gate.
- Bounded loops: `gate3_iterations ≤ 2`, `gate5_iterations ≤ 2`. On overflow →
  pause and enter the USER LOOP.

---

## Phase Dispatch Table

For each phase: which role agent to dispatch, which local methodology skill to
inject, and which **superpowers** skill(s)/agent(s) augment it.

### Superpowers availability rule (applies to every phase below)

A superpowers skill/agent is an **optional amplifier, never a prerequisite**.
Environments differ in which ones are installed, so a missing augmentation must
never stall a phase. When one named below cannot be invoked:

1. Apply the **Fallback** line in that phase's entry. Each fallback preserves the
   *intent* of the augmentation using only this skill's own instructions.
2. Log it once so the gap becomes data rather than a silent degradation:
   ```bash
   python3 .../scripts/cc_log.py event --root "<project_dir>" --slug "<task-slug>" \
     --phase P4 --event superpower_unavailable \
     --detail '{"name":"test-driven-development","fallback":"applied"}'
   ```
   `ca-process-tuning` reads these events to score augmentation ROI and to decide
   which references to keep, promote, or drop.
3. Never weaken a gate, skip a phase, or bend the Dependency Rule to compensate.

**Log the dispatch, not just the completion — this is now mechanical.**
`cc_log.py` refuses a `component_done` that has no matching `agent_dispatch` for the
same component. That one event buys two things, both missing from all three
production runs, which emitted zero of them:

1. **Cost.** The dispatch is the component's start time, so `component_done` gets a
   real `duration_ms` (annotated pauses subtracted), mirrored into
   `state.p4_components[].duration_ms`. Without it, P4 — 79% of run 3's effective
   work — could only be attributed to batch windows, and "which component is
   expensive" was unanswerable.
2. **ROI.** `--agent`, `--skills` and `--superpowers` ride on this event.
   `ca-process-tuning` scores augmentation by separating "fired and visibly helped"
   from "fired and changed nothing" from "was never installed"; with only the
   `superpower_unavailable` side recorded it cannot tell the first two apart, and the
   feedback loop the run log exists for goes dark.

```bash
python3 .../scripts/cc_log.py event --root "<project_dir>" --slug "<task-slug>" \
  --phase P4 --event agent_dispatch --agent ca-clean-implementer \
  --skills ca-dependency-rule,ca-layer-boundaries \
  --superpowers test-driven-development,using-git-worktrees \
  --detail '{"component":"ordering","worktree":"p4/place-order/ordering"}'
```

Dispatching a wave means one such event per component, logged before the work starts.
The sum of component durations divided by that phase's wall clock is then the
**parallelism ratio**: ≈1 means the wave ran serially despite the plan, >1 means the
overlap was real. That ratio is the only way to confirm a plan change meant to
parallelize actually did.

**Waves are a dependency-legality statement, not an execution promise (as of
v1.13).** Runs 6 and 7 both planned multi-component waves and executed strictly
serially in a single session — the orchestrator here has no multi-session fan-out,
so a planned wave only certifies that those components *may* run concurrently
(files pairwise disjoint). Treat it that way: don't invest in worktrees for a
parallelism that the execution environment won't deliver, and when components
execute inline in one session, log the dispatch/done pair per component anyway
(backfilled at completion is acceptable — record `"note":"backfilled, single
session"` in the dispatch detail) so per-component cost stays measurable. The
report's planned-vs-actual overlap row remains the honest signal: it tells you
whether the environment actually parallelized, which is a property of the
environment, not of the plan.

Only a missing **local** methodology skill blocks a phase — those are the load
bearing ones and they ship in this repo.

### P0 — Research (optional, only if working inside an existing codebase)
- Local skill: —
- Superpowers skill: `find-skills` (discover better-fit skills first), `context7`
  (fetch framework docs only when a tech is already fixed).
- Superpowers agent: **Explore** ("very thorough" for import/dependency mapping),
  **Autopilot Researcher** (existing patterns, sibling naming, API conventions).
- Fallback: without `find-skills`, pick candidates from the skill list already
  injected in context; without `context7`, read the framework's own docs or
  vendored source in-repo.
- Exit: a `codebase_notes` artifact (existing layers, boundaries already present,
  naming conventions). Skip entirely for greenfield.

### P1 — Requirements → Entities/Use Cases
- Role agent: `agents/ca-requirements-analyst.md`
- Local skill: `ca-use-case-extraction`
- Superpowers skill: **`brainstorming`** (mandatory — explore intent/requirements
  before modeling), **`feature-spec`** (turn fuzzy asks into scoped requirements /
  handle scope & change requests).
- Superpowers agent: general-purpose for parsing large PRDs.
- Exit artifact: `{entities, use_cases, deferred_details, open_questions}`.

### P2 — Layered Design
- Role agent: `agents/ca-architecture-designer.md`
- Local skills: `ca-layer-boundaries`, `ca-component-principles`, `ca-solid-principles`
- Superpowers skill: **`writing-plans`** (structure the design as an executable
  plan with DAG tasks), **`plan-eng-review`** (eng-manager-mode review of the
  architecture/data-flow/edge-cases before it is locked).
- Superpowers agent: **Plan** (software-architect plan), **Autopilot Designer** /
  **Autopilot Planner** (produce DAG task plan with per-task model routing).
- Bundled tool (EXECUTABLE): `scripts/design_coverage.py` — set difference between
  the authoritative design source's sections and what the exit artifact carries:
  ```bash
  python3 .../scripts/design_coverage.py --design docs/specs/<design>.md \
    --artifact .cc-skill/<slug>/artifacts/p2-design.json
  ```
  `cc_log.py` refuses `phase_exit P2` unless `--detail` names a `design_source`
  (or `"none"` with a reason) and, when a source is named, the check passes.

  Run 3 is the case: the design source's `## 8. 前端约束` required reusing
  `StockSearchWidget`, and `p2-design.json` mentioned search/搜索/widget zero
  times. G3 approved the lossy copy — a gate cannot audit a constraint it cannot
  see — so the omission only surfaced at G5, as a MAJOR (68 of 108 watchlist
  symbols unreachable from the UI, including the spec's own worked example),
  costing a corrective P4 round of 29 minutes plus an unplanned component.
- Bundled tool (EXECUTABLE): `scripts/plan_graph.py` — the Dependency Rule applied
  to the PLAN, plus critical-path depth:
  ```bash
  python3 .../scripts/plan_graph.py \
    --artifact .cc-skill/<slug>/artifacts/p2-design.json
  ```
  A presentation task (templates/static/UI only) that blocks on a server
  implementation is the outer layer reaching for a concretion instead of the
  abstraction — the same violation `ca-dependency-rule` forbids in code, one level up.
  `cc_log.py` refuses `phase_exit P2` on an unwaived finding.

  Run 3's cost: `T8 frontend` declared `depends_on: ["T6"]` (the route
  implementation), putting it at **depth 5** behind T2→T4→T5→T6. It only needed the
  response contract, which P2 had already defined (`### 5.6 响应契约` in the source,
  `ports_and_boundaries.boundary_dtos` in the artifact) and which is therefore
  available at t=0 — depth 1. The backend chain T1→T6 finished in 25.7 minutes
  while the frontend waited, and the frontend turned out to be the expensive half
  of the run. Fix: `depends_on: []` + `consumes_contract: ["<dto>"]`. Genuine cases
  (a server-rendered variable that does not exist until the route is written) go in
  `plan_serialization_waived: [{task, depends_on, reason}]`.

  Depth is structure, not time: the check proves an edge is unnecessary, but the
  minutes it costs need per-task durations (`agent_dispatch` paired with
  `component_done`), which run 3 did not emit.
- **Where the plan lives is a contract, not a per-run discovery.** `cc_log.py`
  refuses `phase_exit P2` when no plan can be checked: the exit artifact carries
  no `dag_tasks` AND no pointer was given. Run 4 invented `p4-components.json`
  (component-keyed, no `dag_tasks` key) and left `p2-design.json` empty, so every
  plan check read a planless artifact as "satisfied". Keep the tasks in the exit
  artifact, or declare `"plan_artifact": "artifacts/p4-components.json"` in the
  exit `--detail` (the checker reads both the contract shape and the
  component-keyed shape). A task with genuinely no components declares
  `"planless": true`.
- Exit artifact: `{layer_map, ports, boundary_dtos, boundary_choices,
  component_map, directory_tree, design_doc, design_source, sections_covered,
  sections_out_of_scope, identifiers_waived, plan_serialization_waived}`. When the
  design decomposes into DAG tasks, each task must also declare `files_touched[]` —
  run 2 hit a parallel write conflict and a stale cross-wave assignment (both
  MAJOR-adjacent) because dispatch had no touch-sets to check overlap against — and
  a presentation task declares `consumes_contract[]` in place of a dependency on
  the implementation.
- **Every DAG task carries its own tests** in `files_touched[]`; never plan a
  trailing "tests" task. Run 3 planned 8 tasks with no test task, shipped 6 of 8
  components with no tests, and let an unplanned `T9_tests` sweep find 2 bugs after
  everything was marked DONE — the batched Red-Green discipline in P4 cannot hold
  if the plan itself defers tests to the end.

### G3 — Dependency Rule Audit (GATE)
- Role agent: `agents/ca-dependency-auditor.md`
- Local skills: `ca-dependency-rule`, `ca-component-principles`
- Bundled tool (EXECUTABLE): `scripts/dep_graph.py` — AST import graph + Tarjan
  SCC scan. Runs 1 and 2 each rewrote this from scratch (~500 lines per run);
  use the bundled one and spend the effort judging the graph instead:
  ```bash
  python3 .../scripts/dep_graph.py --root "<project_dir>" \
    --scan-dirs modules --root-files app.py,main.py \
    --focus <new_component_prefix> --json .cc-skill/<slug>/g3-graph.json
  ```
  It sees inline/function-level imports (run 2's V2 hid there — line-start grep
  misses them) and root-level composition files (V6's hiding spot). Exit 1 when
  the focused component sits in any cycle. Layer direction judgement remains the
  auditor's — the tool reports edges, it does not know the layer map.
- Superpowers skill: **`ast-code-analysis-superpower`** (ast-grep structural rules
  to mechanically detect outward imports / layer-boundary violations / cycles —
  turns the audit from eyeballing into a repeatable scan).
- Superpowers agent: **Explore** (grep every import against the layer map).
- Verdict: `APPROVED` → P4; `REVISE_REQUIRED` → P2 (increment `gate3_iterations`).

### P4 — Implementation (inside-out, per component)
- Role agent: `agents/ca-clean-implementer.md`
- Local skills: `ca-dependency-rule`, `ca-solid-principles`, `ca-layer-boundaries`
- Superpowers skill: **`test-driven-development`** (drives **batched Red-Green**
  TDD — write tests for a cohesion group, confirm batch RED, implement, confirm
  batch GREEN; inner-layer tests need no DB/UI, proving isolation),
  **`executing-plans`** / **`subagent-driven-development`** (drive the P2 plan),
  **`dispatching-parallel-agents`** (fan out per independent component — safe
  because the graph is a DAG), **`using-git-worktrees`** (isolate parallel
  component work), **`systematic-debugging`** / **`investigate`** (root-cause on
  any failure, no fixes without a cause), **`verification-before-completion`**
  (evidence before claiming a layer done).
- Parallel dispatch rule: before fanning out a wave, intersect the
  `files_touched[]` of its tasks pairwise. Tasks sharing any file go to one
  agent (or run serially) — and a task must not be dispatched if an earlier
  wave already modified its files without folding those changes in.
- Superpowers agent: **Autopilot Implementer** (self-verifying single-task
  implementer with 4-state status), one per DAG task.
- **Test execution policy (batched TDD + deduplication):**
  - Each Implementer sub-agent uses **batched Red-Green** within a layer: write
    tests for a cohesion group (3–8 behaviors), confirm batch RED in one run,
    implement, confirm batch GREEN in one run. REFACTOR only re-runs if structure
    truly changed. This replaces per-unit RED→GREEN→REFACTOR 3× execution.
  - **Layer self-verify** runs only incremental tests (new/affected in this layer).
    Tests from inner layers already confirmed GREEN are not re-executed — inner
    code is immutable by definition. The grep-imports structural check still
    covers all layer files.
  - **Merge (worktree join)** runs only boundary/integration tests and tests whose
    sources import changed files — not a full unit-test sweep. Each test executes
    at most once in its originating batch, then only if its covered code changes.
- Fallback (this phase depends on the most augmentations, so each intent has an
  explicit substitute):
  - no `test-driven-development` → still follow batched Red-Green: write tests for
    the cohesion group **before** implementation (test doubles for every port);
    confirm batch RED, implement, confirm batch GREEN. Tests must pass with no
    DB/UI/network.
  - no `executing-plans` / `subagent-driven-development` → implement the P2 DAG
    tasks serially in topological order, one component per step.
  - no `dispatching-parallel-agents` / `using-git-worktrees` → stay serial on the
    current branch. G3 already proved the graph is acyclic, so ordering alone
    keeps this correct; only wall-clock is lost.
  - no `systematic-debugging` / `investigate` → on any failure, state the suspected
    cause in one sentence and confirm it with a failing test or a log line before
    editing code.
  - no `verification-before-completion` → a component counts as done only after
    (a) its imports are checked against the layer map and (b) its incremental tests
    pass; record the command output as the evidence.
- Exit artifact: `{files, tests, status, concerns, debts}` per component.

### G5 — Architecture Review (GATE)
- Role agent: `agents/ca-architecture-reviewer.md`
- Local skill: `ca-architecture-review-checklist` (+ the three deep-dive skills)
- Superpowers skill: **`requesting-code-review`** (frame the review),
  **`ast-code-analysis-superpower`** (re-run structural scans on the actual code),
  **`codex`** (adversarial "try to break it" pass on business rules),
  **`review`** (pre-landing diff review for SQL/side-effect/structural issues).
- Superpowers agent: **Autopilot Code Reviewer** (spec_stage first — dependency
  rule/contracts; quality_stage second — SOLID/tests/security).
- **Delta review mode:** When G5 runs after a targeted fix (not a full P4 re-run),
  it operates as a **delta review** — only the components/layers that changed since
  the last verdict are re-examined. Previously-passing sections retain their prior
  score. This avoids full-codebase re-review on localized fixes.
- **Precise failure routing:** On `FAIL`, each BLOCKER finding carries an explicit
  `scope` (component name + layer). The orchestrator routes the fix back to the
  specific component/layer in P4, not the entire phase. Only the affected scope is
  re-implemented; unaffected components retain their `DONE` status.
- **No passing verdict without observation.** For any component with
  runtime-visible behavior (endpoint, UI, background job), a `PASS` /
  `PASS_WITH_CONCERNS` asserts the behavior was *observed*, not merely that the
  structure reads correctly. If the only evidence is stubbed or mocked, record a
  BLOCKER naming the missing verification and return `FAIL`. **Logging the gap as a
  debt does not discharge it** — run 3's round-2 review listed `stubbed state
  verification` among its own accepted debts, passed anyway, and live browser
  verification 6h18m later found a MAJOR that stubs could not surface by
  construction (a per-symbol coverage 503 escalated into a page-level blocker and
  aborted the whole results table). Deferring a fix is a decision; deferring the
  observation is a guess.
- Verdict: `PASS` → P6; `PASS_WITH_CONCERNS` → P6 only after the named debts are
  signed off by the user (`--detail '{"debts_awaiting_signoff":[...]}'` on the
  verdict, then a `debt_signoff` event — the P6 *entry* latch enforces it, with the
  exit latch retained as defense in depth); `FAIL` →
  route BLOCKERs to P4 (code) or P2 (structural) with precise scope, increment
  `gate5_iterations` (capped at 2, then escalate to the user).

### P6 — Finish
- Local skill: —
- Superpowers skill: **`receiving-code-review`** (process any human feedback with
  rigor, not blind agreement), **`finishing-a-development-branch`** (merge/PR/cleanup
  decision), optionally **`ship`** if the user wants deploy.
- **Processing report (MECHANICAL, required):** before `phase_exit P6`, generate
  `.cc-skill/<slug>/report.md`:
  ```bash
  python3 .../scripts/cc_log.py report --root "<project_dir>" --slug "<task-slug>"
  ```
  It assembles the closing record from the logs — never hand-written: wall clock,
  per-phase and per-component durations (recorded vs raw, with ⚠ on divergence),
  P4 parallelism ratio, user wait per pause, idle pauses, gate verdicts and
  iterations, signed-off debts, question ledger, and the git change list diffed
  against the baseline commit `init` recorded in the manifest.
  `cc_log.py` refuses `phase_exit P6` without it. Runs 3–5 each finished with a
  different hand-assembled artifact (or none); optional closing steps drift, so
  this one is gated like every other artifact.
- Exit: accepted, integrated work + `summary.md` (prose) + `report.md`
  (mechanical) + a summary of `debts`/follow-ups.

---

## Injection Policy (per-layer skill scoping — reduces token overhead)

P4 dispatches multiple Implementer sub-agents (one per component/layer). To
minimize input tokens per spawn, inject **only the relevant subset** of
methodology skills — not the full text of every skill in the system.

### Per-layer injection matrix

| Current layer being implemented | Inject (full text) | Inject (rules summary only) |
|---|---|---|
| Entities | `ca-dependency-rule` | TDD rules summary (below) |
| Use Cases | `ca-dependency-rule` | TDD rules summary (below) |
| Interface Adapters | `ca-dependency-rule`, `ca-layer-boundaries` | TDD rules summary (below) |
| Frameworks & Main | `ca-layer-boundaries` | TDD rules summary (below) |

- `ca-solid-principles` is injected in full only when the component has >3 classes
  in the layer; otherwise its intent is covered by the TDD rules summary.
- `ca-component-principles` is NOT injected at P4 (it is a design-time skill for
  P2/G3); referencing it during implementation adds ~200 tokens for zero value.

### TDD Rules Summary (inline injection source)

This compact summary replaces full `test-driven-development` skill injection for
each sub-agent. It preserves all behavioral constraints in ~120 tokens:

> **TDD Rules (batched Red-Green mode):**
> 1. Every line of production code must have a pre-existing failing test.
> 2. Write tests for a cohesion group (3–8 behaviors) in one batch, then run once
>    to confirm all fail for expected reasons (**batch RED**). If any test
>    unexpectedly passes, fix it before writing production code.
> 3. Implement the batch, then run once to confirm all pass (**batch GREEN**).
> 4. REFACTOR re-runs only if structure truly changes (control flow, interface,
>    responsibility moves); cosmetic refactors skip. Structural refactors = new batch.
> 5. Ports are test-doubled; tests must pass with no DB/UI/network.
> 6. Assert behavior (outputs, state changes, exceptions), never implementation
>    (call counts, internal method sequences).
> 7. Layer self-verify: only run incremental tests (this layer's new/affected);
>    selection by AST import analysis, not filename heuristics.
> 8. Each test executes at most once per batch; no redundant re-runs across layers.

### Purpose

Reduces per-sub-agent input by ~800–1200 tokens (from ~1500 tokens of full skill
texts to ~300 tokens of targeted rules + single skill). Over a typical 4-layer ×
3-component run, this saves ~12,000–15,000 input tokens total.

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

Four triggers, split by what the pause is actually for. The distinction decides
the discipline, and `cc_log.py` enforces it from `detail.trigger`:

**Information gaps** — the answer may already exist on disk, so asking is a
*substitute for looking*. These require `resolution_attempted[]`:
1. `business_rule` — P1 produced `open_questions` about a business rule / actor.
2. `tech_choice` — a concrete technology must be chosen (DB/framework/UI) that the
   design kept behind a port. Surface options; don't decide silently.

**Authority decisions** — no amount of reading grants permission, so no lookup is
required (and demanding one would punish the questions that must be asked):
3. `gate_overflow` — `gate3_iterations` or `gate5_iterations` exceeded 2; the axis
   of change or a boundary is genuinely ambiguous.
4. `debt_signoff` — G5 wants to accept a MAJOR finding as `debt`.

### Resolve before asking

For an information gap, check the sources you already have — P0 `codebase_notes`,
the authoritative design source, the P2 artifact (`boundary_dtos`, `layer_map`) —
and name them in `resolution_attempted[]`. Questions you answer that way are
**adopted, not asked**: record them in `adopted_without_asking{}` with the
one-line basis for each. Run 2's P1 pause did exactly this — 4 questions asked, 3
resolved from the artifacts — and it is the best question discipline of the three
production runs. It was invented on the spot, written down nowhere, and never
recurred: run 3 emitted zero `user_loop` events across 48.

### Batch, don't serialize

**One `user_loop` per phase boundary, carrying up to 4 questions — not one pause
per decision.** `AskUserQuestion` accepts at most 4 questions per call, so 4
questions must cost one round trip, and `cc_log.py` refuses a pause claiming more.

This is deliberately the opposite of an interactive design interview, where asking
one question at a time and waiting is correct because the human is present and
engaged. Here the human is the scarce resource — idle 15% / 71% / 71% of wall
clock across the three runs — so every extra round trip is charged against the
part of the pipeline that is already the bottleneck.

**Exception (the one thing serialization is for):** if question B's options depend
on the answer to A, they cannot share a pause. Split into two rounds and say so —
the second round is its own `user_loop`.

### Make the options decidable

2–4 concrete, mutually exclusive options per question, **recommended option first
and marked as such**. Generic yes/no is useless unless the question is genuinely
binary. Run 1 is the cautionary case: the agent asked, the user cancelled with
"继续", and the agent then proceeded on recommendations it had already marked — so
the pause bought no information and cost a 15-minute gap. If you can already name
the recommendation, ask whether this is an information gap you should have closed
yourself.

### What is mechanical

- `user_loop` must name a legal `trigger`, name its questions **in text** (a bare
  count records that a pause happened but not what was asked), and stay within the
  4-question cap. Information-gap triggers must carry `resolution_attempted[]`.
- A debt ask must follow the current PWC and name the exact `state.debts`.
  `debt_signoff` repeats that list, carries the user's actual `answer`, and sets
  `user_loop_seq` to that exact ask. A stale/generic ask or different debt list is
  refused. Before this, the v1.5.0 P6 latch verified only that a sign-off event had
  been written — not that anyone was asked — so the record was self-issuable. The
  logger proves sequence and subject, not cryptographic authorship; fabricating the
  answer remains a process violation.
- `state.question_ledger` counts `asked` vs `self_resolved` across the run. It is
  fed by any event carrying `adopted_without_asking`, not only by `user_loop`,
  because the ideal run resolves everything off disk and emits no pause at all —
  binding the counter to the pause event would score that run 0/0.

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

## Concurrency Contract (P4 component-level parallelism)

Concurrency in this pipeline is deliberately scoped to **P4, per component**. The
rest of the flow (P1→P2→G3→G5) is sequential because each gate must clear before
the next phase. P0 research may fan out read-only exploration agents, but that is
auxiliary, not main-flow parallelism.

**Current reality (v1.13): single-session serial execution.** Runs 6 and 7 both
planned multi-component waves and ran them strictly serially in one session — this
environment has no multi-session fan-out, so treat everything below as the
contract for when parallel execution IS available (multiple dispatch targets,
worktree isolation), and as a dependency-legality statement otherwise. The wave
plan still matters in both worlds: it proves files are pairwise disjoint, which
is what makes serial execution safe and future fan-out possible.

### Preconditions (all must hold before any fan-out)
1. G3 verdict is `APPROVED` (the component graph is a proven DAG — no cycles).
2. Boundaries between the components to be parallelized are **full/one-dim**
   (interface + DTO), not shared mutable code. Facade-only seams stay sequential.
3. `main`/composition root is NOT parallelized — it is wired once, sequentially,
   after all components are green.

### Scheduling rules
- Respect the component edges: only dispatch a component once every component it
  depends on is `DONE`. Start from leaf components (in-degree satisfied first).
- Within one component, layers stay inside-out sequential (entities → use cases →
  adapters); parallelism is *across* components, never *within* a layer chain.
- **Max concurrency**: default `min(4, ready_components)`. Expose as a
  `max_parallel` config; never exceed it so logs/worktrees stay legible.
- One **Autopilot Implementer** per component task; each reports the 4-state status
  (DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED).

### Isolation (git worktrees)
- Each parallel component runs in its own worktree via `using-git-worktrees`.
- Worktree/branch naming: `p4/<task-slug>/<component-name>` so it maps 1:1 to the
  `.cc-skill/<task-slug>/` run and to the component in the graph.
- Merge order at join: topological (dependency order), one at a time. After each
  merge, run only the component's boundary/integration tests plus any test whose
  source imports changed files (consistent with the P4 Test execution policy's
  deduplication rule — no full unit-test sweep of already-GREEN inner tests).
  Reclaim (remove) each worktree only after its merge is verified. Never
  force-merge; a conflict escalates to the USER LOOP.

### Logging & rollback (append to .cc-skill/<task-slug>/run.jsonl)
- On dispatch: `{event:"agent_dispatch", phase:"P4", detail:{component, worktree,
  parallel_group_id}}`.
- On finish: `{event:"phase_exit", detail:{component, status}}` with the 4-state.
- If any component returns `BLOCKED`/`FAIL`: log `{event:"error", detail:{component,
  cause}}`, then **quarantine** that component's worktree (do not merge it) while
  letting sibling components that don't depend on it continue. Route the blocked
  component to `systematic-debugging`; if it can't clear, roll it back (discard the
  worktree, keep the branch for inspection) and surface it at G5 as a mandatory
  follow-up — never merge a red component to make the group "look done".
- A `parallel_group_id` ties sibling tasks together so `ca-process-tuning` can later
  measure fan-out width vs. wall-clock savings.

### Determinism note
Parallel execution must not change the final result vs. a sequential run — because
components are DAG-independent, only wall-clock differs. If a parallel run produces
a different outcome than sequential, that is a hidden shared-state defect (a missed
boundary) — log a `conflict_logged` and send the design back to P2.

## Orchestrator Output (running log)

```
{
  phase, verdicts:{g3, g5},
  artifacts_index:{p1,p2,p4},
  injections:{ methodology_skills[], superpowers_used[], agents_dispatched[] },
  loops:{gate3_iterations, gate5_iterations},
  debts[], open_questions[], question_ledger:{asked, self_resolved}, next_action
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
    report.md                        # mechanical ledger (cc_log.py report) —
                                     #   REQUIRED before phase_exit P6
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
  "event":"phase_enter|phase_exit|phase_reopened|component_done|agent_dispatch|
           skill_inject|superpower_used|superpower_unavailable|gate_verdict|
           gate_invalidated|gate_scope_narrowed|loop_increment|user_loop|
           debt_signoff|pause|conflict_logged|process_violation|
           artifact_written|error",
  "agent":"...", "skills":[...], "superpowers":[...],
  "verdict":"APPROVED|REVISE_REQUIRED|PASS|PASS_WITH_CONCERNS|FAIL|null",
  "detail":{...}, "duration_ms":N }
```
Rules:
- Emit `phase_enter`/`phase_exit` around every phase; `gate_verdict` at each gate;
  `loop_increment` whenever `gate3/5_iterations` rises; `user_loop` on every pause;
  `conflict_logged` whenever a superpowers suggestion is overridden by the
  Dependency Rule; `superpower_unavailable` (with `detail.name` and
  `detail.fallback`) whenever a named augmentation cannot be invoked and its
  fallback is applied instead.
- **`user_loop` detail is required, not decorative**: `trigger` (one of
  `business_rule` | `tech_choice` | `gate_overflow` | `debt_signoff`), `questions[]`
  in text (≤ 4 per pause), and — for the two information-gap triggers —
  `resolution_attempted[]`. `adopted_without_asking{}` is optional on any event and
  feeds `state.question_ledger.self_resolved`.
- **`debt_signoff` needs an exact provenance chain**: after the current PWC, a
  `user_loop` with `trigger: "debt_signoff"` names the exact `debts[]`; the sign-off
  repeats those debts, quotes the user's `answer`, and references that event's
  `user_loop_seq`. The logger validates order/subject; it cannot authenticate who
  typed JSON, so inventing an answer is still forbidden.
- **`component_done`, not `phase_exit`, for each P4 component.** P4 implements many
  components under one `phase_enter`; reusing `phase_exit` per component makes the
  log look like repeated re-entry and destroys rework analysis.
- **`process_violation` for a broken process rule** — a skipped gate, an
  out-of-order phase, tests written after the code. Do NOT file these under
  `error`, which is for environment failures and retracted findings; mixing them
  makes both uncountable.
- `detail.fallback` is one of `applied` | `partial` | `none`. Use **`partial`**
  when only some of the augmentation's intent could be substituted (e.g. import
  checks done, but test evidence unobtainable) — and say which half is missing.
- When one root cause disables several augmentations at once, use
  `detail.names: [...]` instead of `detail.name`.
- **Log `phase_enter` when the work starts, never as a batch at completion.**
  Run 2 wrote enter+exit together at phase end, so every phase's wall-clock
  landed in its predecessor's gap and the cost analysis was off by one phase.
  `phase_exit` auto-fills `duration_ms` from the matching enter — but only if
  the enter was honest. If work pauses >30 min with no events, add a `user_loop`
  (or an `error` naming the blocker) so idle time is attributable.
- Never mutate a prior line — the log is append-only (use `>>`, not rewrite).
- Redact secrets: never write API keys/tokens/passwords into the log or artifacts.

### summary.md (written at DONE — the tuning artifact)
Must include: final verdicts; `gate3_iterations` / `gate5_iterations` (how many
rounds each gate needed); the list of superpowers actually used vs skipped vs
**unavailable** (the last group is an environment gap, not a judgement call — keep
it separate so tuning does not mistake "never fired" for "no value"); all
`debts` accepted with sign-off; all `open_questions` and how they were resolved;
the `question_ledger` (how many questions were put to the user vs answered from the
artifacts — a run that asked a lot has an upstream information gap, one that asked
nothing while shipping surprises had the opposite problem);
per-phase wall-clock; and a short "what to tune next time" note (e.g. a gate that
looped repeatedly signals an ambiguous boundary upstream).

### report.md (generated at DONE — the mechanical ledger, REQUIRED)
`cc_log.py report` produces it from run.jsonl + state.json + git. Where
`summary.md` narrates, `report.md` counts: wall clock, user wait, idle, P4
parallelism, per-phase and per-component durations (recorded pause-corrected
value AND raw timestamp span, ⚠-flagging divergence), gate verdicts, signed-off
debts, question ledger, and the git change list diffed against the baseline
commit recorded at `init`. Regenerate any time — it is idempotent and never
hand-written. `phase_exit P6` is refused without it.

### How to use it for review & tuning
- **核对 (audit)**: replay `.cc-skill/<task-slug>/run.jsonl` to see exactly which
  agent/skill ran when, every gate verdict, and every user decision — a full
  provenance trail.
- **调优 (tune)**: aggregate `summary.md` across all task subfolders under
  `.cc-skill/` to spot patterns — gates that loop often (design smell), superpowers
  that never help (drop them), phases that dominate wall-clock (parallelize or route
  to a cheaper model).

## Delta Review Data Flow (G5 scoped re-entry → delta review → clear)

End-to-end lifecycle of a scoped fix after G5 FAIL:

1. **G5 FAIL** with findings scoped to `ordering:adapters` and `billing:usecases`.
2. **Orchestrator routes fix** to the specific components — enters P4 with
   `--scope "ordering:adapters,billing:usecases"`.
3. **`cc_log.py`** logs `gate_scope_narrowed`, preserves `gate_verdicts.g5 = PASS`
   (or prior verdict), sets `g5_delta_scopes = ["billing:usecases","ordering:adapters"]`.
4. **Implementer** fixes only the scoped components (batched Red-Green on affected
   tests only).
5. **Orchestrator attempts P6** — `cc_log.py` **blocks**: g5 verdict passes but
   `g5_delta_scopes` is non-empty. Must run G5 first.
6. **G5 delta review** receives `g5_delta_scopes` as input, reviews only those
   components/layers plus cross-boundary imports.
7. **G5 verdict** (PASS/PASS_WITH_CONCERNS) is logged → `cc_log.py` clears
   `g5_delta_scopes` automatically.
8. **P6 now allowed** — both conditions satisfied (verdict passing + scopes empty).

If G5 delta FAILs, `g5_delta_scopes` is preserved, the loop iterates (bounded by
`gate5_iterations ≤ 2`), and only the still-failing scope gets another fix pass.

---

## Progress Checkpoint & Resume (survives context compression)

`run.jsonl` is append-only history — good for audit, but you'd have to replay it to
know "where are we now". To survive context-window compression / a dropped session,
the orchestrator MUST also keep a **single, overwritten-in-place** checkpoint file
that answers "current position" at a glance.

### state.json (rewritten atomically at EVERY phase/gate transition)
Path: `.cc-skill/<task-slug>/state.json`
```
{
  "run_id": "...",
  "task_title": "...",
  "current_phase": "P4",                       // where we are RIGHT NOW
  "phase_status": "in_progress|awaiting_user|blocked|done",
  "completed_phases": ["P1","P2","G3"],        // what's already accepted
  "gate_verdicts": {"g3":"APPROVED","g5":null},
  "loops": {"gate3_iterations":1,"gate5_iterations":0},
  "artifact_pointers": {                        // reload these instead of re-deriving
    "p1":"artifacts/p1-requirements.json",
    "p2":"artifacts/p2-design.json",
    "g3":"artifacts/g3-audit.json"
  },
  "p4_components": [                            // per-component progress + cost
    {"name":"ordering","status":"DONE","worktree":"p4/place-order/ordering",
     "dispatched_at":"ISO-8601","duration_ms":812340},
    {"name":"billing","status":"in_progress","dispatched_at":"ISO-8601"}
  ],
  "g5_delta_scopes": ["ordering:adapters"],    // string[] | absent. Present only after
                                                // scoped P4 re-entry; lists component:layer
                                                // pairs awaiting delta G5 review. Next G5
                                                // runs in delta mode if non-empty; a passing
                                                // G5 verdict clears it. P6 latch blocks
                                                // while non-empty.
  "pending_user_question": null,               // set when phase_status=awaiting_user
  "open_questions": [], "debts": [],
  "question_ledger": {"asked": 0, "self_resolved": 0},
                                                // asked = questions in user_loop
                                                // events; self_resolved = entries in
                                                // adopted_without_asking on ANY event.
                                                // A run that answers everything off
                                                // disk emits no user_loop, so the
                                                // counter cannot live on that event.
  "debts_signed_off": [],                      // moved here by a debt_signoff event;
                                                // P6 phase_exit is refused while
                                                // "debts" is still non-empty
  "next_action": "implement billing component then join",
  "updated_at": "ISO-8601"
}
```

**Every field above is maintained by `cc_log.py event`** — none is a leftover from
`init`. `component_done` appends to `p4_components`, `artifact_written` fills
`artifact_pointers` (keyed by the lowercased phase), `loop_increment` bumps `loops`
(and is refused past 2), a `PASS_WITH_CONCERNS` verdict lands its named debts in
`debts`, `debt_signoff` moves them to `debts_signed_off`, `user_loop` sets
`pending_user_question` plus `open_questions`, and `next_action` is recomputed on
every event. Run 3 predates this: it finished with `p4_components: []`,
`artifact_pointers: {}`, `debts: []`, `loops.gate5_iterations: 0` and
`next_action: "run P0/P1"` — so a resume would have restarted from P0 after 11
components were already built. If you are hand-writing the fallback log, maintain
these fields yourself; a resume trusts this file over the history.
Rules:
- Write it **before** emitting the matching `run.jsonl` event, and overwrite the
  whole file each time (write-temp-then-rename for atomicity). It is the *current*
  truth; `run.jsonl` is the *history*.
- `artifact_pointers` must always point at the last-good snapshot of each phase, so
  a resumed session reloads facts from disk rather than trusting a compressed memory.
- On `awaiting_user`, record the exact question in `pending_user_question` so the
  pause is resumable even if the chat context is lost.

### Resume protocol (run at the start of every turn / after any context reset)
1. If `.cc-skill/<task-slug>/state.json` exists, READ it first — do not re-plan from
   scratch or re-ask answered questions.
2. Reload the artifacts named in `artifact_pointers`; treat them as authoritative
   over anything half-remembered in context.
3. Continue from `current_phase` + `phase_status`:
   - `in_progress` → resume that phase's work;
   - `awaiting_user` → re-surface `pending_user_question`;
   - `blocked` → re-enter systematic-debugging on the blocked component;
   - `done` → report completion.
4. Never advance past a gate whose verdict in `state.json` isn't the required value.
5. Cross-check `state.json` against the tail of `run.jsonl`; if they disagree (e.g. a
   crash between the two writes), trust `run.jsonl` (the durable append) and rebuild
   `state.json` from it, logging a `conflict_logged`.

This gives a deterministic "you are here" marker: even if the model's context is
compressed and in-memory detail is lost, the next turn reads `state.json` +
`artifact_pointers` and picks up exactly where it left off.

