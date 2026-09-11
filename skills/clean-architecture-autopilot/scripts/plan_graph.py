#!/usr/bin/env python3
"""plan_graph.py — apply the Dependency Rule to the PLAN, not just the code.

Why this exists (run 3, option-seller-phase-a):
  The P2 plan had 8 DAG tasks. `T8 frontend` declared `depends_on: ["T6"]`, where
  T6 is the *route implementation*. That put the frontend at depth 5 — the tail of
  the critical path — behind the entire backend chain
  T2 → T4 → T5 → T6 → T8.

  But the frontend never needed the route implementation. It needed the *response
  contract*, and that contract already existed at P2 time: the design source has a
  "### 5.6 响应契约" section and the artifact carries
  `ports_and_boundaries.boundary_dtos`. A contract produced by P2 is available at
  t=0, so the frontend could have started alongside T1/T2 at depth 1.

  Measured cost of getting this wrong: the backend chain T1→T6 completed in 25.7
  minutes while the frontend sat waiting, then ran afterwards. The whole P4 round
  took 75.2 minutes when its floor was max(backend, frontend) rather than the sum.

  This is the Dependency Rule violated one level up. A presentation task depending
  on a server implementation is an outward dependency in the plan graph: the outer
  thing (UI) reaching for a concretion (the route) instead of the abstraction (the
  boundary DTO). `ca-dependency-rule` already forbids this in code; nothing checked
  the plan.

What it does NOT do: quantify the saving. That needs per-task durations, which
require `agent_dispatch` paired with `component_done`. This script proves an edge
is *unnecessary*; it cannot tell you how many minutes it costs.

Usage:
  python3 plan_graph.py --artifact .cc-skill/<slug>/artifacts/p2-design.json
      [--presentation-dirs templates,static,public] [--json out.json]
      [--report-only]

Exit codes: 0 = no avoidable serialization, 2 = found (or the graph is malformed).
"""
import argparse, json, os, sys

DEFAULT_PRESENTATION_DIRS = (
    "templates,static,public,assets,web,frontend,ui,"
    "src/components,src/pages,src/views,app/components"
)
PRESENTATION_EXTS = (".html", ".htm", ".css", ".scss", ".less", ".vue", ".svelte",
                     ".jsx", ".tsx")
TEST_MARKERS = ("test/", "tests/", "spec/", "__tests__/", "_test.", "test_",
                ".test.", ".spec.")


def _is_test(path):
    p = path.replace("\\", "/").lower()
    return any(m in p for m in TEST_MARKERS)


def _is_presentation(path, pres_dirs):
    p = path.replace("\\", "/").lower()
    if any(p == d or p.startswith(d.rstrip("/") + "/") for d in pres_dirs):
        return True
    return p.endswith(PRESENTATION_EXTS)


def classify(files, pres_dirs):
    """presentation | server | mixed | test_only | unknown.

    Tests are excluded from the verdict: a presentation task carrying its own JS
    test is still a presentation task, and the per-task test rule means almost
    every task has some.
    """
    real = [f for f in files if not _is_test(f)]
    if not real:
        return "test_only" if files else "unknown"
    pres = [f for f in real if _is_presentation(f, pres_dirs)]
    if len(pres) == len(real):
        return "presentation"
    if not pres:
        return "server"
    return "mixed"


def _normalize_files(t):
    # Two key spellings occur: the contract's files_touched (run 3) and the
    # component-keyed shape's plain files (run 4's p4-components.json).
    f = t.get("files_touched") or t.get("files") or []
    if isinstance(f, str):
        f = [x.strip() for x in f.split(",") if x.strip()]
    return f


def build(artifact, pres_dirs):
    """Normalize the plan into {id: node}.

    Two shapes occur in the wild, because each run has historically invented its
    own: the contract shape {"dag_tasks": [...]} and run 4's component-keyed
    {"C1_db_schema": {...}, ...} (p4-components.json). The checker reads both —
    but the P2 exit latch decides WHERE the plan lives, via dag_tasks in the
    contract artifact or an explicit plan_artifact pointer; silently guessing at
    locations is how run 4's plan escaped every check.
    """
    tasks = artifact.get("dag_tasks") or artifact.get("tasks") or []
    if not tasks:
        # Component-keyed shape (run 4's p4-components.json): every top-level
        # key whose value looks like a task (declares files or depends_on) is a
        # task; sibling keys like defects_found_and_fixed / browser_verification
        # are run observations, not tasks.
        for key, val in artifact.items():
            if not isinstance(val, dict):
                continue
            if not (val.get("files_touched") or val.get("files")
                    or "depends_on" in val):
                continue
            t = dict(val)
            t.setdefault("id", key)
            t.setdefault("name", key)
            tasks.append(t)
    nodes = {}
    for t in tasks:
        tid = t.get("id") or t.get("name")
        if not tid:
            continue
        files = _normalize_files(t)
        nodes[tid] = {
            "id": tid,
            "name": t.get("name", tid),
            "depends_on": list(t.get("depends_on") or []),
            "files": files,
            "kind": classify(files, pres_dirs),
            "consumes_contract": list(t.get("consumes_contract") or []),
            "model": t.get("recommended_model"),
        }
    return nodes


