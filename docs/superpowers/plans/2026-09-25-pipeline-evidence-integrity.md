# Pipeline Evidence Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make clean-architecture-autopilot reject untraceable design drift and report only evidence-supported timing, authorization, verification, component, and baseline provenance claims.

**Architecture:** Keep `cc_log.py` as append-only event/state/report authority. Add `design_trace.py` for P2/P4/diff reconciliation and extend `dep_graph.py` with explicit component ownership rather than inferring components from module prefixes. Old run logs remain readable; absent new evidence yields `UNCHECKED` or `unsupported`, never invented precision.

**Tech Stack:** Python 3 standard library (`argparse`, `unittest`, `tempfile`, `ast`, `json`, `subprocess`), Bash installer, Markdown skill and agent contracts.

---

## File structure

- Create `skills/clean-architecture-autopilot/scripts/design_trace.py` — resolves a P2 plan, P4 task artifacts, amendments, and baseline diff into a deterministic trace verdict.
- Create `skills/clean-architecture-autopilot/scripts/test_design_trace.py` — isolated unit coverage for trace success, drift, amendment, and legacy degradation.
- Create `skills/clean-architecture-autopilot/scripts/test_cc_log.py` — temporary Git project integration tests for new events, state fold, and report claims.
- Modify `skills/clean-architecture-autopilot/scripts/cc_log.py` — event validation, state fields, baseline provenance, G3/G5 latches, pause accounting, and report rendering.
- Modify `skills/clean-architecture-autopilot/scripts/dep_graph.py` — P2 ownership ingestion and component-level edge/SCC output.
- Modify `skills/clean-architecture-autopilot/scripts/test_dep_graph.py` — ownership, shared adapter, unowned module, and component cycle tests.
- Modify `skills/clean-architecture-autopilot/SKILL.md` — protocol and gate contract.
- Modify `agents/ca-architecture-designer.md`, `agents/ca-dependency-auditor.md`, `agents/ca-clean-implementer.md`, `agents/ca-architecture-reviewer.md`, `agents/ca-requirements-analyst.md` — role responsibilities and consistent version marker.
- Modify `install.sh` — assert and copy the new checker.
- Modify `README.md` — one concise changelog entry describing v1.17 evidence semantics.

## Task 1: Add design trace checker and tests

**Files:**
- Create: `skills/clean-architecture-autopilot/scripts/design_trace.py`
- Create: `skills/clean-architecture-autopilot/scripts/test_design_trace.py`

- [ ] **Step 1: Write failing trace tests**

```python
class DesignTraceTests(unittest.TestCase):
    def test_unamended_task_file_drift_fails(self):
        result = design_trace.check(p2, [p4], [], changed_files=[])
        self.assertEqual("FAIL", result["verdict"])
        self.assertEqual("T-Q2", result["task_drift"][0]["task_id"])

    def test_matching_amendment_makes_task_trace_pass(self):
        amendments = [{"task_id": "T-Q2", "planned_files": ["old.py"],
                       "actual_files": ["new.py"], "reason": "rename"}]
        result = design_trace.check(p2, [p4], amendments, changed_files=[])
        self.assertEqual("PASS", result["verdict"])

    def test_missing_artifact_is_unchecked_not_pass(self):
        result = design_trace.check(p2, [], [], changed_files=[])
        self.assertEqual("UNCHECKED", result["verdict"])
```

Use `tempfile.TemporaryDirectory()` and JSON fixtures containing `dag_tasks`, `task_id`, and `files`.

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 skills/clean-architecture-autopilot/scripts/test_design_trace.py -v`

Expected: FAIL because `design_trace` does not exist.

- [ ] **Step 3: Implement deterministic checker**

Implement `load_json(path)`, `collect_p2_tasks(p2)`, `collect_p4_artifacts(run_dir)`, `collect_amendments(events)`, and:

```python
def check(p2_artifact, p4_artifacts, amendments, changed_files):
    """Return {verdict, task_drift, unimplemented_tasks, unclaimed_changes,
    approved_amendments, warnings}. Never treat unreadable input as PASS."""
