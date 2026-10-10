"""Cross-references and links found by rule, not by AI.

Everything a reader clicks is already written in the page: a URL *is* the
address, "see Figure 3" names its figure, a table of contents line carries the
page number, "[12]" points at entry 12 of the bibliography. So these are found
with patterns and matched against targets collected from the same document; the
AI is only asked about the leftovers (see core/ai.py, `xref_match`).

What this module produces
  * a `Reference` tag around the mention (PDF/UA semantics for a
    cross-reference), when `cross_reference_tags` is on
  * a `Link` element with a real link annotation, for the kinds the options
    switch on (`link_web`, `link_toc`, `link_sections`, `link_figures_tables`,
    `link_references`, `link_index`)

Nothing is invented: a link is only created when its destination was found in
this document, and a kind that is switched off is still listed in the editor so
it can be turned on after a look.

Printed page numbers vs. PDF pages
  A "see page 12" means the 12 printed on that page, which is rarely the 12th
  sheet (front matter, inserts). The page-number artifacts the analyser already
  marked give that mapping for free, so the same map serves the contents list,
  the index and plain page references.
"""
from __future__ import annotations

import collections
import re
import statistics
from dataclasses import dataclass, field

from .compat import house
from .model import DocModel, StructNode, area, intersection

# --------------------------------------------------------------- patterns
URL_RE = re.compile(r"\b(?:https?://|www\.)[^\s<>\"'\\^`{|}]+", re.I)
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
# "Chapter 3", "Section 2.1", "§2.1", "Part II", "Appendix A", "Unit 4"
SECT_WORDS = r"chapter|chap\.|section|sect\.|part|appendix|annex|unit|lesson|module"
# the number keeps its case even though the word does not: with a case-insensitive
# [A-Z] the plain plural "sections" would read as "section" + the letter "s"
SECT_RE = re.compile(r"\b(?P<w>" + SECT_WORDS + r")s?\s+(?P<n>\d+(?:\.\d+)*|(?-i:[IVXLC]{1,6}|[A-Z]))"
                     r"\b(?!\.?\d)", re.I)
PARA_RE = re.compile(r"§\s*(?P<n>\d+(?:\.\d+)*)")
# "see Figure 3", "Table 2.1", "Exhibit 4b"
FIG_WORDS = r"figure|fig\.|table|chart|map|box|exhibit|diagram|scheme|plate|listing|equation|formula"
FIG_RE = re.compile(r"\b(?P<w>" + FIG_WORDS + r")s?\s*(?P<n>\d+(?:\.\d+)*[a-z]?)\b(?!\.?\d)", re.I)
# "[12]", "[3, 7]", "(Smith, 2019)", "Smith (2019)"
BRACKET_REF_RE = re.compile(r"\[(?P<body>\d{1,3}(?:\s*[,–-]\s*\d{1,3})*)\]")
# one citation inside a run: a number, or a range "38–40" (Kriyadocs, V4.17)
CITE_PART_RE = re.compile(r"\d{1,3}(?:\s*[–—‒-]\s*\d{1,3})?")
CITE_DASHES = "–—‒-"
# the first author is the key: "Hasbrouck, 2010", "Treiman et al., 2016",
# "Graham, Harris, & Chorzempa, 2002", "Smith and Jones (2019)"
_AY_NAME = r"[A-Z][A-Za-z'’-]{1,20}"
AUTHOR_YEAR_RE = re.compile(r"\b(?P<a>" + _AY_NAME + r")"
                            r"(?:\s+et\s+al\.?|(?:,\s+" + _AY_NAME + r")*,?\s+(?:and|&)\s+" + _AY_NAME + r")?"
                            r"[,\s]*\(?(?P<y>(?:1[89]|20)\d\d)[a-z]?\)?")
PAGE_REF_RE = re.compile(r"\b(?:pages?|pp?\.)\s*(?P<n>\d{1,4})\b", re.I)
# a page number at the end of a contents / index line
TRAILING_PAGE_RE = re.compile(r"(?P<n>\d{1,4}|[ivxlcdm]{1,7})\s*$", re.I)
INDEX_PAGES_RE = re.compile(r"(?<![\w.])(?P<n>\d{1,4})(?![\w.])")

_SINGULAR = {"chapter", "section", "part", "appendix", "annex", "unit", "lesson", "module",
             "figure", "table", "chart", "map", "box", "exhibit", "diagram", "scheme",
             "plate", "listing", "formula"}

CAPTION_TARGET_RE = re.compile(r"^\s*(?P<w>" + FIG_WORDS + r")\s*\.?\s*(?P<n>\d+(?:\.\d+)*[a-z]?)\b", re.I)
HEADING_TARGET_RE = re.compile(r"^\s*(?P<w>" + SECT_WORDS +
                               r")\s+(?P<n>\d+(?:\.\d+)*|(?-i:[IVXLC]{1,6}|[A-Z]))\b", re.I)
NUMBERED_HEAD_RE = re.compile(r"^\s*(?P<n>\d+(?:\.\d+){0,4})\.?\s+\S")
BIB_LABEL_RE = re.compile(r"^\s*\[?(?P<n>\d{1,3})[\]\.\)]\s*\S")
BIB_AUTHOR_RE = re.compile(r"^\s*(?P<a>[A-Z][A-Za-z'’-]{1,20})[,\s].{0,160}?\(?(?P<y>(?:1[89]|20)\d\d)[a-z]?\)?")
REF_SECTION_RE = re.compile(r"\b(references|bibliography|works cited|literature cited|sources)\b", re.I)
INDEX_SECTION_RE = re.compile(r"\b(index|subject index|author index)\b", re.I)
TOC_SECTION_RE = re.compile(r"^\s*(table of )?contents\s*$|^\s*in this (book|guide|manual)\s*$", re.I)

# text that may hold a mention (containers and table wrappers never do)
TEXT_ROLES = {"P", "H1", "H2", "H3", "H4", "H5", "H6", "H", "LBody", "Lbl", "TD", "TH",
              "Caption", "Note", "TOCI", "Quote", "BlockQuote", "Span", "Code", "BibEntry",
              "Reference"}
KIND_LABEL = {"web": "Web address", "email": "Email address", "toc": "Contents entry",
              "section": "Chapter / section / part", "figure": "Figure / table reference",
              "reference": "Bibliography reference", "index": "Index entry", "page": "Page reference",
              "footnote": "Footnote reference", "footnote_back": "Footnote — back to the text",
              "caption_back": "Caption — back to the text"}
KIND_OPTION = {"web": "link_web", "email": "link_web", "toc": "link_toc",
               "section": "link_sections", "figure": "link_figures_tables",
               "reference": "link_references", "index": "link_index", "page": "link_index",
               "footnote": "link_footnotes", "footnote_back": "link_back",
               "caption_back": "link_back"}
