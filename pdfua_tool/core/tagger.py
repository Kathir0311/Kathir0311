"""Rewrite page content streams with marked content.

Two passes per page:

1. Interpret the stream (core.pdftext) and decide, for every glyph, path,
   image and form, which structure node owns it.
2. Re-emit the stream, opening/closing BDC ... EMC sequences where the owner
   changes, splitting Tj/TJ operators mid-string when ownership changes
   inside one operator.

Visual output is unchanged: only marked-content operators are added or
removed, and split text operators advance the text matrix exactly as the
original single operator did.

Preserved from the source:
  * optional-content markers (/OC ... BDC) - never stripped
  * marked-content spans carrying /ActualText, /Alt, /E or /Lang - kept
    and nested inside the new tagged sequence
  * in import mode, original MCID membership decides ownership
"""
from __future__ import annotations

import collections
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import pikepdf
from pikepdf import Array, Dictionary, Name, Operator, String
from pikepdf import ContentStreamInstruction as CSI

from .model import GRAPHIC_ROLES, DocModel, StructNode, area, intersection
from .pdftext import (CLIP_OPS, INLINE_IMAGE_OPS, PAINT_OPS, PATH_OPS, SHOW_OPS,
                      Interpreter, OpInfo, num, page_box, page_resources)

KEEP_PROP_KEYS = ("/ActualText", "/Alt", "/E", "/Lang")
ART = "ART"                       # generic layout artifact owner


@dataclass
class PageResult:
    mcid_owner: list[str] = field(default_factory=list)      # mcid -> node id
    unmatched: list[tuple[float, float]] = field(default_factory=list)
    to_nearest: int = 0
    glyphs: int = 0
    split_ops: int = 0
    kept_oc: int = 0
    kept_spans: int = 0
    stripped: int = 0
    forms_whole: int = 0
    text_splits: int = 0
    error: str = ""


# ----------------------------------------------------------------- targets
class Targets:
    def __init__(self, nodes: list[StructNode], page: int, model: DocModel | None = None) -> None:
        items = []
        for n in nodes:
            for r in n.rects_on(page):
                items.append((r, n, area(r)))

        def depth(n: StructNode) -> int:
            if model is None:
                return 0
            d, cur = 0, n.parent
            while cur is not None and cur in model.nodes and d < 64:
                d += 1
                cur = model.nodes[cur].parent
            return d
        deep = {id(n): depth(n) for n in nodes}
        # user-drawn nodes first, then the smallest box wins; on the same box the
        # inner tag (a Reference and its Link over one citation: the Link)
        items.sort(key=lambda t: (-t[1].priority, t[2], -deep.get(id(t[1]), 0)))
        self.items = items
        # a tag's own children come before it wherever both cover a glyph: the
        # zone of a paragraph that took in a piece of text ("1,2" after a name)
        # must not take that piece away from the Link printed over it
        self.inner: dict[str, list] = {}
        if model is not None:
            ids = {n.id for n in nodes}
            for t in items:
                cur = t[1].parent
                hops = 0
                while cur is not None and cur in model.nodes and hops < 64:
                    if cur in ids:
                        self.inner.setdefault(cur, []).append(t)
                    cur = model.nodes[cur].parent
                    hops += 1
        # graphics (paths, images) go to artifacts, figures and write-in lines
        # (a P that is only a drawn rule, read through its ActualText)
        self.graphic = [t for t in items if t[1].artifact or t[1].role in GRAPHIC_ROLES
                        or (t[1].style or {}).get("blank_line")
                        or (not t[1].children and not (t[1].text or "").strip()
                            and (t[1].actual_text or "").strip())]

    def hit(self, x: float, y: float, pad: float = 1.5) -> StructNode | None:
        # a glyph printed inside a zone belongs to it before a neighbour that
        # only reaches it with the margin: the ":" of "Date:" sits 1 pt left of
        # the write-in line's Span, and the margin gave it to the Span
        for (x0, y0, x1, y1), n, _ in self.items:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return self._inmost(n, x, y)
        for (x0, y0, x1, y1), n, _ in self.items:
            if x0 - pad <= x <= x1 + pad and y0 - pad <= y <= y1 + pad:
                return self._inmost(n, x, y)
        return None

    def _inmost(self, n: StructNode, x: float, y: float) -> StructNode:
        seen = 0
        while self.inner and seen < 32:
            seen += 1
            for (x0, y0, x1, y1), c, _ in self.inner.get(n.id, ()):
                if x0 <= x <= x1 and y0 <= y <= y1 and not c.artifact:
                    n = c
                    break
            else:
                return n
        return n

    def nearest(self, x: float, y: float, limit: float) -> StructNode | None:
        best, bestd = None, limit
        for (x0, y0, x1, y1), n, _ in self.items:
            if n.artifact:
                continue
            dx = max(x0 - x, 0.0, x - x1)
            dy = max(y0 - y, 0.0, y - y1)
            d = (dx * dx + dy * dy) ** 0.5
            if d < bestd:
                best, bestd = n, d
        return best

    def graphic_owner(self, box: tuple | None, any_role: bool = False) -> StructNode | None:
        if not box:
            return None
        a = max(area(box), 1e-6)
        pool = self.items if any_role else self.graphic
        best, bestf = None, 0.3
        for r, n, _ in pool:
            f = intersection(box, r) / a
            if f > bestf + 1e-9:
                best, bestf = n, f
        if best is None and (box[2] - box[0] < 1 or box[3] - box[1] < 1):
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            for r, n, _ in pool:
                if r[0] <= cx <= r[2] and r[1] <= cy <= r[3]:
                    return n
        return best


