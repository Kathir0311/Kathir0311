"""Edit table and Edit list: the V4.21 options.

Table (the ruled table of table_template_test.py):
* Tidy: a missing cell is filled, an empty column added by hand goes
* row → header, column → header, row → data
* every empty cell read as given words; words pasted from a spreadsheet
* a summary: kept, and written as the table's /Summary in the built file

List (the numbered list of list_edit_test.py):
* an item split at its second line (no label) — "Join lines with no label" makes it one again
* one item per line; items at the labels again
* nest by labels: a) and b) go under 2.; flatten; numbering from the labels
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pikepdf  # noqa: E402

from core import compat, listedit, tableedit  # noqa: E402
from core.build import build  # noqa: E402
from core.options import profile_options  # noqa: E402
from core.pipeline import load_model, process_file  # noqa: E402
from tests.verify import mc_balance, structure_integrity  # noqa: E402


def job_for(make, name):
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, name)
    make(src)
    o = profile_options("Default")
    o.ai_alt_text_auto = False
    o.run_verapdf = False
    o.use_samples = False
    job = os.path.join(tmp, "job")
    process_file(src, job, o)
    compat.use(o)
    return job, o


def tables(bad):
    from tests.table_template_test import make
    job, o = job_for(make, "t.pdf")
    m = load_model(job)
    work = os.path.join(job, "working.pdf")
    t = next(n for n in m.ordered_nodes() if n.role == "Table")
    g = tableedit.Grid(m, t.id)
    R, C = g.R, g.C
    # a cell lost, and an empty column added by hand
    victim = next(cid for cid, v in g.cells.items() if v[0] == R - 1 and v[1] == C - 1)
    m.nodes[m.nodes[victim].parent].children.remove(victim)
    m.nodes.pop(victim)
    r1 = tableedit.apply(m, work, t.id, {"op": "tidy"})
    tableedit.apply(m, work, t.id, {"op": "insert_col", "at": C})
    r = tableedit.apply(m, work, t.id, {"op": "tidy"})
    r["filled"] = r1.get("filled")
    v = tableedit.grid_view(m, work, t.id)
    if r.get("filled") != 1 or r.get("cols_dropped") != 1 or (v["rows"], v["cols"]) != (R, C):
        bad.append(f"tidy {r} → {v['rows']}×{v['cols']} (was {R}×{C})")
    widths = {}
    for c in v["cells"]:
        widths[c["r"]] = widths.get(c["r"], 0) + c["cs"]
    if set(widths.values()) != {C}:
        bad.append(f"rows not all {C} wide: {widths}")
    tableedit.apply(m, work, t.id, {"op": "row_td", "index": 0})
    tableedit.apply(m, work, t.id, {"op": "row_th", "index": 0})
    tableedit.apply(m, work, t.id, {"op": "col_th", "index": 0})
    v = tableedit.grid_view(m, work, t.id)
    roles = {(c["r"], c["c"]): (c["role"], c["scope"]) for c in v["cells"]}
    if roles.get((0, 1)) != ("TH", "Column") or roles.get((2, 0)) != ("TH", "Row"):
        bad.append(f"header row/column {roles.get((0, 1))} {roles.get((2, 0))}")
    r = tableedit.apply(m, work, t.id, {"op": "empty_cells", "text": "No value"})
    filled = m.nodes[next(c["id"] for c in v["cells"] if (c["r"], c["c"]) == (R - 1, C - 1))]
    if r.get("cells", 0) < 1 or filled.actual_text != "No value":
        bad.append(f"empty cells {r} {filled.actual_text!r}")
    r = tableedit.apply(m, work, t.id, {"op": "set_texts", "r": 1, "c": 1, "rows": [["9.9", "8.8"], ["7.7"]]})
    v = tableedit.grid_view(m, work, t.id)
    tx = {(c["r"], c["c"]): c["text"] for c in v["cells"]}
    if (tx.get((1, 1)), tx.get((1, 2)), tx.get((2, 1))) != ("9.9", "8.8", "7.7") or len(r["touched"]) != 3:
        bad.append(f"paste {r} {tx.get((1, 1))} {tx.get((1, 2))} {tx.get((2, 1))}")
    tableedit.apply(m, work, t.id, {"op": "summary", "text": "Scores of four groups in three tests."})
    if tableedit.grid_view(m, work, t.id)["summary"] != "Scores of four groups in three tests.":
        bad.append("summary not kept")
    m.check_integrity()
    out = os.path.join(job, "out.pdf")
    build(m, work, out, o)
    with pikepdf.open(out) as pdf:
        found = False
        stack = [pdf.Root.StructTreeRoot.K]
        while stack:
            e = stack.pop()
            if isinstance(e, pikepdf.Array):
                stack.extend(list(e))
            elif isinstance(e, pikepdf.Dictionary) and "/S" in e:
                for a in ([e.A] if isinstance(e.get("/A"), pikepdf.Dictionary) else list(e.get("/A") or [])):
                    if isinstance(a, pikepdf.Dictionary) and str(a.get("/Summary", "")) == "Scores of four groups in three tests.":
                        found = True
                if "/K" in e:
                    stack.append(e.K)
    if not found:
        bad.append("no /Summary on the built table")
    if structure_integrity(out) or mc_balance(out):
        bad.append(f"table structure {structure_integrity(out)[:2]} {mc_balance(out)[:2]}")


def lists(bad):
    from tests.list_edit_test import make
    job, o = job_for(make, "l.pdf")
    m = load_model(job)
    work = os.path.join(job, "working.pdf")
    L = next(n for n in m.ordered_nodes() if n.role == "L" and not any(a.role == "L" for a in m.ancestors(n.id)))

    def items():
        return listedit.view(m, work, L.id)["items"]
    # flat first
    listedit.apply(m, work, L.id, {"op": "flatten"})
    its = items()
    n0 = len(its)
    if any(it["level"] for it in its):
        bad.append(f"flatten {[(i['lbl'], i['level']) for i in its]}")
    first = its[0]
    r0 = first["rects"][0]
    listedit.apply(m, work, L.id, {"op": "split", "id": first["id"], "page": r0[0], "y": (r0[2] + r0[4]) / 2})
    if len(items()) != n0 + 1:
        bad.append("split for the test")
    r = listedit.apply(m, work, L.id, {"op": "join_unlabelled"})
    its = items()
    if r.get("joined") != 1 or len(its) != n0 or "kept cold" not in its[0]["body"]:
        bad.append(f"join lines with no label {r} {[(i['lbl'], i['body'][:25]) for i in its]}")
    r = listedit.apply(m, work, L.id, {"op": "one_per_line"})
    if r.get("items") != n0 + 1:
        bad.append(f"one per line {r} (items were {n0})")
    r = listedit.apply(m, work, L.id, {"op": "by_labels"})
    its = items()
    if r.get("items") != n0 or [i["lbl"] for i in its][:2] != ["1.", "2."]:
        bad.append(f"items at labels {r} {[i['lbl'] for i in its]}")
    r = listedit.apply(m, work, L.id, {"op": "auto_nest"})
    lv = {i["lbl"]: i["level"] for i in items()}
    if r.get("levels") != 2 or lv.get("a)") != 1 or lv.get("b)") != 1 or lv.get("3.") != 0:
        bad.append(f"nest by labels {r} {lv}")
    listedit.apply(m, work, L.id, {"op": "numbering_auto"})
    subs = [d for d in m.descendants(L.id) if d.role == "L"]
    if L.list_numbering != "Decimal" or not subs or subs[0].list_numbering != "LowerAlpha":
        bad.append(f"numbering {L.list_numbering} {[s.list_numbering for s in subs]}")
    m.check_integrity()
    out = os.path.join(job, "out.pdf")
    build(m, work, out, o)
    if structure_integrity(out) or mc_balance(out):
        bad.append(f"list structure {structure_integrity(out)[:2]} {mc_balance(out)[:2]}")


def main() -> int:
    bad: list = []
    tables(bad)
    lists(bad)
    if bad:
        print("edit advanced test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("edit advanced test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
