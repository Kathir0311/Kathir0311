"""Page surgery that the structure has to follow: splitting a two-page spread.

A book exported as spreads prints pages 12 and 13 side by side on one sheet.
Read as one page, a screen reader goes across both halves line by line, and a
contents entry for page 13 can only land on "the sheet with 12 and 13 on it".
Splitting the sheet gives the left page and then the right page, each a page of
its own, in that order.

Nothing printed is changed or redrawn: the sheet is copied, and each copy shows
only its own half (its MediaBox and CropBox are that half). The other half is
still in the copy's content stream but outside the page, where it is not shown
and is marked as an artifact when the file is built. Tags follow their half —
a tag printed on the right half moves to the new page, keeping its position on
the paper — and every page after the spread moves up by one.
"""
from __future__ import annotations

import statistics

import pikepdf
import pymupdf
from pikepdf import Array, Dictionary, Name

from .model import DocModel


def spreads(pdf_path: str) -> list[int]:
    """Pages that look like two pages printed side by side.

    Wider than tall, and about twice as wide as the ordinary pages of the file —
    a landscape page in a portrait book is not a spread unless it is double."""
    doc = pymupdf.open(pdf_path)
    try:
        sizes = [(p.rect.width, p.rect.height) for p in doc]
    finally:
        doc.close()
    if not sizes:
        return []
    portrait = [w for w, h in sizes if w <= h * 1.05]
    single = statistics.median(portrait) if portrait else None
    out = []
    for i, (w, h) in enumerate(sizes):
        if w < h * 1.25:
            continue
        if single is None or abs(w - 2 * single) <= 0.08 * w:
            out.append(i)
    return out


def _halves(pdf_path: str, pno: int, where: float) -> tuple[tuple, tuple, tuple, tuple]:
    """The left and right halves of the page as you see it, in PDF user space,
    and the size each half is shown at."""
    doc = pymupdf.open(pdf_path)
    try:
        page = doc[pno]
        mat = page.transformation_matrix * page.rotation_matrix
        inv = ~mat
        w, h = page.rect.width, page.rect.height
        cut = w * where
        out = []
        for disp in (pymupdf.Rect(0, 0, cut, h), pymupdf.Rect(cut, 0, w, h)):
            r = disp * inv
            r.normalize()
            out.append((round(r.x0, 3), round(r.y0, 3), round(r.x1, 3), round(r.y1, 3)))
        return out[0], out[1], (cut, h), (w - cut, h)
    finally:
        doc.close()


def _inherited(page_obj, key: str):
    """A page attribute, looked up the page tree the way a reader does."""
    node, hops = page_obj, 0
    while node is not None and hops < 32:
        if key in node:
            return node[key]
        node = node.get("/Parent")
        hops += 1
    return None


def _inside(box, rect, frac: float = 0.5) -> float:
    """How much of box lies inside rect (0 … 1)."""
    if not box:
        return 0.0
    a = max(1e-6, (box[2] - box[0]) * (box[3] - box[1]))
    ix = max(0.0, min(box[2], rect[2]) - max(box[0], rect[0]))
    iy = max(0.0, min(box[3], rect[3]) - max(box[1], rect[1]))
    return ix * iy / a


def split_pdf(pdf_path: str, pno: int, where: float = 0.5) -> dict:
    """Split page `pno` of the file in place: the left half stays page `pno`,
    the right half becomes page `pno + 1`."""
    if not 0.2 <= where <= 0.8:
        raise ValueError("the split must fall in the middle part of the page")
    left, right, lsize, rsize = _halves(pdf_path, pno, where)
    pdf = pikepdf.open(pdf_path, allow_overwriting_input=True)
    try:
        if not 0 <= pno < len(pdf.pages):
            raise ValueError("no such page")
        src = pdf.pages[pno].obj
        # the copy shares the resources (fonts, images) but has a content stream
        # of its own, because building the file writes marked content into it
        data = b""
        contents = src.get("/Contents")
        if isinstance(contents, Array):
            data = b"\n".join(c.read_bytes() for c in contents)
        elif contents is not None:
            data = contents.read_bytes()
        new = Dictionary()
        for k, v in src.items():
            if k in ("/Contents", "/Annots", "/StructParents", "/Parent", "/Thumb", "/B"):
                continue
            new[k] = v
        # inherited from the page tree: make it the page's own before it changes
        for key in ("/Resources", "/Rotate"):
            v = _inherited(src, key)
            if v is not None:
                new[key] = v
                if key not in src:
                    src[key] = v
        new.Type = Name.Page
        new.Contents = pdf.make_stream(data)
        pdf.pages.insert(pno + 1, pikepdf.Page(pdf.make_indirect(new)))
        lp, rp = pdf.pages[pno].obj, pdf.pages[pno + 1].obj
        for pg, box in ((lp, left), (rp, right)):
            arr = Array([float(v) for v in box])
            pg.MediaBox = arr
            pg.CropBox = Array([float(v) for v in box])
            for extra in ("/TrimBox", "/BleedBox", "/ArtBox"):
                if extra in pg:
                    del pg[extra]
        # every annotation goes with the half it sits on — a link on the right
        # page is the same annotation object, now on the new page
        annots = src.get("/Annots")
        moved = 0
        if isinstance(annots, Array):
            keep_l, keep_r = Array(), Array()
            for a in list(annots):
                try:
                    r = [float(v) for v in a.Rect]
                    rr = (min(r[0], r[2]), min(r[1], r[3]), max(r[0], r[2]), max(r[1], r[3]))
                except Exception:
                    keep_l.append(a)
                    continue
                if _inside(rr, right) > _inside(rr, left):
                    a.P = rp
                    keep_r.append(a)
                    moved += 1
                else:
                    keep_l.append(a)
            lp.Annots = keep_l
            if len(keep_r):
                rp.Annots = keep_r
        before = _annot_keys(pdf)
        pdf.save(pdf_path)
    finally:
        pdf.close()
    # saving renumbers the objects: the tags that point at an annotation by
    # its object number follow it through `annot_map` (old -> new)
    after = pikepdf.open(pdf_path)
    try:
        now = {k: v for v, k in _annot_keys(after).items()}
    finally:
        after.close()
    annot_map = {old: now[k] for old, k in before.items() if k in now and now[k] != old}
    return {"page": pno, "left": left, "right": right, "left_size": lsize,
            "right_size": rsize, "annots_moved": moved, "annot_map": annot_map}


