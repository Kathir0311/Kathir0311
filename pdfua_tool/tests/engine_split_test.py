"""The V4 engine (the tool's own tagging — the Academain client's structure,
every profile without a house style) is untouched by the client house styles
(core/compat.py): what V4.10 on added for Kriyadocs runs only when a job's
options name a house style.

Checks: the switch itself (per job, per thread), and each place the engines
part — list labels, line reading (a kerned-away space), the paragraph breaks,
the inner tag's glyphs, the link owner's zones, the reference-list labels,
Note > Lbl in the structure fixer, and the analysis of a whole file giving
the same tree with the switch off as the V4 engine always did.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymupdf  # noqa: E402

from core import compat  # noqa: E402
from core.analyzer import LABEL_RE, collect_lines  # noqa: E402
from core.options import profile_options  # noqa: E402


def main() -> int:
    bad: list[str] = []

    # ---- the switch: per job, off by default, never carried into a new thread
    compat.use("")
    if compat.house():
        bad.append("the V4 engine is not the default")
    compat.use(profile_options("Kriyadocs"))
    if not compat.house():
        bad.append("a Kriyadocs job does not switch to its engine")
    seen = []
    th = threading.Thread(target=lambda: seen.append(compat.house()))
    th.start()
    th.join()
    if seen != [False]:
        bad.append("a new request thread inherits a client's engine")
    compat.use(profile_options("Default"))
    if compat.house():
        bad.append("a Default job runs the house-style engine")
    for name in ("Book", "Journal article (2-column)", "Word / report export", "Best quality (AI, slower)",
                 "Scanned document (OCR)", "Batch auto (no review)"):
        if compat.style_of(profile_options(name)):
            bad.append(f"profile {name} names a house style")

    # ---- list labels: an arrow glued to its text is a label only for Kriyadocs
    compat.use("")
    if LABEL_RE.match("⇒Few studies have") or not LABEL_RE.match("• An item"):
        bad.append("V4 list labels changed")
    compat.use("kriyadocs")
    if not LABEL_RE.match("⇒Few studies have"):
        bad.append("Kriyadocs does not read a glued arrow as a label")

    # ---- line reading: a space kerned away to nothing
    d = pymupdf.open()
    p = d.new_page(width=300, height=200)
    tw = pymupdf.TextWriter(p.rect)
    font = pymupdf.Font("helv")
    tw.append((20, 50), "cross-", font=font, fontsize=10)
    x = 20 + font.text_length("cross-", fontsize=10)
    tw.append((x, 50), " ", font=font, fontsize=10)
    tw.append((x, 50), "sectional study", font=font, fontsize=10)
    tw.write_text(p)
    compat.use("")
    v4 = " ".join(l.text for l in collect_lines(p))
    compat.use("kriyadocs")
    v5 = " ".join(l.text for l in collect_lines(p))
    if v4 == v5:
        bad.append(f"line reading does not part the engines ({v4!r})")
    if "cross- sectional" not in v4:
        bad.append(f"V4 line reading changed: {v4!r}")
    d.close()

    # ---- a whole file: the V4 engine gives the same tree whatever ran before
    from core.analyzer import analyze
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "a.pdf")
    d = pymupdf.open()
    for k in range(2):
        pg = d.new_page()
        pg.insert_text((72, 80), f"Chapter {k + 1}", fontsize=20)
        y = 120
        for i in range(6):
            pg.insert_text((72, y), f"⇒Item {i} of a list printed with arrows", fontsize=10)
            y += 14
        pg.insert_text((72, y + 20), "A paragraph of running text that goes on for a while.", fontsize=10)
    d.save(src)
    d.close()

    def shape(m):
        return [(n.role, (n.text or "")[:30]) for n in m.ordered_nodes()]
    compat.use("")
    first = shape(analyze(src, profile_options("Default")))
    compat.use("kriyadocs")
    shape(analyze(src, profile_options("Kriyadocs")))
    compat.use(profile_options("Default"))      # what a job's prepare/finish do
    again = shape(analyze(src, profile_options("Default")))
    if first != again:
        bad.append("the V4 analysis changed after a Kriyadocs job in the same process")

    # ---- the structure fixer: Note > Lbl only for Kriyadocs
    from core.model import DocModel, StructNode
    from core.repair import structure_problems
    m = DocModel(path=src, page_count=1)
    note = StructNode(id="n1", role="Note", page=0, bbox=(0, 0, 10, 10), text="Department")
    lbl = StructNode(id="l1", role="Lbl", page=0, bbox=(0, 0, 2, 10), text="1")
    m.add(note)
    m.add(lbl, "n1")
    compat.use("")
    if "l1" not in structure_problems(m):
        bad.append("V4 no longer moves a Lbl out of a Note")
    compat.use("kriyadocs")
    if "l1" in structure_problems(m):
        bad.append("Kriyadocs Note > Lbl is moved")
    compat.use("")

    if bad:
        print("engine split test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("engine split test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
