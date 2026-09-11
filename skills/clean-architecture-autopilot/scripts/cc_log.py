#!/usr/bin/env python3
"""cc_log.py — deterministic logger for clean-architecture-autopilot.

Purpose: make .cc-skill/ logging a MECHANICAL command call instead of prose the
model might forget. The orchestrator calls this at bootstrap and at every
phase/gate transition.

Usage:
  # 1) create .cc-skill/<slug>/ (+ artifacts/), write manifest.json + initial state.json
  python3 cc_log.py init --root <project_dir> --slug <task-slug> --title "<task title>"

  # 2) append one event to run.jsonl AND refresh state.json in one atomic step
  python3 cc_log.py event --root <project_dir> --slug <task-slug> \
      --phase P2 --event phase_enter [--agent ca-architecture-designer] \
      [--skills a,b] [--superpowers c,d] [--verdict APPROVED] \
      [--status in_progress] [--detail '{"k":"v"}'] [--duration-ms 1234] \
      [--scope 'component:layer,...']

  # 2b) scoped P4 re-entry (delta review instead of full g5 invalidation)
  python3 cc_log.py event --root <project_dir> --slug <task-slug> \
      --phase P4 --event phase_enter --scope "ordering:adapters,billing:usecases" \
      --status in_progress

  # 2c) one batched USER LOOP pause (<=4 questions == one AskUserQuestion call).
  #     Information-gap triggers must show what was looked up first; questions
  #     answered from disk go in adopted_without_asking instead of being asked.
  python3 cc_log.py event --root <project_dir> --slug <task-slug> \
      --phase P1 --event user_loop --status awaiting_user \
      --detail '{"trigger":"business_rule",
                 "questions":["is a partial refund allowed?","who may void an order?"],
                 "resolution_attempted":["P0 codebase_notes","docs/specs/design.md 5.6"],
                 "adopted_without_asking":{"OQ-2":"single normalization path already in code"}}'

  # 2d) accepting a MAJOR as debt: bind the user's answer to this verdict's
  #     exact debt question (`logged seq N` from the first command)
  python3 cc_log.py event --root <project_dir> --slug <task-slug> \
      --phase G5 --event user_loop --status awaiting_user \
      --detail '{"trigger":"debt_signoff","debts":["F-01"],
                 "questions":["accept F-01 as tracked debt?"]}'
  python3 cc_log.py event --root <project_dir> --slug <task-slug> \
      --phase G5 --event debt_signoff \
      --detail '{"answer":"SIGNED OFF as debt","user_loop_seq":N,
                 "debts":["F-01"],"required_followup":"separate task: ..."}'

  # 3) (optional) just overwrite state.json from a full JSON blob
  python3 cc_log.py state --root <project_dir> --slug <task-slug> --json '<state json>'

  # 4) write summary.md at DONE
  python3 cc_log.py summary --root <project_dir> --slug <task-slug> --body-file <path>

Notes:
- Never mutates prior run.jsonl lines (append-only). state.json is overwritten
  atomically (temp file + os.replace).
- If <root> is not writable, falls back to $CC_SKILL_FALLBACK or ./.cc-skill and
  records the resolved path in manifest.json.resolved_root.
- Secrets: this script writes exactly what you pass; do not pass tokens/keys.
"""
import argparse, json, os, sys, tempfile, time, datetime, re


class CCLogError(Exception):
    """Raised when logging cannot proceed. main() turns this into a clear stderr
    message plus exit code 2, so the orchestrator can tell "log failed" apart
    from "log written" instead of dying on a raw traceback mid-phase.

    hint: remediation line shown only when it actually applies (e.g. an
    unwritable target), so a JSON typo is never answered with a disk-path tip.
    """

    def __init__(self, msg, hint=None):
        super().__init__(msg)
        self.hint = hint


FALLBACK_HINT = "set CC_SKILL_FALLBACK=<writable dir> and retry"

# The mechanical half of the SKILL.md enforcement rule: entering these phases
# requires the named gate to already carry a verdict. Derived from a real
# failure — the first production run entered P4 while gate_verdicts.g3 was still
# null, and nobody noticed until the user happened to ask. Prose did not hold,
# so the writer refuses the transition instead.
PHASE_GATE_PREREQ = {"P4": "g3", "P6": "g5"}

# Only these verdicts open the gate. Run 2 review found the latch accepted any
# non-null verdict — so a recorded REVISE_REQUIRED would have let P4 through.
GATE_PASSING = {"g3": {"APPROVED"}, "g5": {"PASS", "PASS_WITH_CONCERNS"}}

# Re-entering these phases makes the named gate's verdict stale: the work that
# verdict certified is about to change. From run 2 — P4 was re-entered after G5
# passed, the promised delta review never happened, and P6 sailed through on a
# verdict that predated ~2300 new lines.
PHASE_INVALIDATES = {"P2": "g3", "P4": "g5"}

# Re-entering a phase also un-finishes everything downstream of it. Run 3 closed
# P6 at 01:34, then a live-verification FAIL reopened P4 twice; `completed_phases`
# still listed P6 from the first closure, so a resume would have believed the run
# was done. Invalidating the gate verdict alone was not enough — the phase ledger
# has to retract too.
PHASE_REOPENS = {"P2": ["G3", "P4", "G5", "P6"], "P4": ["G5", "P6"]}

# SKILL.md caps both gate loops at 2 ("if still failing, escalate an open_question
# to the user"). Run 3 spent 2 real G5 rounds while state.loops stayed 0, because
# loop_increment only ever reached the append-only log and nothing read it back.
MAX_GATE_ITERATIONS = 2

# The four USER LOOP triggers from SKILL.md, split by what the pause is actually
# for. This distinction is the whole point: `user_loop` was the only significant
# event in the system with no required detail fields, so nothing could tell an
# unavoidable authority decision from a question the agent could have answered
# itself off disk.
USER_LOOP_TRIGGERS = {"business_rule", "tech_choice",
                      "gate_overflow", "debt_signoff"}

# Information gaps: the answer exists somewhere (P0 codebase_notes, the design
# source, the P2 artifact) and asking is a *substitute* for looking. These must
# prove a lookup was attempted. Run 2's P1 pause resolved 3 of 7 open questions
# this way (`adopted_without_asking`) and asked the other 4 in one batch — the
# best question discipline of the three runs, invented ad hoc, recorded nowhere,
# and therefore never repeated: run 3 emitted zero user_loop events.
#
# The other two triggers (gate_overflow, debt_signoff) are authority decisions,
# not information gaps — no amount of reading grants the agent permission to
# accept a MAJOR as debt. Requiring "did you look it up first?" there would
# punish exactly the questions that must be asked.
INFO_GAP_TRIGGERS = {"business_rule", "tech_choice"}

# AskUserQuestion accepts at most 4 questions per call, so a pause claiming more
# than 4 could not have been one interaction. The cap is what makes the batching
# rule mechanical rather than aspirational: N questions must cost 1 round trip,
# not N. grill-me's "one question at a time" is correct for an interactive design
# interview and wrong here — across three runs the human was idle 15%/71%/71% of
# wall clock, so serializing questions multiplies the scarcest resource.
MAX_BATCH_QUESTIONS = 4

# A gap this long with no events means nobody was working: run 3 has 6h18m of
# silence between seq 37 and seq 38, which made P6's duration_ms read 398 minutes.
# Gaps are annotated (not refused) and then subtracted from phase durations.
PAUSE_GAP_MS = 30 * 60 * 1000


