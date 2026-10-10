"""A tag drawn by hand is read where it is printed (V4.9).

A file tagged elsewhere often holds the whole book in one Sect. A zone drawn on
page 2 of such a file went to the end of the document's top level — after the
Sect — so it was read last (reading position 1209 of 1212). Here:

  * a zone drawn on page 2 (with and without replacing the tags inside) goes
    into the Sect, between the page-2 blocks above and below it
  * a block already sitting at the end of the document is reported as out of
    order and "Fix reading order" puts it back inside the Sect, on its page
"""
import os
import sys
import tempfile

import pymupdf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import edit                                # noqa: E402
from core.options import Options                     # noqa: E402
from core.pipeline import load_model, process_file   # noqa: E402


def book(path: str) -> None:
    d = pymupdf.open()
    for k in range(3):
        p = d.new_page(width=612, height=792)
        if k == 0:
            p.insert_text((60, 70), "Assessment Handbook", fontsize=22, fontname="hebo")
        p.insert_text((60, 120), f"Page {k + 1} opens with this paragraph of running text.", fontsize=11)
        p.insert_text((60, 160), f"Name: ______________________  Date: ____________", fontsize=11)
        p.insert_text((60, 200), f"Page {k + 1} closes with this paragraph of running text.", fontsize=11)
    d.save(path)
    d.close()


def blocks_of(m, page):
    nums = edit.reading_numbers(m)
    out = []
    for n in m.ordered_nodes():
        b = nums.get(n.id, ("", None))[1]
        if b is not None and n.page == page and not n.artifact:
            out.append((b, n))
    return out


def main() -> int:
    bad = []
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "book.pdf")
    book(src)
    first = os.path.join(tmp, "first")
    process_file(src, first, Options(existing_tags="retag", run_verapdf=False, section_wrap="h1"))
    job = os.path.join(tmp, "import")
    process_file(os.path.join(first, "accessible.pdf"), job, Options(existing_tags="import", run_verapdf=False))
    m0 = load_model(job)
    if not any(n.role == "Sect" for n in m0.nodes.values()):
        print("zone order test: FAILED — the imported file has no Sect")
        return 1
    work = os.path.join(job, "working.pdf")

    for absorb in (True, False):
        m = load_model(job)
        nid = edit.create_from_rect(m, work, 1, (55, 148, 560, 166), "P", absorb, Options())
        n = m.nodes[nid]
        nums = edit.reading_numbers(m)
        mine = nums[nid][1]
        page2 = [b for b, x in blocks_of(m, 1) if x.id != nid]
        page3 = [b for b, x in blocks_of(m, 2)]
        if n.parent is None or m.nodes[n.parent].role != "Sect":
            bad.append(f"absorb={absorb}: the drawn tag is under {m.nodes[n.parent].role if n.parent else 'the document'}")
        if not page2 or not (min(page2) < mine < max(page3 or [10 ** 6])) or (page3 and mine > min(page3)):
            bad.append(f"absorb={absorb}: drawn tag read at {mine}; page 2 is {page2}, page 3 starts {page3[:1]}")
        if edit.order_problems(m):
            bad.append(f"absorb={absorb}: reading-order problems after drawing: {len(edit.order_problems(m))}")

    # what an older version left behind: the drawn tags at the end of the document
    m = load_model(job)
    moved = [x for _, x in blocks_of(m, 1) if "Name" in (x.text or "") or "Date" in (x.text or "")]
    for x in moved:
        m.detach(x.id)
        m.add(x, None)
    if len(edit.order_problems(m)) < len(moved):
        bad.append(f"blocks read after the whole Sect are not reported ({edit.order_problems(m)})")
    edit.fix_page_order(m, Options())
    if edit.order_problems(m):
        bad.append("Fix reading order left problems")
    for x in moved:
        if x.parent is None or m.nodes[x.parent].role != "Sect":
            bad.append(f"{x.text[:10]!r} was not put back inside the Sect")
    p2 = [x.id for _, x in blocks_of(m, 1)]
    if [p2.index(x.id) for x in moved] != sorted(p2.index(x.id) for x in moved) or \
            p2.index(moved[0].id) in (0, len(p2) - 1):
        bad.append(f"page 2 order after the fix: {[ (m.nodes[i].text or '')[:10] for i in p2]}")

    if bad:
        print("zone order test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("zone order test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
