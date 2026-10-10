"""Putting an imported structure right (V4.5).

A file tagged somewhere else arrives with four things PAC fails on, and none of
them are about what is printed:

  * a contents entry sitting inside a paragraph, a list item at section level, a
    cell outside its row — tags under a parent PDF/UA does not allow
  * a contents list with nothing to click: 41 TOCIs, no Link
  * "1. A ______ flew out of its nest" read as "A flew out of its nest"
  * a figure with no /Alt, with its caption printed right underneath it

This test builds such a file the only honest way — tag it with the tool, with
those three passes switched off, then feed the result back in as an imported
file — and checks that importing it fixes all four.
"""
import os
import sys
import tempfile

import pikepdf
import pymupdf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.model import DocModel, StructNode            # noqa: E402
from core.options import Options                       # noqa: E402
from core.pipeline import load_model, process_file      # noqa: E402
from core.repair import (alt_from_captions, blank_lines_missing,   # noqa: E402
                         fix_structure, structure_problems)


# ----------------------------------------------------------------- structure
def misplaced() -> DocModel:
    """A model with every kind of tag PDF/UA will not allow where it sits."""
    m = DocModel(path="")
    sect = StructNode(id="sect", role="Sect", page=0, bbox=(0, 0, 400, 700))
    m.add(sect, None)
    # a contents list tagged as a paragraph with the entries inside it
    p = StructNode(id="p1", role="P", page=0, bbox=(50, 600, 400, 660), text="Contents")
    m.add(p, "sect")
    for i in range(3):
        m.add(StructNode(id=f"toci{i}", role="TOCI", page=0,
                         bbox=(50, 640 - i * 14, 400, 652 - i * 14),
                         text=f"Chapter {i + 1} ....... {i + 3}"), "p1")
    # a stray list item, and its parts, at section level
    li = StructNode(id="li1", role="LI", page=0, bbox=(50, 560, 400, 580))
    m.add(li, "sect")
    m.add(StructNode(id="lbl1", role="Lbl", page=0, bbox=(50, 560, 60, 580), text="1."), "sect")
    # a cell with no row, inside a table
    tb = StructNode(id="tb1", role="Table", page=0, bbox=(50, 400, 400, 500))
    m.add(tb, "sect")
    for i in range(2):
        m.add(StructNode(id=f"td{i}", role="TD", page=0,
                         bbox=(50 + i * 100, 460, 140 + i * 100, 480), text=f"cell {i}"), "tb1")
    return m


def phantom_row_part(bad: list) -> None:
    """A blank band read as a row of its own, printed on top of a real row."""
    from core.repair import drop_duplicate_rows
    m = DocModel(path="")
    real = StructNode(id="t1", role="Table", page=0, bbox=(50, 400, 450, 500))
    m.add(real, None)
    for r in range(2):
        tr = StructNode(id=f"r{r}", role="TR", page=0, bbox=(50, 450 - r * 50, 450, 500 - r * 50))
        m.add(tr, "t1")
        for c in range(2):
            m.add(StructNode(id=f"c{r}{c}", role="TD", page=0, text=f"word {r}{c}",
                             bbox=(50 + c * 200, 450 - r * 50, 250 + c * 200, 500 - r * 50)), tr.id)
    # the phantom: one wide blank cell over the second row, read as its own table
    ghost = StructNode(id="t2", role="Table", page=0, bbox=(50, 398, 450, 452))
    m.add(ghost, None)
    body = StructNode(id="tb2", role="TBody", page=0, bbox=(50, 398, 450, 452))
    m.add(body, "t2")
    gr = StructNode(id="gr", role="TR", page=0, bbox=(50, 398, 450, 452))
    m.add(gr, "tb2")
    m.add(StructNode(id="gc", role="TD", page=0, bbox=(50, 398, 450, 452)), "gr")
    gone = drop_duplicate_rows(m)
    if gone != 1:
        bad.append(f"the phantom row was not dropped ({gone})")
    if "gr" in m.nodes["tb2"].children:
        bad.append("the phantom row is still in its table")
    if "t2" in m.root_children:
        bad.append("the table left with no rows is still in the tree")
    if len(m.nodes["t1"].children) != 2:
        bad.append("a real row was dropped as well")


