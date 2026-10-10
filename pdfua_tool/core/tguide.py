"""Academain Teacher's Guide structure (V4.24).

The Academain client's Teacher's Guides (Benchmark Phonics Intervention, one
book per unit: cover, contents, ten lessons of four pages, a "stop and assess"
page, the components page) are tagged the way the client's own finished book
X159397 is. That book was read tag by tag; this is its structure:

  Document > Sect (the whole book)
    cover      Figure (programme logo) · P unit name · P level · P "UNIT 15 Rr"
               (/ActualText "UNIT 15 uppercase R, lowercase r") · H1 Teacher's
               Guide · Figure (cover photo) · Figure (publisher logo)
    contents   H2 Table of Contents · TOC > TOCI > Link
    a lesson   H2  Lesson 1 (20–30 MIN.)
               H3  the parts in the red / blue slab serif: Materials, Warm-Up
                   Review and Repetition, Multimodal Mini-Lessons, Vocabulary
                   Booster, Apply to Reading and Writing, Additional Supports
               H4  the bold condensed sub-heads (Reread to Build Fluency,
                   Additional Materials, Introduce, Read Aloud: "…")
               H5  the head of each column of a ruled box (Provide Corrective
                   Feedback | Use Student Observation Form | Extend Practice) —
                   the boxes are NOT tables: each column is H5 + P, read
                   column after column
               P   every paragraph; each word of a blending grid its own P
                   (row by row); "_____" a P read "Blank line"
               L > LI > Lbl + LBody  the bullet and numbered lists
               Figure  pictures, icons, badges (with the text printed in them)
    page number  P read "Page 2" (the running foot "© Benchmark …", "Unit 15
                 • Rr", the lesson tab on the page edge and the printer's
                 marks are artifacts)
  /ActualText: "Rr" → "uppercase R, lowercase r"; "R R R r r r" → "Three
  Uppercase R, Three Lowercase r"; "B H P b h p" → "uppercase B, uppercase H,
  …"; "_____" → "Blank line"; ISBN and the product code read digit by digit.

The analysis (V4) runs first; this pass keeps its figures, its table of
contents, its artifacts and its reading order, and makes the text structure
again from the printed lines and their type styles. It runs for a job whose
`teachers_guide` option is "on", or "auto" (the Academain profile) when the
book is a Teacher's Guide: "Teacher's Guide" on its first pages and lessons
headed "Lesson N (20–30 MIN.)".
"""
from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import pymupdf

from .model import HEADINGS, DocModel, StructNode
from .textflags import RAW_FLAGS

TG_TITLE = re.compile(r"Teacher[’'`]?s\s+Guide", re.I)
LESSON_RE = re.compile(r"^\s*Lesson\s+(\d+)\s*\(\s*\d+\s*[–\-]\s*\d+\s*MIN\.?\s*\)", re.I)
LESSON_ANY = re.compile(r"Lesson\s+\d+\s*\(\s*\d+\s*[–\-]\s*\d+\s*MIN", re.I)
BULLETS = "•●▪■◦‣–"
NUM_ITEM = re.compile(r"^\s*(\d{1,2}|[a-z])[.)]\s+\S")
GRID_FONTS = ("BECAG", "SweaterSchool", "Sassoon", "Zaner", "Schoolbook")
LOGO_FONTS = ("Urbane",)
PAIR_RE = re.compile(r"(?<![A-Za-z])([A-Z])([a-z])(?![A-Za-z])")
BLANK_RE = re.compile(r"_{3,}")
KEEP_ROLES = {"Figure", "Formula", "Form"}
NUMBER_WORDS = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six", 7: "Seven", 8: "Eight",
                9: "Nine", 10: "Ten"}


# ------------------------------------------------------------ is it one?
def detect(path: str) -> bool:
    """A Teacher's Guide of the Academain kind: the title on its first pages
    and at least one lesson head "Lesson N (20–30 MIN.)"."""
    try:
        doc = pymupdf.open(path)
    except Exception:
        return False
    try:
        head = " ".join(doc[i].get_text("text", flags=RAW_FLAGS) for i in range(min(3, doc.page_count)))
        if not TG_TITLE.search(head):
            return False
        for i in range(min(doc.page_count, 14)):
            if LESSON_ANY.search(" ".join(doc[i].get_text("text", flags=RAW_FLAGS).split())):
                return True
        return False
    finally:
        doc.close()


def wanted(opts, path: str) -> bool:
    mode = str(getattr(opts, "teachers_guide", "off") or "off")
    if mode == "on":
        return True
    if mode == "auto":
        return detect(path)
    return False


# ------------------------------------------------------------ the printed lines
@dataclass
class Line:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    chars: list                     # (c, x0, x1) in PDF space, left to right
    font: str
    size: float
    color: int
    bold: bool
    horiz: bool
    cls: str = ""
    region: str = ""
    boxed: bool = False

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def h(self) -> float:
        return self.y1 - self.y0


def _bold(font: str, flags: int) -> bool:
    return bool(re.search(r"(Bold|ExtraB|Heavy|Black|Semibold|SemiBold|Demi|-B$|-E$|-SB$|BoldCn)", font)) \
        or bool(flags & 16)


def _to_pdf(M, r) -> tuple:
    rr = pymupdf.Rect(r) * M
    return (rr.x0, rr.y0, rr.x1, rr.y1)


def page_lines(page, pno: int) -> list[Line]:
    """Every printed line of the page in PDF space, with its type style (the
    style the most characters of the line are set in)."""
    Mi = ~page.transformation_matrix
    out: list[Line] = []
    raw = page.get_text("rawdict", flags=RAW_FLAGS)
    for b in raw.get("blocks", []):
        for ln in b.get("lines", []):
            d = ln.get("dir", (1, 0))
            horiz = abs(d[1]) < 0.2 and d[0] > 0
            chars = []
            weight: Counter = Counter()
            info = {}
            for s in ln.get("spans", []):
                key = (s.get("font", ""), round(float(s.get("size", 0)), 1), int(s.get("color", 0)),
                       int(s.get("flags", 0)))
                for ch in s.get("chars", []):
                    c = ch.get("c", "")
                    x0, y0, x1, y1 = _to_pdf(Mi, ch["bbox"])
                    chars.append((c, x0, x1, y0, y1, key))
                    if c.strip():
                        weight[key] += 1
                info.setdefault(key, s)
            if not chars or not weight:
                continue
            if horiz:
                chars.sort(key=lambda t: t[1])
            text = "".join(c[0] for c in chars)
            if not text.strip():
                continue
            font, size, color, flags = weight.most_common(1)[0][0]
            xs0 = [c[1] for c in chars if c[0].strip()] or [c[1] for c in chars]
            xs1 = [c[2] for c in chars if c[0].strip()] or [c[2] for c in chars]
            ys0 = [c[3] for c in chars if c[0].strip()] or [c[3] for c in chars]
            ys1 = [c[4] for c in chars if c[0].strip()] or [c[4] for c in chars]
            out.append(Line(pno, min(xs0), min(ys0), max(xs1), max(ys1), " ".join(text.split()),
                            [(c[0], c[1], c[2], c[5]) for c in chars], font, size, color,
                            _bold(font, flags), horiz))
    return out


def _split_logo(lines: list[Line]) -> tuple[list[Line], list[Line]]:
    """The programme logo's letters ("P H O N I C S" in the logo face) are part
    of the logo picture; a line that has them and a title ("… Components at a
    Glance") is cut in two."""
    keep, logo = [], []
    for ln in lines:
        lg = [c for c in ln.chars if any(f in c[3][0] for f in LOGO_FONTS)]
        if not lg:
            keep.append(ln)
            continue
        rest = [c for c in ln.chars if c not in lg]
        logo.append(_sub(ln, lg))
        if "".join(c[0] for c in rest).strip():
            keep.append(_sub(ln, rest))
    return keep, logo


def _sub(ln: Line, chars: list) -> Line:
    vis = [c for c in chars if c[0].strip()] or chars
    weight = Counter(c[3] for c in vis)
    font, size, color, flags = weight.most_common(1)[0][0]
    return Line(ln.page, min(c[1] for c in vis), ln.y0, max(c[2] for c in vis), ln.y1,
                " ".join("".join(c[0] for c in chars).split()), list(chars), font, size, color,
                _bold(font, flags), ln.horiz, region=ln.region, boxed=ln.boxed)


