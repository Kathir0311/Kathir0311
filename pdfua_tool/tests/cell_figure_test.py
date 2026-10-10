"""Pictures in table cells and merged rows (V4.19.2).

A ruled table with a picture in each cell of its right column (like "Types of
neurons", Figs. A–D). In the Default (V4) engine and the Kriyadocs house style:

* each picture is tagged once — a Figure inside its cell, none outside the table
* two rows' cells merged into one: no empty TR is left (the row the merged
  cell covers goes, the cell spans one row less)
* a model that already has a duplicate picture and an empty row is put right
  by "Fix structure" (and so by the build)
* the built file has no checker error but missing alt text
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
from core.model import StructNode  # noqa: E402
from core.options import profile_options  # noqa: E402
from core.pipeline import load_model, process_file, save_model  # noqa: E402
from core.repair import fix_structure  # noqa: E402


def make(path):
    d = pymupdf.open()
    p = d.new_page()
    p.insert_text((72, 60), "TABLE 9.1: Description of different types of neurons", fontsize=10, fontname="hebo")
    x0, y = 72, 70
    rows = [("Neurons-description", "Structure"), ("Unipolar neuron: one process", "A"),
            ("Bipolar neurons: two processes", "B"), ("Pseudounipolar neurons: T-shape", "C"),
            ("Multipolar neurons: several dendrites", "D")]
    for i, (a, b) in enumerate(rows):
        h = 22 if i == 0 else 80
        p.draw_rect(pymupdf.Rect(x0, y, x0 + 230, y + h), width=0.6)
        p.draw_rect(pymupdf.Rect(x0 + 230, y, x0 + 460, y + h), width=0.6)
        p.insert_text((x0 + 4, y + 15), a, fontsize=9, fontname="hebo" if i == 0 else "helv")
        if i == 0:
            p.insert_text((x0 + 234, y + 15), b, fontsize=9, fontname="hebo")
        else:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 20), False)
            pix.set_rect(pix.irect, (200, 150, 200))
            p.insert_image(pymupdf.Rect(x0 + 260, y + 8, x0 + 420, y + 60), pixmap=pix)
            p.insert_text((x0 + 300, y + 74), b, fontsize=9, fontname="hebo")
        y += h
    d.save(path)
    d.close()


def figures(m):
    inside = [n for n in m.nodes.values() if n.role == "Figure" and any(a.role in ("TD", "TH") for a in m.ancestors(n.id))]
    outside = [n for n in m.nodes.values() if n.role == "Figure" and not any(a.role == "Table" for a in m.ancestors(n.id))]
    return inside, outside


def run(profile, bad):
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "f.pdf")
    make(src)
    o = profile_options(profile)
    o.ai_alt_text_auto = False
    o.run_verapdf = False
    job = os.path.join(tmp, "job")
    process_file(src, job, o)
    compat.use(o)
    m = load_model(job)
    work = os.path.join(job, "working.pdf")
    ins, out = figures(m)
    if len(ins) != 4 or out:
        bad.append(f"{profile}: figures inside {len(ins)}, outside {len(out)}")
    t = next(n for n in m.nodes.values() if n.role == "Table")
    v = tableedit.grid_view(m, work, t.id)
    rows0, cols0 = v["rows"], v["cols"]
    ids = [c["id"] for c in v["cells"] if c["r"] in (rows0 - 2, rows0 - 1)]
    tableedit.apply(m, work, t.id, {"op": "merge_cells", "ids": ids})
    if [n.id for n in m.nodes.values() if n.role == "TR" and not n.children]:
        bad.append(f"{profile}: an empty TR after merging")
    v = tableedit.grid_view(m, work, t.id)
    if (v["rows"], v["cols"]) != (rows0 - 1, cols0):
        bad.append(f"{profile}: grid after merging {v['rows']}x{v['cols']}")
    # an old model: a duplicate picture outside and an empty row
    f0 = ins[0]
    dup = StructNode(id=m.new_id("fig"), role="Figure", page=f0.page, bbox=f0.bbox, alt="neuron A")
    m.add(dup)
    tr = StructNode(id=m.new_id("tr"), role="TR", page=0)
    body = next((n for n in m.descendants(t.id) if n.role == "TBody"), t)
    m.add(tr, body.id)
    r = fix_structure(m)
    if dup.id in m.nodes or f0.alt != "neuron A" or tr.id in m.nodes:
        bad.append(f"{profile}: fix structure {r}")
    for f in figures(m)[0]:
        f.alt = f.alt or "a neuron"
    m.check_integrity()
    save_model(job, m)
    outp = os.path.join(job, "out.pdf")
    build(m, work, outp, o)
    errs = [f"{e.code} {e.message}" for e in check(outp) if e.severity == "error" and e.code not in ("UA-23",)]
    if errs:
        bad.append(f"{profile}: checker {errs}")
    compat.use("")


def main():
    bad = []
    for prof in ("Default", "Kriyadocs"):
        run(prof, bad)
    if bad:
        print("cell figure test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("cell figure test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