```

Use exact normalized relative paths. A task file-set mismatch passes only when one amendment has the same `task_id`, `planned_files`, and `actual_files`; otherwise append a structured drift record. P2 with no `dag_tasks`, or a run with no P4 artifact, returns `UNCHECKED` and a reason. Extra changed files are `unclaimed_changes`, except `.cc-skill/` and accepted preexisting paths.

Add CLI options `--p2`, `--run-dir`, `--root`, `--baseline`, `--json`; emit JSON and exit 0 for PASS/UNCHECKED, 1 for FAIL, 2 for unreadable required CLI input.

- [ ] **Step 4: Run checker tests**

Run: `python3 skills/clean-architecture-autopilot/scripts/test_design_trace.py -v`

Expected: all trace tests pass.

## Task 2: Add component ownership graph support

**Files:**
- Modify: `skills/clean-architecture-autopilot/scripts/dep_graph.py:21-36,169-230`
- Create: `skills/clean-architecture-autopilot/scripts/test_dep_graph.py`

- [ ] **Step 1: Write failing ownership tests**

```python
class ComponentGraphTests(unittest.TestCase):
    def test_shared_adapter_is_its_own_component_edge(self):
        result = dep_graph.component_graph(graph, ownership)
        self.assertIn({"from": "home", "to": "context"}, result["edges"])
        self.assertIn({"from": "context", "to": "valuation"}, result["edges"])

    def test_unowned_module_is_reported(self):
        result = dep_graph.component_graph({"pkg.unowned": set()}, ownership=[])
        self.assertEqual(["pkg.unowned"], result["unowned_modules"])
```

Construct a three-module temporary Python project and a P2 JSON fixture with `component_map.ownership[]` entries.

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 skills/clean-architecture-autopilot/scripts/test_dep_graph.py -v`

Expected: FAIL because `component_graph` and `--component-map` do not exist.

- [ ] **Step 3: Implement ownership parsing and component output**

Add:

```python
def load_component_ownership(path):
    """Read component_map.ownership[] records {module, component, kind}.
    Return (mapping, errors); shared_adapter is a normal named component."""

def component_graph(module_graph, ownership):
    """Return component_edges, component_sccs, unowned_modules, ownership_errors."""
```

Add CLI `--component-map`. Preserve existing module graph behavior when absent. When present, add `component_graph` to the JSON output and print counts. Ownership must be one-to-one for each scanned module; duplicate ownership or unknown `kind` becomes an ownership error. The checker reports rather than guesses ownership.

- [ ] **Step 4: Run graph tests**

Run: `python3 skills/clean-architecture-autopilot/scripts/test_dep_graph.py -v`

Expected: all ownership tests pass; existing `dep_graph.py --root ...` invocation remains backward compatible.

## Task 3: Extend cc_log event/state protocol

**Files:**
- Modify: `skills/clean-architecture-autopilot/scripts/cc_log.py:123-162,322-424,599-705,708-1770,2198-2230`
- Modify: `skills/clean-architecture-autopilot/scripts/test_cc_log.py`

- [ ] **Step 1: Write failing protocol tests**

```python
class CCLogProtocolTests(unittest.TestCase):
    def test_adopted_records_increment_ledger_with_basis(self): ...
    def test_reused_signoff_does_not_duplicate_debt_or_question(self): ...
    def test_unmatched_reused_signoff_is_rejected(self): ...
    def test_work_lifecycle_is_tracked_per_activity(self): ...
    def test_baseline_accept_only_allows_init_dirty_path_and_hash(self): ...
    def test_verification_record_requires_counts_and_warning_reasons(self): ...
```