def structure_part(bad: list) -> None:
    m = misplaced()
    before = structure_problems(m)
    if len(before) != 7:
        bad.append(f"{len(before)} misplaced tags seen, expected 7")
    moved = fix_structure(m)
    left = structure_problems(m)
    if left:
        roles = [f"{m.nodes[i].role} in {m.nodes[m.nodes[i].parent].role}" for i in left]
        bad.append(f"still misplaced after the fix: {roles}")
    if moved.get("TOCI") != 3 or moved.get("TD") != 2 or not moved.get("LI"):
        bad.append(f"moved {moved}")
    # the contents entries are in one TOC, and that TOC is NOT inside the paragraph
    toc = m.nodes[m.nodes["toci0"].parent]
    if toc.role != "TOC" or len(toc.children) != 3:
        bad.append(f"the entries went into {toc.role} with {len(toc.children)} children")
    if toc.parent != "sect":
        up = m.nodes.get(toc.parent)
        bad.append(f"the TOC sits in {up.role if up else None}, not beside the paragraph")
    # it keeps its place in the reading order: the paragraph still comes first
    kids = m.nodes["sect"].children
    if kids.index("p1") > kids.index(toc.id):
        bad.append("the contents list was moved before the paragraph that held it")
    # the two cells share one row, the row is in the table
    tr = m.nodes[m.nodes["td0"].parent]
    if tr.role != "TR" or len(tr.children) != 2 or tr.parent != "tb1":
        bad.append(f"the cells went into {tr.role} ({len(tr.children)} cells) in {tr.parent}")
    if m.nodes[m.nodes["li1"].parent].role != "L":
        bad.append("the stray list item did not get a list")
    if m.nodes[m.nodes["lbl1"].parent].role != "LI":
        bad.append("the stray label did not get a list item")
    # and it is safe to run twice
    if fix_structure(m):
        bad.append("a second pass still found something to move")


# ----------------------------------------------------------------- a workbook
PAGES = [
    # contents, then two numbered pages whose folios are 4 and 5
    ([("Contents", 16, True),
      ("Unit 1  Birds and Nests ....................... 4", 11, False),
      ("Unit 2  What Plants Need ..................... 5", 11, False)], None),
    ([("Unit 1  Birds and Nests", 15, True),
      ("Write the missing word on each line.", 11, False),
      ("1. A ____________ flew out of its nest.", 11, False),
      ("2. The nest was built in a ____________ tree.", 11, False),
      ("3. Three ____________ eggs were inside it.", 11, False)], "4"),
    ([("Unit 2  What Plants Need", 15, True),
      ("4. Plants need ____________ and water.", 11, False),
      ("5. Leaves make ____________ for the plant.", 11, False)], "5"),
]
CAPTION = "Figure 1  A robin feeding its young"


def workbook(path: str) -> None:
    d = pymupdf.open()
    for lines, folio in PAGES:
        p = d.new_page(width=460, height=640)
        y = 70
        for txt, size, bold in lines:
            p.insert_text((55, y), txt, fontsize=size, fontname="hebo" if bold else "helv")
            y += size * 2.1
        if folio:
            p.insert_text((225, 615), folio, fontsize=9)
    # a picture with its caption under it, on the last page
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 160, 100))
    for x in range(160):
        for y in range(100):
            pix.set_pixel(x, y, (40 + x, 90, 200 - x) if (x + y) % 7 else (250, 240, 60))
    p = d[-1]
    p.insert_image(pymupdf.Rect(55, 330, 255, 455), pixmap=pix)
    p.insert_text((55, 472), CAPTION, fontsize=9, fontname="hebo")
    d.save(path)
    d.close()


def page_text(path: str) -> str:
    d = pymupdf.open(path)
    try:
        return "\n".join(p.get_text() for p in d)
    finally:
        d.close()


def actual_texts(path: str) -> set:
    """Every /ActualText in the built file's structure tree."""
    pdf = pikepdf.open(path)
    out: set = set()
    try:
        stack = [pdf.Root.StructTreeRoot.get("/K")]
        while stack:
            e = stack.pop()
            if isinstance(e, pikepdf.Array):
                stack.extend(list(e))
                continue
            if isinstance(e, pikepdf.Dictionary) and "/S" in e:
                if "/ActualText" in e:
                    out.add(str(e.ActualText))
                k = e.get("/K")
                if k is not None:
                    stack.append(k)
        return out
    finally:
        pdf.close()


def links_of(path: str) -> list:
    pdf = pikepdf.open(path)
    try:
        return [a for p in pdf.pages for a in (p.obj.get("/Annots") or [])
                if str(a.get("/Subtype")) == "/Link"]
    finally:
        pdf.close()


