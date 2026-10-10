"""Make the background under text unambiguous.

Layouts from InDesign often paint a white (or tinted) panel first and then
coloured cells on top: white text sits on the coloured cell, and the panel is
still underneath it. Anything that renders the page sees the right colours,
but a checker that infers the background from vector fills (PAC 2024's
contrast check does) can pick the hidden panel instead of the cell on top and
report "white text on white" for text that really has 8:1 contrast.

trim_hidden_fills() removes, from earlier fill-only rectangle paths, the
parts that are completely hidden under a later opaque rectangle that has
visible text on it. The removed area is inset by 1pt so no seam can appear at
the edges; nothing on the page changes visually (tests compare renders).
Only the safe case is touched: rectangle-only paths, fill only, same
transformation, no clipping, and an opaque, normal-blend, non-overprinting,
non-pattern fill on top.
"""
from __future__ import annotations

import pikepdf
from pikepdf import Name, Operator
from pikepdf import ContentStreamInstruction as CSI

from .pdftext import (IDENTITY, PAINT_OPS, PATH_OPS, Interpreter, mmul, num,
                      page_box, page_resources)

FILL_ONLY = {"f", "F", "f*"}
INSET = 1.0
MAX_PIECES = 400


def _norm_rect(x, y, w, h):
    return (min(x, x + w), min(y, y + h), max(x, x + w), max(y, y + h))


def _overlap(a, b) -> bool:
    return min(a[2], b[2]) - max(a[0], b[0]) > 1e-6 and min(a[3], b[3]) - max(a[1], b[1]) > 1e-6


def _subtract(a, b) -> list:
    """a minus b, both (x0, y0, x1, y1)."""
    if not _overlap(a, b):
        return [a]
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    out = []
    if a[1] < iy0:
        out.append((a[0], a[1], a[2], iy0))
    if iy1 < a[3]:
        out.append((a[0], iy1, a[2], a[3]))
    if a[0] < ix0:
        out.append((a[0], iy0, ix0, iy1))
    if ix1 < a[2]:
        out.append((ix1, iy0, a[2], iy1))
    return [r for r in out if r[2] - r[0] > 1e-4 and r[3] - r[1] > 1e-4]


def _gs_update(state: dict, g) -> None:
    if not isinstance(g, pikepdf.Dictionary):
        return
    if "/ca" in g:
        state["ca"] = num(g.get("/ca"), 1.0)
    if "/BM" in g:
        bm = g.get("/BM")
        if isinstance(bm, pikepdf.Array) and len(bm):
            bm = bm[0]
        state["bm"] = str(bm)
    if "/SMask" in g:
        sm = g.get("/SMask")
        state["smask"] = not (isinstance(sm, Name) and str(sm) == "/None")
    if "/op" in g:
        state["op"] = bool(g.get("/op"))
    elif "/OP" in g:
        state["op"] = bool(g.get("/OP"))


def _opaque(state: dict) -> bool:
    return (state["ca"] >= 0.999 and state["bm"] in ("/Normal", "/Compatible")
            and not state["smask"] and not state["op"] and not state["pattern"])


