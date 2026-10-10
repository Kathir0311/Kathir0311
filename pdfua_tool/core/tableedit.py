"""Edit table (V4.18): a table's rows and columns changed by hand.

The editor's "Edit table" dialog shows the table as a grid (rows × columns,
merged cells spanning several) and changes it one step at a time:

* insert a row / a column (empty cells; a merged cell across the place grows)
* delete a row / a column (one with no words; a row with words can be made an
  artifact instead, e.g. a "continued" line)
* split a row / a column at a line on the page (each cell is cut there and
  both halves take the words printed in them) and merge two rows / columns
* merge a block of cells into one, split a merged cell again
* TH / TD and the scope of cells, header rows and columns
* move a row or column line on the page (the cells on both sides follow it
  and read their words again)

Cells keep their zones (PDF space) so the build tags exactly what is printed
in each; a cell's text is read off the page again whenever its zone changes.
"""
from __future__ import annotations

import pymupdf

from .lint import table_grid
from .model import DocModel, StructNode, union


class TableEditError(Exception):
    pass


GROUPS = ("THead", "TBody", "TFoot")


# ------------------------------------------------------------- reading the grid
class Grid:
    """The table as cells at (row, column) with spans, and its rows (TR)."""

    def __init__(self, model: DocModel, tid: str):
        t = model.nodes.get(tid)
        if t is None or t.role != "Table":
            raise TableEditError("not a table")
        self.m = model
        self.t = t
        self.rows: list[StructNode] = []
        for c in t.children:
            n = model.nodes.get(c)
            if n is None:
                continue
            if n.role == "TR":
                self.rows.append(n)
            elif n.role in GROUPS:
                self.rows += [model.nodes[g] for g in n.children if g in model.nodes and model.nodes[g].role == "TR"]
        occ, widths = table_grid(model, tid)
        self.cells: dict[str, list] = {}           # id -> [r, c, rs, cs]
        for (r, c), cid in sorted(occ.items()):
            if cid not in self.cells:
                cell = model.nodes[cid]
                self.cells[cid] = [r, c, max(1, cell.rowspan), max(1, cell.colspan)]
        self.R = len(self.rows)
        self.C = max(widths or [0])

    def at(self, r: int, c: int) -> str | None:
        for cid, (r0, c0, rs, cs) in self.cells.items():
            if r0 <= r < r0 + rs and c0 <= c < c0 + cs:
                return cid
        return None

    def node(self, cid: str) -> StructNode:
        return self.m.nodes[cid]

    # ---------------------------------------------------------- writing back
    def collapse(self) -> int:
        """A row in which no cell starts (every place in it is taken by a
        merged cell from above) is not a row: it goes and those cells span one
        row less — no empty TR is written (V4.19.2). The same for a column
        that only merged cells cross."""
        gone = 0
        changed = True
        while changed:
            changed = False
            starts_r = {v[0] for v in self.cells.values()}
            for r in range(len(self.rows)):
                if r in starts_r:
                    continue
                if not any(v[0] < r < v[0] + v[2] for v in self.cells.values()):
                    continue                   # an empty row: write() takes it out
                for v in self.cells.values():
                    if v[0] < r < v[0] + v[2]:
                        v[2] -= 1
                    elif v[0] > r:
                        v[0] -= 1
                tr = self.rows.pop(r)
                for c in list(tr.children):
                    n = self.m.nodes.get(c)
                    if n is not None and n.role not in ("TD", "TH") and self.rows:
                        host = self.rows[max(0, r - 1)]
                        n.parent = host.id
                        host.children.append(c)
                tr.children = [c for c in tr.children if c in self.cells]
                for c in tr.children:
                    self.m.nodes[c].parent = None
                tr.children = []
                if tr.id in self.m.nodes:
                    self.m.remove(tr.id, promote_children=False)
                self.R = len(self.rows)
                gone += 1
                changed = True
                break
            if changed:
                continue
            starts_c = {v[1] for v in self.cells.values()}
            for c in range(self.C):
                if c in starts_c or not any(v[1] < c < v[1] + v[3] for v in self.cells.values()):
                    continue
                for v in self.cells.values():
                    if v[1] < c < v[1] + v[3]:
                        v[3] -= 1
                    elif v[1] > c:
                        v[1] -= 1
                self.C -= 1
                gone += 1
                changed = True
                break
        return gone

    def write(self) -> None:
        """Rows (TR) get the cells that start in them, left to right; spans
        are written to the cells; a row left with no cell and no span over it
        goes."""
        self.collapse()
        m = self.m
        starts: dict[int, list] = {}
        for cid, (r, c, rs, cs) in self.cells.items():
            n = m.nodes[cid]
            n.rowspan, n.colspan = rs, cs
            starts.setdefault(r, []).append((c, cid))
        for r, tr in enumerate(self.rows):
            want = [cid for _, cid in sorted(starts.get(r, []))]
            keep_other = [k for k in tr.children if k in m.nodes and m.nodes[k].role not in ("TD", "TH")]
            for cid in want:
                n = m.nodes[cid]
                if n.parent != tr.id:
                    if n.parent is not None and n.parent in m.nodes and cid in m.nodes[n.parent].children:
                        m.nodes[n.parent].children.remove(cid)
                    n.parent = tr.id
            tr.children = want + keep_other
        for tr in list(self.rows):
            covered = any(r0 <= self.rows.index(tr) < r0 + rs for r0, _, rs, _ in self.cells.values())
            if not tr.children and not covered:
                m.remove(tr.id, promote_children=False)
                self.rows.remove(tr)
        self.R = len(self.rows)

    def new_row(self, at: int, like: StructNode | None) -> StructNode:
        """A new TR at row index `at`, in the same group as the row it follows."""
        m = self.m
        ref = like or (self.rows[min(at, len(self.rows) - 1)] if self.rows else None)
        tr = StructNode(id=m.new_id("tr"), role="TR", page=ref.page if ref else self.t.page,
                        source="user", note="row added by hand")
        if ref is not None and ref.parent in m.nodes:
            parent = m.nodes[ref.parent]
            sib = parent.children
            pos = sib.index(ref.id) + (1 if at > self.rows.index(ref) else 0) if ref in self.rows else len(sib)
            m.add(tr, parent.id, pos)
        else:
            m.add(tr, self.t.id)
        self.rows.insert(at, tr)
        self.R = len(self.rows)
        return tr

    def new_cell(self, r: int, c: int, rs: int = 1, cs: int = 1, like: StructNode | None = None,
                 page: int = -1, bbox=None) -> str:
        m = self.m
        role = like.role if like is not None and like.role in ("TD", "TH") else "TD"
        n = StructNode(id=m.new_id("td"), role=role, page=page if page >= 0 else (like.page if like else self.t.page),
                       bbox=tuple(bbox) if bbox is not None else None, source="user", priority=1,
                       note="cell added by hand")
        if role == "TH" and like is not None:
            n.scope = like.scope
        m.nodes[n.id] = n
        n.parent = None
        self.cells[n.id] = [r, c, rs, cs]
        return n.id


def _disp(page: pymupdf.Page, box) -> list[float]:
    r = pymupdf.Rect(box) * (page.transformation_matrix * page.rotation_matrix)
    r.normalize()
    return [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)]


def _pdf(page: pymupdf.Page, box) -> tuple:
    r = pymupdf.Rect(box) * ~(page.transformation_matrix * page.rotation_matrix)
    r.normalize()
    return (round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2))


