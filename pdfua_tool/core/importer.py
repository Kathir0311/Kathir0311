"""Import an existing structure tree so it can be reviewed and edited.

Original MCID membership is kept (StructNode.src_keys); on rebuild the
tagger re-assigns exactly the same content to the same element unless the
user changed it. Geometry and text per element are computed so the editor
can draw boxes and show text.
"""
from __future__ import annotations

import collections
import math
import re
from typing import Any, Callable

import pikepdf
import pymupdf
from pikepdf import Array, Dictionary, Name

from .model import STANDARD_ROLES, DocModel, StructNode, clean_text, position_index, union
from .pdftext import SHOW_OPS, Interpreter, num, page_box, page_resources
from .textflags import RAW_FLAGS

KEY_PROPS = ("/ActualText", "/Alt", "/E", "/Lang")


def _s(v: Any) -> str:
    try:
        return str(v) if v is not None else ""
    except Exception:
        return ""


def resolve_role(s: str, rolemap: Any) -> tuple[str, str]:
    """-> (standard role, original role)"""
    orig = s.lstrip("/")
    cur = orig
    for _ in range(12):
        if cur in STANDARD_ROLES:
            return cur, orig
        if isinstance(rolemap, Dictionary) and ("/" + cur) in rolemap:
            cur = _s(rolemap["/" + cur]).lstrip("/")
        else:
            break
    return "", orig


# ------------------------------------------------------------ assessment
def content_coverage(pdf: pikepdf.Pdf, max_pages: int = 60) -> dict:
    total = tagged = art = 0
    for pno, page in enumerate(pdf.pages):
        if pno >= max_pages:
            break
        try:
            ins = pikepdf.parse_content_stream(page)
        except Exception:
            continue
        stack: list[str] = []
        for i in ins:
            op = str(i.operator)
            if op in ("BDC", "BMC"):
                tag = _s(i.operands[0]) if i.operands else ""
                kind = "other"
                if tag == "/Artifact":
                    kind = "art"
                elif op == "BDC" and len(i.operands) > 1 and isinstance(i.operands[1], Dictionary) \
                        and "/MCID" in i.operands[1]:
                    kind = "mcid"
                stack.append(kind)
            elif op == "EMC":
                if stack:
                    stack.pop()
            elif op in SHOW_OPS:
                total += 1
                if "art" in stack:
                    art += 1
                elif "mcid" in stack:
                    tagged += 1
    covered = tagged + art
    return {"show_ops": total, "tagged": tagged, "artifact": art,
            "coverage": (covered / total) if total else 1.0}


def assess_tags(path: str) -> dict:
    pdf = pikepdf.open(path)
    out: dict = {"tagged": False, "elements": 0, "roles": {}, "verdict": "retag", "reasons": []}
    root = pdf.Root.get("/StructTreeRoot")
    if not isinstance(root, Dictionary):
        out["reasons"].append("no structure tree")
        pdf.close()
        return out
    out["tagged"] = True
    rolemap = root.get("/RoleMap")
    roles: collections.Counter = collections.Counter()
    stack = [root.get("/K")]
    seen = set()
    while stack:
        k = stack.pop()
        if isinstance(k, Array):
            stack.extend(list(k))
            continue
        if not isinstance(k, Dictionary):
            continue
        try:
            if k.objgen in seen:
                continue
            seen.add(k.objgen)
        except Exception:
            pass
        if "/S" in k:
            std, orig = resolve_role(_s(k.S), rolemap)
            roles[std or orig] += 1
            stack.append(k.get("/K"))
    out["elements"] = sum(roles.values())
    out["roles"] = dict(roles.most_common())
    cov = content_coverage(pdf)
    out["coverage"] = round(cov["coverage"], 3)
    pages = len(pdf.pages)
    pdf.close()
    structural = {"H1", "H2", "H3", "H4", "H5", "H6", "H", "L", "Table", "Figure", "TOC"}
    if out["elements"] < 3:
        out["reasons"].append("almost empty structure tree")
    elif cov["coverage"] < 0.9:
        out["reasons"].append(f"only {cov['coverage']:.0%} of text is tagged or marked as artifact")
    elif pages >= 2 and not (structural & set(roles)):
        out["reasons"].append("no headings, lists, tables or figures — tags look auto-generated")
    else:
        out["verdict"] = "import"
        out["reasons"].append(f"{out['elements']} elements, {cov['coverage']:.0%} content coverage")
    return out


