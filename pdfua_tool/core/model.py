"""Shared data model for the PDF/UA pipeline.

Coordinates in StructNode.bbox / parts are ALWAYS PDF user space
(origin bottom-left, y up) so they compare directly with positions
computed from the page content stream.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, fields
from typing import Any

# ---------------------------------------------------------------- roles
GROUPING = ["Document", "Part", "Art", "Sect", "Div", "BlockQuote", "Caption",
            "TOC", "TOCI", "Index", "NonStruct", "Private"]
HEADINGS = ["H1", "H2", "H3", "H4", "H5", "H6"]
BLOCK = ["P", "H"] + HEADINGS + ["L", "LI", "Lbl", "LBody",
                                  "Table", "TR", "TH", "TD", "THead", "TBody", "TFoot"]
INLINE = ["Span", "Quote", "Note", "Reference", "BibEntry", "Code", "Link", "Annot",
          "Ruby", "RB", "RT", "RP", "Warichu", "WT", "WP"]
ILLUSTRATION = ["Figure", "Formula", "Form"]
STANDARD_ROLES = set(GROUPING + BLOCK + INLINE + ILLUSTRATION)

# The order shown in the editor's role picker.
EDITOR_ROLES = (HEADINGS + ["P", "Caption", "Note", "Figure", "Formula", "Form",
                            "L", "LI", "Lbl", "LBody",
                            "Table", "THead", "TBody", "TFoot", "TR", "TH", "TD",
                            "TOC", "TOCI", "Link", "Annot", "Reference", "BibEntry",
                            "Quote", "BlockQuote", "Code", "Span",
                            "Sect", "Div", "Part", "Art", "Index", "NonStruct",
                            "H", "Artifact"])

# Roles that only group other elements. They never receive page content by
# geometry; everything else with a bbox is a "target" that can own glyphs.
CONTAINER_ROLES = {"Document", "Part", "Art", "Sect", "Div", "TOC", "Index",
                   "NonStruct", "Private", "L", "LI", "Table", "TR",
                   "THead", "TBody", "TFoot", "Ruby", "Warichu"}

# Roles that may receive images / vector paths drawn inside them.
GRAPHIC_ROLES = {"Figure", "Formula", "Form"}

ARTIFACT_TYPES = ["Pagination", "Layout", "Page", "Background"]
PAGINATION_SUBTYPES = ["", "Header", "Footer", "Watermark"]
LIST_NUMBERING = ["", "None", "Disc", "Circle", "Square", "Decimal",
                  "UpperRoman", "LowerRoman", "UpperAlpha", "LowerAlpha"]

Rect = tuple[float, float, float, float]


@dataclass
class StructNode:
    """One node of the logical structure tree (or an artifact region)."""

    id: str
    role: str
    page: int = -1                          # 0-based page of bbox; -1 = none
    parent: str | None = None
    children: list[str] = field(default_factory=list)
    bbox: Rect | None = None                # PDF user space
    parts: list[list[float]] = field(default_factory=list)   # extra [page,x0,y0,x1,y1]
    text: str = ""

    # accessibility attributes
    alt: str = ""
    actual_text: str = ""
    expansion: str = ""                     # /E
    lang: str = ""
    id_attr: str = ""                       # /ID (Note, TH)
    scope: str = ""                         # TH: Row | Column | Both
    headers: list[str] = field(default_factory=list)   # TD -> TH node ids
    rowspan: int = 1
    colspan: int = 1
    list_numbering: str = ""

    # artifacts
    artifact: bool = False
    artifact_subtype: str = "Layout"        # /Type of the artifact
    pagination_subtype: str = ""            # Header | Footer | Watermark

    # bookkeeping
    confidence: float = 1.0
    source: str = "auto"                    # auto | user | ai | import
    note: str = ""
    priority: int = 0                       # 1 = user-drawn geometry wins
    annots: list[int] = field(default_factory=list)      # annotation obj numbers
    src_keys: list[str] = field(default_factory=list)    # original MCIDs (import)
    orig_role: str = ""
    style: dict = field(default_factory=dict)            # size / bold / font hints
    mcids: dict[int, list[int]] = field(default_factory=dict)  # filled by tagger

    # ---------------------------------------------------------------- helpers
    @property
    def is_heading(self) -> bool:
        return self.role in HEADINGS

    @property
    def is_target(self) -> bool:
        """Can own page content by geometry."""
        if self.artifact:
            return self.bbox is not None or bool(self.parts)
        return self.role not in CONTAINER_ROLES and (self.bbox is not None or bool(self.parts))

    def rects_on(self, page: int) -> list[Rect]:
        out: list[Rect] = []
        if self.bbox is not None and self.page == page:
            out.append(tuple(self.bbox))            # type: ignore[arg-type]
        for p in self.parts:
            if int(p[0]) == page:
                out.append((p[1], p[2], p[3], p[4]))
        return out

    def pages(self) -> set[int]:
        s = {int(p[0]) for p in self.parts}
        if self.page >= 0:
            s.add(self.page)
        return s


NODE_FIELDS: list[str] = []


def node_dict(n: "StructNode") -> dict:
    """Fast shallow serialisation (dataclasses.asdict deep-copies and is slow)."""
    if not NODE_FIELDS:
        NODE_FIELDS.extend(f.name for f in fields(StructNode))
    return {k: getattr(n, k) for k in NODE_FIELDS}


@dataclass
class DocModel:
    path: str
    page_count: int = 0
    page_sizes: list[tuple[float, float]] = field(default_factory=list)
    nodes: dict[str, StructNode] = field(default_factory=dict)
    root_children: list[str] = field(default_factory=list)
    title: str = ""
    lang: str = "en-US"
    body_size: float = 10.0
    warnings: list[str] = field(default_factory=list)
    mode: str = "retag"                     # retag | import
    counter: int = 0
    info: dict = field(default_factory=dict)
    id_ns: str = ""                          # id namespace (parallel page chunks)

    # ------------------------------------------------------------------ utils
    def new_id(self, prefix: str = "n") -> str:
        self.counter += 1
        nid = f"{prefix}{self.id_ns}{self.counter}"
        while nid in self.nodes:
            self.counter += 1
            nid = f"{prefix}{self.id_ns}{self.counter}"
        return nid

    def add(self, node: StructNode, parent: str | None = None,
            index: int | None = None) -> StructNode:
        self.nodes[node.id] = node
        node.parent = parent
        siblings = self.root_children if parent is None else self.nodes[parent].children
        if index is None:
            siblings.append(node.id)
        else:
            siblings.insert(max(0, min(index, len(siblings))), node.id)
        return node

    def siblings(self, nid: str) -> list[str]:
        n = self.nodes[nid]
        return self.root_children if n.parent is None else self.nodes[n.parent].children

    def detach(self, nid: str) -> int:
        sib = self.siblings(nid)
        i = sib.index(nid)
        sib.pop(i)
        return i

    def absorb(self, target_id: str, nid: str) -> None:
        """Fold node `nid` into `target_id`: its boxes become extra zones of
        the target (so its content is tagged there) and the node goes away."""
        t, n = self.nodes[target_id], self.nodes[nid]
        boxes = ([[n.page, *n.bbox]] if n.bbox is not None else []) + [list(p) for p in n.parts]
        for b in boxes:
            if t.bbox is None and int(b[0]) == t.page:
                t.bbox = tuple(b[1:5])
            else:
                t.parts.append([int(b[0]), *[float(v) for v in b[1:5]]])
        t.src_keys = list(t.src_keys) + [k for k in n.src_keys if k not in t.src_keys]
        for c in list(n.children):
            self.nodes[c].parent = target_id
            t.children.append(c)
        n.children = []
        sib = self.root_children if n.parent is None or n.parent not in self.nodes \
            else self.nodes[n.parent].children
        if nid in sib:
            sib.remove(nid)
        self.nodes.pop(nid, None)

    def remove(self, nid: str, promote_children: bool = True) -> None:
        n = self.nodes.get(nid)
        if n is None:
            return
        sib = self.siblings(nid)
        i = sib.index(nid)
        sib.pop(i)
        if promote_children:
            for k, cid in enumerate(n.children):
                self.nodes[cid].parent = n.parent
                sib.insert(i + k, cid)
        else:
            for cid in list(n.children):
                self._drop_subtree(cid)
        self.nodes.pop(nid, None)

    def _drop_subtree(self, nid: str) -> None:
        n = self.nodes.pop(nid, None)
        if n:
            for c in n.children:
                self._drop_subtree(c)

    def descendants(self, nid: str) -> list[StructNode]:
        out: list[StructNode] = []
        stack = list(reversed(self.nodes[nid].children))
        while stack:
            cid = stack.pop()
            c = self.nodes.get(cid)
            if c is None:
                continue
            out.append(c)
            stack.extend(reversed(c.children))
        return out

    def ancestors(self, nid: str) -> list[StructNode]:
        out = []
        cur = self.nodes[nid].parent
        while cur is not None and cur in self.nodes:
            out.append(self.nodes[cur])
            cur = self.nodes[cur].parent
        return out

    def ordered_nodes(self) -> list[StructNode]:
        out: list[StructNode] = []
        stack = list(reversed(self.root_children))
        while stack:
            nid = stack.pop()
            n = self.nodes.get(nid)
            if n is None:
                continue
            out.append(n)
            stack.extend(reversed(n.children))
        return out

    def depth_of(self) -> dict[str, int]:
        d: dict[str, int] = {}
        for n in self.ordered_nodes():
            d[n.id] = 0 if n.parent is None else d.get(n.parent, 0) + 1
        return d

    def targets_on(self, page: int) -> list[StructNode]:
        return [n for n in self.nodes.values() if n.is_target and n.rects_on(page)]

    def check_integrity(self) -> list[str]:
        """Repair dangling references; return what was fixed."""
        fixed = []
        self.root_children = [r for r in self.root_children if r in self.nodes]
        seen: set[str] = set()
        for n in self.nodes.values():
            kids = []
            for c in n.children:
                if c in self.nodes and c not in seen:
                    kids.append(c)
                    seen.add(c)
                    self.nodes[c].parent = n.id
            if len(kids) != len(n.children):
                fixed.append(f"{n.id}: dropped dangling children")
            n.children = kids
        for r in self.root_children:
            self.nodes[r].parent = None
            seen.add(r)
        orphans = [nid for nid in self.nodes if nid not in seen]
        for nid in orphans:
            self.nodes[nid].parent = None
            self.root_children.append(nid)
            fixed.append(f"{nid}: re-attached orphan to root")
        return fixed

    # ------------------------------------------------------------- (de)serial
    def to_json(self) -> str:
        return json.dumps({
            "path": self.path, "page_count": self.page_count,
            "page_sizes": self.page_sizes, "root_children": self.root_children,
            "title": self.title, "lang": self.lang, "body_size": self.body_size,
            "warnings": self.warnings, "mode": self.mode, "counter": self.counter,
            "info": self.info,
            "nodes": {k: node_dict(v) for k, v in self.nodes.items()},
        }, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> "DocModel":
        d: dict[str, Any] = json.loads(raw)
        m = cls(path=d["path"], page_count=d.get("page_count", 0),
                page_sizes=[tuple(s) for s in d.get("page_sizes", [])],
                root_children=d.get("root_children", []),
                title=d.get("title", ""), lang=d.get("lang", "en-US"),
                body_size=d.get("body_size", 10.0), warnings=d.get("warnings", []),
                mode=d.get("mode", "retag"), counter=d.get("counter", 0),
                info=d.get("info", {}))
        known = {f.name for f in fields(StructNode)}
        for k, v in d.get("nodes", {}).items():
            v = {kk: vv for kk, vv in v.items() if kk in known}
            v["bbox"] = tuple(v["bbox"]) if v.get("bbox") else None
            v["mcids"] = {int(pk): pv for pk, pv in (v.get("mcids") or {}).items()}
            if v.get("actual_text") in ("Blank Box", "Multiple Blank Line"):
                v["actual_text"] = {"Blank Box": "Blank cell",
                                    "Multiple Blank Line": "Multiple Blank Lines"}[v["actual_text"]]
            m.nodes[k] = StructNode(**v)
        if not m.counter:
            m.counter = len(m.nodes) + 1
        return m


@dataclass
class Finding:
    code: str
    title: str
    severity: str             # error | warning | info | pass
    clause: str = ""
    detail: str = ""
    pages: list[int] = field(default_factory=list)
    count: int = 1
    fixable: str = "manual"   # auto | review | manual
    source: str = "checker"   # checker | verapdf

    def as_row(self) -> list[Any]:
        pages = ", ".join(str(p + 1) for p in self.pages[:25]) or "-"
        if len(self.pages) > 25:
            pages += f" (+{len(self.pages) - 25} more)"
        return [self.code, self.severity.upper(), self.title, self.clause,
                self.detail, pages, self.count, self.fixable]


_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffe\uffff]")


def clean_text(s: str | None) -> str:
    """Drop control characters (they break Excel and confuse screen readers) and trim."""
    if not s:
        return ""
    s = _CTRL.sub(" ", str(s))
    return re.sub(r"[ \t]{2,}", " ", s).strip()


def union(a: Rect | None, b: Rect | None) -> Rect | None:
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def area(r: Rect | None) -> float:
    if not r:
        return 0.0
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def intersection(a: Rect, b: Rect) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return ix * iy


# ------------------------------------------------------------ reading order
def position_index(model: "DocModel", sibs: list[str], node: StructNode) -> int:
    """Where `node` belongs in `sibs` by page, then top-to-bottom on its page."""
    nb = node.bbox
    last_on_page = None
    for k, rid in enumerate(sibs):
        r = model.nodes.get(rid)
        if r is None or r.artifact or rid == node.id or r.page is None or r.page < 0:
            continue
        if r.page == node.page:
            last_on_page = k
            if nb is not None and r.bbox:
                rb = r.bbox
                ov = min(nb[3], rb[3]) - max(nb[1], rb[1])
                same_row = ov > 0.5 * min(nb[3] - nb[1], rb[3] - rb[1])
                if (same_row and rb[0] > nb[0] + 1) or (not same_row and rb[3] < nb[3] - 1):
                    return k
        elif r.page > node.page:
            return k if last_on_page is None else last_on_page + 1
    return last_on_page + 1 if last_on_page is not None else len(sibs)


# containers that only group the reading flow (a Sect may run over many pages)
FLOW_GROUPS = {"Document", "Part", "Art", "Sect", "Div", "NonStruct", "Private"}


def page_span(model: "DocModel", nid: str) -> tuple[int, int] | None:
    """First and last page a tag and everything inside it is printed on."""
    n = model.nodes.get(nid)
    if n is None:
        return None
    pages: list[int] = []
    stack = [n]
    while stack:                      # the blocks it reads, not every link inside them
        x = stack.pop()
        if x.artifact:
            continue
        if x.role in FLOW_GROUPS:
            stack.extend(model.nodes[c] for c in x.children if c in model.nodes)
            pages.extend(p for p in x.pages() if p >= 0)
        else:
            pages.extend(p for p in x.pages() if p >= 0)
    return (min(pages), max(pages)) if pages else None


def flow_position(model: "DocModel", page: int, bbox, skip: set | None = None) -> tuple[str | None, int]:
    """(parent, index): where a new block printed at `bbox` on `page` is read.

    A file tagged elsewhere often has one Sect holding the whole book. A tag drawn
    by hand used to go to the end of the document's top level — after that Sect
    and its 1 208 blocks — so it was read last. The new tag goes into the
    container that holds this page's blocks, at its place on the page."""
    skip = skip or set()
    best = None                      # (score, depth, parent id)
    lists = [(None, model.root_children, 0)]
    depth = {nid: d for nid, d in model.depth_of().items()} if hasattr(model, "depth_of") else {}
    for n in model.nodes.values():
        if n.role in FLOW_GROUPS and n.children and not n.artifact and n.id not in skip:
            lists.append((n.id, n.children, depth.get(n.id, 0) + 1))
    for pid, sibs, d in lists:
        on = 0
        bracket_lo = bracket_hi = False
        for c in sibs:
            x = model.nodes.get(c)
            if x is None or x.artifact or c in skip:
                continue
            sp = page_span(model, c) if x.role in FLOW_GROUPS else (
                (x.page, max([x.page] + [int(q[0]) for q in x.parts])) if x.page is not None and x.page >= 0 else None)
            if sp is None:
                continue
            if x.role not in FLOW_GROUPS and sp[0] <= page <= sp[1]:
                on += 1
            if sp[0] < page:
                bracket_lo = True
            if sp[1] > page:
                bracket_hi = True
        score = on * 2 + (1 if bracket_lo and bracket_hi else 0)
        if score and (best is None or (score, d) > (best[0], best[1])):
            best = (score, d, pid)
    pid = best[2] if best else None
    sibs = model.root_children if pid is None else model.nodes[pid].children
    probe = StructNode(id="__probe__", role="P", page=page, bbox=tuple(bbox) if bbox else None)
    return pid, position_index(model, [c for c in sibs if c not in skip], probe)


