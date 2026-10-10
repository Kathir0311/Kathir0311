"""Edit list (V4.19): a list's items changed by hand, and list templates.

The editor's "Edit list" dialog shows the list beside a picture of it: each
item with its label (Lbl) and words (LBody), set in by its level. One step at
a time:

* add items from a zone drawn on the page (a new item at each line that
  starts with a label, one item per line, or the zone as one item); take the
  items in a zone out of the list (they become paragraphs, or artifacts)
* merge an item with the next, split an item at a line, move the line
  between two items
* nest an item under the one above (indent) and take it out again (outdent),
  move items up and down, change a label, the numbering of a list
* split a list in two, join it with the next list, "not a list"
* save a list as template #N and give another list (or a zone) the same
  format: the same labels, levels and numbering

An item's words are read off the page from its lines: the label's
characters make the Lbl, the rest the LBody.
"""
from __future__ import annotations

import re

import pymupdf

from .model import DocModel, StructNode, flow_position
from .zonetools import (DLine, bands, lines_in, place_after, remove_tags_inside, to_disp,
                        to_pdf, ubox, zone_of)


class ListEditError(Exception):
    pass


NUMBERINGS = ["None", "Disc", "Circle", "Square", "Decimal", "UpperRoman", "LowerRoman",
              "UpperAlpha", "LowerAlpha"]


def _label_re():
    from .analyzer import LABEL_RE
    return LABEL_RE


def _numbering(lbl: str) -> str:
    from .analyzer import Analyzer
    return Analyzer._numbering(lbl.strip()) if lbl.strip() else "None"


# ------------------------------------------------------------- reading a list
def _items(model: DocModel, L: StructNode) -> list[StructNode]:
    return [model.nodes[c] for c in L.children if c in model.nodes and model.nodes[c].role == "LI"]


def _part(model: DocModel, li: StructNode, role: str) -> StructNode | None:
    return next((model.nodes[c] for c in li.children if c in model.nodes and model.nodes[c].role == role), None)


def _sub(model: DocModel, li: StructNode) -> StructNode | None:
    """The list nested in an item (inside its LBody, or straight in the LI)."""
    body = _part(model, li, "LBody")
    for host in ([body] if body is not None else []) + [li]:
        for c in host.children:
            n = model.nodes.get(c)
            if n is not None and n.role == "L":
                return n
    return None


def _top(model: DocModel, n: StructNode) -> StructNode:
    """The outermost list a list or item belongs to."""
    top = n if n.role == "L" else None
    for a in model.ancestors(n.id):
        if a.role == "L":
            top = a
    if top is None:
        raise ListEditError("not a list")
    return top


def flatten(model: DocModel, L: StructNode, level: int = 0) -> list[tuple]:
    """(item, level, its list) in reading order, nested items after their host."""
    out = []
    for li in _items(model, L):
        out.append((li, level, L))
        sub = _sub(model, li)
        if sub is not None:
            out += flatten(model, sub, level + 1)
    return out


def _words(model: DocModel, n: StructNode | None) -> str:
    if n is None:
        return ""
    if (n.text or "").strip():
        return n.text.strip()
    parts = []
    for c in n.children:
        k = model.nodes.get(c)
        if k is not None and k.role != "L" and not k.artifact:
            parts.append(_words(model, k))
    return " ".join(p for p in parts if p)


def _rects(n: StructNode) -> list:
    out = []
    if n.bbox is not None and n.page is not None and n.page >= 0:
        out.append((n.page, tuple(n.bbox)))
    out += [(int(p[0]), tuple(p[1:5])) for p in n.parts]
    return out


def _item_rects(model: DocModel, li: StructNode, with_sub: bool = False) -> list:
    """Where an item is printed (PDF space): its LI zone, or its parts' zones
    (not the nested list's unless asked)."""
    own = _rects(li)
    if own and with_sub:
        return own
    subs = set()
    if not with_sub:
        sub = _sub(model, li)
        if sub is not None:
            subs = {sub.id} | {d.id for d in model.descendants(sub.id)}
    got = []
    for d in model.descendants(li.id):
        if d.id in subs or d.artifact:
            continue
        got += _rects(d)
    if got:
        per: dict[int, list] = {}
        for p, r in got:
            per.setdefault(p, []).append(r)
        return [(p, ubox(rs)) for p, rs in sorted(per.items())]
    return own