def grid_view(model: DocModel, working_pdf: str, tid: str) -> dict:
    """What the dialog shows: the cells with their place, spans, role, words
    and zone (display space), the rows, and the lines between rows and columns
    on each page."""
    g = Grid(model, tid)
    doc = pymupdf.open(working_pdf)
    try:
        cells = []
        for cid, (r, c, rs, cs) in sorted(g.cells.items(), key=lambda kv: (kv[1][0], kv[1][1])):
            n = g.node(cid)
            rect = None
            if n.bbox is not None and 0 <= n.page < doc.page_count:
                rect = _disp(doc[n.page], n.bbox)
            kids = [model.nodes[k] for k in n.children if k in model.nodes]
            cells.append({"id": cid, "r": r, "c": c, "rs": rs, "cs": cs, "role": n.role, "scope": n.scope,
                          "text": (n.text or "")[:300], "page": n.page, "rect": rect,
                          "kids": len(n.children), "artifact": n.artifact,
                          # what a screen reader reads inside the cell, in order (V4.21.3)
                          "kid_list": [{"id": k.id, "role": "Artifact" if k.artifact else k.role,
                                        "text": (k.text or "")[:80],
                                        "rect": _disp(doc[k.page], k.bbox) if k.bbox is not None and 0 <= k.page < doc.page_count else None}
                                       for k in kids]})
        # the reading order of the cells: row by row, left to right, as the rows hold them
        for k, c in enumerate(sorted(cells, key=lambda x: (x["r"], x["c"])), 1):
            c["order"] = k
        printed = printed_order(g, doc)
        rows = [{"id": tr.id, "group": model.nodes[tr.parent].role if tr.parent in model.nodes else "Table",
                 "artifact": tr.artifact} for tr in g.rows]
        lines = _lines(g, doc)
        st = g.t.style or {}
        return {"id": tid, "rows": g.R, "cols": g.C, "cells": cells, "row_info": rows,
                "lines": lines, "header_rows": st.get("header_rows", 1), "header_cols": st.get("header_cols", 0),
                "checks": checks(model, g), "has_next": _next_table(model, g.t) is not None,
                "note": (g.t.note or "")[:200], "summary": st.get("summary", ""),
                "printed_order": printed,
                "order_off": [c["id"] for c in sorted(cells, key=lambda x: x["order"])
                              if printed and printed.index(c["id"]) != c["order"] - 1] if printed else [],
                "empty_cells": sum(1 for c in g.cells if not (g.node(c).text or "").strip() and not g.node(c).children),
                "pages": sorted({c["page"] for c in cells if c["rect"]})}
    finally:
        doc.close()


def _clusters(items: list, pad: float = 4.0) -> list[list]:
    """Group boxes that touch (within `pad`): the parts of a table printed
    apart on one page — "Table 3" in the left column and "Table 3 continued"
    in the right one are two parts, each with its own lines."""
    n = len(items)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    order = sorted(range(n), key=lambda i: items[i][0][0])
    for ai, i in enumerate(order):
        a = items[i][0]
        for j in order[ai + 1:]:
            b = items[j][0]
            if b[0] > a[2] + pad:
                break
            if b[1] <= a[3] + pad and a[1] <= b[3] + pad:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri
    groups: dict[int, list] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(items[i])
    return list(groups.values())


def _lines(g: Grid, doc) -> dict:
    """Per page, per part of the table printed there: the row lines {k: y}
    (between row k-1 and k), the column lines {k: x} and the part's box —
    from that part's own cells (display space). Lines never run outside the
    cells they are drawn from (V4.19)."""
    per_page: dict[int, list] = {}
    for cid, (r, c, rs, cs) in g.cells.items():
        n = g.node(cid)
        if n.bbox is None or not 0 <= n.page < doc.page_count:
            continue
        per_page.setdefault(n.page, []).append((_disp(doc[n.page], n.bbox), (r, c, rs, cs), cid))
    out: dict = {}
    for pno, items in per_page.items():
        parts = []
        for grp in _clusters(items):
            rows: dict = {}
            cols: dict = {}
            starts: dict = {}
            ends: dict = {}
            box = list(grp[0][0])
            for d, (r, c, rs, cs), cid in grp:
                box = [min(box[0], d[0]), min(box[1], d[1]), max(box[2], d[2]), max(box[3], d[3])]
                for k, v in ((r, d[1]), (r + rs, d[3])):
                    rows.setdefault(k, []).append(v)
                for k, v in ((c, d[0]), (c + cs, d[2])):
                    cols.setdefault(k, []).append(v)
                starts.setdefault(c, []).append(d[0])
                ends.setdefault(c + cs, []).append(d[2])
            # a column moved for the reading order (V4.21.3) is printed elsewhere:
            # no line is drawn where the columns before and after it cross
            for k in list(cols):
                x = sum(cols[k]) / len(cols[k])
                if (k in starts and k in ends
                        and sum(ends[k]) / len(ends[k]) > sum(starts[k]) / len(starts[k]) + 2) \
                        or any(d[0] + 3 < x < d[2] - 3 and not (c < k < c + cs)
                               for d, (r, c, rs, cs), _ in grp):
                    cols.pop(k)
            # the stretch of each line: from the cells along it only
            rspan: dict = {}
            for d, (r, c, rs, cs), cid in grp:
                for k in (r, r + rs):
                    s0 = rspan.setdefault(k, [d[0], d[2]])
                    s0[0], s0[1] = min(s0[0], d[0]), max(s0[1], d[2])
            cspan: dict = {}
            for d, (r, c, rs, cs), cid in grp:
                for k in (c, c + cs):
                    s0 = cspan.setdefault(k, [d[1], d[3]])
                    s0[0], s0[1] = min(s0[0], d[1]), max(s0[1], d[3])
            parts.append({"box": [round(v, 2) for v in box],
                          "rows": {k: round(sum(v) / len(v), 2) for k, v in rows.items()},
                          "cols": {k: round(sum(v) / len(v), 2) for k, v in cols.items()},
                          "row_span": {k: [round(v[0], 2), round(v[1], 2)] for k, v in rspan.items()},
                          "col_span": {k: [round(v[0], 2), round(v[1], 2)] for k, v in cspan.items()},
                          "cells": [cid for _, _, cid in grp]})
        parts.sort(key=lambda p: (p["box"][0], p["box"][1]))
        out[str(pno)] = {"parts": parts,
                         "box": [min(p["box"][0] for p in parts), min(p["box"][1] for p in parts),
                                 max(p["box"][2] for p in parts), max(p["box"][3] for p in parts)]}
    return out


def _part_of(lines: dict, page: int, cid: str) -> dict:
    for p in (lines.get(str(page)) or {}).get("parts", []):
        if cid in p["cells"]:
            return p
    return {}


# ------------------------------------------------------------------ helpers
def _reread(model: DocModel, working_pdf: str, ids, tid: str | None = None) -> None:
    """The cells take the words printed in their zones again — a line cut at
    the cell borders word by word, never inside a word."""
    ids = [i for i in ids if i in model.nodes]
    if tid is not None:
        from .repair import reread_cells
        reread_cells(model, working_pdf, None, [tid], only=set(ids))
        # a cell that holds nothing printed reads as empty
        return
    from .edit import _text_in
    doc = pymupdf.open(working_pdf)
    try:
        for cid in ids:
            n = model.nodes.get(cid)
            if n is None or n.children or n.bbox is None or not 0 <= n.page < doc.page_count:
                continue
            n.text = _text_in(doc[n.page], n.bbox)
    finally:
        doc.close()


def _relink_headers(model: DocModel, tid: str) -> None:
    """A table whose data cells name their headers (IDs + Headers): the names
    again, from the header cells above and to the left."""
    g = Grid(model, tid)
    if not any(g.node(c).headers for c in g.cells):
        return
    for cid, (r, c, rs, cs) in g.cells.items():
        n = g.node(cid)
        if n.role != "TD":
            n.headers = []
            continue
        hdrs = []
        for cc in range(c, c + cs):
            for rr in range(0, r):
                h = g.at(rr, cc)
                if h and g.node(h).role == "TH" and h not in hdrs:
                    hdrs.append(h)
        for rr in range(r, r + rs):
            for cc in range(0, c):
                h = g.at(rr, cc)
                if h and g.node(h).role == "TH" and h not in hdrs:
                    hdrs.append(h)
        n.headers = hdrs