def misplaced_by_page(model: "DocModel", ids: list[str]) -> list[str]:
    """Blocks that break the page sequence (e.g. a page-2 paragraph read after
    page-30 content): everything outside the longest page-ordered run."""
    seq = [(i, model.nodes[i].page) for i in ids
           if i in model.nodes and not model.nodes[i].artifact and (model.nodes[i].page or 0) >= 0]
    if len(seq) < 2:
        return []
    import bisect
    tails: list[int] = []          # smallest tail page for runs of each length
    tails_idx: list[int] = []
    prev = [-1] * len(seq)
    for k, (_, p) in enumerate(seq):
        j = bisect.bisect_right(tails, p)
        if j == len(tails):
            tails.append(p)
            tails_idx.append(k)
        else:
            tails[j] = p
            tails_idx[j] = k
        prev[k] = tails_idx[j - 1] if j > 0 else -1
    keep = set()
    k = tails_idx[-1]
    while k >= 0:
        keep.add(k)
        k = prev[k]
    out = [seq[k][0] for k in range(len(seq)) if k not in keep]
    # a page-30 paragraph read after a Sect that runs to page 140 is read last
    reach = -1
    for i in ids:
        x = model.nodes.get(i)
        if x is None or x.artifact:
            continue
        if x.role in FLOW_GROUPS:
            sp = page_span(model, i)
            if sp is not None:
                reach = max(reach, sp[1])
            continue
        if x.page is not None and 0 <= x.page < reach and i not in out:
            out.append(i)
    return out