def view(model: DocModel, working_pdf: str, lid: str) -> dict:
    L = model.nodes.get(lid)
    if L is None or L.role != "L":
        raise ListEditError("not a list")
    L = _top(model, L)
    doc = pymupdf.open(working_pdf)
    try:
        items = []
        lists = {}
        for li, level, host in flatten(model, L):
            lbl, body = _part(model, li, "Lbl"), _part(model, li, "LBody")
            rects = [(p, to_disp(doc[p], r)) for p, r in _item_rects(model, li) if 0 <= p < doc.page_count]
            lists.setdefault(host.id, {"id": host.id, "level": level, "numbering": host.list_numbering or "None",
                                       "items": 0})
            lists[host.id]["items"] += 1
            items.append({"id": li.id, "level": level, "list": host.id,
                          "lbl": (lbl.text or "") if lbl is not None else "", "lbl_id": lbl.id if lbl else "",
                          "body": _words(model, body)[:400] if body is not None else _words(model, li)[:400],
                          "page": rects[0][0] if rects else li.page,
                          "rects": [[p, *r] for p, r in rects],
                          "has_sub": _sub(model, li) is not None,
                          "kids": len([c for c in (body.children if body else []) if model.nodes.get(c) is not None
                                       and model.nodes[c].role != "L"])})
        # the picture: the list's zone on each page, in parts that sit apart
        from .tableedit import _clusters
        per: dict[int, list] = {}
        for it in items:
            for r in it["rects"]:
                per.setdefault(r[0], []).append((r[1:5], None, it["id"]))
        pages = {}
        for p, rs in per.items():
            parts = []
            for grp in _clusters(rs, pad=14):
                box = ubox([g[0] for g in grp])
                ids = [g[2] for g in grp]
                tops = sorted(((g[0][1], g[0][3], g[2]) for g in grp), key=lambda t: t[0])
                lines = []
                for (a0, a1, aid), (b0, b1, bid) in zip(tops, tops[1:]):
                    if aid != bid:
                        lines.append({"a": aid, "b": bid, "y": round((a1 + b0) / 2 if b0 > a1 else b0, 2)})
                parts.append({"box": [round(v, 2) for v in box], "items": ids, "lines": lines})
            pages[str(p)] = {"parts": sorted(parts, key=lambda q: (q["box"][0], q["box"][1]))}
        return {"id": L.id, "items": items, "lists": list(lists.values()), "pages": pages,
                "numberings": NUMBERINGS, "count": len(items)}
    finally:
        doc.close()


# ------------------------------------------------------------- building items
def _split_label(line: DLine, regex=None) -> tuple[str, tuple | None, tuple | None, str]:
    """(label, label box, rest box, rest text) of an item's first line."""
    rx = regex or _label_re()
    m = rx.match(line.text)
    if not m:
        return "", None, line.box, line.text
    lbl = (m.group("lbl") if "lbl" in (rx.groupindex or {}) else m.group(0)).strip()
    need = len(re.sub(r"\s", "", lbl))
    vis = [c for c in line.chars if c[0].strip()]
    if need <= 0 or need > len(vis):
        return "", None, line.box, line.text
    lb = ubox([c[1] for c in vis[:need]])
    rest = vis[need:]
    rb = ubox([c[1] for c in rest]) if rest else None
    rtext = line.text[m.end():].strip() if m.end() <= len(line.text) else ""
    return lbl, lb, rb, rtext


def _fill_item(model: DocModel, doc, li: StructNode, lines: list, regex=None, keep_sub: bool = True) -> None:
    """The item's Lbl and LBody again from its printed lines [(page, DLine)]:
    the label's characters make the Lbl, the rest the LBody."""
    sub = _sub(model, li) if keep_sub else None
    if sub is not None:
        model.detach(sub.id)
    for c in list(li.children):
        if c in model.nodes:
            model.remove(c, promote_children=False)
    li.children = []
    if not lines:
        if sub is not None:
            model.add(sub, li.id)
        return
    p0, first = lines[0]
    lbl, lbox, rbox, rtext = _split_label(first, regex)
    body_boxes: dict[int, list] = {}
    texts = []
    if lbl:
        model.add(StructNode(id=model.new_id("lbl"), role="Lbl", page=p0, bbox=to_pdf(doc[p0], lbox),
                             text=lbl, source="user", priority=1), li.id)
        if rbox is not None:
            body_boxes.setdefault(p0, []).append(rbox)
        if rtext:
            texts.append(rtext)
    else:
        body_boxes.setdefault(p0, []).append(first.box)
        texts.append(first.text)
    for p, l in lines[1:]:
        body_boxes.setdefault(p, []).append(l.box)
        texts.append(l.text)
    body = StructNode(id=model.new_id("lb"), role="LBody", page=-1, source="user", priority=1,
                      text=re.sub(r"\s+", " ", " ".join(texts)).strip())
    for p in sorted(body_boxes):
        b = to_pdf(doc[p], ubox(body_boxes[p]))
        if body.bbox is None:
            body.page, body.bbox = p, b
        else:
            body.parts.append([p, *b])
    if body.bbox is None:
        body.page = p0
    model.add(body, li.id)
    if sub is not None:
        model.add(sub, body.id)
    allb: dict[int, list] = {}
    for p, l in lines:
        allb.setdefault(p, []).append(l.box)
    li.page, li.bbox, li.parts = -1, None, []
    for p in sorted(allb):
        b = to_pdf(doc[p], ubox(allb[p]))
        if li.bbox is None:
            li.page, li.bbox = p, b
        else:
            li.parts.append([p, *b])
    li.source = "user"


def _item_lines(model: DocModel, doc, li: StructNode) -> list:
    """The printed lines of an item (not its nested list), in order."""
    sub = _sub(model, li)
    skip = []
    if sub is not None:
        skip = [(p, to_disp(doc[p], r)) for p, r in _rects(sub) if 0 <= p < doc.page_count]
        for d in model.descendants(sub.id):
            skip += [(p, to_disp(doc[p], r)) for p, r in _rects(d) if 0 <= p < doc.page_count]
    out = []
    for p, r in _item_rects(model, li):
        if not 0 <= p < doc.page_count:
            continue
        rd = to_disp(doc[p], r)
        for l in lines_in(doc[p], [rd[0] - 1, rd[1] - 1, rd[2] + 1, rd[3] + 1]):
            if any(q == p and s[0] - 1 <= l.cx <= s[2] + 1 and s[1] - 1 <= l.cy <= s[3] + 1 for q, s in skip):
                continue
            out.append((p, l))
    return out


