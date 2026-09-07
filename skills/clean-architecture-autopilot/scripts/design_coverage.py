#!/usr/bin/env python3
"""design_coverage.py — prove the P2 exit artifact covers the design source.

Why this exists (run 3, option-seller-phase-a):
  The authoritative design was a 446-line markdown spec. Its "## 8. 前端约束"
  required reusing `StockSearchWidget`. The pipeline's contractual P2 exit
  artifact, p2-design.json, mentioned search / 搜索 / widget exactly zero times —
  §8 was dropped wholesale on the way into the artifact.

  G3 then audited that lossy copy and APPROVED it, because a gate cannot audit a
  constraint it cannot see. T8_frontend implemented from the lossy task spec. G5
  caught the omission only by reading the full source, and reported it as a MAJOR:
  68 of 108 watchlist symbols were unreachable from the UI. Cost: one corrective
  P4 round, 29 minutes, plus an unplanned T10 component.

  A set difference would have caught it in under a second. That is this script.

Two layers, because either alone is escapable:
  1. HARD — every section heading in the design source must be listed in the
     artifact's `sections_covered[]`. Catches "the designer never considered §8".
  2. SOFT — for each section, backticked identifiers that appear nowhere in the
     artifact are reported. Catches "§8 was declared covered but the widget it
     names still went missing" — run 3's exact shape. Waivable per identifier via
     the artifact's `identifiers_waived[]`, so skipping one is a recorded decision
     rather than silence.

Usage:
  python3 design_coverage.py --design docs/specs/design.md \
      --artifact .cc-skill/<slug>/artifacts/p2-design.json [--json out.json]
      [--report-only]

Exit codes: 0 = covered, 2 = uncovered sections or unreferenced identifiers.
"""
import argparse, json, os, re, sys

HEADING = re.compile(r"^(#{2,3})\s+(.*?)\s*$")
BACKTICKED = re.compile(r"`([A-Za-z_][A-Za-z0-9_./-]*)`")
LEADING_NUM = re.compile(r"^\s*(?:§\s*)?(\d+(?:\.\d+)*)")

# Backticked tokens that carry no design intent — matching on them produces noise
# rather than signal.
NOISE = {"true", "false", "null", "none", "int", "str", "bool", "float",
         "list", "dict", "json", "md", "py", "js", "css", "html"}


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _section_number(title):
    m = LEADING_NUM.match(title or "")
    return m.group(1) if m else None


