"""Shared helpers for the editors that work with a zone drawn on the page
(Edit table, Edit list, templates — V4.19).

Display space: the page as the editor shows it (rotated, top-down, points).
PDF space: the model's boxes (bottom-up user space)."""
from __future__ import annotations

import re

import pymupdf

from .model import CONTAINER_ROLES, DocModel, StructNode


def disp_matrix(page: pymupdf.Page) -> pymupdf.Matrix:
    return page.transformation_matrix * page.rotation_matrix


def to_disp(page: pymupdf.Page, box) -> list[float]:
    r = pymupdf.Rect(box) * disp_matrix(page)
    r.normalize()
    return [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)]


def to_pdf(page: pymupdf.Page, box) -> tuple:
    r = pymupdf.Rect([float(v) for v in list(box)[:4]]) * ~disp_matrix(page)
    r.normalize()
    return (round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2))


class DLine:
    """A printed line in display space, with its characters."""
    __slots__ = ("text", "box", "chars", "size")

    def __init__(self, text, box, chars, size):
        self.text, self.box, self.chars, self.size = text, box, chars, size

    @property
    def cy(self) -> float:
        return (self.box[1] + self.box[3]) / 2

    @property
    def cx(self) -> float:
        return (self.box[0] + self.box[2]) / 2


def lines_in(page: pymupdf.Page, zone_disp, centre: bool = True) -> list[DLine]:
    """The printed lines whose middle is inside the zone (display space), top
    to bottom, left to right; characters outside the zone are left off."""
    from .analyzer import collect_lines
    rot = page.rotation_matrix
    z = pymupdf.Rect(zone_disp)
    out = []
    for l in collect_lines(page):
        if not (l.text or "").strip():
            continue
        lb = pymupdf.Rect(l.bbox) * rot
        lb.normalize()
        if centre and not z.contains(pymupdf.Point((lb.x0 + lb.x1) / 2, (lb.y0 + lb.y1) / 2)):
            # a line running past the zone: its characters inside it
            if not (lb.y0 < z.y1 and lb.y1 > z.y0 and lb.x0 < z.x1 and lb.x1 > z.x0):
                continue
        chars = []
        for ch, cb in l.chars:
            r = pymupdf.Rect(cb) * rot
            r.normalize()
            if z.contains(pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)):
                chars.append((ch, (r.x0, r.y0, r.x1, r.y1)))
        vis = [c for c in chars if c[0].strip()]
        if not vis:
            continue
        text = re.sub(r"\s+", " ", "".join(c[0] for c in chars)).strip()
        box = (min(c[1][0] for c in vis), min(c[1][1] for c in vis),
               max(c[1][2] for c in vis), max(c[1][3] for c in vis))
        out.append(DLine(text, box, chars, getattr(l, "size", 0.0)))
    out.sort(key=lambda l: (round(l.box[1], 0), l.box[0]))
    return out


def bands(lines: list[DLine], overlap: float = 0.45) -> list[list[DLine]]:
    """Lines side by side on one level (a printed row across columns)."""
    out: list[list[DLine]] = []
    for l in sorted(lines, key=lambda l: l.cy):
        for b in out:
            top = min(x.box[1] for x in b)
            bot = max(x.box[3] for x in b)
            h = min(bot - top, l.box[3] - l.box[1])
            if min(bot, l.box[3]) - max(top, l.box[1]) > overlap * max(h, 1):
                b.append(l)
                break
        else:
            out.append([l])
    for b in out:
        b.sort(key=lambda l: l.box[0])
    out.sort(key=lambda b: min(x.box[1] for x in b))
    return out


def ubox(boxes) -> tuple | None:
    boxes = [b for b in boxes if b is not None]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def h_rules(page: pymupdf.Page, zone_disp, min_frac: float = 0.5) -> list[float]:
    """The y of horizontal rules drawn across most of the zone (display)."""
    z = pymupdf.Rect(zone_disp)
    ys = []
    try:
        for d in page.get_drawings():
            for it in d.get("items", []):
                if it[0] == "l":
                    p1, p2 = it[1], it[2]
                    r = pymupdf.Rect(p1, p2)
                elif it[0] == "re":
                    r = pymupdf.Rect(it[1])
                    if r.height > 2.5:
                        # a box: its top and bottom edges are rules
                        for y in (r.y0, r.y1):
                            rr = pymupdf.Rect(r.x0, y, r.x1, y) * page.rotation_matrix
                            rr.normalize()
                            if rr.width >= min_frac * z.width and z.y0 - 2 <= rr.y0 <= z.y1 + 2 \
                                    and rr.x0 < z.x1 and rr.x1 > z.x0:
                                ys.append(rr.y0)
                        continue
                else:
                    continue
                r = r * page.rotation_matrix
                r.normalize()
                if r.height <= 2.5 and r.width >= min_frac * z.width and z.y0 - 2 <= r.y0 <= z.y1 + 2 \
                        and r.x0 < z.x1 and r.x1 > z.x0:
                    ys.append((r.y0 + r.y1) / 2)
    except Exception:
        return []
    ys.sort()
    out: list[float] = []
    for y in ys:
        if not out or y - out[-1] > 2.5:
            out.append(y)
    return out