def is_empty_header(model: "DocModel", n: "StructNode") -> bool:
    """A header cell (TH) with no content of any kind."""
    if n.role != "TH" or n.artifact:
        return False
    if (n.text or "").strip() or (n.actual_text or "").strip() or (n.alt or "").strip():
        return False
    if n.src_keys or n.annots:
        return False
    return not any(not d.artifact for d in model.descendants(n.id))


def unwrap_table_bodies(model: "DocModel") -> int:
    """The other way round, for a client whose tables hold their rows straight
    under the Table (option "Group rows in THead/TBody" off): THead, TBody and
    TFoot go, their rows stay where they were, in order."""
    gone = 0
    for t in [n for n in list(model.nodes.values()) if n.role == "Table"]:
        kids = list(t.children)
        if not any(model.nodes[c].role in ("THead", "TBody", "TFoot") for c in kids if c in model.nodes):
            continue
        new_kids: list[str] = []
        for c in kids:
            g = model.nodes.get(c)
            if g is not None and g.role in ("THead", "TBody", "TFoot"):
                for r in g.children:
                    if r in model.nodes:
                        model.nodes[r].parent = t.id
                        new_kids.append(r)
                g.children = []
                del model.nodes[g.id]
                gone += 1
            else:
                new_kids.append(c)
        t.children = new_kids
    return gone