def trim_hidden_fills(pdf: pikepdf.Pdf, pno: int) -> int:
    """Returns the number of fill paths that were trimmed on this page."""
    page = pdf.pages[pno]
    res = page_resources(page.obj)
    try:
        ins = list(pikepdf.parse_content_stream(page))
    except Exception:
        return 0
    infos = Interpreter(res, page_box(page.obj)).run(ins)
    extg = res.get("/ExtGState")
    state = {"ctm": IDENTITY, "clipped": False, "ca": 1.0, "bm": "/Normal", "smask": False,
             "op": False, "pattern": False, "tr": 0}
    stack = []
    paths = []
    glyphs = []                    # (op index, x, y) of visible glyph centres
    cur_start = None
    cur_re: list[int] = []
    cur_ok = True
    pending_clip = False
    for idx, instr in enumerate(ins):
        op = str(instr.operator)
        ops = list(instr.operands)
        if op == "q":
            stack.append(dict(state))
        elif op == "Q":
            state = stack.pop() if stack else state
        elif op == "cm" and len(ops) == 6:
            state["ctm"] = mmul(tuple(num(v) for v in ops), state["ctm"])
        elif op == "gs" and ops and isinstance(extg, pikepdf.Dictionary):
            _gs_update(state, extg.get(ops[0]))
        elif op == "Tr" and ops:
            state["tr"] = int(num(ops[0]))
        elif op == "cs" and ops:
            state["pattern"] = str(ops[0]) == "/Pattern"
        elif op in ("g", "rg", "k"):
            state["pattern"] = False
        elif op == "scn" and ops and isinstance(ops[-1], Name):
            state["pattern"] = True
        elif op in PATH_OPS:
            if cur_start is None:
                cur_start, cur_re, cur_ok = idx, [], True
            if op == "re" and len(ops) == 4:
                cur_re.append(idx)
            else:
                cur_ok = False
        elif op in ("W", "W*"):
            pending_clip = True
        elif op in PAINT_OPS:
            if cur_start is not None and op in FILL_ONLY and cur_ok and cur_re and not pending_clip:
                paths.append({"start": cur_start, "paint": idx, "re": list(cur_re), "op": op,
                              "ctm": state["ctm"], "clipped": state["clipped"],
                              "opaque": _opaque(state)})
            if pending_clip:
                state["clipped"] = True
            pending_clip = False
            cur_start = None
        info = infos[idx]
        if info is not None and info.kind == "text" and info.glyphs and state["tr"] not in (3, 7):
            glyphs.extend((idx, g.cx, g.cy) for g in info.glyphs)
    if not paths or not glyphs:
        return 0

    def user_rects(p):
        return [_norm_rect(*[num(v) for v in ins[i].operands]) for i in p["re"]]

    def axis_aligned(m):
        return abs(m[1]) < 1e-9 and abs(m[2]) < 1e-9 and abs(m[0]) > 1e-9 and abs(m[3]) > 1e-9

    def to_page(m, r):
        a, _, _, d, e, f = m
        xs = (a * r[0] + e, a * r[2] + e)
        ys = (d * r[1] + f, d * r[3] + f)
        return (min(xs), min(ys), max(xs), max(ys))

    def to_user(m, r):
        a, _, _, d, e, f = m
        xs = ((r[0] - e) / a, (r[2] - e) / a)
        ys = ((r[1] - f) / d, (r[3] - f) / d)
        return (min(xs), min(ys), max(xs), max(ys))

    current: dict[int, list] = {}
    for ui, U in enumerate(paths):
        if not U["opaque"] or U["clipped"] or not axis_aligned(U["ctm"]):
            continue
        covers = []
        for r in user_rects(U):
            pr = to_page(U["ctm"], r)
            if pr[2] - pr[0] <= 2 * INSET + 0.5 or pr[3] - pr[1] <= 2 * INSET + 0.5:
                continue
            if any(k > U["paint"] and pr[0] <= x <= pr[2] and pr[1] <= y <= pr[3] for k, x, y in glyphs):
                covers.append((pr[0] + INSET, pr[1] + INSET, pr[2] - INSET, pr[3] - INSET))
        if not covers:
            continue
        for li in range(ui):
            L = paths[li]
            if L["paint"] >= U["start"] or not axis_aligned(L["ctm"]) or L["clipped"]:
                continue
            if any(abs(a - b) > 1e-6 for a, b in zip(L["ctm"], U["ctm"])):
                continue
            rects = current.get(li)
            if rects is None:
                rects = user_rects(L)
                if len(rects) > 1:
                    # overlapping subpaths could depend on winding — leave those alone
                    if L["op"] == "f*" or any(_overlap(rects[i], rects[j])
                                              for i in range(len(rects)) for j in range(i + 1, len(rects))):
                        continue
            new = rects
            for c in covers:
                cu = to_user(L["ctm"], c)
                nxt = []
                for r in new:
                    nxt.extend(_subtract(r, cu))
                new = nxt
                if len(new) > MAX_PIECES:
                    break
            if len(new) > MAX_PIECES:
                continue
            if new != rects:
                current[li] = new
    if not current:
        return 0
    replace: dict[int, list] = {}
    drop: set = set()
    for li, rects in current.items():
        L = paths[li]
        first, rest = L["re"][0], L["re"][1:]
        ops = [CSI([round(r[0], 4), round(r[1], 4), round(r[2] - r[0], 4), round(r[3] - r[1], 4)],
                   Operator("re")) for r in rects]
        if not ops:
            ops = [CSI([0, 0, 0, 0], Operator("re"))]
        replace[first] = ops
        drop.update(rest)
    out = []
    for idx, instr in enumerate(ins):
        if idx in drop:
            continue
        if idx in replace:
            out.extend(replace[idx])
            continue
        out.append(instr)
    page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(out))
    return len(current)


