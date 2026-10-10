"""Links both ways: footnotes and captions (V4.8).

  * page 1: running text with footnote marks set small and raised ("…decoding.¹",
    "…every day.²"), the two footnotes at the foot of the page, and a figure
    whose caption "Figure 1 The score sheet" is mentioned in the text
    ("as Figure 1 shows")
  * page 2: text with endnote mark ³ and a "Notes" list at the end with note 3

Each mark has to jump to its note and the note's own number back to the mark;
the text's "Figure 1" jumps to the figure and the caption's "Figure 1" back to
the text — in the built file, as real link annotations inside Link tags, with
no checker errors.
"""
import os
import sys
import tempfile

import pikepdf
import pymupdf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.checker import check                      # noqa: E402
from core.options import Options                    # noqa: E402
from core.pipeline import load_model, process_file  # noqa: E402
from tests.verify import mc_balance, structure_integrity  # noqa: E402

FS = 11.0


def line(p, x, y, parts):
    """parts: [(text, superscript?)] written one after another on one line."""
    for t, sup in parts:
        size = FS * 0.62 if sup else FS
        yy = y - FS * 0.36 if sup else y
        p.insert_text((x, yy), t, fontsize=size, fontname="helv")
        x += pymupdf.get_text_length(t, fontname="helv", fontsize=size)


def book(path: str) -> None:
    d = pymupdf.open()
    p = d.new_page(width=612, height=792)
    p.insert_text((60, 70), "Spelling and Decoding", fontsize=20, fontname="hebo")
    line(p, 60, 110, [("Spelling tells the teacher a great deal about decoding.", False), ("1", True)])
    line(p, 60, 124, [("The two skills grow together, and teachers can watch", False)])
    line(p, 60, 138, [("them grow every day in class.", False), ("2", True),
                      (" The score sheet records both,", False)])
    line(p, 60, 152, [("as Figure 1 shows for one student over a school year.", False)])
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 30), False)
    pix.set_rect(pix.irect, (30, 110, 190))
    p.insert_image(pymupdf.Rect(60, 180, 340, 380), pixmap=pix)
    p.insert_text((60, 398), "Figure 1  The score sheet", fontsize=9.5, fontname="hebo")
    p.draw_line((60, 690), (200, 690), width=0.5)
    p.insert_text((60, 706), "1 Hasbrouck found the same with older students.", fontsize=8.5, fontname="helv")
    p.insert_text((60, 720), "2 See the scoring guide for the full rubric.", fontsize=8.5, fontname="helv")
    p.insert_text((300, 770), "1", fontsize=9, fontname="helv")
    q = d.new_page(width=612, height=792)
    q.insert_text((60, 70), "Writing at Home", fontsize=20, fontname="hebo")
    line(q, 60, 110, [("Families can help by asking the child to spell a word aloud.", False), ("3", True)])
    line(q, 60, 124, [("Short practice works better than long sessions.", False)])
    q.insert_text((60, 300), "Notes", fontsize=16, fontname="hebo")
    q.insert_text((60, 330), "3 Ten minutes a day is enough for most children.", fontsize=8.5, fontname="helv")
    q.insert_text((300, 770), "2", fontsize=9, fontname="helv")
    d.save(path)
    d.close()


def main() -> int:
    bad: list = []
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "notes.pdf")
    book(src)
    job = os.path.join(tmp, "job")
    r = process_file(src, job, Options(existing_tags="retag", run_verapdf=False))
    if r.get("status") != "done":
        print("two-way test: FAILED —", r.get("error"))
        return 1
    m = load_model(job)
    notes = [n for n in m.nodes.values() if n.role == "Note"]
    if len(notes) < 2:
        bad.append(f"{len(notes)} Note tag(s) at the page foot: {[n.text[:20] for n in m.nodes.values() if n.page == 0][-6:]}")
    links = [n for n in m.nodes.values() if n.role == "Link" and (n.style or {}).get("xref")]
    by = {}
    for n in links:
        x = n.style["xref"]
        by.setdefault(x["kind"], []).append((n.text, n.page, m.nodes.get(x.get("node")).role
                                             if x.get("node") in m.nodes else None,
                                             m.nodes[n.parent].role))
    fwd = sorted(t for t, *_ in by.get("footnote", []))
    if fwd != ["1", "2", "3"]:
        bad.append(f"footnote marks linked: {by.get('footnote')}")
    for t, pg, tgt, host in by.get("footnote", []):
        if tgt != "Note":
            bad.append(f"mark {t} points at {tgt}")
    back = sorted(t for t, *_ in by.get("footnote_back", []))
    if back != ["1", "2"] and back != ["1", "2", "3"]:
        bad.append(f"notes linking back: {by.get('footnote_back')}")
    for t, pg, tgt, host in by.get("footnote_back", []):
        if host != "Note" or tgt not in ("P", "H1", "H2", "LBody"):
            bad.append(f"back link {t}: inside {host}, to {tgt}")
    if not by.get("figure"):
        bad.append(f"“Figure 1” in the text is not linked: {by}")
    cb = by.get("caption_back", [])
    if len(cb) != 1 or cb[0][3] != "Caption" or cb[0][2] not in ("P", "LBody"):
        bad.append(f"caption back link: {cb}")

    out = os.path.join(job, "accessible.pdf")
    errs = [f"{f.code}: {f.detail[:60]}" for f in check(out) if f.severity == "error"
            and f.code != "UA-20"]                  # the figure has no alt text in a test
    if errs:
        bad.append(f"checker errors {errs}")
    if structure_integrity(out):
        bad.append(f"structure integrity {structure_integrity(out)[:3]}")
    if mc_balance(out):
        bad.append("marked content unbalanced")
    pdf = pikepdf.open(out)
    contents = sorted(str(a.get("/Contents", "")) for pg in pdf.pages for a in (pg.obj.get("/Annots") or [])
                      if str(a.get("/Subtype")) == "/Link")
    if not any(c.startswith("Back to the text of note") for c in contents) or \
            not any(c.startswith("Go to note") for c in contents) or \
            not any(c.startswith("Back to where Figure 1") for c in contents):
        bad.append(f"link descriptions: {contents}")
    pdf.close()

    if bad:
        print("two-way test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("two-way test: OK", {k: len(v) for k, v in by.items()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