def _has_words(model: DocModel, cid: str) -> bool:
    n = model.nodes[cid]
    return bool((n.text or "").strip() or n.children)


def _union_into(model: DocModel, keep: str, gone: str) -> None:
    """Fold cell `gone` into `keep`: one zone where they are on one page, the
    words and any tags inside joined."""
    a, b = model.nodes[keep], model.nodes[gone]
    if b.bbox is not None:
        if a.bbox is None:
            a.page, a.bbox = b.page, b.bbox
        elif a.page == b.page:
            a.bbox = tuple(union(a.bbox, b.bbox))
        else:
            a.parts.append([b.page, *b.bbox])
    a.parts += [list(p) for p in b.parts]
    a.text = " ".join(t for t in ((a.text or "").strip(), (b.text or "").strip()) if t)
    a.src_keys = list(a.src_keys) + [k for k in b.src_keys if k not in a.src_keys]
    for c in list(b.children):
        model.nodes[c].parent = a.id
        a.children.append(c)
    b.children = []
    if b.parent in model.nodes and gone in model.nodes[b.parent].children:
        model.nodes[b.parent].children.remove(gone)
    model.nodes.pop(gone, None)


# ---------------------------------------------------------------- operations
def _part_cells(g: Grid, working_pdf: str, page_no: int, part) -> set | None:
    """The cells of one printed part of the table on a page (None: all)."""
    if part is None or part == "":
        return None
    doc = pymupdf.open(working_pdf)
    try:
        parts = (_lines(g, doc).get(str(page_no)) or {}).get("parts", [])
    finally:
        doc.close()
    try:
        return set(parts[int(part)]["cells"])
    except (IndexError, ValueError, TypeError):
        return None


def _split_line(model, working_pdf, g: Grid, kind: str, k: int, page_no: int, pos: float,
                only: set | None = None) -> list:
    """Cut row (kind "row") or column k at display `pos` on the page: every
    cell of that row / column that is printed across the line becomes two."""
    doc = pymupdf.open(working_pdf)
    try:
        if not 0 <= page_no < doc.page_count:
            raise TableEditError("no such page")
        page = doc[page_no]
        touched = []
        row = kind == "row"
        lim = g.R if row else g.C
        if not 0 <= k < lim:
            raise TableEditError(f"no such {kind}")
        # make room: everything after k moves on by one
        for cid, v in list(g.cells.items()):
            r0, c0, rs, cs = v
            s0, sp = (r0, rs) if row else (c0, cs)
            if s0 > k:
                if row:
                    v[0] += 1
                else:
                    v[1] += 1
            elif s0 <= k < s0 + sp and sp > 1:
                # a merged cell across the line: it spans the new one too
                if row:
                    v[2] += 1
                else:
                    v[3] += 1
        if row:
            g.new_row(k + 1, g.rows[k])
        g.C += 0 if row else 1
        for cid, v in list(g.cells.items()):
            r0, c0, rs, cs = v
            s0, sp = (r0, rs) if row else (c0, cs)
            if s0 != k or sp != 1:
                continue
            n = g.node(cid)
            second = None
            if n.bbox is not None and n.page == page_no and (only is None or cid in only):
                d = _disp(page, n.bbox)
                lo, hi = (d[1], d[3]) if row else (d[0], d[2])
                if lo + 1 < pos < hi - 1:
                    first = [d[0], d[1], d[2], pos] if row else [d[0], d[1], pos, d[3]]
                    second = [d[0], pos, d[2], d[3]] if row else [pos, d[1], d[2], d[3]]
                    n.bbox = _pdf(page, first)
                    touched.append(cid)
            nr, nc = (r0 + 1, c0) if row else (r0, c0 + 1)
            nid = g.new_cell(nr, nc, rs if not row else 1, cs if row else 1, like=n,
                             page=page_no if second else n.page,
                             bbox=_pdf(page, second) if second else None)
            touched.append(nid)
        return touched
    finally:
        doc.close()


def _drop_row(g: Grid, k: int) -> None:
    """Row k leaves the grid: its cells are let go first (write() puts each
    cell that is left into its row), anything else in it goes to the row
    before."""
    m = g.m
    tr = g.rows.pop(k)
    keep_to = g.rows[k - 1] if k > 0 and g.rows else (g.rows[0] if g.rows else None)
    for c in list(tr.children):
        n = m.nodes.get(c)
        if n is None:
            continue
        if n.role in ("TD", "TH"):
            n.parent = None
        elif keep_to is not None:
            n.parent = keep_to.id
            keep_to.children.append(c)
    tr.children = []
    if tr.id in m.nodes:
        m.remove(tr.id, promote_children=False)
    g.R = len(g.rows)


def _merge_lines(g: Grid, kind: str, k: int) -> list:
    """Row / column k and k+1 become one."""
    row = kind == "row"
    lim = g.R if row else g.C
    if not 0 <= k < lim - 1:
        raise TableEditError(f"pick a {kind} that has another one after it")
    m = g.m
    si, pi = (0, 2) if row else (1, 3)          # start / span index for this direction
    oi, qi = (1, 3) if row else (0, 2)          # … and the other one
    other = g.C if row else g.R
    pairs: list = []
    spanning: set = set()
    for j in range(other):
        a = g.at(k, j) if row else g.at(j, k)
        b = g.at(k + 1, j) if row else g.at(j, k + 1)
        if a is None or b is None:
            continue
        if a == b:
            spanning.add(a)
            continue
        va, vb = g.cells[a], g.cells[b]
        if not (va[oi] == vb[oi] and va[qi] == vb[qi] and va[si] + va[pi] - 1 == k and vb[si] == k + 1):
            raise TableEditError("the cells of these two do not line up (merged cells) — "
                                 "merge or split those cells first")
        if (a, b) not in pairs:
            pairs.append((a, b))
    touched = []
    for a, b in pairs:
        va, vb = g.cells[a], g.cells[b]
        va[pi] = va[pi] + vb[pi] - 1
        _union_into(m, a, b)
        del g.cells[b]
        touched.append(a)
    for a in spanning:
        g.cells[a][pi] -= 1
    for cid, v in g.cells.items():
        if v[si] >= k + 1:
            v[si] -= 1
    if row:
        _drop_row(g, k + 1)
    else:
        g.C -= 1
    return touched


def _insert(g: Grid, kind: str, at: int) -> list:
    """An empty row / column at index `at`; a merged cell across the place grows."""
    row = kind == "row"
    lim = g.R if row else g.C
    at = max(0, min(at, lim))
    covered = set()
    for cid, v in g.cells.items():
        s0, sp = (v[0], v[2]) if row else (v[1], v[3])
        if s0 >= at:
            if row:
                v[0] += 1
            else:
                v[1] += 1
        elif s0 < at < s0 + sp:
            if row:
                v[2] += 1
                covered.update(range(v[1], v[1] + v[3]))
            else:
                v[3] += 1
                covered.update(range(v[0], v[0] + v[2]))
    if row:
        like_row = g.rows[at - 1] if at > 0 else (g.rows[0] if g.rows else None)
        g.new_row(at, like_row)
    else:
        g.C += 1
    made = []
    other = g.C if row else g.R
    for j in range(other):
        if j in covered:
            continue
        if row:
            nb = g.at(at - 1, j) if at > 0 else g.at(at + 1, j)
        else:
            nb = g.at(j, at - 1) if at > 0 else g.at(j, at + 1)
        like = g.node(nb) if nb else None
        made.append(g.new_cell(at if row else j, j if row else at, 1, 1, like=like,
                               page=like.page if like is not None else -1))
    return made