# the mark a footnote starts with: "1", "12", "*", "†"
NOTE_LABEL_RE = re.compile(r"^\s*(?P<n>\d{1,3}|[*\u2020\u2021\u00a7\u00b6])(?=[\s.):]|[A-Z(\u201c\"'])")
SUPER_CHARS = set("0123456789*\u2020\u2021")
SYMBOL_MARKS = set("*\u2020\u2021\u00a7\u00b6")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _key(word: str, num: str) -> str:
    """'Fig. 3', 'Figure 3' and 'Figures 3' all name the same target."""
    w = word.lower().rstrip(".")
    w = {"fig": "figure", "chap": "chapter", "sect": "section", "equation": "formula",
         "boxes": "box"}.get(w, w)
    if w.endswith("s") and w[:-1] in _SINGULAR:
        w = w[:-1]
    return f"{w} {num.upper() if re.fullmatch('[ivxlcIVXLC]+', num) else num.lower()}"


def _roman(s: str) -> int | None:
    vals = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    s = s.lower()
    if not s or any(c not in vals for c in s):
        return None
    total = prev = 0
    for c in reversed(s):
        v = vals[c]
        total += -v if v < prev else v
        prev = max(prev, v)
    return total or None


# ------------------------------------------------------------- line index
@dataclass
class _IndexedLine:
    """A page line with its visible text and a box per character of that text."""
    text: str
    boxes: list                                   # display space, aligned to `text`
    bbox: tuple
    size: float


def index_line(line) -> _IndexedLine:
    """Rebuild the line's collapsed text keeping one char box per character.

    `collect_lines` normalises whitespace for the text it stores but keeps the
    raw characters, so a match found in the text can be mapped back to the
    glyphs it covers only by redoing that collapse here, step by step."""
    text_parts: list[str] = []
    boxes: list = []
    space = False
    for ch, box in line.chars:
        if ch.isspace():
            space = True
            continue
        if space and text_parts:
            text_parts.append(" ")
            boxes.append(None)
        space = False
        text_parts.append(ch)
        boxes.append(box)
    return _IndexedLine("".join(text_parts), boxes, line.bbox, line.size)


def _span_box(il: _IndexedLine, a: int, b: int):
    got = [x for x in il.boxes[a:b] if x is not None]
    if not got:
        return None
    return (min(g[0] for g in got), min(g[1] for g in got),
            max(g[2] for g in got), max(g[3] for g in got))


# ----------------------------------------------------------------- targets
@dataclass
class Target:
    key: str
    page: int
    top: float                       # PDF y of the top of the target
    node: str = ""
    label: str = ""


@dataclass
class Mention:
    kind: str
    page: int
    rects: list = field(default_factory=list)     # PDF space
    text: str = ""
    key: str = ""
    owner: str = ""
    target: dict | None = None
    note: str = ""
    conf: float = 0.8

    def to_dict(self) -> dict:
        return {"kind": self.kind, "page": self.page, "rects": [list(r) for r in self.rects],
                "text": self.text, "key": self.key, "owner": self.owner,
                "target": self.target, "note": self.note, "conf": round(self.conf, 2)}


FOLIO_RE = re.compile(r"^\s*(\d{1,4}|[ivxlcdm]{1,7})\s*$", re.I)


def printed_page_map(model: DocModel, page_lines: list | None = None, doc=None) -> dict[str, int]:
    """Printed page number -> PDF page index.

    Three ways, best first: the page-number artifacts the analysis marked, the
    file's own page labels, and the folio printed at the top or foot of each
    sheet. A file opened with its existing tags has no artifacts of ours, and
    without the other two a contents entry would have nowhere to point.

    A number printed on more than one sheet (restarting roman front matter and
    then arabic, say) keeps its first sheet, which is what a reference means."""
    out: dict[str, int] = {}
    for n in model.nodes.values():
        # a page number is an artifact, or — where the project reads it — a P
        if not (n.style or {}).get("page_number") and \
                (not n.artifact or "page number" not in (n.note or "")):
            continue
        t = _norm(n.text).lower().replace("page", "").strip()
        if re.fullmatch(r"\d{1,4}|[ivxlcdm]{1,7}", t) and t not in out:
            out[t] = n.page
    if doc is not None:                      # the file's own page labels
        for pno in range(getattr(doc, "page_count", 0)):
            try:
                label = (doc[pno].get_label() or "").strip().lower()
            except Exception:
                break
            if label and FOLIO_RE.match(label) and label not in out:
                out[label] = pno
    if page_lines:                           # the folio printed on the sheet
        for pno, lines in enumerate(page_lines):
            if not lines:
                continue
            ys = [l.y0 for l in lines] + [l.y1 for l in lines]
            top, bottom = min(ys), max(ys)
            edge = (bottom - top) * 0.12
            for l in lines:
                if not (l.y1 <= top + edge or l.y0 >= bottom - edge):
                    continue
                if re.fullmatch(r"[A-Z]{1,3}", _norm(l.text)):
                    continue                     # an index letter "D", not page d (500)
                t = _norm(l.text).lower().replace("page", "").strip()
                if FOLIO_RE.match(t) and t not in out:
                    out[t] = pno
    return out


def collect_targets(model: DocModel, page_lines: list | None = None,
                    doc=None) -> tuple[dict[str, Target], dict[str, int]]:
    """Everything in this document a cross-reference can point at."""
    targets: dict[str, Target] = {}
    pages = printed_page_map(model, page_lines, doc)

    def put(key: str, node: StructNode, label: str = "", shortest: bool = False) -> None:
        """First one wins — except for captions, where the shortest text wins: a
        sentence that opens with "Figure 1 shows …" looks like a caption, and the
        real caption ("Figure 1 The score sheet") is the better destination."""
        if not key or node.bbox is None or node.page < 0:
            return
        old = targets.get(key)
        if old is not None and not (shortest and len(_norm(node.text)) < len(old.label)):
            return
        targets[key] = Target(key, node.page, node.bbox[3], node.id,
                              label or _norm(node.text)[:80])

    in_refs = False
    second: dict[str, StructNode] = {}
    for n in model.ordered_nodes():
        if n.artifact:
            continue
        txt = _norm(n.text)
        if n.is_heading or n.role == "H":
            # a bibliography heading opens the stretch where entries are targets
            in_refs = bool(REF_SECTION_RE.search(txt)) and len(txt) < 40
            mt = HEADING_TARGET_RE.match(txt)
            if mt:
                put(_key(mt.group("w"), mt.group("n")), n, txt)
            nm = NUMBERED_HEAD_RE.match(txt)
            if nm:
                num = nm.group("n")
                put(f"section {num}", n, txt)
                if "." not in num:
                    put(f"chapter {num}", n, txt)
            continue
        if n.role == "Caption":
            box_of = n
            if not txt and house():
                # a client's Caption holds its words in a P (Kriyadocs): the
                # caption's text and place are its paragraph's (V4.18)
                kids = [d for d in model.descendants(n.id) if d.text and not d.artifact]
                txt = _norm(" ".join(d.text for d in kids))
                box_of = next((d for d in kids if d.bbox is not None), n)
            mt = CAPTION_TARGET_RE.match(txt)
            if mt:
                # the caption belongs to a figure or table; point at that, so the
                # reader lands on the thing rather than on the words under it
                host = model.nodes.get(n.parent) if n.parent else None
                anchor = host if (host is not None and host.role in ("Figure", "Table", "Formula")) else box_of
                put(_key(mt.group("w"), mt.group("n")), anchor, txt, shortest=anchor in (n, box_of))
            continue
        if n.role == "BibEntry" or (in_refs and n.role in ("LBody", "P", "LI")):
            bl = BIB_LABEL_RE.match(txt)
            if bl:
                put(f"ref {int(bl.group('n'))}", n, txt)
            elif n.role == "BibEntry" or (n.role == "LBody" and house()):
                # a numbered list keeps the number in the item's Lbl
                num = _entry_number(model, n)
                if num:
                    put(f"ref {num}", n, txt)
            ba = BIB_AUTHOR_RE.match(txt)
            if ba:
                put(f"ref {ba.group('a').lower()} {ba.group('y')}", n, txt)
                # the co-authors and an organisation's other words, kept for a
                # citation that names them ("National Reading Panel, 2000")
                head = txt[:ba.end("y")]
                if "(" in head:
                    head = head[:head.index("(")]       # "Moats, L. C. (Winter 2005": not "Winter"
                for w in re.findall(r"[A-Z][A-Za-z'’-]{2,20}", head):
                    second.setdefault(f"ref {w.lower()} {ba.group('y')}", n)
    for key, n in second.items():
        if key not in targets:
            put(key, n, _norm(n.text))
    return targets, pages


