---
name: component-principles
description: Applies Clean Architecture's component cohesion (REP, CCP, CRP) and coupling (ADP, SDP, SAP) principles to decide what goes into a deployable component, how components depend on each other, and how to keep the dependency graph acyclic and stable. Use when defining modules/packages/build units, drawing the component dependency graph, or diagnosing cyclic/unstable dependencies. Language-agnostic.
version: 1.0.0
---

# Component Principles (Component-Level Design)

A *component* is the smallest unit of deployment — a jar, gem, DLL, package,
or independently buildable module. These six principles decide **what classes
belong together** and **how components may depend on one another**.

## Cohesion — Which classes belong in a component

### REP — Reuse/Release Equivalence Principle
"The granule of reuse is the granule of release." Classes grouped into a component
must be releasable together, share a version number and release notes, and form a
coherent, reusable whole. If it isn't tracked, versioned, and released as a unit,
it can't be reused with confidence.

### CCP — Common Closure Principle
"Gather into the same component classes that change for the same reasons and at the
same times; separate classes that change at different times/reasons." This is SRP
restated for components — a component should have a single reason to change. It
minimizes the number of components that must be revalidated/redeployed per change.

### CRP — Common Reuse Principle
"Don't force users of a component to depend on things they don't need." This is
ISP restated for components — classes that are reused together belong together;
don't glue in unrelated classes that drag extra dependencies. CRP tells you which
classes to *keep out*.

### The Tension Triangle (CCP ↔ REP ↔ CRP)
These three pull against each other:
- REP + CCP are *inclusive* (make components bigger).
- CRP is *exclusive* (makes components smaller).

Early projects sit near CCP (favor developability). Mature projects drift toward
CRP (favor reuse). Choose position on the triangle deliberately per component and
revisit it as the project matures.

## Coupling — How components may depend on each other

### ADP — Acyclic Dependencies Principle
"Allow no cycles in the component dependency graph." The graph must be a DAG.
Cycles create a "morning-after syndrome" where nobody can build/test in isolation.

- Break cycles by: (a) applying DIP — invert one dependency with an interface; or
  (b) creating a new component that both cyclic components depend on.
- Detect: run a topological sort; any failure = a cycle to break.

### SDP — Stable Dependencies Principle
"Depend in the direction of stability." A component should depend only on
components that are *more stable* than itself.

- Instability `I = Fan-out / (Fan-in + Fan-out)`, where 0 = maximally stable
  (many depend on it, it depends on nothing), 1 = maximally unstable.
- Rule: `I` must *decrease* along each dependency arrow. A stable component that
  depends on an unstable one is a defect.

### SAP — Stable Abstractions Principle
"A component should be as abstract as it is stable." Stable components should be
abstract (interfaces/policies) so stability doesn't prevent extension; unstable
components should be concrete so they're easy to change.

- Abstractness `A = abstract classes / total classes`.
- The "Main Sequence": plot `(I, A)`; ideal components lie on the line `A + I = 1`.
  - Zone of Pain: `(I≈0, A≈0)` — stable *and* concrete (rigid, e.g. a huge
    concrete utility everyone depends on). Avoid.
  - Zone of Uselessness: `(I≈1, A≈1)` — abstract but nobody depends on it. Avoid.
  - Distance from main sequence `D = |A + I − 1|`; aim for `D → 0`.

## How to Apply (workflow)

1. Group classes into candidate components using CCP (same reason/time to change),
   then trim with CRP (remove classes clients don't need together), then confirm
   each component is independently releasable (REP).
2. Draw the component dependency graph; run a cycle check (ADP). Break any cycle
   via DIP or a new shared component.
3. Compute `I` for each component; verify arrows point from higher-`I` to lower-`I`
   (SDP).
4. Compute `A`; verify stable components are abstract (SAP); flag components in the
   Zone of Pain or Uselessness (high `D`).

## Output Contract

Emit: `{components:[{name, classes[], I, A, D, zone}], graph_edges[], cycles[], sdp_violations[], sap_violations[]}`
plus recommended refactors to reach a DAG and minimize `D`.