def _delete(g: Grid, kind: str, k: int, artifact: bool) -> None:
    row = kind == "row"
    lim = g.R if row else g.C
    if not 0 <= k < lim:
        raise TableEditError(f"no such {kind}")
    m = g.m
    si, pi = (0, 2) if row else (1, 3)
    own = [cid for cid, v in g.cells.items() if v[si] == k and v[pi] == 1]
    if artifact:
        if not row:
            raise TableEditError("only a row can be made an artifact")
        if any(v[si] <= k < v[si] + v[pi] and v[pi] > 1 for v in g.cells.values()):
            raise TableEditError("a merged cell runs through this row — split it first")
        # the row leaves the table; its words stay in the file as an artifact
        # (not read), right after the table
        tr = g.rows[k]
        art = StructNode(id=m.new_id("art"), role="Div", page=tr.page, artifact=True,
                         artifact_subtype="Layout", source="user", note="table row made an artifact")
        sib = m.siblings(g.t.id)
        m.add(art, g.t.parent, sib.index(g.t.id) + 1)
        for c in own:
            n = m.nodes[c]
            del g.cells[c]
            if n.parent in m.nodes and c in m.nodes[n.parent].children:
                m.nodes[n.parent].children.remove(c)
            n.role, n.artifact, n.rowspan, n.colspan, n.headers, n.scope = "P", True, 1, 1, [], ""
            m.add(n, art.id)
        for v in g.cells.values():
            if v[si] > k:
                v[si] -= 1
        _drop_row(g, k)
        return
    if any(_has_words(m, c) for c in own):
        raise TableEditError(f"this {kind} has words in it — merge it with its neighbour"
                             + (", or make it an artifact (not read)" if row else ""))
    for c in own:
        del g.cells[c]
        if c in m.nodes:
            m.remove(c, promote_children=False)
    for v in g.cells.values():
        if v[si] > k:
            v[si] -= 1
        elif v[si] <= k < v[si] + v[pi]:
            v[pi] -= 1
    if row:
        _drop_row(g, k)
    else:
        g.C -= 1


def _merge_cells(g: Grid, ids: list) -> str:
    ids = [i for i in dict.fromkeys(ids) if i in g.cells]
    if len(ids) < 2:
        raise TableEditError("pick two or more cells to merge")
    r0 = min(g.cells[i][0] for i in ids)
    c0 = min(g.cells[i][1] for i in ids)
    r1 = max(g.cells[i][0] + g.cells[i][2] for i in ids)
    c1 = max(g.cells[i][1] + g.cells[i][3] for i in ids)
    inside = set()
    for r in range(r0, r1):
        for c in range(c0, c1):
            cid = g.at(r, c)
            if cid is None:
                continue
            v = g.cells[cid]
            if v[0] < r0 or v[1] < c0 or v[0] + v[2] > r1 or v[1] + v[3] > c1:
                raise TableEditError("the cells do not make a rectangle (a merged cell sticks out)")
            inside.add(cid)
    keep = g.at(r0, c0) or ids[0]
    order = sorted(inside - {keep}, key=lambda i: (g.cells[i][0], g.cells[i][1]))
    for cid in order:
        _union_into(g.m, keep, cid)
        del g.cells[cid]
    g.cells[keep] = [r0, c0, r1 - r0, c1 - c0]
    return keep


def _split_cell(model, working_pdf, g: Grid, cid: str) -> list:
    if cid not in g.cells:
        raise TableEditError("no such cell")
    r0, c0, rs, cs = g.cells[cid]
    if rs == 1 and cs == 1:
        raise TableEditError("this cell is not merged")
    n = g.node(cid)
    made = [cid]
    doc = pymupdf.open(working_pdf)
    try:
        page = doc[n.page] if n.bbox is not None and 0 <= n.page < doc.page_count else None
        d = _disp(page, n.bbox) if page is not None else None
        lines = _part_of(_lines(g, doc), n.page, cid) if page is not None else {}
        ys = [d[1]] + [lines.get("rows", {}).get(k) or d[1] + (d[3] - d[1]) * (k - r0) / rs
                       for k in range(r0 + 1, r0 + rs)] + [d[3]] if d else None
        xs = [d[0]] + [lines.get("cols", {}).get(k) or d[0] + (d[2] - d[0]) * (k - c0) / cs
                       for k in range(c0 + 1, c0 + cs)] + [d[2]] if d else None
        g.cells[cid] = [r0, c0, 1, 1]
        for i in range(rs):
            for j in range(cs):
                box = None
                if d:
                    box = _pdf(page, [xs[j], ys[i], xs[j + 1], ys[i + 1]])
                if i == 0 and j == 0:
                    if box is not None:
                        n.bbox = box
                    continue
                made.append(g.new_cell(r0 + i, c0 + j, 1, 1, like=n, page=n.page, bbox=box))
    finally:
        doc.close()
    return made


def _move_line(model, working_pdf, g: Grid, kind: str, k: int, page_no: int, pos: float,
               only: set | None = None) -> list:
    """The line between row (column) k-1 and k on the page goes to `pos`."""
    row = kind == "row"
    doc = pymupdf.open(working_pdf)
    try:
        page = doc[page_no]
        touched = []
        for cid, (r0, c0, rs, cs) in g.cells.items():
            n = g.node(cid)
            if n.bbox is None or n.page != page_no or (only is not None and cid not in only):
                continue
            s0, sp = (r0, rs) if row else (c0, cs)
            d = _disp(page, n.bbox)
            if s0 + sp == k:                       # ends at the line
                if row and pos > d[1] + 1:
                    d[3] = pos
                elif not row and pos > d[0] + 1:
                    d[2] = pos
                else:
                    raise TableEditError("the line cannot go past the next one")
            elif s0 == k:                          # starts at the line
                if row and pos < d[3] - 1:
                    d[1] = pos
                elif not row and pos < d[2] - 1:
                    d[0] = pos
                else:
                    raise TableEditError("the line cannot go past the next one")
            else:
                continue
            n.bbox = _pdf(page, d)
            n.priority = 1
            touched.append(cid)
        if not touched:
            raise TableEditError("no cells along that line on this page")
        return touched
    finally:
        doc.close()


def apply(model: DocModel, working_pdf: str, tid: str, op: dict) -> dict:
    """One change to the table's grid; returns what changed."""
    kind = str(op.get("op") or "")
    special = _apply_more(model, working_pdf, tid, kind, op)
    if special is not None:
        return special
    g = Grid(model, tid)
    touched: list = []
    if kind in ("insert_row", "insert_col"):
        at = int(op.get("at", 0))
        touched = _insert(g, "row" if kind == "insert_row" else "col", at)
    elif kind in ("delete_row", "delete_col"):
        _delete(g, "row" if kind == "delete_row" else "col", int(op.get("index", -1)),
                bool(op.get("artifact")))
    elif kind in ("split_row", "split_col"):
        touched = _split_line(model, working_pdf, g, "row" if kind == "split_row" else "col",
                              int(op.get("index", -1)), int(op.get("page", -1)), float(op.get("pos", 0)),
                              _part_cells(g, working_pdf, int(op.get("page", -1)), op.get("part")))
    elif kind in ("merge_rows", "merge_cols"):
        touched = _merge_lines(g, "row" if kind == "merge_rows" else "col", int(op.get("index", -1)))
    elif kind == "merge_cells":
        touched = [_merge_cells(g, [str(i) for i in op.get("ids") or []])]
    elif kind == "split_cell":
        touched = _split_cell(model, working_pdf, g, str(op.get("id") or ""))
    elif kind == "move_line":
        touched = _move_line(model, working_pdf, g, str(op.get("kind") or "row"), int(op.get("index", -1)),
                             int(op.get("page", -1)), float(op.get("pos", 0)),
                             _part_cells(g, working_pdf, int(op.get("page", -1)), op.get("part")))
    elif kind == "set_role":
        role = str(op.get("role") or "TD")
        if role not in ("TD", "TH"):
            raise TableEditError("a cell is TD or TH")
        scope = str(op.get("scope") or "")
        for cid in op.get("ids") or []:
            if cid in g.cells:
                n = g.node(cid)
                n.role = role
                n.scope = (scope or n.scope or "Column") if role == "TH" else ""
                n.source = "user"
                touched.append(cid)
    elif kind == "set_text":
        cid = str(op.get("id") or "")
        if cid not in g.cells:
            raise TableEditError("no such cell")
        g.node(cid).text = str(op.get("text") or "")[:4000]
        g.node(cid).source = "user"
        return {"ok": True, "touched": [cid]}
    else:
        raise TableEditError(f"unknown table change {kind}")
    g.write()
    from .repair import dedupe_cell_figures
    dedupe_cell_figures(model, {tid})
    if kind in ("split_row", "split_col", "merge_rows", "merge_cols", "merge_cells", "split_cell", "move_line"):
        _reread(model, working_pdf, [t for t in touched if t in model.nodes], tid)
    _relink_headers(model, tid)
    t = model.nodes[tid]
    t.source = "user"
    t.confidence = 1.0
    return {"ok": True, "touched": [x for x in touched if x in model.nodes]}


