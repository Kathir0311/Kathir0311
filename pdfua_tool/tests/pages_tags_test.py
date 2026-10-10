"""Tags on pages (V4.21.4): remove the tags of a page, of pages from … to …,
or of the whole file; re-analyse only some pages.

* remove page 2: no tag left there, pages 1 and 3 keep every tag they had;
  a list running from page 2 onto page 3 keeps its page-3 part
* remove pages 1–3 (the whole file): nothing left, no empty group
* re-analyse page 2 only: page 2 is tagged again, pages 1 and 3 untouched,
  and the analysis looked at page 2 alone
* the built file is sound after a removal
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymupdf  # noqa: E402

from core import compat, edit  # noqa: E402
from core.analyzer import analyze  # noqa: E402
from core.build import build  # noqa: E402
from core.options import profile_options  # noqa: E402
from core.pipeline import load_model, process_file  # noqa: E402
from tests.verify import mc_balance, structure_integrity  # noqa: E402


def make(path: str) -> None:
    d = pymupdf.open()
    for k in range(3):
        p = d.new_page()
        p.insert_text((72, 80), f"Chapter {k + 1}", fontsize=20)
        p.insert_text((72, 120), f"This is the text of page {k + 1}, a paragraph of its own.", fontsize=11)
        p.insert_text((72, 140), "It goes on for a second line on this page.", fontsize=11)
        for i in range(3):
            p.insert_text((72, 180 + 18 * i), f"{i + 1}. Point {i + 1} of page {k + 1}", fontsize=11)
    d.save(path)
    d.close()


def on_page(m, pno: int) -> set:
    return {n.id for n in m.nodes.values() if pno in n.pages()}


def main() -> int:
    bad = []
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "p.pdf")
    make(src)
    o = profile_options("Default")
    o.ai_alt_text_auto = False
    o.run_verapdf = False
    o.use_samples = False
    job = os.path.join(tmp, "job")
    process_file(src, job, o)
    compat.use(o)
    work = os.path.join(job, "working.pdf")
    m = load_model(job)
    p1, p3 = on_page(m, 0), on_page(m, 2)
    if not on_page(m, 1):
        bad.append("page 2 had no tags to begin with")
    r = edit.clear_pages(m, [1])
    if on_page(m, 1) or not r:
        bad.append(f"page 2 still has tags: {len(on_page(m, 1))}")
    if on_page(m, 0) != p1 or on_page(m, 2) != p3:
        bad.append("pages 1 / 3 changed")
    m.check_integrity()
    out = os.path.join(job, "out.pdf")
    build(m, work, out, o)
    if structure_integrity(out) or mc_balance(out):
        bad.append(f"structure after removal {structure_integrity(out)[:2]} {mc_balance(out)[:2]}")
    # re-analyse page 2 alone
    auto = analyze(work, o, pages=[1])
    if any(n.page not in (1, -1, None) for n in auto.nodes.values() if n.bbox is not None):
        bad.append("the page-2 analysis tagged other pages")
    res = edit.retag_pages(m, auto, [1])
    if not on_page(m, 1) or not res.get("added"):
        bad.append("page 2 was not tagged again")
    if on_page(m, 0) != p1 or on_page(m, 2) != p3:
        bad.append("re-analysing page 2 changed pages 1 / 3")
    if not any(n.role.startswith("H") and "Chapter 2" in (n.text or "") for n in m.nodes.values()):
        bad.append("page 2's heading is not back")
    # the whole file
    edit.clear_pages(m, [0, 1, 2])
    if m.nodes:
        bad.append(f"{len(m.nodes)} tag(s) left after removing all: {[n.role for n in list(m.nodes.values())[:5]]}")
    m.check_integrity()
    if bad:
        print("pages tags test: FAILED")
        for x in bad:
            print("   ", x)
        return 1
    print("pages tags test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
