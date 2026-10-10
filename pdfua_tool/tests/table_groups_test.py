"""Group a table by its headings: "Initial Position" and then its own words (V4.4).

A sound-spelling card sets its sample words in columns under three headings.
Read across the printed rows that is "ace acorn make break away play" — six
unrelated words. What the card teaches is each heading and then the words under
it, read down the first column of the group and then down the next:

    TR > TH  Initial Position
    TR > ace, age, ape, ate, acorn, aid, aim, aimless
    TR > TH  Medial Position
    TR > make, face, paid, wait, break, great, steak
    ...
"""
import os
import sys
import tempfile

import pymupdf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import edit                       # noqa: E402
from core.analyzer import analyze           # noqa: E402
from core.lint import lint                  # noqa: E402
from core.options import Options            # noqa: E402
from core.pipeline import finish, load_model, process_file, save_model  # noqa: E402

GROUPS = [("Initial Position", [["ace", "age", "ape", "ate"], ["acorn", "aid", "aim", "aimless"]]),
          ("Medial Position", [["make", "face", "paid", "wait"], ["break", "great", "steak"]]),
          ("Final Position", [["away", "clay", "day", "may"], ["play", "say", "stay", "way"]])]
WANT = [["Initial Position"],
        ["ace", "age", "ape", "ate", "acorn", "aid", "aim", "aimless"],
        ["Medial Position"],
        ["make", "face", "paid", "wait", "break", "great", "steak"],
        ["Final Position"],
        ["away", "clay", "day", "may", "play", "say", "stay", "way"]]


def ruled_outside_only(path: str) -> None:
    """The card the publisher really prints: only the outside of the word block
    is ruled, and the words simply sit in columns inside it."""
    d = pymupdf.open()
    p = d.new_page(width=612, height=300)
    x0, y0, w, h = 48, 40, 516, 190
    p.draw_rect(pymupdf.Rect(x0, y0, x0 + w, y0 + h), color=(0, 0, 0), width=0.9)
    p.draw_line(pymupdf.Point(x0 + w / 2, y0), pymupdf.Point(x0 + w / 2, y0 + h),
                color=(0, 0, 0), width=0.9)
    p.draw_line(pymupdf.Point(x0, y0 + 34), pymupdf.Point(x0 + w, y0 + 34),
                color=(0, 0, 0), width=0.9)
    p.insert_text((x0 + 60, y0 + 24), "Initial Position", fontsize=13, fontname="hebo")
    p.insert_text((x0 + w / 2 + 60, y0 + 24), "Final Position", fontsize=13, fontname="hebo")
    for side, rows in ((0, [["chat", "chill", "cheese"], ["chin", "child", "choose"],
                            ["chip", "chase", "change"], ["chop", "check", "children"]]),
                       (1, [["lunch", "beach", "batch"], ["punch", "teach", "pitch"],
                            ["reach", "branch", "match"], ["peach", "crunch", "crutch"]])):
        for r, words in enumerate(rows):
            for c, word in enumerate(words):
                p.insert_text((x0 + 12 + side * w / 2 + c * 86, y0 + 62 + r * 32), word, fontsize=13)
    d.save(path)
    d.close()


def gaps_card(path: str) -> None:
    """A card where one column runs out of words: the empty cells are still
    cells, and each heading still covers its own two columns."""
    d = pymupdf.open()
    p = d.new_page(width=612, height=300)
    x0, y0, cw, rh = 48, 40, 86, 30
    p.draw_rect(pymupdf.Rect(x0, y0, x0 + 6 * cw, y0 + 5 * rh), color=(0, 0, 0), width=0.9)
    p.draw_line(pymupdf.Point(x0, y0 + rh), pymupdf.Point(x0 + 6 * cw, y0 + rh),
                color=(0, 0, 0), width=0.9)
    for g in (2, 4):
        p.draw_line(pymupdf.Point(x0 + g * cw, y0), pymupdf.Point(x0 + g * cw, y0 + 5 * rh),
                    color=(0, 0, 0), width=0.9)
    for i, head in enumerate(["Initial Position", "Medial Position", "Final Position"]):
        p.insert_text((x0 + i * 2 * cw + 14, y0 + 20), head, fontsize=11, fontname="hebo")
    for r, row in enumerate([["orbit", "organ", "born", "board", "for", "more"],
                             ["order", "oar", "horn", "coarse", "roar", "chore"],
                             ["", "", "storm", "bored", "soar", "sore"],
                             ["", "", "story", "snored", "core", "store"]]):
        for c, w in enumerate(row):
            if w:
                p.insert_text((x0 + c * cw + 8, y0 + rh * (r + 1) + 20), w, fontsize=11)
    d.save(path)
    d.close()