def _entry_number(model: DocModel, n: StructNode) -> int | None:
    for a in model.ancestors(n.id):
        if a.role == "LI":
            for c in a.children:
                lbl = model.nodes.get(c)
                if lbl is not None and lbl.role == "Lbl":
                    m = re.search(r"\d{1,3}", lbl.text or "")
                    return int(m.group(0)) if m else None
            return None
    return None


# ---------------------------------------------------------------- scanning
def _section_kinds(model: DocModel) -> dict[str, str]:
    """Which stretch of the document each node sits in: contents, index or neither.

    A contents list or an index is recognised by its heading, so entries keep
    working when the analyser saw no dot leaders and tagged them as ordinary
    paragraphs."""
    out: dict[str, str] = {}
    cur = ""
    for n in model.ordered_nodes():
        if n.artifact:
            continue
        if n.is_heading or n.role == "H":
            txt = _norm(n.text)
            cur = ("toc" if TOC_SECTION_RE.match(txt) else
                   "index" if (INDEX_SECTION_RE.search(txt) and len(txt) < 24) else "")
            continue
        if cur:
            out[n.id] = cur
    return out


def _owner_index(model: DocModel) -> dict[int, list[tuple]]:
    """(node, zone) per page: every zone of a tag, the joined parts of a
    paragraph ("Correspondence to" + the name and address under it) too."""
    by_page: dict[int, list[tuple]] = collections.defaultdict(list)
    art: set[str] = set()
    for n in model.ordered_nodes():
        if n.artifact or n.parent in art or (n.style or {}).get("page_number"):
            art.add(n.id)            # a printed page number refers to nothing
            continue
        if n.role in TEXT_ROLES and n.bbox is not None and n.page >= 0:
            if not house():
                by_page[n.page].append((n, n.bbox))      # V4: the tag's own box
                continue
            for p in sorted(n.pages()):
                for r in n.rects_on(p):
                    by_page[p].append((n, r))
    return by_page


def _owner_of(cands: list, box) -> StructNode | None:
    best, best_a = None, 0.0
    for item in cands:
        n, r = item if isinstance(item, tuple) else (item, item.bbox)
        if r is None:
            continue
        ov = intersection(box, r)
        if ov <= 0:
            continue
        # the smallest element that really covers the words owns them
        # (on a tie the inner one: a BibEntry inside an LBody of the same size)
        if ov > 0.5 * area(box) and (best is None or area(r) <= best_a):
            best, best_a = n, area(r)
    return best


def _note_host(model: DocModel, n: StructNode | None) -> StructNode | None:
    """In a client's job a Note holds Lbl (its mark) + P (its words): a word
    found in either is a word of the Note."""
    if n is not None and house() and n.role in ("Lbl", "P") and n.parent in model.nodes \
            and model.nodes[n.parent].role == "Note":
        return model.nodes[n.parent]
    return n


def _removed_by_hand(model: DocModel, page: int, box) -> bool:
    """A link taken out by hand in the Links stage stays out (V4.18)."""
    for g in model.info.get("links_removed") or ():
        try:
            if int(g[0]) == page:
                r = tuple(float(v) for v in g[1:5])
                if intersection(box, r) > 0.4 * min(area(box), area(r)):
                    return True
        except Exception:
            continue
    return False


def _already_linked(model: DocModel, page: int, box) -> bool:
    if model.info.get("links_removed") and _removed_by_hand(model, page, box):
        return True
    if not house():
        for n in model.nodes.values():
            if n.role == "Link" and n.page == page and n.bbox is not None \
                    and (n.annots or "xref" in (n.style or {})):
                if intersection(box, n.bbox) > 0.4 * area(box):
                    return True
        return False
    for n in model.nodes.values():
        if n.role == "Link" and (n.annots or "xref" in (n.style or {})):
            # every zone of the link: one address over two lines is one Link
            for r in n.rects_on(page):
                if intersection(box, r) > 0.4 * area(box):
                    return True
    return False


