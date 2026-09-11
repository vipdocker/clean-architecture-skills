---
name: ca-process-tuning
description: Analyzes a completed Clean Architecture Autopilot run to decide whether the PROCESS itself needs tuning. Takes a finished project directory (required) plus its .cc-skill/ run logs (optional but strongly preferred) and produces a tuning report — gate effectiveness scores, rework hotspots, superpowers ROI, per-phase cost, and concrete "tune this next" recommendations. Use when the user hands over a done project (and/or its logs) and asks "does the pipeline need tuning / optimizing?". Not for judging whether the produced CODE is good (use ca-architecture-review-checklist), nor for driving a new build (use clean-architecture-autopilot).
---
<!-- clean-architecture system v1.12.0 -->

# Process Tuning (Pipeline Retrospective & Optimizer)

This skill answers a different question than the architecture review. The reviewer
asks *"is the produced code good?"*; this skill asks *"did the PROCESS run well,
and what should we change next time?"*. It closes the loop that the `.cc-skill/`
run logs were designed for.

## Two Evidence Sources (and what each can prove)

1. **Project directory (required)** — the terminal artifact. Lets you do a
   *reverse audit*: reconstruct the dependency graph (ast-grep / import scan),
   check for outward dependencies, cycles, I/A/D outliers, framework-polluted
   entities, SOLID breaks. Finding a Dependency-Rule violation in shipped code
   *indirectly* indicts a gate (G3 was skipped or judged too loosely).
2. **`.cc-skill/<task-slug>/` run logs (optional, preferred)** — the *process
   trace*. Only these reveal rework loops, why users were interrupted, which
   superpowers actually fired, and per-phase wall-clock. Code alone cannot show
   these because it only preserves the final state, not the path.

Degraded mode (no logs — e.g. the project was NOT built with the autopilot):
fall back to **git history + code structure**. Infer rework hotspots from commit
churn/revert patterns and file evolution. State clearly this is weaker inference
than structured logs, and mark such findings `low-confidence`.

## Inputs

```
{ project_dir (required),
  cc_skill_dir (optional, default <project_dir>/.cc-skill),
  git_available (bool) }
```
Announce upfront which sources are present so the confidence of each finding is
traceable.

## Analysis Procedure

### A. Reverse architecture audit (from project_dir)
Run the `ca-architecture-review-checklist` logic against the actual code:
- reconstruct dependency arrows (reuse `ast-code-analysis-superpower` rules);
- any outward dependency / cycle → a **gate-escape** signal (which gate failed?).
- compute I/A/D per component; flag Zone-of-Pain outliers.

### B. Gate effectiveness (from logs, else inferred)
For each gate G3 / G5:
- iterations it took (`loop_increment` events); a gate that loops ≥2 repeatedly
  signals an **upstream** defect (ambiguous boundary/actor in P1/P2), not a gate
  problem — recommend tuning the upstream phase, not the gate.
- escapes: defects found in B.A that the gate should have caught but didn't →
  the gate is **too loose**; tighten its checklist/scan rules.
- false stops: REVISE/FAIL later overturned with no change → the gate is **too
  strict**; relax the anchor.

**Do not read `iterations: 0` as "well calibrated" before checking the order.**
A gate that ran *after* the work it was supposed to guard also shows 0 loops.
Compare the `seq` of the `gate_verdict` against the first `phase_enter` /
`component_done` of the phase it gates, and look for `process_violation` events.
If the gate ran late, split the finding in two — they have opposite fixes:

| Question | Evidence | If bad, fix |
|---|---|---|
| Did the gate **judge** correctly? | re-verify its claims against the code (B.A) | tighten the checklist |
| Did the gate **run at the right time**? | `seq` order + `process_violation` | tighten the latch, leave the checklist alone |

A gate whose judgement is sound but whose timing was bypassed needs a mechanical
precondition, not a stricter rubric. Tightening the rubric there is pure waste.

