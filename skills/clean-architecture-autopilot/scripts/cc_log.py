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
      # agent_dispatch requires --agent (P4 also requires --skills). When you
      # backfill a dispatch after inline serial execution, declare the real
      # start so the pair is timed honestly:
      #   --detail '{"component":"T1","started_at":"2026-09-14T00:13:30+08:00",
      #              "note":"inline serial, backfilled"}'

  # 2b) scoped P4 re-entry (delta review instead of full g5 invalidation)
  python3 cc_log.py event --root <project_dir> --slug <task-slug> \
      --phase P4 --event phase_enter --scope "ordering:adapters,billing:usecases" \
      --status in_progress

  # 2e) abort a false start honestly — NOT three --force'd gate violations
  #     posing as "undo". Requires a reason; stamps manifest.aborted and
  #     reaps every open dispatch.
  python3 cc_log.py event --root <project_dir> --slug <task-slug> \
      --phase P0 --event run_aborted \
      --detail '{"reason":"scope error discovered in P0: this pre-item belongs to the DCF M3 run, not its own task"}'

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

  # 5) generate report.md at DONE — the mechanical processing report (time
  #    ledger, per-component cost, parallelism, user wait, gate verdicts,
  #    signed-off debts, git change list). phase_exit P6 is REFUSED without it.
  python3 cc_log.py report --root <project_dir> --slug <task-slug>

Notes:
- Never mutates prior run.jsonl lines (append-only). state.json is overwritten
  atomically (temp file + os.replace).
- If <root> is not writable, falls back to $CC_SKILL_FALLBACK or ./.cc-skill and
  records the resolved path in manifest.json.resolved_root.