# ------------------------------------------------------------ type styles → roles
def style_class(ln: Line) -> str:
    """What the type style of a line says it is."""
    f = ln.font
    if any(g in f for g in GRID_FONTS):
        return "grid_big" if ln.size >= 16 else "grid"
    if LESSON_RE.match(ln.text):
        return "H2"
    if ln.size >= 36 and ln.bold:
        return "H1"
    if ln.size >= 26 and ln.bold:
        return "H2"
    if "Rockwell" in f and ln.bold and ln.size >= 12.5:
        return "H3"
    if ln.bold and 13.5 <= ln.size < 26 and "Condensed" in f and "Extra" not in f.split("Condensed")[0]:
        # FiraSansCondensed-ExtraB 14: the sub-heads
        return "H4"
    if ln.bold and 13.5 <= ln.size < 26 and re.search(r"FiraSans-(Bold|SemiBold)", f):
        return "HX"                      # "Make Informed Decisions"
    if ln.bold and 12.5 <= ln.size < 13.5:
        return "HB"                      # the bold head of a box column (or a sub-head)
    if ln.bold and 11.5 <= ln.size < 12.5 and ln.color not in (0x231f20, 0, 0xffffff):
        return "H4"                      # "Additional Materials" in the lesson's colour
    return "body"


def sentence_like(text: str) -> bool:
    """A bold line that is a sentence ("Review your notes … work.") is a paragraph."""
    t = text.strip()
    return len(t.split()) > 6 and t.endswith((".", "!", "?"))


# ------------------------------------------------------------ blocks
@dataclass
class Block:
    lines: list = field(default_factory=list)
    kind: str = "body"                  # body | grid | grid_big | H1… | HB | HX | item | pagenum | badge
    region: str = ""
    role: str = "P"
    text: str = ""
    at: str = ""
    lbl: tuple | None = None            # the bullet's box (an item)
    order: tuple = ()
    page: int = 0

    @property
    def bbox(self):
        return (min(l.x0 for l in self.lines), min(l.y0 for l in self.lines),
                max(l.x1 for l in self.lines), max(l.y1 for l in self.lines))


def _merge_boxes(boxes: list, share: float = 0.3) -> list:
    """Boxes that overlap by `share` of the smaller one become one."""
    out = [tuple(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        for i in range(len(out)):
            for j in range(i + 1, len(out)):
                a, b = out[i], out[j]
                if _inter(a, b) > share * max(1.0, min(_area(a), _area(b))) or _touching(a, b):
                    out[i] = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
                    out.pop(j)
                    changed = True
                    break
            if changed:
                break
    return out


def _blank(page, r) -> bool:
    """Nothing to see: the picture shows one flat colour where it is printed."""
    try:
        clip = pymupdf.Rect(r) * page.transformation_matrix
        pix = page.get_pixmap(clip=clip, dpi=30, colorspace=pymupdf.csGRAY, alpha=False)
        vals = pix.samples
        if not vals:
            return True
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        return var < 2.5
    except Exception:
        return False


def _touching(a, b, tol: float = 1.5) -> bool:
    """Two slices of one picture: edge to edge, the same width (or height)."""
    same_x = abs(a[0] - b[0]) <= 2 and abs(a[2] - b[2]) <= 2
    same_y = abs(a[1] - b[1]) <= 2 and abs(a[3] - b[3]) <= 2
    if same_x and (abs(a[1] - b[3]) <= tol or abs(b[1] - a[3]) <= tol):
        return True
    if same_y and (abs(a[0] - b[2]) <= tol or abs(b[0] - a[2]) <= tol):
        return True
    return False


def _overlap_x(a, b) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0]))


def _area(r) -> float:
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def _inter(a, b) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def _inside(pt, r, pad=0.5) -> bool:
    return r[0] - pad <= pt[0] <= r[2] + pad and r[1] - pad <= pt[1] <= r[3] + pad