def card(path: str) -> None:
    d = pymupdf.open()
    p = d.new_page(width=612, height=360)
    p.insert_text((48, 50), "Sample Words", fontsize=18, fontname="hebo")
    x0, y0, cw, rowh = 48, 70, 85, 26
    for i, (head, cols) in enumerate(GROUPS):
        hx = x0 + i * 2 * cw
        p.draw_rect(pymupdf.Rect(hx, y0, hx + 2 * cw, y0 + rowh), color=(0, 0, 0), width=0.8)
        p.insert_text((hx + 8, y0 + 18), head, fontsize=11, fontname="hebo")
        for c, words in enumerate(cols):
            for r in range(4):
                cx, cy = hx + c * cw, y0 + rowh * (r + 1)
                p.draw_rect(pymupdf.Rect(cx, cy, cx + cw, cy + rowh), color=(0, 0, 0), width=0.6)
                if r < len(words):
                    p.insert_text((cx + 8, cy + 18), words[r], fontsize=11)
    d.save(path)
    d.close()


def shape(m, tid, keep_blank=False):
    out = []
    for r in [n for n in m.descendants(tid) if n.role == "TR"]:
        texts = [(m.nodes[c].text or "").strip() for c in r.children]
        out.append(texts if keep_blank else [t for t in texts if t])
    return out