def _lum(col) -> float | None:
    """Rough relative luminance of a fill colour (op, comps); None if unknown."""
    op, comps = col
    n = len(comps)
    if n == 1:
        r = g = b = comps[0]
    elif n == 3:
        r, g, b = comps
    elif n == 4:
        c, m, y, k = comps
        r, g, b = (1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)
    else:
        return None

    def lin(v):
        v = max(0.0, min(1.0, v))
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _ratio(a: float, b: float) -> float:
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def stacked_backgrounds(pdf: pikepdf.Pdf, pno: int) -> int:
    """Text runs that sit on two or more opaque fills of different colours
    (the upper one hides the lower). Renders fine, but vector-based contrast
    checkers may measure against the hidden fill."""
    page = pdf.pages[pno]
    res = page_resources(page.obj)
    try:
        ins = list(pikepdf.parse_content_stream(page))
    except Exception:
        return 0
    infos = Interpreter(res, page_box(page.obj)).run(ins)
    state = {"ctm": IDENTITY, "tr": 0, "fill": ("g", (0.0,))}
    stack = []
    fills = []            # (idx, [page rects], colour)
    cur_re: list = []
    cur_rect_only = True
    in_path = False
    count = 0
    for idx, instr in enumerate(ins):
        op = str(instr.operator)
        ops = list(instr.operands)
        if op == "q":
            stack.append(dict(state))
        elif op == "Q":
            state = stack.pop() if stack else state
        elif op == "cm" and len(ops) == 6:
            state["ctm"] = mmul(tuple(num(v) for v in ops), state["ctm"])
        elif op == "Tr" and ops:
            state["tr"] = int(num(ops[0]))
        elif op in ("g", "rg", "k", "sc", "scn"):
            state["fill"] = (op, tuple(round(num(v), 3) for v in ops if not isinstance(v, Name)))
        elif op == "cs" and ops:
            state["fill"] = (str(ops[0]), ())
        elif op in PATH_OPS:
            if not in_path:
                in_path, cur_re, cur_rect_only = True, [], True
            if op == "re" and len(ops) == 4:
                a, b, c, d, e, f = state["ctm"]
                x, y, w, h = (num(v) for v in ops)
                pts = [(a * px + c * py + e, b * px + d * py + f) for px, py in
                       ((x, y), (x + w, y), (x, y + h), (x + w, y + h))]
                cur_re.append((min(p[0] for p in pts), min(p[1] for p in pts),
                               max(p[0] for p in pts), max(p[1] for p in pts)))
            else:
                cur_rect_only = False
        elif op in PAINT_OPS:
            info = infos[idx]
            if op in ("f", "F", "f*", "B", "B*", "b", "b*") and info is not None and info.bbox:
                rects = cur_re if (cur_rect_only and cur_re) else [tuple(info.bbox)]
                fills.append((idx, rects, state["fill"]))
            in_path = False
        info = infos[idx]
        if info is not None and info.kind == "text" and info.glyphs and state["tr"] not in (3, 7):
            g = info.glyphs[len(info.glyphs) // 2]
            under = [col for k, rects, col in fills
                     if k < idx and any(r[0] <= g.cx <= r[2] and r[1] <= g.cy <= r[3] for r in rects)]
            if len(under) >= 2 and len(set(under)) >= 2:
                lt = _lum(state["fill"])
                top = _lum(under[-1])
                hidden = [_lum(u) for u in under[:-1]]
                if lt is not None and top is not None and _ratio(lt, top) >= 4.5 and \
                        any(h is not None and _ratio(lt, h) < 4.5 for h in hidden):
                    count += 1
    return count