def parse_design(path):
    """Return [{title, level, line, identifiers[]}] for each ## / ### heading."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:
        raise SystemExit(f"design_coverage: cannot read --design {path}: {e}")

    sections, current = [], None
    for i, line in enumerate(lines, start=1):
        m = HEADING.match(line)
        if m:
            current = {"title": m.group(2), "level": len(m.group(1)),
                       "line": i, "source": os.path.basename(path),
                       "identifiers": []}
            sections.append(current)
        elif current is not None:
            for tok in BACKTICKED.findall(line):
                if len(tok) < 3 or tok.lower() in NOISE:
                    continue
                if tok not in current["identifiers"]:
                    current["identifiers"].append(tok)
    return sections


def _artifact_mentions(blob, token):
    """A token counts as referenced if it appears verbatim, or (for a path) if its
    basename does — the artifact may name `option_analytics.py` where the design
    wrote `modules/option_analytics.py`."""
    if token in blob:
        return True
    base = token.rsplit("/", 1)[-1]
    return bool(base) and base != token and base in blob


def check(design_paths, artifact_path):
    try:
        with open(artifact_path, encoding="utf-8") as f:
            artifact = json.load(f)
    except OSError as e:
        raise SystemExit(f"design_coverage: cannot read --artifact "
                         f"{artifact_path}: {e}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"design_coverage: --artifact {artifact_path} is not "
                         f"valid JSON ({e})")

    blob = json.dumps(artifact, ensure_ascii=False)

    def _listify(key):
        v = artifact.get(key) or []
        return [v] if isinstance(v, str) else v

    covered_decl = _listify("sections_covered")
    oos_decl = _listify("sections_out_of_scope")
    waived = set(_listify("identifiers_waived"))

    def _matcher(decl):
        return ({_norm(d) for d in decl},
                {_section_number(d) for d in decl} - {None})

    cov_titles, cov_nums = _matcher(covered_decl)
    oos_titles, oos_nums = _matcher(oos_decl)

    sections, unaccounted, unreferenced = [], [], []
    for path in design_paths:
        for sec in parse_design(path):
            num = _section_number(sec["title"])
            title_n = _norm(sec["title"])
            is_cov = title_n in cov_titles or (num is not None and num in cov_nums)
            is_oos = title_n in oos_titles or (num is not None and num in oos_nums)
            where = f"{sec['source']}:{sec['line']} {sec['title']}"

            # The identifier check only applies to sections claimed as covered.
            # A section declared out of scope SHOULD be absent from the artifact —
            # flagging it would bury the real signal (run 3's design has a whole
            # "本期不改动的东西" section whose identifiers are meant to be missing).
            missing = []
            if is_cov:
                missing = [t for t in sec["identifiers"]
                           if t not in waived and not _artifact_mentions(blob, t)]

            sections.append({
                "source": sec["source"], "title": sec["title"], "line": sec["line"],
                "disposition": ("covered" if is_cov else
                                "out_of_scope" if is_oos else "unaccounted"),
                "identifiers": sec["identifiers"],
                "unreferenced_identifiers": missing})

            if not is_cov and not is_oos:
                unaccounted.append(where)
            if missing:
                unreferenced.append({"section": where, "identifiers": missing})

    return {
        "artifact": artifact_path,
        "design_sources": list(design_paths),
        "sections_in_design": len(sections),
        "sections_covered": sum(1 for s in sections
                                if s["disposition"] == "covered"),
        "sections_out_of_scope": sum(1 for s in sections
                                     if s["disposition"] == "out_of_scope"),
        "unaccounted_sections": unaccounted,
        "unreferenced_identifiers": unreferenced,
        "identifiers_waived": sorted(waived),
        "verdict": "PASS" if not unaccounted and not unreferenced else "FAIL",
        "sections": sections,
    }


def format_report(res):
    out = [f"design_coverage: {res['verdict']} — {res['sections_in_design']} "
           f"design sections: {res['sections_covered']} covered, "
           f"{res['sections_out_of_scope']} out of scope, "
           f"{len(res['unaccounted_sections'])} unaccounted"]
    if res["unaccounted_sections"]:
        out.append(f"\nUNACCOUNTED sections ({len(res['unaccounted_sections'])}) — "
                   f"in neither sections_covered[] nor sections_out_of_scope[]:")
        out += [f"  - {s}" for s in res["unaccounted_sections"]]
    if res["unreferenced_identifiers"]:
        out.append(f"\nUNREFERENCED identifiers "
                   f"({len(res['unreferenced_identifiers'])} covered section(s)) — "
                   f"named in the design, absent from the artifact:")
        for u in res["unreferenced_identifiers"]:
            out.append(f"  - {u['section']}")
            out.append(f"      {', '.join(u['identifiers'])}")
    if res["verdict"] == "FAIL":
        out.append("\nFix: account for every section (fold its constraints into "
                   "the artifact, or list it in sections_out_of_scope[]), and for "
                   "covered sections either carry each named identifier into the "
                   "artifact or record it in identifiers_waived[]. Do not exit P2 "
                   "with constraints the G3 gate cannot see.")
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser(prog="design_coverage.py")
    p.add_argument("--design", required=True,
                   help="authoritative design source(s), comma-separated markdown")
    p.add_argument("--artifact", required=True,
                   help="the P2 exit artifact (p2-design.json)")
    p.add_argument("--json", dest="json_out", default=None,
                   help="write the full result JSON here")
    p.add_argument("--report-only", action="store_true",
                   help="always exit 0; report without gating")
    a = p.parse_args()

    designs = [d.strip() for d in a.design.split(",") if d.strip()]
    res = check(designs, a.artifact)
    print(format_report(res))
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
    if res["verdict"] == "FAIL" and not a.report_only:
        sys.exit(2)


if __name__ == "__main__":
    main()
