# Clean Architecture Pipeline — Orchestration

This document defines how the five agents are sequenced into a quality-gated
pipeline, what each hand-off contains, and how feedback loops route rework to the
right station. The design mirrors a harness pattern: **plan → gate → build → gate**.

## The DAG

```
          ┌──────────────────────┐
  input ─▶│ 1. Requirements       │  skills: use-case-extraction
          │    Analyst            │
          └───────────┬──────────┘
                      │ entities[], use_cases[], deferred_details[], open_questions[]
                      ▼
          ┌──────────────────────┐
          │ 2. Architecture       │  skills: layer-boundaries,
          │    Designer           │          component-principles, solid-principles
          └───────────┬──────────┘
                      │ layer_map, ports[], boundary_dtos[], component_map, tree, design_doc
                      ▼
          ┌──────────────────────┐   REVISE_REQUIRED
          │ 3. Dependency         │──────────────────┐
          │    Auditor (GATE)     │  skills:          │ (back to Designer)
          └───────────┬──────────┘  dependency-rule, │
                      │ APPROVED     component-principles
                      ▼             ◀──────────────────┘
          ┌──────────────────────┐
          │ 4. Clean Implementer  │  skills: dependency-rule,
          │  (per layer,          │          solid-principles, layer-boundaries
          │   inside-out)         │
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
- **Gate 5 (pre-acceptance)** — Architecture Reviewer. Full checklist. `FAIL` routes
  BLOCKERs back to Implementer (code) or Designer (structure); `PASS_WITH_CONCERNS`
  accepts with mandatory follow-ups logged.

## Feedback Loops (bounded)

- Gate 3 ⇄ Designer: cap at 2 iterations; if still failing, escalate an
  `open_question` to the user (the axis of change or a boundary may be genuinely
  ambiguous).
- Gate 5 ⇄ Implementer/Designer: BLOCKERs must clear before accept; MAJORs may be
  accepted as tracked debt only with user sign-off.

## When to Enter the User Loop

The orchestrator pauses and asks the user when:
1. Phase 1 produces `open_questions` about a business rule / actor.
2. A specific technology must be chosen (DB/framework/UI) that the design has so
   far kept behind a port.
3. Gate 3 exceeds its iteration cap.
4. Gate 5 wants to accept a MAJOR as debt.

## Parallelization

Phase 4 (Implementer) can fan out **per component** once Gate 3 approves, because a
correct component graph is a DAG — independent components have no shared mutable
state. Run leaf components (fewest inward deps satisfied) first, respecting the
component edges. Entities and use cases for a given domain slice are implemented
before its adapters.

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
| P1 requirements | requirements-analyst | use-case-extraction | brainstorming, feature-spec | general-purpose |
| P2 design | architecture-designer | layer-boundaries, component-principles, solid-principles | writing-plans, plan-eng-review | Plan, Autopilot Designer/Planner |
| G3 dep audit | dependency-auditor | dependency-rule, component-principles | ast-code-analysis-superpower | Explore |
| P4 implement | clean-implementer | dependency-rule, solid-principles, layer-boundaries | test-driven-development, executing-plans, subagent-driven-development, dispatching-parallel-agents, using-git-worktrees, systematic-debugging/investigate, verification-before-completion | Autopilot Implementer |
| G5 review | architecture-reviewer | architecture-review-checklist (+3) | requesting-code-review, ast-code-analysis-superpower, codex, review | Autopilot Code Reviewer |
| P6 finish | — | — | receiving-code-review, finishing-a-development-branch, ship | — |

Conflict rule: if a superpowers suggestion points a dependency outward or wires a
concretion outside `main`, the Dependency Rule wins and the conflict is logged.
