"""Putting a structure right: tags that sit where they are not allowed, and
content that reads as nothing.

Three things live here, all of them safe to run on any model — one we built
ourselves, or one imported from a file that was tagged elsewhere:

  fix_structure(model)      a TOCI outside its TOC, an LI outside a list, a Lbl
                            or LBody outside its item, a row outside its table,
                            a cell outside its row — each goes back where PDF/UA
                            says it belongs, keeping its place in the reading
                            order
  blank_line_spans(...)     the write-in rule in "1. A ______ flew out of its
                            nest" becomes a Span read as "Blank Line"
  alt_from_captions(model)  a figure with no /Alt but a caption under it can be
                            given that caption, in one go

Nothing here changes what is printed; only how it is read.
"""
from __future__ import annotations

import re

from .model import DocModel, StructNode

# role -> the parents PDF/UA allows for it, and what to wrap it in when there is
# no such parent anywhere above
NEEDS: dict[str, tuple[str, ...]] = {
    "TOCI": ("TOC",),
    "LI": ("L",),
    "Lbl": ("LI",),
    "LBody": ("LI",),
    "TR": ("Table", "THead", "TBody", "TFoot"),
    "TD": ("TR",),
    "TH": ("TR",),
}
# a client house style (Kriyadocs) keeps a note's own mark in it: Note > Lbl
NEEDS_HOUSE: dict[str, tuple[str, ...]] = dict(NEEDS, Lbl=("LI", "Note"))


def _needs() -> dict[str, tuple[str, ...]]:
    from .compat import house
    return NEEDS_HOUSE if house() else NEEDS


WRAP: dict[str, str] = {"TOCI": "TOC", "LI": "L", "Lbl": "LI", "LBody": "LI",
                        "TR": "Table", "TD": "TR", "TH": "TR"}
# block-level elements: a container never belongs inside one of these
BLSE = {"P", "H1", "H2", "H3", "H4", "H5", "H6", "Lbl", "Caption", "Span",
        "Quote", "Note", "Reference", "BibEntry", "Formula", "Figure", "Title"}
# three or more underscores anywhere, or the two-underscore tick a fluency
# passage prints before its line count ("__73") — never inside a word
UNDERSCORE_RUN = re.compile(r"_{3,}|(?<![^\W_])_{2}(?!_)")


def dedupe_cell_figures(model: DocModel, tables: set | None = None) -> int:
    """A picture printed in a table cell is tagged once, inside its cell
    (V4.19.2). The analysis tagged such a picture twice — a Figure in the cell
    and the same Figure again outside the table — so the outside one goes;
    what was written on it (alt text, actual text) is kept on the one in the
    cell when that has none."""
    def iou(a, b):
        ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
        iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
        inter = ix * iy
        ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
        return inter / ua if ua > 0 else 0.0
    inside: list[StructNode] = []
    outside: list[StructNode] = []
    for n in model.nodes.values():
        if n.role != "Figure" or n.bbox is None:
            continue
        anc = model.ancestors(n.id)
        cell = next((a for a in anc if a.role in ("TD", "TH")), None)
        if cell is not None:
            t = next((a for a in anc if a.role == "Table"), None)
            if tables is None or (t is not None and t.id in tables):
                inside.append(n)
        elif not any(a.role == "Table" for a in anc):
            outside.append(n)
    gone = 0
    for o in outside:
        if o.id not in model.nodes or o.children:
            continue
        twin = next((f for f in inside if f.page == o.page and iou(f.bbox, o.bbox) > 0.6), None)
        if twin is None:
            continue
        for k in ("alt", "actual_text", "lang"):
            if not getattr(twin, k) and getattr(o, k):
                setattr(twin, k, getattr(o, k))
        if o.artifact and not twin.alt:
            twin.artifact = True
        model.remove(o.id, promote_children=False)
        gone += 1
    return gone


def fix_structure(model: DocModel, limit: int = 4000) -> dict:
    """Every tag under a parent PDF/UA allows, without moving anything on the page.

    A file tagged elsewhere often has a contents entry sitting in a paragraph or
    a stray list item at section level; PAC fails those as structure errors. The
    tag is not moved up or down the reading order — it is wrapped where it
    stands, or joined to the container just before it when that is the right
    kind."""
    moved: dict[str, int] = {}
    twins = dedupe_cell_figures(model)
    if twins:
        moved["duplicate_figures"] = twins
    # a row left with no cell of its own (merged cells cover it) goes; the
    # merged cells span one row less (V4.19.2)
    empty_rows = 0
    for t in [n for n in list(model.nodes.values()) if n.role == "Table"]:
        rows = [d for d in model.descendants(t.id) if d.role == "TR"]
        if any(not any(model.nodes.get(c) is not None and model.nodes[c].role in ("TD", "TH") for c in r.children)
               for r in rows):
            try:
                from .tableedit import Grid
                g = Grid(model, t.id)
                before = len(g.rows)
                g.write()
                empty_rows += before - len(g.rows)
            except Exception:
                continue
    if empty_rows:
        moved["empty_rows"] = empty_rows
    rounds = 0
    while rounds < 6:
        rounds += 1
        changed = 0
        parents: list[tuple[str | None, list[str]]] = [(None, model.root_children)]
        parents += [(n.id, n.children) for n in list(model.nodes.values()) if n.children]
        for pid, _kids in parents:
            parent = model.nodes.get(pid) if pid else None
            prole = parent.role if parent is not None else "Document"
            i = 0
            kids = parent.children if parent is not None else model.root_children
            while i < len(kids):
                nid = kids[i]
                n = model.nodes.get(nid)
                if n is None or n.artifact or n.role not in NEEDS:
                    i += 1
                    continue
                if prole in _needs()[n.role]:
                    i += 1
                    continue
                want = WRAP[n.role]
                # the run of neighbours that needs the same home
                run = [nid]
                j = i + 1
                while j < len(kids):
                    nx = model.nodes.get(kids[j])
                    if nx is None or nx.role not in WRAP or WRAP[nx.role] != want \
                            or prole in _needs().get(nx.role, ()):
                        break
                    run.append(kids[j])
                    j += 1
                # a list or a contents list may not live inside a paragraph or a
                # heading either, so it is lifted out next to the block it sat in
                host_pid, pos = pid, i
                if prole in BLSE and parent is not None:
                    gp = parent.parent
                    sibs = model.nodes[gp].children if gp else model.root_children
                    host_pid = gp
                    pos = (sibs.index(parent.id) + 1) if parent.id in sibs else len(sibs)
                host = None
                where = (model.nodes[host_pid].children if host_pid else model.root_children)
                if pos > 0 and pos - 1 < len(where):      # join the container before it
                    prev = model.nodes.get(where[pos - 1])
                    if prev is not None and prev.role == want and not prev.artifact:
                        host = prev
                if host is None:
                    host = StructNode(id=model.new_id(want.lower()), role=want,
                                      page=n.page, bbox=n.bbox, source=n.source,
                                      note=f"{n.role} put back inside a {want}")
                    model.add(host, host_pid, pos)
                    kids = parent.children if parent is not None else model.root_children
                for rid in run:
                    child = model.nodes[rid]
                    model.detach(rid)
                    model.add(child, host.id)
                    host.bbox = _union(host.bbox, child.bbox)
                    moved[child.role] = moved.get(child.role, 0) + 1
                    changed += 1
                    if sum(moved.values()) >= limit:
                        return moved
                kids = parent.children if parent is not None else model.root_children
                i = max(0, i)
        if not changed:
            break
    cells = _cells_for_rows(model)
    if cells:
        moved["TD"] = moved.get("TD", 0) + cells
    if moved:
        model.info["structure_fixed"] = dict(moved)
    return moved