def _annot_keys(pdf) -> dict:
    """{object number: (page, rounded rect, subtype, n-th on the page)}."""
    out = {}
    for i, pg in enumerate(pdf.pages):
        for k, a in enumerate(pg.obj.get("/Annots") or []):
            try:
                if not a.is_indirect:
                    continue
                r = tuple(round(float(v), 1) for v in a.Rect)
                out[a.objgen[0]] = (i, r, str(a.get("/Subtype", "")), k)
            except Exception:
                continue
    return out


def split_model(model: DocModel, pno: int, left, right, left_size, right_size,
                annot_map: dict | None = None) -> int:
    """Move the tags to follow the split: the right half's tags go to the new
    page, every later page moves up by one, and the left page is read before
    the right page. `annot_map` (from split_pdf) renumbers the annotations the
    tags point at."""
    moved = 0
    if annot_map:
        amap = {int(k): int(v) for k, v in annot_map.items()}
        for n in model.nodes.values():
            if n.annots:
                n.annots = [amap.get(int(a), a) for a in n.annots]

    def shift_keys(keys: list[str], to_right: bool) -> list[str]:
        out = []
        for k in keys:
            if k.startswith("s") or ":" not in k:
                out.append(k)              # inside a form: not tied to a page
                continue
            p, _, mc = k.partition(":")
            try:
                pi = int(p)
            except ValueError:
                out.append(k)
                continue
            if pi > pno or (pi == pno and to_right):
                pi += 1
            out.append(f"{pi}:{mc}")
        return out

    for n in model.nodes.values():
        to_right = False
        if n.page > pno:
            n.page += 1
        elif n.page == pno and n.bbox:
            to_right = _inside(n.bbox, right) > _inside(n.bbox, left)
            if to_right:
                n.page = pno + 1
                moved += 1
        parts = []
        for p in n.parts:
            pp = int(p[0])
            if pp > pno:
                pp += 1
            elif pp == pno and _inside(p[1:5], right) > _inside(p[1:5], left):
                pp = pno + 1
            parts.append([pp, *p[1:5]])
        n.parts = parts
        if n.src_keys:
            n.src_keys = shift_keys(n.src_keys, to_right)
        if n.mcids:
            n.mcids = {}
        xr = (n.style or {}).get("xref")
        if isinstance(xr, dict) and isinstance(xr.get("page"), int) and xr["page"] > pno:
            xr["page"] += 1

    # the left page is read first, then the right page — whatever order the
    # spread was read in before
    roots = model.root_children
    slots = [i for i, r in enumerate(roots)
             if r in model.nodes and model.nodes[r].page in (pno, pno + 1)]
    ordered = sorted((roots[i] for i in slots),
                     key=lambda r: 0 if model.nodes[r].page == pno else 1)
    for i, r in zip(slots, ordered):
        roots[i] = r

    sizes = list(model.page_sizes)
    if 0 <= pno < len(sizes):
        sizes[pno:pno + 1] = [tuple(left_size), tuple(right_size)]
        model.page_sizes = sizes
    model.page_count = max(model.page_count + 1, len(model.page_sizes))
    for key in ("xrefs", "xref"):
        model.info.pop(key, None)
    model.info.setdefault("pages_split", []).append(pno)
    return moved