NEUTRAL = "\x00neutral"        # a text object that shows nothing
MIXED = "\x00mixed"            # a part of a text object with more than one owner


def owner_key(n: StructNode | None) -> str:
    if n is None:
        return ART
    return ("ART:" + n.id) if n.artifact else n.id


# ------------------------------------------------------------ page tagger
class PageTagger:
    def __init__(self, pdf: pikepdf.Pdf, pno: int, model: DocModel, *,
                 import_mode: bool, unmatched: str = "nearest", join: bool = False) -> None:
        # join: one marked-content sequence for a tag across the text objects
        # (BT … ET) it runs over — a title set line by line, with its soft
        # hyphens in text objects of their own, is one content item, not seven
        self.join = join
        self.pdf = pdf
        self.pno = pno
        self.page = pdf.pages[pno]
        self.model = model
        self.import_mode = import_mode
        self.unmatched_policy = unmatched
        self.res = PageResult()
        nodes = model.targets_on(pno)
        from .compat import house
        # the inner tag winning its glyphs (V4.10) only for a client house style
        self.targets = Targets(nodes, pno, model if house() else None)
        self.key_owner: dict[str, str] = {}
        if import_mode:
            for n in model.nodes.values():
                for k in n.src_keys:
                    self.key_owner[k] = owner_key(n)
        self.box = page_box(self.page.obj)
        self.orig_art: dict[str, Dictionary] = {}
        self.properties = None
        try:
            self.properties = page_resources(self.page.obj).get("/Properties")
        except Exception:
            pass

    # ---- ownership ----------------------------------------------------
    def _glyph_owner(self, cx: float, cy: float, fs: float, key: str | None,
                     orig_art: str | None) -> str:
        x0, y0, x1, y1 = self.box
        if not (x0 - 2 <= cx <= x1 + 2 and y0 - 2 <= cy <= y1 + 2):
            return ART
        pad = max(1.5, 0.25 * fs)
        if self.import_mode:
            hit = self.targets.hit(cx, cy, pad)
            if hit is not None and hit.priority > 0:
                return owner_key(hit)
            if key is not None and key in self.key_owner:
                return self.key_owner[key]
            if orig_art is not None:
                return orig_art
            if hit is not None:
                return owner_key(hit)
        else:
            hit = self.targets.hit(cx, cy, pad)
            if hit is not None:
                return owner_key(hit)
        near = self.targets.nearest(cx, cy, limit=1.5 * fs + 6)
        if near is not None:
            return owner_key(near)
        self.res.unmatched.append((round(cx, 1), round(cy, 1)))
        if self.unmatched_policy == "nearest":
            far = self.targets.nearest(cx, cy, limit=1e9)
            if far is not None:
                self.res.to_nearest += 1
                return owner_key(far)
        return ART

    def _graphic(self, box, key: str | None, orig_art: str | None,
                 any_role: bool = False) -> str:
        if self.import_mode:
            n = self.targets.graphic_owner(box, any_role)
            if n is not None and n.priority > 0:
                return owner_key(n)
            if key is not None and key in self.key_owner:
                return self.key_owner[key]
            if orig_art is not None:
                return orig_art
        n = self.targets.graphic_owner(box, any_role)
        return owner_key(n) if n is not None else ART

    # ---- original marked content --------------------------------------
    def _props(self, operand: Any) -> Dictionary | None:
        if isinstance(operand, Dictionary):
            return operand
        if isinstance(operand, Name) and isinstance(self.properties, Dictionary):
            p = self.properties.get(operand)
            if isinstance(p, Dictionary):
                return p
        return None

    def _classify_mc(self, ins: CSI) -> dict:
        op = str(ins.operator)
        ops = list(ins.operands)
        tag = str(ops[0]) if ops else ""
        entry: dict[str, Any] = {"kind": "strip", "key": None, "art": None}
        if tag == "/OC":
            entry["kind"] = "oc"
            return entry
        props = self._props(ops[1]) if op == "BDC" and len(ops) > 1 else None
        if tag == "/Artifact":
            k = f"ORIG{len(self.orig_art)}"
            d = Dictionary()
            if props is not None:
                for kk in ("/Type", "/Subtype", "/Attached", "/BBox"):
                    if kk in props:
                        d[kk] = props[kk]
            self.orig_art[k] = d
            entry["art"] = k
            return entry
        if props is not None:
            if "/MCID" in props:
                stm = props.get("/PDFUA_Stm")
                mcid = int(num(props.get("/MCID")))
                entry["key"] = (f"s{int(num(stm))}:{mcid}" if stm is not None
                                else f"{self.pno}:{mcid}")
            keep = [k for k in KEEP_PROP_KEYS if k in props]
            if keep:
                d = Dictionary()
                for k in keep:
                    d[k] = props[k]
                entry["kind"] = "inner"
                entry["props"] = d
        return entry

    # ---- text objects shared by two tags --------------------------------
    @staticmethod
    def _restore(state: tuple, chain: list) -> list | None:
        """Operators that put a new text object where the old one was: the
        positioning operators of the old one replayed (Tm, Td), so a viewer
        computes the very same line matrix and the page renders to the pixel.
        Only at the start of a line (text matrix = line matrix); None elsewhere."""
        tm, tlm, fs, th, tr, vertical = state
        if vertical or tr >= 4 or not chain:
            return None
        if any(abs(x - y) > 1e-9 for x, y in zip(tm, tlm)):
            return None
        return list(chain)

    def _plan_splits(self, ins, owners, blanks, mc_info, block_owner, text_state,
                     seg_first: dict, seg_split: dict, leading: dict) -> None:
        i = 0
        n = len(ins)
        while i < n:
            if str(ins[i].operator) != "BT" or i in block_owner:
                i += 1
                continue
            bt = i
            parts: list[list] = []          # [owner, first show op]
            depth = 0                       # kept marked content open (spans, layers)
            ok = True
            chain: list = []                # the positioning operators so far
            chains: dict[int, list] = {}
            j = i + 1
            while j < n and str(ins[j].operator) != "ET":
                op = str(ins[j].operator)
                if op in SHOW_OPS:
                    chains[j] = list(chain)
                # replayed as Tm / Td only, so the leading (TL) is left as it is
                if op == "Tm":
                    chain = [ins[j]]
                elif op == "Td":
                    chain.append(ins[j])
                elif op == "TD":
                    chain.append(CSI(list(ins[j].operands), Operator("Td")))
                elif op in ("T*", "'", '"'):
                    if j not in leading:
                        ok = False
                    else:
                        chain.append(CSI([0, -leading[j]], Operator("Td")))
                if op in ("BDC", "BMC"):
                    e = mc_info[j] or {}
                    if e.get("kind") in ("inner", "oc"):
                        depth += 1
                elif op == "EMC":
                    e = mc_info[j] or {}
                    if e.get("kind") in ("inner", "oc"):
                        depth = max(0, depth - 1)
                elif op in SHOW_OPS and owners[j]:
                    st = text_state.get(j)
                    if st is None or st[4] >= 4 or st[5]:
                        ok = False
                    bl = blanks.get(j)
                    own = {o for k, o in enumerate(owners[j]) if not (bl and bl[k])}
                    if own:
                        o = next(iter(own)) if len(own) == 1 else MIXED
                        if not parts or parts[-1][0] != o or o == MIXED:
                            parts.append([o, j, depth])
                j += 1
            i = j + 1
            if not ok or len(parts) < 2:
                continue
            # split only where nothing kept is open and the place can be restored
            cuts = []
            for k, (o, at, d) in enumerate(parts[1:], 1):
                if d or (o == MIXED and parts[k - 1][0] == MIXED):
                    continue
                r = self._restore(text_state[at], chains.get(at, []))
                if r is None:
                    continue
                cuts.append((at, o, r))
            if not cuts:
                continue
            if parts[0][0] != MIXED:
                seg_first[bt] = parts[0][0]
            for at, o, r in cuts:
                seg_split[at] = (o, r)

    # ---- main ----------------------------------------------------------
    def run(self) -> PageResult:
        try:
            ins = list(pikepdf.parse_content_stream(self.page))
        except Exception as exc:
            self.res.error = f"content stream could not be parsed: {exc}"
            return self.res
        res = page_resources(self.page.obj)
        interp = Interpreter(res, self.box)
        infos = interp.run(ins)

        # ---- pass 1: ownership --------------------------------------------
        n = len(ins)
        owners: list[Any] = [None] * n
        mc_stack: list[dict] = []
        mc_info: list[dict | None] = [None] * n
        span_votes: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
        span_seq = 0
        # join: white space shown on its own is not a content item of its
        # own — it goes with the text it stands next to, or is an artifact
        blanks: dict[int, list[bool]] = {}
        span_ink: set[int] = set()

        def ctx() -> tuple[str | None, str | None, int | None]:
            key = art = None
            inner = None
            for e in reversed(mc_stack):
                if key is None and e.get("key"):
                    key = e["key"]
                if art is None and e.get("art"):
                    art = "ORIGART:" + e["art"]
                if inner is None and e["kind"] == "inner":
                    inner = e["span"]
            return key, art, inner

        for i, instr in enumerate(ins):
            op = str(instr.operator)
            if op in ("BDC", "BMC"):
                e = self._classify_mc(instr)
                if e["kind"] == "inner":
                    e["span"] = span_seq
                    span_seq += 1
                mc_stack.append(e)
                mc_info[i] = e
                continue
            if op == "EMC":
                mc_info[i] = mc_stack.pop() if mc_stack else {"kind": "strip"}
                continue
            info: OpInfo | None = infos[i]
            if info is None:
                continue
            key, art, inner = ctx()
            if info.kind == "text":
                gl = []
                for g in info.glyphs:
                    gl.append(self._glyph_owner(g.cx, g.cy, info.font_size, key, art))
                owners[i] = gl
                if self.join and any(g.blank for g in info.glyphs):
                    blanks[i] = [g.blank for g in info.glyphs]
                self.res.glyphs += len(gl)
                if inner is not None:
                    span_votes[inner].update(gl)
                    if not all(g.blank for g in info.glyphs):
                        span_ink.add(inner)
            elif info.kind == "path":
                if info.clip_only:
                    continue
                owners[i] = self._graphic(info.bbox, key, art)
            elif info.kind in ("image", "inline", "shading"):
                owners[i] = self._graphic(info.bbox, key, art)
            elif info.kind == "form":
                owners[i] = self._graphic(info.bbox, key, art, any_role=True)
                self.res.forms_whole += 1
            if inner is not None and info.kind != "text" and owners[i] is not None:
                span_votes[inner][owners[i]] += 1
                span_ink.add(inner)

        if self.join:
            # a kept span that shows only white space and stands for white
            # space (a tab, an em space, a soft hyphen set as a space) is
            # white space: it joins the text beside it, or is an artifact
            for e in mc_info:
                if e is not None and e.get("kind") == "inner" and e["span"] not in span_ink \
                        and _blank_props(e.get("props")):
                    e["kind"] = "strip"
                    span_votes.pop(e["span"], None)

        span_owner = {sid: c.most_common(1)[0][0] for sid, c in span_votes.items() if c}

        # the one owner of each text object (BT … ET), when it has one
        block_owner: dict[int, str] = {}
        if self.join:
            bt_at = None
            seen: set = set()
            mixed = False
            depth = 0
            for i, instr in enumerate(ins):
                op = str(instr.operator)
                if op == "BT":
                    bt_at, seen, mixed, depth = i, set(), False, 0
                    continue
                if bt_at is None:
                    continue
                if op == "ET":
                    if not mixed and len(seen) <= 1:
                        block_owner[bt_at] = next(iter(seen)) if seen else NEUTRAL
                    bt_at = None
                    continue
                if op in ("BDC", "BMC"):
                    e = mc_info[i] or {}
                    if e.get("kind") == "oc":
                        mixed = True
                    elif e.get("kind") == "inner":
                        if depth == 0 and e.get("span") in span_owner:
                            seen.add(span_owner[e["span"]])
                        depth += 1
                    continue
                if op == "EMC":
                    e = mc_info[i] or {}
                    if e.get("kind") == "inner":
                        depth = max(0, depth - 1)
                    continue
                if op in SHOW_OPS and owners[i] and depth == 0:
                    bl = blanks.get(i)
                    seen.update(o for k, o in enumerate(owners[i]) if not (bl and bl[k]))
                if len(seen) > 1:
                    mixed = True

        # a text object shared by two tags (the last line of a title and the
        # authors after it): split it where the owner changes, so each part
        # joins the sequence of its own tag (one content item for the title)
        seg_first: dict[int, str] = {}       # BT -> owner of its first part
        seg_split: dict[int, tuple] = {}     # show op -> (owner, restore ops)
        if self.join:
            self._plan_splits(ins, owners, blanks, mc_info, block_owner, interp.text_state,
                              seg_first, seg_split, interp.leading)

        # ---- pass 2: emit ---------------------------------------------------
        out: list[CSI] = []
        state: dict[str, Any] = {"cur": None, "outside": False, "in_text": False, "block": None}
        mcids: list[str] = []
        inner_depth = 0
        emc_plan: list[str] = []          # what each original EMC becomes
        path_buf: list[CSI] = []

        def close() -> None:
            if state["cur"] is not None:
                out.append(CSI([], Operator("EMC")))
                state["cur"] = None

        def open_owner(o: str) -> None:
            if o == state["cur"]:
                return
            close()
            if o == ART:
                out.append(CSI([Name.Artifact, Dictionary(Type=Name.Layout)], Operator("BDC")))
            elif o.startswith("ORIGART:"):
                props = self.orig_art.get(o[8:]) or Dictionary()
                if len(props):
                    out.append(CSI([Name.Artifact, props], Operator("BDC")))
                else:
                    out.append(CSI([Name.Artifact], Operator("BMC")))
            elif o.startswith("ART:"):
                node = self.model.nodes.get(o[4:])
                d = Dictionary(Type=Name("/" + (node.artifact_subtype if node else "Layout")))
                if node and node.pagination_subtype:
                    d.Subtype = Name("/" + node.pagination_subtype)
                out.append(CSI([Name.Artifact, d], Operator("BDC")))
            else:
                node = self.model.nodes[o]
                mcid = len(mcids)
                mcids.append(o)
                tag = node.role if node.role.isalnum() else "Span"
                out.append(CSI([Name("/" + tag), Dictionary(MCID=mcid)], Operator("BDC")))
            state["cur"] = o
            state["outside"] = not state["in_text"]

        def switch(o: str | None) -> None:
            if o is None or inner_depth > 0:
                return
            open_owner(o)

        for i, instr in enumerate(ins):
            op = str(instr.operator)

            if op in ("BDC", "BMC"):
                e = mc_info[i] or {"kind": "strip"}
                if e["kind"] == "oc":
                    if inner_depth == 0:
                        close()
                    out.append(instr)
                    emc_plan.append("keep")
                    self.res.kept_oc += 1
                elif e["kind"] == "inner":
                    if inner_depth == 0:
                        o = span_owner.get(e["span"])
                        if o is not None:
                            open_owner(o)
                    out.append(CSI([Name.Span, e["props"]], Operator("BDC")))
                    emc_plan.append("inner")
                    inner_depth += 1
                    self.res.kept_spans += 1
                else:
                    emc_plan.append("drop")
                    self.res.stripped += 1
                continue
            if op == "EMC":
                plan = emc_plan.pop() if emc_plan else "drop"
                if plan == "keep":
                    if inner_depth == 0:
                        close()
                    out.append(instr)
                elif plan == "inner":
                    out.append(instr)
                    inner_depth = max(0, inner_depth - 1)
                continue

            if op in PATH_OPS or op in CLIP_OPS:
                path_buf.append(instr)
                continue
            if op in PAINT_OPS:
                o = owners[i]
                if o is not None:
                    switch(o)
                elif state["cur"] is None and inner_depth == 0:
                    open_owner(ART)          # clipping paths stay inside marked content
                out.extend(path_buf)
                out.append(instr)
                path_buf = []
                continue
            if path_buf:
                out.extend(path_buf)
                path_buf = []

            if op in SHOW_OPS and i in seg_split and state["in_text"] and inner_depth == 0:
                # end the text object here and start a new one for the next tag
                o, restore = seg_split[i]
                if not state["outside"]:
                    close()
                out.append(CSI([], Operator("ET")))
                state["in_text"] = False
                if o not in (MIXED, NEUTRAL):
                    if not (state["cur"] == o and state["outside"]):
                        close()
                        open_owner(o)
                else:
                    close()
                out.append(CSI([], Operator("BT")))
                out.extend(restore)
                state["in_text"], state["block"] = True, (o if o not in (MIXED, NEUTRAL) else None)
                self.res.text_splits += 1
                # the show operator itself is emitted below
            if op == "BT" and self.join and inner_depth == 0 and (i in block_owner or i in seg_first):
                # a text object with one owner: the sequence opens (or goes on)
                # outside it, so it may run over the next text object too
                o = block_owner.get(i, seg_first.get(i))
                if o != NEUTRAL and not (state["cur"] == o and state["outside"]):
                    close()
                    open_owner(o)
                elif o == NEUTRAL and state["cur"] is not None and not state["outside"]:
                    close()
                state["in_text"], state["block"] = True, o
                out.append(instr)
                continue
            if op == "ET" and self.join and inner_depth == 0 and state["block"] is not None:
                if not state["outside"]:
                    close()
                state["in_text"], state["block"] = False, None
                out.append(instr)
                continue
            if op in ("q", "Q", "BT", "ET"):
                if inner_depth == 0:
                    close()
                if op == "BT":
                    state["in_text"] = True
                elif op == "ET":
                    state["in_text"] = False
                out.append(instr)
                continue

            if op in SHOW_OPS:
                gl = owners[i] or []
                if not gl:
                    out.append(instr)
                    continue
                bl = blanks.get(i)
                if bl and inner_depth == 0:
                    if all(bl):
                        if state["cur"] is None:
                            open_owner(ART)
                        out.append(instr)
                        continue
                    gl = _fill_blanks(gl, bl, state["cur"])
                if inner_depth > 0 or len(set(gl)) == 1:
                    switch(gl[0])
                    out.append(instr)
                    continue
                self.res.split_ops += 1
                for o, seg in _split_show(instr, op, gl, infos[i]):
                    if o is None:
                        out.extend(seg)
                    else:
                        switch(o)
                        out.extend(seg)
                continue

            if op == "Do" or op in INLINE_IMAGE_OPS or op == "sh":
                o = owners[i]
                if o is not None:
                    switch(o)
                out.append(instr)
                continue

            out.append(instr)

        if path_buf:
            out.extend(path_buf)
        close()
        while emc_plan:                      # unbalanced source: close what we kept
            plan = emc_plan.pop()
            if plan in ("keep", "inner"):
                out.append(CSI([], Operator("EMC")))

        self.page.obj.Contents = self.pdf.make_stream(pikepdf.unparse_content_stream(out))
        self.res.mcid_owner = mcids
        return self.res


