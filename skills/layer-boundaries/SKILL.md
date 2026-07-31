---
name: layer-boundaries
description: Defines the four Clean Architecture layers (Entities, Use Cases, Interface Adapters, Frameworks & Drivers), what belongs in each, the data structures that cross boundaries, and how to draw partial/full boundaries at the right cost. Use when scaffolding a project layout, deciding boundary granularity, or designing the interfaces that separate policy from detail. Language-agnostic.
version: 1.0.0
---

# Layers & Boundaries

Clean Architecture organizes software into four layers separated by architectural
boundaries. This skill defines each layer's responsibility and how boundaries are
drawn and priced.

## The Four Layers

### 1. Entities (Enterprise Business Rules)
- Encapsulate the most general, highest-level rules and *critical business data*.
- A pure business object with methods — not a data bag, not an ORM entity.
- Least likely to change when something external changes.
- Know nothing about use cases, databases, or frameworks.

### 2. Use Cases (Application Business Rules)
- Application-specific rules: orchestrate the flow of data to/from entities to
  achieve one user goal ("what the application does").
- Contain **Interactors**, request/response models (input/output data), and the
  **boundary interfaces** (input boundary, output boundary, data-access
  interfaces) the interactor depends on.
- Depend on Entities; depend on *interfaces* (not implementations) for everything
  external.

### 3. Interface Adapters
- Convert data between the form convenient for use cases/entities and the form
  convenient for external agencies.
- Homes for: **Controllers** (external input → use-case request), **Presenters**
  (use-case response → view model), **Gateways/Repository implementations**
  (use-case data interface → DB/queries), and mappers.
- No business rules here — only conversion.

### 4. Frameworks & Drivers
- The outermost layer: the database, the web framework, the UI, message buses,
  device drivers, and `main`/composition root.
- Almost all glue code; "details" that plug into the inner circles.

## Data Structures That Cross Boundaries

| Crossing | Structure | Owned by | Never |
|---|---|---|---|
| Controller → Use case | Request/Input model (DTO) | Use case layer | pass HTTP request object inward |
| Use case → Presenter | Response/Output model (DTO) | Use case layer | pass an Entity outward |
| Use case ↔ Gateway | plain query/result DTO | Use case layer | pass ORM row inward |
| Presenter → View | View Model | Adapter layer | put logic in the view |

Rule: cross with **isolated, simple, inner-defined data structures**; do not cross
with framework objects or with entities.

## Boundary Granularity — Pay Only For What You Need

Boundaries are expensive (interfaces, DTOs, indirection). Draw them where axes of
change and independent-deployability actually exist. Choose the cheapest boundary
that buys the decoupling you need:

1. **Full boundary** — reciprocal interfaces + input/output data structures + a
   separate component/build unit. Highest cost; use across service or team seams.
2. **One-dimensional boundary** — a single interface (Strategy-like) separating
   two sides. Moderate cost; a common default.
3. **Facade boundary** — a facade class hides a subsystem; cheapest, but the client
   still transitively depends on the hidden classes. Use early / low-risk seams.

You can start with a facade and *promote* it to a full boundary later as the axis
of change proves real. This is the "deferring decisions" benefit of a good
architecture — keep options open.

## Screaming Architecture

The top-level directory structure should *scream the domain* (e.g. `ordering/`,
`billing/`, `shipping/`), not the framework (`controllers/`, `models/`). A newcomer
should see *what the system does*, not *what framework it uses*. The framework is a
detail deferred to the edges.

## Reference Layout (language-agnostic)

```
<domain-name>/
  entities/            # enterprise business rules (pure)
  usecases/            # interactors + request/response models + boundary interfaces
    ports/             # inner-owned interfaces (input, output, repository)
  adapters/            # controllers, presenters, gateways/repo impls, mappers
  frameworks/          # db, web, ui, external clients
  main/                # composition root — the ONLY place concretions are wired
```

## Output Contract

Emit: `{layers:{entities[], usecases[], ports[], adapters[], frameworks[]}, boundary_crossings:[{name, dto, direction}], boundary_choices:[{seam, type: full|onedim|facade, rationale}], directory_tree}`