# ======================================================== V4.19: more editing
def _colx(part: dict, C: int, zone=None) -> list[float]:
    """The column lines 0..C of a part (display x); missing ones in between
    are spread evenly. With `zone`: the part's lines put over the zone's
    width (a zone on another page or beside the table)."""
    cols = {int(k): float(v) for k, v in (part.get("cols") or {}).items()}
    x0 = cols.get(0, part["box"][0])
    x1 = cols.get(C, part["box"][2])
    xs = []
    for k in range(C + 1):
        if k in cols:
            xs.append(cols[k])
        else:
            lo = max((j for j in cols if j < k), default=0)
            hi = min((j for j in cols if j > k), default=C)
            a, b = cols.get(lo, x0), cols.get(hi, x1)
            xs.append(a + (b - a) * (k - lo) / max(1, hi - lo))
    if zone is not None:
        w0, w1 = xs[0], xs[-1]
        z0, z1 = zone[0], zone[2]
        if not (z0 >= w0 - 20 and z1 <= w1 + 20 and abs((z1 - z0) - (w1 - w0)) < 0.5 * (w1 - w0)):
            xs = [z0 + (x - w0) * (z1 - z0) / max(1.0, w1 - w0) for x in xs]
    return xs


def _part_for_zone(g: Grid, doc, page: int, zone) -> tuple[dict | None, int]:
    """The part of the table a zone belongs with: on its page, the one most
    in line with it; else the nearest page's first part."""
    lines = _lines(g, doc)
    here = (lines.get(str(page)) or {}).get("parts", [])
    if here:
        def score(p):
            b = p["box"]
            ov = max(0.0, min(b[2], zone[2]) - max(b[0], zone[0]))
            vo = max(0.0, min(b[3], zone[3]) - max(b[1], zone[1]))
            dist = 0 if vo > 0 else min(abs(zone[1] - b[3]), abs(b[1] - zone[3]))
            return (ov + vo) - 0.2 * dist
        best = max(range(len(here)), key=lambda i: score(here[i]))
        return here[best], page
    if not lines:
        return None, page
    near = min((int(k) for k in lines), key=lambda p: abs(p - page))
    return lines[str(near)]["parts"][0], near


def _band_rows(lines: list, zone, mode: str) -> list[tuple[float, float]]:
    """Rows (display y0, y1) for the lines in a zone: one per printed line,
    or the whole zone as one row."""
    from .zonetools import bands
    if mode == "one" or not lines:
        return [(zone[1], zone[3])]
    bs = bands(lines)
    tops = [min(l.box[1] for l in b) for b in bs]
    bots = [max(l.box[3] for l in b) for b in bs]
    out = []
    for i in range(len(bs)):
        y0 = zone[1] if i == 0 else (bots[i - 1] + tops[i]) / 2
        y1 = zone[3] if i == len(bs) - 1 else (bots[i] + tops[i + 1]) / 2
        out.append((y0, y1))
    return out


def _table_keep(model: DocModel, g: Grid) -> set:
    return {g.t.id} | {d.id for d in model.descendants(g.t.id)}


def add_zone(model: DocModel, working_pdf: str, tid: str, page: int, zone, kind: str = "rows",
             mode: str = "lines") -> list:
    """What is printed in a zone joins the table: as new rows (one per
    printed line, or the zone as one row) cut at the table's column lines,
    or as a new column cut at its row lines. Tags printed wholly inside the
    zone give way to the new cells."""
    from .zonetools import lines_in, remove_tags_inside
    g = Grid(model, tid)
    doc = pymupdf.open(working_pdf)
    try:
        if not 0 <= page < doc.page_count:
            raise TableEditError("no such page")
        pg = doc[page]
        part, ppage = _part_for_zone(g, doc, page, zone)
        if part is None:
            raise TableEditError("this table has no zones on any page yet")
        if kind == "auto":
            b = part["box"]
            xo = max(0.0, min(b[2], zone[2]) - max(b[0], zone[0])) / max(1.0, zone[2] - zone[0])
            yo = max(0.0, min(b[3], zone[3]) - max(b[1], zone[1])) / max(1.0, zone[3] - zone[1])
            kind = "col" if (ppage == page and yo > 0.5 and xo < 0.5) else "rows"
        zone_pdf = _pdf(pg, zone)
        remove_tags_inside(model, page, zone_pdf, _table_keep(model, g))
        made = []
        if kind == "rows":
            xs = _colx(part, g.C, zone if ppage != page else None)
            if ppage != page:
                at = g.R if page > ppage else 0
            else:
                rows = {int(k): float(v) for k, v in part["rows"].items()}
                mid = (zone[1] + zone[3]) / 2
                after = [k for k, y in rows.items() if y >= mid and k < g.R]
                at = min(after) if after else max(rows)
                at = min(at, g.R)
            for i, (y0, y1) in enumerate(_band_rows(lines_in(pg, zone), zone, mode)):
                new = _insert(g, "row", at + i)
                for cid in new:
                    r, c, rs, cs = g.cells[cid]
                    n = g.node(cid)
                    n.page = page
                    n.bbox = _pdf(pg, [xs[c], y0, xs[c + cs], y1])
                made += new
        else:
            if ppage != page:
                raise TableEditError("draw the new column beside the table, on a page it is printed on")
            rows = {int(k): float(v) for k, v in part["rows"].items()}
            cols = {int(k): float(v) for k, v in part["cols"].items()}
            mid = (zone[0] + zone[2]) / 2
            after = [k for k, x in cols.items() if x >= mid and k < g.C]
            at = min(after) if after else max(cols)
            at = min(at, g.C)
            new = _insert(g, "col", at)
            for cid in new:
                r, c, rs, cs = g.cells[cid]
                n = g.node(cid)
                if r in rows and (r + rs) in rows:
                    n.page = page
                    n.bbox = _pdf(pg, [zone[0], rows[r], zone[2], rows[r + rs]])
            made = new
    finally:
        doc.close()
    g.write()
    return made