Each test must initialize a temporary Git repo, run `cc_log.py init`, append events through `subprocess.run`, and inspect `state.json` plus `run.jsonl`. For the baseline case, create `input.md` before init, alter a second file after init, then assert only the hashed initial file can be accepted.

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 skills/clean-architecture-autopilot/scripts/test_cc_log.py -v`

Expected: failures for missing new event validation/state fields.

- [ ] **Step 3: Implement append-only events and state folds**

Add event constants and validation for:

```text
work_started, work_finished, design_amendment,
verification_recorded, debt_signoff_reused, baseline_accept
```

`work_started/work_finished` require `activity_id`; P4 events also require `component`, `logical_wave_id`, and `execution_batch_id`. `_scan_log()` must track open activities independently from open component dispatches. `_apply_state_effects()` must store `activity_lifecycle`, `design_amendments`, `verification_records`, `baseline_acceptances`, and `question_ledger.self_resolved_records`.

For `adopted_without_asking`, require a non-empty basis for each record. Accept existing dict/list/int shapes for compatibility, but only dict/list entries create named records. `debt_signoff_reused` must validate exact current debts and an earlier real signoff/user-loop chain, then clear current debts without appending a duplicate signed debt or changing question counts.

`baseline_accept` must validate the path against manifest `preexisting_files[]` and verify the supplied hash equals the recorded init hash. Add repeated init CLI option `--preexisting` only if needed to surface records; otherwise derive records automatically from dirty Git status and SHA-256 at init.

- [ ] **Step 4: Run protocol tests**

Run: `python3 skills/clean-architecture-autopilot/scripts/test_cc_log.py -v`

Expected: all protocol tests pass.

## Task 4: Gate trace, component audit, and verification evidence

**Files:**
- Modify: `skills/clean-architecture-autopilot/scripts/cc_log.py:972-1016,1205-1275,1591-1625`
- Modify: `skills/clean-architecture-autopilot/scripts/test_cc_log.py`

- [ ] **Step 1: Write failing gate tests**

```python
class CCLogGateTests(unittest.TestCase):
    def test_passing_g5_rejects_failed_design_trace(self): ...
    def test_g3_rejects_missing_component_graph_evidence_when_map_declared(self): ...
    def test_open_gate_activity_prevents_idle_pause_credit(self): ...
    def test_passing_verification_with_warnings_requires_acceptance_reason(self): ...
```

Create minimal `artifacts/p2-design.json`, `artifacts/p4-T1.json`, `artifacts/g3-audit.json`, and `artifacts/g5-review.json` fixtures. Invoke a G5 PASS verdict after trace drift and assert exit code 2. For the pause test, start `g5-full-review`, advance the event time through an injected clock helper or direct pure helper test, and assert `credited_to_idle` is false.

- [ ] **Step 2: Run gate tests to verify failure**

Run: `python3 skills/clean-architecture-autopilot/scripts/test_cc_log.py -v`

Expected: G5 and verification tests fail before the latches exist.

- [ ] **Step 3: Implement latches**

At G3 verdict, when P2 declares `component_map.ownership`, require `artifacts/g3-audit.json` to carry `component_graph` with `unowned_modules` and `component_edges`; reject unknown ownership or component SCCs unless the audit finding explicitly records a non-blocking legacy exception.

At G5 passing verdict, invoke `design_trace.check`. Reject `FAIL`; permit `UNCHECKED` only when the G5 detail includes `trace_degraded_reason`. Require each `verification_recorded` record claiming `PASS_WITH_ACCEPTED_WARNINGS` to include an artifact, integer counts, and a reason for every accepted warning. Do not require any particular scanner; require honest records when a scanner is claimed.

Change pause classification to consider open `work_started` activities. Include `activities_in_flight` in the pause detail.

- [ ] **Step 4: Run gate tests**

Run: `python3 skills/clean-architecture-autopilot/scripts/test_cc_log.py -v`

Expected: all G3/G5, pause, and warning-ledger tests pass.

## Task 5: Make report conservative and provenance-aware

**Files:**
- Modify: `skills/clean-architecture-autopilot/scripts/cc_log.py:1835-2196`
- Modify: `skills/clean-architecture-autopilot/scripts/test_cc_log.py`

- [ ] **Step 1: Write failing report tests**

```python
class CCLogReportTests(unittest.TestCase):
    def test_batched_run_omits_parallelism_and_component_duration_claims(self): ...
    def test_lifecycle_run_reports_supported_component_duration(self): ...
    def test_report_lists_warning_counts_and_accepted_reasons(self): ...
    def test_report_separates_accepted_preexisting_input_from_run_changes(self): ...
