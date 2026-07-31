---
name: process-tuning
description: Analyzes a completed Clean Architecture Autopilot run to decide whether the PROCESS itself needs tuning. Takes a finished project directory (required) plus its .cc-skill/ run logs (optional but strongly preferred) and produces a tuning report — gate effectiveness scores, rework hotspots, superpowers ROI, per-phase cost, and concrete "tune this next" recommendations. Use when the user hands over a done project (and/or its logs) and asks "does the pipeline need tuning / optimizing?".
version: 1.0.0
---

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
Run the `architecture-review-checklist` logic against the actual code:
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

### C. Rework hotspots
Rank phases by how often they were re-entered (from `phase_enter` counts, or git
churn in degraded mode). The most re-entered phase is the top tuning target.

### D. Superpowers ROI
From `superpower_used` events (or absence): which augmentations fired, and did the
run improve because of them? Never-fired or no-signal superpowers → candidate to
**drop** for this project profile. Frequently-decisive ones → keep/promote.

### E. Cost / latency
Per-phase wall-clock (`duration_ms`) and any premium-model routing. Phases that
dominate time → parallelize (if their tasks are independent) or route to a cheaper
model.

## Output — Tuning Report

```
{
  sources: {project_dir, logs_present, git_present},
  reverse_audit: {verdict, dependency_violations[], cycles[], iad_outliers[]},
  gates: [{gate, iterations, escapes[], false_stops[],
           verdict: well_calibrated|too_loose|too_strict, fix}],
  rework_hotspots: [{phase, reentry_count, likely_root_cause, confidence}],
  superpowers_roi: [{name, fired, decisive, recommendation: keep|drop|promote}],
  cost: [{phase, wall_clock, recommendation}],
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
