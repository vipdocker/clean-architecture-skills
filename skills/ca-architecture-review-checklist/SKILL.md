---
name: ca-architecture-review-checklist
description: A gate-style review checklist that scores a design/codebase against all Clean Architecture principles (Dependency Rule, SOLID, component cohesion/coupling, boundaries, testability) and returns pass/fail with severity-ranked findings. Use as the final quality gate before accepting an architecture or merging code, or to audit an existing system. Language-agnostic. Not for designing boundaries in the first place (use ca-layer-boundaries), for the cheaper pre-code dependency-direction audit (use ca-dependency-rule), or for judging whether the PROCESS itself needs tuning (use ca-process-tuning).
---
<!-- clean-architecture system v1.11.0 -->

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

## Delta Review Mode (for G5 re-runs after targeted fixes)

When invoked as a **delta review** (i.e. G5 is re-running after a localized fix
rather than a full implementation pass), restrict the review scope:

1. **Input:** a `scope` parameter listing the specific components/layers that were
   modified since the last G5 verdict (provided by the orchestrator via
   `cc_log.py`'s `gate_scope_narrowed` tracking).
2. **Procedure:** Run the full checklist (Sections A–E) only on the files within
   the specified scope. For files outside the scope, carry forward the prior
   verdict's findings unchanged.
3. **Cross-boundary check:** Even in delta mode, verify that the fix did not
   introduce NEW outward dependencies from unchanged components into the changed
   scope (a fix can break an innocent neighbor). Check imports FROM unchanged
   components INTO changed files.
4. **Output:** The same structured output contract, but `sections[].findings[]`
   only contains new/changed findings for the delta scope plus any cross-boundary
   violations discovered.
5. **Verdict logic:** Apply the same severity rules. If the delta scope is now
   clean and no cross-boundary violations exist, the overall verdict upgrades to
   the best of (prior verdict, current delta verdict).

This avoids re-examining the entire codebase when only one component was patched,
while still catching regressions at the boundary.

**Lifecycle contract:** A successful full or delta G5 (verdict ∈ {PASS,
PASS_WITH_CONCERNS}) MUST clear `g5_delta_scopes` in state.json — this is done
mechanically by `cc_log.py` when the passing `gate_verdict` event is recorded. If
`g5_delta_scopes` remains non-empty after a G5 run, it means the review did not
cover all pending scopes, and P6 entry will be blocked by the latch. Do not
manually clear it outside of a recorded passing G5 verdict.

**Scope coverage risk:** If the `scope` parameter passed to delta review does not
fully cover all components/layers that were actually modified, any un-covered
changes carry the old G5 judgment — risking regression. Therefore, delta G5 MUST
perform a **coverage diagnosis**: compare the input `delta_scope` against the
actual file diff since the prior G5 artifact snapshot. If uncovered modifications
are found, log them as a `mandatory_followup` with severity MAJOR so they are not
silently skipped. The orchestrator should either expand the scope or schedule a
follow-up review.

---

## Section A — The Dependency Rule (BLOCKER-weighted)
- [ ] No inner layer references any outer layer (entities→usecases→adapters→frameworks).
- [ ] Entities are pure (no framework/ORM/UI/DB imports).
- [ ] Use cases depend only on inner-owned interfaces for anything external.
- [ ] Data crossing boundaries is inner-defined DTOs (no ORM rows / HTTP / UI leaking inward).
- [ ] Concretions are wired only in `main`/composition root.

## Section B — SOLID (see `ca-solid-principles`)
- [ ] SRP: each class has exactly one actor / reason to change.
- [ ] OCP: expected variations are added, not edited (abstractions in place).
- [ ] LSP: no `instanceof`/type-switch ladders; implementations are substitutable.
- [ ] ISP: no client depends on interface methods it doesn't use.
- [ ] DIP: source dependencies point at stable abstractions.

## Section C — Component Cohesion & Coupling (see `ca-component-principles`)
- [ ] CCP: classes in a component change for the same reason/time.
- [ ] CRP: no component drags classes its clients don't need.
- [ ] REP: each component is independently releasable/versioned.
- [ ] ADP: component dependency graph is acyclic (topological sort passes).
- [ ] SDP: instability `I` decreases along every dependency arrow.
- [ ] SAP: stable components are abstract; no component in Zone of Pain/Uselessness (`D`→0).

## Section D — Boundaries & Layout (see `ca-layer-boundaries`)
- [ ] Boundary granularity matches the real axis of change (facade / one-dim / full).
- [ ] Screaming architecture: top-level dirs name the domain, not the framework.
- [ ] Framework/DB/UI are plugins at the edge; decisions deferred correctly.

## Section E — Testability
- [ ] Business rules are unit-testable without DB, web, or UI (Humble Object applied).
- [ ] Boundaries allow test doubles to be injected for every port.
- [ ] Tests do not depend on volatile details (no fragile "test-through-the-GUI").
- [ ] **Verification depth matches the risk**: every component with runtime-visible
      behavior (endpoint, UI, background job) has evidence from a real execution,
      not only from stubs. Stubs prove the wiring; only a real run proves the
      behavior.

## Scoring & Verdict

For each section, count findings by severity. Then:
- Any **BLOCKER** open → verdict **FAIL**.
- No BLOCKER but ≥1 MAJOR → **PASS_WITH_CONCERNS** (list mandatory follow-ups).
- Only MINOR/NIT → **PASS**.

### Precondition: no passing verdict without observation

Before applying the rules above, check what evidence you hold. For any component
with runtime-visible behavior where the only evidence is stubbed or mocked, you may
not issue `PASS` or `PASS_WITH_CONCERNS` for it — record a BLOCKER naming the
missing verification and return **FAIL**.

**A debt does not discharge missing evidence.** Run 3's round-2 review listed
`stubbed state verification` among its own accepted debts and passed anyway; the
live check six hours later found a MAJOR that stubs could not surface by
construction (a per-symbol coverage 503 escalated into a page-level blocker and
aborted the entire results table). Deferring a *fix* is a decision you can log;
deferring the *observation* is a guess you cannot.

## Output Contract

These keys are fixed. Run 3's `g5-review.json` omitted `sections`,
`mandatory_followups` and every finding's `scope`, described findings with
title/detail/why_it_matters instead of the contract fields, and added ten invented
top-level keys — so nothing downstream could consume it, and the delta review's
scope routing had no input at all.

```
{
  verdict: PASS | PASS_WITH_CONCERNS | FAIL,
  review_mode: "full" | "delta",
  delta_scope: ["component:layer", ...] | null,   // null when review_mode=full
  sections: {A:{score, findings[]}, B:{...}, C:{...}, D:{...}, E:{...}},
  findings: [{id, severity, section, scope, evidence, principle, recommended_fix}],
  mandatory_followups: [ ... ],   // for PASS_WITH_CONCERNS / FAIL
  checks_passed: [ ... ],         // what the review actually verified (see below)
  tracked_issues: [ ... ]         // recorded but NOT sign-off debts (see below)
}
```

**`checks_passed` — declare the coverage, not just the verdict.** One line per
check actually performed (dependency direction on the real graph, migration/init
schema consistency, idempotency semantics, live evidence per runtime component…).
Run 4 was the first run to do this (8 entries) and it is the single cheapest
improvement to review quality: an audit can tell what was *verified* apart from
what was merely *not flagged*.

**Debts vs tracked issues — two different things, don't conflate them.**

- `debts_awaiting_signoff[]` (on the verdict event) — concerns the user must
  *accept* before P6 closes; `cc_log.py` refuses the P6 exit until they are signed.
  Use for anything whose acceptance is a decision: known gaps, deferred work.
- `tracked_issues[]` (here) — observations recorded for the backlog with no
  acceptance decision attached: style idioms that match existing repo conventions,
  minor refactors, "watch this". Run 4 emitted PASS with a 3-item `debts` array
  whose entries were exactly this kind — NIT-level, no user decision needed —
  which sidestepped the sign-off machinery by mislabeling. Label them
  `tracked_issues` and the contract stops being ambiguous: PASS + non-empty
  `tracked_issues` is fine; PASS + debts is not (record PASS_WITH_CONCERNS
  instead, and let the sign-off do its job).

Every round's findings go into the one `findings` array — run 3's rounds 2 and 3
findings (including a MAJOR) never reached the artifact even though it was
rewritten. Per-round verdicts are separate `gate_verdict` events, not sibling keys.

Each finding must cite concrete evidence (file/class/import or design element),
the exact principle violated, and the **scope** (component:layer) it belongs to —
never a vague "looks off". The `scope` field enables precise routing of fixes back
to the responsible Implementer sub-agent.