def import_part(bad: list, tmp: str) -> None:
    src = os.path.join(tmp, "workbook.pdf")
    workbook(src)

    # 1. tag it with the three passes off: this is the "tagged elsewhere" file
    plain = Options(detect_cross_references=False, detect_blank_lines=False,
                    join_spaced_letters=False, run_verapdf=False)
    first = os.path.join(tmp, "first")
    r1 = process_file(src, first, plain)
    if r1.get("status") != "done":
        bad.append(f"the first build failed: {r1.get('error')}")
        return
    tagged = os.path.join(first, "accessible.pdf")
    if links_of(tagged):
        bad.append("the file to import already has link annotations")
    if "Blank Line" in actual_texts(tagged):
        bad.append("the file to import already reads its write-in lines")

    # 2. feed it back in as an imported file, everything on
    job = os.path.join(tmp, "imported")
    r2 = process_file(tagged, job, Options(existing_tags="import", run_verapdf=False))
    if r2.get("status") != "done":
        bad.append(f"the import failed: {r2.get('error')}")
        return
    if r2.get("mode") != "import":
        bad.append(f"the tagged file was handled as {r2.get('mode')}, not imported")
        return

    fixes = r2.get("imported_fixes") or {}
    if fixes.get("blank_lines", 0) < 4:
        bad.append(f"write-in lines read on import: {fixes.get('blank_lines')} of 5")
    out = os.path.join(job, "accessible.pdf")
    if "Blank Line" not in actual_texts(out):
        bad.append("the imported file does not read any write-in line as Blank Line")

    m = load_model(job)
    spans = [n for n in m.nodes.values()
             if n.role == "Span" and (n.style or {}).get("blank_line")]
    if len(spans) < 4:
        bad.append(f"{len(spans)} blank-line spans in the imported model")
    hosts = {m.nodes[s.parent].role for s in spans if s.parent in m.nodes}
    if not hosts & {"LBody", "LI", "P", "TD"}:
        bad.append(f"blank-line spans ended up under {hosts}")
    left = blank_lines_missing(m)
    if left:
        bad.append(f"{len(left)} write-in line(s) still unread after the import")

    # 3. the contents entries are clickable, and they land on the right sheet
    made = ((r2.get("stats") or {}).get("xref_links") or {}).get("created", 0)
    if made < 2:
        bad.append(f"{made} link(s) created from the imported contents list")
    links = links_of(out)
    if len(links) < 2:
        bad.append(f"{len(links)} link annotation(s) in the imported file")
    tocis = [n for n in m.nodes.values() if n.role == "TOCI"]
    if len(tocis) < 2:
        bad.append(f"{len(tocis)} TOCI in the imported model")
    without = [n.id for n in tocis
               if not any(m.nodes[c].role == "Link" for c in n.children if c in m.nodes)]
    if without:
        bad.append(f"{len(without)} of {len(tocis)} contents entries have no Link")
    # printed page 4 is the second sheet
    pdf = pikepdf.open(out)
    try:
        pages = [p.obj.objgen for p in pdf.pages]
        got = []
        for p in pdf.pages:
            for a in (p.obj.get("/Annots") or []):
                if str(a.get("/Subtype")) != "/Link":
                    continue
                d = a.get("/Dest")
                if d is None and "/A" in a:
                    d = a.A.get("/D")
                if isinstance(d, pikepdf.Array) and len(d) and hasattr(d[0], "objgen") \
                        and d[0].objgen in pages:
                    got.append(pages.index(d[0].objgen))
        if sorted(got)[:2] != [1, 2]:
            bad.append(f"the contents entries jump to sheets {sorted(got)}, expected 1 and 2")
    finally:
        pdf.close()

    # 4. the figure with no alt text, and the caption printed under it
    want = alt_from_captions(m)
    if not want:
        bad.append("no figure was offered alt text from its caption")
    else:
        caps = [w["caption"] for w in want if "robin" in (w["caption"] or "")]
        if not caps:
            bad.append(f"the caption was not found: {[w['caption'] for w in want]}")
    # structure repair leaves a sound imported file alone
    if structure_problems(m):
        bad.append(f"the imported model has {len(structure_problems(m))} misplaced tags")


RUBRIC = ("Rubric Score Key",
          "1 and 2: Student has not achieved an appropriate level of fluency for the",
          "              level of the passage.",
          "3: Fluent reading is being refined at the level of the passage.",
          "4: Fluent reading has been achieved for the level at which the passage",
          "    is written.")