def _cells_for_rows(model: DocModel) -> int:
    """Anything printed in a row but not in a cell gets a cell of its own.

    A paragraph and a list drawn by hand over the text of a cell used to replace
    that cell, leaving `TR > P` and `TR > L` — content a row may not hold, and
    read outside the table. The run of them goes into one `TD` where the cell
    was, so the reading order and the row's shape both stay as they are."""
    made = 0
    for tr in [n for n in model.nodes.values() if n.role == "TR" and not n.artifact]:
        i = 0
        while i < len(tr.children):
            kid = model.nodes.get(tr.children[i])
            if kid is None or kid.role in ("TD", "TH") or kid.artifact:
                i += 1
                continue
            run = [kid.id]
            j = i + 1
            while j < len(tr.children):
                nx = model.nodes.get(tr.children[j])
                if nx is None or nx.role in ("TD", "TH"):
                    break
                run.append(nx.id)
                j += 1
            cell = StructNode(id=model.new_id("cell"), role="TD", page=kid.page, bbox=kid.bbox,
                              source=kid.source,
                              note="content found straight in the row — given a cell of its own")
            model.add(cell, tr.id, i)
            for rid in run:
                c = model.nodes[rid]
                model.detach(rid)
                model.add(c, cell.id)
                cell.bbox = _union(cell.bbox, c.bbox)
            made += 1
            i += 1
    return made


def structure_problems(model: DocModel) -> list[str]:
    """The tags fix_structure would move."""
    out: list[str] = []
    for n in model.nodes.values():
        if n.artifact:
            continue
        parent = model.nodes.get(n.parent) if n.parent else None
        prole = parent.role if parent is not None else "Document"
        if n.role in NEEDS:
            if prole not in _needs()[n.role]:
                out.append(n.id)
        elif prole == "TR":
            out.append(n.id)        # a row may hold cells and nothing else
    return out


def _union(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def blank_lines_missing(model: DocModel) -> list[str]:
    """Tags whose text carries a write-in rule that is not read as one yet."""
    out: list[str] = []
    for n in model.nodes.values():
        if n.artifact or not UNDERSCORE_RUN.search(n.text or ""):
            continue
        style = n.style or {}
        if style.get("blank_line") or style.get("underscores") or _blank_span(n):
            continue                       # this *is* the blank line (ours, or imported)
        if any(_blank_span(model.nodes[c]) for c in n.children if c in model.nodes):
            continue
        out.append(n.id)
    return out


def blank_line_spans(model: DocModel, working_pdf: str, opts=None, ids=None) -> int:
    """A write-in rule is read as "Blank Line", wherever it is printed.

    "1. A ______ flew out of its nest." — the underscores are a space for the
    child to write in, and a screen reader says nothing useful for them. Each run
    becomes a `Span` whose /ActualText is the blank-line wording, inside the tag
    that holds it, so the sentence reads "A Blank Line flew out of its nest". The
    tag may be a paragraph, a list item's body or a table cell; the page itself
    is not touched."""
    import pymupdf

    from .analyzer import Analyzer, collect_lines
    from .options import Options

    o = opts or Options()
    want = ids if ids is not None else blank_lines_missing(model)
    if not want:
        return 0
    a = Analyzer(working_pdf, o)
    a.model = model
    done = 0
    try:
        a.page_lines = [[] for _ in range(a.doc.page_count)]
        for nid in want:
            n = model.nodes.get(nid)
            if n is None or n.page < 0 or n.page >= a.doc.page_count or n.bbox is None:
                continue
            page = a.doc[n.page]
            if not a.page_lines[n.page]:
                a.page_lines[n.page] = collect_lines(page)
            r = pymupdf.Rect(n.bbox) * page.transformation_matrix
            r.normalize()
            group = [l for l in a.page_lines[n.page]
                     if r.x0 - 1 <= (l.x0 + l.x1) / 2 <= r.x1 + 1
                     and r.y0 - 1 <= (l.y0 + l.y1) / 2 <= r.y1 + 1
                     and UNDERSCORE_RUN.search(l.text or "")]
            if not group:
                continue
            before = len(n.children)
            a._underscore_spans(page, n.page, n, group)
            if len(n.children) > before:
                # in an imported file every glyph already belongs to the tag it
                # was imported into, so the underscores would stay with the
                # sentence and the new Span would be dropped as empty: its
                # geometry has to win, the way a zone drawn by hand does
                for cid in n.children[before:]:
                    sp = model.nodes.get(cid)
                    if sp is not None:
                        sp.priority = 1
                        sp.source = "auto"
                done += 1
        return done
    finally:
        a.doc.close()
        a.pdf.close()


def alt_from_captions(model: DocModel) -> list[dict]:
    """Figures with no /Alt, and the caption printed with them."""
    out: list[dict] = []
    nodes = model.ordered_nodes()
    for i, n in enumerate(nodes):
        if n.role != "Figure" or n.artifact or (n.alt or "").strip() or (n.actual_text or "").strip():
            continue
        caption = ""
        for k in (i + 1, i - 1):
            if 0 <= k < len(nodes) and nodes[k].role == "Caption" and (nodes[k].text or "").strip():
                caption = nodes[k].text.strip()
                break
        if not caption:
            for c in n.children:
                kid = model.nodes.get(c)
                if kid is not None and kid.role == "Caption" and (kid.text or "").strip():
                    caption = kid.text.strip()
                    break
        out.append({"id": n.id, "page": n.page, "caption": caption[:300]})
    return out


def polish_imported(model: DocModel, working_pdf: str, opts) -> dict:
    """What an imported structure usually still needs: write-in rules read as
    blank lines, and letter-spaced lines read as the words they spell."""
    from .actualtext import looks_spaced, page_vocabulary, reads_as_words, spaced_words

    done = {"blank_lines": 0, "spaced_lines": 0, "prompt_rows": 0}
    if getattr(opts, "detect_blank_lines", True):
        try:
            # "Name: ____  Date: ____" tagged as one P, or a blank that took the
            # ":" of its label: rebuilt from the page, one P per prompt
            done["prompt_rows"] = fix_prompt_rows(model, working_pdf, opts).get("rows", 0)
        except Exception:
            pass
        try:
            done["blank_lines"] = blank_line_spans(model, working_pdf, opts)
        except Exception:
            pass
    if getattr(opts, "join_spaced_letters", True):
        import pymupdf

        doc = pymupdf.open(working_pdf)
        try:
            vocab: dict[int, set] = {}
            for n in model.nodes.values():
                if n.artifact or (n.actual_text or "").strip() or not looks_spaced(n.text or ""):
                    continue
                if n.page < 0 or n.page >= doc.page_count or n.bbox is None:
                    continue
                page = doc[n.page]
                if n.page not in vocab:
                    vocab[n.page] = page_vocabulary(page)
                r = pymupdf.Rect(n.bbox) * page.transformation_matrix
                r.normalize()
                from .actualtext import glyphs
                gs = glyphs(page, (r.x0, r.y0, r.x1, r.y1))
                said = spaced_words([(g[0], g[2]) for g in gs], vocab[n.page])
                if said and reads_as_words(said, vocab[n.page]):
                    n.actual_text = said
                    n.note = (n.note + "; " if n.note else "") + \
                        "letters set apart — read as the words they spell"
                    done["spaced_lines"] += 1
        finally:
            doc.close()
    return done


def _cover(box, others: list) -> float:
    """How much of box is taken by the boxes in others (0 … 1)."""
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return 0.0
    cuts = sorted({x0, x1} | {min(max(b[0], x0), x1) for b in others}
                  | {min(max(b[2], x0), x1) for b in others})
    taken = 0.0
    for i in range(len(cuts) - 1):
        a, b = cuts[i], cuts[i + 1]
        if b - a <= 0:
            continue
        mid = (a + b) / 2
        spans = sorted((max(r[1], y0), min(r[3], y1)) for r in others
                       if r[0] <= mid <= r[2] and min(r[3], y1) > max(r[1], y0))
        hi = y0
        for s0, s1 in spans:
            if s1 <= hi:
                continue
            hi = max(hi, s0)
            taken += (b - a) * (s1 - hi)
            hi = s1
    return taken / ((x1 - x0) * (y1 - y0))


def drop_duplicate_rows(model: DocModel, least: float = 0.85) -> int:
    """A blank row printed on top of the row that is really there.

    Reading a ruled worksheet, the same band can be found twice: once as the row
    the ruling draws and once as an answer band stacked under the row above it.
    The second one has nothing printed in it and sits on cells that do, so its
    cells can never be given any content — PAC then reports them as elements
    with nothing associated. The blank copy goes."""
    gone = 0
    for tb in [n for n in model.nodes.values() if n.role == "Table"]:
        rows = [model.nodes[c] for c in list(tb.children) if c in model.nodes]
        rows = [r for r in rows if r.role in ("TR", "THead", "TBody", "TFoot")]
        flat: list[StructNode] = []
        for r in rows:
            if r.role == "TR":
                flat.append(r)
            else:
                flat += [model.nodes[c] for c in r.children
                         if c in model.nodes and model.nodes[c].role == "TR"]
        cells = {tr.id: [model.nodes[c] for c in tr.children if c in model.nodes] for tr in flat}
        for tr in flat:
            mine = cells.get(tr.id) or []
            if not mine or any((c.text or "").strip() or c.children or c.src_keys or c.annots
                               for c in mine):
                continue
            boxes = [c.bbox for c in mine if c.bbox]
            if not boxes:
                continue
            box = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                   max(b[2] for b in boxes), max(b[3] for b in boxes))
            # the row it is printed on top of is often read as a table of its
            # own, so every cell on the page counts, not only this table's
            own = {c.id for c in mine}
            others = [c.bbox for c in model.nodes.values()
                      if c.role in ("TD", "TH") and not c.artifact and c.bbox
                      and c.page == tr.page and c.id not in own]
            if _cover(box, others) >= least:
                model.detach(tr.id)
                gone += 1
    # a table left with no rows at all is the duplicate itself
    for tb in [n for n in model.nodes.values() if n.role == "Table"]:
        kids = [model.nodes[c] for c in tb.children if c in model.nodes]
        rows = [k for k in kids if k.role == "TR"]
        rows += [model.nodes[c] for k in kids if k.role in ("THead", "TBody", "TFoot")
                 for c in k.children if c in model.nodes and model.nodes[c].role == "TR"]
        if not rows:
            model.detach(tb.id)
    if gone:
        model.info["duplicate_rows_dropped"] = gone
    return gone