def depths(nodes):
    """Longest-path depth per node. Returns (depths, cycle_nodes)."""
    memo, visiting, cyc = {}, set(), set()

    def walk(i):
        if i in memo:
            return memo[i]
        if i in visiting:
            cyc.add(i)
            return 1
        visiting.add(i)
        deps = [d for d in nodes[i]["depends_on"] if d in nodes]
        memo[i] = 1 + max((walk(d) for d in deps), default=0)
        visiting.discard(i)
        return memo[i]

    for i in nodes:
        walk(i)
    return memo, cyc


def longest_chain(nodes, d):
    """One concrete longest chain, for the report."""
    if not nodes:
        return []
    tail = max(nodes, key=lambda i: d[i])
    chain = [tail]
    while True:
        deps = [x for x in nodes[chain[-1]]["depends_on"] if x in nodes]
        if not deps:
            break
        chain.append(max(deps, key=lambda i: d[i]))
    return list(reversed(chain))


def contracts_available(artifact):
    """The boundary abstractions a presentation task could depend on instead of an
    implementation. P2 owns these, so they exist before any task runs."""
    found = []
    pb = artifact.get("ports_and_boundaries")
    if isinstance(pb, dict):
        dtos = pb.get("boundary_dtos")
        if isinstance(dtos, list):
            found += [d.get("name", str(d)) if isinstance(d, dict) else str(d)
                      for d in dtos]
        elif dtos:
            found.append("boundary_dtos")
        found += [k for k in pb if k != "boundary_dtos"]
    for key in ("boundary_dtos", "response_contract", "ports"):
        v = artifact.get(key)
        if isinstance(v, list):
            found += [x.get("name", str(x)) if isinstance(x, dict) else str(x)
                      for x in v]
        elif v:
            found.append(key)
    return sorted({f for f in found if f})


def check(artifact_path, pres_dirs):
    try:
        with open(artifact_path, encoding="utf-8") as f:
            artifact = json.load(f)
    except OSError as e:
        raise SystemExit(f"plan_graph: cannot read --artifact {artifact_path}: {e}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"plan_graph: --artifact {artifact_path} is not valid "
                         f"JSON ({e})")

    nodes = build(artifact, pres_dirs)
    if not nodes:
        return {"artifact": artifact_path, "verdict": "NO_TASKS", "tasks": 0,
                "avoidable_serialization": [], "cycles": [],
                "contracts_available": [], "critical_path": [],
                "critical_depth": 0, "depth_without_avoidable": 0,
                "waived": []}

    d, cyc = depths(nodes)
    contracts = contracts_available(artifact)
    waived = artifact.get("plan_serialization_waived") or []
    waived_pairs = {(w.get("task"), w.get("depends_on"))
                    for w in waived if isinstance(w, dict)}

    avoidable = []
    for n in nodes.values():
        if n["kind"] != "presentation":
            continue
        for dep in n["depends_on"]:
            if dep not in nodes:
                continue
            if nodes[dep]["kind"] not in ("server", "mixed"):
                continue
            if (n["id"], dep) in waived_pairs:
                continue
            # Only avoidable when an abstraction exists to depend on instead.
            if not contracts:
                continue
            avoidable.append({
                "task": n["id"], "task_name": n["name"],
                "depends_on": dep, "dep_name": nodes[dep]["name"],
                "dep_kind": nodes[dep]["kind"],
                "task_depth": d[n["id"]],
                "declares_consumes_contract": bool(n["consumes_contract"]),
                "why": (f"{n['id']} touches only presentation files but blocks on "
                        f"{dep} ({nodes[dep]['kind']} implementation). The plan "
                        f"already carries boundary abstractions "
                        f"({', '.join(contracts[:4])}"
                        f"{'…' if len(contracts) > 4 else ''}), which P2 owns and "
                        f"which exist before any task runs — depend on the "
                        f"contract, not the implementation."),
            })

    # Depth if every avoidable edge were removed. The graph maximum often does NOT
    # move — on run 3 it stays 5 because T7 (composition root) also sits there — so
    # each flagged task also carries its OWN before/after, which is the number that
    # means something: T8 goes 5 → 1 and can start immediately.
    trimmed = {k: dict(v) for k, v in nodes.items()}
    for a in avoidable:
        trimmed[a["task"]]["depends_on"] = [
            x for x in trimmed[a["task"]]["depends_on"] if x != a["depends_on"]]
    d2, _ = depths(trimmed)
    for a in avoidable:
        a["task_depth_after"] = d2.get(a["task"], a["task_depth"])

    verdict = "FAIL" if (avoidable or cyc) else "PASS"
    return {
        "artifact": artifact_path,
        "verdict": verdict,
        "tasks": len(nodes),
        "cycles": sorted(cyc),
        "contracts_available": contracts,
        "critical_path": longest_chain(nodes, d),
        "critical_depth": max(d.values()),
        "depth_without_avoidable": max(d2.values()) if d2 else 0,
        "avoidable_serialization": avoidable,
        "waived": [f"{w.get('task')}->{w.get('depends_on')}: "
                   f"{w.get('reason', 'no reason given')}" for w in waived],
        "kinds": {i: n["kind"] for i, n in nodes.items()},
    }