def v_rules(page: pymupdf.Page, zone_disp, min_frac: float = 0.4) -> list[float]:
    """The x of vertical rules drawn down most of the zone (display)."""
    z = pymupdf.Rect(zone_disp)
    xs = []
    try:
        for d in page.get_drawings():
            for it in d.get("items", []):
                rects = []
                if it[0] == "l":
                    rects.append(pymupdf.Rect(it[1], it[2]))
                elif it[0] == "re":
                    r = pymupdf.Rect(it[1])
                    if r.width > 2.5:
                        rects += [pymupdf.Rect(r.x0, r.y0, r.x0, r.y1), pymupdf.Rect(r.x1, r.y0, r.x1, r.y1)]
                    else:
                        rects.append(r)
                for r in rects:
                    r = r * page.rotation_matrix
                    r.normalize()
                    if r.width <= 2.5 and r.height >= min_frac * z.height and z.x0 - 2 <= r.x0 <= z.x1 + 2 \
                            and r.y0 < z.y1 and r.y1 > z.y0:
                        xs.append((r.x0 + r.x1) / 2)
    except Exception:
        return []
    xs.sort()
    out: list[float] = []
    for x in xs:
        if not out or x - out[-1] > 2.5:
            out.append(x)
    return out


def gaps(lines: list[DLine], x0: float, x1: float) -> list[tuple[float, float]]:
    """Vertical white stripes between x0 and x1 where no character is printed."""
    cover = []
    for l in lines:
        for ch, b in l.chars:
            if ch.strip():
                cover.append((b[0], b[2]))
    cover.sort()
    out = []
    cur = x0
    for a, b in cover:
        if a > cur + 1.5:
            out.append((cur, min(a, x1)))
        cur = max(cur, b)
        if cur >= x1:
            break
    if cur < x1 - 1.5:
        out.append((cur, x1))
    return [(a, b) for a, b in out if b > a and a > x0 + 0.5 and b < x1 - 0.5]


def remove_tags_inside(model: DocModel, page_no: int, zone_pdf, keep: set) -> int:
    """Tags printed wholly inside the zone (their middle in it) give way to
    what the zone becomes — like drawing a tag with "replace tags inside"."""
    zx0, zy0, zx1, zy1 = zone_pdf
    gone = 0
    for n in list(model.nodes.values()):
        if n.id in keep or n.id not in model.nodes or n.role in CONTAINER_ROLES or n.artifact:
            continue
        rects = n.rects_on(page_no)
        if not rects or len(rects) != len(n.rects_on(page_no)) or n.pages() - {page_no}:
            continue
        if all(zx0 - 1 <= (r[0] + r[2]) / 2 <= zx1 + 1 and zy0 - 1 <= (r[1] + r[3]) / 2 <= zy1 + 1
               for r in rects):
            model.remove(n.id, promote_children=True)
            gone += 1
    # containers left empty by that
    for n in list(model.nodes.values()):
        if n.id in model.nodes and n.id not in keep and n.role in ("L", "LI", "LBody", "Div", "Table", "TR") \
                and not n.children and n.bbox is None and not n.parts and not n.artifact:
            model.remove(n.id)
    return gone


def place_after(model: DocModel, anchor: StructNode, nodes: list[StructNode]) -> None:
    """New tags right after `anchor`, in order."""
    sib = model.siblings(anchor.id)
    at = sib.index(anchor.id) + 1
    for k, n in enumerate(nodes):
        model.add(n, anchor.parent, at + k)


def zone_of(model: DocModel, n: StructNode) -> None:
    """A container's zone again from what is inside it: its box on its first
    page, the other pages as parts."""
    per: dict[int, list] = {}
    for d in model.descendants(n.id):
        if d.artifact:
            continue
        for p in d.pages():
            for r in d.rects_on(p):
                per.setdefault(p, []).append(r)
    if not per:
        return
    first = min(per)
    n.page = first
    n.bbox = ubox(per[first])
    n.parts = [[p, *ubox(rs)] for p, rs in sorted(per.items()) if p != first]