def wrap_table_bodies(model: "DocModel") -> int:
    """Every table keeps its rows in THead / TBody.

    A row group is not optional in practice: PAC and most readers expect the
    body rows of a table to sit in a `TBody`, and the header rows (when the
    table has any) in a `THead`. A table with no header needs no `THead` — but
    it still needs the `TBody`.

    Rows already inside a group are left where they are; stray rows join the
    table's body group, which is created if it is missing."""
    made = 0
    for t in [n for n in list(model.nodes.values()) if n.role == "Table"]:
        kids = [model.nodes[c] for c in t.children if c in model.nodes]
        rows = [c for c in kids if c.role == "TR"]
        groups = [c for c in kids if c.role in ("THead", "TBody", "TFoot")]
        if not rows and groups:
            continue                          # already grouped
        if not rows:
            continue
        head_n = int((t.style or {}).get("header_rows", 0) or 0)
        if head_n >= len(rows):
            # every row a header means no data at all: a header row announces
            # columns that are not there, so these rows are the table's body
            head_n = 0
            t.style = dict(t.style or {}, header_rows=0)
            t.note = (t.note + "; " if t.note else "") + \
                "header dropped: the table has no rows under it"
            for r in rows:
                for c in [model.nodes[x] for x in r.children if x in model.nodes]:
                    if c.role == "TH":
                        c.role, c.scope, c.headers = "TD", "", []
        if not head_n and not groups:
            # no count recorded: leading rows of nothing but TH are the header
            for r in rows:
                cells = [model.nodes[c] for c in r.children if c in model.nodes]
                if cells and all(c.role == "TH" for c in cells):
                    head_n += 1
                else:
                    break
            if head_n >= len(rows):           # every row is TH: no header group
                head_n = 0
        body_group = next((g for g in groups if g.role == "TBody"), None)
        head_group = next((g for g in groups if g.role == "THead"), None)
        for i, r in enumerate(rows):
            want_head = head_group is None and i < head_n and not groups
            if want_head:
                if head_group is None:
                    head_group = StructNode(id=model.new_id("thead"), role="THead",
                                            page=r.page, source=t.source)
                    model.add(head_group, t.id, t.children.index(r.id))
                    made += 1
                model.detach(r.id)
                model.add(r, head_group.id)
                continue
            if body_group is None:
                body_group = StructNode(id=model.new_id("tbody"), role="TBody",
                                        page=r.page, source=t.source)
                model.add(body_group, t.id, t.children.index(r.id))
                made += 1
            model.detach(r.id)
            model.add(r, body_group.id)
        for g in (head_group, body_group):
            if g is not None:
                boxes = [model.nodes[c].bbox for c in g.children
                         if c in model.nodes and model.nodes[c].bbox]
                if boxes:
                    g.bbox = _ubox_simple(boxes)
    return made