class Scanner:
    """Finds mentions on one document and resolves them against its targets."""

    def __init__(self, model: DocModel, page_lines: list, opts, doc=None) -> None:
        self.m = model
        self.page_lines = page_lines
        self.opts = opts
        self.targets, self.pages = collect_targets(model, page_lines, doc)
        self.owners = _owner_index(model)
        self.section_kind = _section_kinds(model)
        self.to_pdf = None                 # set by find()
        # footnotes / endnotes by their mark, in reading order
        self.order = {n.id: i for i, n in enumerate(model.ordered_nodes())}
        self.notes: dict[str, list[StructNode]] = collections.defaultdict(list)
        for n in model.ordered_nodes():
            if n.role == "Note" and not n.artifact and n.bbox is not None:
                # the note's mark: its Lbl ("1" + "Department of …"), or the
                # start of its own text ("1 Department of …")
                lbl = next((model.nodes[c] for c in n.children
                            if c in model.nodes and model.nodes[c].role == "Lbl"), None)
                mark = _norm(lbl.text) if lbl is not None and house() else ""
                if re.fullmatch(r"\d{1,3}|[*\u2020\u2021\u00a7\u00b6]{1,3}", mark):
                    self.notes[mark].append(n)
                    continue
                mm = NOTE_LABEL_RE.match(_norm(n.text))
                if mm:
                    self.notes[mm.group("n")].append(n)
        self.note_labels: dict[str, tuple] = {}       # note id -> (page, box, mark)
        # headings by their words: a contents line with no page number (a group
        # title such as "Placement") still has somewhere to go
        self.heading_titles: dict[str, StructNode] = {}
        for n in model.ordered_nodes():
            if (n.is_heading or n.role == "H") and not n.artifact and n.bbox is not None \
                    and not any(a.role in ("TOC", "TOCI") for a in model.ancestors(n.id)):
                self.heading_titles.setdefault(_norm(n.text).lower().rstrip(":. "), n)
        self.caption_labels: list[tuple] = []         # (page, box, key, caption id)

    # -------------------------------------------------- per-kind resolution
    def _target_for(self, key: str) -> dict | None:
        t = self.targets.get(key)
        if t is None:
            return None
        return {"page": t.page, "top": t.top, "node": t.node, "label": t.label}

    def _owner_role(self, il, pno, a, b) -> str:
        box = _span_box(il, a, b)
        if box is None:
            return ""
        owner = _owner_of(self.owners.get(pno, []), self.to_pdf(pno, box))
        return owner.role if owner is not None else ""

    def _is_self(self, owner: StructNode, target: dict | None) -> bool:
        """A heading "Chapter 2", the caption "Table 1" and the entry "[2] …" each
        contain the words that name them. Those are definitions, not references."""
        tid = (target or {}).get("node")
        if not tid:
            return False
        if owner.id == tid:
            return True
        return any(x.id == tid for x in self.m.ancestors(owner.id))

    def _in_bibliography(self, owner: StructNode) -> bool:
        if not house():
            return False                  # V4 links a citation wherever it stands
        for x in [owner] + self.m.ancestors(owner.id):
            if x.role == "BibEntry" or (x.role == "L" and (x.style or {}).get("bibliography")):
                return True
        return False

    def _page_target(self, printed: str) -> dict | None:
        pno = self.pages.get(printed.lower())
        if pno is None and printed.isdigit():
            # no printed numbers anywhere: fall back to the sheet itself, but
            # only when the document is numbered from its first page
            if not self.pages and 1 <= int(printed) <= self.m.page_count:
                pno = int(printed) - 1
        if pno is None:
            return None
        return {"page": pno, "top": None, "node": "", "label": f"page {printed}"}

    # ------------------------------------------------------------- scanning
    def find(self, to_pdf) -> list[Mention]:
        """to_pdf(page_no, display_box) -> PDF-space box."""
        self.to_pdf = to_pdf
        out: list[Mention] = []
        for pno, lines in enumerate(self.page_lines):
            cands = self.owners.get(pno, [])
            if not cands:
                continue
            for line in lines:
                il = index_line(line)
                if not il.text:
                    continue
                for mt in self._matches(il, pno):
                    out.append(mt)
                self._note_label(il, pno)
            out.extend(self._across_lines(lines, pno))
        self._group_titles(out)
        out.extend(self._back_links(out))
        return out

    def _next_entry_target(self, owner: StructNode) -> dict | None:
        """The page the next numbered contents entry after this one points at."""
        item = next((a for a in [owner] + self.m.ancestors(owner.id) if a.role == "TOCI"), None)
        if item is None or item.parent not in self.m.nodes:
            return None
        sibs = self.m.nodes[item.parent].children
        for sid in sibs[sibs.index(item.id) + 1:]:
            sub = self.m.nodes.get(sid)
            if sub is None:
                continue
            words = _norm(" ".join(d.text or "" for d in [sub] + self.m.descendants(sid)))
            mm = TRAILING_PAGE_RE.search(words)
            if mm:
                return self._page_target(mm.group("n"))
        return None

    @staticmethod
    def _group_titles(found: list[Mention]) -> None:
        """A contents group title ("Progress Monitoring", no page number) goes
        where its first entry goes — unless a heading of that name comes first."""
        toc = [m for m in found if m.kind == "toc"]
        for i, mn in enumerate(toc):
            if not mn.key.startswith("heading "):
                continue
            nxt = next((t for t in toc[i + 1:] if not t.key.startswith("heading ") and t.target
                        and t.target.get("page") is not None), None)
            if nxt is None:
                continue
            if mn.target is None or (mn.target.get("page") or 0) > nxt.target["page"]:
                mn.target = dict(nxt.target)
                mn.note = "contents group title — goes where its first entry goes"

    # ----------------------------------------------------- notes, both ways
    def _note_for(self, mark: str, owner: StructNode, pno: int) -> StructNode | None:
        """The note a mark in the text points at: a footnote on the same page,
        or else the next note with that mark (endnotes at a chapter's end)."""
        cands = self.notes.get(mark) or []
        same = [n for n in cands if n.page == pno]
        here = self.order.get(owner.id, -1)
        if same:
            # two tables on a page, each with its "*": the note after the mark
            after = [n for n in same if self.order.get(n.id, -1) > here] if house() else []
            return after[0] if after else same[0]
        later = [n for n in cands if self.order.get(n.id, -1) > here]
        return later[0] if later else None

    def _superscripts(self, il: _IndexedLine, pno: int, free):
        """Footnote marks in running text: digits (or * †) set small and raised."""
        text, boxes = il.text, il.boxes
        letters = [(b[3] - b[1], b[3]) for ch, b in zip(text, boxes) if b is not None and ch.isalpha()]
        # a client's house style (Kriyadocs) links the note signs in table cells too
        cells_too = (getattr(self.opts, "house_style", "") or "").lower() not in ("", "none", "off")
        if len(letters) < 3 and cells_too:
            # a table cell's figures ("<0.001*", "59.7±10.7†"): measured on the digits
            sizes = [b[3] - b[1] for ch, b in zip(text, boxes) if b is not None and ch.isalnum()]
            top = max(sizes) if sizes else 0
            letters = [(b[3] - b[1], b[3]) for ch, b in zip(text, boxes)
                       if b is not None and ch.isalnum() and b[3] - b[1] > 0.9 * top]
        # Kriyadocs (V4.17): a raised number with no note of its own is a
        # citation of the reference list ("…in the region.³⁵ ³⁷ ³⁸")
        cites = cells_too and house() and any(k.startswith("ref ") and k[4:].isdigit() for k in self.targets)
        if len(letters) < 3 or not (self.notes or cites):
            return
        h = statistics.median(x[0] for x in letters)
        base = statistics.median(x[1] for x in letters)

        def raised(i):
            b = boxes[i]
            return (b is not None and text[i] in SUPER_CHARS and (b[3] - b[1]) <= 0.82 * h
                    and b[3] <= base - 0.16 * h)

        def symbol(i):
            # "<0.001*", "0.281†": a note sign set straight after a figure —
            # the glyph sits high by itself, it need not be raised
            return (cells_too and text[i] in SYMBOL_MARKS and i > 0 and not text[i - 1].isspace()
                    and (i + 1 == len(text) or not text[i + 1].isalnum()))

        def cand(i):
            return raised(i) or symbol(i)
        i = 0
        while i < len(text):
            if not cand(i) or i == 0:
                i += 1
                continue
            j = i + 1
            while j < len(text) and cand(j) and text[j].isdigit() == text[i].isdigit():
                j += 1
            if cites and text[i].isdigit() and not text[i - 1].isdigit() \
                    and j + 1 < len(text) and text[j] in CITE_DASHES \
                    and boxes[j] is not None and cand(j + 1) and text[j + 1].isdigit():
                # a raised range "³⁸⁻⁴⁰": one zone, one link
                k = j + 1
                while k < len(text) and cand(k) and text[k].isdigit():
                    k += 1
                rng = text[i:k]
                ref = self._target_for(f"ref {int(text[i:j])}")
                if ref is not None and free(i, k):
                    mn = self._emit(il, pno, i, k, "reference", f"ref {int(text[i:j])}", ref, "", 0.85)
                    if mn is not None:
                        mn.text = rng
                        yield i, k, mn
                i = k
                continue
            mark = text[i:j]
            only_sign = not all(raised(k) for k in range(i, j))
            # "1070 per km²", "5 m³": a unit's power, not a note
            if house() and mark in ("2", "3") and re.search(r"(?:\b(?:k|c|m|µ|n|d)?m|\bft|\bin|\bmi|/m|/km)$", text[:i]):
                i = j
                continue
            if free(i, j):
                box = _span_box(il, i, j)
                owner = _note_host(self.m, _owner_of(self.owners.get(pno, []), self.to_pdf(pno, box))) if box else None
                in_cell = owner is not None and any(
                    x.role in ("TD", "TH") for x in [owner] + self.m.ancestors(owner.id))
                if only_sign and not in_cell:
                    owner = None
                if owner is not None and owner.role not in ("Note", "Formula", "Lbl", "TOCI"):
                    note = self._note_for(mark, owner, pno) if self.notes else None
                    if note is not None and cites and (note.note or "").startswith("affiliation") \
                            and not any((x.style or {}).get("house") == "authors"
                                        for x in [owner] + self.m.ancestors(owner.id)):
                        note = None             # only the authors' marks go to the affiliations
                    if note is None and cites and mark.isdigit() and not in_cell \
                            and not (text[i - 1].isdigit() or text[i - 1] in "×x^·/="):   # not a power: 10⁶
                        ref = self._target_for(f"ref {int(mark)}")
                        if ref is not None:
                            mn = self._emit(il, pno, i, j, "reference", f"ref {int(mark)}", ref, "", 0.8)
                            if mn is not None:
                                yield i, j, mn
                        i = j
                        continue
                    if note is not None:
                        tgt = {"page": note.page, "top": note.bbox[3], "node": note.id,
                               "label": f"note {mark}"}
                        mn = self._emit(il, pno, i, j, "footnote", f"note {mark}", tgt, "", 0.85)
                        if mn is not None:
                            yield i, j, mn
            i = j

    def _note_label(self, il: _IndexedLine, pno: int) -> None:
        """Remember where a note prints its own mark — the back link starts there."""
        mm = NOTE_LABEL_RE.match(il.text)
        if not mm:
            return
        a, b = mm.span("n")
        box = _span_box(il, a, b)
        if box is None:
            return
        pdf_box = self.to_pdf(pno, box)
        owner = _note_host(self.m, _owner_of(self.owners.get(pno, []), pdf_box))
        if owner is None or owner.role != "Note" or owner.id in self.note_labels:
            return
        if owner.bbox is not None and pdf_box[3] < owner.bbox[3] - 1.2 * max(4.0, il.size or 10.0):
            return                              # not the note's first line
        self.note_labels[owner.id] = (pno, pdf_box, mm.group("n"))

    def _back_links(self, found: list[Mention]) -> list[Mention]:
        """A note's mark goes back to where the text refers to it; a caption's
        "Figure 3" goes back to where the text first mentions Figure 3."""
        out: list[Mention] = []
        first: dict[str, Mention] = {}
        for mn in found:
            if mn.kind == "footnote" and mn.target:
                nid = mn.target.get("node")
                if nid and nid not in first:
                    first[nid] = mn
        for nid, mn in first.items():
            lab = self.note_labels.get(nid)
            if lab is None or _already_linked(self.m, lab[0], lab[1]):
                continue
            out.append(Mention(kind="footnote_back", page=lab[0], rects=[lab[1]],
                               text=lab[2], key=f"note {lab[2]}", owner=nid,
                               target={"page": mn.page, "top": mn.rects[0][3], "node": mn.owner,
                                       "label": f"the text of note {lab[2]}"},
                               note="back to the footnote reference", conf=0.85))
        firsts: dict[str, Mention] = {}
        for mn in sorted((m for m in found if m.kind == "figure" and m.target),
                         key=lambda m: (self.order.get(m.owner, 1 << 30), m.page,
                                        -(m.rects[0][3] if m.rects else 0))):
            if any(x.role in ("TOC", "TOCI") for x in [self.m.nodes[mn.owner]] +
                   self.m.ancestors(mn.owner)):
                continue                        # a list of figures is not the text
            firsts.setdefault(mn.key, mn)
        for pno, box, key, cap in self.caption_labels:
            mn = firsts.get(key)
            if mn is None or mn.owner == cap or _already_linked(self.m, pno, box):
                continue
            out.append(Mention(kind="caption_back", page=pno, rects=[box],
                               text=key.title(), key=key, owner=cap,
                               target={"page": mn.page, "top": mn.rects[0][3], "node": mn.owner,
                                       "label": f"where {key.title()} is mentioned"},
                               note="back to where the text mentions it", conf=0.8))
        return out

    def _across_lines(self, lines: list, pno: int) -> list[Mention]:
        """A citation broken over two lines ("… Moats," / "2005/2006; …") is one
        mention: the link gets a zone on each line."""
        out: list[Mention] = []
        for l1, l2 in zip(lines, lines[1:]):
            size = max(getattr(l1, "size", 10.0) or 10.0, 6.0)
            if not (l2.y0 > l1.y0 + 0.4 * size and l2.y0 - l1.y1 < 1.2 * size
                    and l2.x0 < l1.x1 and l2.x1 > l1.x0):
                continue                       # not the next line of the same column
            a1, a2 = index_line(l1), index_line(l2)
            if not a1.text or not a2.text:
                continue
            joined = a1.text + " " + a2.text
            cut = len(a1.text) + 1
            for rx, kind in ((AUTHOR_YEAR_RE, "reference"), (FIG_RE, "figure"), (SECT_RE, "section")):
                for m in rx.finditer(joined):
                    a, b = m.span()
                    if not (a < len(a1.text) and b > cut):
                        continue               # the one-line search already saw it
                    if kind == "reference" and joined[b - 1] == ")" and "(" not in joined[a:b]:
                        b -= 1
                    if kind == "reference":
                        key = f"ref {m.group('a').lower()} {m.group('y')}"
                    else:
                        key = _key(m.group("w"), m.group("n"))
                    tgt = self._target_for(key)
                    if tgt is None:
                        continue
                    r1, r2 = _span_box(a1, a, len(a1.text)), _span_box(a2, 0, b - cut)
                    if r1 is None or r2 is None:
                        continue
                    p1, p2 = self.to_pdf(pno, r1), self.to_pdf(pno, r2)
                    owner = _owner_of(self.owners.get(pno, []), p1)
                    if owner is None or self._is_self(owner, tgt) or _already_linked(self.m, pno, p1) \
                            or (kind == "reference" and self._in_bibliography(owner)):
                        continue
                    out.append(Mention(kind=kind, page=pno, rects=[p1, p2],
                                       text=_norm(joined[a:b]), key=key, owner=owner.id,
                                       target=tgt, note="runs over two lines", conf=0.7))
        return out

    def _emit(self, il, pno, a, b, kind, key, target, note="", conf=0.8) -> Mention | None:
        box = _span_box(il, a, b)
        if box is None:
            return None
        pdf_box = self.to_pdf(pno, box)
        owner = _owner_of(self.owners.get(pno, []), pdf_box)
        if owner is None:
            return None
        if _already_linked(self.m, pno, pdf_box):
            return None
        if self._is_self(owner, target):
            return None            # the heading, caption or entry naming itself
        if kind == "reference" and self._in_bibliography(owner):
            return None            # "… Accra, Ghana, 2020." in an entry cites nothing
        return Mention(kind=kind, page=pno, rects=[pdf_box], text=il.text[a:b].strip(),
                       key=key, owner=owner.id, target=target, note=note, conf=conf)

    def _matches(self, il: _IndexedLine, pno: int):
        text = il.text
        taken: list[tuple[int, int]] = []        # spans already claimed on this line

        def free(a, b):
            return not any(a < y and x < b for x, y in taken)

        for rx, kind in ((URL_RE, "web"), (EMAIL_RE, "email")):
            for m in rx.finditer(text):
                a, b = m.span()
                while b > a and text[b - 1] in ".,;:)]}>":
                    b -= 1
                if not free(a, b):
                    continue
                raw = text[a:b]
                uri = raw if "://" in raw else (f"mailto:{raw}" if kind == "email" else f"https://{raw}")
                mn = self._emit(il, pno, a, b, kind, raw.lower(),
                                {"uri": uri, "label": raw}, conf=0.95)
                taken.append((a, b))
                if mn is not None:
                    yield mn

        for m in SECT_RE.finditer(text):
            a, b = m.span()
            if not free(a, b):
                continue
            key = _key(m.group("w"), m.group("n"))
            tgt = self._target_for(key)
            mn = self._emit(il, pno, a, b, "section", key, tgt,
                            "" if tgt else "no heading with this number was found",
                            0.85 if tgt else 0.3)
            if mn is not None:
                taken.append((a, b))
                yield mn
        for m in PARA_RE.finditer(text):
            a, b = m.span()
            if not free(a, b):
                continue
            key = f"section {m.group('n')}"
            tgt = self._target_for(key)
            mn = self._emit(il, pno, a, b, "section", key, tgt,
                            "" if tgt else "no heading with this number was found",
                            0.85 if tgt else 0.3)
            if mn is not None:
                taken.append((a, b))
                yield mn

        for m in FIG_RE.finditer(text):
            a, b = m.span()
            if not free(a, b):
                continue
            key = _key(m.group("w"), m.group("n"))
            if a <= 2 and self._owner_role(il, pno, a, b) == "Caption":
                # the caption's own label, not a reference — the link back starts here
                box = _span_box(il, a, b)
                if box is not None:
                    cap = _owner_of(self.owners.get(pno, []), self.to_pdf(pno, box))
                    if cap is not None:
                        self.caption_labels.append((pno, self.to_pdf(pno, box), key, cap.id))
                taken.append((a, b))
                continue
            tgt = self._target_for(key)
            mn = self._emit(il, pno, a, b, "figure", key, tgt,
                            "" if tgt else "no figure/table with this number was found",
                            0.85 if tgt else 0.3)
            if mn is not None:
                taken.append((a, b))
                yield mn

        for m in BRACKET_REF_RE.finditer(text):
            a, b = m.span()
            if not free(a, b):
                continue
            nums = [int(x) for x in re.findall(r"\d{1,3}", m.group("body"))]
            if not nums:
                continue
            if house():
                # Kriyadocs (V4.17): "[35, 37, 38]" — one zone and one link per
                # number, "[38–40]" one for the range; the brackets stay text
                parts = list(CITE_PART_RE.finditer(m.group("body")))
                if len(parts) > 1 or (parts and parts[0].group(0) != m.group("body")):
                    off = m.start("body")
                    for pt in parts:
                        pa, pb = off + pt.start(), off + pt.end()
                        first = re.match(r"\d+", pt.group(0)).group(0)
                        key = f"ref {int(first)}"
                        tgt = self._target_for(key)
                        mn = self._emit(il, pno, pa, pb, "reference", key, tgt,
                                        "" if tgt else "no bibliography entry with this number was found",
                                        0.8 if tgt else 0.3)
                        if mn is not None:
                            taken.append((pa, pb))
                            yield mn
                    taken.append((a, b))
                    continue
            key = f"ref {nums[0]}"
            tgt = self._target_for(key)
            mn = self._emit(il, pno, a, b, "reference", key, tgt,
                            "" if tgt else "no bibliography entry with this number was found",
                            0.8 if tgt else 0.3)
            if mn is not None:
                taken.append((a, b))
                yield mn
        for m in AUTHOR_YEAR_RE.finditer(text):
            a, b = m.span()
            if text[b - 1] == ")" and "(" not in text[a:b]:
                b -= 1                        # "(Hasbrouck, 2010)": the bracket is not the name
            if not free(a, b):
                continue
            key = f"ref {m.group('a').lower()} {m.group('y')}"
            tgt = self._target_for(key)
            if tgt is None:
                continue                      # author-year guesses are only kept when they land
            mn = self._emit(il, pno, a, b, "reference", key, tgt, "", 0.7)
            if mn is not None:
                taken.append((a, b))
                yield mn

        for m in PAGE_REF_RE.finditer(text):
            a, b = m.span()
            if not free(a, b):
                continue
            tgt = self._page_target(m.group("n"))
            mn = self._emit(il, pno, a, b, "page", f"page {m.group('n')}", tgt,
                            "" if tgt else "that printed page number was not found",
                            0.8 if tgt else 0.3)
            if mn is not None:
                taken.append((a, b))
                yield mn

        # footnote marks: small raised digits after a word
        for a, b, mn in self._superscripts(il, pno, free):
            taken.append((a, b))
            yield mn

        # contents and index lines: the trailing number is the destination
        yield from self._leader_lines(il, pno, taken)

    def _section_of(self, owner: StructNode) -> tuple[bool, bool]:
        anc = [owner] + self.m.ancestors(owner.id)
        kinds = {self.section_kind.get(x.id, "") for x in anc}
        in_toc = any(x.role in ("TOC", "TOCI") for x in anc) or "toc" in kinds
        in_index = any(x.role == "Index" for x in anc) or "index" in kinds
        return in_toc, in_index

    def _leader_lines(self, il: _IndexedLine, pno: int, taken: list):
        text = il.text
        if re.fullmatch(r"\s*[A-Z](?:\s*[\u2013\u2014-]\s*[A-Z])?\s*", text):
            return                                  # an index letter divider, not page "D"
        m = TRAILING_PAGE_RE.search(text)
        a = b = -1
        in_toc = in_index = False
        if m:
            a, b = m.span()
            box = _span_box(il, a, b)
            owner = _owner_of(self.owners.get(pno, []), self.to_pdf(pno, box)) if box else None
            if owner is not None:
                in_toc, in_index = self._section_of(owner)
        if not m:
            # a contents line with no page number: its title names a heading
            a0 = len(text) - len(text.lstrip())
            box = _span_box(il, a0, len(text))
            owner = _owner_of(self.owners.get(pno, []), self.to_pdf(pno, box)) if box else None
            if owner is not None and self._section_of(owner)[0] and len(text.strip()) > 2 \
                    and not any(a0 < y and x < len(text) for x, y in taken):
                h = self.heading_titles.get(_norm(text).lower().rstrip(":. "))
                tgt = None
                if h is not None and h.page != pno:
                    tgt = {"page": h.page, "top": h.bbox[3], "node": h.id, "label": _norm(h.text)[:80]}
                # where the group's first entry goes; a heading of that name only
                # wins when it comes no later (a later one is a running title)
                first = self._next_entry_target(owner)
                if first is not None and (tgt is None or tgt["page"] > first["page"]):
                    tgt = dict(first, label=_norm(text)[:80])
                mn = self._emit(il, pno, a0, len(text), "toc", f"heading {_norm(text).lower()}", tgt,
                                "contents group title", 0.8)
                if mn is not None:
                    yield mn
                return
        if not (in_toc or in_index):
            # an index line can end with a comma when its page numbers wrap
            # ("of spelling, 3," / "4"): look at the line's first number instead
            mm = INDEX_PAGES_RE.search(text)
            if mm is None:
                return
            box = _span_box(il, *mm.span())
            owner = _owner_of(self.owners.get(pno, []), self.to_pdf(pno, box)) if box else None
            if owner is None:
                return
            in_toc, in_index = self._section_of(owner)
            if not in_index or in_toc:
                return
            m = None
        if m is not None and not any(a < y and x < b for x, y in taken):
            kind = "toc" if in_toc else "index"
            tgt = self._page_target(m.group("n"))
            if tgt is None and in_toc:
                # no printed numbers: match the entry title against a heading
                title = _norm(re.sub(r"[.…·_\s]{2,}.*$", "", text))
                for t in self.targets.values():
                    if title and t.label and _norm(t.label).lower().startswith(title.lower()[:40]):
                        tgt = {"page": t.page, "top": t.top, "node": t.node, "label": t.label}
                        break
            mn = self._emit(il, pno, a, b, kind, f"page {m.group('n')}", tgt,
                            "" if tgt else "that printed page number was not found",
                            0.9 if tgt else 0.3)
            if mn is not None:
                yield mn
        if in_index:
            for mm in INDEX_PAGES_RE.finditer(text):
                x, y = mm.span()
                if (x, y) == (a, b) or any(x < q and p < y for p, q in taken):
                    continue
                t2 = self._page_target(mm.group("n"))
                if t2 is None:
                    continue
                m2 = self._emit(il, pno, x, y, "index", f"page {mm.group('n')}", t2, "", 0.85)
                if m2 is not None:
                    yield m2


