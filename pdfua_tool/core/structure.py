"""Write the logical structure tree (StructTreeRoot) for a tagged PDF."""
from __future__ import annotations

from typing import Any

import pikepdf
from pikepdf import Array, Dictionary, Name, String

from .compat import house
from .model import STANDARD_ROLES, DocModel, StructNode, clean_text

SKIP_ANNOTS = {"/Popup", "/PrinterMark", "/TrapNet"}
# tags whose /ActualText is NOT moved to a Span inside them: a Figure or
# Formula is described by it, a Span is one already, and a tag whose children
# have to stay where they are (a list item's Lbl/LBody, a table's rows, a
# contents entry's Reference) may not hold a Span in their place
NO_SPAN = {"Span", "Figure", "Formula", "Document", "Part", "Art", "Sect", "Div",
           "TOC", "TOCI", "Index", "L", "LI", "Table", "TR", "THead", "TBody", "TFoot",
           "Ruby", "Warichu", "RB", "RT", "RP", "WT", "WP"}
HIDDEN_FLAG = 2


def _annot_is_hidden(a: Dictionary) -> bool:
    try:
        return bool(int(a.get("/F", 0)) & HIDDEN_FLAG)
    except Exception:
        return False


def annotation_index(pdf: pikepdf.Pdf) -> dict[int, tuple[int, Dictionary]]:
    """objnum -> (page index, annotation dict) for every indirect annotation."""
    out: dict[int, tuple[int, Dictionary]] = {}
    for pno, page in enumerate(pdf.pages):
        annots = page.obj.get("/Annots")
        if not isinstance(annots, Array):
            continue
        for a in annots:
            try:
                if a.is_indirect:
                    out[a.objgen[0]] = (pno, a)
            except Exception:
                continue
    return out


# Figure, Formula and Note are inline-level structure types by default
# (ISO 32000-1 Table 333). One of them reads fine without a Placement
# attribute only while it sits inside running text — nested in a P, a
# heading, a caption, a list item, a table cell, or inside another
# inline element such as a Note, Reference, Link or Span. Anywhere else
# (directly under Sect/Div/Document/Art/Part/BlockQuote/Table/TR/…) it
# is standing on its own as a block — the masthead logo, a content
# figure, every affiliation/table Note in this house style — and must
# say /Placement /Block, or PAC reports "Possibly inappropriate use of
# a Figure/Note structure element" because assistive tech can no longer
# tell inline from block (support.axes4.com/hc/en-us/articles/
# 7371524021266 for Note, .../7371484607634 for Figure).
INLINE_TEXT_HOSTS = {"P", "H", "H1", "H2", "H3", "H4", "H5", "H6", "Caption",
                     "Lbl", "LBody", "TD", "TH", "Note", "Reference", "Link",
                     "Span", "Quote", "BlockQuote", "Code"}


def _is_block_context(parent_role: str | None) -> bool:
    """True when a Figure/Formula/Note nested under parent_role is not part
    of a line of running text, so it needs Placement: Block."""
    return parent_role not in INLINE_TEXT_HOSTS