def _blank_props(props: Any) -> bool:
    """Span properties that only say "this is white space"."""
    if not isinstance(props, Dictionary):
        return False
    if any(k in props for k in ("/Alt", "/E")) or "/ActualText" not in props:
        return False
    try:
        t = str(props.get("/ActualText"))
    except Exception:
        return False
    return all(ch.isspace() or unicodedata.category(ch) in ("Zs", "Cc", "Cf") or ch == "\xad"
               for ch in t)


def _fill_blanks(gl: list[str], blank: list[bool], cur: str | None) -> list[str]:
    """White-space glyphs take the owner of the glyph before them (or, at
    the start, of what is open already, else of the glyph after them)."""
    out = list(gl)
    prev = None
    for k, b in enumerate(blank):
        if not b:
            prev = gl[k]
        elif prev is not None:
            out[k] = prev
    first = next((gl[k] for k, b in enumerate(blank) if not b), None)
    lead = cur if cur is not None else first
    for k, b in enumerate(blank):
        if not b:
            break
        if lead is not None:
            out[k] = lead
    return out


def _split_show(instr: CSI, op: str, owners: list[str], info: OpInfo):
    """Split a text-showing operator into per-owner pieces.

    Yields (owner | None, [instructions]). owner None = positioning ops that
    must be emitted before the text (from ' and ").
    """
    ops = list(instr.operands)
    pre: list[CSI] = []
    if op == "'":
        pre.append(CSI([], Operator("T*")))
        elems = [ops[0]]
    elif op == '"':
        pre.append(CSI([ops[0]], Operator("Tw")))
        pre.append(CSI([ops[1]], Operator("Tc")))
        pre.append(CSI([], Operator("T*")))
        elems = [ops[2]]
    elif op == "Tj":
        elems = [ops[0]]
    else:
        elems = list(ops[0])
    if pre:
        yield None, pre

    # atoms: ("g", elem, b0, b1, owner) | ("n", value, owner)
    atoms: list[tuple] = []
    gi = 0
    glyphs = info.glyphs
    for ei, el in enumerate(elems):
        if isinstance(el, pikepdf.String):
            data = bytes(el)
            while gi < len(glyphs) and glyphs[gi].elem == (ei if op == "TJ" else 0):
                g = glyphs[gi]
                atoms.append(("g", data[g.b0:g.b1], owners[gi]))
                gi += 1
                if op != "TJ" and gi >= len(glyphs):
                    break
        else:
            prev = atoms[-1][2] if atoms else None
            atoms.append(("n", el, prev))
    # numbers at the very start take the first glyph's owner
    first = next((a[2] for a in atoms if a[0] == "g"), owners[0])
    atoms = [(a[0], a[1], a[2] if a[2] is not None else first) for a in atoms]

    segs: list[tuple[str, list]] = []
    for kind, val, o in atoms:
        if not segs or segs[-1][0] != o:
            segs.append((o, []))
        items = segs[-1][1]
        if kind == "g":
            if items and isinstance(items[-1], (bytes, bytearray)):
                items[-1] = bytes(items[-1]) + val
            else:
                items.append(bytes(val))
        else:
            items.append(val)

    for o, items in segs:
        if op == "TJ":
            arr = Array([String(x) if isinstance(x, (bytes, bytearray)) else x for x in items])
            yield o, [CSI([arr], Operator("TJ"))]
        else:
            data = b"".join(x for x in items if isinstance(x, (bytes, bytearray)))
            yield o, [CSI([String(data)], Operator("Tj"))]