def _starts(lines: list, mode: str, regex=None) -> list[list]:
    """Group lines [(page, DLine)] into items: a new item at each line that
    starts with a label ("labels"), at every line ("lines"), or one item
    ("one"). Lines before the first label make an item of their own."""
    if mode == "one":
        return [lines] if lines else []
    rx = regex or _label_re()
    groups: list[list] = []
    for p, l in lines:
        if mode == "lines" or not groups or rx.match(l.text):
            groups.append([(p, l)])
        else:
            groups[-1].append((p, l))
    return groups


# ----------------------------------------------------------------- operations
def _new_item(model: DocModel) -> StructNode:
    return StructNode(id=model.new_id("li"), role="LI", page=-1, source="user", note="item set by hand")


def _to_paragraph(model: DocModel, li: StructNode, artifact: bool) -> StructNode:
    """An item out of the list: one P with its label and words (an artifact
    if asked), in the item's zone."""
    lbl, body = _part(model, li, "Lbl"), _part(model, li, "LBody")
    text = " ".join(t for t in ((lbl.text if lbl else ""), _words(model, body) if body else _words(model, li)) if t)
    rects = _item_rects(model, li)
    p = StructNode(id=model.new_id("p"), role="P", page=rects[0][0] if rects else li.page,
                   bbox=rects[0][1] if rects else li.bbox, text=text.strip(), source="user", priority=1,
                   note="list item taken out of the list")
    p.parts = [[q, *r] for q, r in rects[1:]]
    if artifact:
        p.artifact, p.artifact_subtype = True, "Layout"
    return p


def _remove_items(model: DocModel, L: StructNode, ids: list, artifact: bool) -> list:
    """Items out of the list (nested ones with their host), as paragraphs right
    after the outermost list."""
    top = _top(model, L)
    made = []
    for li, level, host in flatten(model, top):
        if li.id in ids and li.id in model.nodes:
            sub = _sub(model, li)
            group = [li] + ([x for x, _, _ in flatten(model, sub)] if sub is not None else [])
            for x in group:
                made.append(_to_paragraph(model, x, artifact))
            model.remove(li.id, promote_children=False)
    anchor = top
    for n in made:
        place_after(model, anchor, [n])
        anchor = n
    _tidy(model, top)
    return [n.id for n in made]


def _tidy(model: DocModel, top: StructNode) -> None:
    """Empty nested lists go; the zones of the lists follow their items."""
    changed = True
    while changed:
        changed = False
        for n in [top] + [d for d in model.descendants(top.id) if top.id in model.nodes]:
            if n.id in model.nodes and n.role == "L" and not _items(model, n) and n.id != top.id:
                model.remove(n.id, promote_children=True)
                changed = True
    if top.id in model.nodes:
        for n in [d for d in model.descendants(top.id) if d.role in ("L",)] + [top]:
            zone_of(model, n)


def _insert_at(model: DocModel, L: StructNode, page: int, y_pdf_top: float) -> tuple[StructNode, int]:
    """Where an item printed at (page, top) goes: after the last item printed
    above it, in that item's list."""
    best = None
    for li, level, host in flatten(model, L):
        rs = _item_rects(model, li, with_sub=True)
        if not rs:
            continue
        p, r = rs[0]
        key = (p, -r[3])
        if (p, -r[3]) <= (page, -y_pdf_top):
            if best is None or key >= best[0]:
                best = (key, li, host)
    if best is None:
        return L, 0
    _, li, host = best
    return host, host.children.index(li.id) + 1


def add_zone(model: DocModel, doc, L: StructNode, page: int, rect, mode: str = "labels") -> list:
    zone_pdf = to_pdf(doc[page], rect)
    lines = [(page, l) for l in lines_in(doc[page], rect)]
    if not lines:
        raise ListEditError("nothing is printed in that zone")
    keep = {L.id} | {d.id for d in model.descendants(_top(model, L).id)}
    # items whose zone the new one covers give way to it
    gone = [li.id for li, _, _ in flatten(model, _top(model, L))
            if any(p == page and zone_pdf[0] - 1 <= (r[0] + r[2]) / 2 <= zone_pdf[2] + 1
                   and zone_pdf[1] - 1 <= (r[1] + r[3]) / 2 <= zone_pdf[3] + 1
                   for p, r in _item_rects(model, li))]
    for g in gone:
        if g in model.nodes:
            model.remove(g, promote_children=False)
    remove_tags_inside(model, page, zone_pdf, keep)
    host, at = _insert_at(model, _top(model, L), page, max(zone_pdf[1], zone_pdf[3]))
    made = []
    for grp in _starts(lines, mode):
        li = _new_item(model)
        model.add(li, host.id, at)
        at += 1
        _fill_item(model, doc, li, grp)
        made.append(li.id)
    if (host.list_numbering or "None") == "None":
        lbl = _part(model, model.nodes[made[0]], "Lbl")
        if lbl is not None:
            host.list_numbering = _numbering(lbl.text)
    _tidy(model, _top(model, host))
    return made