- Secrets: this script writes exactly what you pass; do not pass tokens/keys.
"""
import argparse, json, os, subprocess, sys, tempfile, time, datetime, re


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

# An in-flight pause longer than this is a suspected suspension, not execution:
# run 11 (phase-a) left C6_frontend dispatched overnight and the 8.9h gap read
# as component/P4 wall time, burying the 1.19x active parallelism under 1.01x.
# The report flags such gaps and computes a suspension-adjusted parallelism.
SUSPEND_MS = 2 * 60 * 60 * 1000


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

def _git_out(root, *args):
    """Run git in <root>, return stripped stdout or None. The report shells out
    for the change list; git being absent or the dir not being a repo must
    degrade to a note, never a traceback."""
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True,
                           text=True, timeout=30)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def cmd_init(a):
    slug = a.slug or slugify(a.title)
    run_dir = resolve_dir(a.root, slug)
    # collision: append timestamp if exists and not empty
    if os.path.isdir(run_dir) and os.listdir(run_dir):
        run_dir = run_dir + "-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    os.makedirs(os.path.join(run_dir, "artifacts"), exist_ok=True)
    run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + slug
    # The commit this run started from: the report's "changed files" section
    # diffs against it, so the list is this run's work rather than whatever
    # happens to be uncommitted when the report runs.
    baseline = _git_out(a.root, "rev-parse", "HEAD")
    # Dirt at init is inherited work, not this run's: run 9 started on run 8's
    # 18 uncommitted files and its report claimed all 26 new files as its own
    # (one of them was even run 11's design doc). Count it so the report can
    # warn instead of letting attribution drift silently.
    dirty_status = _git_out(a.root, "status", "--porcelain") or ""
    dirty_at_init = [l[3:] for l in dirty_status.splitlines()
                     if l and not l[3:].startswith(".cc-skill")]
    manifest = {
        "run_id": run_id, "task_title": a.title or slug, "slug": slug,
        "start": now_iso(), "end": None, "resolved_root": os.path.abspath(run_dir),
        "baseline_commit": baseline,
        "dirty_at_init": len(dirty_at_init),
        "dirty_at_init_sample": dirty_at_init[:5],
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
      component_done can be timed from its agent_dispatch. When the dispatch
      carries detail.started_at (backfill after inline serial execution — runs
      8-10 logged dispatch+done in the same second, recording 0s against real
      minutes), that timestamp becomes the effective start and
      dispatch_backfilled marks the pair so the report can flag it.
    - dispatch_phase: which phase a dispatch was opened in, so phase_exit can
      reap the ones never paired. Run 9's P0/P1/P2 dispatches were never closed
      and stayed "in flight" for hours, which reclassified a 7.26h user wait as
      in-flight silence instead of idle.
    - open_components: dispatched but not yet done — a gap with work in flight
      is silence, not idleness (run 5's T4 did 37.8 min of silent work and the
      pause heuristic charged it to idle, recording duration 0).
    - last_interaction_ts: ts of the latest user_loop / debt_signoff / pause —
      the freshness floor a closing report.md must postdate. Runs 8-9 generated
      the report before the sign-off, so "user wait 0.0s / debts unsigned" in
      the closing record contradicted the final state.
    - user_loop_triggers: which USER LOOP triggers have already been recorded.
    - latest_debt_user_loop_seq / latest_g5_pwc_seq: the exact authority chain for
      a debt sign-off. Existence alone was too weak: an old debt question could be
      reused against a new G5 verdict. A sign-off must reference the latest ask,
      and that ask must follow the current PASS_WITH_CONCERNS verdict.
    """
    facts = {"enters": {}, "exits": {}, "last_ts": None,
             "enter_ts": {}, "pause_ms_since_enter": {},
             "dispatch_ts": {}, "pause_ms_since_dispatch": {},
             "dispatch_backfilled": {}, "dispatch_phase": {},
             "open_components": set(),
             "last_interaction_ts": None,
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
                    # Backfilled dispatches declare when the work really began;
                    # an unparseable started_at falls back to the event ts rather
                    # than poisoning every duration computed from it.
                    started = det.get("started_at")
                    facts["dispatch_ts"][comp] = (
                        started if _parse_ts(started) else r.get("ts"))
                    facts["dispatch_backfilled"][comp] = bool(
                        started and _parse_ts(started))
                    facts["dispatch_phase"][comp] = ph
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
                facts["last_interaction_ts"] = r.get("ts")
            elif ev == "debt_signoff":
                facts["last_interaction_ts"] = r.get("ts")
            elif (ev == "gate_verdict" and ph == "G5"
                  and r.get("verdict") == "PASS_WITH_CONCERNS"):
                facts["latest_g5_pwc_seq"] = r.get("seq")
            elif ev == "pause":
                facts["last_interaction_ts"] = r.get("ts")
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
    if event == "run_aborted":
        return "run aborted — see abort_reason; do not resume this run"
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
            #
            # v1.14.0: this latch has NO --force. The compliant path IS asking the
            # user, so "skip asking" has no legal form — yet runs 8 and 9 both
            # forced it and then asked for the sign-off INSIDE P6, which the error
            # text itself had suggested ("re-run with --force"). The same ask, one
            # step earlier, satisfies the latch; --force only converted the
            # orchestrator's impatience into a bypass. Authority latches stay
            # unforceable; mechanical ones keep the escape hatch.
            unsigned = st.get("debts") or []
            if unsigned:
                raise CCLogError(
                    f"refusing to log phase_enter P6: {len(unsigned)} debt(s) "
                    f"still await user sign-off {unsigned}. G5 "
                    f"PASS_WITH_CONCERNS opens P6 only after a real "
                    f"debt_signoff (preceded by a user_loop and carrying the "
                    f"user's answer). There is no --force here: asking the user "
                    f"is the compliant path, so skipping it is not a decision "
                    f"anyone can authorize — if the user says 'skip', that "
                    f"answer IS the sign-off; record it as debt_signoff with "
                    f"their words. Note report.md needs no P6 entry: "
                    f"'cc_log.py report' runs from any phase, so generate it "
                    f"whenever the numbers are wanted.")

    # --- gate_verdict must be spoken inside its gate phase -----------------
    # Run 10 logged G3's verdict 15s BEFORE phase_enter G3: a backfilled batch
    # inverted the order, and the enter/verdict/exits of G5 all landed in one
    # second. A verdict that predates the phase it certifies cannot have been
    # produced by that gate's run — the sequence itself is the evidence.
    if a.event == "gate_verdict" and facts["enter_ts"].get(a.phase) is None:
        if not a.force:
            raise CCLogError(
                f"refusing to log gate_verdict {a.phase}: no phase_enter "
                f"{a.phase} is recorded yet — the verdict must be spoken "
                f"INSIDE the gate phase, after phase_enter. Log the "
                f"phase_enter {a.phase} first, then the verdict. (Backfilled "
                f"batches inverted this order in run 10; the timestamps are "
                f"the only witness.) If the bypass is intentional, re-run "
                f"with --force.")
        violations.append({
            "rule": f"gate_verdict {a.phase} requires a prior phase_enter "
                    f"{a.phase}",
            "actual": f"no phase_enter {a.phase} in run.jsonl"})

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
        # The run's closing report is mechanical, like every other gated
        # artifact. Runs 3–5 each finished with a different hand-assembled
        # artifact (or none); without this latch the report would be optional,
        # and optional steps drift.
        report_path = os.path.join(run_dir, "report.md")
        if not os.path.exists(report_path):
            if not a.force:
                raise CCLogError(
                    f"refusing to log phase_exit P6: {report_path} does "
                    f"not exist. Generate the mechanical processing report "
                    f"before closing the run:\n"
                    f"  python3 cc_log.py report --root <project_dir> "
                    f"--slug <task-slug>\n"
                    f"It assembles the time ledger, per-component cost, "
                    f"parallelism, user wait, gate verdicts, signed-off debts "
                    f"and the git change list from the logs — never "
                    f"hand-written. If the bypass is intentional, re-run "
                    f"with --force.")
            violations.append({
                "rule": "P6 exit requires a generated report.md",
                "actual": f"no report.md in {run_dir}"})
        # Freshness: the report must postdate the last user-visible event.
        # Runs 8-9 generated it before the debt sign-off, so the closing record
        # said "user wait 0.0s / debts unsigned" while the final state was the
        # opposite on both counts. A report is cheap to regenerate; a report
        # that disagrees with the log it summarizes is worse than none.
        else:
            gen_ts = None
            try:
                with open(report_path, encoding="utf-8") as f:
                    m = re.search(r"生成时间\*\*:\s*(\S+)", f.read())
                if m:
                    gen_ts = _parse_ts(m.group(1))
            except OSError:
                pass
            last_int = _parse_ts(facts["last_interaction_ts"])
            stale = True
            if gen_ts is not None and last_int is not None:
                try:
                    stale = gen_ts < last_int
                except TypeError:  # naive vs aware mix — cannot prove fresh
                    stale = True
            elif last_int is None:
                stale = False
            if stale:
                if not a.force:
                    raise CCLogError(
                        f"refusing to log phase_exit P6: {report_path} is "
                        f"STALE — generated before the last user-visible "
                        f"event ({facts['last_interaction_ts']}). Runs 8-9 "
                        f"closed on reports that predated their sign-offs, so "
                        f"user wait read 0.0s and debts read unsigned while "
                        f"both had changed. Regenerate it:\n"
                        f"  python3 cc_log.py report --root <project_dir> "
                        f"--slug <task-slug>\n"
                        f"If the bypass is intentional, re-run with --force.")
                violations.append({
                    "rule": "P6 exit requires a report.md fresher than the "
                            "last user interaction",
                    "actual": f"report predates {facts['last_interaction_ts']}"})
        # Git closure: an uncommitted tree poisons the NEXT run's baseline.
        # Run 5 (etf) closed with "git 收尾" written in its exit detail but
        # never committed; run 6's report then diffed against a stale baseline
        # and listed 19 of run 5's files as its own. A run that finishes must
        # leave a tree whose diff-to-baseline is exactly its own work.
        # (Plain if, not elif: under --force each closure defect records its
        # own violation instead of the first one masking the rest.)
        if detail.get("commit") != "deferred":
            dirty = _git_out(a.root, "status", "--porcelain") or ""
            dirty = [l for l in dirty.splitlines()
                     if l and not l[3:].startswith(".cc-skill")]
            if dirty:
                if not a.force:
                    raise CCLogError(
                        f"refusing to log phase_exit P6: {len(dirty)} "
                        f"uncommitted change(s) remain (excluding .cc-skill/). "
                        f"Commit this run's work before closing, so the next "
                        f"run's report baseline contains only its own work. "
                        f"Run 5 closed without committing and run 6's change "
                        f"list inherited 19 of its files. If deferring the "
                        f"commit is a deliberate choice, re-run with "
                        f"--detail '{{\"commit\":\"deferred\","
                        f"\"reason\":\"...\"}}'. If the bypass is intentional, "
                        f"re-run with --force.")
                violations.append({
                    "rule": "P6 exit requires a committed tree (or an "
                            "explicit deferral)",
                    "actual": f"{len(dirty)} uncommitted path(s), e.g. "
                              f"{dirty[:3]}"})

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

    # --- G5 verdict requires its artifact on disk --------------------------
    # The review's findings have lived only inside gate_verdict event details
    # for two consecutive runs (5 and 6) despite the contract naming
    # artifacts/g5-review.json as their machine-readable home — the delta
    # review's scope routing and the process-tuning audit both read the
    # artifact, not the event stream. Prose didn't hold; this is the same
    # lesson as every other latch.
    #
    # v1.11 checked only that the keys existed, and run 7 delivered exactly to
    # that floor: 4 findings all with id=None and evidence=None, content
    # stuffed into invented top-level keys (run/gate/checklist), review_mode
    # missing. A latch validates what it checks — so this one now checks every
    # field the downstream consumers actually read: per-finding id, evidence
    # and scope (the delta review routes on scope), and review_mode.
    if a.event == "gate_verdict" and a.phase == "G5":
        g5_path = os.path.join(run_dir, "artifacts", "g5-review.json")
        errs = []
        if not os.path.exists(g5_path):
            errs.append("missing")
        else:
            try:
                with open(g5_path, encoding="utf-8") as f:
                    art = json.load(f)
                if not isinstance(art.get("findings"), list) or not art["findings"]:
                    errs.append("no findings[] array")
                else:
                    for i, fnd in enumerate(art["findings"]):
                        if not isinstance(fnd, dict):
                            errs.append(f"findings[{i}] not an object")
                            continue
                        for k in ("id", "evidence", "scope"):
                            if not fnd.get(k):
                                errs.append(f"findings[{i}].{k} empty")
                if not art.get("verdict"):
                    errs.append("no verdict key")
                if not art.get("review_mode"):
                    errs.append("no review_mode (full|delta)")
            except (OSError, json.JSONDecodeError) as e:
                errs.append(f"unreadable ({e})")
        if errs:
            if not a.force:
                raise CCLogError(
                    f"refusing to log gate_verdict G5: {g5_path} — "
                    + "; ".join(errs[:6])
                    + (f" (+{len(errs) - 6} more)" if len(errs) > 6 else "")
                    + ". The review's findings must live in the artifact in "
                    f"contract shape: every finding carries id / scope / "
                    f"evidence (scope is what the delta review routes on), "
                    f"plus verdict and review_mode. Run 7 delivered exactly "
                    f"to the old key-existence floor (id=None, evidence=None, "
                    f"content in invented keys) — a latch validates what it "
                    f"checks, so this one now checks every field downstream "
                    f"consumers read. If the bypass is intentional, re-run "
                    f"with --force.")
            violations.append({
                "rule": "G5 verdict requires artifacts/g5-review.json in "
                        "contract shape (per-finding id/scope/evidence, "
                        "review_mode)",
                "actual": "; ".join(errs[:6])})

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
            # recorded pointer, not a per-run discovery). _resolve_path
            # returns None when the artifact is nowhere on disk — open(None)
            # raises TypeError, which the except clause below does not catch
            # (the v1.14.0 test suite hit it with a planless dry-run).
            try:
                art_path = _resolve_path(art, os.path.join(run_dir, art),
                                         os.path.join(a.root, art))
                with open(art_path, encoding="utf-8") as f:
                    plan_ptr = (json.load(f) or {}).get("plan_artifact")
            except (OSError, TypeError, json.JSONDecodeError):
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

    # --- dispatches must say WHO ran them ---------------------------------
    # Runs 8-11 logged every agent_dispatch with agent=None and no skills —
    # run 5 carried both, so this is a regression, not a tool limit. Without
    # them the augmentation-ROI audit (ca-process-tuning's input) cannot tell
    # "fired and changed nothing" from "was never installed". A latch validates
    # what it checks; unvalidated fields get dropped.
    if a.event == "agent_dispatch":
        if not (a.agent or "").strip():
            if not a.force:
                raise CCLogError(
                    "refusing to log agent_dispatch: --agent is required — "
                    "the dispatched role (e.g. ca-clean-implementer), or "
                    "'orchestrator' when the orchestrator does the work "
                    "itself. Runs 8-11 logged every dispatch with no agent, "
                    "blinding the augmentation-ROI audit: it can no longer "
                    "tell which agent or skill mix produced which component. "
                    "If the bypass is intentional, re-run with --force.")
            violations.append({
                "rule": "agent_dispatch must name its agent",
                "actual": "--agent missing/empty"})
        if a.phase == "P4" and not (a.skills or "").strip():
            if not a.force:
                raise CCLogError(
                    "refusing to log agent_dispatch (P4): --skills is "
                    "required — the methodology skills injected into the "
                    "implementer (e.g. ca-dependency-rule,"
                    "ca-layer-boundaries). This is the field the per-layer "
                    "injection matrix exists to fill; an empty value records "
                    "that a component was built with no methodology at all. "
                    "If the bypass is intentional, re-run with --force.")
            violations.append({
                "rule": "P4 agent_dispatch must name its injected skills",
                "actual": "--skills missing/empty"})
        # F3: a dispatch must be logged while its phase is OPEN. Run 13 wrote
        # P1's dispatch/done AFTER phase_exit P1 (a backfill ordering slip), so
        # the pair's phase attribution dangled outside any open phase — and
        # paired with the fabricated started_at, both errors compounded. The
        # work belongs to the phase that was executing when it began; logging
        # it after the phase closed breaks that attribution. Log dispatch
        # BEFORE phase_exit, or accept the mechanical refusal.
        opened = (facts["enters"].get(a.phase, 0)
                  > facts["exits"].get(a.phase, 0))
        if not opened:
            if not a.force:
                raise CCLogError(
                    f"refusing to log agent_dispatch: phase {a.phase} has "
                    f"exited (enters={facts['enters'].get(a.phase, 0)}, "
                    f"exits={facts['exits'].get(a.phase, 0)}). A dispatch "
                    f"records work BEGINNING in an open phase; logging it "
                    f"after the phase closed (run 13 did this for P1) breaks "
                    f"the phase attribution. Log the dispatch before the "
                    f"phase_exit, or log it against the phase actually open. "
                    f"If the bypass is intentional, re-run with --force.")
            violations.append({
                "rule": "agent_dispatch must be logged while its phase is open",
                "actual": f"phase {a.phase} not open "
                          f"(enters={facts['enters'].get(a.phase, 0)}, "
                          f"exits={facts['exits'].get(a.phase, 0)})"})
        # started_at credibility: the field is BACKFILL-ONLY, and its value
        # must be plausible. Run 13 attached one to every dispatch — synthetic
        # round timestamps (00:00, 00:10, 00:20…) that predated the run by
        # ~12h — inflating every component to 4h+ and printing a physically
        # impossible 261x parallelism. A field's PRESENCE is not its TRUTH:
        # this is the third manifestation of the same minimal-compliance game
        # (run 5 pause-swallow, runs 8-10 same-second backfill, run 13
        # fabricated stamps). The plausible window is [phase_enter of this
        # phase, the dispatch event's own ts]: work cannot start before its
        # phase opened, nor after the moment the dispatch is recorded.
        started = detail.get("started_at")
        if started is not None:
            s_ts = _parse_ts(started)
            phase_open = _parse_ts(facts["enter_ts"].get(a.phase))
            now = datetime.datetime.now().astimezone()
            bad = None
            if s_ts is None:
                bad = "unparseable timestamp"
            else:
                # Normalize naive stamps (run 13's fabrications carried no
                # timezone) to aware before comparing against aware ts —
                # naive-vs-aware raises TypeError instead of comparing.
                if s_ts.tzinfo is None:
                    s_ts = s_ts.replace(tzinfo=now.tzinfo)
                if phase_open is not None and phase_open.tzinfo is None:
                    phase_open = phase_open.replace(tzinfo=now.tzinfo)
                if s_ts > now:
                    bad = f"in the future ({started})"
                elif phase_open is not None and s_ts < phase_open:
                    bad = (f"before phase_enter {a.phase} "
                           f"({facts['enter_ts'].get(a.phase)})")
            if bad:
                if not a.force:
                    raise CCLogError(
                        f"refusing to log agent_dispatch: detail.started_at "
                        f"is implausible — {bad}. started_at is a BACKFILL-"
                        f"ONLY field (declare the real start when logging a "
                        f"dispatch after inline serial execution); a live "
                        f"dispatch omits it entirely — the event's own ts is "
                        f"the start. Run 13 fabricated round stamps on every "
                        f"dispatch and the report printed a 261x parallelism. "
                        f"Drop the field, or give the true start time. If the "
                        f"bypass is intentional, re-run with --force.")
                violations.append({
                    "rule": "agent_dispatch started_at must fall within "
                            "[phase_enter, dispatch ts]",
                    "actual": f"started_at={started} ({bad})"})
        elif a.phase == "P4":
            # Distinguish the two legal shapes in the record: live dispatch
            # (no started_at) vs backfill (declared). Nothing is rejected —
            # this only labels the pair for the report's * marker.
            detail.setdefault("dispatch_kind", "live")

    # --- run_aborted: the honest way to kill a false start ----------------
    # Run 10's cache-key pre-item was a false start: init → P0 → scope error
    # → three --force'd latch violations posing as "undo". The vocabulary had
    # no abort, so the only exits were "hang forever" or "disguise a
    # non-event as three MAJOR violations", which pollutes the violation
    # statistics that real signals depend on.
    if a.event == "run_aborted":
        if not str(detail.get("reason") or "").strip():
            if not a.force:
                raise CCLogError(
                    "refusing to log run_aborted: --detail must carry the "
                    "reason, e.g. '{\"reason\":\"scope error discovered in "
                    "P0: this pre-item belongs to the DCF M3 run, not its own "
                    "task\"}'. An abort without a reason is indistinguishable "
                    "from a crash and teaches the next run nothing. If the "
                    "bypass is intentional, re-run with --force.")
            violations.append({
                "rule": "run_aborted must record its reason",
                "actual": "no reason in --detail"})

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

    # --- phase_exit / run_aborted reaps stale dispatches -------------------
    # A dispatch opened inside a phase belongs to that phase: if the phase is
    # closing and the dispatch was never paired with a component_done, it is
    # stale bookkeeping, not work in flight. Run 9's P0/P1/P2 dispatches were
    # never closed, so every later pause heuristic saw them as "components
    # executing" and reclassified a 7.26h user wait as in-flight silence
    # instead of idle. Reap them (AUTO_CLOSED, with the pairing gap measured)
    # BEFORE the pause annotation below, so the heuristic judges the real
    # in-flight set. run_aborted reaps across ALL phases — an aborted run has
    # no legitimate open work left.
    if a.event in ("phase_exit", "run_aborted"):
        if a.event == "run_aborted":
            reaped_names = sorted(facts["open_components"])
        else:
            reaped_names = sorted(
                c for c in facts["open_components"]
                if facts["dispatch_phase"].get(c) == a.phase)
        for c in reaped_names:
            dur = _elapsed_ms_since_dispatch(facts, c)
            _append_jsonl(run_dir, _event_rec(
                st.get("run_id"), seq, a.phase, "component_done",
                {"component": c, "status": "AUTO_CLOSED",
                 "reason": (f"dispatch opened in {facts['dispatch_phase'].get(c)} "
                            f"never paired with component_done; reaped at "
                            f"{a.event} so it stops masking pause accounting"
                            if a.event == "phase_exit" else
                            "run aborted; reaped so the ledger closes clean")},
                duration_ms=dur))
            _apply_state_effects(st, a.phase, "component_done", None,
                                 {"component": c, "status": "AUTO_CLOSED"},
                                 seq, duration_ms=dur)
            facts["open_components"].discard(c)
            seq += 1

    # --- run_aborted stamps the manifest and freezes the state -------------
    if a.event == "run_aborted":
        st["phase_status"] = "aborted"
        mp = os.path.join(run_dir, "manifest.json")
        try:
            with open(mp, encoding="utf-8") as f:
                m = json.load(f)
            m["end"] = now_iso()
            m["aborted"] = True
            m["abort_reason"] = detail.get("reason")
            atomic_write(mp, json.dumps(m, ensure_ascii=False, indent=2))
        except (OSError, json.JSONDecodeError) as e:
            print(f"cc_log: WARNING manifest.json not stamped aborted ({e})",
                  file=sys.stderr)

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

    # --- mid-review fix counts as a gate iteration -------------------------
    # Run 13's G5 reviewer found a defect, the orchestrator fixed it with a
    # scoped P4 re-entry WHILE G5 was still open, and the review continued to
    # PASS — efficient, legal, and completely invisible to the loop counter
    # (gate5_iterations read 0). A mid-review fix IS a rework round the gate
    # surfaced; cross-run tuning that reads the counter would under-count the
    # gate's interception cost. Auto-increment when P4 re-enters while G5 is
    # open, and say why in the record.
    if (a.event == "phase_enter" and a.phase == "P4"
            and facts["enters"].get("G5", 0) > facts["exits"].get("G5", 0)
            and (st.get("gate_verdicts") or {}).get("g5")
            not in GATE_PASSING["g5"]):
        loops = st.setdefault("loops", {"gate3_iterations": 0,
                                        "gate5_iterations": 0})
        loops["gate5_iterations"] = loops.get("gate5_iterations", 0) + 1
        _append_jsonl(run_dir, _event_rec(
            st.get("run_id"), seq, a.phase, "loop_increment",
            {"gate": "g5", "gate5_iterations": loops["gate5_iterations"],
             "reason": "P4 re-entered while G5 open — mid-review fix counts "
                       "as a gate iteration"}))
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


def _fmt_dur(ms):
    if ms is None:
        return "—"
    if ms >= 3_600_000:
        return f"{ms / 3_600_000:.1f}h"
    if ms >= 60_000:
        return f"{ms / 60_000:.1f}m"
    return f"{ms / 1000:.1f}s"


def _load_events(run_dir):
    p = os.path.join(run_dir, "run.jsonl")
    if not os.path.exists(p):
        raise CCLogError(f"cannot report: no run.jsonl in {run_dir}")
    events = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not events:
        raise CCLogError(f"cannot report: {p} has no readable events")
    return events


def cmd_report(a):
    """Generate report.md — the mechanical processing report at DONE.

    Everything here is assembled from run.jsonl + state.json + git, never
    hand-written: the time ledger, per-component cost, parallelism, user wait,
    gate verdicts, signed-off debts and the change list. P6's exit latch
    refuses to close a run without this file, for the same reason every other
    artifact is gated — prose doesn't hold.
    """
    run_dir = _find_run_dir(a.root, a.slug)
    st = load_state(run_dir)
    events = _load_events(run_dir)

    t0, t1 = _parse_ts(events[0].get("ts")), _parse_ts(events[-1].get("ts"))
    wall_ms = int((t1 - t0).total_seconds() * 1000) if (t0 and t1) else None

    # --- per-phase: recorded (pause-corrected) vs raw (enter→exit timestamps)
    # Accumulated across segments: a phase re-entered after corrective work has
    # multiple enter→exit spans, and dict-overwrite kept only the LAST one —
    # run 13's P4 showed 10m (the mid-review fix segment) while its main
    # 22.8m segment silently vanished. phase_nsegs records how many segments
    # each phase ran so the report can show "(2 段)" next to re-entered phases.
    phase_rec, phase_raw, enter_ts, exit_ts = {}, {}, {}, {}
    phase_nsegs = {}
    for e in events:
        ph, ev = e.get("phase"), e.get("event")
        if ev == "phase_enter":
            enter_ts[ph] = e.get("ts")
        elif ev == "phase_exit":
            exit_ts[ph] = e.get("ts")
            if e.get("duration_ms") is not None:
                phase_rec[ph] = phase_rec.get(ph, 0) + e["duration_ms"]
            s, x = _parse_ts(enter_ts.get(ph)), _parse_ts(e.get("ts"))
            if s and x:
                seg = int((x - s).total_seconds() * 1000)
                phase_raw[ph] = phase_raw.get(ph, 0) + seg
                phase_nsegs[ph] = phase_nsegs.get(ph, 0) + 1

    # --- per-component: same two views, from dispatch to done --------------
    # started_at (backfill after inline serial execution) is the effective
    # start when present: runs 8-10 logged dispatch+done in the same second
    # and recorded 0.5-0.9s against real minutes — a 600x ledger error.
    dispatch_ts, backfilled, comp = {}, {}, {}
    for e in events:
        ev, det = e.get("event"), e.get("detail") or {}
        name = det.get("component") or det.get("task")
        if not name:
            continue
        if ev == "agent_dispatch":
            started = det.get("started_at")
            dispatch_ts[name] = started if _parse_ts(started) else e.get("ts")
            backfilled[name] = bool(started and _parse_ts(started))
        elif ev == "component_done":
            rec = {"status": det.get("status", "DONE")}
            if e.get("duration_ms") is not None:
                rec["rec"] = e["duration_ms"]
            s, x = _parse_ts(dispatch_ts.get(name)), _parse_ts(e.get("ts"))
            if s and x:
                rec["raw"] = max(0, int((x - s).total_seconds() * 1000))
            if det.get("tests"):
                rec["tests"] = det["tests"]
            comp[name] = rec

    # --- user wait: each user_loop → the next non-pause event (the answer)
    user_wait_ms, user_pause_count, questions = 0, 0, 0
    for i, e in enumerate(events):
        if e.get("event") != "user_loop":
            continue
        user_pause_count += 1
        q = (e.get("detail") or {}).get("questions")
        questions += len(q) if isinstance(q, list) else 1
        for f in events[i + 1:]:
            if f.get("event") == "pause":
                continue
            s, x = _parse_ts(e.get("ts")), _parse_ts(f.get("ts"))
            if s and x:
                user_wait_ms += int((x - s).total_seconds() * 1000)
            break

    # --- pauses: only idle-credited ones count as idle
    idle_ms, inflight_gaps = 0, 0
    for e in events:
        if e.get("event") == "pause":
            det = e.get("detail") or {}
            if det.get("credited_to_idle", True):
                idle_ms += det.get("gap_ms") or 0
            else:
                inflight_gaps += 1

    # --- parallelism from RAW values: recorded durations can carry accounting
    # bugs (run 5's T4 recorded 0m against a raw 37.8m); timestamps cannot.
    # manifest is loaded here (before the git section) because the
    # physical-limit check reads config.max_parallel.
    manifest = {}
    mp = os.path.join(run_dir, "manifest.json")
    if os.path.exists(mp):
        try:
            manifest = json.load(open(mp, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
    comp_raw_sum = sum(r.get("raw") or 0 for r in comp.values())
    # Sum across ALL P4 segments (re-entries included): run 13's P4 had two
    # segments (main + mid-review fix) and the last one overwrote the first,
    # showing 10m instead of 33m.
    p4_raw = sum(v for k, v in phase_raw.items() if k == "P4")
    parallel = (comp_raw_sum / p4_raw) if (p4_raw and comp_raw_sum) else None
    # A ratio above max_parallel is physically impossible: N components can
    # at most overlap N-way. Run 13 printed 261.86x because every dispatch
    # carried a fabricated started_at — the ledger itself was the bug, and
    # the report should say so instead of laundering the number.
    max_par = (manifest.get("config") or {}).get("max_parallel", 4) \
        if isinstance(manifest, dict) and manifest else 4
    impossible = parallel is not None and parallel > max_par * 1.05

    # Planned waves (from the P4 enter detail) vs actual overlap (from
    # dispatch/done timestamp spans): a ratio of exactly 1.00x with a planned
    # multi-component wave means parallelism was available but not taken —
    # run 6 planned [[U1,U4],...] yet executed strictly serially, and a bare
    # 1.00x could not distinguish "could not parallelize" from "did not".
    p4_enter_detail = {}
    for e in events:
        if (e.get("phase") == "P4" and e.get("event") == "phase_enter"
                and isinstance(e.get("detail"), dict)):
            p4_enter_detail = e["detail"]
    waves = p4_enter_detail.get("waves")
    planned_wave = None
    if isinstance(waves, list) and waves:
        planned_wave = max(len(w) if isinstance(w, list) else 1 for w in waves)
    spans = []
    for name, r in comp.items():
        if r.get("raw") is not None:
            d = dispatch_ts.get(name)
            if d:
                t = _parse_ts(d)
                spans.append((t, t + datetime.timedelta(milliseconds=r["raw"]),
                              name))
    actual_overlap = 0
    for i, (s1, e1, _) in enumerate(spans):
        for s2, e2, _ in spans[i + 1:]:
            if s1 < e2 and s2 < e1:
                actual_overlap += 1
    overlap_note = None
    if planned_wave and planned_wave > 1 and actual_overlap == 0 and parallel \
            and parallel < 1.05:
        overlap_note = ("计划含多组件 wave 但零重叠 —— 可并行而未并行"
                        "（对照 wave 计划与串行执行原因）")

    # --- suspected suspensions: in-flight pauses longer than SUSPEND_MS -----
    # An overnight gap with a component "in flight" is usually a suspended
    # session, not hours of execution (run 11's C6_frontend sat 8.9h overnight
    # and read as component/P4 wall time, burying the 1.19x active parallelism
    # under 1.01x). Keep the wall numbers but flag them and compute a
    # suspension-adjusted view: the active ratio is what tells tuning whether
    # the wave structure actually overlapped.
    suspensions = []
    for e in events:
        if e.get("event") != "pause":
            continue
        det = e.get("detail") or {}
        if (not det.get("credited_to_idle", True)
                and (det.get("gap_ms") or 0) > SUSPEND_MS):
            since = _parse_ts(det.get("since"))
            if since:
                suspensions.append((since, since + datetime.timedelta(
                    milliseconds=det["gap_ms"]),
                    det.get("components_in_flight") or []))

    def _active_ms(start, end):
        """Wall span minus its overlap with suspected-suspension windows."""
        try:
            total = (end - start).total_seconds() * 1000
        except TypeError:
            return None
        for s, x, _ in suspensions:
            try:
                ov = (min(end, x) - max(start, s)).total_seconds() * 1000
            except TypeError:
                continue
            if ov > 0:
                total -= ov
        return max(0.0, total)

    parallel_active = None
    if suspensions:
        comp_active_sum = 0.0
        for name, r in comp.items():
            if r.get("raw") is None or not dispatch_ts.get(name):
                continue
            d = _parse_ts(dispatch_ts[name])
            if d is None:
                continue
            act = _active_ms(d, d + datetime.timedelta(milliseconds=r["raw"]))
            if act is not None:
                comp_active_sum += act
        p4s, p4x = _parse_ts(enter_ts.get("P4")), _parse_ts(exit_ts.get("P4"))
        p4_active = _active_ms(p4s, p4x) if (p4s and p4x) else None
        if p4_active and comp_active_sum:
            parallel_active = comp_active_sum / p4_active

    def _flag(rec, raw):
        if rec is None or raw is None:
            return ""
        if raw > 60_000 and abs(raw - rec) > max(60_000, 0.2 * raw):
            return " ⚠"
        return ""

    # --- git change list, diffed against the run's baseline commit
    # (manifest loaded earlier, before the parallelism check)
    baseline = manifest.get("baseline_commit")
    ref = baseline or "HEAD"
    shortstat = _git_out(a.root, "diff", "--shortstat", ref)
    numstat = _git_out(a.root, "diff", "--numstat", ref) or ""
    plus_del = {}
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            plus_del[parts[2]] = (parts[0], parts[1])
    porcelain = _git_out(a.root, "status", "--porcelain") or ""
    new_files, new_lines = [], 0
    for line in porcelain.splitlines():
        if not line.startswith("??"):
            continue
        path = line[3:]
        if path.startswith(".cc-skill"):
            continue
        new_files.append(path)
        try:
            with open(os.path.join(a.root, path), encoding="utf-8",
                      errors="ignore") as f:
                new_lines += sum(1 for _ in f)
        except OSError:
            pass

    L = []
    L.append(f"# 处理报告 — {st.get('task_title', a.slug)}")
    L.append("")
    L.append(f"- **run_id**: `{st.get('run_id')}`")
    L.append(f"- **运行区间**: {events[0].get('ts')} → {events[-1].get('ts')}"
             f"（墙钟 **{_fmt_dur(wall_ms)}**）")
    baseline_note = baseline or "（init 时未记录，diff 针对 HEAD）"
    L.append(f"- **基线提交**: `{baseline_note}`")
    L.append(f"- **生成时间**: {now_iso()}")
    L.append("")
    L.append("## 时间账本")
    L.append("")
    L.append("| 维度 | 数值 |")
    L.append("|---|---|")
    L.append(f"| 墙钟 | {_fmt_dur(wall_ms)} |")
    L.append(f"| 用户等待（user_loop → 应答，{user_pause_count} 次 / "
             f"{questions} 问） | {_fmt_dur(user_wait_ms)} |")
    L.append(f"| 空转（pause·计为空闲） | {_fmt_dur(idle_ms)} |")
    if inflight_gaps:
        L.append(f"| 在制静默（pause·未计空闲，见 F1 修复说明） | {inflight_gaps} 段 |")
    for s, x, s_comps in suspensions:
        L.append(f"| ⚠ 疑似挂起 | {_fmt_dur(int((x - s).total_seconds() * 1000))}"
                 f"（in-flight {s_comps} 静默 >2h —— 会话挂起而非执行，"
                 f"已计入组件/阶段墙钟） |")
    if parallel:
        wave_desc = (f"（计划最大 wave {planned_wave} 组件 / 实际重叠 "
                     f"{actual_overlap} 对）"
                     if planned_wave is not None else "")
        if impossible:
            L.append(f"| ⚠ P4 并行度 | **{parallel:.2f}x — IMPOSSIBLE**（超过 "
                     f"max_parallel={max_par}：N 个组件最多重叠 N 路，账本被 "
                     f"污染——查 dispatch 的 started_at 是否为编造时间戳，"
                     f"run 13 曾以此打出 261.86x）{wave_desc} |")
        else:
            L.append(f"| P4 并行度（Σ组件墙钟 ÷ P4 墙钟） | {parallel:.2f}x "
                     f"{wave_desc} |")
    if parallel_active:
        L.append(f"| P4 并行度（活跃口径，剔除疑似挂起段） | "
                 f"{parallel_active:.2f}x |")
    if overlap_note:
        L.append(f"| ⚠ 并行机会 | {overlap_note} |")
    L.append("")
    L.append("### 各阶段耗时")
    L.append("")
    L.append("| 阶段 | 记录耗时¹ | 墙钟 |")
    L.append("|---|---|---|")
    for ph in ["P0", "P1", "P2", "G3", "P4", "G5", "P6"]:
        if ph in phase_rec or ph in phase_raw:
            nsegs = phase_nsegs.get(ph, 0)
            seg_note = f"（{nsegs} 段累计）" if nsegs > 1 else ""
            L.append(f"| {ph}{seg_note} | {_fmt_dur(phase_rec.get(ph))}"
                     f"{_flag(phase_rec.get(ph), phase_raw.get(ph))}"
                     f" | {_fmt_dur(phase_raw.get(ph))} |")
    L.append("")
    if comp:
        L.append("### 组件耗时")
        L.append("")
        L.append("| 组件 | 状态 | 记录耗时¹ | 墙钟 | 测试 |")
        L.append("|---|---|---|---|---|")
        for name, r in comp.items():
            bf = "*" if backfilled.get(name) else ""
            L.append(f"| {name}{bf} | {r['status']} | {_fmt_dur(r.get('rec'))}"
                     f"{_flag(r.get('rec'), r.get('raw'))}"
                     f" | {_fmt_dur(r.get('raw'))} | {r.get('tests', '—')} |")
        L.append("")
        L.append("¹ 记录耗时 = pause 校正值；标 ⚠ 表示与墙钟偏差超过 20%，以墙钟列为准；"
                 "组件名带 \\* 表示 dispatch 为补录（detail.started_at 声明真实开始时刻，"
                 "墙钟从该时刻起算——内联串行执行后补记时必须给出，"
                 "否则 dispatch/done 同刻会把几分钟记成 0 秒）。")
        L.append("")
    gv = st.get("gate_verdicts") or {}
    loops = st.get("loops") or {}
    L.append("## 门与裁决")
    L.append("")
    L.append(f"- **G3 依赖审计**: {gv.get('g3')}（{loops.get('gate3_iterations', 0)} 轮迭代）")
    L.append(f"- **G5 架构评审**: {gv.get('g5')}（{loops.get('gate5_iterations', 0)} 轮迭代）")
    signed = st.get("debts_signed_off") or []
    if signed:
        L.append(f"- **已签字债务**: {len(signed)} 项")
        for d in signed:
            L.append(f"  - {d}")
    if st.get("debts"):
        L.append(f"- **未签字债务**: {len(st['debts'])} 项 ⚠（P6 出口门闩应已拦截）")
    ledger = st.get("question_ledger")
    if ledger:
        L.append(f"- **提问账本**: 询问 {ledger.get('asked', 0)} / "
                 f"自答 {ledger.get('self_resolved', 0)}")
    L.append("")
    L.append("## 改动文件")
    L.append("")
    if shortstat is None and not new_files:
        L.append("（git 不可用或非 git 仓库 —— 文件清单无法生成）")
    else:
        if shortstat:
            L.append(f"相对基线 `{ref[:12] if ref != 'HEAD' else 'HEAD'}`：{shortstat}")
        else:
            L.append(f"相对基线 `{ref[:12] if ref != 'HEAD' else 'HEAD'}`：无已跟踪文件改动")
        if new_files:
            L.append("")
            L.append(f"新增 {len(new_files)} 个文件，共 {new_lines} 行：")
            L.append("")
            for path in new_files:
                L.append(f"- `{path}`")
        if plus_del:
            L.append("")
            L.append("| 文件 | +行 | −行 |")
            L.append("|---|---|---|")
            for path, (p, d) in sorted(plus_del.items()):
                if path.startswith(".cc-skill"):
                    continue
                L.append(f"| `{path}` | {p} | {d} |")
        L.append("")
        L.append("> 注意：清单为基线以来的全部工作区改动；若基线前已有未提交工作，"
                 "可能混入本次运行之外的文件。")
        dirty0 = manifest.get("dirty_at_init")
        if dirty0:
            L.append(">")
            L.append(f"> ⚠ init 时工作区已有 {dirty0} 个未提交文件"
                     f"（如 {manifest.get('dirty_at_init_sample')}）——"
                     f"本清单可能混入上一轮的交付，归因以下一轮干净基线为准"
                     f"（run 9 曾把 run 8 的 18 个文件记成自己的新增）。")
    L.append("")
    out = os.path.join(run_dir, "report.md")
    atomic_write(out, "\n".join(L) + "\n")
    print(f"report written -> {out}")
    print(f"  wall {_fmt_dur(wall_ms)} | user wait {_fmt_dur(user_wait_ms)} | "
          f"idle {_fmt_dur(idle_ms)}"
          + (f" | parallel {parallel:.2f}x" if parallel else "")
          + f" | {len(comp)} components | {len(new_files)} new files")

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

    pr = sub.add_parser("report"); pr.set_defaults(fn=cmd_report)
    pr.add_argument("--root", required=True); pr.add_argument("--slug", required=True)

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