def main() -> int:
    bad = []
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "card.pdf")
    card(src)

    m = analyze(src, Options())
    m.path = src
    t = next((n for n in m.ordered_nodes() if n.role == "Table"), None)
    if t is None:
        print("table groups test: FAILED\n    no table found")
        return 1
    r = edit.convert_table_columns(m, t.id, "cells")
    got = shape(m, t.id)
    if got != WANT:
        bad.append(f"grouped shape is {got}")
    if r["groups"] != 3 or r["cells"] != 23:
        bad.append(f"convert reported {r}")
    # the rows must stay the same width, or PAC calls the table irregular
    widths = [sum(max(1, m.nodes[c].colspan) for c in row.children)
              for row in [n for n in m.descendants(t.id) if n.role == "TR"]]
    if len(set(widths)) != 1:
        bad.append(f"rows have different widths {widths}")
    msgs = {(i.get("message") if isinstance(i, dict) else getattr(i, "message", "")) for i in lint(m)}
    if msgs:
        bad.append(f"lint complains after grouping: {sorted(msgs)}")

    # the words may be P tags rather than cells (a table the grid finder only
    # half read); "paragraphs" keeps them exactly as they are
    m2 = analyze(src, Options())
    m2.path = src
    t2 = next(n for n in m2.ordered_nodes() if n.role == "Table")
    for n in list(m2.descendants(t2.id)):
        if n.role == "TD" and (n.text or "").strip():
            n.role = "P"
    edit.convert_table_columns(m2, t2.id, "paragraphs")
    if shape(m2, t2.id) != WANT:
        bad.append(f"keeping the tags gave {shape(m2, t2.id)}")
    roles = {m2.nodes[c].role for row in [n for n in m2.descendants(t2.id) if n.role == "TR"]
             for c in row.children}
    if "P" not in roles:
        bad.append("“keep the tags as they are” turned them into cells")

    # a table with no heading says so instead of guessing
    m3 = analyze(src, Options())
    m3.path = src
    t3 = next(n for n in m3.ordered_nodes() if n.role == "Table")
    for n in list(m3.descendants(t3.id)):
        if n.role == "TH":
            n.role = "TD"
    try:
        edit.convert_table_columns(m3, t3.id, "cells")
        bad.append("a table with no TH was grouped anyway")
    except edit.EditError:
        pass

    # and it survives the build
    job = os.path.join(tmp, "job")
    res = process_file(src, job, Options())
    if res.get("status") != "done":
        bad.append(f"build failed: {res.get('error')}")
    else:
        m4 = load_model(job)
        t4 = next(n for n in m4.ordered_nodes() if n.role == "Table")
        edit.convert_table_columns(m4, t4.id, "cells")
        save_model(job, m4)
        out = finish(job, Options(), filename="card.pdf")
        errs = [f for f in out.get("findings_after", [])
                if getattr(f, "severity", "") == "error"]
        if errs:
            bad.append(f"checker errors after grouping: {[f.code for f in errs]}")
        import pikepdf
        pdf = pikepdf.open(os.path.join(job, "accessible.pdf"))
        rows = 0
        stack = [pdf.Root.StructTreeRoot.get("/K")]
        while stack:
            e = stack.pop()
            if isinstance(e, pikepdf.Array):
                stack.extend(list(e))
                continue
            if isinstance(e, pikepdf.Dictionary) and "/S" in e:
                if str(e.S) == "/TR":
                    rows += 1
                k = e.get("/K")
                if k is not None:
                    stack.append(k)
        if rows != 6:
            bad.append(f"the built file has {rows} rows, expected 6")

    # A card ruled only on the outside, with the words simply sitting in columns
    # inside it: drawing the Table zone by hand reads the layout, so every word
    # is a cell of its own and each heading spans its own columns. (The page
    # sweep leaves such a panel alone — a box round a block of words is not
    # always meant to be a grid.)
    plain = os.path.join(tmp, "outside.pdf")
    ruled_outside_only(plain)
    mp = analyze(plain, Options())
    mp.path = plain
    nid = edit._structured_from_rect(mp, plain, 0, (44, 66, 568, 264), "Table",
                                     Options(), deep=True)
    tp = mp.nodes[nid]
    rows = shape(mp, tp.id)
    if len(rows) != 5 or rows[0] != ["Initial Position", "Final Position"]:
        bad.append(f"the drawn zone gave {len(rows)} rows, first {rows[:1]}")
    elif rows[1] != ["chat", "chill", "cheese", "lunch", "beach", "batch"]:
        bad.append(f"the words are not cells of their own: {rows[1]}")
    heads = [n for n in mp.descendants(tp.id) if n.role == "TH"]
    if len(heads) != 2 or any(h.colspan != 3 for h in heads):
        bad.append(f"headings span {[h.colspan for h in heads]}, expected 3 each")
    # and grouping it then reads down each column of each heading
    edit.convert_table_columns(mp, tp.id, "paragraphs")
    got = shape(mp, tp.id)
    if got[1][:4] != ["chat", "chin", "chip", "chop"]:
        bad.append(f"grouping the drawn table gave {got[1][:4]}")

    # a column that runs out of words keeps its empty cells
    gaps = os.path.join(tmp, "gaps.pdf")
    gaps_card(gaps)
    mg = analyze(gaps, Options())
    mg.path = gaps
    gid = edit._structured_from_rect(mg, gaps, 0, (44, 106, 568, 264), "Table",
                                     Options(), deep=True)
    tg = mg.nodes[gid]
    widths = [len(r.children) for r in [n for n in mg.descendants(tg.id) if n.role == "TR"]]
    if widths != [3, 6, 6, 6, 6]:
        bad.append(f"the drawn zone gave rows of {widths}, expected [3, 6, 6, 6, 6]")
    rows = shape(mg, tg.id, keep_blank=True)
    if len(rows) == 5 and rows[3][:2] != ["", ""]:
        bad.append(f"the empty cells are missing: {rows[3]}")
    gheads = [n for n in mg.descendants(tg.id) if n.role == "TH"]
    if len(gheads) != 3 or any(h.colspan != 2 for h in gheads):
        bad.append(f"headings span {[h.colspan for h in gheads]}, expected 2 each")

    if bad:
        print("table groups test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("table groups test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