def label_zone(model: DocModel, doc, L: StructNode, page: int, rect) -> list:
    """A box drawn over printed labels (V4.21.2): what is printed inside it
    becomes the Lbl of the item it belongs to, the rest of the item its LBody.
    One box down a column of numbers gives every item there its label."""
    z = [float(v) for v in rect]
    top = _top(model, L)
    done = []
    for li, _lvl, _host in flatten(model, top):
        lines = _item_lines(model, doc, li)
        # a label the analysis did not see lies outside the item's zone: the
        # item's rows are read again as wide as the box on that side
        wide = []
        for p, r in _item_rects(model, li):
            if p != page or not 0 <= p < doc.page_count:
                continue
            rd = to_disp(doc[p], r)
            if min(rd[3], z[3]) - max(rd[1], z[1]) <= 0:
                continue
            reg = [min(rd[0], z[0]) - 1, rd[1] - 1, max(rd[2], z[2]) + 1, rd[3] + 1]
            wide += [(p, l) for l in lines_in(doc[p], reg)]
        if wide:
            lines = [(p, l) for p, l in lines if p != page] + wide
            lines.sort(key=lambda t: (t[0], t[1].box[1], t[1].box[0]))
        if not lines:
            continue
        inside = [(p, l, c) for p, l in lines if p == page for c in l.chars
                  if c[0].strip() and z[0] <= (c[1][0] + c[1][2]) / 2 <= z[2] and z[1] <= (c[1][1] + c[1][3]) / 2 <= z[3]]
        if not inside:
            continue
        _fill_labelled(model, doc, li, lines, page, z)
        done.append(li.id)
    if not done:
        raise ListEditError("draw the box over the label (number or bullet) of an item of this list")
    if (top.list_numbering or "None") == "None":
        lbl = _part(model, model.nodes[done[0]], "Lbl")
        if lbl is not None:
            top.list_numbering = _numbering(lbl.text)
    return done


def _fill_labelled(model: DocModel, doc, li: StructNode, lines: list, page: int, z: list) -> None:
    """The item again from its printed lines, its label = the characters inside
    the box `z` (display space, on `page`)."""
    def in_box(p, c):
        return p == page and z[0] <= (c[1][0] + c[1][2]) / 2 <= z[2] and z[1] <= (c[1][1] + c[1][3]) / 2 <= z[3]
    sub = _sub(model, li)
    if sub is not None:
        model.detach(sub.id)
    for c in list(li.children):
        if c in model.nodes:
            model.remove(c, promote_children=False)
    li.children = []
    lbl_chars = [(p, c) for p, l in lines for c in l.chars if c[0].strip() and in_box(p, c)]
    p0 = lbl_chars[0][0]
    lbl_text = re.sub(r"\s+", " ", "".join(c[0] for _, c in lbl_chars)).strip()
    model.add(StructNode(id=model.new_id("lbl"), role="Lbl", page=p0,
                         bbox=to_pdf(doc[p0], ubox([c[1] for _, c in lbl_chars])),
                         text=lbl_text[:40], source="user", priority=1, note="label set by hand (zone)"), li.id)
    body_boxes: dict[int, list] = {}
    texts = []
    for p, l in lines:
        rest = [c for c in l.chars if not (c[0].strip() and in_box(p, c))]
        vis = [c for c in rest if c[0].strip()]
        if not vis:
            continue
        body_boxes.setdefault(p, []).append(ubox([c[1] for c in vis]))
        texts.append("".join(c[0] for c in rest))
    body = StructNode(id=model.new_id("lb"), role="LBody", page=-1, source="user", priority=1,
                      text=re.sub(r"\s+", " ", " ".join(texts)).strip())
    for p in sorted(body_boxes):
        b = to_pdf(doc[p], ubox(body_boxes[p]))
        if body.bbox is None:
            body.page, body.bbox = p, b
        else:
            body.parts.append([p, *b])
    if body.bbox is not None:
        model.add(body, li.id)
        if sub is not None:
            model.add(sub, body.id)
    elif sub is not None:
        model.add(sub, li.id)
    li.source = "user"


def remove_zone(model: DocModel, L: StructNode, doc, page: int, rect, artifact: bool) -> list:
    zone_pdf = to_pdf(doc[page], rect)
    ids = [li.id for li, _, _ in flatten(model, _top(model, L))
           if any(p == page and zone_pdf[0] - 1 <= (r[0] + r[2]) / 2 <= zone_pdf[2] + 1
                  and zone_pdf[1] - 1 <= (r[1] + r[3]) / 2 <= zone_pdf[3] + 1
                  for p, r in _item_rects(model, li))]
    if not ids:
        raise ListEditError("no item of this list is in that zone")
    return _remove_items(model, L, ids, artifact)


def merge_next(model: DocModel, doc, li: StructNode) -> str:
    host = model.nodes[li.parent]
    items = _items(model, host)
    k = items.index(li)
    if k + 1 >= len(items):
        raise ListEditError("this is the last item of its list")
    nxt = items[k + 1]
    lines = _item_lines(model, doc, li) + _item_lines(model, doc, nxt)
    # the next item's nested list comes along
    sub2 = _sub(model, nxt)
    if sub2 is not None:
        model.detach(sub2.id)
    model.remove(nxt.id, promote_children=False)
    _fill_item(model, doc, li, lines)
    if sub2 is not None:
        body = _part(model, li, "LBody")
        sub1 = _sub(model, li)
        if sub1 is not None:
            for it in _items(model, sub2):
                model.detach(it.id)
                model.add(it, sub1.id)
            model.nodes.pop(sub2.id, None)
        else:
            model.add(sub2, (body or li).id)
    return li.id


