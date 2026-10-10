"""Word-grid tables: no invented header, no sliver rows, sentences inside (V4.3).

A phonics workbook prints the same worksheet several times, once per type size.
Four things went wrong on those pages:

  * every cell is set the same way, so the first row and the first column were
    read as headers — a grid of words announced column headings that are not
    there
  * page N+1 repeats page N, and the "table continued over pages" rule joined
    them: the repeated first row then became a running-header artifact and was
    lost, and the page title with it
  * a rule drawn a few points off added an empty sliver row along the top of
    the grid, which then read as the header row
  * the sentences under the grid are drawn as their own boxes, so they fell out
    of the table — printed inside the ruling, read after it
  * the empty answer row ruled between the grid and the sentence ended the
    table altogether — it is a row of it, read once as "Blank cell"
"""
import os
import sys
import tempfile

import pymupdf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import edit                       # noqa: E402
from core.analyzer import analyze           # noqa: E402
from core.options import Options            # noqa: E402

WORDS = [["at", "sat", "bat", "cat"], ["an", "man", "tan", "can"],
         ["nap", "map", "mat", "mad"], ["pan", "plan", "plant", ""]]


def worksheet(doc, sentence_gap: float = 14.0, sliver: bool = False) -> None:
    """One worksheet page: a word grid, then a sentence in a box of its own."""
    p = doc.new_page(width=612, height=500)
    p.insert_text((48, 60), "Blend Words", fontsize=24, fontname="hebo")
    x0, y0, w, h = 48, 90, 128, 42
    if sliver:                       # a rule drawn 5 pt above the first row
        p.draw_line(pymupdf.Point(x0, y0 - 5), pymupdf.Point(x0 + 4 * w, y0 - 5),
                    color=(0, 0, 0), width=0.7)
        p.draw_line(pymupdf.Point(x0, y0 - 5), pymupdf.Point(x0, y0), color=(0, 0, 0), width=0.7)
        p.draw_line(pymupdf.Point(x0 + 4 * w, y0 - 5), pymupdf.Point(x0 + 4 * w, y0),
                    color=(0, 0, 0), width=0.7)
    for r, row in enumerate(WORDS):
        for c, t in enumerate(row):
            rect = pymupdf.Rect(x0 + c * w, y0 + r * h, x0 + (c + 1) * w, y0 + (r + 1) * h)
            p.draw_rect(rect, color=(0, 0, 0), width=0.7)
            if t:
                p.insert_text((rect.x0 + 8, rect.y0 + 27), t, fontsize=15)
    sy = y0 + len(WORDS) * h + sentence_gap
    box = pymupdf.Rect(x0, sy, x0 + 4 * w, sy + 40)
    p.draw_rect(box, color=(0, 0, 0), width=0.7)
    p.insert_text((box.x0 + 8, box.y0 + 26), "I see an ant. Dad can see the ant.", fontsize=15)


def page_one(doc) -> None:
    """The small-print sheet: the grid, an empty answer row, then the sentence —
    three boxes, each drawn on its own."""
    p = doc.new_page(width=612, height=560)
    p.insert_text((48, 60), "Blend Words", fontsize=24, fontname="hebo")
    x0, y0, w, h = 48, 90, 128, 42
    for r, row in enumerate(WORDS):
        for c, t in enumerate(row):
            rect = pymupdf.Rect(x0 + c * w, y0 + r * h, x0 + (c + 1) * w, y0 + (r + 1) * h)
            p.draw_rect(rect, color=(0, 0, 0), width=0.7)
            if t:
                p.insert_text((rect.x0 + 8, rect.y0 + 27), t, fontsize=15)
    sy = y0 + len(WORDS) * h + 14
    p.draw_rect(pymupdf.Rect(x0, sy, x0 + 4 * w, sy + 42), color=(0, 0, 0), width=0.7)
    sent = pymupdf.Rect(x0, sy + 42, x0 + 4 * w, sy + 84)
    p.draw_rect(sent, color=(0, 0, 0), width=0.7)
    p.insert_text((sent.x0 + 8, sent.y0 + 27), "I see an ant. Dad can see the ant.", fontsize=15)


def tab_page(doc) -> None:
    """The same sheet with the unit title as a tab turned on its side in the
    outer margin — by position it comes last on the page."""
    page_one(doc)
    p = doc[-1]
    p.insert_text((585, 440), "Unit 1  Short a", fontsize=30, fontname="hebo", rotate=90)