CELL_LABEL = re.compile(
    r"(?:^|\s)(?:[•●▪■◦○]|\(?\d{1,3}[.)]|\d{1,3}(?:\s*(?:and|&|to|or|[–—-])\s*\d{1,3})?:)(?=\s)")


def cells_with_lists(model: DocModel) -> list[str]:
    """Table cells whose text reads like a list but holds no list.

    "Rubric Score Key  1 and 2: …  3: …  4: …" is three graded items printed in
    one cell; as one block of text a screen reader gives no sense of that."""
    out: list[str] = []
    for n in model.nodes.values():
        if n.role not in ("TD", "TH") or n.artifact or n.children:
            continue
        text = " " + (n.text or "").strip()
        if len(text) < 20:
            continue
        if len(CELL_LABEL.findall(text)) >= 2:
            out.append(n.id)
    return out


def cell_lists(model: DocModel, working_pdf: str, opts=None, ids=None) -> dict:
    """Tag the list printed inside a table cell, cell by cell.

    The cell keeps its place in the table; inside it the lead-in line becomes a
    `P` and the items an `L` > `LI` > `Lbl` + `LBody`. Only a cell whose lines
    really are labelled items — same kind of label, same margin, numbers going
    up — is changed; anything else is left exactly as it is."""
    import pymupdf

    from .analyzer import Analyzer, collect_lines
    from .options import Options

    o = opts or Options()
    want = ids if ids is not None else cells_with_lists(model)
    if not want:
        return {"fixed": 0, "left": 0}
    a = Analyzer(working_pdf, o)
    a.model = model
    done = 0
    try:
        a.page_lines = [[] for _ in range(a.doc.page_count)]
        for nid in want:
            n = model.nodes.get(nid)
            if n is None or n.children or n.bbox is None \
                    or not (0 <= n.page < a.doc.page_count):
                continue
            page = a.doc[n.page]
            if not a.page_lines[n.page]:
                a.page_lines[n.page] = collect_lines(page)
            r = pymupdf.Rect(n.bbox) * page.transformation_matrix
            r.normalize()
            group = [l for l in a.page_lines[n.page]
                     if (l.text or "").strip()
                     and r.x0 - 1 <= (l.x0 + l.x1) / 2 <= r.x1 + 1
                     and r.y0 - 1 <= (l.y0 + l.y1) / 2 <= r.y1 + 1]
            if len(group) >= 3 and a.cell_list(page, n.page, n, group):
                done += 1
        return {"fixed": done, "left": len(cells_with_lists(model))}
    finally:
        a.doc.close()
        a.pdf.close()


def reread_cells(model: DocModel, working_pdf: str, opts=None, ids=None, only=None) -> dict:
    """Read every table cell's text off the page again, cell by cell.

    A row of a letter grid is often one line of text ("M N O") even though each
    letter is printed in its own ruled cell. A file tagged before that was
    understood has the whole row in the cell its middle fell in, and the cells
    beside it empty — so they are read "Blank cell" over letters plainly printed
    there. Nothing is moved: each cell simply takes the words printed inside it,
    a line being cut at the cell borders word by word, never inside a word.

    `ids` limits it to those tables; cells that already hold tags of their own
    (a list inside a cell) are left alone. `only`: just these cells take their
    words again (Edit table, V4.18) — the others still set the borders."""
    import pymupdf

    from .analyzer import _center_in, _line_pieces, collect_lines
    from .options import Options

    tables = [n for n in model.nodes.values()
              if n.role == "Table" and not n.artifact and (ids is None or n.id in ids)]
    if not tables:
        return {"changed": 0, "tables": 0, "cells": 0}
    o = opts or Options()
    doc = pymupdf.open(working_pdf)
    page_lines: dict[int, list] = {}
    changed = seen = 0
    try:
        for tb in tables:
            cells = [d for d in model.descendants(tb.id)
                     if d.role in ("TD", "TH") and not d.artifact and d.bbox and not d.children]
            by_page: dict[int, list] = {}
            for c in cells:
                if 0 <= c.page < doc.page_count:
                    by_page.setdefault(c.page, []).append(c)
            for pno, group in by_page.items():
                page = doc[pno]
                if pno not in page_lines:
                    page_lines[pno] = collect_lines(page)
                mat = page.transformation_matrix
                box_of = {}
                for c in group:
                    r = pymupdf.Rect(c.bbox) * mat
                    r.normalize()
                    box_of[c.id] = (r.x0, r.y0, r.x1, r.y1)
                boxes = list(box_of.values())
                inside = [l for l in page_lines[pno] if (l.text or "").strip()
                          and any(_center_in(l.bbox, b, 1.0) for b in boxes)]
                hits: dict[tuple, list] = {}
                for l in inside:
                    pieces = _line_pieces(l, boxes)
                    if pieces:
                        for b, t in pieces:
                            hits.setdefault(b, []).append((l, t))
                    else:
                        home = [b for b in boxes if _center_in(l.bbox, b)]
                        if home:
                            b = min(home, key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))
                            hits.setdefault(b, []).append((l, l.text))
                for c in group:
                    if only is not None and c.id not in only:
                        continue
                    seen += 1
                    got = hits.get(box_of[c.id]) or []
                    text = " ".join(t for _, t in sorted(got, key=lambda it: (it[0].y0, it[0].x0))).strip()
                    if text != (c.text or "").strip():
                        c.text = text
                        c.note = (c.note + "; " if c.note else "") + "text read off the page again"
                        changed += 1
        return {"changed": changed, "tables": len(tables), "cells": seen}
    finally:
        doc.close()