def _attr_dicts(pdf: pikepdf.Pdf, node: StructNode, ids: dict[str, str],
                figure_bbox: bool, parent_role: str | None = None) -> list[Dictionary]:
    attrs: list[Dictionary] = []
    if node.role in ("TH", "TD"):
        d = Dictionary(O=Name.Table)
        if node.role == "TH" and node.scope in ("Row", "Column", "Both"):
            d.Scope = Name("/" + node.scope)
        if node.rowspan and node.rowspan > 1:
            d.RowSpan = int(node.rowspan)
        if node.colspan and node.colspan > 1:
            d.ColSpan = int(node.colspan)
        hdrs = [ids[h] for h in node.headers if h in ids]
        if hdrs:
            d.Headers = Array([String(h) for h in hdrs])
        if len(d) > 1:
            attrs.append(d)
    if node.role == "Table" and (node.style or {}).get("summary"):
        # a summary written for the table (Edit table, V4.21) — only then
        attrs.append(Dictionary(O=Name.Table, Summary=String(str(node.style["summary"])[:600])))
    if node.role == "L" and node.list_numbering:
        attrs.append(Dictionary(O=Name.List, ListNumbering=Name("/" + node.list_numbering)))
    # Kriyadocs only (compat.house()): the V4/Academain engine never set a
    # Note attribute dict at all, and always said Placement: Block for every
    # Figure/Formula — that stays exactly as it was for every other client.
    if node.role == "Note" and house() and _is_block_context(parent_role):
        attrs.append(Dictionary(O=Name.Layout, Placement=Name.Block))
    if figure_bbox and node.role in ("Figure", "Formula", "Table") and node.bbox:
        x0, y0, x1, y1 = node.bbox
        d = Dictionary(O=Name.Layout, BBox=Array([round(x0, 2), round(y0, 2),
                                                   round(x1, 2), round(y1, 2)]))
        if node.role == "Table" or not house() or _is_block_context(parent_role):
            d.Placement = Name.Block
        attrs.append(d)
    return attrs