def rubric(path: str) -> None:
    """A fluency rubric: the score key is a list printed inside one cell."""
    d = pymupdf.open()
    p = d.new_page(width=560, height=320)
    x0, y0 = 40, 40
    cols = [x0, x0 + 150, x0 + 330, x0 + 500]
    rows = [y0, y0 + 20, y0 + 40, y0 + 60, y0 + 80, y0 + 170]
    for r in range(len(rows) - 2):
        for c in range(len(cols) - 1):
            p.draw_rect(pymupdf.Rect(cols[c], rows[r], cols[c + 1], rows[r + 1]),
                        color=(0, 0.4, 0.4), width=0.7)
    p.insert_text((cols[1] + 6, rows[0] + 14), "Rubric Score", fontsize=9)
    p.insert_text((cols[2] + 6, rows[0] + 14), "Comments:", fontsize=9)
    for i, name in enumerate(("Phrasing and Fluency", "Intonation", "Comprehension")):
        p.insert_text((cols[0] + 4, rows[i + 1] + 14), name, fontsize=9)
        p.insert_text((cols[1] + 20, rows[i + 1] + 14), "1      2      3      4", fontsize=9)
    p.draw_rect(pymupdf.Rect(cols[0], rows[4], cols[2], rows[5]), color=(0, 0.4, 0.4), width=0.7)
    p.draw_rect(pymupdf.Rect(cols[2], rows[4], cols[3], rows[5]), color=(0, 0.4, 0.4), width=0.7)
    y = rows[4] + 14
    for t in RUBRIC:
        p.insert_text((cols[0] + 4, y), t, fontsize=9)
        y += 15
    d.save(path)
    d.close()


def cell_list_part(bad: list, tmp: str) -> None:
    """A list printed inside one table cell — found automatically, and fixable."""
    from core.analyzer import analyze
    from core.repair import cell_lists, cells_with_lists

    src = os.path.join(tmp, "rubric.pdf")
    rubric(src)

    def items_of(model):
        out = []
        for n in model.nodes.values():
            if n.role != "L" or n.parent is None:
                continue
            if model.nodes[n.parent].role not in ("TD", "TH"):
                continue
            for c in n.children:
                li = model.nodes[c]
                lbl = next((model.nodes[k].text for k in li.children
                            if model.nodes[k].role == "Lbl"), None)
                body = next((model.nodes[k].text for k in li.children
                             if model.nodes[k].role == "LBody"), None)
                out.append((lbl, (body or "")[:14]))
        return out

    # 1. switched off: the cell is one block of text, and it is offered as a fix
    off = analyze(src, Options(lists_in_cells=False))
    if items_of(off):
        bad.append("a list was tagged inside a cell with the option off")
    want = cells_with_lists(off)
    if len(want) != 1:
        bad.append(f"{len(want)} cell(s) offered the fix, expected 1")
    r = cell_lists(off, src, Options(lists_in_cells=False))
    if r["fixed"] != 1 or r["left"]:
        bad.append(f"the fix reported {r}")
    got = items_of(off)
    if [g[0] for g in got] != ["1 and 2:", "3:", "4:"]:
        bad.append(f"the fix made {got}")
    elif not got[0][1].startswith("Student has"):
        bad.append(f"the item body starts {got[0][1]!r}")
    cell = off.nodes[off.nodes[[n.id for n in off.nodes.values()
                                if n.role == 'L' and n.parent][0]].parent]
    if not any(off.nodes[c].role == "P" and "Rubric Score Key" in off.nodes[c].text
               for c in cell.children):
        bad.append("the lead-in line did not become a P inside the cell")
    if cell.children and off.nodes[cell.children[0]].role != "P":
        bad.append("the list was put before the lead-in line")

    # 2. on by default: the same list is tagged during analysis, once
    on = analyze(src, Options())
    if [g[0] for g in items_of(on)] != ["1 and 2:", "3:", "4:"]:
        bad.append(f"analysis did not tag the list inside the cell: {items_of(on)}")
    if cells_with_lists(on):
        bad.append("a cell with a tagged list is still offered the fix")
    rows = [n for n in on.nodes.values() if n.role == "TR"]
    if len(rows) != 5:
        bad.append(f"the rubric table has {len(rows)} rows, expected 5")
    scores = [n.text for n in on.nodes.values() if n.role == "TD" and n.text.strip() == "1 2 3 4"]
    if len(scores) != 3:
        bad.append(f"{len(scores)} score cells kept their four numbers (expected 3)")

    # 3. and it survives the build
    job = os.path.join(tmp, "rubricjob")
    res = process_file(src, job, Options(run_verapdf=False))
    if res.get("status") != "done":
        bad.append(f"the rubric build failed: {res.get('error')}")
        return
    pdf = pikepdf.open(os.path.join(job, "accessible.pdf"))
    try:
        found = []

        def walk(e, parent=""):
            if isinstance(e, pikepdf.Array):
                for x in e:
                    walk(x, parent)
                return
            if isinstance(e, pikepdf.Dictionary) and "/S" in e:
                role = str(e.S)[1:]
                if role == "L" and parent in ("TD", "TH"):
                    found.append(role)
                k = e.get("/K")
                if k is not None:
                    walk(k, role)
        walk(pdf.Root.StructTreeRoot.get("/K"))
        if not found:
            bad.append("the built file has no list inside a table cell")
    finally:
        pdf.close()