# characters that are not really printed: controls, joiners, soft hyphens, the
# replacement character, and the private-use codes a symbol font maps its
# bullets to — a screen reader reads them as nothing, or as "unknown"
TEXT_JUNK = re.compile(
    r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f­؜᠎"
    r"​-‏‪-‮⁠-⁤﻿￹-�-]")


def _clean(text: str) -> str:
    out = TEXT_JUNK.sub(" ", text or "")
    return re.sub(r"\s{2,}", " ", out).strip()


def junk_in_text(model: DocModel) -> list[str]:
    """Tags whose text carries characters that are not really printed."""
    return [n.id for n in model.nodes.values()
            if not n.artifact and (TEXT_JUNK.search(n.text or "")
                                   or TEXT_JUNK.search(n.actual_text or "")
                                   or TEXT_JUNK.search(n.alt or ""))]


def stray_list_items(model: DocModel) -> list[str]:
    """List items that are really the wrapped rest of the item before them.

    "1 and 2: Student has not achieved an appropriate level of fluency for the
    level of the passage." is one item printed over two lines. Converted line by
    line, the second line becomes an item of its own with no number at all —
    the list then counts six items where the page shows three."""
    out: list[str] = []
    for L in [n for n in model.nodes.values() if n.role == "L" and not n.artifact]:
        items = [model.nodes[c] for c in L.children
                 if c in model.nodes and model.nodes[c].role == "LI"]
        has_lbl = [any(model.nodes[k].role == "Lbl" for k in it.children if k in model.nodes)
                   for it in items]
        if not any(has_lbl):
            continue                      # a list with no numbers at all is fine
        for i, it in enumerate(items):
            if i and not has_lbl[i] and has_lbl[i - 1] and \
                    not any(model.nodes[k].role in ("L", "Table") for k in it.children
                            if k in model.nodes):
                out.append(it.id)
    return out


def tidy_lists(model: DocModel, ids=None, working_pdf: str | None = None, opts=None) -> dict:
    """Wrapped lines go back into the item they belong to, and tag text loses
    the characters that are not really printed.

    Nothing is dropped: the wrapped line keeps its own zone, so the words stay
    tagged exactly where they are printed — they are simply read as part of the
    item, which is what the page shows."""
    merged = 0
    for nid in (ids if ids is not None else stray_list_items(model)):
        it = model.nodes.get(nid)
        if it is None or it.parent is None:
            continue
        kids = [model.nodes[c] for c in it.parent and model.nodes[it.parent].children or []
                if c in model.nodes and model.nodes[c].role == "LI"]
        try:
            prev = kids[kids.index(it) - 1]
        except (ValueError, IndexError):
            continue
        body = next((model.nodes[c] for c in prev.children
                     if c in model.nodes and model.nodes[c].role == "LBody"), None)
        if body is None or prev is it:
            continue
        for c in list(it.children):
            part = model.nodes.get(c)
            if part is None:
                continue
            text = (part.text or "").strip()
            if text:
                body.text = ((body.text or "").rstrip() + " " + text).strip()
            if part.role in ("LBody", "P"):
                model.absorb(body.id, part.id)
            else:
                model.detach(c)
                model.add(part, body.id)
                body.bbox = _union(body.bbox, part.bbox)
        prev.bbox = _union(prev.bbox, it.bbox)
        model.remove(it.id, promote_children=False)
        merged += 1

    values = reread = 0
    if working_pdf:
        try:
            values = fix_list_values(model, working_pdf)
        except Exception:
            values = 0
        # a list carried over a page break by hand, its second part never read
        from .edit import _rebuild_list_across_pages
        from .options import Options
        for lid in lists_read_short(model):
            try:
                if _rebuild_list_across_pages(model, working_pdf, lid, opts or Options()):
                    reread += 1
            except Exception:
                pass
    shaped = fix_list_children(model)
    joined = join_split_lists(model)
    nested = nest_sub_items(model)

    cleaned = 0
    for n in list(model.nodes.values()):
        if n.artifact:
            continue
        before = (n.text, n.actual_text, n.alt)
        n.text = _clean(n.text) if TEXT_JUNK.search(n.text or "") else n.text
        if TEXT_JUNK.search(n.actual_text or ""):
            n.actual_text = _clean(n.actual_text)
        if TEXT_JUNK.search(n.alt or ""):
            n.alt = _clean(n.alt)
        if (n.text, n.actual_text, n.alt) != before:
            cleaned += 1
    return {"merged": merged, "cleaned": cleaned, "joined": joined, "nested": nested,
            "values": values, "shaped": shaped, "reread": reread,
            "left": len(stray_list_items(model)) + len(junk_in_text(model))
                    + len(list_children_wrong(model))
                    + (len(lists_read_short(model)) if working_pdf else 0)
                    + len(split_lists(model)) + len(loose_sub_items(model))
                    + (len(list_values_apart(model)) if working_pdf else 0)}


def _item_label(model: DocModel, li: StructNode) -> str:
    for c in li.children:
        k = model.nodes.get(c)
        if k is not None and k.role == "Lbl":
            return (k.text or "").strip()
    return ""


def _items(model: DocModel, L: StructNode) -> list:
    return [model.nodes[c] for c in L.children
            if c in model.nodes and model.nodes[c].role == "LI"]


def _follows(model: DocModel, first: str, second: str) -> bool:
    """Does the label `second` carry on from `first`? (3 then 4, b then c, • then •)"""
    from .analyzer import Analyzer
    if not first or not second:
        return False
    k1, v1 = Analyzer._label_value(first)
    k2, v2 = Analyzer._label_value(second)
    if Analyzer._numbering(first) != Analyzer._numbering(second):
        return False
    if k1 != k2:
        return False
    if v1 is None or v2 is None:          # bullets: the same mark carries on
        return first.strip() == second.strip()
    return v2 == v1 + 1


INSIDE_ROLES = {"P", "L", "Note", "Quote", "BlockQuote", "Caption", "Span", "Div", "Figure"}


def _lbl_node(model: DocModel, li: StructNode):
    return next((model.nodes[c] for c in li.children
                 if c in model.nodes and model.nodes[c].role == "Lbl"), None)


def _overlap(a, b) -> float:
    """How much of box a's width lies over box b (0 … 1)."""
    if not a or not b:
        return 0.0
    w = max(1e-6, a[2] - a[0])
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) / w


