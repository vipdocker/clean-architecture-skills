---
name: use-case-extraction
description: Extracts Entities and Use Cases from raw requirements the Clean Architecture way — separating enterprise business rules (Entities) from application business rules (Use Cases), and defining request/response models plus the interactor for each. Use at the start of a design, when turning a PRD/feature request/user story into a policy-first model before choosing any framework or database. Language-agnostic.
version: 1.0.0
---

# Use Case & Entity Extraction

The Clean Architecture way to start: identify the **business rules** first,
defer the database, the web, and the framework. Requirements become Entities and
Use Cases *before* any detail is chosen.

## Step 1 — Separate the Two Kinds of Business Rule

- **Enterprise business rules → Entities.** Rules and critical data that would be
  true even if the application didn't exist / were done by hand. They outlive any
  single application. Nouns with invariants + behavior (not data bags).
- **Application business rules → Use Cases.** Rules that exist *because* this
  application automates something: the sequence of steps, what input is accepted,
  what is validated when, what output is produced. "How and when the entities are
  invoked."

Test: "Would this rule still hold if we ran the business on paper?" Yes → Entity.
"Is this a step the *software* performs to deliver a user goal?" → Use Case.

## Step 2 — Write Each Use Case as an Interactor Contract

For every use case, define four things (all inner-owned, framework-free):

1. **Name** — a verb-phrase goal, e.g. "Place Order", "Register Customer".
2. **Request model (input data)** — a plain DTO of exactly the fields the use case
   needs. NOT the HTTP request, NOT a UI form object.
3. **Response model (output data)** — a plain DTO of what the use case produces.
   NOT an Entity, NOT a DB row.
4. **Steps** — the ordered application rules: validations, entity invocations,
   gateway calls (through interfaces the use case owns), and the outcome.

Also list the **ports** each interactor needs:
- Input boundary (interface the controller calls),
- Output boundary (interface the presenter implements),
- Data-access interfaces (repository/gateway interfaces the use case owns).

## Step 3 — Keep Details Out

At this stage do NOT decide:
- which database (SQL/NoSQL/file) — it's a detail behind a repository interface;
- which web framework or UI — a detail behind controller/presenter;
- which delivery mechanism — a plugin.

If a requirement mentions a technology, restate it as a capability behind a port.
("Store in Postgres" → "persist via `OrderRepository`").

## Step 4 — Derive the Entity Model

For each Entity: its critical data, its invariants (rules it always enforces), and
its methods (behavior). Entities must not reference use cases, adapters, or
frameworks.

## Worked Micro-Example (pseudocode)

```
Entity: Order
  data: id, lines[], customerId, status
  invariant: total() >= 0; cannot confirm() an empty order
  behavior: addLine(), total(), confirm()

UseCase: PlaceOrder
  request : { customerId, items:[{sku, qty}] }
  response: { orderId, total, status }
  ports   : input=PlaceOrderInput, output=PlaceOrderOutput,
            data=OrderRepository, ProductCatalog
  steps:
    1. validate items non-empty            (application rule)
    2. load products via ProductCatalog    (through owned interface)
    3. build Order entity, addLine per item
    4. order.confirm()                      (enterprise rule enforced by entity)
    5. OrderRepository.save(order)          (through owned interface)
    6. present PlaceOrderOutput(response)
```

## Output Contract

Emit:
```
{
  entities: [{name, data[], invariants[], behavior[]}],
  use_cases: [{name, request_model, response_model,
               ports:{input, output, data[]}, steps[]}],
  deferred_details: [{requirement, restated_as_port}]
}
```
This artifact is the input to `layer-boundaries` and the architecture design phase.