def letter_grid(path: str) -> None:
    """A ruled alphabet grid whose rows are each ONE line of text."""
    d = pymupdf.open()
    p = d.new_page(width=420, height=330)
    fs, y0, ch = 22, 40, 46
    rows = ["ABCDE", "FGHIJ", "KLMNO", "PQRST", "UVWXY"]
    centres = []
    for r, row in enumerate(rows):
        p.insert_text((60, y0 + r * ch + 32), " ".join(row), fontsize=fs)
        if r == 0:
            cur = 60
            for c in " ".join(row):
                w = pymupdf.get_text_length(c, fontname="helv", fontsize=fs)
                if c != " ":
                    centres.append(cur + w / 2)
                cur += w
    bounds = ([centres[0] - 22]
              + [(centres[i] + centres[i + 1]) / 2 for i in range(len(centres) - 1)]
              + [centres[-1] + 22, centres[-1] + 66])
    for r in range(len(rows)):
        for c in range(len(bounds) - 1):
            p.draw_rect(pymupdf.Rect(bounds[c], y0 + r * ch, bounds[c + 1], y0 + (r + 1) * ch),
                        color=(0, 0.4, 0.4), width=0.7)
    d.save(path)
    d.close()


def reread_part(bad: list, tmp: str) -> None:
    """Reading each cell off the page again — the fix for a file tagged before."""
    from core.analyzer import analyze
    from core.repair import reread_cells

    src = os.path.join(tmp, "letters.pdf")
    letter_grid(src)
    m = analyze(src, Options())
    tables = [n for n in m.nodes.values() if n.role == "Table"]
    if not tables:
        bad.append("the letter grid was not found as a table")
        return
    want = [list(w) + [""] for w in ("ABCDE", "FGHIJ", "KLMNO", "PQRST", "UVWXY")]

    def grid():
        return [[m.nodes[c].text.strip() for c in r.children]
                for r in m.descendants(tables[0].id) if r.role == "TR"]

    if grid() != want:
        bad.append(f"the letters did not land one per cell: {grid()}")
        return
    # what a file tagged before this rule looks like: the whole row in one cell
    rows = [r for r in m.descendants(tables[0].id) if r.role == "TR"]
    for r in rows:
        kids = [m.nodes[c] for c in r.children]
        kids[3].text = " ".join(k.text for k in kids if k.text.strip())
        for k in kids:
            if k is not kids[3]:
                k.text = ""
    r1 = reread_cells(m, src, Options())
    if r1["changed"] < 20:
        bad.append(f"re-reading changed {r1['changed']} cell(s) of {r1['cells']}")
    if grid() != want:
        bad.append(f"re-reading did not put the letters back: {grid()}")
    # running it again changes nothing
    r2 = reread_cells(m, src, Options())
    if r2["changed"]:
        bad.append(f"a second re-read changed {r2['changed']} cell(s)")
    # and it leaves a cell that holds tags of its own alone
    rub = os.path.join(tmp, "rubric2.pdf")
    rubric(rub)
    mr = analyze(rub, Options())
    before = [(n.id, n.text) for n in mr.nodes.values() if n.role in ("TD", "TH")]
    rr = reread_cells(mr, rub, Options())
    after = [(n.id, n.text) for n in mr.nodes.values() if n.role in ("TD", "TH")]
    if before != after or rr["changed"]:
        bad.append(f"re-reading changed a table that was already right ({rr})")


