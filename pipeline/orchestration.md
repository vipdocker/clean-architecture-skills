# Clean Architecture Pipeline — Orchestration

This document defines how the five agents are sequenced into a quality-gated
pipeline, what each hand-off contains, and how feedback loops route rework to the
right station. The design mirrors a harness pattern: **plan → gate → build → gate**.

## The DAG

```
          ┌──────────────────────┐
  input ─▶│ 1. Requirements       │  skills: ca-use-case-extraction
          │    Analyst            │
          └───────────┬──────────┘
                      │ entities[], use_cases[], deferred_details[], open_questions[]
                      ▼
          ┌──────────────────────┐
          │ 2. Architecture       │  skills: ca-layer-boundaries,
          │    Designer           │          ca-component-principles, ca-solid-principles
          └───────────┬──────────┘
                      │ layer_map, ports[], boundary_dtos[], component_map, tree, design_doc
                      ▼
          ┌──────────────────────┐   REVISE_REQUIRED
          │ 3. Dependency         │──────────────────┐
          │    Auditor (GATE)     │  skills:          │ (back to Designer)
          └───────────┬──────────┘  ca-dependency-rule, │
                      │ APPROVED     ca-component-principles
                      ▼             ◀──────────────────┘
          ┌──────────────────────┐
          │ 4. Clean Implementer  │  skills: ca-dependency-rule,
          │  (per layer,          │          ca-solid-principles, ca-layer-boundaries
          │   inside-out,         │          (batched Red-Green TDD,
          │   batched TDD)        │           cross-layer deduplication)
          └───────────┬──────────┘
                      │ files[], tests[], status
                      ▼
          ┌──────────────────────┐   FAIL / PASS_WITH_CONCERNS
          │ 5. Architecture       │──────────────────┐
          │    Reviewer (GATE)    │  skills:          │ code fix → Implementer
          └───────────┬──────────┘  architecture-     │ structural fix → Designer
                      │ PASS         review-checklist  │
                      ▼             ◀──────────────────┘
                   accept
```

## Quality Gates

- **Gate 3 (pre-implementation)** — Dependency Auditor. Cheap to run, catches the
  most expensive class of defect (wrong dependency direction / cycles) *before* any
  code is written. `REVISE_REQUIRED` loops back to Phase 2.
- **Gate 5 (pre-acceptance)** — Architecture Reviewer. Full checklist on first pass;
  **delta review** (only changed components/layers) on subsequent passes after
  targeted fixes. `FAIL` routes BLOCKERs back to the **specific component/layer** in
  Implementer (code) or Designer (structure) via precise scope routing;
  `PASS_WITH_CONCERNS` accepts with mandatory follow-ups logged.

## Feedback Loops (bounded)

- Gate 3 ⇄ Designer: cap at 2 iterations; if still failing, escalate an
  `open_question` to the user (the axis of change or a boundary may be genuinely
  ambiguous).
- Gate 5 ⇄ Implementer/Designer: BLOCKERs must clear before accept; fixes are
  routed to the **precise component/layer** (not the entire phase); G5 re-runs as
  a **delta review** covering only the fixed scope. MAJORs may be accepted as
  tracked debt only with user sign-off.

## When to Enter the User Loop

The orchestrator pauses and asks the user when:
1. `business_rule` — Phase 1 produces `open_questions` about a business rule / actor.
2. `tech_choice` — a specific technology must be chosen (DB/framework/UI) that the
   design has so far kept behind a port.
3. `gate_overflow` — Gate 3 or Gate 5 exceeds its iteration cap.
4. `debt_signoff` — Gate 5 wants to accept a MAJOR as debt.

Triggers 1–2 are **information gaps**: the answer may already sit in the P0
`codebase_notes`, the design source, or the P2 artifact, so the orchestrator looks
there first and records what it checked. Questions answered that way are adopted,
not asked. Triggers 3–4 are **authority decisions** — only the user can make them.
A debt sign-off is bound to the current verdict's exact debt question by event
sequence and debt list; the logger proves that provenance, not the speaker's identity.

