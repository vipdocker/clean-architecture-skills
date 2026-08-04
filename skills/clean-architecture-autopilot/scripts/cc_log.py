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
      --phase P2 --event phase_enter [--agent architecture-designer] \
      [--skills a,b] [--superpowers c,d] [--verdict APPROVED] \
      [--status in_progress] [--detail '{"k":"v"}'] [--duration-ms 1234]

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
    return (s or "run")[:40]

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

def cmd_event(a):
    run_dir = _find_run_dir(a.root, a.slug)
    try:
        os.makedirs(run_dir, exist_ok=True)
    except OSError as e:
        raise CCLogError(f"cannot create run dir {run_dir}: {e}",
                         hint=FALLBACK_HINT)
    detail = parse_json_arg(a.detail, "--detail") if a.detail else {}
    rec = {"ts": now_iso(), "run_id": load_state(run_dir).get("run_id"),
           "seq": next_seq(run_dir), "phase": a.phase, "event": a.event,
           "agent": a.agent, "skills": a.skills.split(",") if a.skills else [],
           "superpowers": a.superpowers.split(",") if a.superpowers else [],
           "verdict": a.verdict, "detail": detail, "duration_ms": a.duration_ms}
    # state.json is written FIRST (current truth), then the append (history)
    st = load_state(run_dir)
    st["current_phase"] = a.phase
    if a.status:
        st["phase_status"] = a.status
    if a.verdict and a.phase in ("G3", "G5"):
        # state.json may be absent or partial after an interrupted run — rebuild
        # the key rather than raising KeyError and blocking the gate.
        st.setdefault("gate_verdicts", {"g3": None, "g5": None})
        st["gate_verdicts"]["g3" if a.phase == "G3" else "g5"] = a.verdict
    if a.event == "phase_exit" and a.phase not in st.get("completed_phases", []):
        st.setdefault("completed_phases", []).append(a.phase)
    st["updated_at"] = now_iso()
    atomic_write(os.path.join(run_dir, "state.json"),
                 json.dumps(st, ensure_ascii=False, indent=2))
    try:
        with open(os.path.join(run_dir, "run.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        raise CCLogError(f"cannot append run.jsonl in {run_dir}: {e}",
                         hint=FALLBACK_HINT)
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
