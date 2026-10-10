"""Journal tables: rows from the ruling, columns from the words (V4.10).

A journal set in InDesign (BMJ and the like) rules its tables with horizontal
lines or shaded row bands only. MuPDF's "lines" detection then finds the rows
but not the columns: each row comes back as one cell the width of the table,
and the cleaner either drops the rows or keeps one cell per row. MuPDF's
"text" columns, on the other side, split a cell wherever three words happen to
line up ("W|ald", "95% C|I", "1.041|to|1.066").

Here the grid is read the way a person reads the table:

  * the bands between the rules are the outer rows; inside a band a new row
    starts where a column's text starts again after a row gap (a cell's own
    lines sit closer together than two rows do)
  * the columns are the gaps that run down the whole body of the table, once
    the words of a cell have been joined into phrases
  * a cell alone in its column across a band ("<60, male" beside the ten rows
    of its group) spans the band
  * a header row that leaves the columns to the right of a heading empty while
    the header row under it fills them ("Gender" over "Male | Female") spans
    them
  * the caption bands at the top ("Table 1 …", "Table 3 Continued") and the
    notes at the bottom ("Significant associations …", "*T-test.") are left
    out of the table, so they are tagged as the Caption and the notes after it

`journal_cells(page, region)` returns the cell rectangles (MuPDF space) or None
when the region does not read as a table this way.
"""
from __future__ import annotations

import re
import statistics

import pymupdf

from .textflags import WORD_FLAGS

CAPTION_TOP_RE = re.compile(r"^\s*(?:table|tab\.)\s*[\dIVXivx]+[A-Za-z]?\b", re.I)
NOTE_START_RE = re.compile(r"^\s*(?:[*†‡§¶#]|\(?[a-z]\)?\s|[a-z](?=[A-Z]))")


class _Word:
    __slots__ = ("x0", "y0", "x1", "y1", "text")

    def __init__(self, w):
        self.x0, self.y0, self.x1, self.y1, self.text = w[0], w[1], w[2], w[3], w[4]

    @property
    def h(self):
        return self.y1 - self.y0


class _Line:
    """Words printed on one baseline inside the table, as phrases."""

    def __init__(self, words: list):
        words.sort(key=lambda w: w.x0)
        self.words = words
        self.y0 = min(w.y0 for w in words)
        self.y1 = max(w.y1 for w in words)
        h = statistics.median([w.h for w in words])
        self.h = h
        self.phrases: list[list[_Word]] = []
        for w in words:
            if self.phrases and w.x0 - self.phrases[-1][-1].x1 < 0.45 * h:
                self.phrases[-1].append(w)
            else:
                self.phrases.append([w])

    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def boxes(self):
        return [(p[0].x0, p[-1].x1) for p in self.phrases]

    def rects(self):
        """Each phrase as (x0, y0, x1, y1): its own height, not the line's."""
        return [(p[0].x0, min(w.y0 for w in p), p[-1].x1, max(w.y1 for w in p)) for p in self.phrases]


def _lines(words: list[_Word]) -> list[_Line]:
    out: list[list[_Word]] = []
    for w in sorted(words, key=lambda w: ((w.y0 + w.y1) / 2, w.x0)):
        cy = (w.y0 + w.y1) / 2
        if out:
            last = out[-1]
            ly = statistics.median([(x.y0 + x.y1) / 2 for x in last])
            if abs(cy - ly) < 0.35 * w.h:
                last.append(w)
                continue
        out.append([w])
    return [_Line(ws) for ws in out]


def _bands(tb_cells, region) -> list[float]:
    ys = sorted({c[1] for c in tb_cells if c} | {c[3] for c in tb_cells if c} | {region[1], region[3]})
    out: list[float] = []
    for y in ys:
        if region[1] - 1 <= y <= region[3] + 1 and (not out or y - out[-1] > 1.5):
            out.append(y)
    return out


def _columns(rows: list[list[_Line]], x0: float, x1: float) -> list[float]:
    """Column edges: the gaps that run down every body row."""
    spans = []
    for row in rows:
        boxes = [b for l in row for b in l.boxes()]
        if len(boxes) < 2:
            continue                         # a one-phrase row may span the table
        spans.extend(boxes)
    if not spans:
        return [x0, x1]
    spans.sort()
    merged = [list(spans[0])]
    for a, b in spans[1:]:
        if a <= merged[-1][1] + 0.8:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    edges = [x0]
    for (a0, a1), (b0, b1) in zip(merged, merged[1:]):
        # a cell's text sits a few points inside its left edge
        edges.append(max((a1 + b0) / 2, b0 - 3.5))
    edges.append(x1)
    return edges


def _col_of(edges: list[float], x: float) -> int:
    for i in range(len(edges) - 1):
        if x < edges[i + 1]:
            return i
    return len(edges) - 2