Pauses are **batched**: one user loop per phase boundary carrying up to 4 questions
(one `AskUserQuestion` call), not one pause per decision. Questions whose options
depend on an earlier answer are the one case that splits into a second round. Both
disciplines are mechanically enforced by `cc_log.py`; the full contract lives in the
USER LOOP section of `skills/clean-architecture-autopilot/SKILL.md`.

## Parallelization

Phase 4 (Implementer) can fan out **per component** once Gate 3 approves, because a
correct component graph is a DAG — independent components have no shared mutable
state. Run leaf components (fewest inward deps satisfied) first, respecting the
component edges. Entities and use cases for a given domain slice are implemented
before its adapters.

The full rules — preconditions, `max_parallel` cap, git-worktree naming
(`p4/<task-slug>/<component>`), topological merge order, and the quarantine/rollback
behavior when a parallel component fails — live in the **Concurrency Contract**
section of `skills/clean-architecture-autopilot/SKILL.md`. Concurrency is scoped to
P4 only; P1→P2→G3→G5 stay sequential (gates must clear in order), and `main` is
wired once, sequentially, after all components are green.

## Artifacts Passed Between Phases

| From → To | Artifact key |
|---|---|
| 1 → 2 | `entities`, `use_cases`, `deferred_details` |
| 2 → 3 | `layer_map`, `ports`, `component_map`, `directory_tree` |
| 3 → 4 | `APPROVED` + design artifacts |
| 4 → 5 | `files`, `tests`, `status`, `concerns` |
| 5 → accept | `verdict`, `findings`, `mandatory_followups` |

## Mapping to Book Concepts

- Phase 1 = "Business Rules" chapters (Entities vs Use Cases).
- Phase 2 = "Boundaries", "Screaming Architecture", component chapters.
- Phase 3 = "The Dependency Rule" enforcement + "Component Coupling" (ADP/SDP/SAP).
- Phase 4 = "Humble Object", "Partial Boundaries", "Main Component".
- Phase 5 = the whole rulebook as an acceptance checklist.

## Superpowers Augmentation Per Phase

The orchestrator (`skills/clean-architecture-autopilot`) layers matching
superpowers skills/agents onto each phase. Augmentation is **additive** — it adds
rigor but never overrides the Dependency Rule or the local methodology skills.

| Phase | Role agent | Local skill(s) | Superpowers skill(s) | Superpowers agent(s) |
|---|---|---|---|---|
| P0 research | — | — | find-skills, context7 | Explore, Autopilot Researcher |
| P1 requirements | ca-requirements-analyst | ca-use-case-extraction | brainstorming, feature-spec | general-purpose |
| P2 design | ca-architecture-designer | ca-layer-boundaries, ca-component-principles, ca-solid-principles | writing-plans, plan-eng-review | Plan, Autopilot Designer/Planner |
| G3 dep audit | ca-dependency-auditor | ca-dependency-rule, ca-component-principles | ast-code-analysis-superpower | Explore |
| P4 implement | ca-clean-implementer | ca-dependency-rule, ca-solid-principles, ca-layer-boundaries | test-driven-development (batched Red-Green + dedup), executing-plans, subagent-driven-development, dispatching-parallel-agents, using-git-worktrees, systematic-debugging/investigate, verification-before-completion | Autopilot Implementer |
| G5 review (delta on re-run) | ca-architecture-reviewer | ca-architecture-review-checklist (+3) | requesting-code-review, ast-code-analysis-superpower, codex, review | Autopilot Code Reviewer |
| P6 finish | — | — | receiving-code-review, finishing-a-development-branch, ship | — |

Conflict rule: if a superpowers suggestion points a dependency outward or wires a
concretion outside `main`, the Dependency Rule wins and the conflict is logged.