def split_item(model: DocModel, doc, li: StructNode, page: int, y: float) -> str:
    lines = _item_lines(model, doc, li)
    first = [(p, l) for p, l in lines if (p, l.cy) < (page, y)]
    second = [(p, l) for p, l in lines if (p, l.cy) >= (page, y)]
    if not first or not second:
        raise ListEditError("split between two printed lines of the item")
    sub = _sub(model, li)
    host = model.nodes[li.parent]
    new = _new_item(model)
    model.add(new, host.id, host.children.index(li.id) + 1)
    _fill_item(model, doc, li, first, keep_sub=False)
    _fill_item(model, doc, new, second, keep_sub=False)
    if sub is not None and sub.id in model.nodes:
        # the nested list goes with the half it is printed under
        if sub.parent in model.nodes and sub.id in model.nodes[sub.parent].children:
            model.detach(sub.id)
        body = _part(model, new, "LBody") or new
        model.add(sub, body.id)
    return new.id


def indent(model: DocModel, li: StructNode) -> None:
    host = model.nodes[li.parent]
    items = _items(model, host)
    k = items.index(li)
    if k == 0:
        raise ListEditError("the first item of a list has nothing above it to go under")
    above = items[k - 1]
    sub = _sub(model, above)
    if sub is None:
        body = _part(model, above, "LBody") or above
        lbl = _part(model, li, "Lbl")
        sub = StructNode(id=model.new_id("list"), role="L", page=li.page, source="user",
                         list_numbering=_numbering(lbl.text) if lbl is not None else "None",
                         note="nested by hand")
        model.add(sub, body.id)
    model.detach(li.id)
    model.add(li, sub.id)


def outdent(model: DocModel, li: StructNode) -> None:
    host = model.nodes[li.parent]
    owner = next((a for a in model.ancestors(host.id) if a.role == "LI"), None)
    if owner is None:
        raise ListEditError("this item is in the outermost list already")
    outer = model.nodes[owner.parent]
    items = _items(model, host)
    k = items.index(li)
    after = items[k + 1:]
    model.detach(li.id)
    model.add(li, outer.id, outer.children.index(owner.id) + 1)
    if after:
        # the items after it in that list now sit under it
        sub = _sub(model, li)
        if sub is None:
            body = _part(model, li, "LBody") or li
            sub = StructNode(id=model.new_id("list"), role="L", page=after[0].page, source="user",
                             list_numbering=host.list_numbering, note="nested by hand")
            model.add(sub, body.id)
        for it in after:
            model.detach(it.id)
            model.add(it, sub.id)
    if not _items(model, host):
        model.remove(host.id, promote_children=True)


def move(model: DocModel, li: StructNode, d: int) -> None:
    host = model.nodes[li.parent]
    sib = host.children
    items = [c for c in sib if model.nodes.get(c) is not None and model.nodes[c].role == "LI"]
    k = items.index(li.id)
    j = k + (1 if d > 0 else -1)
    if not 0 <= j < len(items):
        raise ListEditError("it is at the end of its list")
    a, b = sib.index(items[k]), sib.index(items[j])
    sib[a], sib[b] = sib[b], sib[a]


def not_a_list(model: DocModel, L: StructNode) -> list:
    top = _top(model, L)
    made = [_to_paragraph(model, li, False) for li, _, _ in flatten(model, top)]
    parent = top.parent
    sib = model.siblings(top.id)
    at = sib.index(top.id)
    model.remove(top.id, promote_children=False)
    for k, n in enumerate(made):
        model.add(n, parent, at + k)
    return [n.id for n in made]


def split_list(model: DocModel, li: StructNode) -> str:
    host = model.nodes[li.parent]
    top = _top(model, host)
    if host.id != top.id:
        raise ListEditError("split the outermost list (pick an item of it)")
    items = _items(model, host)
    k = items.index(li)
    if k == 0:
        raise ListEditError("pick the item the new list starts with (not the first)")
    L2 = StructNode(id=model.new_id("list"), role="L", page=li.page, source="user",
                    list_numbering=host.list_numbering, note="list split by hand")
    place_after(model, host, [L2])
    for it in items[k:]:
        model.detach(it.id)
        model.add(it, L2.id)
    zone_of(model, host)
    zone_of(model, L2)
    return L2.id


def merge_next_list(model: DocModel, L: StructNode) -> str:
    top = _top(model, L)
    order = model.ordered_nodes()
    k = order.index(top)
    nxt = next((n for n in order[k + 1:] if n.role == "L" and not any(a.id == top.id for a in model.ancestors(n.id))
                and not any(a.role == "L" for a in model.ancestors(n.id))), None)
    if nxt is None:
        raise ListEditError("no list after this one")
    for it in _items(model, nxt):
        model.detach(it.id)
        model.add(it, top.id)
    model.remove(nxt.id, promote_children=True)
    zone_of(model, top)
    return nxt.id


# ------------------------------------------------- V4.21: the whole list at once
def flatten_all(model: DocModel, top: StructNode) -> list:
    """Every item of the list (nested ones too) into the outermost list, in order."""
    items = [li for li, _, _ in flatten(model, top)]
    subs = [d for d in model.descendants(top.id) if d.role == "L"]
    for li in items:
        model.detach(li.id)
    for sub in subs:
        if sub.id in model.nodes:
            if sub.parent is not None and sub.parent in model.nodes or sub.id in model.root_children:
                model.remove(sub.id, promote_children=False)
            else:
                model.nodes.pop(sub.id, None)
    for li in items:
        model.add(li, top.id)
    return items