# ------------------------------------------------------------- model edits
def apply(model: DocModel, mentions: list[Mention], opts) -> dict:
    """Put Reference / Link elements into the tree for the mentions found."""
    stats: dict = {"found": len(mentions), "tagged": 0, "linked": 0,
                   "by_kind": collections.Counter(), "linked_by_kind": collections.Counter(),
                   "unresolved": 0}
    want_tag = bool(getattr(opts, "cross_reference_tags", False))
    for mn in mentions:
        stats["by_kind"][mn.kind] += 1
        if mn.target is None:
            stats["unresolved"] += 1
        owner = model.nodes.get(mn.owner)
        if owner is None:
            continue
        link_on = bool(getattr(opts, KIND_OPTION.get(mn.kind, ""), False))
        make_link = link_on and mn.target is not None
        internal = mn.kind not in ("web", "email")
        if not make_link and not (want_tag and internal):
            continue
        host = owner
        if want_tag and internal:
            ref = StructNode(id=model.new_id("xref"), role="Reference", page=mn.page,
                             bbox=mn.rects[0], text=mn.text, confidence=mn.conf,
                             note=f"cross-reference to {mn.key}")
            ref.parts = [[mn.page, *r] for r in mn.rects[1:]]
            ref.style = {"xref_kind": mn.kind}
            model.add(ref, owner.id)
            host = ref
            stats["tagged"] += 1
        if make_link:
            ln = StructNode(id=model.new_id("xlink"), role="Link", page=mn.page,
                            bbox=mn.rects[0], text=mn.text, confidence=mn.conf,
                            note=f"{KIND_LABEL.get(mn.kind, mn.kind)} — link created by the tool")
            ln.parts = [[mn.page, *r] for r in mn.rects[1:]]
            ln.style = {"xref": {"kind": mn.kind, "key": mn.key, **(mn.target or {})}}
            model.add(ln, host.id)
            stats["linked"] += 1
            stats["linked_by_kind"][mn.kind] += 1
    for oid in {mn.owner for mn in mentions}:
        if oid in model.nodes:
            _sort_inline(model, model.nodes[oid])
    stats["by_kind"] = dict(stats["by_kind"])
    stats["linked_by_kind"] = dict(stats["linked_by_kind"])
    return stats