class _Group:
    """The text of one cell: a column's phrases that follow each other with no
    row gap between them."""

    def __init__(self, col: int, y0: float, y1: float, p0: float, p1: float):
        self.col = col
        self.y0, self.y1 = y0, y1
        self.p0, self.p1 = p0, p1
        self.line_tops = [y0]
        self.first = self.last = 0
        self.end_col = col


def _band_layout(lines: list[_Line], edges: list[float]):
    """Row starts and cell groups of one band. A cell's own lines sit closer
    together than two rows: a column whose text starts again after a gap wider
    than that starts a row."""
    if not lines:
        return [], []
    h = statistics.median([l.h for l in lines])
    gap = min(3.0, max(1.5, 0.22 * h))
    by_col: dict[int, list[_Group]] = {}
    for l in sorted(lines, key=lambda l: l.y0):
        for p0, q0, p1, q1 in l.rects():
            c = _col_of(edges, p0)
            gs = by_col.setdefault(c, [])
            if gs and q0 - gs[-1].y1 <= gap:
                g = gs[-1]
                if q0 > g.line_tops[-1] + 0.5:
                    g.line_tops.append(q0)
                g.y1 = max(g.y1, q1)
                g.p0, g.p1 = min(g.p0, p0), max(g.p1, p1)
            else:
                gs.append(_Group(c, q0, q1, p0, p1))
    groups = [g for gs in by_col.values() for g in gs]
    starts: list[float] = []
    for y in sorted(g.y0 for g in groups):
        if not starts or y - starts[-1] > gap:
            starts.append(y)

    def row_of(y):
        return max(i for i, s in enumerate(starts) if y >= s - 0.6) if y >= starts[0] - 0.6 else 0
    for g in groups:
        g.first = row_of(g.y0)
        g.last = max(g.first, row_of(g.line_tops[-1]))
        g.end_col = max(g.col, _col_of(edges, g.p1 - 0.5))
    if len(starts) > 1:
        # a column with one cell, in the band's first row, beside rows of
        # others: a row label for the whole band ("<60, male" over its rows)
        for c, gs in by_col.items():
            if len(gs) == 1 and gs[0].first == 0:
                gs[0].last = len(starts) - 1
    return starts, groups


def _is_note_band(lines: list[_Line], x0: float, width: float) -> bool:
    if not lines:
        return False
    for l in lines:
        if len(l.phrases) != 1 or l.words[0].x0 - x0 > 10:
            return False
    first = lines[0].text()
    words = sum(len(l.words) for l in lines)
    return bool(NOTE_START_RE.match(first)) or words >= 4 or \
        max(l.words[-1].x1 for l in lines) - x0 > 0.45 * width


