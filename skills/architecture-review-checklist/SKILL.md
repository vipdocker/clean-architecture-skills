---
name: architecture-review-checklist
description: A gate-style review checklist that scores a design/codebase against all Clean Architecture principles (Dependency Rule, SOLID, component cohesion/coupling, boundaries, testability) and returns pass/fail with severity-ranked findings. Use as the final quality gate before accepting an architecture or merging code, or to audit an existing system. Language-agnostic.
version: 1.1.0
---

# Clean Architecture Review Checklist (Quality Gate)

Run this as the final gate. Score each section, collect findings with severity,
and emit an overall PASS / PASS_WITH_CONCERNS / FAIL verdict.

## Severity Calibration (anchor to prevent drift)

- **BLOCKER** — a Dependency Rule violation (inner depends on outer), a dependency
  cycle (ADP), or business rules coupled to a framework/DB. Must fix before merge.
- **MAJOR** — a SOLID violation causing rigidity/fragility, a fat interface, a
  boundary crossed with framework/entity objects, or a stable→unstable dependency
  (SDP). Fix soon.
- **MINOR** — naming that hides the domain (non-screaming), over-drawn boundary
  (paying for indirection not yet needed), missing DTO where a facade would do.
- **NIT** — style/consistency; optional.

## Section A — The Dependency Rule (BLOCKER-weighted)
- [ ] No inner layer references any outer layer (entities→usecases→adapters→frameworks).
- [ ] Entities are pure (no framework/ORM/UI/DB imports).
- [ ] Use cases depend only on inner-owned interfaces for anything external.
- [ ] Data crossing boundaries is inner-defined DTOs (no ORM rows / HTTP / UI leaking inward).
- [ ] Concretions are wired only in `main`/composition root.

## Section B — SOLID (see `solid-principles`)
- [ ] SRP: each class has exactly one actor / reason to change.
- [ ] OCP: expected variations are added, not edited (abstractions in place).
- [ ] LSP: no `instanceof`/type-switch ladders; implementations are substitutable.
- [ ] ISP: no client depends on interface methods it doesn't use.
- [ ] DIP: source dependencies point at stable abstractions.

## Section C — Component Cohesion & Coupling (see `component-principles`)
- [ ] CCP: classes in a component change for the same reason/time.
- [ ] CRP: no component drags classes its clients don't need.
- [ ] REP: each component is independently releasable/versioned.
- [ ] ADP: component dependency graph is acyclic (topological sort passes).
- [ ] SDP: instability `I` decreases along every dependency arrow.
- [ ] SAP: stable components are abstract; no component in Zone of Pain/Uselessness (`D`→0).

## Section D — Boundaries & Layout (see `layer-boundaries`)
- [ ] Boundary granularity matches the real axis of change (facade / one-dim / full).
- [ ] Screaming architecture: top-level dirs name the domain, not the framework.
- [ ] Framework/DB/UI are plugins at the edge; decisions deferred correctly.

## Section E — Testability
- [ ] Business rules are unit-testable without DB, web, or UI (Humble Object applied).
- [ ] Boundaries allow test doubles to be injected for every port.
- [ ] Tests do not depend on volatile details (no fragile "test-through-the-GUI").

## Scoring & Verdict

For each section, count findings by severity. Then:
- Any **BLOCKER** open → verdict **FAIL**.
- No BLOCKER but ≥1 MAJOR → **PASS_WITH_CONCERNS** (list mandatory follow-ups).
- Only MINOR/NIT → **PASS**.

## Output Contract

```
{
  verdict: PASS | PASS_WITH_CONCERNS | FAIL,
  sections: {A:{score, findings[]}, B:{...}, C:{...}, D:{...}, E:{...}},
  findings: [{id, severity, section, evidence, principle, recommended_fix}],
  mandatory_followups: [ ... ]   // for PASS_WITH_CONCERNS / FAIL
}
```

Each finding must cite concrete evidence (file/class/import or design element) and
the exact principle violated — never a vague "looks off".