def _plan_join(model: DocModel, kids: list, i: int):
    """If the list at kids[i] carries on further down, how: (L2, inside, other).

    `inside` are the tags between the two parts that belong in the last item —
    a note, a paragraph that runs on from the page before, a list of points —
    recognisable because they sit in the same column, set in past the item
    numbers. `other` are tags the reading order happened to put between them
    that are printed in another column; they are not part of the list."""
    L1 = kids[i]
    items = _items(model, L1)
    if not items or _lbl_node(model, items[-1]) is None:
        return None
    last_lbl = _item_label(model, items[-1])
    # find the part that carries on
    j, L2 = i + 1, None
    while j < len(kids) and j <= i + 14:
        x = kids[j]
        if x.role == "L" and not x.artifact:
            nxt = _items(model, x)
            if nxt and _follows(model, last_lbl, _item_label(model, nxt[0])):
                L2 = x
                break
        j += 1
    if L2 is None:
        return None
    first = _items(model, L2)[0]
    l2_lbl = _lbl_node(model, first)
    l1_lbl = _lbl_node(model, items[-1])
    if l2_lbl is None or l2_lbl.bbox is None or l1_lbl is None or l1_lbl.bbox is None:
        return None
    # two lists on one page carry on only down the same column
    if L1.page == L2.page and _overlap(L2.bbox, L1.bbox) < 0.5 and _overlap(L1.bbox, L2.bbox) < 0.5:
        return None
    if L2.page not in (L1.page, L1.page + 1):
        return None
    inside, other = [], []
    for x in kids[i + 1:j]:
        if x.artifact or (x.style or {}).get("page_number"):
            continue                 # a page number stays where it is printed
        if x.page == L2.page:
            ref_x, col = l2_lbl.bbox[0], L2.bbox
        elif x.page == L1.page:
            ref_x, col = l1_lbl.bbox[0], items[-1].bbox or L1.bbox
        else:
            return None
        if not x.bbox:
            return None
        same_col = _overlap(x.bbox, col) >= 0.5
        if same_col and x.role in INSIDE_ROLES and x.bbox[0] >= ref_x + 4:
            inside.append(x)
        elif not same_col and _overlap(x.bbox, col) == 0.0:
            other.append(x)
        else:
            return None              # a heading, or text at the item margin: two lists
    return L2, inside, other


def split_lists(model: DocModel) -> list[tuple[str, str]]:
    """Lists broken in two by what belongs inside them.

    "3. Point to the first nonsense word …" is followed by a note set at the
    item's own indent — no number of its own — and then "4. Record the student's
    responses …". Read line by line that note ends the list, and item 4 starts a
    second list of its own that counts from 4. The same happens when an item
    runs on to the next page, or holds a list of points of its own. The page
    shows one list."""
    out: list[tuple[str, str]] = []
    for parent_children in [model.root_children] + [n.children for n in model.nodes.values()]:
        kids = [model.nodes[c] for c in parent_children if c in model.nodes]
        for i, n in enumerate(kids):
            if n.role != "L" or n.artifact:
                continue
            plan = _plan_join(model, kids, i)
            if plan is not None:
                out.append((n.id, plan[0].id))
    return out


def join_split_lists(model: DocModel, focus: set | None = None) -> int:
    """Put a list broken in two back together, notes and points and all.

    The tags between the two parts that belong in the last item go into its
    body, in the order they are read; tags from another column that the reading
    order had put in between are moved out of the way — before the list when
    that column is to its left on the same page, after it otherwise."""
    joined = 0
    for _ in range(20):
        done = False
        for parent_children in [model.root_children] + [n.children for n in list(model.nodes.values())]:
            kids = [model.nodes[c] for c in parent_children if c in model.nodes]
            for i, L1 in enumerate(kids):
                if L1.role != "L" or L1.artifact:
                    continue
                plan = _plan_join(model, kids, i)
                if plan is None:
                    continue
                L2, inside, other = plan
                if focus is not None and L1.id not in focus and L2.id not in focus:
                    continue
                sibs = parent_children
                items = _items(model, L1)
                body = next((model.nodes[c] for c in items[-1].children
                             if c in model.nodes and model.nodes[c].role == "LBody"), None)
                if body is None:
                    continue
                for x in inside:
                    model.detach(x.id)
                    model.add(x, body.id)
                    if x.page == body.page:
                        body.bbox = _union(body.bbox, x.bbox)
                        items[-1].bbox = _union(items[-1].bbox, x.bbox)
                    if x.page == L1.page:
                        L1.bbox = _union(L1.bbox, x.bbox)
                for it in _items(model, L2):
                    model.detach(it.id)
                    model.add(it, L1.id)
                    if it.page == L1.page:
                        L1.bbox = _union(L1.bbox, it.bbox)
                after = 0
                for x in other:
                    model.detach(x.id)
                    left = (x.page == L1.page and x.bbox and L1.bbox and x.bbox[2] <= L1.bbox[0] + 1)
                    if left:
                        model.add(x, L1.parent, sibs.index(L1.id))
                    else:
                        after += 1
                        model.add(x, L1.parent, sibs.index(L1.id) + after)
                if not L2.children:
                    model.detach(L2.id)
                    model.nodes.pop(L2.id, None)
                joined += 1
                done = True
                break
            if done:
                break
        if not done:
            break
    return joined


def loose_sub_items(model: DocModel) -> list[str]:
    """Items set further in than the item above them: a list inside a list.

    "– If the student has successfully completed the previous skill set …" is
    printed under a bulleted item and indented past it. Kept beside its parent
    it reads as one flat list of five, where the page shows two with three
    points under the second."""
    out: list[str] = []
    for L in [n for n in model.nodes.values() if n.role == "L" and not n.artifact]:
        items = _items(model, L)
        if len(items) < 2:
            continue
        lefts = []
        for it in items:
            lbl = next((model.nodes[c] for c in it.children
                        if c in model.nodes and model.nodes[c].role == "Lbl"), None)
            lefts.append(lbl.bbox[0] if lbl is not None and lbl.bbox else
                         (it.bbox[0] if it.bbox else 0.0))
        base = min(lefts)
        if max(lefts) - base < 8.0:
            continue                      # every item at the same margin: one list
        from .analyzer import Analyzer
        base_lbl = {(_item_label(model, it) or "").strip() for k, it in enumerate(items)
                    if lefts[k] <= base + 8.0}
        base_kind = {Analyzer._numbering(x) for x in base_lbl if x}
        for k, it in enumerate(items):
            lbl = (_item_label(model, it) or "").strip()
            if not k or not lbl or lefts[k] <= base + 8.0:
                continue
            # set further in AND marked differently. Numbers that carry on are
            # one list however they are indented (a rubric sets "1 and 2:" at the
            # margin and "3:" under it); a different mark — a dash under a
            # bullet, letters under numbers — is a level of its own.
            kind = Analyzer._numbering(lbl)
            if kind in base_kind and not (kind in ("Disc", "Circle", "Square")
                                          and lbl not in base_lbl):
                continue
            out.append(it.id)
    return out


def nest_sub_items(model: DocModel, focus: set | None = None) -> int:
    """An indented run of items becomes a list inside the item above it."""
    from .analyzer import Analyzer
    moved = 0
    for L in [n for n in model.nodes.values() if n.role == "L" and not n.artifact
              and (focus is None or n.id in focus)]:
        want = set(loose_sub_items(model))
        items = _items(model, L)
        k = 0
        while k < len(items):
            if items[k].id not in want or k == 0:
                k += 1
                continue
            run = [items[k]]
            j = k + 1
            while j < len(items) and items[j].id in want:
                run.append(items[j])
                j += 1
            host = items[k - 1]
            body = next((model.nodes[c] for c in host.children
                         if c in model.nodes and model.nodes[c].role == "LBody"), None)
            if body is None:
                k = j
                continue
            sub = StructNode(id=model.new_id("list"), role="L", page=run[0].page,
                             bbox=run[0].bbox, source=L.source,
                             list_numbering=Analyzer._numbering(_item_label(model, run[0])),
                             note="set further in than the item above — a list inside it")
            model.add(sub, body.id)
            for it in run:
                model.detach(it.id)
                model.add(it, sub.id)
                sub.bbox = _union(sub.bbox, it.bbox)
                body.bbox = _union(body.bbox, it.bbox)
                host.bbox = _union(host.bbox, it.bbox)
                moved += 1
            items = _items(model, L)
            k = 0
            want = set(loose_sub_items(model))
    return moved