def _ubox_simple(boxes) -> tuple:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def clamp_heading_levels(model: "DocModel") -> int:
    """No skipped levels, and the first heading is H1 (PDF/UA 7.4).

    Levels follow the reading order, so this is run again after anything that
    moves a heading — a page that opens with its own title, say."""
    prev = 0
    changed = 0
    for n in model.ordered_nodes():
        if n.role in HEADINGS and not n.artifact:
            lv = int(n.role[1])
            new = 1 if prev == 0 else min(lv, prev + 1)
            if new != lv:
                n.note = (n.note + "; " if n.note else "") + f"level adjusted from H{lv}"
                n.role = f"H{new}"
                changed += 1
            prev = new
    return changed


def open_pages_with_heading(model: "DocModel", mode: str = "margin") -> int:
    """The heading that names a page is read first on that page.

    A workbook prints the unit title as a tab in the outer margin, turned on its
    side. By position it comes last, so a screen reader met the page's heading
    after everything else — "Blend Words", the table, then "Unit 1 Short a". The
    heading that names a page belongs at the front of it: the page then opens
    with its title, and the Sect built at the end wraps the page's content under
    the right heading.

    mode "margin" (default) moves the first heading set outside the text — a
    side tab, a rotated running title — whatever its level; "first" moves the
    page's top-level heading wherever it is, unless a heading of the same or a
    better level comes before it (so a numbered sequence of headings keeps its
    order); "off" leaves the order alone.
    """
    if mode not in ("margin", "first"):
        return 0
    roots = model.root_children

    def rank(nid) -> int | None:
        n = model.nodes.get(nid)
        if n is None or not n.is_heading or n.artifact:
            return None
        return int(n.role[1])

    def in_margin(node, ids) -> bool:
        """Is this heading a tab down the side of the page?

        Tall and narrow — the text is set on its side — and out at one edge of
        the sheet. Both are needed: a short word that happens to sit right of
        the text is not a tab, and a heading in the text column never is."""
        if node.bbox is None:
            return False
        w, h = node.bbox[2] - node.bbox[0], node.bbox[3] - node.bbox[1]
        if h <= 1.6 * w or len((node.text or "").strip()) <= 6:
            return False
        try:
            pw = model.page_sizes[node.page][0]
        except Exception:
            pw = 0.0
        if pw <= 0:
            return True
        return node.bbox[0] >= 0.78 * pw or node.bbox[2] <= 0.22 * pw

    moved = 0
    for pno in sorted({model.nodes[i].page for i in roots
                       if i in model.nodes and model.nodes[i].page >= 0}):
        idxs = [i for i, nid in enumerate(roots)
                if nid in model.nodes and model.nodes[nid].page == pno]
        if len(idxs) < 2:
            continue
        ids = [roots[i] for i in idxs]
        ranks = [rank(i) for i in ids]
        if not any(r is not None for r in ranks):
            continue
        if mode == "margin":
            pos = next((k for k, r in enumerate(ranks)
                        if r is not None and in_margin(model.nodes[ids[k]], ids)), None)
        else:
            top = min(r for r in ranks if r is not None)
            pos = next(k for k, r in enumerate(ranks) if r == top)
            if any(r is not None and r <= top for r in ranks[:pos]):
                pos = None
        if not pos:                      # nothing to move, or it is first already
            continue
        node = model.nodes[ids[pos]]
        roots.insert(idxs[0], roots.pop(idxs[pos]))
        node.note = (node.note + "; " if node.note else "") + "page title read first"
        moved += 1
    if moved:
        model.info["headings_moved_first"] = moved
    return moved


