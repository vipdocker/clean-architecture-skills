---
name: solid-principles
description: Applies the five SOLID class-design principles (SRP, OCP, LSP, ISP, DIP) from Robert C. Martin's Clean Architecture. Use when designing or reviewing class/module structure, deciding where to place responsibilities, choosing interfaces, or resolving rigidity/fragility smells. Language-agnostic. Not for component/package-level cohesion and coupling (use component-principles), for cross-layer dependency direction (use dependency-rule), or for assigning code to the four layers (use layer-boundaries).
---
<!-- clean-architecture system v1.2.0 -->

# SOLID Principles (Class-Level Design)

SOLID governs how individual classes and modules are arranged so that software
is tolerant to change, easy to understand, and reusable across many contexts.
These are the *mid-level* rules of Clean Architecture — they sit between code
(functions) and components (deployable units).

## The Five Principles

### SRP — Single Responsibility Principle
"A module should have one, and only one, reason to change."

The key word is *reason to change*, which maps to **one actor** (a group of
users/stakeholders who request changes together). Do not read SRP as "a function
should do one thing" — that is a lower-level heuristic.

- Symptom of violation: one class serves two actors, so a change requested by
  actor A accidentally breaks behavior owned by actor B (accidental duplication /
  merge collisions).
- Fix patterns: split the class per actor; use a Facade or an interactor that
  delegates to per-actor classes.

Litmus: "If I list every stakeholder who could request a change to this class,
is the count exactly one?"

### OCP — Open-Closed Principle
"A software artifact should be open for extension but closed for modification."

You add new behavior by adding new code, not by editing working code. Achieved by
arranging dependencies so that higher-level policy is protected from lower-level
detail changes via interfaces/abstractions.

- Direction: dependencies and level of protection flow so that the component you
  most want to protect (business rules) depends on nothing volatile.
- Fix patterns: polymorphism behind an interface; plugin points; strategy.

Litmus: "To add the next expected variation, do I edit an existing class or add a
new one?"

### LSP — Liskov Substitution Principle
"Subtypes must be substitutable for their base types."

Any consumer of an interface/base type must work correctly with every
implementation, with no special-casing. Violations produce `if (x instanceof
Special)` branches that leak implementation details upward.

- Classic violation: the Square/Rectangle trap; a subtype that strengthens
  preconditions or weakens postconditions.
- Scope: applies to any interface contract, not just inheritance.

Litmus: "Can I swap any implementation of this interface without the caller
knowing or caring?"

### ISP — Interface Segregation Principle
"Do not force clients to depend on methods they do not use."

Fat interfaces create needless coupling: a client that uses one method still
recompiles/redeploys when an unrelated method changes.

- Fix patterns: split fat interfaces into role-specific interfaces; each client
  depends only on the operations it actually calls.

Litmus: "Does any client of this interface use less than half of its methods?"

### DIP — Dependency Inversion Principle
"Depend on abstractions, not on concretions." High-level policy must not depend on
low-level detail; both depend on abstractions.

This is the engine behind the Dependency Rule (see `dependency-rule` skill). The
abstraction (interface) is *owned by the caller's layer*; the implementation lives
in an outer layer and is injected.

- Rules of thumb: don't refer to volatile concrete classes; don't derive from
  volatile concrete classes; don't override concrete functions; never mention the
  name of anything concrete and volatile.
- Mechanism: Abstract Factories and Dependency Injection cross the boundary.

Litmus: "Does the source-code dependency arrow point *toward* the more abstract,
more stable, higher-level policy?"

## How to Apply (workflow)

1. For each proposed class, name every actor that could request a change → if >1,
   apply SRP and split.
2. Identify the axis of expected change → introduce an abstraction so new
   variations are added, not edited (OCP).
3. For every interface, check substitutability (LSP) and client-fit (ISP).
4. Draw the source-code dependency arrows; ensure they point toward stable
   abstractions (DIP). Any arrow pointing at a volatile concretion is a defect.

## Common Smells This Skill Detects

- Rigidity: a small change forces a cascade of edits (OCP/DIP failure).
- Fragility: a change breaks unrelated parts (SRP failure).
- Immobility: a component can't be reused because it drags dependencies (ISP/DIP).
- `instanceof` / type-switch ladders (LSP failure).

## Output Contract

When invoked, emit for each class/module reviewed:
`{name, actors[], change_axis, abstractions_introduced[], violations:[{principle, evidence, fix}]}`
