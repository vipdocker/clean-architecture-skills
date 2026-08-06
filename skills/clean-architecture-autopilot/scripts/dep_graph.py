#!/usr/bin/env python3
"""dep_graph.py — G3 audit instrument: in-project import graph + SCC cycles.

Runs 1 and 2 each rewrote this tool from scratch inside the run folder
(~500 lines of Tarjan written twice per run). It is now bundled with the
skill, like cc_log.py, so G3 spends its effort judging the graph instead
of rebuilding the scanner. EXECUTABLE audit tool, read-only, never
production code.

Two miss-mechanisms this scanner exists to close (both let real defects
survive a full audit round):
  * inline imports — `from x import y` inside a function body is invisible
    to line-start grep; run 2's V2 (a 57-module SCC edge) hid exactly there.
    AST walking sees every import regardless of nesting, and inline ones
    are reported with line numbers.
  * repo-root single-file modules — composition roots like app.py often
    live at the root, outside any scanned package dir. Run 2's V6 survived
    an audit because the tool could not see the node. --root-files scans
    them explicitly.

Usage:
  # full graph + cycles, human summary to stdout
  python3 dep_graph.py --root <project_dir> --scan-dirs modules,scripts

  # focus on one component: its outward edges + whether it sits in any SCC
  python3 dep_graph.py --root <project_dir> --scan-dirs modules \
      --focus modules.screener --json out.json

  # include root-level single files (composition roots)
  python3 dep_graph.py --root <project_dir> --scan-dirs modules \
      --root-files app.py,main.py,web_server.py

Exit codes: 0 = scanned, no cycles touching --focus (or no --focus given and
no cycles at all); 1 = cycles found in scope; 2 = usage/IO error.
Layer judgement (which direction is "inward") stays with the auditor — this
tool reports edges and cycles, it does not know your layer map.
"""
from __future__ import annotations
import argparse
import ast
import json
import os
import sys
from collections import defaultdict


def module_name(path, root):
    rel = os.path.relpath(path, root)
    rel = rel[:-3] if rel.endswith(".py") else rel
    parts = rel.split(os.sep)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def collect_files(root, scan_dirs, root_files):
    files = []
    for d in scan_dirs:
        base = os.path.join(root, d)
        if not os.path.isdir(base):
            print(f"dep_graph: WARNING scan dir not found: {base}", file=sys.stderr)
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames
                           if x not in {"__pycache__", ".venv", "node_modules",
                                        ".git", ".worktrees"}]
            for fn in filenames:
                if fn.endswith(".py"):
                    files.append(os.path.join(dirpath, fn))
    for fn in root_files:
        p = os.path.join(root, fn)
        if os.path.isfile(p):
            files.append(p)
        else:
            print(f"dep_graph: WARNING root file not found: {p}", file=sys.stderr)
    return files


def resolve(imported, cur_mod, level, known):
    """Map an import target onto a known in-project module (else None)."""
    if level:  # relative import
        base = cur_mod.split(".")
        base = base[: len(base) - level] if level <= len(base) else []
        imported = ".".join([*base, imported]) if imported else ".".join(base)
    if imported in known:
        return imported
    # `from pkg.mod import attr` arrives as pkg.mod; `import pkg.mod.sub`
    # may only match a prefix — walk up until a known module is found.
    parts = imported.split(".")
    while parts:
        cand = ".".join(parts)
        if cand in known:
            return cand
        parts = parts[:-1]
    return None