def write_structure(pdf: pikepdf.Pdf, model: DocModel,
                    mcid_owner: list[list[str]], *, figure_bbox: bool = True,
                    empty_cell_text: str = "", actualtext_span: bool = False) -> dict:
    stats: dict[str, Any] = {"elements": 0, "pruned": [], "annots_tagged": 0, "empty_cells": 0,
                             "annots_fallback": 0, "orphan_mcids": 0, "ids": 0}

    # -------- content per node
    content: dict[str, list[tuple[int, int]]] = {}
    for pno, owners in enumerate(mcid_owner):
        for mcid, nid in enumerate(owners):
            content.setdefault(nid, []).append((pno, mcid))
    for n in model.nodes.values():
        n.mcids = {}
    for nid, items in content.items():
        n = model.nodes.get(nid)
        if n is None:
            continue
        for pno, m in items:
            n.mcids.setdefault(pno, []).append(m)

    annots = annotation_index(pdf)
    used_annots: set[int] = set()

    # -------- IDs (TH referenced by Headers, Note, explicit id_attr)
    need_ids = {h for n in model.nodes.values() for h in n.headers}
    ids: dict[str, str] = {}
    taken: set[str] = set()
    for n in model.ordered_nodes():
        if n.artifact:
            continue
        if n.id in need_ids or n.role == "Note" or n.id_attr:
            base = n.id_attr or (("note-" if n.role == "Note" else "th-") + n.id)
            ident, k = base, 2
            while ident in taken:
                ident, k = f"{base}-{k}", k + 1
            taken.add(ident)
            ids[n.id] = ident
            n.id_attr = ident

    # -------- remove previous structure
    for key in ("/StructTreeRoot",):
        if key in pdf.Root:
            del pdf.Root[key]
    for page in pdf.pages:
        if "/StructParents" in page.obj:
            del page.obj["/StructParents"]
        for a in (page.obj.get("/Annots") or []):
            if isinstance(a, Dictionary) and "/StructParent" in a:
                del a["/StructParent"]

    root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
    refs: dict[str, Any] = {}
    # the element that holds a node's own marked content. It is the node's own
    # element, except where its /ActualText sits on a Span inside it
    # (actualtext_span, the Academain client) and the Span holds the content
    mc_holder: dict[str, Any] = {}
    parent_tree: dict[int, Any] = {}
    next_key = len(pdf.pages)
    elem_first: dict[str, tuple[int, int]] = {}
    INF = (10 ** 9, 10 ** 9)

    def build(node: StructNode, parent_ref) -> Any | None:
        nonlocal next_key
        if node.artifact:
            return None
        role = node.role if node.role in STANDARD_ROLES else ("Div" if node.children else "Span")
        d = Dictionary(Type=Name.StructElem, S=Name("/" + role), P=parent_ref)
        elem = pdf.make_indirect(d)

        kid_elems: list[tuple[Any, tuple[int, int]]] = []
        for cid in node.children:
            c = model.nodes.get(cid)
            if c is None:
                continue
            cref = build(c, elem)
            if cref is not None:
                kid_elems.append((cref, elem_first.get(cid, INF)))

        own = sorted((p, m) for p, ms in node.mcids.items() for m in ms)
        objrs = []
        for objnum in node.annots:
            if objnum in annots and objnum not in used_annots:
                pno, a = annots[objnum]
                if a.get("/Subtype") in SKIP_ANNOTS:
                    continue
                used_annots.add(objnum)
                objrs.append((pno, a))

        empty_cell = role in ("TD", "TH") and not own and not kid_elems and not objrs
        if not own and not kid_elems and not objrs and not empty_cell:
            stats["pruned"].append({"id": node.id, "role": node.role,
                                    "page": node.page, "text": (node.alt or node.text)[:80]})
            return None
        if empty_cell or (role in ("TD", "TH") and node.style.get("blank_text")):
            # empty cells stay (table regularity) and are announced, e.g. "Blank"
            stats["empty_cells"] += 1
            if empty_cell_text and not clean_text(node.actual_text) and not clean_text(node.alt):
                node.actual_text = empty_cell_text

        pages_of_own = {p for p, _ in own}
        pg = own[0][0] if own else (objrs[0][0] if objrs else node.page)
        if pg is not None and pg >= 0:
            d.Pg = pdf.pages[pg].obj

        def mc_item(p: int, m: int):
            if len(pages_of_own) == 1 and p == pg:
                return m
            return Dictionary(Type=Name.MCR, Pg=pdf.pages[p].obj, MCID=m)

        kids = Array([])
        objr_keys: list[int] = []
        oi = 0
        for cref, first in kid_elems:
            while oi < len(own) and own[oi] < first:
                kids.append(mc_item(*own[oi]))
                oi += 1
            kids.append(cref)
        while oi < len(own):
            kids.append(mc_item(*own[oi]))
            oi += 1
        for pno, a in objrs:
            kids.append(Dictionary(Type=Name.OBJR, Obj=a, Pg=pdf.pages[pno].obj))
            a.StructParent = next_key
            parent_tree[next_key] = elem        # fixed up below if a Span holds it
            objr_keys.append(next_key)
            next_key += 1
            stats["annots_tagged"] += 1
        # Academain: the /ActualText is not set on the tag itself but on a Span
        # made inside it, and the Span holds the tag's content (P > Span). A
        # Figure / Formula keeps it on the tag — it is the tag's description
        # there, and a Figure with no /Alt or /ActualText fails PAC. A tag with
        # no content (an empty cell) has nothing to wrap, so it keeps it too.
        holder = elem
        span = None
        if (actualtext_span and clean_text(node.actual_text)
                and role not in NO_SPAN
                and (own or kid_elems or objrs)):
            span = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Span, P=elem))
            if pg is not None and pg >= 0:
                span.Pg = pdf.pages[pg].obj
            for cref, _first in kid_elems:
                cref.P = span                    # the inner tags now sit under the Span
            holder = span
            mc_holder[node.id] = span
            for k in objr_keys:                  # a link annotation now sits in the Span
                parent_tree[k] = span
            stats["actualtext_spans"] = stats.get("actualtext_spans", 0) + 1

        if len(kids):
            holder.K = kids if len(kids) != 1 or isinstance(kids[0], Dictionary) else kids[0]
        if span is not None:
            span.ActualText = String(clean_text(node.actual_text))
            d.K = span

        if clean_text(node.alt):
            d.Alt = String(clean_text(node.alt))
        if clean_text(node.actual_text) and span is None:
            d.ActualText = String(clean_text(node.actual_text))
        if clean_text(node.expansion):
            d.E = String(clean_text(node.expansion))
        if node.lang:
            d.Lang = String(node.lang)
        if node.id in ids:
            d.ID = String(ids[node.id])
        parent_node = model.nodes.get(node.parent) if node.parent else None
        attrs = _attr_dicts(pdf, node, ids, figure_bbox, parent_node.role if parent_node else None)
        if len(attrs) == 1:
            d.A = attrs[0]
        elif attrs:
            d.A = Array(attrs)

        firsts = [own[0]] if own else []
        firsts += [f for _, f in kid_elems if f != INF]
        if objrs:
            firsts.append((objrs[0][0], INF[1] - 1))
        elem_first[node.id] = min(firsts) if firsts else INF
        refs[node.id] = elem
        stats["elements"] += 1
        return elem

    # -------- top level
    roots = [model.nodes[r] for r in model.root_children if r in model.nodes]
    if len([r for r in roots if not r.artifact]) == 1 and roots and \
            [r for r in roots if not r.artifact][0].role == "Document":
        doc_node = [r for r in roots if not r.artifact][0]
        document = build(doc_node, root)
        if document is None:
            document = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Document,
                                                    P=root, K=Array([])))
    else:
        document = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Document,
                                                P=root))
        dk = Array([])
        for r in roots:
            ref = build(r, document)
            if ref is not None:
                dk.append(ref)
        document.K = dk
    root.K = document

    # -------- annotations nobody claimed
    top = document.get("/K")
    if not isinstance(top, Array):
        top = Array([top]) if top is not None else Array([])
        document.K = top
    for objnum, (pno, a) in sorted(annots.items(), key=lambda t: (t[1][0], t[0])):
        if objnum in used_annots:
            continue
        sub = a.get("/Subtype")
        if sub in SKIP_ANNOTS or _annot_is_hidden(a):
            continue
        role = {"/Link": "Link", "/Widget": "Form"}.get(str(sub), "Annot")
        el = pdf.make_indirect(Dictionary(
            Type=Name.StructElem, S=Name("/" + role), P=document,
            Pg=pdf.pages[pno].obj,
            K=Array([Dictionary(Type=Name.OBJR, Obj=a, Pg=pdf.pages[pno].obj)])))
        a.StructParent = next_key
        parent_tree[next_key] = el
        next_key += 1
        # place after the last top-level element that starts on or before pno
        pos = len(top)
        for k in range(len(top)):
            kid = top[k]
            pg = kid.get("/Pg") if isinstance(kid, Dictionary) else None
            try:
                kp = [p.objgen for p in pdf.pages].index(pg.objgen) if pg is not None else -1
            except Exception:
                kp = -1
            if kp > pno:
                pos = k
                break
        kids_now = list(top)
        kids_now.insert(pos, el)
        top = Array(kids_now)
        document.K = top
        stats["annots_fallback"] += 1

    # -------- parent tree
    doc_ref = document
    for pno, owners in enumerate(mcid_owner):
        if not owners:
            continue
        arr = Array([])
        for nid in owners:
            ref = mc_holder.get(nid) or refs.get(nid)
            if ref is None:
                stats["orphan_mcids"] += 1
                ref = doc_ref
            arr.append(ref)
        pdf.pages[pno].obj.StructParents = pno
        parent_tree[pno] = pdf.make_indirect(arr)
    nums = Array([])
    for k in sorted(parent_tree):
        nums.append(k)
        nums.append(parent_tree[k])
    root.ParentTree = pdf.make_indirect(Dictionary(Nums=nums))
    root.ParentTreeNextKey = next_key

    # -------- ID tree
    if ids:
        names = Array([])
        for ident, nid in sorted(((v, k) for k, v in ids.items() if k in refs),
                                 key=lambda t: t[0].encode("utf-8")):
            names.append(String(ident))
            names.append(refs[nid])
        root.IDTree = pdf.make_indirect(Dictionary(Names=names))
        stats["ids"] = len(names) // 2

    pdf.Root.StructTreeRoot = root
    mi = pdf.Root.get("/MarkInfo")
    if not isinstance(mi, Dictionary):
        pdf.Root.MarkInfo = Dictionary(Marked=True)
    else:
        mi.Marked = True
        if "/Suspects" in mi:
            mi.Suspects = False
    return stats
