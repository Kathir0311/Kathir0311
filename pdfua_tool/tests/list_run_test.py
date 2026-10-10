"""Numbered items that the page splits up still make one list (V4.2).

Three shapes that were coming out as loose paragraphs:
  * a rubric whose first item sits at the margin and whose later items are
    indented, with labels like "1 and 2:" and "3:"
  * a question list where only the first item has a write-in blank
  * a wrapped last line that landed beside the item it belongs to
And: a zone drawn over several paragraphs gives one tag per paragraph.

V4.3: a large-print page — the student pages of a workbook set at 18 pt while
the teacher pages are 11 pt. Measured against the document body, every question
on those pages reads as a heading and no number ever becomes a label; measured
against the page's own running text, they are the list they look like.
"""
import os
import sys
import tempfile

import pymupdf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import edit                       # noqa: E402
from core.analyzer import analyze           # noqa: E402
from core.options import Options            # noqa: E402

STORY = ["Mom and I were in the yard. I saw a baby blue jay fly out of its nest.",
         "The baby bird landed on the grass. Then I saw Tabby, our cat!",
         "Tabby walked slowly toward the bird. Mom said, “Oh no! Save the bird!”"]


def make(path: str) -> None:
    d = pymupdf.open()
    p = d.new_page(width=460, height=640)
    y = [50]

    def line(t, size=10, bold=False, x=40, gap=0.0):
        p.insert_text((x, y[0]), t, fontsize=size, fontname="hebo" if bold else "helv")
        y[0] += size * 1.6 + gap

    line("Rubric Score", 11, True)
    line("1 and 2: Student has not achieved an appropriate level of fluency for the passage.", 9)
    line("3: Fluent reading is being refined at the Phonics Intervention level of the passage.", 9, x=52)
    line("6: Fluent reading has been achieved for the Phonics Intervention level at which the", 9, x=52)
    line("is written.", 9, x=58, gap=16)
    line("Self-Check", 11, True)
    line("1. Tim and Jim kicked the ___________.", 10, gap=6)
    line("2. Why did Tim and Jim go to bed?", 10, gap=6)
    line("3. Did Tim and Jim like Mom's story?", 10)
    line("How can you tell?", 10, x=52, gap=16)
    for t in STORY:
        line(t, 9, gap=5)
    d.save(path)
    d.close()


def make_bigprint(path: str) -> None:
    """Three 11 pt teacher pages, then two large-print student pages."""
    d = pymupdf.open()
    for _ in range(3):
        pg = d.new_page(width=612, height=792)
        y = 90
        pg.insert_text((72, 60), "Teacher Notes", fontsize=16, fontname="hebo")
        for _ in range(28):
            pg.insert_text((72, y), "Ordinary running text for the teacher edition of the book.",
                           fontsize=11)
            y += 15
    pg = d.new_page(width=612, height=792)
    pg.insert_text((72, 70), "Self-Check", fontsize=20, fontname="hebo")
    y = 130
    for q in ("1. What might you see at night?", "2. What might you hear at night?",
              "3. What can you do at night?"):
        pg.insert_text((80, y), q, fontsize=18)
        y += 60
    pg = d.new_page(width=612, height=792)
    y = 120
    pg.insert_text((80, y), "1. Tim and Jim kicked the ___________.", fontsize=18)
    pg.insert_text((80, y + 60), "2. Why did Tim and Jim go to bed?", fontsize=18)
    pg.insert_text((80, y + 120), "3. Did Tim and Jim like Mom's story?", fontsize=18)
    pg.insert_text((92, y + 146), "How can you tell?", fontsize=18)   # wrapped, barely indented
    d.save(path)
    d.close()


def make_fluency(path: str) -> None:
    """A fluency passage: every line carries its word count out in the margin."""
    d = pymupdf.open()
    p = d.new_page(width=460, height=400)
    y = 60
    for text, num in (('"The ranger told me that', "__73"),
                      ("we should get to higher", "__78"),
                      ("ground because the river will", "__85"),
                      ('crest in about two hours,"', "__90"),
                      ("said Dad.", "__92"),
                      ("But it was too late. The", "__98"),
                      ("river had flowed over its", "__103")):
        p.insert_text((50, y), text, fontsize=13)
        p.insert_text((330, y), num, fontsize=11)
        y += 34
    d.save(path)
    d.close()