def journal_cells(page: pymupdf.Page, region, tb_cells=()) -> dict | None:
    """Cells of a journal table inside `region` (x0, y0, x1, y1).

    Returns {"cells": [rect, …], "bbox": (…), "caption": rect | None,
    "notes": rect | None} or None."""
    region = tuple(region)
    x0, y0, x1, y1 = region
    try:
        raw = page.get_text("words", clip=pymupdf.Rect(region) + (-0.5, -0.5, 0.5, 0.5), flags=WORD_FLAGS)
    except Exception:
        return None
    words = [_Word(w) for w in raw if (w[4] or "").strip()
             and x0 - 1 <= (w[0] + w[2]) / 2 <= x1 + 1 and y0 - 1 <= (w[1] + w[3]) / 2 <= y1 + 1]
    if len(words) < 4:
        return None
    lines = _lines(words)
    bands = _bands(tb_cells, region)
    if len(bands) < 2:
        bands = [y0, y1]
    band_lines: list[list[_Line]] = []
    for a, b in zip(bands, bands[1:]):
        band_lines.append([l for l in lines if a - 0.5 <= (l.y0 + l.y1) / 2 < b + 0.5])

    # caption bands on top, empty bands, note bands at the bottom
    top = 0
    caption = None
    while top < len(band_lines):
        bl = band_lines[top]
        if not bl:
            top += 1
            continue
        if CAPTION_TOP_RE.match(bl[0].text()) and caption is None:
            caption = (x0, bands[top], x1, bands[top + 1])
            top += 1
            continue
        break
    bottom = len(band_lines)
    notes = None
    while bottom > top + 1:
        bl = band_lines[bottom - 1]
        if not bl:
            bottom -= 1
            continue
        if _is_note_band(bl, x0, x1 - x0):
            notes = (x0, bands[bottom - 1], x1, notes[3] if notes else bands[bottom])
            bottom -= 1
            continue
        break
    body = [(bands[i], bands[i + 1], band_lines[i]) for i in range(top, bottom) if band_lines[i]]
    if len(body) < 1:
        return None
    # a note printed in the last band under the body rows ("Significant …")
    last_a, last_b, last = body[-1]
    if len(last) >= 2:
        tail = []
        while len(last) - len(tail) > 1 and _is_note_band([last[-1 - len(tail)]], x0, x1 - x0) \
                and NOTE_START_RE.match(last[-1 - len(tail)].text()):
            tail.append(last[-1 - len(tail)])
        if tail:
            cut = min(l.y0 for l in tail) - 0.5
            body[-1] = (last_a, cut, last[:len(last) - len(tail)])
            notes = (x0, cut, x1, notes[3] if notes else last_b)

    edges = _columns([bl for _, _, bl in body], x0, x1)
    if len(edges) < 3:
        return None
    # a bulleted or numbered list between two rules is not a table: its first
    # "column" is nothing but the labels
    first = [" ".join(w.text for w in ph) for _, _, bl in body for l in bl for ph in l.phrases
             if _col_of(edges, ph[0].x0) == 0]
    if first and all(re.fullmatch(r"[•●▪■◦○–—·‣⁃➢➤►✓✔∙◆◇♦⇒→*-]|\(?\d{1,2}[.)]|\(?[a-z][.)]", t) for t in first):
        return None
    ncols = len(edges) - 1
    tops: list[float] = []
    bottoms: list[float] = []
    groups: list[_Group] = []
    for a, b, bl in body:
        starts, gs = _band_layout(bl, edges)
        if not starts:
            continue
        base = len(tops)
        for k, s0 in enumerate(starts):
            top_y = a if k == 0 else max(s0 - 1.0, tops[-1] + 0.5)
            if k:
                bottoms[-1] = top_y
            tops.append(top_y)
            bottoms.append(b)
        for g in gs:
            g.first += base
            g.last += base
        groups.extend(gs)
    if not tops:
        return None
    nrows = len(tops)
    filled = [set() for _ in range(nrows)]
    for g in groups:
        filled[g.first].add(g.col)
    taken: dict[tuple[int, int], _Group] = {}
    # wider and taller cells first, so a plain cell never cuts into them
    for g in sorted(groups, key=lambda g: (-(g.last - g.first), g.first, g.col)):
        r = g.first
        end = g.end_col
        # a heading row over the columns it heads: "Gender" over Male | Female
        # (only the rows on top, above the first row with a row label)
        if g.first == g.last and r + 1 < nrows and all(0 not in filled[k] for k in range(r + 2)) \
                and not filled[r] >= filled[r + 1]:
            while end + 1 < ncols and end + 1 not in filled[r] and end + 1 in filled[r + 1]:
                end += 1
        cols = range(g.col, end + 1)
        rows_ = range(g.first, g.last + 1)
        if any((rr, cc) in taken for rr in rows_ for cc in cols):
            # keep what is free: the first row, then as many columns as fit
            rows_ = range(g.first, g.first + 1)
            if (g.first, g.col) in taken:
                continue
            end = g.col
            while end + 1 <= g.end_col and (g.first, end + 1) not in taken:
                end += 1
            cols = range(g.col, end + 1)
        g.first, g.last, g.end_col = rows_[0], rows_[-1], cols[-1]
        for rr in rows_:
            for cc in cols:
                taken[(rr, cc)] = g
    cells = []
    for g in {id(g): g for g in taken.values()}.values():
        cells.append((edges[g.col], tops[g.first], edges[g.end_col + 1], bottoms[g.last]))
    for r in range(nrows):
        for c in range(ncols):
            if (r, c) not in taken:
                cells.append((edges[c], tops[r], edges[c + 1], bottoms[r]))
    rows = list(zip(tops, bottoms))
    bbox = (x0, rows[0][0], x1, rows[-1][1])
    return {"cells": cells, "bbox": bbox, "caption": caption, "notes": notes,
            "rows": nrows, "cols": ncols}


def rule_regions(rules: list, page_width: float, taken: list) -> list:
    """Tables ruled with horizontal lines only (no cell edges for MuPDF to
    find): three or more rules of one width stacked down the page. Returns
    [(region, band cells)] for the stacks that no table found already covers."""
    rows = sorted((r for r in rules if r[2] - r[1] >= max(80.0, 0.12 * page_width)), key=lambda r: r[0])
    groups: list[list] = []
    for y, a, b in rows:
        for g in groups:
            ly, la, lb = g[-1]
            if abs(la - a) < 3 and abs(lb - b) < 3 and 0.5 < y - ly < 140:
                g.append((y, a, b))
                break
        else:
            groups.append([(y, a, b)])
    out = []
    for g in groups:
        if len(g) < 3:
            continue
        x0, x1 = min(r[1] for r in g), max(r[2] for r in g)
        region = (x0, g[0][0], x1, g[-1][0])
        if any(min(region[2], t[2]) - max(region[0], t[0]) > 0 and
               min(region[3], t[3]) - max(region[1], t[1]) > 0 for t in taken):
            continue
        cells = [(x0, a[0], x1, b[0]) for a, b in zip(g, g[1:])]
        out.append((region, cells))
    return out