def format_report(res):
    if res["verdict"] == "NO_TASKS":
        return ("plan_graph: NO_TASKS — the artifact declares no dag_tasks; "
                "nothing to check.")
    out = [f"plan_graph: {res['verdict']} — {res['tasks']} tasks, critical depth "
           f"{res['critical_depth']}"]
    out.append("  critical path: " + " → ".join(res["critical_path"]))
    out.append("  task kinds: " + ", ".join(
        f"{k}={v}" for k, v in sorted(res["kinds"].items())))
    if res["cycles"]:
        out.append(f"\nCYCLE in the plan graph: {res['cycles']} — a DAG is a "
                   f"precondition for wave dispatch. Break it before exiting P2.")
    if res["avoidable_serialization"]:
        out.append(f"\nAVOIDABLE SERIALIZATION "
                   f"({len(res['avoidable_serialization'])}):")
        for a in res["avoidable_serialization"]:
            out.append(f"  - {a['task']} ({a['task_name']}): depth "
                       f"{a['task_depth']} → {a['task_depth_after']} if the edge "
                       f"to {a['depends_on']} ({a['dep_name']}, {a['dep_kind']}) "
                       f"is dropped")
            out.append(f"      {a['why']}")
        gmax, gmax2 = res["critical_depth"], res["depth_without_avoidable"]
        if gmax2 < gmax:
            out.append(f"\n  graph critical depth {gmax} → {gmax2}.")
        else:
            out.append(f"\n  graph critical depth stays {gmax} — another task "
                       f"still sits at that depth. That does not make the fix "
                       f"pointless: the freed task starts immediately instead of "
                       f"waiting out the chain, and whether that shortens the run "
                       f"depends on which task is actually expensive.")
        out.append("  Note: depth is structure, not time. The real saving needs "
                   "per-task durations (agent_dispatch paired with "
                   "component_done); this check only proves the edge is "
                   "unnecessary.")
    if res["waived"]:
        out.append("\nWAIVED (recorded decisions):")
        out += [f"  - {w}" for w in res["waived"]]
    if res["verdict"] == "FAIL":
        out.append("\nFix: point the presentation task at the boundary contract "
                   "instead of the implementation — clear its depends_on and "
                   "declare `consumes_contract: [\"<dto name>\"]`. If it genuinely "
                   "needs the implementation (e.g. a server-rendered variable that "
                   "only exists once the route is written), record it in "
                   "`plan_serialization_waived: [{task, depends_on, reason}]`.")
    return "\n".join(out)


def summarize(res):
    """One line for a caller that refuses a transition and needs the reason."""
    bits = []
    if res.get("cycles"):
        bits.append(f"cycle {res['cycles']}")
    for a in res.get("avoidable_serialization") or []:
        bits.append(f"{a['task']}→{a['depends_on']} "
                    f"(depth {a['task_depth']}→{a['task_depth_after']})")
    return "; ".join(bits) or "no findings"


def main():
    p = argparse.ArgumentParser(prog="plan_graph.py")
    p.add_argument("--artifact", required=True,
                   help="the P2 exit artifact (p2-design.json)")
    p.add_argument("--presentation-dirs", default=DEFAULT_PRESENTATION_DIRS,
                   help="comma-separated dir prefixes counted as presentation "
                        f"(default: {DEFAULT_PRESENTATION_DIRS})")
    p.add_argument("--json", dest="json_out", default=None)
    p.add_argument("--report-only", action="store_true",
                   help="always exit 0; report without gating")
    a = p.parse_args()

    pres = [d.strip().lower().rstrip("/") for d in a.presentation_dirs.split(",")
            if d.strip()]
    res = check(a.artifact, pres)
    print(format_report(res))
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
    if res["verdict"] == "FAIL" and not a.report_only:
        sys.exit(2)


if __name__ == "__main__":
    main()