PAGENO = re.compile(r"^\s*(?:page\s+)?(?:[ivxlcdm]{1,7}|\d{1,5})(?:\s*(?:of|/)\s*\d{1,5})?\s*$", re.I)


def page_number_tags(model: DocModel, as_p: bool = True) -> list[str]:
    """Page numbers that are not yet what the project wants them to be."""
    out = []
    for n in model.nodes.values():
        if not PAGENO.match(n.text or ""):
            continue
        is_pn = (n.style or {}).get("page_number") or (
            n.artifact and (n.pagination_subtype in ("Header", "Footer")
                            or "page number" in (n.note or "")))
        if not is_pn:
            continue
        if as_p and n.artifact:
            out.append(n.id)
        elif not as_p and not n.artifact:
            out.append(n.id)
    return out


def page_numbers_as(model: DocModel, as_p: bool = True, word: str = "Page") -> int:
    """Read the printed page numbers as P tags — or leave them out again.

    Some publishers want the page number read where it is printed. It keeps its
    place on its page: an artifact is listed where it is printed, so the P it
    becomes is read there too — first when it sits at the top, last at the
    bottom."""
    changed = 0
    ids = page_number_tags(model, as_p)
    if as_p:
        # the same reading as "These are all page numbers": a P said "Page 5"
        from .edit import mark_page_numbers
        r = mark_page_numbers(model, ids, word)
        for nid in ids:
            n = model.nodes.get(nid)
            if n is not None and not n.artifact:
                n.style = dict(n.style or {}, page_number=True)
        return r["changed"]
    for nid in ids:
        n = model.nodes[nid]
        n.actual_text = ""
        n.role, n.artifact = "Artifact", True
        n.artifact_subtype = "Pagination"
        if n.pagination_subtype not in ("Header", "Footer"):
            # PDF space: the top of the page is the high end
            h = model.page_sizes[n.page][1] if 0 <= n.page < len(model.page_sizes) else 0
            n.pagination_subtype = "Header" if (n.bbox and h and n.bbox[1] > h / 2) else "Footer"
        n.note = "page number"
        n.style = {k: v for k, v in (n.style or {}).items() if k != "page_number"}
        n.source = "user"
        changed += 1
    return changed


def _rows_overlap(a, b) -> bool:
    """Do boxes a and b share a printed row (PDF space, y up)?"""
    if not a or not b:
        return False
    ov = min(a[3], b[3]) - max(a[1], b[1])
    return ov > 0.4 * max(1e-6, min(a[3] - a[1], b[3] - b[1]))


def list_values_apart(model: DocModel) -> list[str]:
    """Values printed beside list items but tagged apart from them.

    "• Task A:   9/10 correct" / "• Task B:   19/20 correct": the scores are
    set at a tab stop on the items' own rows. Read as a column of their own they
    end up as one paragraph — "9/10 correct 19/20 correct" — inside the last
    item, or after the list."""
    out: list[str] = []
    for L in [n for n in model.nodes.values() if n.role == "L" and not n.artifact]:
        items = _items(model, L)
        bodies = []
        for it in items:
            b = next((model.nodes[c] for c in it.children
                      if c in model.nodes and model.nodes[c].role == "LBody"), None)
            if b is not None and b.bbox:
                bodies.append(b)
        if len(bodies) < 2:
            continue
        cands = [model.nodes[c] for b in bodies for c in b.children
                 if c in model.nodes and model.nodes[c].role == "P"]
        sibs = model.siblings(L.id)
        k = sibs.index(L.id) if L.id in sibs else -1
        if 0 <= k < len(sibs) - 1 and sibs[k + 1] in model.nodes and model.nodes[sibs[k + 1]].role == "P":
            cands.append(model.nodes[sibs[k + 1]])
        for p in cands:
            if not p.bbox or p.page != L.page or len((p.text or "").split()) > 12:
                continue
            hit = [b for b in bodies if b.id != p.parent and _rows_overlap(p.bbox, b.bbox)
                   and p.bbox[0] > b.bbox[0]]
            mine = model.nodes.get(p.parent) if p.parent else None
            if hit or (mine is not None and mine.role == "LBody" and mine.bbox
                       and p.bbox[3] > mine.bbox[3] - 1 and p.bbox[0] > mine.bbox[0] + 20):
                out.append(p.id)
    return out


def fix_list_values(model: DocModel, working_pdf: str) -> int:
    """Each value printed beside an item goes into that item, on its row."""
    want = list_values_apart(model)
    if not want:
        return 0
    import pymupdf

    from .analyzer import Analyzer, collect_lines
    doc = pymupdf.open(working_pdf)
    fixed = 0
    try:
        lines_of: dict[int, list] = {}
        for pid in want:
            p = model.nodes.get(pid)
            if p is None or p.page < 0 or p.page >= doc.page_count:
                continue
            L = p
            while L is not None and L.role != "L":
                L = model.nodes.get(L.parent) if L.parent else None
            if L is None:                                  # the P after the list
                sibs = model.siblings(pid)
                k = sibs.index(pid)
                L = model.nodes.get(sibs[k - 1]) if k > 0 else None
                if L is None or L.role != "L":
                    continue
            page = doc[p.page]
            mat = page.transformation_matrix
            if p.page not in lines_of:
                lines_of[p.page] = collect_lines(page)
            r = pymupdf.Rect(p.bbox) * mat
            r.normalize()
            run = sorted((l for l in lines_of[p.page] if (l.text or "").strip()
                          and r.x0 - 1 <= (l.x0 + l.x1) / 2 <= r.x1 + 1
                          and r.y0 - 1 <= (l.y0 + l.y1) / 2 <= r.y1 + 1), key=lambda l: l.y0)
            built = []
            for it in _items(model, L):
                b = next((model.nodes[c] for c in it.children
                          if c in model.nodes and model.nodes[c].role == "LBody"), None)
                if b is None or not b.bbox:
                    continue
                rows = [l.bbox for l in lines_of[p.page]
                        if (l.text or "").strip() and l not in run
                        and _center_in_disp(l.bbox, b.bbox, mat)]
                if rows:
                    built.append({"body": b.id, "li": it.id, "rows": rows})
            hits = Analyzer._row_values(run, built)
            if not hits:
                continue
            for ln, it in hits:
                b = model.nodes[it["body"]]
                b.text = ((b.text or "") + " " + ln.text).strip()
                box = pymupdf.Rect(ln.bbox) * ~mat
                box.normalize()
                b.bbox = _union(b.bbox, (box.x0, box.y0, box.x1, box.y1))
                li = model.nodes[it["li"]]
                li.bbox = _union(li.bbox, b.bbox)
            model.remove(pid, promote_children=False)
            fixed += 1
        return fixed
    finally:
        doc.close()


def _center_in_disp(line_box, pdf_box, mat) -> bool:
    import pymupdf
    r = pymupdf.Rect(pdf_box) * mat
    r.normalize()
    cx, cy = (line_box[0] + line_box[2]) / 2, (line_box[1] + line_box[3]) / 2
    return r.x0 - 1 <= cx <= r.x1 + 1 and r.y0 - 1 <= cy <= r.y1 + 1


def list_children_wrong(model: DocModel) -> list[str]:
    """Lists holding something other than items, and items holding something
    other than a label and a body — or two labels."""
    out = []
    for n in model.nodes.values():
        if n.artifact:
            continue
        kids = [model.nodes[c] for c in n.children if c in model.nodes]
        if n.role == "L":
            if any(k.role not in ("LI", "Caption", "L") and not k.artifact for k in kids):
                out.append(n.id)
        elif n.role == "LI":
            if any(k.role not in ("Lbl", "LBody") and not k.artifact for k in kids) \
                    or sum(1 for k in kids if k.role == "Lbl") > 1:
                out.append(n.id)
    return out