def _label_of(model: DocModel, li: StructNode) -> str:
    lbl = _part(model, li, "Lbl")
    return (lbl.text or "").strip() if lbl is not None else ""


def join_unlabelled(model: DocModel, doc, top: StructNode) -> int:
    """An item with no label that does not start like one is the line after
    the item above it (a wrapped line taken for an item): they become one."""
    rx = _label_re()
    joined = 0
    changed = True
    while changed:
        changed = False
        for li, level, host in flatten(model, top):
            items = _items(model, host)
            k = items.index(li)
            if k == 0 or _label_of(model, li):
                continue
            first = _item_lines(model, doc, li)
            if first and rx.match(first[0][1].text):
                continue
            merge_next(model, doc, items[k - 1])
            joined += 1
            changed = True
            break
    return joined


def regroup(model: DocModel, doc, top: StructNode, mode: str = "labels") -> int:
    """The list's items made again from its printed lines: a new item at each
    label ("labels") or at each line ("lines"). Nesting is let go."""
    items = flatten_all(model, top)
    lines = []
    for li in items:
        lines += _item_lines(model, doc, li)
    if not lines:
        return 0
    lines.sort(key=lambda t: (t[0], t[1].box[1], t[1].box[0]))
    groups = _starts(lines, mode)
    for li in items:
        model.remove(li.id, promote_children=False)
    for g in groups:
        li = _new_item(model)
        model.add(li, top.id)
        _fill_item(model, doc, li, g)
    return len(groups)


def auto_nest(model: DocModel, doc, top: StructNode) -> int:
    """Nest the items by their labels: the first kind of label (1. / • …) is
    the outer level, another kind one level in, a kind seen before goes back
    to its level; items with no label nest by how far in they are printed."""
    items = flatten_all(model, top)
    levels: list[int] = []
    shapes: list[str] = []
    xs: list[float] = []
    for li in items:
        lbl = _label_of(model, li)
        shp = _shape(lbl) if lbl else ""
        rs = _item_rects(model, li)
        xs.append(rs[0][1][0] if rs else 0.0)
        shapes.append(shp)
    seen: list[str] = []
    for k, li in enumerate(items):
        if shapes[k]:
            if shapes[k] not in seen:
                seen.append(shapes[k])
            lv = seen.index(shapes[k])
        else:
            lv = levels[-1] if levels else 0
            if k and xs[k] > xs[k - 1] + 6:
                lv = (levels[-1] if levels else 0) + 1
            elif k and xs[k] < xs[k - 1] - 6:
                lv = max(0, (levels[-1] if levels else 0) - 1)
        if levels:
            lv = min(lv, levels[-1] + 1)
        else:
            lv = 0
        levels.append(lv)
    for li, lv in zip(items, levels):
        for _ in range(lv):
            try:
                indent(model, li)
            except ListEditError:
                break
    for n in [top] + [d for d in model.descendants(top.id) if d.role == "L"]:
        lb = next((_label_of(model, li) for li in _items(model, n) if _label_of(model, li)), "")
        if lb:
            n.list_numbering = _numbering(lb)
    return max(levels) + 1 if levels else 0


def numbering_from_labels(model: DocModel, top: StructNode) -> int:
    k = 0
    for n in [top] + [d for d in model.descendants(top.id) if d.role == "L"]:
        lb = next((_label_of(model, li) for li in _items(model, n) if _label_of(model, li)), "")
        v = _numbering(lb) if lb else "None"
        if n.list_numbering != v:
            n.list_numbering = v
            k += 1
    return k