def _made_here(n: StructNode) -> bool:
    st = n.style or {}
    return (n.role == "Link" and "xref" in st) or (n.role == "Reference" and "xref_kind" in st)


def _sort_inline(model: DocModel, owner: StructNode) -> None:
    """The links this module put into a tag, in the order they are printed:
    line by line, left to right ("decoding 3, 4" reads 3 before 4). The tag's
    other children keep their places."""
    slots = [i for i, c in enumerate(owner.children)
             if c in model.nodes and _made_here(model.nodes[c]) and model.nodes[c].bbox is not None]
    if len(slots) < 2:
        return
    nodes = [model.nodes[owner.children[i]] for i in slots]
    bands: list[list] = []                       # [page, top, bottom]
    band_of: dict[str, int] = {}
    for n in sorted(nodes, key=lambda n: (n.page, -(n.bbox[1] + n.bbox[3]) / 2)):
        y0, y1 = n.bbox[1], n.bbox[3]
        for k, b in enumerate(bands):
            if b[0] == n.page and min(b[2], y1) - max(b[1], y0) > 0.3 * min(y1 - y0, b[2] - b[1]):
                b[1], b[2] = min(b[1], y0), max(b[2], y1)
                band_of[n.id] = k
                break
        else:
            bands.append([n.page, y0, y1])
            band_of[n.id] = len(bands) - 1
    nodes.sort(key=lambda n: (n.page, band_of[n.id], n.bbox[0]))
    for i, n in zip(slots, nodes):
        owner.children[i] = n.id