def draw_in_cell_part(bad: list, tmp: str) -> None:
    """A tag drawn over the text in a cell belongs inside that cell."""
    from core import edit
    from core.analyzer import analyze

    src = os.path.join(tmp, "rubric3.pdf")
    rubric(src)
    m = analyze(src, Options(lists_in_cells=False))
    m.path = src
    cell = next((n for n in m.nodes.values()
                 if n.role in ("TD", "TH") and "Rubric Score Key" in (n.text or "")), None)
    if cell is None:
        bad.append("the rubric key cell was not found")
        return
    row = m.nodes[cell.parent]
    before = len(row.children)
    doc = pymupdf.open(src)
    r = pymupdf.Rect(cell.bbox) * doc[cell.page].transformation_matrix
    r.normalize()
    doc.close()
    pid = edit.create_from_rect(m, src, cell.page, (r.x0 + 1, r.y0 + 1, r.x1 - 1, r.y0 + 15),
                                "P", True, Options())
    lid = edit.create_from_rect(m, src, cell.page, (r.x0 + 1, r.y0 + 16, r.x1 - 1, r.y1 - 1),
                                "L", True, Options())
    for nid, what in ((pid, "paragraph"), (lid, "list")):
        par = m.nodes.get(m.nodes[nid].parent)
        if par is None or par.role not in ("TD", "TH"):
            bad.append(f"the drawn {what} went into {par.role if par else 'the page'}, not a cell")
    if m.nodes[pid].parent != m.nodes[lid].parent:
        bad.append("the paragraph and the list went into different cells")
    else:
        kids = m.nodes[m.nodes[pid].parent].children
        if kids.index(pid) > kids.index(lid):
            bad.append("the list was put before the paragraph it follows on the page")
    if len(row.children) != before:
        bad.append(f"the row now has {len(row.children)} cells, had {before}")
    if structure_problems(m):
        bad.append("drawing inside a cell left the structure wrong")
    items = [c for c in m.nodes[lid].children if m.nodes[c].role == "LI"]
    if len(items) != 3:
        bad.append(f"the drawn list has {len(items)} items, expected 3")

    # and a file where this already went wrong is repaired in place
    host = m.nodes[m.nodes[pid].parent]
    for nid in (pid, lid):                      # put them back in the row, as before
        n = m.nodes[nid]
        m.detach(nid)
        m.add(n, row.id, row.children.index(host.id))
    m.detach(host.id)
    if len(structure_problems(m)) != 2:
        bad.append(f"{len(structure_problems(m))} problems seen for P and L in a row")
    fixed = fix_structure(m)
    if fixed.get("TD") != 1:
        bad.append(f"the row content was not given a cell: {fixed}")
    par = m.nodes.get(m.nodes[pid].parent)
    if par is None or par.role != "TD" or m.nodes[par.parent].role != "TR":
        bad.append("the repaired cell is not in the row")
    elif [m.nodes[c].role for c in par.children] != ["P", "L"]:
        bad.append(f"the cell holds {[m.nodes[c].role for c in par.children]}")
    if structure_problems(m):
        bad.append("the repair left the structure wrong")


def tidy_and_empty_part(bad: list, tmp: str) -> None:
    """Wrapped lines back in their item, junk characters out, and an empty tag."""
    from core import edit
    from core.repair import junk_in_text, stray_list_items, tidy_lists

    m = DocModel(path="")
    L = StructNode(id="L", role="L", page=0, bbox=(0, 0, 300, 100))
    m.add(L, None)
    rows = [("1 and 2:", "\uf0b7Student has not achieved an appropriate level"),
            (None, "of the passage."),
            ("3:", "Fluent reading is being refined")]
    for i, (lbl, body) in enumerate(rows):
        li = StructNode(id=f"li{i}", role="LI", page=0, bbox=(0, 90 - i * 20, 300, 100 - i * 20))
        m.add(li, "L")
        if lbl:
            m.add(StructNode(id=f"lb{i}", role="Lbl", page=0, text=lbl,
                             bbox=(0, 90 - i * 20, 30, 100 - i * 20)), li.id)
        m.add(StructNode(id=f"bd{i}", role="LBody", page=0, text=body,
                         bbox=(30, 90 - i * 20, 300, 100 - i * 20)), li.id)
    if stray_list_items(m) != ["li1"]:
        bad.append(f"the wrapped line was not spotted: {stray_list_items(m)}")
    if junk_in_text(m) != ["bd0"]:
        bad.append(f"the junk character was not spotted: {junk_in_text(m)}")
    r = tidy_lists(m)
    if (r["merged"], r["cleaned"], r["left"]) != (1, 1, 0):
        bad.append(f"tidying reported {r}")
    items = [m.nodes[c] for c in m.nodes["L"].children]
    if len(items) != 2:
        bad.append(f"the list still has {len(items)} items, expected 2")
    else:
        body = m.nodes[next(c for c in items[0].children if m.nodes[c].role == "LBody")]
        if "of the passage." not in body.text:
            bad.append(f"the wrapped line was lost: {body.text!r}")
        if "\uf0b7" in body.text or body.text.startswith(" "):
            bad.append(f"the junk character is still there: {body.text!r}")
        if not body.parts:
            bad.append("the wrapped line's own zone was not kept")
    # a list with no numbers at all is left alone
    m2 = DocModel(path="")
    L2 = StructNode(id="L", role="L", page=0, bbox=(0, 0, 300, 100))
    m2.add(L2, None)
    for i in range(3):
        li = StructNode(id=f"li{i}", role="LI", page=0, bbox=(0, 90 - i * 20, 300, 100 - i * 20))
        m2.add(li, "L")
        m2.add(StructNode(id=f"bd{i}", role="LBody", page=0, text=f"item {i}",
                          bbox=(0, 90 - i * 20, 300, 100 - i * 20)), li.id)
    if stray_list_items(m2):
        bad.append("an unnumbered list was taken for wrapped lines")

    # an empty tag, added by hand
    row = StructNode(id="tr", role="TR", page=0, bbox=(0, 200, 300, 220))
    m.add(row, None)
    cell = StructNode(id="c1", role="TD", page=0, bbox=(0, 200, 150, 220), text="one")
    m.add(cell, "tr")
    nid = edit.create_empty(m, "TD", "c1", "after")
    new = m.nodes[nid]
    if new.parent != "tr" or m.nodes["tr"].children != ["c1", nid]:
        bad.append("the empty cell did not land next to the cell it was added to")
    if new.bbox is None:
        bad.append("the empty tag has no zone to drag")
    said = edit.create_empty(m, "P", "c1", "inside", "Blank Line")
    sp = m.nodes[said]
    if sp.parent != "c1":
        bad.append("an empty tag added inside a cell went somewhere else")
    if sp.actual_text != "Blank Line" or not (sp.style or {}).get("blank_line"):
        bad.append(f"the empty tag is not read as anything: {sp.actual_text!r} {sp.style}")