def apply(model: DocModel, working_pdf: str, lid: str, op: dict) -> dict:
    if op.get("op") == "many":
        # several items at once (V4.21.4: Shift / Ctrl selection): one change, one undo
        out: dict = {"ok": True, "touched": []}
        for sub in op.get("ops") or []:
            if lid not in model.nodes or model.nodes[lid].role != "L":
                sid = str(sub.get("id") or "")
                nl = next((a.id for a in (model.ancestors(sid) if sid in model.nodes else []) if a.role == "L"), None)
                if nl is None:
                    return {**out, "gone": True}
                lid = nl
            r = apply(model, working_pdf, lid, sub)
            out["touched"] += r.get("touched") or []
            if r.get("gone"):
                return {**out, **r}
        return out
    L = model.nodes.get(lid)
    if L is None or L.role != "L":
        raise ListEditError("not a list")
    top = _top(model, L)
    kind = str(op.get("op") or "")
    it = model.nodes.get(str(op.get("id") or ""))
    if kind in ("remove", "merge_next", "split", "indent", "outdent", "move", "relabel", "set_label",
                "set_body", "split_list") and (it is None or it.role != "LI"):
        raise ListEditError("pick an item")
    doc = pymupdf.open(working_pdf)
    out: dict = {"ok": True}
    try:
        if kind == "add_zone":
            out["touched"] = add_zone(model, doc, top, int(op.get("page", 0)), op.get("rect"),
                                      str(op.get("mode") or "labels"))
        elif kind == "label_zone":
            out["touched"] = label_zone(model, doc, top, int(op.get("page", 0)), op.get("rect"))
        elif kind == "remove_zone":
            out["made"] = remove_zone(model, top, doc, int(op.get("page", 0)), op.get("rect"),
                                      bool(op.get("artifact")))
        elif kind == "remove":
            out["made"] = _remove_items(model, top, [it.id], bool(op.get("artifact")))
        elif kind == "merge_next":
            out["touched"] = [merge_next(model, doc, it)]
        elif kind == "split":
            out["touched"] = [it.id, split_item(model, doc, it, int(op.get("page", 0)), float(op.get("y", 0)))]
        elif kind == "move_line":
            b = model.nodes.get(str(op.get("b") or ""))
            if b is None:
                raise ListEditError("no such item")
            merge_next(model, doc, it)
            out["touched"] = [it.id, split_item(model, doc, it, int(op.get("page", 0)), float(op.get("y", 0)))]
        elif kind == "indent":
            indent(model, it)
            out["touched"] = [it.id]
        elif kind == "outdent":
            outdent(model, it)
            out["touched"] = [it.id]
        elif kind == "move":
            move(model, it, int(op.get("dir", 1)))
            out["touched"] = [it.id]
        elif kind == "relabel":
            _fill_item(model, doc, it, _item_lines(model, doc, it))
            out["touched"] = [it.id]
        elif kind == "set_label":
            lbl = _part(model, it, "Lbl")
            if lbl is None:
                raise ListEditError("this item has no label printed — use “Find the label”")
            lbl.text = str(op.get("text") or "")[:40]
            lbl.source = "user"
            out["touched"] = [it.id]
        elif kind == "set_body":
            body = _part(model, it, "LBody")
            if body is None:
                raise ListEditError("this item has no LBody")
            body.text = str(op.get("text") or "")[:4000]
            body.source = "user"
            out["touched"] = [it.id]
        elif kind == "numbering":
            target = model.nodes.get(str(op.get("list") or lid))
            if target is None or target.role != "L":
                raise ListEditError("no such list")
            v = str(op.get("value") or "None")
            if v not in NUMBERINGS:
                raise ListEditError("unknown numbering")
            target.list_numbering = v
            target.source = "user"
        elif kind == "not_a_list":
            out["made"] = not_a_list(model, top)
            out["gone"] = True
            return out
        elif kind == "split_list":
            out["new_list"] = split_list(model, it)
        elif kind == "merge_next_list":
            out["joined"] = merge_next_list(model, top)
        elif kind == "join_unlabelled":
            out["joined"] = join_unlabelled(model, doc, top)
        elif kind == "one_per_line":
            out["items"] = regroup(model, doc, top, "lines")
        elif kind == "by_labels":
            out["items"] = regroup(model, doc, top, "labels")
        elif kind == "auto_nest":
            out["levels"] = auto_nest(model, doc, top)
        elif kind == "flatten":
            out["items"] = len(flatten_all(model, top))
        elif kind == "numbering_auto":
            out["changed"] = numbering_from_labels(model, top)
        else:
            raise ListEditError(f"unknown list change {kind}")
    finally:
        doc.close()
    if top.id in model.nodes:
        _tidy(model, top)
        top.source = "user"
    return out


# --------------------------------------------------------------- templates
def _shape(lbl: str) -> str:
    """A label's shape as a pattern: "3." -> \\d{1,3}\\.  "(b)" -> \\([a-z]\\)."""
    s = lbl.strip()
    if not s:
        return ""
    out = ""
    i = 0
    while i < len(s):
        ch = s[i]
        if ch.isdigit():
            j = i
            while j < len(s) and s[j].isdigit():
                j += 1
            out += r"\d{1,3}"
            i = j
            continue
        if re.fullmatch(r"[ivxlc]+", s[i:].rstrip(".)]:")) and len(s.rstrip(".)]:")) - i > 1:
            j = i
            while j < len(s) and s[j] in "ivxlc":
                j += 1
            out += "[ivxlc]{1,6}"
            i = j
            continue
        if re.fullmatch(r"[IVXLC]+", s[i:].rstrip(".)]:")) and len(s.rstrip(".)]:")) - i > 1:
            j = i
            while j < len(s) and s[j] in "IVXLC":
                j += 1
            out += "[IVXLC]{1,6}"
            i = j
            continue
        if ch.isalpha():
            out += "[a-z]" if ch.islower() else "[A-Z]"
        else:
            out += re.escape(ch)
        i += 1
    return out


def list_spec(model: DocModel, working_pdf: str, lid: str) -> dict:
    """What makes this list's format: per level its label shape and
    numbering, how many lines an item has, nesting."""
    L = model.nodes.get(lid)
    if L is None or L.role != "L":
        raise ListEditError("not a list")
    top = _top(model, L)
    levels: dict[int, dict] = {}
    doc = pymupdf.open(working_pdf)
    nlines = []
    xs: dict[int, list] = {}
    try:
        for li, level, host in flatten(model, top):
            lbl = _part(model, li, "Lbl")
            lv = levels.setdefault(level, {"shapes": {}, "numbering": host.list_numbering or "None"})
            if lbl is not None and (lbl.text or "").strip():
                sh = _shape(lbl.text)
                lv["shapes"][sh] = lv["shapes"].get(sh, 0) + 1
            ls = _item_lines(model, doc, li)
            if ls:
                nlines.append(len(ls))
                xs.setdefault(level, []).append(ls[0][1].box[0])
    finally:
        doc.close()
    spec_levels = []
    for lv in sorted(levels):
        d = levels[lv]
        shapes = sorted(d["shapes"].items(), key=lambda kv: -kv[1])
        spec_levels.append({"level": lv, "shape": shapes[0][0] if shapes else "", "numbering": d["numbering"],
                            "x": round(sum(xs.get(lv, [0])) / max(1, len(xs.get(lv, []))), 1)})
    return {"kind": "list", "levels": spec_levels, "nested": len(spec_levels) > 1,
            "labels": any(lv["shape"] for lv in spec_levels),
            "lines_per_item": round(sum(nlines) / max(1, len(nlines)), 2), "items": len(nlines)}