def strip_form_marked_content(pdf: pikepdf.Pdf, xobj: pikepdf.Stream,
                              seen: set | None = None) -> int:
    """Remove MCID-bearing marked content from a form XObject (retag mode)."""
    seen = set() if seen is None else seen
    try:
        if xobj.objgen in seen:
            return 0
        seen.add(xobj.objgen)
    except Exception:
        pass
    try:
        ins = list(pikepdf.parse_content_stream(xobj))
    except Exception:
        return 0
    out = []
    stack = []
    removed = 0
    for i in ins:
        op = str(i.operator)
        if op in ("BDC", "BMC"):
            tag = str(i.operands[0]) if i.operands else ""
            keep = tag == "/OC"
            if op == "BDC" and not keep and len(i.operands) > 1 and isinstance(i.operands[1], Dictionary):
                keep = not ("/MCID" in i.operands[1]) and tag != "/Artifact"
            stack.append(keep)
            if keep:
                out.append(i)
            else:
                removed += 1
            continue
        if op == "EMC":
            keep = stack.pop() if stack else False
            if keep:
                out.append(i)
            continue
        out.append(i)
    if removed:
        xobj.write(pikepdf.unparse_content_stream(out))
        if "/StructParents" in xobj:
            del xobj["/StructParents"]
        if "/StructParent" in xobj:
            del xobj["/StructParent"]
    res = xobj.get("/Resources")
    xo = res.get("/XObject") if isinstance(res, Dictionary) else None
    if isinstance(xo, Dictionary):
        for _, child in xo.items():
            if isinstance(child, pikepdf.Stream) and child.get("/Subtype") == Name.Form:
                removed += strip_form_marked_content(pdf, child, seen)
    return removed
