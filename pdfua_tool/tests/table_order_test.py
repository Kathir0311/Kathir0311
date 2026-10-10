"""Edit table: the reading order (V4.21.3).

* every cell has its reading number (row by row, left to right)
* a column moves left / right — its cells are read in the new place
* a cell holding several tags: their order set by hand, then by position
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import compat, tableedit  # noqa: E402
from core.build import build  # noqa: E402
from core.model import StructNode  # noqa: E402
from core.options import profile_options  # noqa: E402
from core.pipeline import load_model, process_file  # noqa: E402
from tests.verify import mc_balance, structure_integrity  # noqa: E402


def main() -> int:
    from tests.table_template_test import make
    bad = []
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "t.pdf")
    make(src)
    o = profile_options("Default")
    o.ai_alt_text_auto = False
    o.run_verapdf = False
    o.use_samples = False
    job = os.path.join(tmp, "job")
    process_file(src, job, o)
    compat.use(o)
    m = load_model(job)
    work = os.path.join(job, "working.pdf")
    t = next(n for n in m.ordered_nodes() if n.role == "Table")
    v = tableedit.grid_view(m, work, t.id)
    orders = [c["order"] for c in sorted(v["cells"], key=lambda c: (c["r"], c["c"]))]
    if orders != list(range(1, len(orders) + 1)):
        bad.append(f"reading numbers {orders}")
    before = {(c["r"], c["c"]): c["text"] for c in v["cells"]}
    tableedit.apply(m, work, t.id, {"op": "move_col", "index": 0, "dir": 1})
    v = tableedit.grid_view(m, work, t.id)
    after = {(c["r"], c["c"]): c["text"] for c in v["cells"]}
    if after.get((0, 1)) != before.get((0, 0)) or after.get((0, 0)) != before.get((0, 1)):
        bad.append(f"move column: {before.get((0, 0))}|{before.get((0, 1))} → {after.get((0, 0))}|{after.get((0, 1))}")
    tableedit.apply(m, work, t.id, {"op": "move_col", "index": 1, "dir": -1})
    try:
        tableedit.apply(m, work, t.id, {"op": "move_col", "index": 0, "dir": -1})
        bad.append("moved a column off the table")
    except tableedit.TableEditError:
        pass
    # a cell with two tags, printed top then bottom, held in the wrong order
    cell = next(c for c in v["cells"] if c["r"] == 1 and c["c"] == 1)
    n = m.nodes[cell["id"]]
    x0, y0, x1, y1 = n.bbox
    a = StructNode(id=m.new_id("p"), role="P", page=n.page, bbox=(x0, (y0 + y1) / 2, x1, y1), text="top")
    b = StructNode(id=m.new_id("p"), role="P", page=n.page, bbox=(x0, y0, x1, (y0 + y1) / 2), text="bottom")
    m.add(b, n.id)
    m.add(a, n.id)
    v = tableedit.grid_view(m, work, t.id)
    kl = next(c for c in v["cells"] if c["id"] == n.id)["kid_list"]
    if [k["text"] for k in kl] != ["bottom", "top"]:
        bad.append(f"kid list {kl}")
    tableedit.apply(m, work, t.id, {"op": "cell_order", "id": n.id, "order": [a.id, b.id]})
    if n.children != [a.id, b.id]:
        bad.append("order by hand")
    try:
        tableedit.apply(m, work, t.id, {"op": "cell_order", "id": n.id, "order": [a.id]})
        bad.append("an order missing a tag was taken")
    except tableedit.TableEditError:
        pass
    n.children = [b.id, a.id]
    r = tableedit.apply(m, work, t.id, {"op": "cell_order_auto"})
    if n.children != [a.id, b.id] or n.id not in r["touched"]:
        bad.append(f"order by position {r} {[m.nodes[k].text for k in n.children]}")
    m.check_integrity()
    out = os.path.join(job, "out.pdf")
    build(m, work, out, o)
    if structure_integrity(out) or mc_balance(out):
        bad.append(f"structure {structure_integrity(out)[:2]} {mc_balance(out)[:2]}")
    if bad:
        print("table order test: FAILED")
        for x in bad:
            print("   ", x)
        return 1
    print("table order test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