def find_and_apply(model: DocModel, page_lines: list, doc, opts) -> dict:
    """Analysis entry point: scan the pages, record and tag what was found."""
    from .analyzer import to_pdf as _to_pdf

    def to_pdf(pno, box):
        return _to_pdf(doc[pno], box)

    sc = Scanner(model, page_lines, opts, doc)
    mentions = sc.find(to_pdf)
    stats = apply(model, mentions, opts)
    stats["targets"] = len(sc.targets)
    stats["printed_pages"] = len(sc.pages)
    model.info["xref"] = stats
    model.info["xrefs"] = [mn.to_dict() for mn in mentions]
    return stats


# ------------------------------------------------------- annotation writing
def _dest(pdf, page, top):
    from pikepdf import Array, Name
    if top is None:
        return Array([page.obj, Name.Fit])
    return Array([page.obj, Name.XYZ, 0, round(float(top) + 6, 2), 0])


def create_annotations(pdf, model: DocModel, opts) -> dict:
    """Write the link annotations for the Link elements this module added.

    Called from build before the structure is written, so each new annotation
    already has its object number when the OBJR entries are made."""
    from pikepdf import Array, Dictionary, Name, String

    stats = {"created": 0, "by_kind": collections.Counter(), "failed": 0}
    for n in model.ordered_nodes():
        if n.role != "Link" or n.annots:
            continue
        x = (n.style or {}).get("xref")
        if not isinstance(x, dict):
            continue
        rects = ([n.bbox] + [tuple(p[1:]) for p in n.parts]) if n.bbox else []
        if not rects or n.page < 0 or n.page >= len(pdf.pages):
            continue
        try:
            page = pdf.pages[n.page]
            made = []
            for r in rects:
                a = Dictionary(Type=Name.Annot, Subtype=Name.Link,
                               Rect=Array([round(v, 2) for v in r]),
                               Border=Array([0, 0, 0]), F=4)       # 4 = Print, required by PDF/UA
                if x.get("uri"):
                    a.A = Dictionary(S=Name.URI, URI=String(str(x["uri"])))
                    label = str(x.get("label") or n.text)
                    a.Contents = String(f"Email {label}" if x.get("kind") == "email"
                                        else f"Link to {label}")
                else:
                    tp = int(x.get("page", -1))
                    if not 0 <= tp < len(pdf.pages):
                        continue
                    a.A = Dictionary(S=Name.GoTo, D=_dest(pdf, pdf.pages[tp], x.get("top")))
                    kind = x.get("kind", "")
                    what = str(x.get("key") if kind == "reference" else
                               (x.get("label") or x.get("key") or "this document"))
                    if kind in ("footnote_back", "caption_back", "manual_back"):
                        a.Contents = String(f"Back to {what}"[:200])
                    else:
                        a.Contents = String(f"Go to {what}"[:200])
                ind = pdf.make_indirect(a)
                arr = page.obj.get("/Annots")
                if not isinstance(arr, Array):
                    page.obj.Annots = pdf.make_indirect(Array([ind]))
                else:
                    arr.append(ind)
                made.append(ind.objgen[0])
            if made:
                n.annots = made
                stats["created"] += len(made)
                stats["by_kind"][x.get("kind", "?")] += 1
        except Exception:
            stats["failed"] += 1
    stats["by_kind"] = dict(stats["by_kind"])
    return stats