def wrap_sections(model: "DocModel", mode: str = "h1") -> int:
    """Each top heading opens a `Sect` that holds it and everything under it.

    A flat run of H1, P, H2, P, H1 … is legal but tells a screen-reader user
    nothing about where one part of the document ends and the next begins. With
    sections the tree reads: Document > Sect > (H1, H2, P, Table …) > next Sect,
    which is the shape a tagged book is expected to have.

    mode 'h1'  — one section per top-level heading (the highest level present)
    mode 'all' — sections nest: an H2 opens a section inside its H1's section
    mode 'one' — the whole article in one Sect (Document > Sect > everything),
                 the shape InDesign exports a journal article in
    Everything before the first heading stays at document level."""
    if mode == "one":
        return _one_section(model)
    if mode not in ("h1", "all"):
        return 0
    roots = [model.nodes[r] for r in model.root_children if r in model.nodes]
    heads = [n for n in roots if n.role in HEADINGS and not n.artifact]
    if not heads:
        return 0
    # a file that already carries its own sections is left alone
    for n in roots:
        if n.role in ("Sect", "Part", "Art") and any(
                model.nodes[c].role in HEADINGS for c in n.children if c in model.nodes):
            return 0
    top = min(int(n.role[1]) for n in heads)
    made = 0
    new_roots: list[str] = []
    stack: list[tuple[int, StructNode]] = []
    for n in roots:
        lvl = int(n.role[1]) if (n.role in HEADINGS and not n.artifact) else None
        if lvl is not None and (mode == "all" or lvl == top):
            while stack and stack[-1][0] >= lvl:
                stack.pop()
            sect = StructNode(id=model.new_id("sect"), role="Sect", page=n.page,
                              confidence=1.0, source=n.source,
                              note=f"section opened by {n.role}: {(n.text or '')[:40]}")
            model.nodes[sect.id] = sect
            if stack:
                parent = stack[-1][1]
                sect.parent = parent.id
                parent.children.append(sect.id)
            else:
                sect.parent = None
                new_roots.append(sect.id)
            stack.append((lvl, sect))
            made += 1
        if stack:
            host = stack[-1][1]
            n.parent = host.id
            host.children.append(n.id)
        else:
            new_roots.append(n.id)
    model.root_children = new_roots
    return made