def parse_json_arg(raw, flag):
    """Parse a JSON CLI argument, naming the legal shape on failure so the caller
    can self-correct without reading this source."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        raise CCLogError(
            f"{flag} is not valid JSON ({e}). Expected a JSON object, e.g. "
            f"{flag} '{{\"reason\":\"boundary ambiguous\",\"count\":2}}'")


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")

def slugify(s):
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    # Strip AFTER truncating: cutting at 40 can land on a separator, which is how
    # run 3 ended up with the slug "option-seller-phase-a-covered-call-cash-".
    return s[:40].strip("-") or "run"

def resolve_dir(root, slug):
    base = os.path.join(root, ".cc-skill")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError as primary:
        # <root> unwritable (read-only mount, missing parent, permissions):
        # retry under the fallback root before giving up.
        base = os.path.join(os.environ.get("CC_SKILL_FALLBACK", "."), ".cc-skill")
        try:
            os.makedirs(base, exist_ok=True)
        except OSError as fallback:
            raise CCLogError(
                f"cannot create .cc-skill under {root} ({primary}) nor under "
                f"fallback {base} ({fallback})", hint=FALLBACK_HINT)
    # collision handling: keep distinct runs
    d = os.path.join(base, slug)
    if os.path.exists(d) and os.environ.get("CC_LOG_NO_SUFFIX") != "1":
        # only suffix on init; event/state/summary reuse the newest matching dir
        pass
    return d

def atomic_write(path, text):
    d = os.path.dirname(path)
    tmp = None
    try:
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError as e:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)   # never leave a half-written .tmp behind
            except OSError:
                pass
        raise CCLogError(f"cannot write {path}: {e}", hint=FALLBACK_HINT)

def load_state(run_dir):
    p = os.path.join(run_dir, "state.json")
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return {}
    return {}

def cmd_init(a):
    slug = a.slug or slugify(a.title)
    run_dir = resolve_dir(a.root, slug)
    # collision: append timestamp if exists and not empty
    if os.path.isdir(run_dir) and os.listdir(run_dir):
        run_dir = run_dir + "-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    os.makedirs(os.path.join(run_dir, "artifacts"), exist_ok=True)
    run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + slug
    manifest = {
        "run_id": run_id, "task_title": a.title or slug, "slug": slug,
        "start": now_iso(), "end": None, "resolved_root": os.path.abspath(run_dir),
        "config": {"max_parallel": a.max_parallel},
    }
    atomic_write(os.path.join(run_dir, "manifest.json"),
                 json.dumps(manifest, ensure_ascii=False, indent=2))
    state = {
        "run_id": run_id, "task_title": a.title or slug,
        "current_phase": "INIT", "phase_status": "in_progress",
        "completed_phases": [], "gate_verdicts": {"g3": None, "g5": None},
        "loops": {"gate3_iterations": 0, "gate5_iterations": 0},
        "artifact_pointers": {}, "p4_components": [],
        "pending_user_question": None, "open_questions": [], "debts": [],
        "question_ledger": {"asked": 0, "self_resolved": 0},
        "next_action": "run P0/P1", "updated_at": now_iso(),
    }
    atomic_write(os.path.join(run_dir, "state.json"),
                 json.dumps(state, ensure_ascii=False, indent=2))
    # seed run.jsonl
    with open(os.path.join(run_dir, "run.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now_iso(), "run_id": run_id, "seq": 0,
                            "phase": "INIT", "event": "init",
                            "detail": {"slug": slug}}, ensure_ascii=False) + "\n")
    print(os.path.abspath(run_dir))

def _find_run_dir(root, slug):
    base = os.path.join(root, ".cc-skill")
    cands = [os.path.join(base, slug)]
    if os.path.isdir(base):
        cands += sorted([os.path.join(base, d) for d in os.listdir(base)
                         if d.startswith(slug + "-")], reverse=True)
    for c in cands:
        if os.path.isdir(c):
            return c
    # fallback location
    fb = os.path.join(os.environ.get("CC_SKILL_FALLBACK", "."), ".cc-skill", slug)
    return fb if os.path.isdir(fb) else cands[0]

def next_seq(run_dir):
    p = os.path.join(run_dir, "run.jsonl")
    if not os.path.exists(p):
        return 0
    n = 0
    with open(p, encoding="utf-8") as f:
        for _ in f:
            n += 1
    return n

def _parse_ts(ts):
    try:
        return datetime.datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _scan_log(run_dir):
    """Read run.jsonl once and return the facts the latches need.

    - enters/exits: per-phase phase_enter / phase_exit counts. A phase_exit with
      no open enter is a logging defect: run 3 appended a second `P6 phase_exit`
      (seq 46) with only one `phase_enter` (seq 36), so the run recorded two
      closures for one entry.
    - last_ts: timestamp of the newest event, used to spot an unlogged gap.
    - enter_ts / pause_ms_since_enter: per-phase, for pause-corrected durations.
    - dispatch_ts / pause_ms_since_dispatch: the same per component, so a
      component_done can be timed from its agent_dispatch.
    - open_components: dispatched but not yet done — a gap with work in flight
      is silence, not idleness (run 5's T4 did 37.8 min of silent work and the
      pause heuristic charged it to idle, recording duration 0).
    - user_loop_triggers: which USER LOOP triggers have already been recorded.
    - latest_debt_user_loop_seq / latest_g5_pwc_seq: the exact authority chain for
      a debt sign-off. Existence alone was too weak: an old debt question could be
      reused against a new G5 verdict. A sign-off must reference the latest ask,
      and that ask must follow the current PASS_WITH_CONCERNS verdict.
    """
    facts = {"enters": {}, "exits": {}, "last_ts": None,
             "enter_ts": {}, "pause_ms_since_enter": {},
             "dispatch_ts": {}, "pause_ms_since_dispatch": {},
             "open_components": set(),
             "user_loop_triggers": set(),
             "latest_debt_user_loop_seq": None,
             "latest_g5_pwc_seq": None}
    p = os.path.join(run_dir, "run.jsonl")
    if not os.path.exists(p):
        return facts
    with open(p, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            ph, ev = r.get("phase"), r.get("event")
            det = r.get("detail") or {}
            if r.get("ts"):
                facts["last_ts"] = r["ts"]
            if ev == "phase_enter":
                facts["enters"][ph] = facts["enters"].get(ph, 0) + 1
                facts["enter_ts"][ph] = r.get("ts")
                # a fresh entry restarts the pause tally for this phase
                facts["pause_ms_since_enter"][ph] = 0
            elif ev == "phase_exit":
                facts["exits"][ph] = facts["exits"].get(ph, 0) + 1
            elif ev == "agent_dispatch":
                comp = det.get("component") or det.get("task")
                if comp:
                    facts["dispatch_ts"][comp] = r.get("ts")
                    facts["pause_ms_since_dispatch"][comp] = 0
                    facts["open_components"].add(comp)
            elif ev == "component_done":
                comp = det.get("component") or det.get("task")
                facts["open_components"].discard(comp)
            elif ev == "user_loop":
                trig = det.get("trigger")
                if trig:
                    facts["user_loop_triggers"].add(trig)
                if trig == "debt_signoff":
                    facts["latest_debt_user_loop_seq"] = r.get("seq")
            elif (ev == "gate_verdict" and ph == "G5"
                  and r.get("verdict") == "PASS_WITH_CONCERNS"):
                facts["latest_g5_pwc_seq"] = r.get("seq")
            elif ev == "pause":
                gap = det.get("gap_ms") or 0
                # Match the writer: only idle-credited pauses subtract. Older
                # logs predate the flag and default to idle. Without this check
                # the replayed ledger re-charges an in-flight gap to the very
                # component it was exempting.
                if det.get("credited_to_idle", True):
                    for k in facts["pause_ms_since_enter"]:
                        facts["pause_ms_since_enter"][k] += gap
                    for k in facts["pause_ms_since_dispatch"]:
                        facts["pause_ms_since_dispatch"][k] += gap
    return facts


def _elapsed_ms_since_enter(facts, phase):
    """Wall-clock since this phase's latest phase_enter, minus annotated pauses,
    used to auto-fill duration_ms on phase_exit. Both early runs left duration_ms
    empty, so ca-process-tuning's cost section had nothing to work with; run 3 then
    showed the opposite failure — a 6h18m unlogged gap inflated P6 to 398 minutes.
    Returns None when no matching enter exists (degraded logs)."""
    then = _parse_ts(facts["enter_ts"].get(phase))
    if then is None:
        return None
    elapsed = int((datetime.datetime.now().astimezone() - then).total_seconds() * 1000)
    return max(0, elapsed - facts["pause_ms_since_enter"].get(phase, 0))


def _elapsed_ms_since_dispatch(facts, component):
    """Same idea one level down: time this component actually took, from its
    agent_dispatch to now. All three production runs emitted zero agent_dispatch
    events, so P4 — 79% of run 3's effective work — could only be attributed to
    batch windows, never to a single component. Pairing the two events makes the
    per-component cost fall out for free.
    Returns None when the component was never dispatched."""
    then = _parse_ts(facts["dispatch_ts"].get(component))
    if then is None:
        return None
    elapsed = int((datetime.datetime.now().astimezone() - then).total_seconds() * 1000)
    return max(0, elapsed - facts["pause_ms_since_dispatch"].get(component, 0))

def _append_jsonl(run_dir, rec):
    """Append one event. Never rewrites prior lines."""
    try:
        with open(os.path.join(run_dir, "run.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        raise CCLogError(f"cannot append run.jsonl in {run_dir}: {e}",
                         hint=FALLBACK_HINT)


def _event_rec(run_id, seq, phase, event, detail, agent=None, skills=None,
               superpowers=None, verdict=None, duration_ms=None):
    return {"ts": now_iso(), "run_id": run_id, "seq": seq, "phase": phase,
            "event": event, "agent": agent, "skills": skills or [],
            "superpowers": superpowers or [], "verdict": verdict,
            "detail": detail or {}, "duration_ms": duration_ms}


def _resolve_path(*candidates):
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def _design_coverage(run_dir, root, design_source, artifact):
    """Run the bundled design-source coverage check for a P2 exit.

    Returns the result dict, or None when it cannot run (script missing, or
    either input unresolvable). None means "unchecked", never "passed" — the
    caller warns rather than silently approving, the same stance the rest of the
    pipeline takes on an unavailable helper.
    """
    design = _resolve_path(design_source,
                           os.path.join(root, design_source),
                           os.path.join(run_dir, design_source))
    art = _resolve_path(artifact,
                        os.path.join(run_dir, artifact) if artifact else None,
                        os.path.join(root, artifact) if artifact else None)
    if not design or not art:
        return None
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import design_coverage
        return design_coverage.check([design], art)
    except (ImportError, SystemExit, OSError, ValueError):
        return None


def _plan_graph(run_dir, root, artifact, plan_artifact=None):
    """Run the bundled plan-graph check for a P2 exit.

    Catches the Dependency Rule violated one level up: a presentation task that
    blocks on a server implementation when the plan already carries the boundary
    contract. Run 3's `T8 frontend` depended on `T6 routes`, which put the
    frontend at depth 5 behind the whole backend chain even though it only needed
    the response contract P2 had already defined.

    `artifact` is the P2 exit artifact (where dag_tasks belongs by contract);
    `plan_artifact` is the explicit pointer for runs that keep the plan in a
    separate file. Run 4 invented `p4-components.json` and left `dag_tasks` empty
    in p2-design.json, so the checker read a planless artifact and reported
    NO_TASKS — the gate looked satisfied while holding nothing. Where the plan
    lives must be a declared pointer, not a per-run discovery.

    Returns the result dict, or None when it cannot run — None means "unchecked",
    never "passed".
    """
    # The explicit pointer wins: it is the recorded statement of "the plan is
    # here", checked against all the usual locations.
    art = None
    for cand in (plan_artifact, artifact):
        if not cand:
            continue
        art = _resolve_path(cand,
                            os.path.join(run_dir, cand),
                            os.path.join(root, cand))
        if art:
            break
    if not art:
        return None
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import plan_graph
        pres = [d.strip().lower().rstrip("/")
                for d in plan_graph.DEFAULT_PRESENTATION_DIRS.split(",")
                if d.strip()]
        return plan_graph.check(art, pres)
    except (ImportError, SystemExit, OSError, ValueError):
        return None


_PHASE_ORDER = {"P0": "P1", "P1": "P2", "P2": "G3", "G3": "P4",
                "P4": "G5", "G5": "P6", "P6": None}


def _questions_of(detail):
    """Normalise a user_loop batch into a list of question strings.

    Three shapes appear in real logs: a single `question` string (run 1), a
    `questions` list (the documented batch), and run 2's `questions: 4` integer
    count beside a separate `decisions` map. The integer is tolerated for
    backward compatibility but carries no text, so it cannot satisfy the
    "name the questions" check — it is reported as an unnamed batch instead.
    """
    qs = detail.get("questions")
    if isinstance(qs, list):
        return [str(q) for q in qs if q]
    single = detail.get("question") or detail.get("reason")
    if single:
        return [str(single)]
    if isinstance(qs, int) and qs > 0:
        # count without content: legal shape, zero traceability
        return []
    return []


def _derive_next_action(phase, event, verdict, st):
    """Recompute the resume hint. Run 3 ended with next_action still reading
    "run P0/P1" from init, which is exactly the string a post-compression resume
    would have acted on after 11 components were already done."""
    if event == "phase_exit":
        nxt = _PHASE_ORDER.get(phase)
        return f"run {nxt}" if nxt else "run complete — write summary.md"
    if event == "gate_verdict":
        if verdict == "APPROVED":
            return "run P4"
        if verdict == "REVISE_REQUIRED":
            return "route findings back to P2, then re-run G3"
        if verdict == "FAIL":
            return "route BLOCKERs back to P4 (code) or P2 (structure) with scope"
        if verdict in GATE_PASSING["g5"]:
            debts = st.get("debts") or []
            return ("get user sign-off on the logged debts, then run P6"
                    if debts else "run P6")
    if event == "component_done":
        return f"continue {phase} with the next component"
    if event == "user_loop":
        return "awaiting the user's answer"
    if event == "phase_enter":
        return f"finish {phase}"
    return st.get("next_action")


def _apply_state_effects(st, phase, event, verdict, detail, seq,
                         duration_ms=None, dispatched_at=None):
    """Fold an event into the state fields that used to be written once at init
    and then frozen. Run 3 shipped with p4_components=[] after 11 component_done
    events, artifact_pointers={} after 5 artifact_written events, and debts=[]
    while two debts awaited sign-off."""

    def _upsert_component(name, **fields):
        comps = st.setdefault("p4_components", [])
        for c in comps:
            if c.get("name") == name:
                c.update(fields)
                return c
        entry = {"name": name}
        entry.update(fields)
        comps.append(entry)
        return entry

    if event == "agent_dispatch":
        name = detail.get("component") or detail.get("task")
        if name:
            # Opening the entry here means a resume that lands mid-P4 can see what
            # was in flight, not just what finished.
            fields = {"status": "in_progress", "dispatched_at": dispatched_at}
            if detail.get("worktree"):
                fields["worktree"] = detail["worktree"]
            _upsert_component(name, **fields)

    elif event == "component_done":
        name = detail.get("component") or detail.get("task")
        if name:
            fields = {"status": detail.get("status", "DONE")}
            for k in ("worktree", "files", "evidence"):
                if detail.get(k):
                    fields[k] = detail[k]
            if duration_ms is not None:
                fields["duration_ms"] = duration_ms
            _upsert_component(name, **fields)

    elif event == "artifact_written":
        path = detail.get("path")
        if path:
            # lowercase key to match the documented schema ("p1", "p2", "g3")
            st.setdefault("artifact_pointers", {})[phase.lower()] = path

    elif event == "gate_verdict" and phase == "G5" and verdict == "PASS_WITH_CONCERNS":
        # The contract is "PASS_WITH_CONCERNS → P6 with logged debts (needs user
        # sign-off)". Give those debts a machine-readable home so the P6 exit
        # latch can see them.
        new = detail.get("debts_awaiting_signoff") or detail.get("debts") or []
        if isinstance(new, str):
            new = [new]
        debts = st.setdefault("debts", [])
        for d in new:
            if d not in debts:
                debts.append(d)

    elif event == "debt_signoff":
        signed = st.get("debts") or []
        if signed:
            # Carry the user's words with the debt. Moving the strings alone
            # recorded that a sign-off happened but not that anyone agreed.
            answer = detail.get("answer") or detail.get("user_response")
            pending = st.get("pending_user_question")
            if pending:
                st["open_questions"] = [
                    q for q in (st.get("open_questions") or []) if q != pending]
            st["pending_user_question"] = None
            st["phase_status"] = "in_progress"
            st.setdefault("debts_signed_off", []).extend(
                [f"{d} — signed off: {answer}" if answer else d for d in signed])
            st["debts"] = []

    elif event == "user_loop":
        qs = _questions_of(detail)
        if qs:
            st["pending_user_question"] = qs[0] if len(qs) == 1 else "; ".join(qs)
            oq = st.setdefault("open_questions", [])
            for q in qs:
                if q not in oq:
                    oq.append(q)

    # --- question ledger (every event, not just user_loop) -------------------
    # The ratio of "asked the user" to "resolved off disk" is what tells tuning
    # whether the self-resolution discipline is holding. It cannot live only on
    # user_loop: the best possible run resolves every open question from the P0
    # notes and the design artifact and therefore emits no user_loop at all, so
    # binding the counter to the pause event would score a perfect run as 0/0.
    # Any event may carry adopted_without_asking (P1/P2 phase_exit is the natural
    # place when nothing needed asking).
    adopted = detail.get("adopted_without_asking")
    asked = len(_questions_of(detail)) if event == "user_loop" else 0
    if adopted or asked:
        ledger = st.setdefault("question_ledger",
                               {"asked": 0, "self_resolved": 0})
        ledger["asked"] = ledger.get("asked", 0) + asked
        if isinstance(adopted, dict):
            n = len(adopted)
        elif isinstance(adopted, list):
            n = len(adopted)
        elif isinstance(adopted, int):
            n = adopted
        elif adopted:
            n = 1
        else:
            n = 0
        ledger["self_resolved"] = ledger.get("self_resolved", 0) + n


def cmd_event(a):
    run_dir = _find_run_dir(a.root, a.slug)
    try:
        os.makedirs(run_dir, exist_ok=True)
    except OSError as e:
        raise CCLogError(f"cannot create run dir {run_dir}: {e}",
                         hint=FALLBACK_HINT)
    detail = parse_json_arg(a.detail, "--detail") if a.detail else {}
    st = load_state(run_dir)
    facts = _scan_log(run_dir)

    # --- gate latch -------------------------------------------------------
    # Refuse to record entry into a gated phase before its gate has spoken
    # with a PASSING verdict (a recorded REVISE/FAIL does not open the gate).
    # A deliberate bypass needs --force and leaves a permanent process_violation.
    gate = PHASE_GATE_PREREQ.get(a.phase) if a.event == "phase_enter" else None
    # Each latch that --force can override appends its own rule here. This used to
    # be a single boolean plus the gate name, which only worked while the gate
    # latch was the sole bypass source — the phase_exit latches below have no gate.
    violations = []
    if gate:
        current = (st.get("gate_verdicts") or {}).get(gate)
        if current not in GATE_PASSING[gate]:
            if not a.force:
                raise CCLogError(
                    f"refusing to log phase_enter {a.phase}: gate_verdicts.{gate} "
                    f"is {current!r}, but {a.phase} requires one of "
                    f"{sorted(GATE_PASSING[gate])}. Run the {gate.upper()} gate "
                    f"and log its gate_verdict first. If the bypass is "
                    f"intentional, re-run with --force — that records a MAJOR "
                    f"process_violation.")
            violations.append({
                "rule": f"{a.phase} may only be entered after {gate.upper()} "
                        f"records a passing gate_verdict",
                "actual": f"gate_verdicts.{gate} was {current!r} at phase_enter"})
        # P6-specific: even if g5 verdict passes, block if g5_delta_scopes is
        # non-empty — those scopes have not yet been delta-reviewed. Without this,
        # a scoped P4 re-entry preserves the old verdict and P6 sails through on
        # stale certification (the exact "run 2" failure this mechanism prevents).
        elif gate == "g5" and a.phase == "P6":
            pending_scopes = st.get("g5_delta_scopes") or []
            if pending_scopes:
                if not a.force:
                    raise CCLogError(
                        f"refusing to log phase_enter P6: gate_verdicts.g5 is "
                        f"{current!r} (passing) but g5_delta_scopes is non-empty "
                        f"{pending_scopes} — these components have been modified "
                        f"since the last G5 verdict and require a delta review "
                        f"before P6 can proceed. Run G5 (full or delta) to clear "
                        f"the pending scopes first. If the bypass is intentional, "
                        f"re-run with --force.")
                violations.append({
                    "rule": "P6 requires g5_delta_scopes to be empty",
                    "actual": f"pending delta scopes {pending_scopes}"})

            # PASS_WITH_CONCERNS is a passing G5 verdict only conditionally: the
            # user must accept the named debts before finish work begins. The
            # v1.5.0 latch originally checked only phase_exit P6, which let the
            # pipeline enter P6, perform integration/cleanup work, and discover at
            # the last line that it never had authority to ship the debt. Keep the
            # exit check as defense in depth, but put the real stop at the entrance.
            unsigned = st.get("debts") or []
            if unsigned:
                if not a.force:
                    raise CCLogError(
                        f"refusing to log phase_enter P6: {len(unsigned)} debt(s) "
                        f"still await user sign-off {unsigned}. G5 "
                        f"PASS_WITH_CONCERNS opens P6 only after a real "
                        f"debt_signoff (preceded by a user_loop and carrying the "
                        f"user's answer). Green CI or a deadline cannot grant that "
                        f"authority. Ask and record the sign-off first. If the "
                        f"bypass is intentional, re-run with --force.")
                violations.append({
                    "rule": "P6 may only be entered after all G5 debts are "
                            "signed off by the user",
                    "actual": f"{len(unsigned)} unsigned debt(s): {unsigned}"})

    # --- unmatched phase_exit --------------------------------------------
    # Run 3 appended a second `P6 phase_exit` (seq 46) against a single
    # `phase_enter` (seq 36): the run closed, corrective work reopened it, and the
    # second closure was recorded without ever re-entering. Durations and the
    # phase ledger both go wrong when exits outnumber entries.
    if a.event == "phase_exit":
        opened = facts["enters"].get(a.phase, 0) - facts["exits"].get(a.phase, 0)
        if opened <= 0:
            if not a.force:
                raise CCLogError(
                    f"refusing to log phase_exit {a.phase}: no open phase_enter "
                    f"for it (enters={facts['enters'].get(a.phase, 0)}, "
                    f"exits={facts['exits'].get(a.phase, 0)}). Log a "
                    f"phase_enter {a.phase} first — if this is a second closure "
                    f"after corrective work, the re-entry is what was missing. "
                    f"If the bypass is intentional, re-run with --force.")
            violations.append({
                "rule": f"phase_exit {a.phase} requires an open phase_enter",
                "actual": f"enters={facts['enters'].get(a.phase, 0)}, "
                          f"exits={facts['exits'].get(a.phase, 0)}"})

    # --- P6 may not close over unsigned debts ----------------------------
    # "PASS_WITH_CONCERNS → P6 with logged debts (needs user sign-off)" had no
    # enforcement: run 3 closed P6 twice with 2 debts awaiting sign-off and
    # state.debts empty, so nothing could tell the sign-off never happened.
    if a.event == "phase_exit" and a.phase == "P6":
        unsigned = st.get("debts") or []
        if unsigned:
            if not a.force:
                raise CCLogError(
                    f"refusing to log phase_exit P6: {len(unsigned)} debt(s) still "
                    f"await user sign-off {unsigned} — PASS_WITH_CONCERNS accepts "
                    f"them only with the user's agreement. Record the sign-off "
                    f"(--event debt_signoff) or clear the debts first. If the "
                    f"bypass is intentional, re-run with --force.")
            violations.append({
                "rule": "P6 may not close while debts await user sign-off",
                "actual": f"{len(unsigned)} unsigned debt(s): {unsigned}"})

    # --- PASS_WITH_CONCERNS must name its concerns ------------------------
    # The verdict means "passing, but with debts the user must accept". A PWC that
    # names nothing is indistinguishable from PASS and silently skips the sign-off.
    if (a.event == "gate_verdict" and a.phase == "G5"
            and a.verdict == "PASS_WITH_CONCERNS"):
        named = detail.get("debts_awaiting_signoff") or detail.get("debts")
        if not named:
            if not a.force:
                raise CCLogError(
                    "refusing to log gate_verdict PASS_WITH_CONCERNS: no debts "
                    "named. Pass them via --detail "
                    "'{\"debts_awaiting_signoff\":[\"...\"]}' so the P6 latch can "
                    "require sign-off, or record PASS if there is nothing to "
                    "accept. If the bypass is intentional, re-run with --force.")
            violations.append({
                "rule": "PASS_WITH_CONCERNS must name the debts it accepts",
                "actual": "no debts_awaiting_signoff in --detail"})

    # --- P2 may not exit with constraints the artifact never carried -------
    # Run 3's design source had "## 8. 前端约束" requiring `StockSearchWidget`;
    # p2-design.json mentioned search/widget zero times. G3 approved the lossy copy
    # because a gate cannot audit what it cannot see, and G5 caught it only against
    # the full source — a MAJOR finding and a 29-minute corrective P4 round for a
    # gap a set difference finds instantly.
    if a.event == "phase_exit" and a.phase == "P2":
        # Resolve the P2 artifact robustly. Run 5 slipped through with a
        # self-reported "PASS 10/10" that the latch never produced: P2 never
        # logged artifact_written, the exit detail used the plan_artifact key
        # rather than artifact, and the conventional path was never tried.
        art = (detail.get("artifact")
               or (st.get("artifact_pointers") or {}).get("p2")
               or detail.get("plan_artifact")
               or "artifacts/p2-design.json")
        src = detail.get("design_source")
        if not src:
            if not a.force:
                raise CCLogError(
                    "refusing to log phase_exit P2: --detail must name the "
                    "authoritative design source, e.g. "
                    "'{\"design_source\":\"docs/specs/design.md\"}'. If "
                    "p2-design.json IS the whole design, say so explicitly with "
                    "'{\"design_source\":\"none\",\"reason\":\"...\"}'. Without "
                    "this, nothing can tell whether the artifact dropped a "
                    "constraint. If the bypass is intentional, re-run with --force.")
            violations.append({
                "rule": "P2 exit must name its authoritative design source",
                "actual": "no design_source in --detail"})
        elif src != "none":
            cov = _design_coverage(run_dir, a.root, src, art)
            if cov is None:
                print("cc_log: WARNING design coverage not checked "
                      "(design_coverage.py unavailable or artifact not found); "
                      "verify by hand that every design section reached "
                      "p2-design.json", file=sys.stderr)
            elif cov["verdict"] == "FAIL":
                summary = (f"{len(cov['unaccounted_sections'])} unaccounted "
                           f"section(s), "
                           f"{len(cov['unreferenced_identifiers'])} covered "
                           f"section(s) with identifiers missing from the artifact")
                if not a.force:
                    raise CCLogError(
                        f"refusing to log phase_exit P2: design coverage FAILED — "
                        f"{summary}. Run `design_coverage.py --design {src} "
                        f"--artifact {art}` for the itemised list. Fold the missing "
                        f"constraints in, or record a deliberate omission in the "
                        f"artifact's sections_out_of_scope[] / "
                        f"identifiers_waived[]. If the bypass is intentional, "
                        f"re-run with --force.")
                violations.append({
                    "rule": "P2 artifact must cover every design source section",
                    "actual": summary})
            # Overwrite, never setdefault: run 5's orchestrator self-reported a
            # PASS here that the latch never produced, and setdefault preserved
            # it. The value in the event must be the latch's own verdict; a
            # caller wanting to pre-state one has --detail to lose.
            detail["design_coverage"] = (
                cov["verdict"] if cov else "unchecked")
            if cov is None:
                detail["design_coverage_source"] = "self_report_overridden"

        # The plan graph is checked whether or not an external design source
        # exists — the DAG is in the artifact either way. Run 3's frontend sat at
        # depth 5 behind the backend chain for a contract P2 had already defined.
        plan_ptr = detail.get("plan_artifact")
        if not plan_ptr and art:
            # The artifact may itself declare where the plan lives (a
            # recorded pointer, not a per-run discovery).
            try:
                art_path = _resolve_path(art, os.path.join(run_dir, art),
                                         os.path.join(a.root, art))
                with open(art_path, encoding="utf-8") as f:
                    plan_ptr = (json.load(f) or {}).get("plan_artifact")
            except (OSError, json.JSONDecodeError):
                plan_ptr = None

        plan = _plan_graph(run_dir, a.root, art, plan_ptr)

        # _plan_graph returns None for two different reasons; they need opposite
        # handling. Distinguish them by whether a plan file was even found.
        probe = plan_ptr or art or ""
        plan_file_found = _resolve_path(
            probe, os.path.join(run_dir, probe) if probe else "",
            os.path.join(a.root, probe) if probe else "")
        if plan is None and plan_file_found:
            # The plan file exists but the checker could not run on it — the
            # degraded mode every bundled helper uses: warn, never silently pass.
            print("cc_log: WARNING plan graph not checked (plan_graph.py "
                  "unavailable or the plan file could not be parsed); verify by "
                  "hand that no presentation task blocks on a server "
                  "implementation", file=sys.stderr)
            detail.setdefault("plan_graph", "unchecked")
        elif plan is None:
            # No plan file at all. Run 4 moved the whole plan into a fresh
            # `p4-components.json` and left dag_tasks empty: NO_TASKS then read
            # as "nothing to check" while the gate held nothing. A missing plan
            # is only legitimate for work with no components at all, which must
            # say so explicitly.
            declared_planless = detail.get("planless") or (plan_ptr == "none")
            if declared_planless:
                detail.setdefault("plan_graph", "NO_TASKS_DECLARED")
            elif not a.force:
                raise CCLogError(
                    f"refusing to log phase_exit P2: no plan found to check — "
                    f"no artifact carries dag_tasks and no plan_artifact pointer "
                    f"was given. Either keep dag_tasks in the P2 exit artifact, "
                    f"or pass --detail "
                    f"'{{\"plan_artifact\":\"artifacts/p4-components.json\"}}' "
                    f"naming where the plan lives. Run 4 invented a separate "
                    f"plan file silently and the checker approved a planless "
                    f"artifact. If this task genuinely has no components, say so "
                    f"with '\"planless\":true'. If the bypass is intentional, "
                    f"re-run with --force.")
            else:
                violations.append({
                    "rule": "P2 exit must either carry dag_tasks or declare "
                            "where the plan lives",
                    "actual": "no dag_tasks, no plan_artifact pointer"})
        elif plan["verdict"] == "NO_TASKS":
            # The plan file exists but declares zero tasks. Same failure shape as
            # run 4: p2-design.json carried dag_tasks:0 while the real plan lived
            # in an undeclared p4-components.json — NO_TASKS read as "satisfied".
            declared_planless = detail.get("planless") or (plan_ptr == "none")
            if declared_planless:
                detail.setdefault("plan_graph", "NO_TASKS_DECLARED")
            elif not a.force:
                raise CCLogError(
                    f"refusing to log phase_exit P2: the plan file "
                    f"({plan_ptr or art}) declares no dag_tasks. Run 4 shipped "
                    f"exactly this shape — the real plan lived in an undeclared "
                    f"p4-components.json and every plan check silently passed on "
                    f"an empty artifact. Put the tasks in the plan file, point "
                    f"plan_artifact at the file that actually holds them, or "
                    f"declare '\"planless\":true' if this task has no components. "
                    f"If the bypass is intentional, re-run with --force.")
            else:
                violations.append({
                    "rule": "the plan file must carry dag_tasks or the run must "
                            "declare planlessness",
                    "actual": f"zero dag_tasks in {plan_ptr or art}"})
        elif plan["verdict"] == "FAIL":
            try:
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                import plan_graph as _pg
                summary = _pg.summarize(plan)
            except ImportError:
                summary = f"{len(plan['avoidable_serialization'])} avoidable edge(s)"
            if not a.force:
                raise CCLogError(
                    f"refusing to log phase_exit P2: plan graph FAILED — "
                    f"{summary}. A presentation task blocking on a server "
                    f"implementation is the Dependency Rule violated in the plan: "
                    f"point it at the boundary contract instead (clear depends_on, "
                    f"declare consumes_contract[]). Run `plan_graph.py --artifact "
                    f"{art}` for detail. If the task genuinely needs the "
                    f"implementation, record it in plan_serialization_waived[] "
                    f"with a reason. If the bypass is intentional, re-run "
                    f"with --force.")
            violations.append({
                "rule": "P2 plan graph must not serialize presentation work "
                        "behind a server implementation",
                "actual": summary})
        if plan is not None:
            detail.setdefault("plan_graph", plan["verdict"])
            detail.setdefault("plan_critical_depth", plan["critical_depth"])

    # --- gate loop cap ----------------------------------------------------
    # SKILL.md caps each gate at 2 iterations and then escalates to the user.
    # Nothing read the counter back, so run 3 used both G5 rounds with
    # state.loops.gate5_iterations stuck at 0.
    if a.event == "loop_increment":
        key = {"G3": "gate3_iterations", "G5": "gate5_iterations"}.get(a.phase)
        if key:
            nxt = (st.get("loops") or {}).get(key, 0) + 1
            if nxt > MAX_GATE_ITERATIONS and not a.force:
                raise CCLogError(
                    f"refusing to log loop_increment: {key} would reach {nxt}, "
                    f"over the cap of {MAX_GATE_ITERATIONS}. A gate that keeps "
                    f"looping signals an upstream ambiguity, not a gate problem — "
                    f"escalate an open_question to the user (--event user_loop) "
                    f"instead of iterating again. If the bypass is intentional, "
                    f"re-run with --force.")

    # --- component_done needs its agent_dispatch ---------------------------
    # Pairing the two events buys two things that were both missing from all three
    # production runs, which emitted zero agent_dispatch events. First, cost: P4 was
    # 79% of run 3's effective work and could only be attributed to batch windows,
    # never to a single component. Second, ROI: `agent`, `skills` and `superpowers`
    # ride on the dispatch event, so without it ca-process-tuning cannot tell
    # "fired and changed nothing" from "was never installed".
    if a.event == "component_done":
        comp = detail.get("component") or detail.get("task")
        if not comp:
            if not a.force:
                raise CCLogError(
                    "refusing to log component_done: --detail must name the "
                    "component, e.g. '{\"component\":\"ordering\",\"status\":"
                    "\"DONE\"}'. Without it the event cannot be paired with its "
                    "dispatch or tracked in p4_components. If the bypass is "
                    "intentional, re-run with --force.")
            violations.append({
                "rule": "component_done must name its component",
                "actual": "no component in --detail"})
        elif comp not in facts["dispatch_ts"]:
            if not a.force:
                raise CCLogError(
                    f"refusing to log component_done for {comp!r}: no "
                    f"agent_dispatch was recorded for it, so the component has no "
                    f"start time and its cost cannot be measured. Log the dispatch "
                    f"when the work begins:\n"
                    f"  cc_log.py event --root <dir> --slug <slug> --phase "
                    f"{a.phase} --event agent_dispatch \\\n"
                    f"    --agent ca-clean-implementer --skills ca-dependency-rule,"
                    f"ca-layer-boundaries \\\n"
                    f"    --superpowers test-driven-development \\\n"
                    f"    --detail '{{\"component\":\"{comp}\"}}'\n"
                    f"That one event also carries the agent/skills/superpowers "
                    f"that augmentation ROI is scored from. If the bypass is "
                    f"intentional, re-run with --force.")
            violations.append({
                "rule": "component_done requires a matching agent_dispatch",
                "actual": f"no agent_dispatch recorded for {comp!r}"})

    # --- user_loop must be a real question, batched, and looked up first ----
    # This was the only significant event in the system with no required detail
    # fields, while every neighbour got a latch (P2 exit names its design_source,
    # component_done pairs with agent_dispatch, PASS_WITH_CONCERNS names its
    # debts, P6 needs empty delta scopes). The gap shows in the runs: run 1 asked
    # a question the user cancelled with "继续" — the agent then decided from its
    # own pre-marked recommendations, so the pause bought no information and cost
    # a 15-minute gap. Run 2 batched 4 questions and self-resolved 3 more from the
    # artifacts. Run 3 asked nothing at all across 48 events.
    if a.event == "user_loop":
        trigger = detail.get("trigger")
        if trigger not in USER_LOOP_TRIGGERS:
            if not a.force:
                raise CCLogError(
                    f"refusing to log user_loop: --detail must name a trigger "
                    f"from {sorted(USER_LOOP_TRIGGERS)}, got {trigger!r}. The "
                    f"trigger decides which discipline applies — "
                    f"{sorted(INFO_GAP_TRIGGERS)} are information gaps and must "
                    f"show the lookup you tried first, while gate_overflow and "
                    f"debt_signoff are authority decisions that only the user can "
                    f"make. If the bypass is intentional, re-run with --force.")
            violations.append({
                "rule": f"user_loop must name a trigger from "
                        f"{sorted(USER_LOOP_TRIGGERS)}",
                "actual": f"trigger was {trigger!r}"})
        questions = _questions_of(detail)
        if not questions:
            if not a.force:
                raise CCLogError(
                    "refusing to log user_loop: --detail must name the questions "
                    "in text, e.g. '{\"questions\":[\"which DB backs the "
                    "repository port?\",\"is a partial refund allowed?\"]}'. A "
                    "bare count (run 2's 'questions': 4) records that a pause "
                    "happened but not what was asked, so a resume cannot "
                    "re-surface it and tuning cannot judge whether it was "
                    "necessary. If the bypass is intentional, re-run with --force.")
            violations.append({
                "rule": "user_loop must name its questions in text",
                "actual": f"no question text in --detail (got keys "
                          f"{sorted(detail)})"})
        elif len(questions) > MAX_BATCH_QUESTIONS:
            if not a.force:
                raise CCLogError(
                    f"refusing to log user_loop: {len(questions)} questions in one "
                    f"pause, over the batch cap of {MAX_BATCH_QUESTIONS} "
                    f"(AskUserQuestion accepts at most that many per call), so "
                    f"this could not have been one interaction. Split it into "
                    f"rounds — and if the extra questions are only dependent "
                    f"follow-ups (B's options depend on A's answer), that second "
                    f"round belongs in its own user_loop. If the bypass is "
                    f"intentional, re-run with --force.")
            violations.append({
                "rule": f"a user_loop may carry at most {MAX_BATCH_QUESTIONS} "
                        f"questions (one AskUserQuestion call)",
                "actual": f"{len(questions)} questions in one pause"})
        if trigger in INFO_GAP_TRIGGERS:
            tried = detail.get("resolution_attempted")
            if isinstance(tried, str):
                tried = [tried] if tried.strip() else []
            if not tried:
                if not a.force:
                    raise CCLogError(
                        f"refusing to log user_loop (trigger={trigger!r}): "
                        f"--detail must carry resolution_attempted[] naming what "
                        f"you checked before asking, e.g. "
                        f"'{{\"resolution_attempted\":[\"P0 codebase_notes\","
                        f"\"docs/specs/design.md §5.6\",\"p2-design.json "
                        f"boundary_dtos\"]}}'. This trigger is an information gap: "
                        f"the answer may already be on disk, and asking is a "
                        f"substitute for looking. Questions you resolved that way "
                        f"go in adopted_without_asking{{}} instead of being asked. "
                        f"If the bypass is intentional, re-run with --force.")
                violations.append({
                    "rule": f"user_loop with trigger {trigger!r} must record "
                            f"resolution_attempted[] before asking the user",
                    "actual": "no resolution_attempted in --detail"})

        if trigger == "debt_signoff":
            current_debts = st.get("debts") or []
            asked_debts = detail.get("debts")
            if isinstance(asked_debts, str):
                asked_debts = [asked_debts]
            if not current_debts or asked_debts != current_debts:
                if not a.force:
                    raise CCLogError(
                        "refusing to log debt-signoff user_loop: --detail.debts "
                        f"must exactly match the debts awaiting sign-off. Current "
                        f"state.debts={current_debts!r}, asked debts="
                        f"{asked_debts!r}. Pass "
                        f"'{{\"trigger\":\"debt_signoff\",\"debts\":"
                        f"{json.dumps(current_debts, ensure_ascii=False)},"
                        f"\"questions\":[\"accept these named debts?\"]}}'. "
                        f"This binds the question to the current G5 verdict instead "
                        f"of letting an old or generic ask authorize a new debt. If "
                        f"the bypass is intentional, re-run with --force.")
                violations.append({
                    "rule": "a debt-signoff user_loop must name exactly the "
                            "current state.debts",
                    "actual": f"state.debts={current_debts!r}, "
                              f"detail.debts={asked_debts!r}"})

    # --- debt_signoff must bind to this verdict's exact question + debts -----
    # The v1.5.0 P6 latch refused to close over unsigned debts, but debt_signoff
    # cleared state.debts unconditionally. Requiring only "some debt question once"
    # was still too weak: a stale ask could authorize a later verdict, or the answer
    # could refer to different debts. The chain below is exact and auditable:
    # current G5 PWC seq < referenced debt user_loop seq < this signoff, with the
    # same debt list at both events. This is structural provenance, not cryptographic
    # authentication of the human — the logger cannot know who typed a JSON string.
    if a.event == "debt_signoff":
        answer = detail.get("answer") or detail.get("user_response")
        if not answer:
            if not a.force:
                raise CCLogError(
                    "refusing to log debt_signoff: --detail must carry the user's "
                    "actual response, e.g. '{\"answer\":\"SIGNED OFF as debt\","
                    "\"user_loop_seq\":12,\"debts\":[\"F-01\"],"
                    "\"required_followup\":\"...\"}'. Without it this event "
                    "clears every debt in state.debts on the agent's own word. If "
                    "the bypass is intentional, re-run with --force.")
            violations.append({
                "rule": "debt_signoff must record the user's actual response",
                "actual": f"no answer in --detail (got keys {sorted(detail)})"})

        current_debts = st.get("debts") or []
        signed_debts = detail.get("debts")
        if isinstance(signed_debts, str):
            signed_debts = [signed_debts]
        if not current_debts or signed_debts != current_debts:
            if not a.force:
                raise CCLogError(
                    "refusing to log debt_signoff: --detail.debts must exactly "
                    f"match current state.debts. Current={current_debts!r}, "
                    f"signed={signed_debts!r}. This prevents an answer to one "
                    f"finding from clearing a different set. If the bypass is "
                    f"intentional, re-run with --force.")
            violations.append({
                "rule": "debt_signoff must name exactly the current state.debts",
                "actual": f"state.debts={current_debts!r}, "
                          f"detail.debts={signed_debts!r}"})

        ref = detail.get("user_loop_seq")
        latest_ask = facts["latest_debt_user_loop_seq"]
        pwc_seq = facts["latest_g5_pwc_seq"]
        chain_ok = (isinstance(ref, int) and ref == latest_ask
                    and isinstance(pwc_seq, int) and isinstance(latest_ask, int)
                    and latest_ask > pwc_seq)
        if not chain_ok:
            if not a.force:
                raise CCLogError(
                    "refusing to log debt_signoff: --detail.user_loop_seq must "
                    "reference the latest debt-signoff user_loop, and that ask "
                    "must come after the current G5 PASS_WITH_CONCERNS verdict. "
                    f"Got ref={ref!r}, latest debt ask seq={latest_ask!r}, "
                    f"latest G5 PWC seq={pwc_seq!r}. Log the current debts in a "
                    "new user_loop, wait for the user's response, then quote that "
                    "event's seq here. If the bypass is intentional, re-run with "
                    "--force.")
            violations.append({
                "rule": "debt_signoff must reference the latest debt user_loop "
                        "after the current G5 PASS_WITH_CONCERNS verdict",
                "actual": f"ref={ref!r}, latest_ask={latest_ask!r}, "
                          f"latest_pwc={pwc_seq!r}"})

    seq = next_seq(run_dir)

    # --- unlogged gap annotation -----------------------------------------
    # Run 3 has 6h18m of silence between seq 37 and seq 38 with no marker, which
    # left P6's duration reading 398 minutes of "work". Annotate the gap rather
    # than refusing it — long pauses are legitimate, unmeasurable ones are not.
    gap_from = _parse_ts(facts["last_ts"])
    if gap_from is not None:
        gap_ms = int((datetime.datetime.now().astimezone()
                      - gap_from).total_seconds() * 1000)
        if gap_ms >= PAUSE_GAP_MS:
            # A silent gap only means "nobody logged", not "nobody worked": run
            # 5's T4 executed silently for 37.8 minutes, the heuristic charged
            # the whole gap to idle, and its recorded duration came back 0 ms.
            # With components in flight the pause is recorded for the audit
            # trail but excluded from every subtraction ledger — that time is
            # work, not waiting.
            in_flight = sorted(facts["open_components"])
            credited = not in_flight
            _append_jsonl(run_dir, _event_rec(
                st.get("run_id"), seq, a.phase, "pause",
                {"gap_ms": gap_ms, "since": facts["last_ts"],
                 "credited_to_idle": credited,
                 "components_in_flight": in_flight,
                 "reason": (f"no events for {gap_ms // 60000} min; auto-"
                            f"annotated so phase durations exclude it"
                            if credited else
                            f"no events for {gap_ms // 60000} min while "
                            f"{in_flight} were executing — recorded for audit "
                            f"but NOT counted as idle, so component and phase "
                            f"durations keep this time")}))
            if credited:
                for k in facts["pause_ms_since_enter"]:
                    facts["pause_ms_since_enter"][k] += gap_ms
                for k in facts["pause_ms_since_dispatch"]:
                    facts["pause_ms_since_dispatch"][k] += gap_ms
            seq += 1

    # --- gate staleness ---------------------------------------------------
    # Re-entering P2/P4 voids the downstream verdict so the next latch check
    # demands a fresh one — UNLESS --scope is provided for P4, which narrows
    # the verdict to a delta review instead of full invalidation.
    stale = PHASE_INVALIDATES.get(a.phase) if a.event == "phase_enter" else None
    if stale and (st.get("gate_verdicts") or {}).get(stale):
        was = st["gate_verdicts"][stale]
        scope_list = [s.strip() for s in a.scope.split(",") if s.strip()] if getattr(a, "scope", None) else []

        if scope_list and a.phase == "P4":
            # Scoped re-entry: don't void the full verdict; instead track which
            # components need delta review. The gate remains "conditionally open"
            # — P6 requires either a fresh full verdict OR a delta review covering
            # all narrowed scopes.
            existing_scopes = st.get("g5_delta_scopes", [])
            merged_scopes = sorted(set(existing_scopes + scope_list))
            st["g5_delta_scopes"] = merged_scopes
            _append_jsonl(run_dir, _event_rec(
                st.get("run_id"), seq, a.phase, "gate_scope_narrowed",
                {"gate": stale, "verdict_preserved": was,
                 "affected_scopes": scope_list,
                 "cumulative_delta_scopes": merged_scopes,
                 "reason": f"scoped re-entry into {a.phase} for {scope_list}; "
                           f"{stale.upper()} verdict preserved for unaffected "
                           f"components, delta review required for listed scopes"}))
            seq += 1
        else:
            # Unscoped re-entry: full invalidation (original behavior)
            _append_jsonl(run_dir, _event_rec(
                st.get("run_id"), seq, a.phase, "gate_invalidated",
                {"gate": stale, "was": was,
                 "reason": f"re-entering {a.phase} changes what {stale.upper()} "
                           f"certified; a fresh verdict is required before the "
                           f"next gated phase"}))
            st["gate_verdicts"][stale] = None
            # Clear delta scopes only when the voided gate is g5 (P4 full re-run).
            # P2 re-entry voids g3 but must NOT touch g5_delta_scopes — those
            # track pending delta work for g5, which P2 doesn't affect.
            if stale == "g5":
                st.pop("g5_delta_scopes", None)
            seq += 1

    # --- downstream phases retract ----------------------------------------
    # Voiding the gate verdict was not enough: run 3 re-entered P4 twice after P6
    # had already closed, and `completed_phases` still listed P6 from the first
    # closure. A resume reading that ledger would conclude the run was done.
    if a.event == "phase_enter":
        reopen = [p for p in PHASE_REOPENS.get(a.phase, [])
                  if p in (st.get("completed_phases") or [])]
        if reopen:
            st["completed_phases"] = [p for p in st["completed_phases"]
                                      if p not in reopen]
            _append_jsonl(run_dir, _event_rec(
                st.get("run_id"), seq, a.phase, "phase_reopened",
                {"retracted": reopen,
                 "reason": f"re-entering {a.phase} un-finishes the phases "
                           f"downstream of it; they must run again"}))
            seq += 1

    for v in violations:
        _append_jsonl(run_dir, _event_rec(
            st.get("run_id"), seq, a.phase, "process_violation",
            {"severity": "MAJOR", "rule": v["rule"], "actual": v["actual"],
             "bypass": "--force"}))
        # A forced bypass becomes a debt, so it cannot be closed out silently:
        # the P6 exit latch will require sign-off for it like any other concern.
        st.setdefault("debts", []).append(
            f"[process] {v['rule']} — bypassed with --force at seq {seq} "
            f"({v['actual']})")
        seq += 1

    if a.duration_ms is not None:
        auto_duration = a.duration_ms
    elif a.event == "phase_exit":
        auto_duration = _elapsed_ms_since_enter(facts, a.phase)
    elif a.event == "component_done":
        # paired with agent_dispatch — this is the per-component cost that no run
        # has been able to report so far
        auto_duration = _elapsed_ms_since_dispatch(
            facts, detail.get("component") or detail.get("task"))
    else:
        auto_duration = None

    rec = _event_rec(st.get("run_id"), seq, a.phase, a.event, detail,
                     agent=a.agent,
                     skills=a.skills.split(",") if a.skills else [],
                     superpowers=a.superpowers.split(",") if a.superpowers else [],
                     verdict=a.verdict,
                     duration_ms=auto_duration)
    # state.json is written FIRST (current truth), then the append (history)
    st["current_phase"] = a.phase
    if a.status:
        st["phase_status"] = a.status
        if a.status != "awaiting_user":
            st["pending_user_question"] = None
    if a.verdict and a.phase in ("G3", "G5"):
        # state.json may be absent or partial after an interrupted run — rebuild
        # the key rather than raising KeyError and blocking the gate.
        st.setdefault("gate_verdicts", {"g3": None, "g5": None})
        st["gate_verdicts"]["g3" if a.phase == "G3" else "g5"] = a.verdict
        # A *passing* G5 verdict clears the pending delta scopes — the reviewer
        # has examined them and found no blockers. A non-passing verdict (FAIL)
        # preserves g5_delta_scopes so the orchestrator can continue targeted fixes
        # on those specific components without losing track of which scopes need work.
        if a.phase == "G5" and a.verdict in GATE_PASSING["g5"]:
            st.pop("g5_delta_scopes", None)
    if a.event == "loop_increment":
        key = {"G3": "gate3_iterations", "G5": "gate5_iterations"}.get(a.phase)
        if key:
            loops = st.setdefault("loops", {"gate3_iterations": 0,
                                            "gate5_iterations": 0})
            loops[key] = loops.get(key, 0) + 1
    if a.event == "phase_exit" and a.phase not in st.get("completed_phases", []):
        st.setdefault("completed_phases", []).append(a.phase)
    _apply_state_effects(st, a.phase, a.event, a.verdict, detail, seq,
                         duration_ms=auto_duration, dispatched_at=rec["ts"])
    st["next_action"] = _derive_next_action(a.phase, a.event, a.verdict, st)
    st["updated_at"] = now_iso()
    atomic_write(os.path.join(run_dir, "state.json"),
                 json.dumps(st, ensure_ascii=False, indent=2))
    _append_jsonl(run_dir, rec)
    print("logged seq", rec["seq"], "->", run_dir)

def cmd_state(a):
    run_dir = _find_run_dir(a.root, a.slug)
    st = parse_json_arg(a.json, "--json")
    st["updated_at"] = now_iso()
    atomic_write(os.path.join(run_dir, "state.json"),
                 json.dumps(st, ensure_ascii=False, indent=2))
    print("state updated ->", run_dir)

def cmd_summary(a):
    run_dir = _find_run_dir(a.root, a.slug)
    if a.body_file:
        try:
            with open(a.body_file, encoding="utf-8") as f:
                body = f.read()
        except FileNotFoundError:
            raise CCLogError(
                f"--body-file {a.body_file} not found. Write the summary markdown "
                "first, or omit --body-file to emit a placeholder.")
        except (PermissionError, UnicodeDecodeError) as e:
            raise CCLogError(f"cannot read --body-file {a.body_file}: {e}")
    else:
        body = "# Summary\n"
    atomic_write(os.path.join(run_dir, "summary.md"), body)
    # stamp manifest end — a damaged manifest must not lose the summary itself
    mp = os.path.join(run_dir, "manifest.json")
    if os.path.exists(mp):
        try:
            with open(mp, encoding="utf-8") as f:
                m = json.load(f)
            m["end"] = now_iso()
            atomic_write(mp, json.dumps(m, ensure_ascii=False, indent=2))
        except (json.JSONDecodeError, OSError) as e:
            print(f"cc_log: WARNING manifest.json not stamped ({e}); "
                  "summary.md was written", file=sys.stderr)
    print("summary written ->", run_dir)

def main():
    p = argparse.ArgumentParser(prog="cc_log.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init"); pi.set_defaults(fn=cmd_init)
    pi.add_argument("--root", required=True); pi.add_argument("--slug", default="")
    pi.add_argument("--title", default=""); pi.add_argument("--max-parallel", type=int, default=4)

    pe = sub.add_parser("event"); pe.set_defaults(fn=cmd_event)
    pe.add_argument("--root", required=True); pe.add_argument("--slug", required=True)
    pe.add_argument("--phase", required=True); pe.add_argument("--event", required=True)
    pe.add_argument("--agent", default=None); pe.add_argument("--skills", default="")
    pe.add_argument("--superpowers", default=""); pe.add_argument("--verdict", default=None)
    pe.add_argument("--status", default=None); pe.add_argument("--detail", default="")
    pe.add_argument("--duration-ms", dest="duration_ms", type=int, default=None)
    pe.add_argument("--scope", default=None,
                    help="comma-separated component:layer list for scoped re-entry "
                         "(e.g. 'ordering:adapters,billing:usecases'); when provided "
                         "on a phase_enter for P4, narrows the g5 verdict instead of "
                         "fully invalidating it — enables delta review")
    pe.add_argument("--force", action="store_true",
                    help="bypass the gate latch; logs a MAJOR process_violation")

    ps = sub.add_parser("state"); ps.set_defaults(fn=cmd_state)
    ps.add_argument("--root", required=True); ps.add_argument("--slug", required=True)
    ps.add_argument("--json", required=True)

    pm = sub.add_parser("summary"); pm.set_defaults(fn=cmd_summary)
    pm.add_argument("--root", required=True); pm.add_argument("--slug", required=True)
    pm.add_argument("--body-file", dest="body_file", default=None)

    a = p.parse_args()
    try:
        a.fn(a)
    except CCLogError as e:
        # Logging gates phase entry in this pipeline, so fail loudly with a fix
        # hint instead of writing nothing silently.
        print(f"cc_log: ERROR {e}", file=sys.stderr)
        if e.hint:
            print(f"cc_log: hint — {e.hint}.", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
