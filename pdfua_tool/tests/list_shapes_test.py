"""Lists the shape the page shows (V4.6).

Built from the layout of an assessment handbook, two columns a page:

  * a bulleted list whose second point has two dash points under it, the dash
    printed doubled ("–– If the student …")
  * a numbered list with a note set in under item 3, then item 4
  * a numbered list whose item 4 runs on to the next page, with paragraphs and
    a bulleted list of its own, and item 5 below them
  * a sentence wrapped so that the next line starts "2: Letter Sounds." — not
    an item
"""
import os
import sys
import tempfile

import pymupdf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.analyzer import analyze      # noqa: E402
from core.options import Options       # noqa: E402

FS = 10.5
LH = 13.0


# a font built into PyMuPDF that has the bullet, the dashes and the curly quote
NOTO = pymupdf.Font("cjk")


def write(p, x, y, lines, hang=0.0):
    """Lines of text; with `hang`, the lines after the first are set in that far
    (the body of a list item hangs under its text, not under its number)."""
    if "noto" not in [f[3] for f in p.get_fonts()]:
        p.insert_font(fontname="noto", fontbuffer=NOTO.buffer)
    for k, t in enumerate(lines):
        p.insert_text((x + (hang if k else 0.0), y), t, fontsize=FS, fontname="noto")
        y += LH
    return y


BUL, DASH, Q = "\u2022", "\u2013\u2013", "\u2019"


def handbook(path: str) -> None:
    d = pymupdf.open()
    # ---- page 1: left column, bullets with dash points under the second one;
    #      right column, items 1-3, a note set in under item 3, then item 4
    p = d.new_page(width=612, height=792)
    y = write(p, 54, 80, ["As you complete Skill Set 1, decide whether to go on."])
    y = write(p, 54, y + 8, [f"{BUL} If the score for Skill Set 1 is 20 or more,",
                             "go on to Skill Set 2."], hang=12)
    y = write(p, 54, y + 6, [f"{BUL} If the score for Skill Set 1 is fewer than 20,",
                             "use these guidelines to decide."], hang=12)
    y = write(p, 66, y + 4, [f"{DASH} If the student has completed the easier set,",
                             "say to the student:", f"We{Q}ll stop here. Thank you."], hang=12)
    y = write(p, 66, y + 6, [f"{DASH} If the student has not been assessed on it,",
                             "go back and give it now."], hang=12)
    y = write(p, 54, y + 14, ["If the score is 20 or more items, go on to Skill Set",
                              "2: Letter Sounds."])
    y = write(p, 321, 80, ["1. Say to the student:",
                           "I am going to ask you to read some words."], hang=12)
    y = write(p, 321, y + 6, ["2. Place the task card in front of the student."])
    y = write(p, 321, y + 6, ["3. Point to the first word, and say to the student:",
                              "Please read these words."], hang=12)
    y = write(p, 333, y + 6, ["To pace the student, you may want to tell the",
                              "student to wait until you say next."])
    y = write(p, 321, y + 6, ["4. Record the responses on the scoring sheet.",
                              "Note how quickly the student answers."], hang=12)
    # ---- page 2: a list that starts here, item 4 at the foot of the column
    q = d.new_page(width=612, height=792)
    y = write(q, 321, 80, ["Administering the Task"])
    y = write(q, 321, y + 6, ["1. Say to the student: Read these letters."])
    y = write(q, 321, y + 6, ["2. Place the task card in front of the student."])
    y = write(q, 321, y + 6, ["3. Point to the first letter."])
    y = write(q, 321, y + 6, ["4. Record the responses on the scoring sheet,",
                              "using the guide in the appendix."], hang=12)
    # ---- page 3: item 4 runs on, with a paragraph and bullets, then item 5
    r = d.new_page(width=612, height=792)
    y = write(r, 333, 80, ["provided for comments, and the rate of response."])
    y = write(r, 333, y + 8, ["Scoring vowels: the short sound is correct."])
    y = write(r, 333, y + 8, [f"{BUL} When a student reads a letter correctly,",
                              "do not mark it."], hang=12)
    y = write(r, 333, y + 4, [f"{BUL} When a student reads a letter incorrectly,",
                              "make a slash through it."], hang=12)
    y = write(r, 321, y + 8, ["5. When the student finishes, record the score."])
    d.save(path)
    d.close()