def pad_table_spans(model: "DocModel") -> int:
    """Every merged cell becomes one cell per grid position: the cell keeps its
    text (and its box, so its words stay with it) in its first position, the
    positions it covered become empty cells with nothing printed in them."""
    made = 0
    for t in [n for n in model.nodes.values() if n.role == "Table" and not n.artifact]:
        rows: list[StructNode] = []
        for c in t.children:
            n = model.nodes.get(c)
            if n is None:
                continue
            if n.role == "TR":
                rows.append(n)
            elif n.role in ("THead", "TBody", "TFoot"):
                rows.extend(model.nodes[x] for x in n.children if x in model.nodes and model.nodes[x].role == "TR")
        # grid positions taken, row by row
        taken: list[set] = [set() for _ in rows]
        place: dict[str, tuple[int, int]] = {}
        for r, tr in enumerate(rows):
            col = 0
            for cid in tr.children:
                cell = model.nodes.get(cid)
                if cell is None or cell.role not in ("TD", "TH"):
                    continue
                while col in taken[r]:
                    col += 1
                place[cell.id] = (r, col)
                for rr in range(r, min(len(rows), r + max(1, cell.rowspan))):
                    for cc in range(col, col + max(1, cell.colspan)):
                        taken[rr].add(cc)
                col += max(1, cell.colspan)
        for cid, (r, col) in list(place.items()):
            cell = model.nodes[cid]
            rs, cs = max(1, cell.rowspan), max(1, cell.colspan)
            if rs == 1 and cs == 1:
                continue
            for rr in range(r, min(len(rows), r + rs)):
                for cc in range(col, col + cs):
                    if rr == r and cc == col:
                        continue
                    pad = StructNode(id=model.new_id("pad"), role="TD", page=cell.page, source="auto",
                                     note="position covered by a merged cell (table_spans = pad)")
                    pad.style = {"pad_col": cc}
                    tr = rows[rr]
                    # in column order among the row's cells
                    at = len(tr.children)
                    for k, other in enumerate(tr.children):
                        oc = place.get(other, (rr, (model.nodes[other].style or {}).get("pad_col", 10 ** 6)))[1]
                        if oc > cc:
                            at = k
                            break
                    model.nodes[pad.id] = pad
                    pad.parent = tr.id
                    tr.children.insert(at, pad.id)
                    place[pad.id] = (rr, cc)
                    made += 1
            cell.rowspan = cell.colspan = 1
    return made


