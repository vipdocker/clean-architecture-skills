---
name: dependency-rule
description: Enforces Clean Architecture's central Dependency Rule — source-code dependencies must point only inward, toward higher-level policy. Use when placing a class in a layer, deciding which way an import/reference may point, designing how control crosses a boundary (DIP + Humble Object), or auditing for outward-pointing dependencies. Language-agnostic.
version: 1.1.0
---

# The Dependency Rule

> "Source code dependencies must point only inward, toward higher-level
> policies." — Clean Architecture

This is the one rule that makes the whole architecture work. Everything else
(SOLID, boundaries, layers) exists to serve it.

## The Concentric Circles (inner = more abstract, more stable, higher policy)

```
        ┌─────────────────────────────────────────┐
        │  Frameworks & Drivers (DB, Web, UI, IO)   │   outermost — details
        │   ┌───────────────────────────────────┐   │
        │   │  Interface Adapters                │   │   controllers, presenters,
        │   │   (Controllers/Presenters/Gateways)│   │   gateways, view models
        │   │   ┌───────────────────────────┐    │   │
        │   │   │  Use Cases                 │    │   │   application business rules
        │   │   │   ┌───────────────────┐    │    │   │
        │   │   │   │   Entities        │    │    │   │   enterprise business rules
        │   │   │   └───────────────────┘    │    │   │
        │   │   └───────────────────────────┘    │   │
        │   └───────────────────────────────────┘   │
        └─────────────────────────────────────────┘
              ── dependency arrows point INWARD ──▶
```

## Core Statements

1. **Nothing in an inner circle may name anything in an outer circle.** No
   variable, class, function, or data structure declared outward may be mentioned
   by inward code.
2. **Data crossing a boundary is simple and inward-defined.** Pass plain data
   structures / DTOs (defined by the inner layer). Never pass an Entity outward as
   a row object, and never pass a framework object (e.g. an ORM row, HTTP request)
   inward.
3. **Business rules know nothing about details.** The database, the web, the UI,
   and the framework are plugins to the business rules — not the other way around.

## Crossing a Boundary Against the Flow of Control

Frequently control flows *outward* (a use case must invoke the database), but the
*source-code dependency* must still point *inward*. Resolve this with **DIP**:

- The inner layer (use case) declares an **interface** it needs (e.g.
  `OrderRepository`, `Presenter`).
- The outer layer (a gateway/DB adapter) **implements** that interface.
- At runtime the implementation is injected inward.

So: control flow (outer → inner → outer) is decoupled from source dependency
(always outer → inner). The interface is *owned by the inner layer*.

### Humble Object Pattern
To keep boundaries testable, split hard-to-test behavior (framework/UI code) from
easy-to-test behavior (policy). The "humble" side (view, DB row mapper) holds only
untestable glue; all logic moves inward where it can be unit-tested without the
framework.

## Placement Decision Procedure

For any class `X`, ask in order:
1. Is `X` an enterprise-wide business rule / critical data with methods, reusable
   across applications? → **Entities**.
2. Is `X` application-specific orchestration ("what the system does" for one use
   case)? → **Use Cases**.
3. Does `X` convert data between use-case form and an external form
   (controller, presenter, gateway, repository impl mapper)? → **Interface
   Adapters**.
4. Is `X` a framework, driver, DB client, web server, or main/wiring? →
   **Frameworks & Drivers**.

Then verify: does every `import`/reference in `X` point at its own circle or
*inward*? If any points outward, it is a **Dependency Rule violation** — fix by
introducing an inner-owned interface (DIP) and injecting the concretion.

## Audit Checklist (what a Dependency Auditor looks for)

- [ ] No inner module imports an outer module (grep imports against layer map).
- [ ] Entities reference no use case, adapter, or framework type.
- [ ] Use cases reference no controller, presenter, DB, or framework type — only
      inner-owned interfaces.
- [ ] Data crossing boundaries is a plain inner-defined DTO (no ORM/HTTP/UI types
      leaking inward).
- [ ] Every outward call from a use case goes through an interface it owns.
- [ ] `main` / composition root is the *only* place concretions are wired.

## Output Contract

Emit: `{placements:[{class, layer, justification}], violations:[{class, offending_import, direction, fix_via_DIP}], boundary_crossings:[{from, to, interface_owner, dto}]}`
