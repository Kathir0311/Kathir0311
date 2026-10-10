"""Splitting a two-page spread (V4.6).

A book exported as spreads prints pages 12 and 13 side by side on one sheet.
Split, the sheet becomes the left page and then the right page: each shows
only its own half, every tag goes with its half, the pages after it move up by
one, and the built file is as sound as before — in a file tagged by the tool
and in a file whose tags were imported.
"""
import os
import sys
import tempfile

import pikepdf
import pymupdf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.checker import check                         # noqa: E402
from core.options import Options                       # noqa: E402
from core.pages import split_model, split_pdf, spreads  # noqa: E402
from core.pipeline import finish, load_model, process_file, save_model  # noqa: E402
from tests.verify import mc_balance, qpdf_check, structure_integrity   # noqa: E402

LEFT = ["Chapter 2  Planning", "The left page of the spread is read first.",
        "It has a paragraph of its own here."]
RIGHT = ["Chapter 3  Teaching", "The right page comes after the left page.",
         "Its link goes to the publisher's site."]


def book(path: str) -> None:
    d = pymupdf.open()
    p = d.new_page(width=420, height=600)                       # an ordinary page
    p.insert_text((50, 80), "Contents", fontsize=16, fontname="hebo")
    p.insert_text((50, 120), "An ordinary page before the spread.", fontsize=11)
    s = d.new_page(width=840, height=600)                       # the spread
    for half, lines in ((0, LEFT), (420, RIGHT)):
        y = 80
        for k, t in enumerate(lines):
            s.insert_text((half + 50, y), t, fontsize=16 if k == 0 else 11,
                          fontname="hebo" if k == 0 else "helv")
            y += 30
    s.insert_link({"kind": pymupdf.LINK_URI, "uri": "https://www.example.org/",
                   "from": pymupdf.Rect(470, 128, 800, 145)})
    q = d.new_page(width=420, height=600)                       # and one after
    q.insert_text((50, 80), "Chapter 4  After the spread", fontsize=16, fontname="hebo")
    q.insert_text((50, 120), "A page after the spread.", fontsize=11)
    d.save(path)
    d.close()


def texts(path: str) -> list[str]:
    d = pymupdf.open(path)
    try:
        return [p.get_text() for p in d]
    finally:
        d.close()


def check_job(bad: list, job: str, label: str) -> None:
    out = os.path.join(job, "accessible.pdf")
    t = texts(out)
    if len(t) != 4:
        bad.append(f"{label}: the built file has {len(t)} pages, expected 4")
        return
    if "left page of the spread" not in t[1] or "right page comes" in t[1]:
        bad.append(f"{label}: page 2 does not show only the left half")
    if "right page comes" not in t[2] or "left page of the spread" in t[2]:
        bad.append(f"{label}: page 3 does not show only the right half")
    if "After the spread" not in t[3]:
        bad.append(f"{label}: the page after the spread is not page 4")
    errs = [f.code for f in check(out) if f.severity == "error"]
    if errs:
        bad.append(f"{label}: checker errors after the split: {errs}")
    if structure_integrity(out):
        bad.append(f"{label}: structure integrity: {structure_integrity(out)[:3]}")
    if mc_balance(out):
        bad.append(f"{label}: marked content unbalanced")
    if qpdf_check(out) != "ok":
        bad.append(f"{label}: qpdf: {qpdf_check(out)}")
    # the right half's words are tagged on the new page, not left as artifacts
    m = load_model(job)
    on3 = " ".join(n.text or "" for n in m.nodes.values() if n.page == 2 and not n.artifact)
    if "right page comes" not in on3:
        bad.append(f"{label}: the right half's text is not tagged on the new page")
    on2 = " ".join(n.text or "" for n in m.nodes.values() if n.page == 1 and not n.artifact)
    if "right page comes" in on2:
        bad.append(f"{label}: the right half's text was left on the left page")
    order = [m.nodes[r].page for r in m.root_children if not m.nodes[r].artifact]
    if order != sorted(order):
        bad.append(f"{label}: the reading order is not page by page: {order}")


def split_job(job: str) -> dict:
    m = load_model(job)
    work = os.path.join(job, "working.pdf")
    r = split_pdf(work, 1)
    moved = split_model(m, 1, r["left"], r["right"], r["left_size"], r["right_size"], r.get("annot_map"))
    save_model(job, m)
    return {"moved": moved, **r}


def main() -> int:
    bad: list = []
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "spread.pdf")
    book(src)

    if spreads(src) != [1]:
        bad.append(f"spreads found: {spreads(src)}, expected [1]")

    # the PDF itself
    work = os.path.join(tmp, "work.pdf")
    import shutil
    shutil.copyfile(src, work)
    r = split_pdf(work, 1)
    pdf = pikepdf.open(work)
    try:
        if len(pdf.pages) != 4:
            bad.append(f"{len(pdf.pages)} pages after the split, expected 4")
        boxes = [[float(v) for v in p.obj.MediaBox] for p in pdf.pages]
        if boxes[1] != [0.0, 0.0, 420.0, 600.0] or boxes[2] != [420.0, 0.0, 840.0, 600.0]:
            bad.append(f"the halves are {boxes[1]} and {boxes[2]}")
        left_links = list(pdf.pages[1].obj.get("/Annots") or [])
        right_links = list(pdf.pages[2].obj.get("/Annots") or [])
        if left_links or len(right_links) != 1:
            bad.append(f"the link did not go with the right half ({len(left_links)}, {len(right_links)})")
        elif right_links[0].P.objgen != pdf.pages[2].obj.objgen:
            bad.append("the moved link does not point at its new page")
        if pdf.pages[1].obj.Contents.objgen == pdf.pages[2].obj.Contents.objgen:
            bad.append("the two halves share one content stream")
    finally:
        pdf.close()
    if r["annots_moved"] != 1:
        bad.append(f"{r['annots_moved']} annotation(s) moved, expected 1")

    # a file tagged by the tool
    job = os.path.join(tmp, "retag")
    res = process_file(src, job, Options(existing_tags="retag", run_verapdf=False))
    if res.get("status") != "done":
        bad.append(f"the first build failed: {res.get('error')}")
    else:
        s = split_job(job)
        if s["moved"] < 2:
            bad.append(f"{s['moved']} tag(s) moved to the right page")
        finish(job, Options(existing_tags="retag", run_verapdf=False), filename="spread.pdf")
        check_job(bad, job, "tagged by the tool")

    # a file whose tags are imported: the marked-content keys follow the half
    tagged = os.path.join(tmp, "tagged.pdf")
    if res.get("status") == "done":
        first = os.path.join(tmp, "first")
        process_file(src, first, Options(existing_tags="retag", run_verapdf=False))
        shutil.copyfile(os.path.join(first, "accessible.pdf"), tagged)
        job2 = os.path.join(tmp, "import")
        r2 = process_file(tagged, job2, Options(existing_tags="import", run_verapdf=False))
        if r2.get("mode") != "import":
            bad.append(f"the tagged file was handled as {r2.get('mode')}")
        else:
            split_job(job2)
            m2 = load_model(job2)
            keys = [k for n in m2.nodes.values() if n.page == 2 for k in n.src_keys]
            if keys and not all(k.startswith("2:") or k.startswith("s") for k in keys):
                bad.append(f"the right half's keys still point at the old page: {keys[:4]}")
            finish(job2, Options(existing_tags="import", run_verapdf=False), filename="tagged.pdf")
            check_job(bad, job2, "imported")

    if bad:
        print("pages test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("pages test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