```

Assert report text contains `timestamp_granularity=batched` and `不可归因` for dispatch/done-only events. Assert it contains a lifecycle duration only after matching `work_started/work_finished`. Assert `0 errors / 6 warnings` and accepted reason render exactly. Assert accepted initial input is absent from “本轮新增” and present in a dedicated provenance section.

- [ ] **Step 2: Run report tests to verify failure**

Run: `python3 skills/clean-architecture-autopilot/scripts/test_cc_log.py -v`

Expected: report assertions fail against the current optimistic output.

- [ ] **Step 3: Implement conservative rendering**

Build component spans from lifecycle events only. If any P4 DONE component lacks a finished lifecycle pair, set `timestamp_granularity` to `batched`, omit P4 parallelism/model ROI, and render the dispatch-to-done values only as “batch window, not worker duration”.

Render a verification section from `verification_records` with counts and accepted reasons. Render a baseline provenance section with accepted and unaccepted initial dirt. Exclude every manifest `preexisting_files` path from new-file totals, regardless of later commit state. Preserve old report sections for old logs, with explicit unsupported labels.

- [ ] **Step 4: Run report tests**

Run: `python3 skills/clean-architecture-autopilot/scripts/test_cc_log.py -v`

Expected: all report assertions pass.

## Task 6: Update the skill contract, agents, installer, and version markers

**Files:**
- Modify: `skills/clean-architecture-autopilot/SKILL.md`
- Modify: `agents/ca-architecture-designer.md`
- Modify: `agents/ca-dependency-auditor.md`
- Modify: `agents/ca-clean-implementer.md`
- Modify: `agents/ca-architecture-reviewer.md`
- Modify: `agents/ca-requirements-analyst.md`
- Modify: `install.sh:73-81,172-180`
- Modify: `README.md`

- [ ] **Step 1: Write failing static contract test**

Add to `test_cc_log.py`:

```python
def test_skill_contract_names_all_new_events_and_design_trace():
    text = SKILL.read_text()
    for token in ("design_amendment", "work_started", "work_finished",
                  "verification_recorded", "debt_signoff_reused",
                  "baseline_accept", "design_trace.py", "component_map.ownership"):
        self.assertIn(token, text)
```

Also assert `install.sh` lists `design_trace.py` in both preflight and post-copy loops and each skill/agent version is `1.17.0`.

- [ ] **Step 2: Run static test to verify failure**

Run: `python3 skills/clean-architecture-autopilot/scripts/test_cc_log.py -v`

Expected: FAIL because v1.16.0 documents do not contain the new protocol.

- [ ] **Step 3: Update contracts**

Bump the system marker/frontmatter to `1.17.0`. Document exact JSON examples for all six events and the P2 `component_map.ownership[]` schema. Assign responsibilities:

- architecture designer declares ownership and logs amendments;
- dependency auditor writes component graph evidence;
- clean implementer emits worker lifecycle and verification records;
- architecture reviewer consumes trace/verification evidence and uses `debt_signoff_reused` only for unchanged debt lists;
- requirements analyst records each self-resolved decision with source basis.

Update install loops to require and verify `design_trace.py`. Update README with one concise v1.17.0 evidence-integrity entry.

- [ ] **Step 4: Run contract tests and installer syntax check**

Run: `python3 skills/clean-architecture-autopilot/scripts/test_cc_log.py -v && bash -n install.sh && bash -n uninstall.sh`

Expected: all Python tests pass; both shell scripts have exit code 0.

## Task 7: Run complete non-mutating verification

**Files:**
- Verify only all files changed by Tasks 1-6.

- [ ] **Step 1: Run complete Python test suite**

Run:

```bash
python3 skills/clean-architecture-autopilot/scripts/test_design_trace.py -v && \
python3 skills/clean-architecture-autopilot/scripts/test_dep_graph.py -v && \
python3 skills/clean-architecture-autopilot/scripts/test_cc_log.py -v
```

Expected: every test passes.

- [ ] **Step 2: Run syntax and compile checks**

Run:

```bash
python3 -m py_compile \
  skills/clean-architecture-autopilot/scripts/cc_log.py \
  skills/clean-architecture-autopilot/scripts/dep_graph.py \
  skills/clean-architecture-autopilot/scripts/design_trace.py && \
bash -n install.sh && bash -n uninstall.sh
```

Expected: exit code 0 with no syntax errors.

- [ ] **Step 3: Review changed-file scope**

Run: `git diff --check && git status --short`

Expected: no whitespace errors; only the planned scripts, tests, contracts, installer, README, and design/plan documents are modified or untracked.

- [ ] **Step 4: Do not commit or install**

A local commit and `./install.sh` both mutate additional state. They require separate explicit user authorization after review of the verified diff.

## Spec coverage review

- P2/P4/diff traceability: Tasks 1 and 4.
- Component ownership and shared adapters: Task 2 and G3 portion of Task 4.
- Lifecycle timing and gate activity: Tasks 3–5.
- Question ledger and signoff reuse: Task 3.
- Verification counts and accepted warnings: Tasks 3–5.
- Dirty baseline provenance: Tasks 3 and 5.
- Skill/agent/install synchronization: Task 6.
- Regression and non-mutating verification: Task 7.

The plan intentionally omits commits and installation because neither has been authorized.