def fix_list_children(model: DocModel) -> int:
    """A list holds items, an item holds its label and its body.

    A paragraph left straight in a list ("Scoring vowels: …" after item 4) goes
    into the body of the item above it; a paragraph straight in an item goes
    into that item's body; a second label in one item ("4." inside item 3)
    starts the item it is the number of, taking what follows it."""
    fixed = 0

    def body_of(li: StructNode) -> StructNode:
        b = next((model.nodes[c] for c in li.children
                  if c in model.nodes and model.nodes[c].role == "LBody"), None)
        if b is None:
            b = StructNode(id=model.new_id("lb"), role="LBody", page=li.page, bbox=li.bbox,
                           source=li.source)
            model.add(b, li.id)
        return b

    for _ in range(4):
        changed = 0
        for li in [n for n in model.nodes.values() if n.role == "LI" and not n.artifact]:
            if li.id not in model.nodes:
                continue
            kids = [model.nodes[c] for c in li.children if c in model.nodes]
            lbls = [k for k in kids if k.role == "Lbl"]
            if len(lbls) > 1 and li.parent in model.nodes:
                # "3." … "4." in one item: the second number starts its own item
                second = lbls[1]
                at = li.children.index(second.id)
                moving = [model.nodes[c] for c in li.children[at:] if c in model.nodes]
                new = StructNode(id=model.new_id("li"), role="LI", page=second.page,
                                 bbox=second.bbox, source=li.source,
                                 note="its own item — its number was inside the item above")
                sibs = model.nodes[li.parent].children
                model.add(new, li.parent, sibs.index(li.id) + 1)
                for k in moving:
                    model.detach(k.id)
                    model.add(k, new.id)
                    new.bbox = _union(new.bbox, k.bbox) if k.page == new.page else new.bbox
                changed += 1
                continue
            stray = [k for k in kids if k.role not in ("Lbl", "LBody") and not k.artifact]
            if stray:
                b = body_of(li)
                for k in stray:
                    model.detach(k.id)
                    model.add(k, b.id)
                    if k.page == b.page:
                        b.bbox = _union(b.bbox, k.bbox)
                changed += 1
        for L in [n for n in model.nodes.values() if n.role == "L" and not n.artifact]:
            kids = [model.nodes[c] for c in list(L.children) if c in model.nodes]
            for k in kids:
                if k.role in ("LI", "Caption", "L") or k.artifact:
                    continue
                prev = None
                for c in L.children[:L.children.index(k.id)]:
                    if c in model.nodes and model.nodes[c].role == "LI":
                        prev = model.nodes[c]
                if prev is None:            # before the first item: an item of its own
                    li = StructNode(id=model.new_id("li"), role="LI", page=k.page, bbox=k.bbox,
                                    source=L.source)
                    model.add(li, L.id, L.children.index(k.id))
                    prev = li
                b = body_of(prev)
                model.detach(k.id)
                model.add(k, b.id)
                if k.page == b.page:
                    b.bbox = _union(b.bbox, k.bbox)
                changed += 1
        fixed += changed
        if not changed:
            break
    return fixed


def lists_read_short(model: DocModel) -> list[str]:
    """Lists stretched over a page break by hand whose part on the next page was
    never read: the zone is there, but no item, paragraph or point is tagged in
    it — so what is printed there would belong to the list tag itself."""
    out = []
    for L in [n for n in model.nodes.values() if n.role == "L" and not n.artifact]:
        pages = {int(p[0]) for p in L.parts if int(p[0]) != L.page}
        if not pages:
            continue
        have = {d.page for d in model.descendants(L.id)}
        if pages - have:
            out.append(L.id)
    return out


def joined_word_grids(model: DocModel) -> list[str]:
    """Paragraphs made by joining the words of a word grid.

    "discount", "index", "return" — each word of a grid ends without a full stop
    and the next starts with a small letter, which is exactly what a sentence
    carried over a column break looks like. Joined, a page of words reads as
    one paragraph: "nonsense index station fastest return madness careless"."""
    out = []
    for n in model.nodes.values():
        if n.artifact or n.role not in ("P", "Span") or not n.parts:
            continue
        if "joined with continuation" not in (n.note or ""):
            continue
        zones = 1 + len(n.parts)
        words = len((n.text or "").split())
        if words <= 3 * zones:
            out.append(n.id)
    return out


TEXT_SPLIT_ROLES = {"P", "Span", "H1", "H2", "H3", "H4", "H5", "H6", "Caption", "Note", "Quote"}


def split_zones(model: DocModel, working_pdf: str, ids=None) -> int:
    """One tag per zone: a tag joined from several pieces becomes those pieces
    again, each a paragraph of its own with its own text, in reading order."""
    import pymupdf

    from .analyzer import collect_lines
    want = ids if ids is not None else joined_word_grids(model)
    if not want:
        return 0
    doc = pymupdf.open(working_pdf)
    lines_of: dict[int, list] = {}

    def text_in(pg: int, box) -> str:
        if pg not in lines_of:
            lines_of[pg] = collect_lines(doc[pg])
        r = pymupdf.Rect(box) * doc[pg].transformation_matrix
        r.normalize()
        got = sorted((l for l in lines_of[pg] if (l.text or "").strip()
                      and r.x0 - 1 <= (l.x0 + l.x1) / 2 <= r.x1 + 1
                      and r.y0 - 1 <= (l.y0 + l.y1) / 2 <= r.y1 + 1),
                     key=lambda l: (round(l.y0, 1), l.x0))
        return " ".join(l.text for l in got).strip()

    done = 0
    try:
        for nid in want:
            n = model.nodes.get(nid)
            if n is None or not n.parts or n.bbox is None or n.children \
                    or n.role not in TEXT_SPLIT_ROLES:
                continue
            zones = [[int(p[0]), *p[1:5]] for p in n.parts]
            sibs = model.siblings(nid)
            at = sibs.index(nid) + 1 if nid in sibs else len(sibs)
            n.text = text_in(n.page, n.bbox) or n.text.split()[0]
            n.parts = []
            n.note = "a tag of its own again — it had been joined with the next ones"
            if "grid" in (n.note or "") or len(n.text.split()) <= 3:
                n.style = dict(n.style or {}, grid_item=True)
            for k, z in enumerate(zones):
                if not (0 <= z[0] < doc.page_count):
                    continue
                p = StructNode(id=model.new_id("p"), role=n.role, page=z[0], bbox=tuple(z[1:5]),
                               text=text_in(z[0], z[1:5]), source="user", priority=n.priority,
                               note="a tag of its own again — it had been joined to the one before")
                p.style = dict(n.style or {})
                model.add(p, n.parent, at + k)
            done += 1
        return done
    finally:
        doc.close()


# ------------------------------------------------ "Name: ____  Date: ____"
BLANK_WORDS = {"blank line", "blank", "underscore", "multiple blank lines", "blank lines"}


def _blank_span(n: StructNode, opts=None) -> bool:
    st = n.style or {}
    said = (n.actual_text or "").strip().lower()
    mine = {str(getattr(opts, k, "") or "").strip().lower() for k in
            ("blank_line_text", "multi_blank_line_text", "underscore_tick_text")} - {""}
    return n.role == "Span" and bool(st.get("blank_line") or said in BLANK_WORDS | mine)