def _one_section(model: "DocModel") -> int:
    roots = [model.nodes[r] for r in model.root_children if r in model.nodes]
    if any(n.role in ("Sect", "Part", "Art") and not n.artifact for n in roots):
        return 0                       # a file with its own sections is left alone
    content = [n for n in roots if not n.artifact]
    if not content:
        return 0
    sect = StructNode(id=model.new_id("sect"), role="Sect", page=content[0].page,
                      confidence=1.0, source="auto", note="the whole article in one section")
    model.nodes[sect.id] = sect
    new_roots: list[str] = []
    for n in roots:
        if n.artifact:
            new_roots.append(n.id)
            continue
        if not sect.children:
            new_roots.append(sect.id)
        n.parent = sect.id
        sect.children.append(n.id)
    model.root_children = new_roots
    return 1


def convert_empty_headers(model: "DocModel") -> int:
    """Every empty TH becomes TD (no Scope), and no cell refers to it in Headers.

    Not in a client house style that keeps them (Kriyadocs, V4.17): there an
    empty cell in a header row is still a header cell — TH, as the client's
    files have it (core/compat.py; the V4 engine is unchanged)."""
    from .compat import house
    if house():
        return 0
    empties = {n.id for n in model.nodes.values() if is_empty_header(model, n)}
    for nid in empties:
        n = model.nodes[nid]
        n.role = "TD"
        n.scope = ""
        n.headers = []
    if empties:
        for n in model.nodes.values():
            if n.headers:
                n.headers = [h for h in n.headers if h not in empties]
    return len(empties)
