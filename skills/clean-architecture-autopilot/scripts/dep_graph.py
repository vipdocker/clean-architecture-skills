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

  # include P2 component ownership and component-level cycles
  python3 dep_graph.py --root <project_dir> --scan-dirs modules \
      --component-map artifacts/p2-design.json --json graph.json

Exit codes: 0 = scanned, no cycles touching --focus (or no --focus given and
no cycles at all); 1 = cycles found in scope or an invalid component map;
2 = usage/IO error.
Layer judgement (which direction is "inward") stays with the auditor — this
tool reports edges and cycles, it does not know your layer map.
"""
from __future__ import annotations
import argparse
import ast
import json
import keyword
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
        index_of[start] = low[start] = counter[0]
        counter[0] += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in graph:
                    continue
                if nxt not in index_of:
                    index_of[nxt] = low[nxt] = counter[0]
                    counter[0] += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
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
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                if len(comp) > 1:
                    sccs.append(sorted(comp))
    return sorted(sccs, key=len, reverse=True)


_COMPONENT_KINDS = {"domain", "adapter", "shared_adapter", "framework"}


def is_canonical_dotted_identifier(value):
    """Return whether ``value`` is an unambiguous Python-style dotted name."""
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and not value.endswith(".py")
        and all(part.isidentifier() and not keyword.iskeyword(part) for part in value.split("."))
    )


def load_component_ownership(path):
    """Read P2 ``component_map.ownership`` as module-to-component records.

    Returns ``(mapping, errors)``. A missing ownership list is compatible with
    pre-component-map P2 artifacts and returns an empty mapping. Invalid records
    are reported instead of being repaired or inferred.
    """
    with open(path, encoding="utf-8") as fh:
        artifact = json.load(fh)

    errors = []
    if not isinstance(artifact, dict):
        return {}, [{"message": "P2 artifact must be a JSON object"}]
    component_map = artifact.get("component_map", {})
    if not isinstance(component_map, dict):
        return {}, [{"message": "component_map must be a JSON object"}]
    records = component_map.get("ownership", [])
    if records is None:
        records = []
    if not isinstance(records, list):
        return {}, [{"message": "component_map.ownership must be a list"}]

    mapping = {}
    expected_keys = {"module", "component", "kind"}
    for index, record in enumerate(records):
        prefix = f"ownership record {index}"
        if not isinstance(record, dict) or set(record) != expected_keys:
            errors.append({
                "message": f"{prefix} must contain exactly module, component, and kind",
            })
            continue
        module = record["module"]
        component = record["component"]
        kind = record["kind"]
        if not isinstance(module, str) or not module.strip():
            errors.append({"message": f"{prefix} module must be a non-empty string"})
            continue
        if not is_canonical_dotted_identifier(module):
            errors.append({
                "message": f"{prefix} module must be a canonical dotted identifier: {module!r}",
            })
            continue
        if not isinstance(component, str) or not component.strip():
            errors.append({"message": f"{prefix} component must be a non-empty string"})
            continue
        if not is_canonical_dotted_identifier(component):
            errors.append({
                "message": f"{prefix} component must be a canonical dotted identifier: {component!r}",
            })
            continue
        if kind not in _COMPONENT_KINDS:
            errors.append({
                "message": f"{prefix} for module {module!r} has invalid kind: {kind!r}",
            })
            continue
        if module in mapping:
            errors.append({
                "message": f"{prefix} has duplicate module ownership: {module!r}",
            })
            continue
        mapping[module] = {"component": component, "kind": kind}
    return mapping, errors


def component_graph(module_graph, ownership):
    """Project module dependencies into explicit component dependencies.

    ``ownership`` is normally the ``(mapping, errors)`` tuple from
    :func:`load_component_ownership`; a mapping or an empty list is also
    accepted for direct callers. ``shared_adapter`` records remain ordinary
    named components and receive no implicit owner.
    """
    ownership_errors = []
    if isinstance(ownership, tuple) and len(ownership) == 2:
        module_ownership, ownership_errors = ownership
    elif isinstance(ownership, dict):
        module_ownership = ownership
    elif isinstance(ownership, list) and not ownership:
        module_ownership = {}
    else:
        module_ownership = {}
        ownership_errors.append({
            "message": "ownership must be a mapping or (mapping, errors)"
        })

    raw_ownership_errors = (
        ownership_errors if isinstance(ownership_errors, list) else [ownership_errors]
    )
    ownership_errors = []
    for error in raw_ownership_errors:
        message = error.get("message") if isinstance(error, dict) else None
        ownership_errors.append(
            {"message": message}
            if isinstance(message, str) and message.strip()
            else {"message": f"invalid ownership error record: {error!r}"}
        )

    if not isinstance(module_ownership, dict):
        ownership_errors.append({"message": "ownership mapping must be a dictionary"})
        module_ownership = {}

    components_by_module = {}
    for module, record in module_ownership.items():
        if isinstance(record, dict) and isinstance(record.get("component"), str):
            components_by_module[module] = record["component"]
        else:
            ownership_errors.append({
                "message": f"ownership mapping record for {module!r} must include a component string",
            })

    modules = set(module_graph)
    modules.update(target for targets in module_graph.values() for target in targets)
    for module in sorted(components_by_module):
        if module not in modules:
            ownership_errors.append({
                "message": f"ownership module {module!r} is not in the scanned module graph",
            })
    unowned_modules = sorted(module for module in modules if module not in components_by_module)

    graph = {component: set() for component in components_by_module.values()}
    edges = set()
    for source, targets in module_graph.items():
        source_component = components_by_module.get(source)
        if source_component is None:
            continue
        for target in targets:
            target_component = components_by_module.get(target)
            if target_component is None or target_component == source_component:
                continue
            graph[source_component].add(target_component)
            edges.add((source_component, target_component))

    component_sccs = tarjan_scc(graph)
    return {
        "component_edges": [
            {"from": source, "to": target} for source, target in sorted(edges)
        ],
        "component_sccs": component_sccs,
        "unowned_modules": unowned_modules,
        "ownership_errors": ownership_errors,
    }


def main():
    p = argparse.ArgumentParser(prog="dep_graph.py")
    p.add_argument("--root", required=True, help="project root directory")
    p.add_argument("--scan-dirs", default="modules",
                   help="comma-separated package dirs under root")
    p.add_argument("--root-files", default="",
                   help="comma-separated root-level .py files (composition roots)")
    p.add_argument("--focus", default=None,
                   help="module-name prefix to report edges/cycles for")
    p.add_argument("--component-map", default=None,
                   help="P2 artifact containing component_map.ownership")
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
    if a.component_map is not None:
        try:
            ownership = load_component_ownership(a.component_map)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            print(
                f"dep_graph: ERROR cannot read --component-map {a.component_map}: {e}",
                file=sys.stderr,
            )
            sys.exit(2)
        result["component_graph"] = component_graph(graph, ownership)
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
    if "component_graph" in result:
        component_result = result["component_graph"]
        print(
            f"component_edges={len(component_result['component_edges'])} "
            f"component_sccs(>1)={len(component_result['component_sccs'])} "
            f"unowned_modules={len(component_result['unowned_modules'])} "
            f"ownership_errors={len(component_result['ownership_errors'])}"
        )
    for c in sccs[:5]:
        print(f"  SCC size={len(c)}: {', '.join(c[:6])}{' ...' if len(c) > 6 else ''}")
    if focus:
        print(f"focus '{focus}': outward_edges={len(focus_out)} "
              f"in_scc={'YES' if focus_sccs else 'no'}")
        for e in focus_out[:20]:
            print(f"  OUT {e['from']} -> {e['to']}")
    bad = focus_sccs if focus else sccs
    component_bad = False
    if "component_graph" in result:
        component_result = result["component_graph"]
        component_bad = any(
            component_result[key]
            for key in ("ownership_errors", "unowned_modules", "component_sccs")
        )
    sys.exit(1 if bad or component_bad else 0)


if __name__ == "__main__":
    main()