def letter_grid(doc) -> None:
    """A 5x6 ruled alphabet grid whose rows are each ONE line of text.

    The letters are printed as "A B C D E", one line across five ruled cells.
    Read line by line the whole row went into the cell its middle fell in, so
    "M N O" sat in the N cell and the M and O cells were read "Blank cell" over
    letters plainly printed there."""
    fs, y0, ch = 22, 40, 46
    rows = ["ABCDE", "FGHIJ", "KLMNO", "PQRST", "UVWXY"]
    p = doc.new_page(width=420, height=330)
    p.insert_text((60, 28), "Uppercase Letters", fontsize=12, fontname="hebo")
    centres = []
    for r, row in enumerate(rows):
        x = 60
        p.insert_text((x, y0 + r * ch + 32), " ".join(row), fontsize=fs)
        if r == 0:
            cur = x
            for c in " ".join(row):
                w = pymupdf.get_text_length(c, fontname="helv", fontsize=fs)
                if c != " ":
                    centres.append(cur + w / 2)
                cur += w
    bounds = ([centres[0] - 22]
              + [(centres[i] + centres[i + 1]) / 2 for i in range(len(centres) - 1)]
              + [centres[-1] + 22, centres[-1] + 66])          # a blank last column
    for r in range(len(rows)):
        for c in range(len(bounds) - 1):
            p.draw_rect(pymupdf.Rect(bounds[c], y0 + r * ch, bounds[c + 1], y0 + (r + 1) * ch),
                        color=(0, 0.4, 0.4), width=0.7)


def cells(m, table, role=None):
    return [n for n in m.descendants(table.id)
            if n.role in (("TD", "TH") if role is None else (role,))]