# ---------------------------------------------------------------- import
class Importer:
    def __init__(self, path: str, progress: Callable | None = None) -> None:
        self.path = path
        self.pdf = pikepdf.open(path)
        self.doc = pymupdf.open(path)
        self.progress = progress or (lambda f, m: None)
        self.model = DocModel(path=path, page_count=len(self.pdf.pages))
        self.model.mode = "import"
        self.page_index = {p.objgen: i for i, p in enumerate(self.pdf.pages)}
        root = self.pdf.Root.get("/StructTreeRoot")
        self.root = root if isinstance(root, Dictionary) else Dictionary()
        self.rolemap = self.root.get("/RoleMap")
        self.id_to_node: dict[str, str] = {}
        self.pending_headers: list[tuple[StructNode, list[str]]] = []
        self.seen: set = set()

    def _page_of(self, obj) -> int:
        try:
            return self.page_index.get(obj.objgen, -1) if obj is not None else -1
        except Exception:
            return -1

    def _attrs(self, node: StructNode, elem: Dictionary) -> None:
        a = elem.get("/A")
        dicts = [a] if isinstance(a, Dictionary) else ([x for x in a if isinstance(x, Dictionary)]
                                                      if isinstance(a, Array) else [])
        for d in dicts:
            if "/Scope" in d:
                node.scope = _s(d.Scope).lstrip("/")
            if "/RowSpan" in d:
                node.rowspan = int(num(d.RowSpan, 1)) or 1
            if "/ColSpan" in d:
                node.colspan = int(num(d.ColSpan, 1)) or 1
            if "/ListNumbering" in d:
                node.list_numbering = _s(d.ListNumbering).lstrip("/")
            if "/Headers" in d and isinstance(d.Headers, Array):
                self.pending_headers.append((node, [_s(h) for h in d.Headers]))

    def _walk(self, elem: Dictionary, parent: str | None, inherited_pg: int) -> None:
        try:
            if elem.objgen in self.seen:
                return
            self.seen.add(elem.objgen)
        except Exception:
            pass
        m = self.model
        std, orig = resolve_role(_s(elem.get("/S")), self.rolemap)
        pg = self._page_of(elem.get("/Pg"))
        if pg < 0:
            pg = inherited_pg
        kids = elem.get("/K")
        kids = list(kids) if isinstance(kids, Array) else ([kids] if kids is not None else [])
        has_struct_kids = any(isinstance(k, Dictionary) and "/S" in k for k in kids)
        role = std or ("Div" if has_struct_kids else "Span")
        node = StructNode(id=m.new_id("i"), role=role, page=pg, source="import",
                          orig_role=orig if orig != role else "", confidence=1.0)
        if not std:
            node.note = f"non-standard role /{orig} mapped to {role}"
        node.alt = clean_text(_s(elem.get("/Alt")))
        node.actual_text = clean_text(_s(elem.get("/ActualText")))
        node.expansion = clean_text(_s(elem.get("/E")))
        node.lang = clean_text(_s(elem.get("/Lang")))
        if "/ID" in elem:
            node.id_attr = _s(elem.ID)
            self.id_to_node[node.id_attr] = node.id
        self._attrs(node, elem)
        m.add(node, parent)
        for k in kids:
            if isinstance(k, Dictionary) and "/S" in k:
                self._walk(k, node.id, pg)
            elif isinstance(k, Dictionary) and k.get("/Type") == Name.MCR:
                kp = self._page_of(k.get("/Pg"))
                kp = pg if kp < 0 else kp
                stm = k.get("/Stm")
                mcid = int(num(k.get("/MCID"), -1))
                if mcid < 0:
                    continue
                if isinstance(stm, (pikepdf.Stream, Dictionary)) and stm.is_indirect:
                    node.src_keys.append(f"s{stm.objgen[0]}:{mcid}")
                else:
                    node.src_keys.append(f"{kp}:{mcid}")
            elif isinstance(k, Dictionary) and k.get("/Type") == Name.OBJR:
                obj = k.get("/Obj")
                try:
                    if obj is not None and obj.is_indirect:
                        node.annots.append(obj.objgen[0])
                except Exception:
                    pass
            elif not isinstance(k, Dictionary):
                try:
                    node.src_keys.append(f"{pg}:{int(k)}")
                except Exception:
                    pass

    # -------------------------------------------------------- geometry
    def _geometry(self) -> tuple[dict, dict, dict]:
        key_box: dict[str, tuple] = {}
        key_pages: dict[str, int] = {}
        key_text: dict[str, list[str]] = collections.defaultdict(list)
        self.orig_artifacts: list[dict] = []
        self.untagged: dict[int, int] = {}
        n = len(self.pdf.pages)
        for pno, page in enumerate(self.pdf.pages):
            self.progress(0.2 + 0.6 * pno / max(1, n), f"reading tags on page {pno + 1}")
            try:
                ins = list(pikepdf.parse_content_stream(page))
            except Exception:
                continue
            box = page_box(page.obj)
            infos = Interpreter(page_resources(page.obj), box).run(ins)
            stack: list[dict] = []
            centers: list[tuple[float, float, str]] = []
            cur_art: dict | None = None
            loose = 0
            for i, instr in enumerate(ins):
                op = str(instr.operator)
                if op in ("BDC", "BMC"):
                    ops = list(instr.operands)
                    tag = _s(ops[0]) if ops else ""
                    e: dict = {"key": None, "art": False}
                    props = ops[1] if op == "BDC" and len(ops) > 1 and isinstance(ops[1], Dictionary) else None
                    if tag == "/Artifact":
                        e["art"] = True
                        e["props"] = props
                        cur_art = {"page": pno, "box": None, "text": [],
                                   "type": _s(props.get("/Type")).lstrip("/") if props is not None else "",
                                   "sub": _s(props.get("/Subtype")).lstrip("/") if props is not None else ""}
                        e["collect"] = cur_art
                    elif props is not None and "/MCID" in props:
                        stm = props.get("/PDFUA_Stm")
                        mcid = int(num(props.MCID))
                        e["key"] = f"s{int(num(stm))}:{mcid}" if stm is not None else f"{pno}:{mcid}"
                    stack.append(e)
                    continue
                if op == "EMC":
                    if stack:
                        e = stack.pop()
                        if e.get("collect") and e["collect"]["box"] is not None:
                            self.orig_artifacts.append(e["collect"])
                    continue
                info = infos[i]
                if info is None:
                    continue
                key = next((e["key"] for e in reversed(stack) if e["key"]), None)
                art = next((e for e in reversed(stack) if e["art"]), None)
                if info.kind == "text":
                    boxes = [g.box for g in info.glyphs]
                    if not boxes:
                        continue
                    b = (min(x[0] for x in boxes), min(x[1] for x in boxes),
                         max(x[2] for x in boxes), max(x[3] for x in boxes))
                    if key:
                        key_box[key] = union(key_box.get(key), b)
                        key_pages.setdefault(key, pno)
                        for g in info.glyphs:
                            centers.append((g.cx, g.cy, key))
                    elif art is not None and art.get("collect"):
                        c = art["collect"]
                        c["box"] = union(c["box"], b)
                        for g in info.glyphs:
                            centers.append((g.cx, g.cy, "ART#%d" % id(c)))
                    else:
                        loose += 1
                elif info.kind in ("image", "form", "inline", "path") and info.bbox and key:
                    if info.kind == "path" and info.clip_only:
                        continue
                    key_box[key] = union(key_box.get(key), info.bbox)
                    key_pages.setdefault(key, pno)
            if loose:
                self.untagged[pno] = loose
            # text per key, character by character: each printed character
            # goes to the marked content whose glyph sits under it. (Word by
            # word, "Name:________" is one word and its centre lies in the
            # underscores, so "Name:" went to the write-in line's Span.)
            if centers:
                mp = self.doc[pno]
                grid: dict[tuple[int, int], list] = collections.defaultdict(list)
                for cx, cy, k in centers:
                    grid[(int(cx // 20), int(cy // 20))].append((cx, cy, k))
                art_text: dict[str, list[str]] = collections.defaultdict(list)
                inv = ~mp.transformation_matrix

                def key_at(box) -> str | None:
                    r = pymupdf.Rect(box) * inv
                    r.normalize()
                    wx, wy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
                    gx, gy = int(wx // 20), int(wy // 20)
                    best, bd = None, max(1.5, 0.6 * min(r.width, r.height) if r.width > 0 else 1.5)
                    for dx in (-1, 0, 1):
                        for dy in (-1, 0, 1):
                            for cx, cy, k in grid.get((gx + dx, gy + dy), []):
                                d = math.hypot(cx - wx, cy - wy)
                                if d < bd:
                                    best, bd = k, d
                    return best

                def flush(k, buf):
                    t = "".join(buf).strip()
                    if k and t:
                        (art_text if k.startswith("ART#") else key_text)[k].append(t)

                raw = mp.get_text("rawdict", flags=RAW_FLAGS)
                for blk in raw.get("blocks", []):
                    for ln in blk.get("lines", []):
                        cur, buf = None, []
                        for sp in ln.get("spans", []):
                            for ch in sp.get("chars", []):
                                c = ch.get("c", "")
                                k = key_at(ch["bbox"]) if c.strip() else None
                                if k is None:
                                    if cur is not None:
                                        buf.append(c if c.strip() else " ")
                                    continue
                                if k != cur:
                                    flush(cur, buf)
                                    cur, buf = k, []
                                buf.append(c)
                        flush(cur, buf)
                for c in self.orig_artifacts:
                    if c["page"] == pno:
                        c["text"] = art_text.get("ART#%d" % id(c), c["text"])
        return key_box, key_pages, key_text

    def run(self) -> DocModel:
        m = self.model
        m.page_sizes = [(p.rect.width, p.rect.height) for p in self.doc]
        self.progress(0.05, "reading structure tree")
        k = self.root.get("/K")
        tops = list(k) if isinstance(k, Array) else ([k] if k is not None else [])
        tops = [t for t in tops if isinstance(t, Dictionary)]
        if len(tops) == 1 and resolve_role(_s(tops[0].get("/S")), self.rolemap)[0] == "Document":
            doc_el = tops[0]
            dk = doc_el.get("/K")
            tops = list(dk) if isinstance(dk, Array) else ([dk] if dk is not None else [])
            if "/Lang" in doc_el:
                m.lang = _s(doc_el.Lang)
        for t in tops:
            if isinstance(t, Dictionary) and "/S" in t:
                self._walk(t, None, -1)
        # headers -> node ids
        for node, ids in self.pending_headers:
            node.headers = [self.id_to_node[i] for i in ids if i in self.id_to_node]

        key_box, key_pages, key_text = self._geometry()
        self.progress(0.85, "placing tags")
        for n in m.ordered_nodes():
            if not n.src_keys:
                continue
            by_page: dict[int, tuple] = {}
            texts = []
            for key in n.src_keys:
                b = key_box.get(key)
                if b is not None:
                    p = key_pages.get(key, n.page)
                    by_page[p] = union(by_page.get(p), b)
                texts.extend(key_text.get(key, []))
            if by_page:
                first = min(by_page)
                n.page = first
                n.bbox = tuple(round(v, 2) for v in by_page[first])
                n.parts = [[p, *[round(v, 2) for v in b]] for p, b in sorted(by_page.items()) if p != first]
            # pieces of one word marked separately (" Date" + ":") join without a gap
            n.text = clean_text(re.sub(r"\s+([:;,.!?)\]])", r"\1", " ".join(texts)))[:2000]
        # containers get a display box from their descendants
        for n in reversed(m.ordered_nodes()):
            if n.bbox is None and n.children:
                boxes = [(m.nodes[c].page, m.nodes[c].bbox) for c in n.children
                         if m.nodes[c].bbox is not None]
                if boxes:
                    first = min(p for p, _ in boxes)
                    n.page = first if n.page < 0 else n.page
                    for p, b in boxes:
                        if p == n.page:
                            n.bbox = union(n.bbox, b)
        # empty table cells get a zone from the table grid
        try:
            from .edit import fill_cell_zones
            m.info["cell_zones_filled"] = fill_cell_zones(m, self.path)
        except Exception as exc:
            m.warnings.append(f"could not place empty table cells: {exc}")
        # tags left over from pages that are no longer in the file (pages cut
        # out of a bigger PDF keep the whole old tag tree): drop them
        npages = len(self.pdf.pages)

        def key_ok(k: str) -> bool:
            if k.startswith("s"):
                return True
            try:
                return 0 <= int(k.split(":", 1)[0]) < npages
            except ValueError:
                return False
        live: dict[str, bool] = {}

        def is_live(nid: str) -> bool:
            if nid in live:
                return live[nid]
            n = m.nodes[nid]
            ok = any(key_ok(k) for k in n.src_keys) or bool(n.annots) or \
                (n.page is not None and 0 <= n.page < npages and not n.children and not n.src_keys) or \
                any(is_live(c) for c in list(n.children) if c in m.nodes)
            live[nid] = ok
            return ok
        orphans = [nid for nid in list(m.nodes) if nid in m.nodes and not is_live(nid)
                   and (m.nodes[nid].parent is None or is_live(m.nodes[nid].parent))]
        for nid in orphans:
            if nid in m.nodes:
                m.remove(nid, promote_children=False)
        if orphans:
            m.info["orphan_tags_removed"] = len(orphans)
            m.warnings.append(f"removed {len(orphans)} tag branch(es) that belong to pages no longer in this file")

        # original text artifacts, for visibility in the editor
        for c in self.orig_artifacts:
            if not c["text"] and c["box"] is None:
                continue
            node = StructNode(id=m.new_id("art"), role="Artifact", page=c["page"],
                              bbox=tuple(round(v, 2) for v in c["box"]), artifact=True,
                              artifact_subtype=c["type"] or "Pagination",
                              pagination_subtype=c["sub"] if c["sub"] in ("Header", "Footer", "Watermark") else "",
                              text=" ".join(c["text"])[:300], source="import",
                              note="artifact in the original file")
            # listed at its place on its page (not at the end of the document),
            # so turning it into a real tag keeps a sensible reading position
            m.add(node, None, position_index(m, m.root_children, node))
        # empty / placeholder nodes
        empty = [n for n in m.nodes.values() if not n.src_keys and not n.children and not n.annots
                 and not n.artifact]
        m.info["import"] = {
            "elements": len(m.nodes), "empty_elements": len(empty),
            "untagged_ops": sum(self.untagged.values()),
            "untagged_pages": sorted(p + 1 for p in self.untagged)[:50],
        }
        if self.untagged:
            m.warnings.append(f"{sum(self.untagged.values())} text operator(s) on "
                              f"{len(self.untagged)} page(s) were neither tagged nor artifacts; "
                              "they will be attached to the nearest tag")
        # title and language
        try:
            with self.pdf.open_metadata() as meta:
                m.title = _s(meta.get("dc:title"))
        except Exception:
            pass
        if not m.title:
            m.title = _s(self.pdf.docinfo.get("/Title")) if self.pdf.docinfo else ""
        cat_lang = _s(self.pdf.Root.get("/Lang"))
        if cat_lang:
            m.lang = cat_lang
        m.check_integrity()
        self.progress(1.0, "import complete")
        self.pdf.close()
        self.doc.close()
        return m


def import_tags(path: str, progress: Callable | None = None) -> DocModel:
    return Importer(path, progress).run()
