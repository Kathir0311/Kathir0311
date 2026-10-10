"""Edit table (V4.18): rows and columns changed by hand.

A ruled table, 4 rows × 3 columns with a header row; one row prints two
lines of words in each cell ("Bob / Robert"). In the Default (V4) engine and
the Kriyadocs house style:

* the grid is read (4 × 3, the words in the right cells)
* that row is split at the line between its two lines of words: 5 rows, each
  half with its own words; and the two merged back again
* a column inserted and deleted again; a column with words cannot be deleted
* two by two cells merged into one (rowspan / colspan 2) and split again
* a column line moved on the page: the cells on both sides read their words
  again
* a row made an artifact (out of the table, not read); TH / TD set by hand
* the built file has a regular table and no checker error
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymupdf  # noqa: E402

from core import compat, tableedit  # noqa: E402
from core.build import build  # noqa: E402
from core.checker import check  # noqa: E402
from core.lint import table_grid  # noqa: E402
from core.options import profile_options  # noqa: E402
from core.pipeline import load_model, process_file, save_model  # noqa: E402
from tests.verify import mc_balance, structure_integrity  # noqa: E402

ROWS = [["Name", "Age", "City"], ["Ann", "34", "Leeds"], [("Bob", "Robert"), ("41", "40"), ("York", "Hull")],
        ["Cy", "29", "Bath"]]
X0, Y0, W, H = 72, 140, 150, 34


def make(path: str) -> None:
    d = pymupdf.open()
    p = d.new_page()
    p.insert_text((72, 80), "Staff list", fontsize=20)
    p.insert_text((72, 110), "The table below lists the staff of the office.", fontsize=11)
    y = Y0
    for r, row in enumerate(ROWS):
        h = H * (2 if isinstance(row[0], tuple) else 1)
        for c, t in enumerate(row):
            rect = pymupdf.Rect(X0 + c * W, y, X0 + (c + 1) * W, y + h)
            p.draw_rect(rect, color=(0, 0, 0), width=0.7)
            font = "hebo" if r == 0 else "helv"
            if isinstance(t, tuple):
                p.insert_text((rect.x0 + 8, rect.y0 + 22), t[0], fontsize=12, fontname=font)
                p.insert_text((rect.x0 + 8, rect.y0 + 22 + H), t[1], fontsize=12, fontname=font)
            else:
                p.insert_text((rect.x0 + 8, rect.y0 + 22), t, fontsize=12, fontname=font)
        y += h
    p.insert_text((72, y + 40), "Everyone works on the first floor.", fontsize=11)
    d.save(path)
    d.close()


def texts(view) -> dict:
    return {(c["r"], c["c"]): c["text"] for c in view["cells"]}


def run(profile: str, bad: list) -> None:
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "t.pdf")
    make(src)
    o = profile_options(profile)
    o.ai_alt_text_auto = False
    o.run_verapdf = False
    job = os.path.join(tmp, "job")
    process_file(src, job, o)
    compat.use(o)
    m = load_model(job)
    work = os.path.join(job, "working.pdf")
    tables = [n for n in m.nodes.values() if n.role == "Table"]
    if len(tables) != 1:
        bad.append(f"{profile}: {len(tables)} tables found")
        return
    tid = tables[0].id
    v = tableedit.grid_view(m, work, tid)
    if (v["rows"], v["cols"]) == (5, 3) and profile == "Kriyadocs":
        # the house style reads each printed line of a cell as a row: the two
        # are merged by hand first
        tableedit.apply(m, work, tid, {"op": "merge_rows", "index": 2})
        v = tableedit.grid_view(m, work, tid)
    if (v["rows"], v["cols"]) != (4, 3):
        bad.append(f"{profile}: grid {v['rows']}x{v['cols']} {texts(v)}")
        return
    t = texts(v)
    if "Bob" not in t[(2, 0)] or "Robert" not in t[(2, 0)]:
        bad.append(f"{profile}: the two-line cell reads {t[(2, 0)]!r}")

    # split the two-line row between its lines
    line_y = Y0 + 2 * H + H          # display y between "Bob" and "Robert"
    tableedit.apply(m, work, tid, {"op": "split_row", "index": 2, "page": 0, "pos": line_y})
    v = tableedit.grid_view(m, work, tid)
    t = texts(v)
    if (v["rows"], v["cols"]) != (5, 3) or t.get((2, 0)) != "Bob" or t.get((3, 0)) != "Robert" \
            or t.get((3, 2)) != "Hull":
        bad.append(f"{profile}: after split row {v['rows']}x{v['cols']} {t}")
    tableedit.apply(m, work, tid, {"op": "merge_rows", "index": 2})
    v = tableedit.grid_view(m, work, tid)
    t = texts(v)
    if (v["rows"], v["cols"]) != (4, 3) or "Robert" not in t.get((2, 0), "") or t.get((3, 0)) != "Cy":
        bad.append(f"{profile}: after merge rows {v['rows']}x{v['cols']} {t}")

    # a column in and out again
    tableedit.apply(m, work, tid, {"op": "insert_col", "at": 1})
    v = tableedit.grid_view(m, work, tid)
    t = texts(v)
    if v["cols"] != 4 or t.get((1, 1)) != "" or t.get((1, 2)) != "34":
        bad.append(f"{profile}: after insert column {v['cols']} {t}")
    tableedit.apply(m, work, tid, {"op": "delete_col", "index": 1})
    try:
        tableedit.apply(m, work, tid, {"op": "delete_col", "index": 1})
        bad.append(f"{profile}: a column with words was deleted")
    except tableedit.TableEditError:
        pass
    v = tableedit.grid_view(m, work, tid)
    if v["cols"] != 3 or texts(v).get((1, 1)) != "34":
        bad.append(f"{profile}: after delete column {v['cols']} {texts(v)}")

    # merge 2×2 and split again
    ids = [c["id"] for c in v["cells"] if c["r"] in (1, 2) and c["c"] in (1, 2)]
    tableedit.apply(m, work, tid, {"op": "merge_cells", "ids": ids})
    v = tableedit.grid_view(m, work, tid)
    big = [c for c in v["cells"] if c["rs"] == 2 and c["cs"] == 2]
    occ, widths = table_grid(m, tid)
    if len(big) != 1 or len(set(widths)) != 1 or len(v["cells"]) != 9:
        bad.append(f"{profile}: merge cells {[(c['r'], c['c'], c['rs'], c['cs']) for c in v['cells']]}")
    else:
        tableedit.apply(m, work, tid, {"op": "split_cell", "id": big[0]["id"]})
        v = tableedit.grid_view(m, work, tid)
        t = texts(v)
        if len(v["cells"]) != 12 or t.get((1, 1)) != "34" or t.get((1, 2)) != "Leeds" or "York" not in t.get((2, 2), ""):
            bad.append(f"{profile}: split cell {t}")

    # move the line between columns 1 and 2 left, into "Age": the words of
    # column 1 now in column 2 as well
    tableedit.apply(m, work, tid, {"op": "move_line", "kind": "col", "index": 2, "page": 0, "pos": X0 + W + 6})
    v = tableedit.grid_view(m, work, tid)
    t = texts(v)
    if t.get((1, 1)) != "" or "34" not in t.get((1, 2), ""):
        bad.append(f"{profile}: after moving a line {t}")
    tableedit.apply(m, work, tid, {"op": "move_line", "kind": "col", "index": 2, "page": 0, "pos": X0 + 2 * W})
    v = tableedit.grid_view(m, work, tid)
    if texts(v).get((1, 1)) != "34":
        bad.append(f"{profile}: the line moved back {texts(v)}")

    # a row with words cannot be deleted; as an artifact it leaves the table
    try:
        tableedit.apply(m, work, tid, {"op": "delete_row", "index": 3})
        bad.append(f"{profile}: a row with words was deleted")
    except tableedit.TableEditError:
        pass
    tableedit.apply(m, work, tid, {"op": "delete_row", "index": 3, "artifact": True})
    v = tableedit.grid_view(m, work, tid)
    if v["rows"] != 3 or not any(n.artifact and n.role == "Div" for n in m.nodes.values()):
        bad.append(f"{profile}: a row made an artifact {v['rows']}")
    # an inserted row and TH set by hand
    tableedit.apply(m, work, tid, {"op": "insert_row", "at": 3})
    v = tableedit.grid_view(m, work, tid)
    if v["rows"] != 4:
        bad.append(f"{profile}: insert row {v['rows']}")
    first_col = [c["id"] for c in v["cells"] if c["c"] == 0 and c["r"] > 0]
    tableedit.apply(m, work, tid, {"op": "set_role", "ids": first_col, "role": "TH", "scope": "Row"})
    if any(m.nodes[i].role != "TH" or m.nodes[i].scope != "Row" for i in first_col):
        bad.append(f"{profile}: TH by hand")

    m.check_integrity()
    save_model(job, m)
    out = os.path.join(job, "out.pdf")
    build(m, work, out, o)
    errs = [f"{f.code} {f.message}" for f in check(out) if f.severity == "error"]
    if errs:
        bad.append(f"{profile}: checker errors {errs}")
    if structure_integrity(out) or mc_balance(out):
        bad.append(f"{profile}: structure {structure_integrity(out)[:2]} {mc_balance(out)[:2]}")
    compat.use("")


def main() -> int:
    bad: list = []
    for prof in ("Default", "Kriyadocs"):
        run(prof, bad)
    if bad:
        print("table edit test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("table edit test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