class TeachersGuide:
    def __init__(self, model: DocModel, path: str, pages=None, order_mode: str = "xy"):
        self.model = model
        self.order_mode = order_mode
        self._moved: dict = {}
        self.path = path
        self.pages = sorted(pages) if pages is not None else None
        self.stats: Counter = Counter()

    # -------------------------------------------------------------- run
    def run(self) -> dict:
        doc = pymupdf.open(self.path)
        try:
            pages = self.pages if self.pages is not None else list(range(doc.page_count))
            lines = {p: page_lines(doc[p], p) for p in pages if 0 <= p < doc.page_count}
            self.step = self._steps([l for p in lines for l in lines[p]])
            order: list[str] = []
            for p in pages:
                if p not in lines:
                    continue
                try:
                    self._page(doc[p], p, lines[p])
                except Exception as exc:              # the V4 page stays as it was
                    self.model.warnings.append(f"Teacher's Guide page {p + 1}: {exc}")
        finally:
            doc.close()
        self._cleanup()
        self._reorder_roots()
        self.model.info["teachers_guide"] = dict(self.stats)
        return dict(self.stats)

    def _cleanup(self) -> None:
        """No child that is gone, no group left empty, no node without its parent."""
        m = self.model
        from .model import CONTAINER_ROLES
        for _ in range(4):
            for n in m.nodes.values():
                n.children = [c for c in n.children if c in m.nodes]
            m.root_children = [c for c in m.root_children if c in m.nodes]
            empty = [n.id for n in m.nodes.values() if not n.children and not n.artifact
                     and (n.role in CONTAINER_ROLES or n.role in ("TR", "THead", "TBody", "TFoot"))
                     and n.role != "TOC"]
            for nid in empty:
                self._unhook(nid)
                m.nodes.pop(nid, None)
            if not empty:
                break
        placed = set(m.root_children)
        for n in m.nodes.values():
            if n.parent is not None and n.parent not in m.nodes:
                n.parent = None
            if n.parent is None and n.id not in placed:
                m.root_children.append(n.id)
                placed.add(n.id)

    def _reorder_roots(self) -> None:
        """The top of the tree page by page (a page read again keeps its place)."""
        m = self.model

        def first_page(nid):
            n = m.nodes[nid]
            if n.page is not None and n.page >= 0:
                return n.page
            ps = [d.page for d in m.descendants(nid) if d.page is not None and d.page >= 0]
            return min(ps) if ps else 10 ** 6
        keyed = [(first_page(r), k, r) for k, r in enumerate(m.root_children)]
        keyed.sort(key=lambda t: (t[0], t[1]))
        m.root_children = [r for _, _, r in keyed]

    @staticmethod
    def _steps(lines: list[Line]) -> dict:
        """The usual distance from one line to the next of a paragraph, per
        type size — a bigger step starts a new paragraph."""
        by: dict = defaultdict(list)
        ls = sorted([l for l in lines if l.horiz], key=lambda l: (l.page, round(l.x0), -l.y1))
        for a, b in zip(ls, ls[1:]):
            if a.page != b.page or abs(a.x0 - b.x0) > 2 or abs(a.size - b.size) > 0.3:
                continue
            d = a.y0 - b.y0
            if 0.8 * a.size < d < 2.2 * a.size:
                by[round(a.size)].append(round(d, 1))
        return {k: statistics.median(v) for k, v in by.items() if len(v) >= 3}

    def _lead(self, size: float) -> float:
        return self.step.get(round(size), 1.3 * size)

    # -------------------------------------------------------------- one page
    def _page(self, page, p: int, lines: list[Line]) -> None:
        m = self.model
        crop = _to_pdf(~page.transformation_matrix, page.rect)
        bottom, top = min(crop[1], crop[3]), max(crop[1], crop[3])
        left, right = min(crop[0], crop[2]), max(crop[0], crop[2])
        on_page = [n for n in m.nodes.values() if n.page == p or any(int(q[0]) == p for q in n.parts)]
        # what the analysis found and this pass keeps
        keep = [n for n in on_page if not n.artifact and n.role in KEEP_ROLES and n.bbox is not None and n.page == p]
        tocs = [n for n in on_page if not n.artifact and n.role == "TOC" and n.page == p]
        toc_boxes = [self._box_of(t) for t in tocs]
        toc_boxes = [b for b in toc_boxes if b]
        cells = [n for n in on_page if not n.artifact and n.role in ("TD", "TH") and n.bbox is not None and n.page == p]
        lines, logo = _split_logo(lines)
        self._fig_anchor: dict = {}
        keep, fig_text = self._pictures(page, p, keep, lines)
        keep = self._merge_figures(keep)
        # ---------------------------------------------------- the lines
        text_lines: list[Line] = []
        rot: list[Line] = []
        pagenum: list[Line] = []
        arts: list[tuple] = []
        for ln in lines:
            if ln.y1 <= bottom + 18 or ln.y0 >= top - 14 or ln.x1 <= left + 12 or ln.x0 >= right - 12:
                arts.append((ln, "Layout", ""))            # slug, printer's marks
                continue
            if not ln.horiz:
                if ln.x0 > right - 70 or ln.x1 < left + 30:
                    arts.append((ln, "Pagination", "Header"))   # the lesson tab on the page edge
                    continue
                rot.append(ln)
                continue
            if ln.y1 < bottom + 45:                        # the running foot
                if re.fullmatch(r"\d{1,3}(\s+\d{1,3})?", ln.text.strip()) and not pagenum:
                    pagenum.append(ln)
                else:
                    arts.append((ln, "Pagination", "Footer"))
                continue
            if any(_inside((ln.cx, ln.cy), b) for b in toc_boxes):
                continue
            if id(ln) in fig_text:
                continue                                   # printed in a picture
            text_lines.append(ln)
        # badges set in a circle (the PD seal): a picture of their own
        new_figs = self._badges(p, rot, text_lines, keep)
        for nf in new_figs:
            text_lines = [l for l in text_lines if not _inside((l.cx, l.cy), nf.bbox, 1.0)
                          or len(l.text.replace(" ", "")) > 6]
        # the logo letters join the picture next to them (or make one)
        if logo:
            self._logo(p, logo, keep)
        boxes = self._ruled_boxes(page)
        for ln in text_lines:
            ln.cls = style_class(ln)
            cell = next((c for c in cells if _inside((ln.cx, ln.cy), c.bbox, 0.5)), None)
            ln.region = cell.id if cell is not None else ""
            ln.boxed = bool(ln.region) or any(_inside((ln.cx, ln.cy), r, 0) for r in boxes)
        if p == 0:
            for ln in text_lines:
                if ln.cls not in ("H1",):
                    ln.cls = "grid" if ln.cls == "grid" else "body"
        text_lines = self._join_baseline(text_lines)
        blocks = self._blocks(text_lines)
        blocks += [self._pagenum(ln) for ln in pagenum[:1]]
        new_figs += self._side_icons(page, p, blocks, keep + new_figs, text_lines)
        self._badge_unit(blocks)
        # ---------------------------------------------------- the order
        order_ix = self._v4_order(on_page)
        self._fix_order(on_page, order_ix)
        for b in blocks:
            b.page = p
            b.order = self._key(b, on_page, order_ix)
        figures = list(keep) + new_figs
        fig_keys = {f.id: self._fig_key(f, on_page, order_ix) for f in figures}
        toc_keys = {t.id: (order_ix.get(t.id, 10 ** 6), -(self._box_of(t) or (0, 0, 0, 0))[3], 0) for t in tocs}
        # ---------------------------------------------------- the new tree of the page
        links = [n for n in on_page if n.role == "Link" and not n.artifact and n.annots
                 and not any(a.role == "TOC" for a in m.ancestors(n.id))]
        drop = [n for n in on_page if not n.artifact and n.role not in KEEP_ROLES and n.page == p
                and not any(a.role == "TOC" for a in m.ancestors(n.id)) and n.role != "TOC"
                and n.id not in {l.id for l in links}]
        # artifacts the new text now owns (a lesson head the analysis took for a running head)
        for a in [n for n in on_page if n.artifact and n.page == p and n.bbox is not None and (n.text or "").strip()]:
            if any(_inter(a.bbox, b.bbox) > 0.4 * max(1.0, _area(a.bbox)) for b in blocks):
                self._unhook(a.id)
                for d in m.descendants(a.id):
                    m.nodes.pop(d.id, None)
                m.nodes.pop(a.id, None)
        for f in figures:
            self._unhook(f.id)
        for t in tocs:
            self._unhook(t.id)
        for l in links:
            self._unhook(l.id)
        for n in drop:
            if n.id in m.nodes:
                self._unhook(n.id)
        for n in drop:
            m.nodes.pop(n.id, None)
        self._artifacts(p, arts + [(l, "Layout", "") for l in rot if not any(
            _inside((l.cx, l.cy), f.bbox, 1.0) for f in figures if f.bbox is not None)])
        items: list[tuple] = [(b.order, "b", b) for b in blocks]
        items += [(fig_keys[f.id], "f", f) for f in figures]
        items += [(toc_keys[t.id], "t", t) for t in tocs]
        items.sort(key=lambda x: x[0])
        # what shares one place of the analysis's order (a box cell, a merged
        # region) is read by its own layout: columns left to right
        grouped: list = []
        for it in items:
            if grouped and grouped[-1][0][0][0] == it[0][0]:
                grouped[-1].append(it)
            else:
                grouped.append([it])
        items = [x for g in grouped for x in (self._xy_items(g) if len(g) > 2 else g)]
        items = self._icons_first(items)
        made = self._emit(p, items, links)
        self.stats["pages"] += 1
        self.stats["nodes"] += made

    @staticmethod
    def _ruled_boxes(page) -> list:
        """The ruled boxes of the page (a box with its column rules is where a
        bold head is the head of a column, H5)."""
        out = []
        Mi = ~page.transformation_matrix
        W, H = page.rect.width, page.rect.height
        try:
            for dr in page.get_drawings():
                if "s" not in str(dr.get("type", "")):
                    continue
                r = dr["rect"]
                if r.width < 40 or r.height < 18 or (r.width > 0.9 * W and r.height > 0.9 * H):
                    continue
                kinds = "".join(str(it[0]) for it in dr.get("items", []))
                if kinds in ("re",) or kinds.count("l") >= 4 or "qu" in kinds:
                    out.append(_to_pdf(Mi, r))
        except Exception:
            pass
        return out

    def _artifacts(self, p: int, arts: list) -> None:
        """What this pass leaves out of the text (the running foot, the lesson
        tab, the slug) is marked an artifact, so that no tag takes it in."""
        m = self.model
        olds = [n for n in m.nodes.values() if n.artifact and n.page == p and n.bbox is not None]
        for ln, typ, sub in arts:
            box = (ln.x0, ln.y0, ln.x1, ln.y1)
            if any(_inter(o.bbox, box) > 0.8 * max(1.0, _area(box)) for o in olds):
                continue
            n = StructNode(id=m.new_id("tgart"), role="Artifact", page=p, bbox=box, artifact=True,
                           artifact_subtype=typ, pagination_subtype=sub, text=ln.text[:200], confidence=1.0,
                           note="Teacher's Guide: not read")
            m.add(n)
            olds.append(n)
            self.stats["artifacts"] += 1

    # -------------------------------------------------------------- helpers
    def _box_of(self, n: StructNode):
        boxes = [d.bbox for d in [n] + self.model.descendants(n.id) if d.bbox is not None and d.page == n.page]
        if not boxes:
            return None
        return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))

    def _unhook(self, nid: str) -> None:
        m = self.model
        n = m.nodes.get(nid)
        if n is None:
            return
        sib = m.root_children if n.parent is None or n.parent not in m.nodes else m.nodes[n.parent].children
        if nid in sib:
            sib.remove(nid)
        n.parent = None

    def _v4_order(self, on_page) -> dict:
        ids = {n.id for n in on_page}
        return {n.id: k for k, n in enumerate(self.model.ordered_nodes()) if n.id in ids}

    def _key(self, b: Block, on_page, order_ix) -> tuple:
        """A block is read where the analysis read the text it covers (its
        column, its box); inside one of those, top to bottom."""
        bb = b.bbox
        best, score = None, 0.0
        for n in on_page:
            if n.bbox is None or n.page != b.page or n.id not in order_ix:
                continue
            if n.role in ("Table", "TBody", "THead", "TFoot", "TR", "L", "LI", "Sect", "Div", "TOC"):
                continue
            s = _inter(bb, n.bbox)
            if s > score:
                best, score = n, s
        if b.kind == "pagenum":
            return (10 ** 7, 0, 0)
        if best is not None and best.id not in self._moved:
            # a list item of the margin column that the analysis stretched over
            # the main column does not carry the main column's words
            for a in [best] + self.model.ancestors(best.id):
                if a.role == "LI":
                    lbl = next((self.model.nodes[c] for c in a.children if c in self.model.nodes
                                and self.model.nodes[c].role == "Lbl" and self.model.nodes[c].bbox), None)
                    if lbl is not None and bb[0] - lbl.bbox[0] > 120:
                        best = None
                    break
        if best is None:
            # nothing there: read by place among the blocks (the top of the page first)
            return (self._nearest_above(bb, on_page, order_ix), -bb[3], bb[0])
        return (self._anchor(best, order_ix), -bb[3], bb[0])

    def _anchor(self, n: StructNode, order_ix) -> float:
        """Text in a box (a cell) is read in the box's turn; a figure in it too."""
        m = self.model
        if n.id in self._moved:
            return self._moved[n.id]
        for a in [n] + m.ancestors(n.id):
            if a.role in ("TD", "TH") and a.id in order_ix:
                return order_ix[a.id]
            if a.role in ("LI",) and a.id in order_ix:
                return order_ix[a.id]
        return order_ix.get(n.id, 10 ** 6)

    def _far_item(self, n: StructNode, x0: float) -> bool:
        """`n` is (in) a list item of the margin column whose label is far left of x0."""
        m = self.model
        for a in [n] + m.ancestors(n.id):
            if a.role == "LI":
                lbl = next((m.nodes[c] for c in a.children if c in m.nodes and m.nodes[c].role == "Lbl"
                            and m.nodes[c].bbox), None)
                return lbl is not None and x0 - lbl.bbox[0] > 120
        return False

    def _fix_order(self, on_page, order_ix) -> None:
        """Lines of the main column the analysis put into a list item of the
        margin column (its label far to the left) are read where they are
        printed: after the main-column text above them."""
        m = self.model
        self._moved = {}
        leaves = [x for x in on_page if x.bbox is not None and not x.artifact and x.id in order_ix
                  and x.role not in ("Sect", "Div", "Table", "TBody", "THead", "TFoot", "TR", "L", "LI", "TOC")]
        bad = set()
        for n in leaves:
            for a in m.ancestors(n.id):
                if a.role == "LI":
                    lbl = next((m.nodes[c] for c in a.children if c in m.nodes and m.nodes[c].role == "Lbl"
                                and m.nodes[c].bbox), None)
                    if lbl is not None and n.bbox[0] - lbl.bbox[0] > 120:
                        bad.add(n.id)
                    break
        for nid in bad:
            n = m.nodes[nid]
            above = [c for c in leaves if c.id not in bad and c.bbox[1] >= n.bbox[3] - 2
                     and _overlap_x(c.bbox, n.bbox) > 0 and not self._far_item(c, n.bbox[0])]
            if above:
                c = min(above, key=lambda c: c.bbox[1] - n.bbox[3])
                self._moved[nid] = self._anchor(c, order_ix) + 0.5
            self.stats["order_fixed"] += 1

    def _nearest_above(self, bb, on_page, order_ix) -> int:
        cands = [n for n in on_page if n.bbox is not None and n.id in order_ix and not n.artifact
                 and n.role not in ("Sect", "Div", "Table", "TBody", "TR", "L", "LI")
                 and n.bbox[1] >= bb[3] - 2 and _overlap_x(n.bbox, bb) > 0
                 and (n.id in self._moved or not self._far_item(n, bb[0]))]
        if cands:
            n = min(cands, key=lambda n: n.bbox[1] - bb[3])
            return self._anchor(n, order_ix)
        return -1                                         # the top of the page: first

    def _fig_key(self, f: StructNode, on_page, order_ix) -> tuple:
        if f.id in self._fig_anchor:
            return (self._fig_anchor[f.id], -f.bbox[3], f.bbox[0])
        return (self._anchor(f, order_ix) if f.id in order_ix else self._nearest_above(f.bbox, on_page, order_ix),
                -f.bbox[3], f.bbox[0])

    def _pictures(self, page, p: int, keep: list, lines: list[Line]):
        """Which printed lines belong to a picture and which are text, and the
        figures cut back to their pictures.

        The panels of these books are images too (a white box with its
        shadow), so text on an image is not always a picture's: an image that
        carries several lines of text is a background; a short word set on a
        small label of its own is text; a letter on a card is the card's. A
        figure the analysis drew round a whole box keeps only the pictures in
        it."""
        m = self.model
        Mi = ~page.transformation_matrix
        imgs = []
        for inf in page.get_image_info():
            r = _to_pdf(Mi, inf["bbox"])
            if r[2] - r[0] >= 4 and r[3] - r[1] >= 4:
                imgs.append(r)
        hl = [l for l in lines if l.horiz]
        on = {k: [l for l in hl if _inside((l.cx, l.cy), r, 0)] for k, r in enumerate(imgs)}

        def is_bg(k):
            ls = on[k]
            plain = [l for l in ls if not any(g in l.font for g in GRID_FONTS)]
            # (a picture of words — a word web — carries only the school face)
            return (len(plain) >= 1 and len(ls) >= 3) or (len(plain) >= 2 and sum(len(l.text) for l in plain) >= 15)
        bg = {k for k in on if is_bg(k)}
        # layers of one background (a panel and its soft shadows) are background too
        grew = True
        while grew:
            grew = False
            for k in range(len(imgs)):
                if k in bg:
                    continue
                for b in list(bg):
                    a1, a2 = _area(imgs[k]), _area(imgs[b])
                    if (a2 and 0.5 <= a1 / a2 <= 2.0 and _inter(imgs[k], imgs[b]) >= 0.5 * a1) \
                            or _touching(imgs[k], imgs[b], 2.5):
                        bg.add(k)
                        grew = True
                        break
        # a frame round a picture (a panel image holding a smaller picture) is background
        def iou(a, b):
            i = _inter(a, b)
            return i / max(1.0, _area(a) + _area(b) - i)
        for k in range(len(imgs)):
            if k in bg:
                continue
            twin = any(j != k and iou(imgs[j], imgs[k]) > 0.85 for j in range(len(imgs)))
            if twin and any(j != k and j not in bg and _inter(imgs[j], imgs[k]) > 0.95 * _area(imgs[j])
                            and _area(imgs[k]) > 1.8 * _area(imgs[j]) and _area(imgs[j]) >= 600
                            for j in range(len(imgs))):
                bg.add(k)
                for j in range(len(imgs)):
                    if j != k and iou(imgs[j], imgs[k]) > 0.85:
                        bg.add(j)

        def sliver(r):
            w, h = r[2] - r[0], r[3] - r[1]
            return w < 8 or h < 8 or w > 6 * h or h > 6 * w
        fig_text: set = set()
        for l in hl:
            if re.match(r"(FiraSans|RockwellStd)", l.font) and l.size >= 7.5:
                continue                              # the book's own text faces are text
            ks = sorted([k for k in on if any(o is l for o in on[k])], key=lambda k: _area(imgs[k]))
            pics_under = [k for k in ks if k not in bg]
            k = ks[0] if ks else None
            if pics_under:
                small = pics_under[0]
                bigger = [k2 for k2 in pics_under[1:] if _area(imgs[k2]) > 4 * _area(imgs[small])]
                if any(g in l.font for g in GRID_FONTS) and not bigger \
                        and _area((l.x0, l.y0, l.x1, l.y1)) >= 0.25 * _area(imgs[small]):
                    continue                          # a word on a small label of its own (-at, -ot)
                fig_text.add(id(l))
                continue
            if k is not None:
                continue                              # on a background: text
            inF = [f for f in keep if f.role == "Figure" and f.bbox is not None and _inside((l.cx, l.cy), f.bbox, 1.0)]
            if not inF:
                continue
            f = inF[0]
            if any(k2 not in bg and not sliver(imgs[k2]) and _inter(imgs[k2], f.bbox) > 0.5 * _area(imgs[k2])
                   for k2 in range(len(imgs))):
                continue                              # a caption beside the pictures, in the figure's box
            if l.size < 11:
                fig_text.add(id(l))                   # the lettering of a drawing
        # a sign drawn as a polygon (the octagon of STOP AND ASSESS) with its words
        signs = []
        try:
            for dr in page.get_drawings():
                kinds = "".join(str(it[0]) for it in dr.get("items", []))
                r = _to_pdf(Mi, dr["rect"])
                if dr.get("fill") is None or "c" in kinds or kinds.count("l") < 6:
                    continue
                if not (30 < r[2] - r[0] < 400 and 30 < r[3] - r[1] < 400):
                    continue
                signs.append(r)
        except Exception:
            signs = []
        for r in _merge_boxes(signs, 0.0):
            inner = [l for l in hl if _inside((l.cx, l.cy), r, 0) and l.color == 0xffffff]
            if not inner or any(f.bbox is not None and _inter(f.bbox, r) > 0.5 * _area(r) for f in keep):
                continue
            for l in hl:
                if _inside((l.cx, l.cy), r, 0):
                    fig_text.add(id(l))
            n = StructNode(id=m.new_id("tgfig"), role="Figure", page=p, bbox=r, confidence=0.8,
                           note="Teacher's Guide: a sign with its words")
            m.nodes[n.id] = n
            keep = keep + [n]
            self.stats["signs"] += 1
        order_ix = None
        out = []
        for f in keep:
            if f.role != "Figure" or f.bbox is None:
                out.append(f)
                continue
            if any(k in bg and iou(imgs[k], f.bbox) > 0.85 for k in range(len(imgs))) \
                    and not any(id(l) in fig_text and _inside((l.cx, l.cy), f.bbox, 0.5) for l in hl) \
                    and not any(k not in bg and _inter(imgs[k], f.bbox) > 0.6 * _area(imgs[k]) for k in range(len(imgs))):
                self._unhook(f.id)                    # a slice of a panel, not a picture
                m.nodes.pop(f.id, None)
                self.stats["panel_slices"] += 1
                continue
            texts = [l for l in hl if id(l) not in fig_text and _inside((l.cx, l.cy), f.bbox, 0.5)]
            if not texts:
                out.append(f)
                continue
            if order_ix is None:
                ids = {n.id for n in m.nodes.values() if n.page == p}
                order_ix = {n.id: k for k, n in enumerate(m.ordered_nodes()) if n.id in ids}
            anchor = self._anchor(f, order_ix) if f.id in order_ix else 10 ** 6
            pics = [r for k, r in enumerate(imgs) if k not in bg and not sliver(r)
                    and _inter(r, f.bbox) > 0.6 * _area(r) and _area(r) < 0.9 * _area(f.bbox)
                    and not any(_inside((l.cx, l.cy), r, -0.5) for l in texts)]
            merged = _merge_boxes(pics)
            drawn = [l for l in hl if id(l) in fig_text and _inside((l.cx, l.cy), f.bbox, 0.5)]
            if not merged and drawn:
                out.append(f)                         # a drawing with its lettering; the text beside it is text
                continue
            if merged:
                f.bbox = merged[0]
                f.note = (f.note + " · cut back to its picture").strip(" ·")
                out.append(f)
                for r in merged[1:]:
                    n = StructNode(id=m.new_id("tgfig"), role="Figure", page=p, bbox=r, confidence=0.8,
                                   note="Teacher's Guide: a picture in a text box")
                    m.nodes[n.id] = n
                    self._fig_anchor[n.id] = anchor
                    out.append(n)
            else:
                self._unhook(f.id)
                for d in m.descendants(f.id):
                    if d.role in KEEP_ROLES and d.bbox is not None:
                        self._unhook(d.id)
                        self._fig_anchor[d.id] = anchor
                        out.append(d)
                m.nodes.pop(f.id, None)
            self.stats["text_boxes"] += 1
        # pictures the analysis left out (set on a panel it took for background)
        H = page.rect.height * page.rect.width
        try:
            log = [(t, _to_pdf(Mi, r)) for t, r in page.get_bboxlog()]
        except Exception:
            log = []

        def hidden(r) -> bool:
            """Painted over: a panel drawn after the image covers most of it."""
            for k, (t, q) in enumerate(log):
                if t == "fill-image" and _inter(q, r) > 0.9 * max(_area(r), 1.0) \
                        and _inter(q, r) > 0.9 * max(_area(q), 1.0):
                    return any(t2.startswith("fill-path") and _inter(q2, r) > 0.6 * _area(r)
                               for t2, q2 in log[k + 1:])
            return False
        cand = []
        for k, r in enumerate(imgs):
            if k in bg or sliver(r) or _area(r) < 600 or min(r[2] - r[0], r[3] - r[1]) < 20 or _area(r) > 0.4 * H:
                continue
            if hidden(r) or _blank(page, r):
                continue
            if any(f.bbox is not None and _inter(r, f.bbox) > 0.3 * _area(r) for f in out):
                continue
            if any(id(l) not in fig_text and _inside((l.cx, l.cy), r, -0.5) for l in hl):
                continue
            cand.append(r)
        for r in _merge_boxes(cand):
            n = StructNode(id=m.new_id("tgfig"), role="Figure", page=p, bbox=r, confidence=0.7,
                           note="Teacher's Guide: a picture")
            m.nodes[n.id] = n
            out.append(n)
            for a in [x for x in m.nodes.values() if x.artifact and x.page == p and x.bbox is not None
                      and _inter(x.bbox, r) > 0.7 * max(_area(x.bbox), 1.0) and not (x.text or "").strip()]:
                self._unhook(a.id)
                m.nodes.pop(a.id, None)
            self.stats["pictures_found"] += 1
        return out, fig_text

    def _merge_figures(self, figs: list) -> list:
        """Pictures stacked on each other (a pile of cards) are one Figure."""
        m = self.model
        figs = [f for f in figs if f.id in m.nodes]
        changed = True
        while changed:
            changed = False
            for i in range(len(figs)):
                for j in range(i + 1, len(figs)):
                    a, b = figs[i], figs[j]
                    if a.role != "Figure" or b.role != "Figure" or a.bbox is None or b.bbox is None:
                        continue
                    small = min(_area(a.bbox), _area(b.bbox))
                    if small <= 0 or _inter(a.bbox, b.bbox) < 0.5 * small:
                        continue
                    keep_, gone = (a, b) if (a.alt or _area(a.bbox) >= _area(b.bbox)) else (b, a)
                    keep_.bbox = (min(a.bbox[0], b.bbox[0]), min(a.bbox[1], b.bbox[1]),
                                  max(a.bbox[2], b.bbox[2]), max(a.bbox[3], b.bbox[3]))
                    if not keep_.alt and gone.alt:
                        keep_.alt = gone.alt
                    if gone.id in self._fig_anchor and keep_.id not in self._fig_anchor:
                        self._fig_anchor[keep_.id] = self._fig_anchor[gone.id]
                    self._unhook(gone.id)
                    for c in list(gone.children):
                        m.nodes[c].parent = None
                    m.nodes.pop(gone.id, None)
                    figs.remove(gone)
                    self.stats["figures_merged"] += 1
                    changed = True
                    break
                if changed:
                    break
        return figs

    # -------------------------------------------------------------- reading order
    def _icons_first(self, items: list) -> list:
        """An icon printed just left of a label or a head is read right before it."""
        def box(it):
            return it[2].bbox if it[1] in ("b", "f") else None
        out = list(items)
        for f in [it for it in items if it[1] == "f" and it[2].bbox is not None]:
            fb = f[2].bbox
            if (fb[2] - fb[0]) > 60 or (fb[3] - fb[1]) > 60:
                continue
            best = None
            for it in out:
                if it[1] != "b" or box(it) is None:
                    continue
                bb = box(it)
                gap = bb[0] - fb[2]
                ov = min(bb[3], fb[3]) - max(bb[1], fb[1])
                if -2 <= gap <= 8 and ov >= 0.4 * min(fb[3] - fb[1], bb[3] - bb[1]):
                    if best is None or abs(bb[3] - fb[3]) < abs(box(best)[3] - fb[3]):
                        best = it
            if best is None:
                continue
            out.remove(f)
            out.insert(out.index(best), f)
        return out


    def _xy_items(self, items: list) -> list:
        """The page read the way it is laid out: the header band, then the
        margin column and the main column, a box row's columns left to right,
        each top to bottom (an XY cut over the blocks and figures; what cannot
        be cut is read top to bottom, as the analysis had it)."""
        def box(it):
            kind, obj = it[1], it[2]
            if kind == "b":
                return obj.bbox
            if kind == "t":
                return self._box_of(obj) or (0, 0, 0, 0)
            return obj.bbox
        tail = [it for it in items if it[1] == "b" and it[2].kind == "pagenum"]
        body = [it for it in items if it not in tail]
        rank = {id(it): k for k, it in enumerate(body)}

        def cut(group: list) -> list:
            if len(group) <= 1:
                return group
            bx = [(box(it), it) for it in group]
            # horizontal cut: a band of white across the whole group
            ys = sorted(((b[1], b[3]) for b, _ in bx), key=lambda t: -t[1])
            cuts_y = []
            low = ys[0][0]
            for y0, y1 in ys[1:]:
                if y1 < low - 1.5:
                    cuts_y.append(((low + y1) / 2, low - y1))
                low = min(low, y0)
            xs = sorted(((b[0], b[2]) for b, _ in bx))
            cuts_x = []
            right = xs[0][1]
            for x0, x1 in xs[1:]:
                if x0 > right + 3:
                    cuts_x.append(((right + x0) / 2, x0 - right))
                right = max(right, x1)
            if cuts_y:
                y = cuts_y[0][0]
                top = [it for b, it in bx if (b[1] + b[3]) / 2 > y]
                rest = [it for b, it in bx if (b[1] + b[3]) / 2 <= y]
                if top and rest:
                    return cut(top) + cut(rest)
            if cuts_x:
                x = cuts_x[0][0]
                left = [it for b, it in bx if (b[0] + b[2]) / 2 < x]
                rest = [it for b, it in bx if (b[0] + b[2]) / 2 >= x]
                if left and rest:
                    return cut(left) + cut(rest)
            return sorted(group, key=lambda it: (-box(it)[3], rank[id(it)]))
        return cut(body) + tail

    # -------------------------------------------------------------- pictures
    def _badges(self, p: int, rot: list[Line], text_lines: list[Line], keep) -> list[StructNode]:
        """Letters set round a circle (a seal, a badge): one Figure over them
        and the text in their middle."""
        out = []
        pts = [l for l in rot if len(l.text.strip()) <= 3]
        groups: list[list[Line]] = []
        for l in pts:
            for g in groups:
                if any(abs(l.cx - o.cx) < 30 and abs(l.cy - o.cy) < 30 for o in g):
                    g.append(l)
                    break
            else:
                groups.append([l])
        tiny = [l for l in text_lines if len(l.text.replace(" ", "")) <= 3 and l.size < 6]
        for g in groups:
            if len(g) < 4:
                continue
            box = (min(l.x0 for l in g), min(l.y0 for l in g), max(l.x1 for l in g), max(l.y1 for l in g))
            grew = True
            while grew:
                grew = False
                for l in tiny:
                    if l in g:
                        continue
                    if box[0] - 6 <= l.cx <= box[2] + 6 and box[1] - 6 <= l.cy <= box[3] + 6:
                        g.append(l)
                        box = (min(box[0], l.x0), min(box[1], l.y0), max(box[2], l.x1), max(box[3], l.y1))
                        grew = True
            if any(_inter(box, k.bbox) > 0.5 * _area(box) for k in keep):
                continue
            n = StructNode(id=self.model.new_id("tgfig"), role="Figure", page=p, bbox=box, confidence=0.8,
                           note="Teacher's Guide: a badge with its lettering")
            self.model.nodes[n.id] = n
            out.append(n)
            self.stats["badges"] += 1
        return out

    def _side_icons(self, page, p: int, blocks, figs, lines) -> list:
        """The icon printed left of a short bold label in the margin column
        ("PD Expert Video: Assessment" has the PD seal beside it): a Figure,
        when an image is there and nothing else is."""
        out = []
        Mi = ~page.transformation_matrix
        imgs = [_to_pdf(Mi, i["bbox"]) for i in page.get_image_info()]
        for b in blocks:
            if b.kind != "body" or len(b.lines) > 2 or any(l.size > 10.5 or not l.bold for l in b.lines):
                continue
            bb = b.bbox
            sq = (bb[0] - 31, bb[1] - 1, bb[0] - 2, bb[3] + 1)
            left = _to_pdf(Mi, page.rect)[0]
            if sq[0] < left - 3 or sq[0] > left + 45 or _area(sq) <= 0:
                continue                              # only in the margin column
            if any(f.bbox is not None and _inter(f.bbox, sq) > 0.2 * _area(sq) for f in list(figs) + out):
                continue
            if any(_inside((l.cx, l.cy), sq, 0) for l in lines):
                continue
            if not any(_inter(r, sq) > 0.6 * _area(sq) and _area(r) < 12000 for r in imgs):
                continue
            n = StructNode(id=self.model.new_id("tgfig"), role="Figure", page=p, bbox=sq, confidence=0.7,
                           note="Teacher's Guide: the icon beside a label")
            self.model.nodes[n.id] = n
            out.append(n)
            self.stats["side_icons"] += 1
        return out

    def _logo(self, p: int, logo: list[Line], keep) -> None:
        for l in logo:
            box = (l.x0, l.y0, l.x1, l.y1)
            near = [k for k in keep if k.bbox is not None and abs(k.bbox[1] - l.y0) < 40
                    and (abs(k.bbox[2] - l.x0) < 25 or _inter(k.bbox, box) > 0)]
            if near:
                k = min(near, key=lambda k: abs(k.bbox[2] - l.x0))
                k.bbox = (min(k.bbox[0], l.x0), min(k.bbox[1], l.y0), max(k.bbox[2], l.x1), max(k.bbox[3], l.y1))
            else:
                n = StructNode(id=self.model.new_id("tgfig"), role="Figure", page=p, bbox=box, confidence=0.8,
                               note="Teacher's Guide: the programme logo")
                self.model.nodes[n.id] = n
                keep.append(n)
            self.stats["logo"] += 1

    # -------------------------------------------------------------- lines → blocks
    def _join_baseline(self, lines: list[Line]) -> list[Line]:
        """Pieces of one printed line (a head broken by an icon, "Lesson" and
        "1 (20–30 MIN.)" in two type faces) become one line."""
        lines = sorted(lines, key=lambda l: (-round(l.cy), l.x0))
        out: list[Line] = []
        used = [False] * len(lines)
        for i, a in enumerate(lines):
            if used[i]:
                continue
            cur = a
            for j in range(len(lines)):
                b = lines[j]
                if used[j] or j == i or b.region != cur.region:
                    continue
                if abs(b.cy - cur.cy) > 0.35 * min(b.h, cur.h) or b.x0 < cur.x1 - 1:
                    continue
                gap = b.x0 - cur.x1
                heady = cur.cls not in ("body", "grid", "grid_big") or b.cls not in ("body", "grid", "grid_big")
                lim = 2.2 * max(cur.size, b.size) if heady else 0.9 * min(cur.size, b.size)
                if cur.cls == "grid_big" and b.cls == "grid_big":
                    continue                       # letter tiles: see _tiles
                if cur.cls in ("grid", "grid_big") or b.cls in ("grid", "grid_big"):
                    continue
                if gap > lim:
                    continue
                if heady and cur.cls != b.cls and not LESSON_RE.match(cur.text + " " + b.text):
                    continue
                cur = self._merge(cur, b)
                used[j] = True
            used[i] = True
            out.append(cur)
        for l in out:
            if LESSON_RE.match(l.text):
                l.cls = "H2"
        return self._tiles(out)

    def _tiles(self, lines: list[Line]) -> list[Line]:
        """Letter tiles in a row ("r a n   r i p   f u n"): the letters of one
        word are one line; a wider gap starts the next word."""
        # word cards in a row ("automatic   difficult"): a word each
        split: list[Line] = []
        for l in lines:
            if l.cls == "grid_big" and " " in l.text.strip() and len(l.text.replace(" ", "")) > 2:
                groups = [g for g in self._split_gaps(l, 0.6 * l.size) if "".join(c[0] for c in g).strip()]
                if len(groups) > 1 and all(len("".join(c[0] for c in g).replace(" ", "")) > 1 for g in groups):
                    for g in groups:
                        while g and not g[-1][0].strip():
                            g.pop()
                        sub = _sub(l, g)
                        sub.cls, sub.region = "grid_big", l.region
                        split.append(sub)
                    continue
            split.append(l)
        lines = split
        tiles = [l for l in lines if l.cls == "grid_big" and len(l.text.replace(" ", "")) <= 2]
        rest = [l for l in lines if not (l.cls == "grid_big" and len(l.text.replace(" ", "")) <= 2)]
        rows: list[list[Line]] = []
        for t in sorted(tiles, key=lambda l: (-l.cy, l.x0)):
            for r in rows:
                if r[0].region == t.region and abs(r[0].cy - t.cy) < 0.4 * min(r[0].h, t.h):
                    r.append(t)
                    break
            else:
                rows.append([t])
        for r in rows:
            r.sort(key=lambda l: l.x0)
            gaps = [b.x0 - a.x1 for a, b in zip(r, r[1:])]
            small = sorted(gaps)[:max(1, len(gaps) // 2)] if gaps else []
            unit = statistics.median(small) if small else 0
            word = [r[0]]
            for a, b, g in zip(r, r[1:], gaps):
                if g > max(1.6 * unit, unit + 0.8 * b.size) and len(r) > 2:
                    rest.append(self._join_tiles(word))
                    word = [b]
                else:
                    word.append(b)
            rest.append(self._join_tiles(word))
        return rest

    def _join_tiles(self, word: list[Line]) -> Line:
        cur = word[0]
        for b in word[1:]:
            cur = self._merge(cur, b)
        cur.cls = "grid_big"
        return cur

    @staticmethod
    def _merge(a: Line, b: Line) -> Line:
        big = a if (a.cls != "body" or b.cls == "body") else b
        return Line(a.page, min(a.x0, b.x0), min(a.y0, b.y0), max(a.x1, b.x1), max(a.y1, b.y1),
                    (a.text + " " + b.text).strip(), a.chars + b.chars, big.font, max(a.size, b.size),
                    big.color, big.bold, True, big.cls, a.region, a.boxed or b.boxed)

    def _blocks(self, lines: list[Line]) -> list[Block]:
        out: list[Block] = []
        lines = sorted(lines, key=lambda l: (-l.y1, l.x0))
        open_: list[Block] = []
        for ln in lines:
            kind = ln.cls
            t = ln.text.strip()
            bullet = t[:1] in BULLETS and (len(t) > 1 and (t[1] == " " or not t[1].isalnum()))
            numbered = bool(NUM_ITEM.match(t)) and kind == "body"
            if kind in ("grid",):
                out.extend(self._grid_words(ln))
                continue
            target = None
            for b in reversed(open_):
                last = b.lines[-1]
                if b.region != ln.region:
                    continue
                if _overlap_x((last.x0, 0, last.x1, 0), (ln.x0, 0, ln.x1, 0)) <= 0 and abs(last.x0 - ln.x0) > 3:
                    continue
                gap = last.y0 - ln.y1
                step = last.y0 - ln.y0
                if gap < -0.3 * ln.h or gap > 2.5 * max(ln.size, last.size):
                    continue
                if bullet or numbered:
                    break
                if b.kind == "item":
                    # a wrapped item line starts under the item's words
                    if kind == "body" and ln.x0 >= b.lines[0].x0 + 2 and abs(ln.size - last.size) < 1 \
                            and step <= 1.35 * self._lead(ln.size):
                        target = b
                    break
                if kind != b.kind:
                    break
                if kind == "grid_big":
                    if step <= 2.0 * ln.size:
                        target = b
                    break
                if kind in HEADINGS or kind in ("HB", "HX"):
                    if step <= 1.6 * ln.size and abs(ln.size - last.size) < 1:
                        target = b
                    break
                # body: the same paragraph while the lines keep their step
                if abs(ln.size - last.size) < 1.2 and step <= 1.18 * self._lead(ln.size) + 0.6 \
                        and ln.x0 >= b.bbox[0] - 3:
                    target = b
                break
            if target is not None:
                target.lines.append(ln)
                continue
            nb = Block(lines=[ln], kind="item" if (bullet or numbered) and kind == "body" else kind,
                       region=ln.region)
            if nb.kind == "item":
                nb.lbl = self._label_box(ln, numbered)
            open_.append(nb)
            out.append(nb)
        for b in out:
            self._finish(b)
        return out

    @staticmethod
    def _label_box(ln: Line, numbered: bool):
        vis = [c for c in ln.chars if c[0].strip()]
        if not vis:
            return None
        if numbered:
            lab = []
            for c in vis:
                lab.append(c)
                if c[0] in ".)":
                    break
            return (min(c[1] for c in lab), ln.y0, max(c[2] for c in lab), ln.y1)
        return (vis[0][1], ln.y0, vis[0][2], ln.y1)

    @staticmethod
    def _split_gaps(ln: Line, limit: float) -> list[list]:
        groups: list[list] = [[]]
        prev = None
        for c in ln.chars:
            if prev is not None and c[0].strip():
                gap = c[1] - prev[2]
                if gap > limit and groups[-1]:
                    groups.append([])
            if c[0].strip() or groups[-1]:
                groups[-1].append(c)
            if c[0].strip():
                prev = c
        return groups

    def _grid_words(self, ln: Line) -> list[Block]:
        """A line of a blending grid: each word is a P of its own (row by
        row); a sentence ("My cat can run.") stays one P."""
        t = ln.text.strip()
        sentence = bool(re.search(r"[.!?”\"]$", t)) or len(t.split()) >= 4 \
            or not all(re.fullmatch(r"[A-Za-z’'\-]+", w) for w in t.split())   # /r/ /un/: sounds, one P
        limit = 0.9 * ln.size if sentence else 0.18 * ln.size
        vis = [c for c in ln.chars]
        groups: list[list] = [[]]
        prev = None
        for c in vis:
            if prev is not None and c[0].strip() and prev[0].strip() is not None:
                gap = c[1] - prev[2]
                if gap > limit and groups[-1]:
                    groups.append([])
            if c[0].strip() or groups[-1]:
                groups[-1].append(c)
            if c[0].strip():
                prev = c
        out = []
        for g in groups:
            g = [c for c in g]
            while g and not g[-1][0].strip():
                g.pop()
            if not g:
                continue
            sub = _sub(ln, g)
            sub.cls = "grid"
            sub.region = ln.region
            b = Block(lines=[sub], kind="grid", region=ln.region)
            self._finish(b)
            out.append(b)
        return out

    def _pagenum(self, ln: Line) -> Block:
        b = Block(lines=[ln], kind="pagenum")
        n = ln.text.split()[0]
        b.role, b.text, b.at = "P", n, f"Page {n}"
        return b

    def _finish(self, b: Block) -> None:
        txt = " ".join(" ".join(l.text for l in b.lines).split())
        b.text = txt
        if b.kind in ("H3", "H4", "HB", "HX") and sentence_like(txt):
            b.kind = "body"
        k = b.kind
        if k in ("H1", "H2", "H3", "H4"):
            b.role = k
        elif k in ("HB", "HX"):
            b.role = k                     # settled by what comes before (see _levels)
        elif k == "item":
            b.role = "LI"
        else:
            b.role = "P"
        b.at = actual_text(txt, k)

    def _badge_unit(self, blocks: list[Block]) -> None:
        """The unit badge ("UNIT 15 Rr", set in two sizes) is one P."""
        big = [b for b in blocks if b.kind in ("H1", "H2", "H3", "H4", "body", "HB", "HX")
               and any("AvantGarde" in l.font or "Avenir" in l.font for l in b.lines)]
        if not big:
            return
        grp = sorted(big, key=lambda b: (-b.bbox[3], b.bbox[0]))
        joined = " ".join(b.text for b in grp)
        mm = re.search(r"U\s*N\s*I\s*T\s*(\d+)\s*([A-Z])\s*([a-z])", joined.replace("UN IT", "UNIT"))
        if not mm:
            mm = re.search(r"(\d+)\s*U\s*N\s*I\s*T\s*([A-Z])\s*([a-z])", joined)
        if not mm:
            return
        head = [b for b in grp if re.search(r"UN|IT|\d|^[A-Z][a-z]$", b.text)]
        cluster = [b for b in head if abs(b.bbox[3] - grp[0].bbox[3]) < 110]
        if len(cluster) < 1:
            return
        first = cluster[0]
        for b in cluster[1:]:
            first.lines.extend(b.lines)
            blocks.remove(b)
        first.kind = "badge"
        first.role = "P"
        first.text = f"UNIT {mm.group(1)} {mm.group(2)}{mm.group(3)}"
        first.at = f"UNIT {mm.group(1)} uppercase {mm.group(2)}, lowercase {mm.group(3)}"
        self.stats["unit_badge"] += 1

    # -------------------------------------------------------------- the tree
    def _emit(self, p: int, items: list, links: list) -> int:
        m = self.model
        made = 0
        cur_list: StructNode | None = None
        last_kind = None
        for key, kind, obj in items:
            if kind == "b" and obj.role == "LI":
                if cur_list is None or last_kind != "li" or cur_list.page != p:
                    cur_list = StructNode(id=m.new_id("tgL"), role="L", page=p, confidence=0.9,
                                          note="Teacher's Guide list")
                    m.add(cur_list)
                    made += 1
                self._item(cur_list, obj)
                made += 3
                last_kind = "li"
                continue
            last_kind = kind
            cur_list = None
            if kind == "b":
                n = StructNode(id=m.new_id("tg"), role=obj.role, page=p, bbox=obj.bbox, text=obj.text,
                               actual_text=obj.at, confidence=0.9, note="Teacher's Guide",
                               style={"size": round(max(l.size for l in obj.lines), 1),
                                      "bold": all(l.bold for l in obj.lines),
                                      "font": obj.lines[0].font,
                                      "in_box": bool(obj.region) or any(l.boxed for l in obj.lines)})
                m.add(n)
                made += 1
                for l in links:
                    if l.parent is None and l.bbox is not None and _inside(((l.bbox[0] + l.bbox[2]) / 2,
                                                                            (l.bbox[1] + l.bbox[3]) / 2), n.bbox, 2):
                        l.parent = n.id
                        n.children.append(l.id)
            else:
                m.add(obj)
        # links not placed in a block stay where the page ends
        for l in links:
            if l.parent is None and l.id in m.nodes:
                m.add(l)
        return made

    def _item(self, lst: StructNode, b: Block) -> None:
        m = self.model
        li = StructNode(id=m.new_id("tgLI"), role="LI", page=b.page, confidence=0.9)
        m.add(li, lst.id)
        bb = b.bbox
        lb = b.lbl or (bb[0], b.lines[0].y0, bb[0] + 4, b.lines[0].y1)
        lbl_txt = b.text.split()[0] if b.text else "•"
        m.add(StructNode(id=m.new_id("tgLbl"), role="Lbl", page=b.page, bbox=lb, text=lbl_txt, confidence=0.9), li.id)
        body_box = (lb[2] + 0.5, bb[1], bb[2], bb[3])
        body_txt = b.text[len(lbl_txt):].strip()
        m.add(StructNode(id=m.new_id("tgLB"), role="LBody", page=b.page, bbox=body_box, text=body_txt,
                         actual_text=actual_text(body_txt, "body"), confidence=0.9), li.id)
        if lbl_txt[:1].isdigit():
            lst.list_numbering = "Decimal"
        elif lbl_txt[:1] in BULLETS:
            lst.list_numbering = "Disc"
        lst.bbox = bb if lst.bbox is None else (min(lst.bbox[0], bb[0]), min(lst.bbox[1], bb[1]),
                                               max(lst.bbox[2], bb[2]), max(lst.bbox[3], bb[3]))
        li.bbox = bb


# ------------------------------------------------------------ heading levels over the book
def settle_levels(model: DocModel) -> int:
    """A bold box head (HB) is the H4 of a part (H3) that has no sub-head yet,
    else the H5 under the sub-head; a boxed title (HX) is H2 when it opens its
    page, H3 inside a lesson part."""
    changed = 0
    last = {"H2": False, "H3": False, "H4": False}
    page_has_heading: set = set()
    for n in model.ordered_nodes():
        if n.artifact:
            continue
        r = n.role
        if r == "H2":
            last = {"H2": True, "H3": False, "H4": False}
        elif r == "H3":
            last["H3"], last["H4"] = True, False
        elif r == "H4":
            last["H4"] = True
        elif r == "HB":
            if not (n.style or {}).get("in_box"):
                n.role = "H4"                  # a sub-head of its own, not a box column's head
            else:
                n.role = "H4" if (last["H3"] and not last["H4"]) else ("H5" if last["H4"] else "H4")
            if n.role == "H4":
                last["H4"] = True
            changed += 1
        elif r == "HX":
            n.role = "H3" if n.page in page_has_heading else "H2"
            changed += 1
        if n.role in HEADINGS:
            page_has_heading.add(n.page)
    return changed


# ------------------------------------------------------------ what a tag is read as
def _letters_at(text: str) -> str:
    """ "R R R r r r" → "Three Uppercase R, Three Lowercase r"; "B H P b h p"
    → "uppercase B, uppercase H, uppercase P, lowercase b, lowercase h,
    lowercase p" (the client's wording)."""
    toks = text.split()
    if not toks or not all(len(t) == 1 and t.isalpha() for t in toks) or len(toks) < 2:
        return ""
    if len(set(t.lower() for t in toks)) == 1:
        up = sum(1 for t in toks if t.isupper())
        lo = len(toks) - up
        parts = []
        L = toks[0].upper()
        if up:
            parts.append(f"{NUMBER_WORDS.get(up, str(up))} Uppercase {L}" if up > 1 else f"Uppercase {L}")
        if lo:
            parts.append(f"{NUMBER_WORDS.get(lo, str(lo))} Lowercase {L.lower()}" if lo > 1 else f"Lowercase {L.lower()}")
        return ", ".join(parts)
    if any(t.isupper() for t in toks) and any(t.islower() for t in toks):
        return ", ".join(("uppercase " if t.isupper() else "lowercase ") + t for t in toks)
    return ""


def actual_text(text: str, kind: str = "body") -> str:
    t = text.strip()
    if not t:
        return ""
    if kind == "grid_big" or (len(t) <= 24 and re.fullmatch(r"([A-Za-z] )+[A-Za-z]", t)):
        s = _letters_at(t)
        if s:
            return s
    out = t
    if BLANK_RE.search(out):
        out = BLANK_RE.sub("Blank line", out)
    out = PAIR_RE.sub(lambda mm: f"uppercase {mm.group(1)}, lowercase {mm.group(2)}"
                      if mm.group(1).lower() == mm.group(2) else mm.group(0), out)
    mm = re.match(r"^ISBN:?\s*([\d\-]+)\s*(\(([A-Z])\))?", out)
    if mm:
        digits = " ".join(re.sub(r"\D", "", mm.group(1)))
        out = f"ISBN: {digits}" + (f" {mm.group(3)}" if mm.group(3) else "")
    mm = re.fullmatch(r"([A-Z])(\d{4,6})\s*(\(([A-Z])\))?", out)
    if mm:
        out = " ".join(mm.group(1) + mm.group(2)) + (f" ({mm.group(4)})" if mm.group(4) else "")
    return "" if out == t else out


# ------------------------------------------------------------ entry
def apply(model: DocModel, path: str, pages=None, order_mode: str = "xy") -> dict:
    """Make the Teacher's Guide structure on the pages (all, or `pages`)."""
    tg = TeachersGuide(model, path, pages, order_mode)
    stats = tg.run()
    stats["levels"] = settle_levels(model)
    model.info["teachers_guide"] = stats
    try:
        model.check_integrity()
    except Exception:
        pass
    return stats