Also check **verdict freshness**: a `phase_enter P4` after a `g5` verdict (or
`P2` after `g3`) should be followed by a `gate_invalidated` event and, later, a
fresh verdict. A run that ends on a verdict older than the last re-entry shipped
unreviewed work — same defect class as a skipped gate, one level subtler.

### C. Rework hotspots
Rank phases by how often they were re-entered (from `phase_enter` counts, or git
churn in degraded mode). The most re-entered phase is the top tuning target.

Count **`phase_enter`**, never `phase_exit`. P4 legitimately emits many
`component_done` events under a single entry; older logs sometimes used
`phase_exit` per component, which inflates apparent re-entry. If `phase_exit`
outnumbers `phase_enter` for a phase, treat the extras as component progress and
say the log predates the `component_done` convention.

### D. Superpowers ROI
Read both `superpower_used` and `superpower_unavailable` events, and keep the three
cases apart — conflating them produces the wrong recommendation:

| Evidence | Meaning | Recommendation |
|---|---|---|
| fired, and the run visibly improved | earning its keep | **keep** / **promote** |
| fired, no signal either way | ceremony | **drop** for this project profile |
| `superpower_unavailable` logged | environment gap, **not** a value judgement | **install** it, or promote its fallback to the default and stop naming it |
| `fallback: partial` logged | the substitute only covered part of the intent | **install** it — the uncovered half is a real evidence gap, not a solved problem |
| never mentioned at all | never reached that phase | insufficient evidence — say so |

The trap to avoid: reading "never fired" as "worthless". A skill that was never
installed cannot have produced signal, so deleting the reference on that basis
throws away an untested idea. Only a skill that *ran* and changed nothing earns a
drop.

### E. Cost / latency

**Separate wall clock from effective work before calling anything slow.** Sum the
gaps between consecutive events: intervals over ~30 min are idle, not compute. Run 3
looked like an 8.93-hour task and was 2.63 hours of work plus one 6.30-hour
overnight gap (71% idle); run 2 was 21.50h wall / 6.25h work; run 1 was 6.47h /
5.47h. An agent that got *faster* reads as "too slow" if you only look at the
calendar.

Then attribute the effective work:
- **Per phase** — `duration_ms` on `phase_exit`, pause-corrected.
- **Per component** — `duration_ms` on `component_done`, measured from that
  component's `agent_dispatch` with annotated pauses subtracted, and mirrored into
  `state.p4_components[].duration_ms`.
- **Parallelism ratio** — sum of component durations ÷ that phase's wall clock. ≈1
  means the wave ran serially; >1 means the overlap was real. This is how you check
  whether a plan change meant to parallelize actually did.
- **Model routing** — each task's `recommended_model` against its measured duration.
  Only here can you say whether `premium` earned its place.

**Distrust coarse timestamps.** Compare distinct timestamps against event count: run
1 was 30 events / 30 timestamps (real-time), run 3 was 47 / 16 because events were
batch-written at the end of each stretch. With a ratio far above 1 you can attribute
time to batch *windows* only — say that instead of reporting per-component numbers
the log cannot support. A missing `agent_dispatch` has the same effect and is now
refused, but older logs still lack it.

Phases or components that dominate → parallelize (if genuinely independent — check
the plan graph for a presentation task stuck behind an implementation) or route to a
cheaper model.

### F. Question discipline (asked vs self-resolved)

Read `state.question_ledger` (`asked` / `self_resolved`) together with the
`user_loop` events. Each pause carries a `trigger`, and for the two information-gap
triggers (`business_rule`, `tech_choice`) a `resolution_attempted[]` naming what was
checked before asking. What the two ends mean:

- **Many asks, thin `resolution_attempted`** → the agent is using the user as a
  lookup table. The fix is upstream: P0 research or the P2 artifact is missing
  something the pipeline already had access to.
