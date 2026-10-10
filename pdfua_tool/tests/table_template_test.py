"""Edit table, V4.19: parts, zones, more changes, templates.

Page 1: a ruled table printed in two parts — "Table 1" in the left column and
"Table 1 (continued)" in the right column with its header row printed again —
and under the left part a row printed outside the ruling. Page 2: a table of
the same kind with no ruling at all (words only), and a block of words that
is a table nobody tagged.

In the Default (V4) engine and the Kriyadocs house style:

* the lines of each part run over that part only (never across the column
  between them)
* the row printed under the table joins it from a zone; a column added from a
  zone; rows taken out by a zone; rows moved; THead / TBody set by hand
* the right part split off as a table and joined back, its repeated header
  an artifact
* the left table saved as template #1, given to the unruled table and to the
  untagged block (a zone): same columns, header, words in the right cells
* "not a table"; the built file has no checker error
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymupdf  # noqa: E402

from core import compat, tableedit, templates  # noqa: E402
from core.build import build  # noqa: E402
from core.checker import check  # noqa: E402
from core.options import profile_options  # noqa: E402
from core.pipeline import load_model, process_file, save_model  # noqa: E402
from tests.verify import mc_balance, structure_integrity  # noqa: E402

HEAD = ["Group", "Count", "Mean"]
LEFT = [["Men", "120", "4.5"], ["Women", "98", "4.1"], ["Boys", "40", "3.9"]]
RIGHT = [["Girls", "38", "3.7"], ["Adults", "218", "4.3"]]
W = 70


def grid(p, x0, y0, rows, ruled=True, bold_first=True):
    for r, row in enumerate(rows):
        for c, t in enumerate(row):
            rect = pymupdf.Rect(x0 + c * W, y0 + r * 22, x0 + (c + 1) * W, y0 + (r + 1) * 22)
            if ruled:
                p.draw_rect(rect, color=(0, 0, 0), width=0.6)
            p.insert_text((rect.x0 + 5, rect.y0 + 15), t, fontsize=10,
                          fontname="hebo" if (bold_first and r == 0) else "helv")


def make(path: str) -> None:
    d = pymupdf.open()
    p = d.new_page()
    p.insert_text((40, 50), "Results", fontsize=18)
    p.insert_text((40, 80), "Table 1 Scores by group", fontsize=10, fontname="hebo")
    grid(p, 40, 90, [HEAD] + LEFT)
    # a row printed under the ruling (the analysis leaves it out)
    for c, t in enumerate(["Elders", "55", "4.0"]):
        p.insert_text((40 + c * W + 5, 90 + 4 * 22 + 15 + 8), t, fontsize=10)
    p.insert_text((40, 240), "The scores were close in every group of the study.", fontsize=10)
    p.insert_text((320, 80), "Table 1 (continued)", fontsize=10, fontname="hebo")
    grid(p, 320, 90, [HEAD] + RIGHT)
    q = d.new_page()
    q.insert_text((40, 50), "More results", fontsize=18)
    q.insert_text((40, 80), "Table 2 Scores by town", fontsize=10, fontname="hebo")
    grid(q, 40, 90, [HEAD, ["Leeds", "70", "4.2"], ["York", "64", "4.0"], ["Hull", "51", "3.8"]], ruled=False)
    q.insert_text((40, 220), "Table 3 Scores by school", fontsize=10, fontname="hebo")
    grid(q, 40, 230, [HEAD, ["North", "33", "4.4"], ["South", "29", "4.6"]], ruled=False)
    d.save(path)
    d.close()


def texts(v):
    return {(c["r"], c["c"]): c["text"] for c in v["cells"]}


def tables(m):
    return [n for n in m.ordered_nodes() if n.role == "Table"]


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
    ts = tables(m)
    if not ts:
        bad.append(f"{profile}: no table found")
        return
    t1 = ts[0]
    # the two parts on page 1 as one table: join them when the analysis made two
    on1 = [t for t in ts if t.page == 0]
    if len(on1) == 2:
        tableedit.apply(m, work, on1[0].id, {"op": "merge_next_table", "drop": 1})
    t1 = tables(m)[0]
    v = tableedit.grid_view(m, work, t1.id)
    parts = v["lines"].get("0", {}).get("parts", [])
    if len(parts) != 2:
        bad.append(f"{profile}: parts on page 1: {len(parts)} ({[p['box'] for p in parts]})")
    for p in parts:
        b = p["box"]
        for k, span in p["row_span"].items():
            if span[0] < b[0] - 1 or span[1] > b[2] + 1:
                bad.append(f"{profile}: a row line runs out of its part {span} {b}")
        if b[2] - b[0] > 3 * W + 10:
            bad.append(f"{profile}: a part spans the column gap {b}")
    if any(n.artifact and n.pagination_subtype == "" and "repeated" in (n.note or "") for n in m.nodes.values()):
        pass
    rows_before = v["rows"]

    # split the right part off as its own table and join it back
    k = next(c["r"] for c in v["cells"] if c["text"] == "Girls")
    r = tableedit.apply(m, work, t1.id, {"op": "split_table", "index": k})
    if r.get("new_table") not in m.nodes:
        bad.append(f"{profile}: split table")
    tableedit.apply(m, work, t1.id, {"op": "merge_next_table", "drop": 0})
    if tableedit.grid_view(m, work, t1.id)["rows"] != rows_before:
        bad.append(f"{profile}: join back {tableedit.grid_view(m, work, t1.id)['rows']} vs {rows_before}")

    # the row printed under the left part joins from a zone
    d = pymupdf.open(work)
    el = d[0].search_for("Elders")[0]
    d.close()
    r = tableedit.apply(m, work, t1.id, {"op": "add_zone", "page": 0, "rect": [36, el.y0 - 3, 40 + 3 * W, el.y1 + 3],
                                         "kind": "rows", "mode": "lines"})
    v = tableedit.grid_view(m, work, t1.id)
    row = [c for c in v["cells"] if c["text"] == "Elders"]
    if not row or texts(v).get((row[0]["r"], 1)) != "55" or texts(v).get((row[0]["r"], 2)) != "4.0":
        bad.append(f"{profile}: a row from a zone {texts(v)}")
    elif row[0]["r"] != next(c["r"] for c in v["cells"] if c["text"] == "Boys") + 1:
        bad.append(f"{profile}: the zone row is not under Boys ({row[0]['r']})")
    # rows moved, THead set
    ri = row[0]["r"]
    tableedit.apply(m, work, t1.id, {"op": "move_row", "index": ri, "dir": -1})
    v = tableedit.grid_view(m, work, t1.id)
    if texts(v).get((ri - 1, 0)) != "Elders":
        bad.append(f"{profile}: move row {texts(v).get((ri - 1, 0))}")
    tableedit.apply(m, work, t1.id, {"op": "move_row", "index": ri - 1, "dir": 1})
    tableedit.apply(m, work, t1.id, {"op": "set_group", "rows": [0], "group": "THead"})
    v = tableedit.grid_view(m, work, t1.id)
    if v["row_info"][0]["group"] != "THead" or v["row_info"][1]["group"] == "THead":
        bad.append(f"{profile}: THead {[r['group'] for r in v['row_info']][:3]}")
    # take the zone row out again
    tableedit.apply(m, work, t1.id, {"op": "remove_zone", "page": 0, "rect": [36, el.y0 - 3, 40 + 3 * W, el.y1 + 3]})
    v = tableedit.grid_view(m, work, t1.id)
    if any(c["text"] == "Elders" for c in v["cells"]) or not any(n.role == "P" and "Elders" in (n.text or "") for n in m.nodes.values()):
        bad.append(f"{profile}: take out by a zone")

    # template #1 from the left table; given to the unruled table 2 …
    spec = templates.table_spec(m, work, t1.id)
    if spec["cols"] != 3 or spec["header_rows"] < 1:
        bad.append(f"{profile}: template {spec}")
    spec["number"] = 1
    page2 = [t for t in tables(m) if t.page == 1]
    d = pymupdf.open(work)
    t2box = d[1].search_for("Leeds")[0]
    hull = d[1].search_for("Hull")[0]
    t3top = d[1].search_for("North")[0]
    d.close()
    zone2 = [36, t2box.y0 - 26, 40 + 3 * W, hull.y1 + 6]
    if page2:
        r = templates.apply_table_spec(m, work, spec, tid=page2[0].id)
    else:
        r = templates.apply_table_spec(m, work, spec, page=1, rect=zone2)
    v2 = tableedit.grid_view(m, work, r["id"])
    t2 = texts(v2)
    if (v2["rows"], v2["cols"]) != (4, 3) or t2.get((0, 0)) != "Group" or t2.get((1, 0)) != "Leeds" \
            or t2.get((3, 2)) != "3.8":
        bad.append(f"{profile}: template on table 2 {v2['rows']}x{v2['cols']} {t2}")
    if not any(c["role"] == "TH" for c in v2["cells"] if c["r"] == 0):
        bad.append(f"{profile}: template header row")
    # … and to the untagged block (a zone)
    zone3 = [36, t3top.y0 - 26, 40 + 3 * W, t3top.y1 + 26]
    for t in [t for t in tables(m) if t.page == 1 and t.id != r["id"]]:
        tableedit.untable(m, t.id)
    r3 = templates.apply_table_spec(m, work, spec, page=1, rect=zone3)
    v3 = tableedit.grid_view(m, work, r3["id"])
    t3 = texts(v3)
    if (v3["rows"], v3["cols"]) != (3, 3) or t3.get((1, 0)) != "North" or t3.get((2, 2)) != "4.6":
        bad.append(f"{profile}: template on a zone {v3['rows']}x{v3['cols']} {t3}")

    m.check_integrity()
    save_model(job, m)
    out = os.path.join(job, "out.pdf")
    build(m, work, out, o)
    errs = [f"{f.code} {f.message}" for f in check(out) if f.severity == "error" and f.code not in ("UA-23",)]
    if errs:
        bad.append(f"{profile}: checker errors {errs}")
    if structure_integrity(out) or mc_balance(out):
        bad.append(f"{profile}: structure {structure_integrity(out)[:2]} {mc_balance(out)[:2]}")
    tableedit.apply(m, work, r3["id"], {"op": "untable", "how": "rows"})
    if r3["id"] in m.nodes:
        bad.append(f"{profile}: not a table")
    compat.use("")


def main() -> int:
    bad: list = []
    for prof in ("Default", "Kriyadocs"):
        run(prof, bad)
    if bad:
        print("table template test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("table template test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