def make_twocol(path: str) -> None:
    d = pymupdf.open()
    p = d.new_page(width=460, height=300)
    y = 60
    for a, b in zip(["The river rose quickly that night and",
                     "the family had very little time to pack",
                     "their things before leaving the cabin."],
                    ["A ranger arrived at dawn with a boat",
                     "and carried everyone across the water",
                     "to the shelter on the higher ground."]):
        p.insert_text((40, y), a, fontsize=9)
        p.insert_text((250, y), b, fontsize=9)
        y += 24
    d.save(path)
    d.close()


def items(m, lid):
    return [c for c in m.nodes[lid].children if m.nodes[c].role == "LI"]


def main() -> int:
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "lists.pdf")
    make(src)
    bad = []
    m = analyze(src, Options())
    lists = [n for n in m.ordered_nodes() if n.role == "L"]
    if len(lists) != 2:
        bad.append(f"expected 2 lists (rubric, self-check), got {len(lists)}")
    for L in lists:
        got = items(m, L.id)
        if len(got) != 3:
            bad.append(f"list on page {L.page + 1} has {len(got)} items, expected 3")
        for li in got:
            kids = {m.nodes[c].role for c in m.nodes[li].children}
            if kids != {"Lbl", "LBody"}:
                bad.append(f"list item has {sorted(kids)}, expected Lbl + LBody")
            body = next((m.nodes[c] for c in m.nodes[li].children if m.nodes[c].role == "LBody"), None)
            lbl = next((m.nodes[c] for c in m.nodes[li].children if m.nodes[c].role == "Lbl"), None)
            if body is not None and lbl is not None and body.text.startswith(lbl.text):
                bad.append(f"the label {lbl.text!r} is still inside the LBody")
    # the wrapped line belongs to its item, not to a paragraph of its own
    if not any(n.role == "LBody" and n.text.endswith("is written.") for n in m.nodes.values()):
        bad.append("the wrapped line did not join its list item")
    if any(n.role == "P" and n.text.strip() == "is written." for n in m.nodes.values()):
        bad.append("the wrapped line is still a paragraph of its own")
    # only the story paragraphs stay paragraphs
    ps = [n for n in m.ordered_nodes() if n.role == "P" and not n.artifact and (n.text or "").strip()]
    if len(ps) != len(STORY):
        bad.append(f"{len(ps)} paragraphs left, expected {len(STORY)}: {[p.text[:30] for p in ps]}")

    # a zone drawn over all three paragraphs gives three tags, not one
    boxes = [p.bbox for p in ps]
    if boxes:
        rect_pdf = (min(b[0] for b in boxes) - 6, min(b[1] for b in boxes) - 6,
                    max(b[2] for b in boxes) + 6, max(b[3] for b in boxes) + 6)
        doc = pymupdf.open(src)
        disp = edit.pdf_to_display(doc[0], rect_pdf)
        doc.close()
        edit.create_from_rect(m, src, 0, disp, "P", True, Options(), split=True)
        made = [n for n in m.ordered_nodes() if n.role == "P" and n.source == "user"]
        if len(made) != len(STORY):
            bad.append(f"the drawn zone made {len(made)} paragraph(s), expected {len(STORY)}")
        if any(len((n.text or "").split()) > 30 for n in made):
            bad.append("the drawn zone put several paragraphs into one tag")
        if not made:
            bad.append("nothing was created from the drawn zone")

    # a zone split line by line gives one tag per printed line
    if boxes:
        m3 = analyze(src, Options())
        story = [n for n in m3.ordered_nodes()
                 if n.role == "P" and not n.artifact and (n.text or "").strip()]
        if story:
            sb = [n.bbox for n in story]
            doc = pymupdf.open(src)
            d2 = edit.pdf_to_display(doc[0], (min(b[0] for b in sb) - 6, min(b[1] for b in sb) - 6,
                                              max(b[2] for b in sb) + 6, max(b[3] for b in sb) + 6))
            doc.close()
            edit.create_from_rect(m3, src, 0, d2, "P", True, Options(), split="lines")
            lines_made = [n for n in m3.ordered_nodes() if n.role == "P" and n.source == "user"]
            if len(lines_made) != len(STORY):
                bad.append(f"split by line gave {len(lines_made)} tag(s)")
            starts = {t[:24] for t in STORY}
            if any(n.text.strip()[:24] not in starts for n in lines_made):
                bad.append(f"a line tag is not one printed line: {[n.text[:30] for n in lines_made]}")

    # an item that lost its number to the body text gets a Lbl back
    m4 = analyze(src, Options())
    li = next((n for n in m4.ordered_nodes() if n.role == "LI"), None)
    if li is not None:
        kids = {m4.nodes[c].role: m4.nodes[c] for c in li.children}
        lbl, body = kids.get("Lbl"), kids.get("LBody")
        if lbl is None or body is None:
            bad.append("the first list item has no Lbl/LBody to test with")
        else:
            body.text = lbl.text + " " + body.text
            body.bbox = (lbl.bbox[0], body.bbox[1], body.bbox[2], body.bbox[3])
            m4.detach(lbl.id)
            m4.nodes.pop(lbl.id)
            edit.fix_item_labels(m4, src, Options())
            roles = {m4.nodes[c].role for c in m4.nodes[li.id].children}
            if "Lbl" not in roles:
                bad.append("fix_item_labels did not put the number back in a Lbl")
            elif m4.nodes[[c for c in m4.nodes[li.id].children
                           if m4.nodes[c].role == "LBody"][0]].text.startswith(lbl.text):
                bad.append("the number is still inside the LBody")

    # ---------------------------------------------------- large-print pages
    big = os.path.join(tmp, "bigprint.pdf")
    make_bigprint(big)
    mb = analyze(big, Options())
    lists = [n for n in mb.ordered_nodes() if n.role == "L" and n.page in (3, 4)]
    if len(lists) != 2:
        bad.append(f"large print: {len(lists)} list(s) on the student pages, expected 2")
    for L in lists:
        its = items(mb, L.id)
        if len(its) != 3:
            bad.append(f"large print: list on page {L.page + 1} has {len(its)} items, expected 3")
            continue
        for it in its:
            roles = {mb.nodes[c].role for c in mb.nodes[it].children}
            if "Lbl" not in roles:
                bad.append(f"large print: an item on page {L.page + 1} has no Lbl")
                break
    tails = [n for n in mb.ordered_nodes() if (n.text or "").strip() == "How can you tell?"]
    if tails:
        bad.append("large print: the wrapped line is still a tag of its own")
    bodies = [n for n in mb.ordered_nodes()
              if n.role == "LBody" and "How can you tell?" in (n.text or "")]
    if not bodies:
        bad.append("large print: the wrapped line did not join its item")
    # the editor's button repairs items whose number is back inside the text
    broken = 0
    for li in [n for n in list(mb.nodes.values()) if n.role == "LI"]:
        kids = {mb.nodes[c].role: mb.nodes[c] for c in li.children}
        lbl, body = kids.get("Lbl"), kids.get("LBody")
        if lbl is None or body is None:
            continue
        body.text = (lbl.text + " " + body.text).strip()
        body.bbox = (min(lbl.bbox[0], body.bbox[0]), min(lbl.bbox[1], body.bbox[1]),
                     max(lbl.bbox[2], body.bbox[2]), max(lbl.bbox[3], body.bbox[3]))
        mb.detach(lbl.id)
        mb.nodes.pop(lbl.id, None)
        broken += 1
    fixed = edit.fix_item_labels(mb, big, Options())
    if fixed != broken:
        bad.append(f"large print: Fix list labels put back {fixed} of {broken} labels")
    if edit.items_missing_labels(mb):
        bad.append("large print: an item still carries its number inside the LBody")

    # ------------------------------- split by line keeps the margin number
    flu = os.path.join(tmp, "fluency.pdf")
    make_fluency(flu)
    mf = analyze(flu, Options())
    mf.path = flu
    rows = edit._paragraphs_from_rect(mf, flu, 0, (40, 30, 420, 320), Options(), mode="lines")
    if len(rows) != 6:
        bad.append(f"split by line gave {len(rows)} tags for 6 printed rows")
    for nd in rows:
        if "__" not in (nd.text or ""):
            bad.append(f"a line lost its margin number: {(nd.text or '')[:40]!r}")
            break
    lone = [nd for nd in rows if (nd.text or "").strip().startswith("__")]
    if lone:
        bad.append("the margin number is still a tag of its own")

    two = os.path.join(tmp, "twocol.pdf")
    make_twocol(two)
    mt = analyze(two, Options())
    mt.path = two
    cols = edit._paragraphs_from_rect(mt, two, 0, (30, 150, 440, 290), Options(), mode="lines")
    if len(cols) != 6:
        bad.append(f"two columns split by line gave {len(cols)} tags, expected 6")
    if any(len((nd.text or "").split()) > 9 for nd in cols):
        bad.append("split by line joined two columns into one tag")

    if bad:
        print("list/zone test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("list/zone test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