- **Zero asks on a run that shipped surprises** → the opposite failure. Run 3 asked
  nothing across 48 events and a live-verification MAJOR landed 6h18m after P6
  first closed. Silence is not discipline if the guesses were wrong.
- **A high `self_resolved` with the basis recorded** is the target state, and run 2's
  P1 pause (4 asked, 3 adopted from the artifacts) is the reference example.

**Do not score on the ledger alone.** The counts say how often the user was
consulted, not whether the answers were right. Cross-check the adopted decisions
against what G5 later found: an `adopted_without_asking` entry that a gate
subsequently overturned is a self-resolution that should have been a question, and
it is worth more than any ratio.

Also verify each `debt_signoff` has the full chain: current G5 PWC → later
`user_loop(trigger: "debt_signoff")` naming those exact debts → sign-off with the
same `debts[]`, matching `user_loop_seq`, and a quoted `answer`. Sign-offs are now
latched, but older logs predate it — run 3 closed P6 twice with two debts
outstanding, so a sign-off with no matching ask is a MAJOR process finding, not a
bookkeeping slip. This proves structural provenance, not speaker identity; an
answer contradicted by the conversation is still a process violation.

**Bound the claim honestly.** Question-blocked idle is small: across the three runs
it is roughly 35 minutes total (run 1's single pause sat in a 15-minute gap; run 2's
two pauses had ~0-minute gaps; run 3 had none). The 15.25h and 6.30h idle stretches
were the user being away, not the pipeline waiting on an answer. So treat this
section as a **correctness and authority** check, not a latency lever — the wall
clock it recovers is minutes. Its real payoff is making unattended operation safe:
a run that resolves its own information gaps and cannot self-sign its debts is one
you can leave alone.

## Output — Tuning Report

```
{
  sources: {project_dir, logs_present, git_present},
  reverse_audit: {verdict, dependency_violations[], cycles[], iad_outliers[]},
  gates: [{gate, iterations, escapes[], false_stops[],
           verdict: well_calibrated|too_loose|too_strict, fix}],
  rework_hotspots: [{phase, reentry_count, likely_root_cause, confidence}],
  superpowers_roi: [{name, status: fired|unavailable|not_reached, decisive,
                     recommendation: keep|drop|promote|install|promote_fallback}],
  cost: {wall_clock, effective_work, idle, idle_pct,
         per_phase: [{phase, duration, recommendation}],
         per_component: [{component, duration, model, recommendation}],
         parallelism_ratio,            // Σ component durations ÷ phase wall clock
         timestamp_granularity},       // "real_time" | "batched" — batched means
                                       // per-component numbers are unsupported
  question_discipline: {asked, self_resolved,
         asks_without_lookup: [{seq, trigger}],   // info-gap pause, no resolution_attempted
         adopted_then_overturned: [{question, gate_finding}],
         invalid_signoff_chains: [{seq, debt, reason}], // MAJOR: missing/stale/mismatched ask
         verdict: healthy|user_as_lookup_table|silent_guessing},
  top_3_tuning_actions: [ "..." ],   // ranked, concrete, each tied to evidence
  confidence_note                     // states which findings are low-confidence
}
```
Also emit a short human-readable `tuning-report.md` and, if a `.cc-skill/` root
exists, write it there so retrospectives accumulate alongside the runs they judge.

## Guardrails
- Separate "code quality" findings (→ fix the code) from "process" findings
  (→ tune the pipeline). Don't conflate them.
- A looping gate usually means an upstream ambiguity — resist "just relax the gate".
- Every recommendation must cite evidence (a log event, a file/import, or a commit).
- If neither logs nor git exist, say so and limit output to the reverse audit only.

## Cross-run tuning (optional)
When several `.cc-skill/<task-slug>/` folders exist, aggregate their `summary.md`
to find *systemic* patterns (a gate that loops on every task, a superpower that
never helps) — these are higher-value than single-run findings.