# ------------------------------------------------------------------ rescan
def dead_links(model: DocModel) -> int:
    """A Link tag with no link annotation behind it (PDF/UA 7.18.5, PAC "Link
    element without a link annotation") keeps its words as a Span, so the search
    can make it a real link."""
    n_ = 0
    for n in model.nodes.values():
        if n.role == "Link" and not n.annots and "xref" not in (n.style or {}):
            n.role = "Span"
            n.note = (n.note + "; " if n.note else "") + "was a Link with no link annotation"
            n_ += 1
    return n_


def clear(model: DocModel) -> int:
    """Remove the Reference / Link elements this module added (not the ones that
    came from real annotations in the file, which have `annots`)."""
    gone = 0
    for n in list(model.nodes.values()):
        st = n.style or {}
        if n.id not in model.nodes:
            continue
        if n.role == "Link" and not n.annots and "xref" not in st:
            continue                          # dead_links() below
        if st.get("manual"):
            continue                          # set by hand in the Links stage: kept
        if n.role == "Link" and "xref" in st and not n.annots:
            model.detach(n.id)
            model._drop_subtree(n.id)
            gone += 1
        elif n.role == "Reference" and "xref_kind" in st:
            # keep the children (the link may have been kept by the user)
            parent, sibs = n.parent, model.siblings(n.id)
            pos = sibs.index(n.id) if n.id in sibs else None
            kids = list(n.children)
            model.detach(n.id)
            for k in kids:
                model.detach(k)
                model.add(model.nodes[k], parent, pos)
                if pos is not None:
                    pos += 1
            model.nodes.pop(n.id, None)
            gone += 1
    return gone


def rescan(model: DocModel, pdf_path: str, opts) -> dict:
    """Editor: find the cross-references again with the options as they are now."""
    import pymupdf

    from .compat import use as use_engine
    use_engine(opts)

    from .analyzer import collect_lines
    clear(model)
    dead_links(model)
    doc = pymupdf.open(pdf_path)
    try:
        for p in doc:
            if p.rotation:
                p.set_rotation(0)
        page_lines = [collect_lines(p) for p in doc]
        stats = find_and_apply(model, page_lines, doc, opts)
        if (getattr(opts, "house_style", "") or "").lower() not in ("", "none", "off"):
            # the client's own link structure again (Reference > Link …)
            try:
                from .house import after_rescan
                after_rescan(model, doc, pdf_path, page_lines, opts)
            except Exception as exc:
                model.warnings.append(f"house style links: {exc}")
        return stats
    finally:
        doc.close()