def main() -> int:
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "worksheets.pdf")
    d = pymupdf.open()
    worksheet(d)                       # page 1
    worksheet(d)                       # page 2 — the same sheet printed again
    worksheet(d, sliver=True)          # page 3 — with a stray rule above the grid
    d.save(src)
    d.close()
    bad = []

    m = analyze(src, Options())
    tables = [n for n in m.ordered_nodes() if n.role == "Table"]
    if len(tables) != 3:
        bad.append(f"{len(tables)} table(s) for 3 worksheet pages — repeated pages were joined")
    heads = [n for n in m.ordered_nodes() if n.is_heading and not n.artifact]
    if len(heads) != 3:
        bad.append(f"{len(heads)} heading(s) kept; every page keeps its own title")
    if any(n.artifact for n in m.nodes.values() if n.role in ("TR", "THead")):
        bad.append("a table row was turned into a running-header artifact")

    for t in tables:
        if cells(m, t, "TH"):
            bad.append("a plain word grid has header cells (TH)")
            break
    for t in tables:
        rows = [n for n in m.descendants(t.id) if n.role == "TR"]
        if len(rows) != 5:
            bad.append(f"table on page {t.page + 1} has {len(rows)} rows, expected 5 "
                       "(4 word rows + the sentence)")
            break
        last = list(m.nodes[rows[-1].id].children)
        texts = [(m.nodes[c].text or "") for c in last]
        if not any("I see an ant" in x for x in texts):
            bad.append("the sentence box under the grid is not a row of the table")
            break
        if len(last) != 1 or m.nodes[last[0]].colspan != 4:
            bad.append("the sentence row does not span the grid")
            break
    loose = [n for n in m.ordered_nodes()
             if n.parent is None and n.role == "P" and "I see an ant" in (n.text or "")]
    if loose:
        bad.append("the sentence is still tagged outside the table")

    # a table that really does have a header keeps it
    hsrc = os.path.join(tmp, "withheader.pdf")
    d = pymupdf.open()
    p = d.new_page(width=400, height=240)
    rows = [["Name", "Score"], ["Ravi", "12"], ["Mina", "15"], ["Sam", "11"]]
    for r, row in enumerate(rows):
        for c, t in enumerate(row):
            rect = pymupdf.Rect(40 + c * 140, 40 + r * 36, 40 + (c + 1) * 140, 40 + (r + 1) * 36)
            p.draw_rect(rect, color=(0, 0, 0), width=0.7)
            p.insert_text((rect.x0 + 6, rect.y0 + 24), t, fontsize=11,
                          fontname="hebo" if r == 0 else "helv")
    d.save(hsrc)
    d.close()
    mh = analyze(hsrc, Options())
    th = [n for n in mh.nodes.values() if n.role == "TH"]
    if len(th) != 2:
        bad.append(f"a real header row gave {len(th)} TH, expected 2")

    # drawing a Table zone over grid + sentence box gives one table
    zsrc = os.path.join(tmp, "zone.pdf")
    d = pymupdf.open()
    worksheet(d, sentence_gap=30.0)        # too far to attach by itself
    d.save(zsrc)
    d.close()
    mz = analyze(zsrc, Options())
    mz.path = zsrc
    nid = edit._structured_from_rect(mz, zsrc, 0, (44, 160, 568, 414), "Table",
                                     Options(), deep=True)
    t = mz.nodes[nid]
    rows = [n for n in mz.descendants(t.id) if n.role == "TR"]
    if len(rows) != 5:
        bad.append(f"the drawn Table zone gave {len(rows)} rows, expected 5")
    else:
        last = list(mz.nodes[rows[-1].id].children)
        if len(last) != 1 or "I see an ant" not in (mz.nodes[last[0]].text or ""):
            bad.append("the drawn Table zone left the sentence out of the table")
        elif mz.nodes[last[0]].colspan != 4:
            bad.append("the added row does not span the grid")

    # the sheet with an empty answer row between the grid and the sentence
    p1 = os.path.join(tmp, "page1.pdf")
    d = pymupdf.open()
    page_one(d)
    d.save(p1)
    d.close()
    m1 = analyze(p1, Options())
    t1 = next((n for n in m1.ordered_nodes() if n.role == "Table"), None)
    if t1 is None:
        bad.append("no table on the small-print sheet")
    else:
        rows = [n for n in m1.descendants(t1.id) if n.role == "TR"]
        shape = [[(m1.nodes[c].text or "").strip(), m1.nodes[c].colspan]
                 for r in rows for c in r.children]
        if len(rows) != 6:
            bad.append(f"the small-print sheet gave {len(rows)} rows, expected 6")
        else:
            blank = list(m1.nodes[rows[4].id].children)
            sent = list(m1.nodes[rows[5].id].children)
            if len(blank) != 1 or m1.nodes[blank[0]].colspan != 4 or \
                    (m1.nodes[blank[0]].text or "").strip():
                bad.append("the empty answer row is not one blank cell across the grid")
            if len(sent) != 1 or m1.nodes[sent[0]].colspan != 4:
                bad.append("the sentence row is not one cell across the grid")
        if any(n.role == "P" and "I see an ant" in (n.text or "")
               for n in m1.ordered_nodes() if n.parent is None):
            bad.append("the sentence is outside the table on the small-print sheet")
        del shape

    # every table keeps its rows in a TBody; only a table with a header has a THead
    for t in [n for n in m1.ordered_nodes() if n.role == "Table"] if t1 else []:
        kinds = [m1.nodes[c].role for c in t.children]
        if kinds != ["TBody"]:
            bad.append(f"a table with no header has children {kinds}, expected ['TBody']")
            break
    hkinds = [mh.nodes[c].role for c in
              next(n for n in mh.ordered_nodes() if n.role == "Table").children]
    if hkinds != ["THead", "TBody"]:
        bad.append(f"a table with a header has children {hkinds}, expected ['THead', 'TBody']")

    # the unit tab in the margin is read first, not after the table
    tsrc = os.path.join(tmp, "tab.pdf")
    d = pymupdf.open()
    tab_page(d)
    d.save(tsrc)
    d.close()
    off = Options()
    off.page_title_first = "off"
    roles_off = [n.role for n in analyze(tsrc, off).ordered_nodes() if n.parent is None]
    if roles_off[0] == "H1" and roles_off[-1] != "H1":
        bad.append("the margin tab already read first — the test page does not show the problem")
    mt = analyze(tsrc, Options())           # default: page_title_first = margin
    first = next((n for n in mt.ordered_nodes() if n.parent is None), None)
    if first is None or not first.is_heading or "Unit 1" not in (first.text or ""):
        bad.append(f"the page does not open with its own heading "
                   f"({[ (n.role, (n.text or '')[:18]) for n in mt.ordered_nodes() if n.parent is None]})")
    if mt.info.get("headings_moved_first") != 1:
        bad.append("the move was not recorded in info['headings_moved_first']")

    # a ruled letter grid printed row by row: one letter per cell
    lsrc = os.path.join(tmp, "letters.pdf")
    d = pymupdf.open()
    letter_grid(d)
    d.save(lsrc)
    d.close()
    from core.analyzer import collect_lines
    pg = pymupdf.open(lsrc)[0]
    if not any(l.text.strip() == "A B C D E" for l in collect_lines(pg)):
        bad.append("the grid rows are not one line of text — the test does not show the problem")
    ml = analyze(lsrc, Options())
    lt = [n for n in ml.ordered_nodes() if n.role == "Table"]
    if not lt:
        bad.append("the ruled letter grid was not found as a table")
    else:
        grid = [[ml.nodes[c].text.strip() for c in r.children]
                for r in ml.descendants(lt[0].id) if r.role == "TR"]
        want = [list(w) + [""] for w in ("ABCDE", "FGHIJ", "KLMNO", "PQRST", "UVWXY")]
        if grid != want:
            bad.append(f"the letters did not land one per cell: {grid}")

    if bad:
        print("grid table test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("grid table test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