FLUENCY = [("Tim and Jim play ball. They", "__6"), ("run. They jump. They toss", "__11"),
           ("the ball. They kick the ball.", "__17"), ("They have fun.", "__20"),
           ("\u201cTime for bed!\u201d says Mom.", "__25"),
           ("\u201cCan we play more?\u201d asks Tim.", "__31")]


def fluency(path: str) -> None:
    """A fluency passage: every line is counted with a two-underscore tick."""
    d = pymupdf.open()
    p = d.new_page(width=420, height=300)
    y = 50
    for line, count in FLUENCY:
        p.insert_text((50, y), line, fontsize=11)
        p.insert_text((330, y), count, fontsize=11)
        y += 22
    d.save(path)
    d.close()


def short_blank_part(bad: list, tmp: str) -> None:
    """"__73" in the margin is a rule to write on, like a longer one."""
    from core.analyzer import analyze
    from core.repair import blank_line_spans, blank_lines_missing

    src = os.path.join(tmp, "fluency.pdf")
    fluency(src)
    m = analyze(src, Options())
    spans = [n for n in m.nodes.values()
             if n.role == "Span" and (n.style or {}).get("blank_line")]
    if len(spans) < len(FLUENCY):
        bad.append(f"{len(spans)} of {len(FLUENCY)} line counts read as a blank line")
    if blank_lines_missing(m):
        bad.append(f"{len(blank_lines_missing(m))} write-in tick(s) still unread")
    # and the fix finds them in a file tagged without the rule
    m2 = analyze(src, Options(detect_blank_lines=False))
    want = blank_lines_missing(m2)
    if len(want) < len(FLUENCY):
        bad.append(f"the fix offers {len(want)} of {len(FLUENCY)} lines")
    done = blank_line_spans(m2, src, Options())
    if done < len(FLUENCY):
        bad.append(f"the fix read {done} of {len(FLUENCY)} lines")
    if blank_lines_missing(m2):
        bad.append("the fix left a write-in tick unread")
    # the short tick is read as "Underscore", not as a line to write on
    if any(not (n.style or {}).get("tick") for n in spans):
        bad.append("a two-underscore tick was taken for a rule to write on")
    job = os.path.join(tmp, "fluencyjob")
    res = process_file(src, job, Options(run_verapdf=False))
    if res.get("status") != "done":
        bad.append(f"the fluency build failed: {res.get('error')}")
    else:
        said = actual_texts(os.path.join(job, "accessible.pdf"))
        if "Underscore" not in said:
            bad.append(f"the built file does not read the tick as Underscore: {sorted(said)}")
        if "Blank Line" in said:
            bad.append("a tick was read as Blank Line")
        if (res["stats"].get("underscore_ticks") or 0) < len(FLUENCY):
            bad.append(f"{res['stats'].get('underscore_ticks')} tick(s) in the built file")

    # a word with underscores in it is not a write-in line
    from core.repair import UNDERSCORE_RUN
    for word in ("snake_case_name", "x__y"):
        if UNDERSCORE_RUN.search(word):
            bad.append(f"{word!r} was taken for a rule to write on")