def scores(path: str) -> None:
    """A results box: '• Task A:' with its score set at a tab stop on the same
    row, then a comment; the scores read as a column of their own."""
    d = pymupdf.open()
    p = d.new_page(width=612, height=792)
    y = write(p, 309, 80, ["Skill Set 3: VC and CVC"])
    rows = [("Task A:", "9/10 correct"), ("Task B:", "19/20 correct")]
    for label_text, value in rows:
        write(p, 309, y, [f"{BUL} {label_text}"])
        write(p, 396, y, [value])
        y += 16
    y = write(p, 309, y, [f"{BUL} Comments: the responses were reasonably quick",
                          "and confident."], hang=12)
    y = write(p, 309, y + 14, ["Action: the teacher went on to the next skill set."])
    # page numbers at the foot of two pages, and a sentence running across them
    write(p, 309, 700, ["The assessment goes on until the student has"])
    write(p, 300, 760, ["9"])
    q = d.new_page(width=612, height=792)
    write(q, 309, 80, ["finished every task in the skill set."])
    write(q, 300, 760, ["10"])
    d.save(path)
    d.close()


GRID = [["discount", "confide", "portable", "nonsense"],
        ["index", "station", "fastest"],
        ["return", "madness", "careless"]]


def word_grid(path: str) -> None:
    """A task form: a number, then words set in three columns."""
    d = pymupdf.open()
    p = d.new_page(width=612, height=792)
    write(p, 90, 60, ["10."])
    for c, col in enumerate(GRID):
        y = 140
        for w in col:
            p.insert_text((100 + c * 140, y), w, fontsize=20, fontname="noto")
            y += 70
    d.save(path)
    d.close()


def items(m, L):
    return [m.nodes[c] for c in L.children if m.nodes[c].role == "LI"]


def label(m, li):
    return next((m.nodes[c].text for c in li.children if m.nodes[c].role == "Lbl"), "")


def body(m, li):
    return next((m.nodes[c] for c in li.children if m.nodes[c].role == "LBody"), None)