def _row_of(page_lines: list, rects: list) -> "object | None":
    """The characters printed inside these zones, as one printed row — or None
    when they are on more than one row. Only the tag's own characters count: the
    page may print "Name: ____ Date: ____" as one line, and a tag that holds
    only "Name: ____" must not be read as the whole row."""
    from .analyzer import Line
    def inside(b) -> bool:
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        return any(r[0] - 0.5 <= cx <= r[2] + 0.5 and r[1] - 1 <= cy <= r[3] + 1 for r in rects)

    pieces = []
    for l in page_lines:
        mine = [(c, b) for c, b in l.chars if b is not None and inside(b)]
        if any(c.strip() for c, _ in mine):
            pieces.append((l, mine))
    if not pieces:
        return None
    vis = [b for _, mine in pieces for c, b in mine if c.strip()]
    top, bottom = min(b[1] for b in vis), max(b[3] for b in vis)
    tallest = max(b[3] - b[1] for b in vis)
    if bottom - top > 1.6 * tallest:
        return None
    pieces.sort(key=lambda t: min(b[0] for _, b in t[1]))
    chars: list = []
    for k, (_, mine) in enumerate(pieces):
        if k:
            chars.append((" ", None))
        chars.extend(mine)
    first = pieces[0][0]
    return Line(text="".join(c for c, _ in chars), bbox=(min(b[0] for b in vis), top, max(b[2] for b in vis), bottom),
                size=max(l.size for l, _ in pieces), bold=first.bold, italic=first.italic, font=first.font,
                chars=chars, flow=first.flow)


def prompt_rows(model: DocModel, working_pdf: str, opts=None) -> list[str]:
    """Paragraphs that need their write-in row rebuilt from the page:

    * one row holding several prompts — "Name: ____  Date: ____" tagged as a
      single P, so a screen reader ran the two together;
    * a blank-line Span that took a printed character — the ":" of "Date:" sat
      1 pt left of the line and went into the Span, so "Date:" was read
      "Date Blank Line Blank Line"."""
    import pymupdf

    from .analyzer import collect_lines, split_prompts
    out: list[str] = []
    cands = []
    for n in model.nodes.values():
        if n.artifact or n.role != "P" or n.page < 0:
            continue
        kids = [model.nodes[c] for c in n.children if c in model.nodes]
        if "_" in (n.text or "") or any(_blank_span(k, opts) for k in kids):
            cands.append(n)
    if not cands:
        return out
    doc = pymupdf.open(working_pdf)
    try:
        lines_of: dict[int, list] = {}
        for n in cands:
            if n.page >= doc.page_count:
                continue
            page = doc[n.page]
            if n.page not in lines_of:
                lines_of[n.page] = collect_lines(page)
            disp = []
            for x in [n] + model.descendants(n.id):
                for r in x.rects_on(n.page):
                    rr = pymupdf.Rect(r) * page.transformation_matrix
                    rr.normalize()
                    disp.append((rr.x0, rr.y0, rr.x1, rr.y1))
            row = _row_of(lines_of[n.page], disp)
            if row is None or not UNDERSCORE_RUN.search(row.text):
                continue
            if split_prompts(row):
                out.append(n.id)
                continue
            for k in [model.nodes[c] for c in n.children if c in model.nodes]:
                if not _blank_span(k, opts):
                    continue
                spans = []
                for r in k.rects_on(n.page):
                    rr = pymupdf.Rect(r) * page.transformation_matrix
                    rr.normalize()
                    spans.append(rr)
                held = "".join(c for c, b in row.chars if b is not None and any(
                    s.x0 - 0.2 <= (b[0] + b[2]) / 2 <= s.x1 + 0.2 and s.y0 - 0.2 <= (b[1] + b[3]) / 2 <= s.y1 + 0.2
                    for s in spans))
                if re.sub(r"[_\s]", "", held):
                    out.append(n.id)
                    break
    finally:
        doc.close()
    return out


def split_prompt_rows(model: DocModel, working_pdf: str, opts=None, ids=None) -> dict:
    """Rebuild each such row from the page: one P per prompt ("Name: ____",
    "Date: ____"), its label in the P and only the underscores in the
    blank-line Span. The new zones win over the file's old marked content, so it
    works the same in a file tagged by the tool and in one whose tags were
    imported."""
    import pymupdf

    from .analyzer import Analyzer, collect_lines, split_prompts, to_pdf
    from .options import Options

    o = opts or Options()
    want = [i for i in (ids if ids is not None else prompt_rows(model, working_pdf, o)) if i in model.nodes]
    stats = {"rows": 0, "tags": 0}
    if not want:
        return stats
    a = Analyzer(working_pdf, o)
    a.model = model
    try:
        lines_of: dict[int, list] = {}
        for nid in want:
            n = model.nodes.get(nid)
            if n is None or n.page < 0 or n.page >= a.doc.page_count:
                continue
            page = a.doc[n.page]
            if n.page not in lines_of:
                lines_of[n.page] = collect_lines(page)
            disp = []
            for x in [n] + model.descendants(n.id):
                for r in x.rects_on(n.page):
                    rr = pymupdf.Rect(r) * page.transformation_matrix
                    rr.normalize()
                    disp.append((rr.x0, rr.y0, rr.x1, rr.y1))
            row = _row_of(lines_of[n.page], disp)
            if row is None:
                continue
            segs = split_prompts(row) or [row]
            rescued = [d for d in model.descendants(n.id) if d.role in ("Link", "Annot", "Form") and d.annots]
            for d in rescued:
                try:
                    model.detach(d.id)
                except (ValueError, KeyError):
                    pass
            parent = n.parent
            sibs = model.siblings(n.id)
            at = sibs.index(n.id)
            model.remove(n.id, promote_children=False)
            made = []
            for seg in segs:
                vis = [b for c, b in seg.chars if c.strip() and b is not None]
                if not vis:
                    continue
                seg.bbox = (min(b[0] for b in vis), min(b[1] for b in vis),
                            max(b[2] for b in vis), max(b[3] for b in vis))
                p = StructNode(id=model.new_id("pr"), role="P", page=n.page, bbox=to_pdf(page, seg.bbox),
                               text=re.sub(r"\s+", " ", seg.text).strip(), source="auto", priority=1,
                               note="a label and its write-in line" if len(segs) > 1 else
                               "write-in row read again from the page")
                model.add(p, parent, at + len(made))
                a._underscore_spans(page, n.page, p, [seg])
                for c in p.children:
                    sp = model.nodes.get(c)
                    if sp is not None:
                        sp.priority = 1
                made.append(p)
            for d in rescued:
                cx = (d.bbox[0] + d.bbox[2]) / 2 if d.bbox else None
                home = next((p for p in made if cx is not None and p.bbox[0] - 2 <= cx <= p.bbox[2] + 2), None)
                model.add(d, home.id if home else (made[0].id if made else parent))
            stats["rows"] += 1
            stats["tags"] += len(made)
    finally:
        a.doc.close()
        a.pdf.close()
    return stats


def fix_prompt_rows(model: DocModel, working_pdf: str, opts=None, ids=None) -> dict:
    """Rebuild the rows, then take the line out of every tag's own text."""
    st = split_prompt_rows(model, working_pdf, opts, ids)
    st["texts"] = tidy_blank_text(model)
    return st



def blank_text_left(model: DocModel) -> list[str]:
    """Tags that still show their write-in line in their own text although a
    blank-line Span holds it ("Name:________" over "Span ________")."""
    from .analyzer import own_text
    return [n.id for n in model.nodes.values()
            if not n.artifact and n.text and "__" in n.text and own_text(n.text, model, n) != n.text]


def tidy_blank_text(model: DocModel) -> int:
    """"Name:________" -> "Name:" when the Span under it is the line."""
    from .analyzer import own_text
    done = 0
    for nid in blank_text_left(model):
        n = model.nodes[nid]
        n.text = own_text(n.text, model, n)
        done += 1
    return done