def _list_item(m, L, lbl, text, y, x=40.0):
    li = StructNode(id=m.new_id("li"), role="LI", page=0, bbox=(x, y, 400, y + 12))
    m.add(li, L)
    if lbl:
        m.add(StructNode(id=m.new_id("lbl"), role="Lbl", page=0, text=lbl,
                         bbox=(x, y, x + 10, y + 12)), li.id)
    m.add(StructNode(id=m.new_id("lb"), role="LBody", page=0, text=text,
                     bbox=(x + 12, y, 400, y + 12)), li.id)
    return li


def list_shape_part(bad: list) -> None:
    """One list with a note in it, and points set further in than their item."""
    from core.repair import loose_sub_items, split_lists, tidy_lists

    m = DocModel(path="")
    L1 = StructNode(id="L1", role="L", page=0, bbox=(40, 150, 400, 200))
    m.add(L1, None)
    for k, t in enumerate(("Say to the student", "Place the task card", "Point to the word")):
        _list_item(m, "L1", f"{k + 1}.", t, 190 - k * 15)
    m.add(StructNode(id="note", role="P", page=0, bbox=(54, 140, 400, 152),
                     text="To pace the student, you may want to…"), None)
    L2 = StructNode(id="L2", role="L", page=0, bbox=(40, 105, 400, 135))
    m.add(L2, None)
    for k, t in enumerate(("Record the responses", "Stop here")):
        _list_item(m, "L2", f"{k + 4}.", t, 125 - k * 13)
    if [p[0] for p in split_lists(m)] != ["L1"]:
        bad.append(f"the split list was not spotted: {split_lists(m)}")
    r = tidy_lists(m)
    if r["joined"] != 1:
        bad.append(f"the list was not joined: {r}")
    lists = [n for n in m.nodes.values() if n.role == "L"]
    if len(lists) != 1:
        bad.append(f"{len(lists)} lists left, expected 1")
    else:
        items = [m.nodes[c] for c in lists[0].children if m.nodes[c].role == "LI"]
        if len(items) != 5:
            bad.append(f"the joined list has {len(items)} items, expected 5")
        third = items[2] if len(items) > 2 else None
        body = next((m.nodes[c] for c in third.children
                     if m.nodes[c].role == "LBody"), None) if third else None
        if body is None or not any(m.nodes[c].role == "P" for c in body.children):
            bad.append("the note did not go inside the item it belongs to")

    # a dash under a bullet is a list inside that item
    m2 = DocModel(path="")
    L = StructNode(id="L", role="L", page=0, bbox=(40, 100, 400, 200))
    m2.add(L, None)
    _list_item(m2, "L", "\u2022", "score 20 or more", 190, 40)
    _list_item(m2, "L", "\u2022", "score fewer than 20", 175, 40)
    _list_item(m2, "L", "\u2013", "completed the easier set", 160, 62)
    _list_item(m2, "L", "\u2013", "not been assessed", 145, 62)
    _list_item(m2, "L", "\u2022", "stop", 130, 40)
    if len(loose_sub_items(m2)) != 2:
        bad.append(f"the indented points were not spotted: {loose_sub_items(m2)}")
    r2 = tidy_lists(m2)
    if r2["nested"] != 2:
        bad.append(f"the points were not moved into their item: {r2}")
    top = [m2.nodes[c] for c in m2.nodes["L"].children if m2.nodes[c].role == "LI"]
    if len(top) != 3:
        bad.append(f"the top list has {len(top)} items, expected 3")
    else:
        body = next((m2.nodes[c] for c in top[1].children if m2.nodes[c].role == "LBody"), None)
        subs = [m2.nodes[c] for c in (body.children if body else []) if m2.nodes[c].role == "L"]
        if len(subs) != 1 or len([c for c in subs[0].children
                                  if m2.nodes[c].role == "LI"]) != 2:
            bad.append("the two points are not one list inside the item above them")
    # numbers that simply carry on, indented, stay one flat list
    m3 = DocModel(path="")
    L3 = StructNode(id="L", role="L", page=0, bbox=(40, 100, 400, 200))
    m3.add(L3, None)
    _list_item(m3, "L", "1 and 2:", "has not achieved", 190, 40)
    _list_item(m3, "L", "3:", "is being refined", 175, 52)
    _list_item(m3, "L", "4:", "has been achieved", 160, 52)
    if loose_sub_items(m3):
        bad.append("a rubric that indents its later items was split into levels")


def main() -> int:
    bad: list = []
    structure_part(bad)
    phantom_row_part(bad)
    tmp = tempfile.mkdtemp()
    import_part(bad, tmp)
    cell_list_part(bad, tmp)
    reread_part(bad, tmp)
    draw_in_cell_part(bad, tmp)
    tidy_and_empty_part(bad, tmp)
    short_blank_part(bad, tmp)
    list_shape_part(bad)
    if bad:
        print("repair test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("repair test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