def remove_zone(model: DocModel, working_pdf: str, tid: str, page: int, zone, artifact: bool = False) -> list:
    """The rows (or columns) printed in a zone leave the table: their words
    become a paragraph each after the table (or an artifact)."""
    from .zonetools import place_after
    g = Grid(model, tid)
    doc = pymupdf.open(working_pdf)
    try:
        pg = doc[page]
        zp = _pdf(pg, zone)
    finally:
        doc.close()

    def inside(n):
        if n.bbox is None or n.page != page:
            return False
        cx, cy = (n.bbox[0] + n.bbox[2]) / 2, (n.bbox[1] + n.bbox[3]) / 2
        return zp[0] - 1 <= cx <= zp[2] + 1 and zp[1] - 1 <= cy <= zp[3] + 1
    hit = {cid for cid in g.cells if inside(g.node(cid))}
    if not hit:
        raise TableEditError("no cell of this table is in that zone")
    if any(g.cells[c][2] > 1 or g.cells[c][3] > 1 for c in hit):
        raise TableEditError("a merged cell is in the zone — split it first")
    rows = sorted({g.cells[c][0] for c in hit})
    whole_rows = all(all(c in hit or g.node(c).bbox is None for c, v in g.cells.items() if v[0] == r) for r in rows)
    cols = sorted({g.cells[c][1] for c in hit})
    whole_cols = all(all(c in hit or g.node(c).bbox is None or g.node(c).page != page
                         for c, v in g.cells.items() if v[1] == k) for k in cols)
    if not whole_rows and not whole_cols:
        raise TableEditError("the zone takes only part of a row — draw it over whole rows (or whole columns)")
    by_row = whole_rows
    groups: list[list[str]] = []
    keys = rows if by_row else cols
    for k in keys:
        own = sorted((c for c, v in g.cells.items() if (v[0] if by_row else v[1]) == k),
                     key=lambda c: (g.cells[c][1], g.cells[c][0]) if by_row else (g.cells[c][0], g.cells[c][1]))
        groups.append(own)
    made = []
    for own in groups:
        nodes = [g.node(c) for c in own]
        text = " ".join((n.text or "").strip() for n in nodes if (n.text or "").strip())
        boxes = [n.bbox for n in nodes if n.bbox is not None and n.page == page]
        from .zonetools import ubox
        p = StructNode(id=model.new_id("p"), role="P", page=page, bbox=ubox(boxes), text=text, source="user", priority=1,
                       note=("row" if by_row else "column") + " taken out of the table")
        if artifact:
            p.artifact, p.artifact_subtype = True, "Layout"
        made.append(p)
    for k, own in sorted(zip(keys, groups), key=lambda t: -t[0]):
        for c in own:
            del g.cells[c]
            if c in model.nodes:
                model.remove(c, promote_children=False)
        _delete(g, "row" if by_row else "col", k, False)
    g.write()
    anchor = g.t
    for n in made:
        place_after(model, anchor, [n])
        anchor = n
    return [n.id for n in made]


def _move_row(g: Grid, k: int, d: int) -> list:
    j = k + (1 if d > 0 else -1)
    if not (0 <= k < g.R and 0 <= j < g.R):
        raise TableEditError("the row is at the end of the table")
    for v in g.cells.values():
        if v[2] > 1 and (v[0] <= k < v[0] + v[2] or v[0] <= j < v[0] + v[2]):
            raise TableEditError("a merged cell runs over these rows — split it first")
    moved = []
    for cid, v in g.cells.items():
        if v[0] == k:
            v[0] = j
            moved.append(cid)
        elif v[0] == j:
            v[0] = k
    return moved


def printed_order(g: Grid, doc) -> list:
    """The cells in the order they are printed: by rows on the page (cells
    whose middles are on one line), left to right; [] when a cell has no zone."""
    items = []
    for cid in g.cells:
        n = g.node(cid)
        if n.bbox is None or not 0 <= n.page < doc.page_count:
            return []
        d = _disp(doc[n.page], n.bbox)
        items.append((n.page, (d[1] + d[3]) / 2, d[0], d[3] - d[1], cid))
    items.sort(key=lambda t: (t[0], t[1]))
    rows: list[list] = []
    for it in items:
        if rows and rows[-1][0][0] == it[0] and abs(it[1] - rows[-1][0][1]) < 0.5 * max(4.0, min(it[3], rows[-1][0][3])):
            rows[-1].append(it)
        else:
            rows.append([it])
    return [it[4] for r in rows for it in sorted(r, key=lambda t: t[2])]