def _apply_groups(spec: dict, lines: list) -> list[tuple[int, list]]:
    """[(level, lines)] for the lines of a list, the template's way."""
    shapes = [(lv["level"], re.compile(r"^\s*(?P<lbl>" + lv["shape"] + r")(?=\s|$)"))
              for lv in spec["levels"] if lv.get("shape")]
    groups: list[list] = []
    if shapes:
        for p, l in lines:
            hit = [lv for lv, rx in shapes if rx.match(l.text)]
            if hit or not groups:
                groups.append([hit, [(p, l)]])
            else:
                groups[-1][1].append((p, l))
    else:
        # no labels: an item per line, or per block of lines set apart
        one = spec.get("lines_per_item", 1) <= 1.2
        prev = None
        for p, l in lines:
            h = l.box[3] - l.box[1]
            new = one or prev is None or prev[0] != p or l.box[1] - prev[1].box[3] > 0.55 * h
            if new or not groups:
                groups.append([[], [(p, l)]])
            else:
                groups[-1][1].append((p, l))
            prev = (p, l)
    # the level: by the shape where the shapes differ, else by how far in
    starts = [g[1][0][1].box[0] for g in groups]
    indents = sorted({round(x / 6) for x in starts})
    out = []
    nlev = max(1, len(spec["levels"])) if spec.get("nested") else 1
    for (hit, ls), x in zip(groups, starts):
        if len(hit) == 1 and spec.get("nested"):
            lv = hit[0]
        elif spec.get("nested"):
            lv = min(nlev - 1, indents.index(round(x / 6)))
        else:
            lv = 0
        out.append((lv, ls))
    return out


def apply_spec(model: DocModel, working_pdf: str, spec: dict, lid: str | None = None,
               page: int | None = None, rect=None) -> str:
    """Give a list (or the lines in a zone) the template's format: items at the
    template's labels, nested by its levels, its numbering."""
    if spec.get("kind") != "list":
        raise ListEditError("that template is not a list template")
    doc = pymupdf.open(working_pdf)
    try:
        lines: list = []
        if lid:
            L = model.nodes.get(lid)
            if L is None or L.role != "L":
                raise ListEditError("not a list")
            top = _top(model, L)
            zones = {}
            for d in [top] + model.descendants(top.id):
                for p, r in _rects(d):
                    zones.setdefault(p, []).append(r)
            for p in sorted(zones):
                z = to_disp(doc[p], ubox(zones[p]))
                lines += [(p, l) for l in lines_in(doc[p], [z[0] - 1, z[1] - 1, z[2] + 1, z[3] + 1])]
            parent = top.parent
            at = model.siblings(top.id).index(top.id)
            keep_id = top.id
            model.remove(top.id, promote_children=False)
            L = StructNode(id=keep_id, role="L", page=lines[0][0] if lines else top.page, source="user",
                           note=f"list given template {spec.get('number', '')}".strip())
            model.add(L, parent, at)
        else:
            if page is None or rect is None:
                raise ListEditError("pick a list or draw a zone")
            lines = [(page, l) for l in lines_in(doc[page], rect)]
            zone_pdf = to_pdf(doc[page], rect)
            remove_tags_inside(model, page, zone_pdf, set())
            parent, at = flow_position(model, page, zone_pdf)
            L = StructNode(id=model.new_id("list"), role="L", page=page, source="user",
                           note=f"list made with template {spec.get('number', '')}".strip())
            model.add(L, parent, at)
        if not lines:
            raise ListEditError("nothing is printed there")
        groups = _apply_groups(spec, lines)
        nums = {lv["level"]: lv.get("numbering") or "None" for lv in spec["levels"]}
        L.list_numbering = nums.get(0, "None")
        stack: list[tuple[int, StructNode, StructNode | None]] = [(0, L, None)]   # level, list, last item
        rxs = {lv["level"]: re.compile(r"^\s*(?P<lbl>" + lv["shape"] + r")(?=\s|$)")
               for lv in spec["levels"] if lv.get("shape")}
        for lv, ls in groups:
            while stack[-1][0] > lv:
                stack.pop()
            if stack[-1][0] < lv and stack[-1][2] is not None:
                host_item = stack[-1][2]
                body = _part(model, host_item, "LBody") or host_item
                sub = StructNode(id=model.new_id("list"), role="L", page=ls[0][0], source="user",
                                 list_numbering=nums.get(lv, "None"))
                model.add(sub, body.id)
                stack.append((lv, sub, None))
            lvl, host, _ = stack[-1]
            li = _new_item(model)
            model.add(li, host.id)
            _fill_item(model, doc, li, ls, rxs.get(lvl) or (None if spec.get("labels") else re.compile(r"^(?!)")))
            stack[-1] = (lvl, host, li)
        _tidy(model, L)
        return L.id
    finally:
        doc.close()