def main() -> int:
    bad: list = []
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "handbook.pdf")
    handbook(src)
    m = analyze(src, Options())
    tops = [n for n in m.ordered_nodes() if n.role == "L" and
            (n.parent is None or m.nodes[n.parent].role not in ("LBody", "LI", "L"))]

    # 1. bullets with dash points nested under the second bullet
    bl = next((L for L in tops if L.page == 0 and label(m, items(m, L)[0]) == "•"), None)
    if bl is None:
        bad.append("the bulleted list on page 1 was not found")
    else:
        its = items(m, bl)
        if len(its) != 2:
            bad.append(f"the bulleted list has {len(its)} items, expected 2")
        else:
            subs = [m.nodes[c] for c in body(m, its[1]).children if m.nodes[c].role == "L"]
            if len(subs) != 1:
                bad.append("the dash points are not a list inside the second bullet")
            else:
                dash = items(m, subs[0])
                if len(dash) != 2 or label(m, dash[0]) != "––":
                    bad.append(f"the dash list has {len(dash)} items, labels "
                               f"{[label(m, x) for x in dash]}")
                elif "stop here" not in (body(m, dash[0]).text or ""):
                    bad.append("the line under the first dash point is not in its item")
    # the wrapped "2: Letter Sounds." is no item
    if any(label(m, li) == "2:" for L in m.nodes.values() if L.role == "L" for li in items(m, L)):
        bad.append("a wrapped '2: Letter Sounds.' became a list item")

    # 2. one numbered list 1-4 on page 1, with the note inside item 3
    nl = [L for L in tops if L.page == 0 and label(m, items(m, L)[0]) == "1."]
    if len(nl) != 1:
        bad.append(f"{len(nl)} numbered list(s) on page 1, expected 1")
    else:
        its = items(m, nl[0])
        if [label(m, x) for x in its] != ["1.", "2.", "3.", "4."]:
            bad.append(f"the numbered list reads {[label(m, x) for x in its]}")
        else:
            b3 = body(m, its[2])
            said = " ".join([b3.text or ""] + [m.nodes[c].text or "" for c in b3.children])
            if "To pace the student" not in said:
                bad.append("the note is not inside item 3")
            b4 = body(m, its[3])
            if "To pace the student" in (b4.text or ""):
                bad.append("the note went into item 4")

    # 3. the list that starts on page 2 carries on to item 5 on page 3
    l2 = [L for L in tops if L.page == 1 and label(m, items(m, L)[0]) == "1."]
    if len(l2) != 1:
        bad.append(f"{len(l2)} numbered list(s) start on page 2, expected 1")
    else:
        its = items(m, l2[0])
        labels = [label(m, x) for x in its]
        if labels != ["1.", "2.", "3.", "4.", "5."]:
            bad.append(f"the list across pages 2-3 reads {labels}")
        else:
            b4 = body(m, its[3])
            kinds = [(m.nodes[c].role, m.nodes[c].page) for c in b4.children]
            if ("L", 2) not in kinds:
                bad.append(f"item 4's bullets on page 3 are not inside it: {kinds}")
            if sum(1 for k in kinds if k == ("P", 2)) < 2:
                bad.append(f"item 4's paragraphs on page 3 are not inside it: {kinds}")
    if any(L.page == 2 for L in tops):
        bad.append("page 3 starts a list of its own")

    # 3b. the same list as a job tagged by hand leaves it: one L stretched over
    #     the page break, items 1-3 with a paragraph straight in the list and
    #     item 4's number inside item 3, and the part on page 3 never read
    from core.model import StructNode
    from core.repair import (_union, fix_list_children, list_children_wrong,
                             lists_read_short, tidy_lists)
    mj = analyze(src, Options())
    Lj = next((L for L in mj.ordered_nodes() if L.role == "L" and L.parent is None
               and L.page == 1 and any(d.page == 2 for d in mj.descendants(L.id))), None)
    if Lj is None:
        bad.append("the list across pages 2-3 was not found for the job test")
    else:
        part = None
        for d in mj.descendants(Lj.id):
            if d.page == 2 and d.bbox:
                part = _union(part, d.bbox)
        for c in list(Lj.children):
            mj.detach(c)
            mj._drop_subtree(c)
        box = Lj.bbox
        for k in range(3):
            li = StructNode(id=f"jli{k}", role="LI", page=1, bbox=box)
            mj.add(li, Lj.id)
            mj.add(StructNode(id=f"jlbl{k}", role="Lbl", page=1, text=f"{k + 1}.", bbox=box), li.id)
            mj.add(StructNode(id=f"jlb{k}", role="LBody", page=1, text="item", bbox=box), li.id)
        mj.add(StructNode(id="jp_in_li", role="P", page=1, text="a note", bbox=box), "jli2")
        mj.add(StructNode(id="jlbl4", role="Lbl", page=1, text="4.", bbox=box), "jli2")
        mj.add(StructNode(id="jb4", role="LBody", page=1, text="Record", bbox=box), "jli2")
        mj.add(StructNode(id="jp_in_l", role="P", page=1, text="using the guide", bbox=box), Lj.id)
        Lj.parts = [[2, *part]]
        if len(list_children_wrong(mj)) < 2:
            bad.append(f"the wrong list inside was not seen: {list_children_wrong(mj)}")
        if lists_read_short(mj) != [Lj.id]:
            bad.append("the unread part on page 3 was not seen")
        # without the page: the shape alone is put right
        import copy
        shape_only = copy.deepcopy(mj)
        fix_list_children(shape_only)
        if list_children_wrong(shape_only):
            bad.append("fixing the list's shape left something wrong")
        L_ = shape_only.nodes[Lj.id]
        got = [next((shape_only.nodes[k].text for k in shape_only.nodes[c].children
                     if shape_only.nodes[k].role == "Lbl"), "") for c in L_.children]
        if got != ["1.", "2.", "3.", "4."]:
            bad.append(f"the list's shape after the fix reads {got}")
        # with the page: Tidy the lists reads it again across the page break
        r = tidy_lists(mj, None, src)
        if not r.get("reread"):
            bad.append(f"Tidy the lists did not read the list again: {r}")
        Lr = next((L for L in mj.ordered_nodes() if L.role == "L" and L.parent is None
                   and L.page == 1 and label(mj, items(mj, L)[0]) == "1."), None)
        labs = [label(mj, x) for x in items(mj, Lr)] if Lr is not None else []
        if labs != ["1.", "2.", "3.", "4.", "5."]:
            bad.append(f"after Tidy the lists the list reads {labs}")
        elif not any(mj.nodes[c].role == "L" and mj.nodes[c].page == 2
                     for c in body(mj, items(mj, Lr)[3]).children):
            bad.append("item 4's bullets on page 3 are not inside it after the repair")
        if list_children_wrong(mj):
            bad.append("the repaired list still holds something other than items")

    # 4. a score printed beside its item belongs to it, not to a paragraph
    sc = os.path.join(tmp, "scores.pdf")
    scores(sc)
    ms = analyze(sc, Options())
    ls = [n for n in ms.ordered_nodes() if n.role == "L" and n.page == 0]
    if len(ls) != 1:
        bad.append(f"{len(ls)} list(s) in the results box, expected 1")
    else:
        bodies = [(body(ms, li).text or "") for li in items(ms, ls[0])]
        if bodies[:2] != ["Task A: 9/10 correct", "Task B: 19/20 correct"]:
            bad.append(f"the scores did not join their items: {bodies}")
        if any(ms.nodes[c].role == "P" for li in items(ms, ls[0])
               for c in body(ms, li).children):
            bad.append("a paragraph was left inside the results list")
    # and a file tagged before this rule is put right by Tidy the lists
    from core.analyzer import Analyzer
    from core.repair import list_values_apart, tidy_lists
    keep = Analyzer._row_values
    Analyzer._row_values = staticmethod(lambda run, built: None)
    try:
        old = analyze(sc, Options())
    finally:
        Analyzer._row_values = staticmethod(keep)
    if list_values_apart(old):
        r = tidy_lists(old, None, sc)
        lo = [n for n in old.ordered_nodes() if n.role == "L" and n.page == 0]
        got = [(body(old, li).text or "") for li in items(old, lo[0])] if lo else []
        if r["values"] < 1 or got[:2] != ["Task A: 9/10 correct", "Task B: 19/20 correct"]:
            bad.append(f"Tidy the lists did not put the scores on their items: {r} {got}")

    # 5. page numbers: left out by default, read as a P where the project asks
    art = [n for n in ms.nodes.values() if (n.text or "").strip() in ("9", "10")]
    if not art or any(not n.artifact for n in art):
        bad.append("a page number was read by default")
    mp = analyze(sc, Options(page_numbers="P"))
    pn = [n for n in mp.nodes.values() if (n.text or "").strip() in ("9", "10")]
    if len(pn) != 2 or any(n.artifact or n.role != "P" for n in pn):
        bad.append(f"page numbers with the option on: {[(n.role, n.artifact) for n in pn]}")
    elif sorted(n.actual_text for n in pn) != ["Page 10", "Page 9"]:
        bad.append(f"page numbers are read as {[n.actual_text for n in pn]}, not 'Page 9'")
    else:
        roots = [r for r in mp.root_children if not mp.nodes[r].artifact]
        first_page = [r for r in roots if mp.nodes[r].page == 0]
        if mp.nodes[first_page[-1]].text.strip() != "9":
            bad.append("the page number is not read last on its page")
    joined = [n for n in mp.nodes.values() if "until the student has" in (n.text or "")]
    if not joined or "finished every task" not in joined[0].text:
        bad.append("the sentence across the page break was not joined past the page number")
    from core.repair import page_numbers_as
    if page_numbers_as(ms, True) != 2:
        bad.append("the page-number switch did not read both numbers")
    elif sorted(n.actual_text for n in ms.nodes.values()
                if (n.text or "").strip() in ("9", "10")) != ["Page 10", "Page 9"]:
        bad.append("the page-number switch does not read them as 'Page 9'")
    if page_numbers_as(ms, False) != 2 or any(
            not n.artifact or n.actual_text for n in ms.nodes.values()
            if (n.text or "").strip() in ("9", "10")):
        bad.append("switching page numbers back did not leave them out again")

    # 6. a grid of words: one P per word, read down each column in turn
    wg = os.path.join(tmp, "grid.pdf")
    word_grid(wg)
    mw = analyze(wg, Options())
    words = [mw.nodes[r].text.strip() for r in mw.root_children
             if not mw.nodes[r].artifact and mw.nodes[r].role == "P"
             and mw.nodes[r].text.strip() != "10."]
    want = [w for col in GRID for w in col]
    if words != want:
        bad.append(f"the word grid reads {words}")
    # a page tagged before this rule had the words joined into one paragraph
    from core.repair import joined_word_grids, split_zones
    roots = [r for r in mw.root_children if mw.nodes[r].role == "P"
             and mw.nodes[r].text.strip() in want]
    host = mw.nodes[roots[3]]
    for r in roots[4:]:
        b = mw.nodes[r]
        host.text += " " + b.text
        host.parts.append([b.page, *b.bbox])
        mw.remove(b.id)
    host.note = "joined with continuation on the next column/page"
    if joined_word_grids(mw) != [host.id]:
        bad.append("the joined words were not spotted")
    split_zones(mw, wg)
    again = [mw.nodes[r].text.strip() for r in mw.root_children
             if mw.nodes[r].role == "P" and mw.nodes[r].text.strip() in want]
    if again != want:
        bad.append(f"after the split the grid reads {again}")

    if bad:
        print("list shapes test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("list shapes test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