def reorder_cells(g: Grid, order: list) -> list:
    """The cells read in the order given (V4.21.4): the k-th cell goes to row
    k // columns, column k % columns. Cells not named follow in their present
    order. Every cell must be one row and one column, the rows equally wide."""
    if any(v[2] > 1 or v[3] > 1 for v in g.cells.values()):
        raise TableEditError("the table has merged cells — split them first to set the order by hand")
    if len(g.cells) != g.R * g.C:
        raise TableEditError("rows differ in width — use Tidy the table first")
    named = [c for c in dict.fromkeys(str(x) for x in order) if c in g.cells]
    rest = [cid for cid, _ in sorted(g.cells.items(), key=lambda kv: (kv[1][0], kv[1][1])) if cid not in named]
    seq = named + rest
    moved = []
    for k, cid in enumerate(seq):
        pos = [k // g.C, k % g.C]
        if g.cells[cid][:2] != pos:
            moved.append(cid)
        g.cells[cid][0], g.cells[cid][1] = pos
    return moved


def _move_col(g: Grid, k: int, d: int) -> list:
    """Column k and the one beside it change places (V4.21.3): their cells are
    read in the new order in every row."""
    j = k + (1 if d > 0 else -1)
    if not (0 <= k < g.C and 0 <= j < g.C):
        raise TableEditError("the column is at the edge of the table")
    for v in g.cells.values():
        if v[3] > 1 and (v[1] <= k < v[1] + v[3] or v[1] <= j < v[1] + v[3]):
            raise TableEditError("a merged cell runs over these columns — split it first")
    moved = []
    for cid, v in g.cells.items():
        if v[1] == k:
            v[1] = j
            moved.append(cid)
        elif v[1] == j:
            v[1] = k
    return moved


def _regroup(model: DocModel, g: Grid, rows: list[int], group: str) -> None:
    """Rows into THead / TBody / TFoot: the head at the top, the foot at the
    bottom, the rest the body."""
    if group not in ("THead", "TBody", "TFoot"):
        raise TableEditError("a row group is THead, TBody or TFoot")
    t = g.t
    cur = []
    for tr in g.rows:
        p = model.nodes.get(tr.parent)
        cur.append(p.role if p is not None and p.role in ("THead", "TBody", "TFoot") else "TBody")
    for r in rows:
        if 0 <= r < len(cur):
            cur[r] = group
    # a head only from the top, a foot only at the bottom
    seen_body = False
    for i, gr in enumerate(cur):
        if gr != "THead":
            seen_body = True
        elif seen_body:
            raise TableEditError("header rows (THead) can only be at the top of the table")
    seen_foot = False
    for gr in cur:
        if gr == "TFoot":
            seen_foot = True
        elif seen_foot:
            raise TableEditError("footer rows (TFoot) can only be at the bottom of the table")
    old = {model.nodes[c].role: model.nodes[c] for c in t.children
           if c in model.nodes and model.nodes[c].role in ("THead", "TBody", "TFoot")}
    first_row_pos = next((i for i, c in enumerate(t.children) if c in model.nodes and
                          model.nodes[c].role in ("TR", "THead", "TBody", "TFoot")), len(t.children))
    before = [c for c in t.children[:first_row_pos]]
    after = [c for c in t.children[first_row_pos:] if c in model.nodes and
             model.nodes[c].role not in ("TR", "THead", "TBody", "TFoot")]
    for grp in old.values():
        grp.children = []
    kids = []
    for role in ("THead", "TBody", "TFoot"):
        rows_in = [tr for tr, gr in zip(g.rows, cur) if gr == role]
        if not rows_in:
            if role in old:
                model.nodes.pop(old[role].id, None)
            continue
        node = old.get(role) or StructNode(id=model.new_id(role.lower()), role=role, page=rows_in[0].page,
                                           source="user")
        model.nodes[node.id] = node
        node.parent = t.id
        node.children = [tr.id for tr in rows_in]
        for tr in rows_in:
            tr.parent = node.id
        kids.append(node.id)
    t.children = before + kids + after
    head = sum(1 for gr in cur if gr == "THead")
    t.style = dict(t.style or {}, header_rows=head if head else (t.style or {}).get("header_rows", 0))


def _next_table(model: DocModel, t: StructNode) -> StructNode | None:
    order = model.ordered_nodes()
    k = order.index(t)
    inside = {d.id for d in model.descendants(t.id)}
    return next((n for n in order[k + 1:] if n.role == "Table" and n.id not in inside and not n.artifact), None)


def split_table(model: DocModel, g: Grid, k: int) -> str:
    """Rows k and after become a table of their own, right after this one."""
    from .zonetools import place_after, zone_of
    if not 0 < k < g.R:
        raise TableEditError("pick a row below the first one: the new table starts there")
    for v in g.cells.values():
        if v[0] < k < v[0] + v[2]:
            raise TableEditError("a merged cell runs over the split — split that cell first")
    t = g.t
    t2 = StructNode(id=model.new_id("table"), role="Table", page=g.rows[k].page, source="user",
                    note="table split by hand", style={"header_rows": 0, "header_cols": (t.style or {}).get("header_cols", 0)})
    place_after(model, t, [t2])
    grouped = any(model.nodes[c].role in ("THead", "TBody", "TFoot") for c in t.children if c in model.nodes)
    host = t2
    if grouped:
        body = StructNode(id=model.new_id("tbody"), role="TBody", page=t2.page, source="user")
        model.add(body, t2.id)
        host = body
    for tr in g.rows[k:]:
        p = model.nodes.get(tr.parent)
        if p is not None and tr.id in p.children:
            p.children.remove(tr.id)
        model.add(tr, host.id)
    for grp in [model.nodes[c] for c in list(t.children) if c in model.nodes and model.nodes[c].role in ("THead", "TBody", "TFoot")]:
        if not grp.children:
            model.remove(grp.id)
    zone_of(model, t)
    zone_of(model, t2)
    return t2.id


def merge_next_table(model: DocModel, tid: str, drop: int = 0) -> str:
    """The next table's rows join this one (a table continued in the next
    column or on the next page). `drop`: its first rows repeat this table's
    header — they leave the table as an artifact (pagination)."""
    from .zonetools import place_after, zone_of
    t = model.nodes[tid]
    t2 = _next_table(model, t)
    if t2 is None:
        raise TableEditError("no table after this one")
    g2 = Grid(model, t2.id)
    rows2 = list(g2.rows)
    gone = rows2[:max(0, drop)]
    keep = rows2[max(0, drop):]
    if gone:
        art = StructNode(id=model.new_id("art"), role="Div", page=gone[0].page, artifact=True,
                         artifact_subtype="Pagination", source="user", note="header repeated by a continued table")
        place_after(model, t, [art])
        for tr in gone:
            for c in list(tr.children):
                n = model.nodes.get(c)
                if n is None:
                    continue
                tr.children.remove(c)
                n.role, n.artifact, n.rowspan, n.colspan, n.headers, n.scope = "P", True, 1, 1, [], ""
                model.add(n, art.id)
    body = next((model.nodes[c] for c in reversed(t.children) if c in model.nodes and model.nodes[c].role == "TBody"), None)
    host = body or t
    for tr in keep:
        p = model.nodes.get(tr.parent)
        if p is not None and tr.id in p.children:
            p.children.remove(tr.id)
        model.add(tr, host.id)
    # what else the second table held (its caption) stays where it was
    others = [c for c in t2.children if c in model.nodes and model.nodes[c].role not in ("TR", "THead", "TBody", "TFoot")]
    for c in others:
        t2.children.remove(c)
    rest_parent, rest_at = t2.parent, model.siblings(t2.id).index(t2.id)
    for tr in gone:
        if tr.id in model.nodes:
            model.remove(tr.id, promote_children=False)
    model.remove(t2.id, promote_children=False)
    for k, c in enumerate(others):
        model.add(model.nodes[c], rest_parent, rest_at + k)
    zone_of(model, t)
    return t2.id


def untable(model: DocModel, tid: str, how: str = "rows") -> list:
    """Not a table: each row (or each cell) becomes a paragraph, in place."""
    t = model.nodes[tid]
    g = Grid(model, tid)
    made = []
    if how == "cells":
        groups = [[cid] for cid, _ in sorted(g.cells.items(), key=lambda kv: (kv[1][0], kv[1][1]))]
    else:
        groups = [[cid for cid, v in sorted(g.cells.items(), key=lambda kv: kv[1][1]) if v[0] == r] for r in range(g.R)]
    for grp in groups:
        nodes = [g.node(c) for c in grp]
        text = " ".join((n.text or "").strip() for n in nodes if (n.text or "").strip())
        per: dict[int, list] = {}
        for n in nodes:
            if n.bbox is not None:
                per.setdefault(n.page, []).append(n.bbox)
        if not per and not text:
            continue
        pages = sorted(per)
        from .zonetools import ubox
        p = StructNode(id=model.new_id("p"), role="P", page=pages[0] if pages else t.page,
                       bbox=ubox(per[pages[0]]) if pages else None, text=text, source="user", priority=1,
                       note="table taken apart: not a table")
        p.parts = [[q, *ubox(per[q])] for q in pages[1:]]
        kids = [k for n in nodes for k in n.children if k in model.nodes]
        made.append((p, kids))
    others = [c for c in t.children if c in model.nodes and model.nodes[c].role not in ("TR", "THead", "TBody", "TFoot")]
    parent, at = t.parent, model.siblings(t.id).index(t.id)
    for c in others:
        t.children.remove(c)
    keep_kids = {k for _, kids in made for k in kids}
    for k in keep_kids:
        if model.nodes[k].parent in model.nodes:
            model.detach(k)
    model.remove(t.id, promote_children=False)
    i = at
    for c in others:
        model.add(model.nodes[c], parent, i)
        i += 1
    out = []
    for p, kids in made:
        model.add(p, parent, i)
        for k in kids:
            if k in model.nodes:
                model.add(model.nodes[k], p.id)
        i += 1
        out.append(p.id)
    return out


def checks(model: DocModel, g: Grid) -> list[dict]:
    """What the dialog flags about the table's structure."""
    out = []
    occ, widths = table_grid(model, g.t.id)
    if widths and len(set(widths)) > 1:
        out.append({"level": "warning", "msg": f"Rows differ in width ({sorted(set(widths))}) — a cell is missing "
                                               "or a span is wrong (red · in the grid)."})
    if not any(g.node(c).role == "TH" for c in g.cells):
        out.append({"level": "error", "msg": "No header cell (TH): set header rows or columns."})
    from .compat import house
    if not house():
        n = sum(1 for c in g.cells if g.node(c).role == "TH" and not (g.node(c).text or "").strip()
                and not g.node(c).children)
        if n:
            out.append({"level": "warning", "msg": f"{n} empty header cell(s) — the build writes them as TD."})
    nz = sum(1 for c in g.cells if g.node(c).bbox is None and (g.node(c).text or "").strip())
    if nz:
        out.append({"level": "warning", "msg": f"{nz} cell(s) have words but no zone on the page."})
    if g.R == 1 or g.C == 1:
        out.append({"level": "info", "msg": "One row or one column only — is this really a table?"})
    return out


def tidy(model: DocModel, g: Grid, drop_empty: bool = True) -> dict:
    """Put the grid right (V4.21): rows and columns with no word and no picture
    taken out, every row as wide as the table (an empty cell where one is
    missing), a header cell with no words a data cell (not for a house style)."""
    from .compat import house
    filled = dropped_r = dropped_c = th_td = 0

    def empty(cid):
        n = g.node(cid)
        return not (n.text or "").strip() and not n.children
    if drop_empty:
        for r in sorted(range(g.R), reverse=True):
            if g.R <= 1:
                break
            here = [cid for cid, v in g.cells.items() if v[0] <= r < v[0] + v[2]]
            if here and all(g.cells[c][0] == r and g.cells[c][2] == 1 and empty(c) for c in here):
                try:
                    _delete(g, "row", r, False)
                    dropped_r += 1
                except TableEditError:
                    pass
        for c in sorted(range(g.C), reverse=True):
            if g.C <= 1:
                break
            here = [cid for cid, v in g.cells.items() if v[1] <= c < v[1] + v[3]]
            if here and all(g.cells[i][1] == c and g.cells[i][3] == 1 and empty(i) for i in here):
                try:
                    _delete(g, "col", c, False)
                    dropped_c += 1
                except TableEditError:
                    pass
    occ = {}
    for cid, (r, c, rs, cs) in g.cells.items():
        for rr in range(r, r + rs):
            for cc in range(c, c + cs):
                occ[(rr, cc)] = cid
    for r in range(g.R):
        like = next((g.node(occ[(r, c)]) for c in range(g.C) if (r, c) in occ), None)
        for c in range(g.C):
            if (r, c) not in occ:
                cid = g.new_cell(r, c, like=like)
                g.node(cid).role, g.node(cid).scope = "TD", ""
                occ[(r, c)] = cid
                filled += 1
    if not house():
        for cid in g.cells:
            n = g.node(cid)
            if n.role == "TH" and empty(cid):
                n.role, n.scope = "TD", ""
                th_td += 1
    return {"filled": filled, "rows_dropped": dropped_r, "cols_dropped": dropped_c, "th_to_td": th_td}


def _apply_more(model: DocModel, working_pdf: str, tid: str, kind: str, op: dict) -> dict | None:
    """The V4.19 changes; None for the ones apply() does itself."""
    if kind in ("reorder_cells", "cells_as_printed"):
        g = Grid(model, tid)
        if kind == "cells_as_printed":
            doc = pymupdf.open(working_pdf)
            try:
                order = printed_order(g, doc)
            finally:
                doc.close()
            if not order:
                raise TableEditError("a cell has no zone on the page — its printed place is not known")
        else:
            order = op.get("order") or []
        moved = reorder_cells(g, order)
        g.write()
        _relink_headers(model, tid)
        g.t.source = "user"
        return {"ok": True, "touched": moved}
    if kind == "move_col":
        g = Grid(model, tid)
        moved = _move_col(g, int(op.get("index", -1)), int(op.get("dir", 1)))
        g.write()
        _relink_headers(model, tid)
        return {"ok": True, "touched": moved}
    if kind == "cell_order":
        g = Grid(model, tid)
        cid = str(op.get("id") or "")
        if cid not in g.cells:
            raise TableEditError("no such cell")
        n = g.node(cid)
        want = [str(x) for x in op.get("order") or []]
        if sorted(want) != sorted(n.children):
            raise TableEditError("the order must name every tag in the cell once")
        n.children = want
        n.source = "user"
        return {"ok": True, "touched": [cid]}
    if kind == "cell_order_auto":
        g = Grid(model, tid)
        ids = [i for i in (op.get("ids") or list(g.cells)) if i in g.cells]
        changed = []
        for cid in ids:
            n = g.node(cid)
            if len(n.children) < 2:
                continue
            kids = [k for k in n.children if k in model.nodes]

            def key(k):
                x = model.nodes[k]
                if x.bbox is None:
                    return (10 ** 6, 0, 0)
                # PDF space: a higher y is higher on the page; lines within 3 pt are one row
                return (x.page, -round(max(x.bbox[1], x.bbox[3]) / 3), x.bbox[0])
            new = sorted(kids, key=key) + [k for k in n.children if k not in model.nodes]
            if new != n.children:
                n.children = new
                n.source = "user"
                changed.append(cid)
        return {"ok": True, "touched": changed}
    if kind == "tidy":
        g = Grid(model, tid)
        r = tidy(model, g, bool(op.get("drop_empty", True)))
        g.write()
        _relink_headers(model, tid)
        return {"ok": True, **r}
    if kind == "empty_cells":
        g = Grid(model, tid)
        text = str(op.get("text") or "")[:60]
        n_ = 0
        for cid in g.cells:
            n = g.node(cid)
            if not (n.text or "").strip() and not n.children:
                n.actual_text = text
                n.source = "user"
                n_ += 1
        return {"ok": True, "cells": n_}
    if kind in ("row_th", "col_th", "row_td", "col_td"):
        g = Grid(model, tid)
        k = int(op.get("index", -1))
        touched = []
        for cid, (r, c, rs, cs) in g.cells.items():
            hit = (r <= k < r + rs) if kind.startswith("row") else (c <= k < c + cs)
            if not hit:
                continue
            n = g.node(cid)
            if kind.endswith("_th"):
                n.role = "TH"
                n.scope = "Column" if kind == "row_th" else "Row"
            else:
                n.role, n.scope = "TD", ""
            n.source = "user"
            touched.append(cid)
        _relink_headers(model, tid)
        return {"ok": True, "touched": touched}
    if kind == "set_texts":
        # words pasted from a spreadsheet (tab-separated): cell by cell from (r, c)
        g = Grid(model, tid)
        r0, c0 = int(op.get("r", 0)), int(op.get("c", 0))
        touched = []
        for i, row in enumerate(op.get("rows") or []):
            for j, txt in enumerate(row or []):
                cid = g.at(r0 + i, c0 + j)
                if cid is None or cid in touched or g.cells[cid][:2] != [r0 + i, c0 + j]:
                    continue
                g.node(cid).text = str(txt or "")[:4000]
                g.node(cid).source = "user"
                touched.append(cid)
        return {"ok": True, "touched": touched}
    if kind == "summary":
        t = model.nodes[tid]
        t.style = dict(t.style or {}, summary=str(op.get("text") or "").strip()[:600])
        t.source = "user"
        return {"ok": True}
    page = int(op.get("page", -1)) if op.get("page") is not None else -1
    if kind == "add_zone":
        made = add_zone(model, working_pdf, tid, page, op.get("rect"), str(op.get("kind") or "rows"),
                        str(op.get("mode") or "lines"))
        _reread(model, working_pdf, made, tid)
        _relink_headers(model, tid)
        return {"ok": True, "touched": made}
    if kind == "remove_zone":
        made = remove_zone(model, working_pdf, tid, page, op.get("rect"), bool(op.get("artifact")))
        _relink_headers(model, tid)
        return {"ok": True, "made": made}
    if kind == "move_row":
        g = Grid(model, tid)
        moved = _move_row(g, int(op.get("index", -1)), int(op.get("dir", 1)))
        g.write()
        _relink_headers(model, tid)
        return {"ok": True, "touched": moved}
    if kind == "set_group":
        g = Grid(model, tid)
        _regroup(model, g, [int(r) for r in op.get("rows") or []], str(op.get("group") or "TBody"))
        return {"ok": True}
    if kind == "split_table":
        g = Grid(model, tid)
        return {"ok": True, "new_table": split_table(model, g, int(op.get("index", -1)))}
    if kind == "merge_next_table":
        gone = merge_next_table(model, tid, int(op.get("drop", 0) or 0))
        _relink_headers(model, tid)
        return {"ok": True, "joined": gone}
    if kind == "untable":
        return {"ok": True, "made": untable(model, tid, str(op.get("how") or "rows")), "gone": True}
    if kind == "reread":
        g = Grid(model, tid)
        ids = [i for i in (op.get("ids") or list(g.cells)) if i in g.cells]
        _reread(model, working_pdf, ids, tid)
        return {"ok": True, "touched": ids}
    if kind == "rebuild":
        from .templates import apply_table_spec, table_spec
        spec = table_spec(model, working_pdf, tid)
        if op.get("cols"):
            c = max(1, min(60, int(op["cols"])))
            if c != spec["cols"]:
                spec["cols"] = c
                spec["col_fracs"] = [round(k / c, 4) for k in range(1, c)]
                spec["header_spans"] = []
        if op.get("row_mode") in ("ruled", "lines", "first_col"):
            spec["row_mode"] = op["row_mode"]
        if op.get("header_rows") is not None:
            spec["header_rows"] = int(op["header_rows"])
            spec["header_bands"] = (spec.get("header_bands") or [])[:spec["header_rows"]] + \
                [1] * max(0, spec["header_rows"] - len(spec.get("header_bands") or []))
        return apply_table_spec(model, working_pdf, spec, tid=tid)
    return None