def build_graph(root, files):
    known = {module_name(f, root): f for f in files}
    graph = defaultdict(set)
    inline = []      # (module, lineno, target) — imports below module level
    parse_errors = []
    for f in files:
        me = module_name(f, root)
        graph.setdefault(me, set())
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                tree = ast.parse(fh.read(), filename=f)
        except (SyntaxError, OSError) as e:
            parse_errors.append({"file": f, "error": str(e)})
            continue
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Import):
                targets = [(a.name, 0) for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                targets = [(node.module or "", node.level or 0)]
            for name, level in targets:
                tgt = resolve(name, me, level, known)
                if tgt and tgt != me:
                    graph[me].add(tgt)
                    if node.col_offset > 0:
                        inline.append({"module": me, "line": node.lineno,
                                       "imports": tgt})
    return graph, inline, parse_errors


def tarjan_scc(graph):
    """Iterative Tarjan (recursion-free: real graphs exceed the stack limit)."""
    index_of, low, on_stack = {}, {}, set()
    stack, sccs, counter = [], [], [0]
    for start in graph:
        if start in index_of:
            continue
        work = [(start, iter(sorted(graph[start])))]
        index_of[start] = low[start] = counter[0]; counter[0] += 1
        stack.append(start); on_stack.add(start)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in graph:
                    continue
                if nxt not in index_of:
                    index_of[nxt] = low[nxt] = counter[0]; counter[0] += 1
                    stack.append(nxt); on_stack.add(nxt)
                    work.append((nxt, iter(sorted(graph[nxt]))))
                    advanced = True
                    break
                elif nxt in on_stack:
                    low[node] = min(low[node], index_of[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index_of[node]:
                comp = []
                while True:
                    w = stack.pop(); on_stack.discard(w); comp.append(w)
                    if w == node:
                        break
                if len(comp) > 1:
                    sccs.append(sorted(comp))
    return sorted(sccs, key=len, reverse=True)


def main():
    p = argparse.ArgumentParser(prog="dep_graph.py")
    p.add_argument("--root", required=True, help="project root directory")
    p.add_argument("--scan-dirs", default="modules",
                   help="comma-separated package dirs under root")
    p.add_argument("--root-files", default="",
                   help="comma-separated root-level .py files (composition roots)")
    p.add_argument("--focus", default=None,
                   help="module-name prefix to report edges/cycles for")
    p.add_argument("--json", dest="json_out", default=None,
                   help="write full result JSON to this path")
    a = p.parse_args()

    root = os.path.abspath(a.root)
    if not os.path.isdir(root):
        print(f"dep_graph: ERROR --root is not a directory: {root}", file=sys.stderr)
        sys.exit(2)
    scan_dirs = [d for d in a.scan_dirs.split(",") if d]
    root_files = [f for f in a.root_files.split(",") if f]

    files = collect_files(root, scan_dirs, root_files)
    graph, inline, parse_errors = build_graph(root, files)
    sccs = tarjan_scc(graph)

    focus = a.focus
    focus_out, focus_sccs = [], []
    if focus:
        for src in sorted(graph):
            if src.startswith(focus):
                for dst in sorted(graph[src]):
                    if not dst.startswith(focus):
                        focus_out.append({"from": src, "to": dst})
        focus_sccs = [c for c in sccs if any(m.startswith(focus) for m in c)]

    result = {
        "root": root, "modules": len(graph),
        "edges": sum(len(v) for v in graph.values()),
        "inline_imports": inline, "parse_errors": parse_errors,
        "sccs_gt1": [{"size": len(c), "members": c} for c in sccs],
        "focus": focus, "focus_outward_edges": focus_out,
        "focus_sccs": [{"size": len(c), "members": c} for c in focus_sccs],
    }
    if a.json_out:
        try:
            with open(a.json_out, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except OSError as e:
            print(f"dep_graph: ERROR cannot write {a.json_out}: {e}", file=sys.stderr)
            sys.exit(2)

    print(f"modules={result['modules']} edges={result['edges']} "
          f"sccs(>1)={len(sccs)} inline_imports={len(inline)} "
          f"parse_errors={len(parse_errors)}")
    for c in sccs[:5]:
        print(f"  SCC size={len(c)}: {', '.join(c[:6])}{' ...' if len(c) > 6 else ''}")
    if focus:
        print(f"focus '{focus}': outward_edges={len(focus_out)} "
              f"in_scc={'YES' if focus_sccs else 'no'}")
        for e in focus_out[:20]:
            print(f"  OUT {e['from']} -> {e['to']}")
    bad = focus_sccs if focus else sccs
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
