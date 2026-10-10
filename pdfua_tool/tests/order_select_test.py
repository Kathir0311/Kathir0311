"""Reading order check & adjust in Edit table, many items at once in Edit
list (V4.21.4).

* every cell knows its printed place: a table read as printed has no cell
  "out of order"; a column moved makes the cells out of order, and "Order as
  printed" puts them back
* the cells read in an order clicked by hand (the rest follow), and the same
  refused on a table with a merged cell
* Edit list: several items nested, moved, un-nested and taken out in one
  change ("many") — one Undo; joining the selected items
* the built file is sound
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import compat, listedit, tableedit  # noqa: E402
from core.build import build  # noqa: E402
from core.options import profile_options  # noqa: E402
from core.pipeline import load_model, process_file  # noqa: E402
from tests.verify import mc_balance, structure_integrity  # noqa: E402


def _opts():
    o = profile_options("Default")
    o.ai_alt_text_auto = False
    o.run_verapdf = False
    o.use_samples = False
    return o


def table_part(bad: list) -> None:
    from tests.table_template_test import make
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "t.pdf")
    make(src)
    o = _opts()
    job = os.path.join(tmp, "job")
    process_file(src, job, o)
    compat.use(o)
    m = load_model(job)
    work = os.path.join(job, "working.pdf")
    t = next(n for n in m.ordered_nodes() if n.role == "Table")
    v = tableedit.grid_view(m, work, t.id)
    if len(v["printed_order"]) != len(v["cells"]):
        bad.append(f"printed order {len(v['printed_order'])} of {len(v['cells'])}")
    if v["order_off"]:
        bad.append(f"a table read as printed has cells out of order: {v['order_off']}")
    texts = {(c["r"], c["c"]): c["text"] for c in v["cells"]}
    tableedit.apply(m, work, t.id, {"op": "move_col", "index": 0, "dir": 1})
    v = tableedit.grid_view(m, work, t.id)
    if len(v["order_off"]) < 2 * v["rows"]:
        bad.append(f"column moved: only {len(v['order_off'])} cell(s) out of order")
    r = tableedit.apply(m, work, t.id, {"op": "cells_as_printed"})
    v = tableedit.grid_view(m, work, t.id)
    if v["order_off"] or {(c["r"], c["c"]): c["text"] for c in v["cells"]} != texts or not r["touched"]:
        bad.append(f"order as printed: {v['order_off']}")
    # by hand: the first row read right to left
    C = v["cols"]
    row0 = [c["id"] for c in sorted(v["cells"], key=lambda c: (c["r"], c["c"])) if c["r"] == 0]
    tableedit.apply(m, work, t.id, {"op": "reorder_cells", "order": list(reversed(row0))})
    v = tableedit.grid_view(m, work, t.id)
    pos = {c["id"]: (c["r"], c["c"]) for c in v["cells"]}
    if [pos[i] for i in reversed(row0)] != [(0, k) for k in range(C)]:
        bad.append(f"order by hand {[pos[i] for i in reversed(row0)]}")
    if len(v["order_off"]) != (C if C % 2 == 0 else C - 1):
        bad.append(f"order by hand: out of order {len(v['order_off'])}")
    after = {(c["r"], c["c"]): c["text"] for c in v["cells"] if c["r"] > 0}
    if after != {k: x for k, x in texts.items() if k[0] > 0}:
        bad.append("the rows not clicked moved")
    tableedit.apply(m, work, t.id, {"op": "cells_as_printed"})
    # a merged cell: refused
    ids = [c["id"] for c in sorted(v["cells"], key=lambda c: (c["r"], c["c"])) if c["r"] == 1][:2]
    tableedit.apply(m, work, t.id, {"op": "merge_cells", "ids": ids})
    try:
        tableedit.apply(m, work, t.id, {"op": "reorder_cells", "order": ids[:1]})
        bad.append("reordered a table with a merged cell")
    except tableedit.TableEditError:
        pass
    m.check_integrity()
    out = os.path.join(job, "out.pdf")
    build(m, work, out, o)
    if structure_integrity(out) or mc_balance(out):
        bad.append(f"table structure {structure_integrity(out)[:2]} {mc_balance(out)[:2]}")


def list_part(bad: list) -> None:
    from tests.list_edit_test import make
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "l.pdf")
    make(src)
    o = _opts()
    job = os.path.join(tmp, "job")
    process_file(src, job, o)
    compat.use(o)
    m = load_model(job)
    work = os.path.join(job, "working.pdf")
    L = next(n for n in m.ordered_nodes() if n.role == "L")
    v = listedit.view(m, work, L.id)
    lid = v["id"]
    listedit.apply(m, work, lid, {"op": "flatten"})
    v = listedit.view(m, work, lid)
    items = [it["id"] for it in v["items"]]
    if len(items) < 4:
        bad.append(f"list items {len(items)}")
        return
    sel = items[1:3]
    listedit.apply(m, work, lid, {"op": "many", "ops": [{"op": "indent", "id": i} for i in sel]})
    v = listedit.view(m, work, lid)
    lv = {it["id"]: it["level"] for it in v["items"]}
    if [lv[i] for i in sel] != [1, 1] or lv[items[0]] != 0:
        bad.append(f"nest two: {[lv[i] for i in items]}")
    if [it["id"] for it in v["items"]] != items:
        bad.append("nesting changed the order")
    listedit.apply(m, work, lid, {"op": "many", "ops": [{"op": "outdent", "id": i} for i in sel]})
    v = listedit.view(m, work, lid)
    if any(it["level"] for it in v["items"]) or [it["id"] for it in v["items"]] != items:
        bad.append(f"un-nest two: {[it['level'] for it in v['items']]}")
    # move two down: the lower one first
    listedit.apply(m, work, lid, {"op": "many", "ops": [{"op": "move", "id": i, "dir": 1} for i in reversed(sel)]})
    v = listedit.view(m, work, lid)
    now = [it["id"] for it in v["items"]]
    if now[2:4] != sel or now[1] != items[3]:
        bad.append(f"move two down: {now} from {items}")
    # join the two selected
    listedit.apply(m, work, lid, {"op": "many", "ops": [{"op": "merge_next", "id": sel[0]}]})
    v = listedit.view(m, work, lid)
    if len(v["items"]) != len(items) - 1 or sel[1] in [it["id"] for it in v["items"]]:
        bad.append("join selected")
    # take two out
    two = [it["id"] for it in v["items"]][-2:]
    r = listedit.apply(m, work, lid, {"op": "many", "ops": [{"op": "remove", "id": i} for i in two]})
    v = listedit.view(m, work, lid)
    if len(v["items"]) != len(items) - 3 or r.get("gone"):
        bad.append(f"take two out: {len(v['items'])}")
    m.check_integrity()
    out = os.path.join(job, "out.pdf")
    build(m, work, out, o)
    if structure_integrity(out) or mc_balance(out):
        bad.append(f"list structure {structure_integrity(out)[:2]} {mc_balance(out)[:2]}")


def main() -> int:
    bad: list = []
    table_part(bad)
    list_part(bad)
    if bad:
        print("order / select test: FAILED")
        for x in bad:
            print("   ", x)
        return 1
    print("order / select test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
