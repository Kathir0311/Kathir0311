"""Client house styles: the tag structure a publisher asks for (V4.10).

A house style runs inside the analysis, after the pages are read, and turns
the tool's own tagging into the structure that client delivers. It is chosen
with the "House style" option — the client's profile sets it.

kriyadocs — BMJ journal articles as Kriyadocs tags them (InDesign export,
sample: bmjgh-2024-018847):

  Document > Sect (one, holding the whole article; see section_wrap "one")
    P "Original research"                 the article type printed on top
    Figure /Alt "BMJ Open"                the journal's masthead logo
    H1 title
    P authors > Figure /Alt "Orcid icon", Reference > Link (affiliation marks)
    P To cite … · P supplemental (Reference > Link doi) · P Received … ·
    P Accepted … · P > Reference > Link (Check for updates) · P © … ·
    Note > Lbl "1" + "Department …" (one Note per affiliation) ·
    P Correspondence to …
    H2 ABSTRACT, P per abstract section
    H3 box heading + L (Disc) > LI > Lbl "⇒" + LBody   — after the abstract
    H2 / H3 sections, P > Reference > Link for every citation and link
    Figure + Caption > P "Figure 1 …"
    Caption "Table 1 …" + Table > TR > TH / TD (no THead/TBody)
    Note > Lbl "*" + "Mann-Whitney U." (the table's notes), P for the abbreviations
    Figure /Alt "BMJ Group Logo" + P > Link     — the footer logo of page 1
    H3 Author affiliations + Note per affiliation · P Acknowledgements … ·
    H3 ORCID iDs · H2 REFERENCES + L > LI > Lbl "1" + LBody

V4.11 — as the client's own file: "Open access" and the article type printed
on top are a P each; a Lbl inside each Note (the number or mark); every
Formula says the equation in words (/ActualText and /Alt, read from the
layout: stacked fractions, lowered subscripts); a Link carries no /Alt (its
description goes in the annotation's /Contents); the download stamp's link
up the margin is not in the file; white space set on its own is no content
item of its own; empty table cells stay empty; PDF 1.6 at least.
ISO 32000-1 (Table 333) names Lbl as a child of LI only, so PAC may report
the Lbl inside a Note — the client asks for it.

V4.12 — each author's affiliation mark and each note sign in a table cell is
Reference > Link to its Note, named for where it goes ("Affiliation 1: …");
the formula is said as a sentence ("the average of", "times the sum of");
and what is drawn or converted in the editor gets this structure too
(after_edit, called by the web app after every edit of a Kriyadocs job).

V4.14 — every Note is Note > Lbl (the number or mark) + P (the text), nothing
loose in the Note (note_text_p, run from post() so it also follows every edit).
"""
from __future__ import annotations

import re
import statistics

import pymupdf

from .model import StructNode

HOUSE_STYLES = ["none", "kriyadocs"]

# the journals' abbreviated names (as printed in "Yan Y, et al. BMJ Open 2023;…")
BMJ_JOURNALS = {
    "BMJ Glob Health": "BMJ Global Health",
    "BMJ Open Qual": "BMJ Open Quality",
    "BMJ Open Respir Res": "BMJ Open Respiratory Research",
    "BMJ Open Sport Exerc Med": "BMJ Open Sport & Exercise Medicine",
    "BMJ Open Diabetes Res Care": "BMJ Open Diabetes Research & Care",
    "BMJ Open Gastroenterol": "BMJ Open Gastroenterology",
    "BMJ Open Ophthalmol": "BMJ Open Ophthalmology",
    "BMJ Paediatr Open": "BMJ Paediatrics Open",
    "BMJ Ment Health": "BMJ Mental Health",
    "BMJ Neurol Open": "BMJ Neurology Open",
    "BMJ Nutr Prev Health": "BMJ Nutrition, Prevention & Health",
    "BMJ Health Care Inform": "BMJ Health & Care Informatics",
    "BMJ Med": "BMJ Medicine",
    "BMJ Evid Based Med": "BMJ Evidence-Based Medicine",
    "BMJ Qual Saf": "BMJ Quality & Safety",
    "BMJ Support Palliat Care": "BMJ Supportive & Palliative Care",
    "BMJ Mil Health": "BMJ Military Health",
    "BMJ Case Rep": "BMJ Case Reports",
    "Gen Psychiatr": "General Psychiatry",
    "Fam Med Com Health": "Family Medicine and Community Health",
    "J Immunother Cancer": "Journal for ImmunoTherapy of Cancer",
    "RMD Open": "RMD Open",
    "ESMO Open": "ESMO Open",
}
JOURNAL_RE = re.compile(r"et al\.?,?\s+(?P<j>[A-Z][A-Za-z&\- ]{1,60}?)\s+(?:19|20)\d\d\s*;")
DATE_LINE_RE = re.compile(r"^\s*(Received|Revised|Accepted|Published(?: Online First)?|First published)"
                          r"(?: on)?:?\s+\d", re.I)
ARROW_LABELS = "►▶➤▸▷→⇒•●"
BULLET_LABELS = "⇒→•●▪■◦○►▶➤▸–—-"
NOTE_MARK_RE = re.compile(r"^\s*(?P<m>[*†‡§¶#]{1,3}|\*\*)\s*(?=\S)")
TABLE_CONTINUED_RE = re.compile(r"^\s*(?:table|tab\.)\s*[\dIVX]+[A-Za-z]?\s*\(?\s*continued\b", re.I)
CONTINUED_RE = re.compile(r"^\s*\(?continued(?:\s+(?:on|overleaf)\b.*)?\)?\s*$", re.I)
AFFIL_WORDS = re.compile(r"\b(Department|Dept|University|School|Faculty|Institute|Hospital|Centre|Center|"
                         r"College|Unit|Division|Laboratory|Programme|Program|Ministry|Foundation|Clinic)\b")
OPEN_END = (".", "?", "!", ":", ";", "”", '"', ")")
SIDEBAR_START_RE = re.compile(r"^(To cite|Handling editor|Prepublication|Additional supplemental|Received|Revised|"
                              r"Accepted|©|For numbered|Correspondence|Twitter|X @)")
SECTION_HEAD_RE = re.compile(r"^(REFERENCES|ABSTRACT|INTRODUCTION|BACKGROUND|METHODS?|RESULTS|DISCUSSION|"
                             r"CONCLUSIONS?|LIMITATIONS|STRENGTHS AND LIMITATIONS)$")
# "Author affiliations" — the fi ligature is often lost ("Author affliations")
AFFIL_HEAD = r"author\s+af{1,2}i?l?iations?"
BACK_HEAD_RE = re.compile(r"(ORCID iDs?|" + AFFIL_HEAD + ")", re.I)
REF_HEAD_RE = re.compile(r"(REFERENCES|References)")
RUN_IN_RE = re.compile(r"^(Acknowledgements?|Contributors|Funding|Competing interests|Patient and public involvement|"
                       r"Patient consent for publication|Ethics approval|Provenance and peer review|"
                       r"Data availability statement|Supplemental material|Open access|Author note|"
                       r"Correction notice|Collaborators|Map disclaimer|Disclaimer|Twitter|X)\b")


def apply(an, phase: str) -> dict:
    """Run the house style chosen in the options at `phase` ("pre": before
    the cross-references are matched, "post": after)."""
    style = (getattr(an.opts, "house_style", "") or "").lower()
    if style != "kriyadocs":
        return {}
    k = Kriyadocs(an)
    return {"early": k.early, "pre": k.pre}.get(phase, k.post)()


def after_rescan(model, doc, pdf_path: str, page_lines: list, opts) -> dict:
    """The editor searched the text for links again (Links tab, Fix everything):
    the links it made are wrapped the client's way again, and a Reference left
    empty by the search is taken out."""
    style = (getattr(opts, "house_style", "") or "").lower()
    if style != "kriyadocs":
        return {}
    import types

    import pikepdf
    pdf = pikepdf.open(pdf_path)
    try:
        an = types.SimpleNamespace(model=model, doc=doc, pdf=pdf, page_lines=page_lines, opts=opts)
        k = Kriyadocs(an)
        for n in list(model.nodes.values()):
            if n.role == "Reference" and n.id in model.nodes and not n.children and n.bbox is None:
                model.remove(n.id)
        return k.post()
    finally:
        pdf.close()


class _LazyLines:
    """The printed lines of a page, read only when a page is asked for."""

    def __init__(self, doc):
        self.doc = doc
        self.cache: dict[int, list] = {}

    def __len__(self) -> int:
        return len(self.doc)

    def __getitem__(self, p: int) -> list:
        if p not in self.cache:
            from .analyzer import collect_lines
            try:
                self.cache[p] = collect_lines(self.doc[p])
            except Exception:
                self.cache[p] = []
        return self.cache[p]


EDIT_ROLES = ("Note", "L", "LI", "Lbl", "Formula", "Link", "TD", "TH", "Table", "Caption", "Reference")


def after_edit(model, pdf_path: str, opts, changed: set, gone: set | None = None) -> dict:
    """A tag drawn or converted by hand in the editor, in a client's job, is
    given the client's structure too: a Note gets its Lbl, a bulleted list
    ListNumbering Disc, a Formula the equation in words, a Link its Reference,
    a cell its own text before a list, and the Notes their IDs."""
    style = (getattr(opts, "house_style", "") or "").lower()
    if style != "kriyadocs" or not changed:
        return {}
    touched: set = set()
    for nid in changed:
        if nid in model.nodes:
            touched.add(nid)
            touched.update(d.id for d in model.descendants(nid))
    nodes = [model.nodes[i] for i in touched if i in model.nodes]
    if not gone and not any(n.role in EDIT_ROLES for n in nodes):
        return {}
    import types
    doc = pymupdf.open(pdf_path)
    try:
        an = types.SimpleNamespace(model=model, doc=doc, pdf=None, page_lines=_LazyLines(doc), opts=opts)
        k = Kriyadocs(an)
        for n in nodes:
            if n.id not in model.nodes or n.artifact:
                continue
            if n.role == "Note":
                k.label_note(n)
            elif n.role == "Formula" and n.id in changed:
                # only the Formula just drawn or converted: an /Alt typed by
                # hand on another one is left as it is
                k.speak_formula(n)
        k.lists()
        k.cell_lead_ins()
        k.captions()
        k.renumber_notes()
        if gone:
            # the tags drawn again replaced the citations and note marks that
            # were linked inside them: those words are searched again — only
            # inside the new tags, so a link taken out by hand stays out
            try:
                k.relink({n.id for n in nodes if n.id in model.nodes})
            except Exception as exc:
                model.warnings.append(f"house style: links not searched again ({exc})")
        k.post()
        return k.stats
    finally:
        doc.close()


# ------------------------------------------------------------------ helpers
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _equation(t: str) -> bool:
    """A displayed equation line: an equals sign between terms joined by
    operators, and next to no words ("ln (output)i = ln(HFi) + ln(CSi) …")."""
    t = _norm(t)
    if "=" not in t or t.endswith((".", ";")) or len(t) > 160:
        return False
    ops = len(re.findall(r"[=+−\-×÷/^]", t))
    words = re.findall(r"\b[a-z]{4,}\b", t)
    return ops >= 2 and len(words) <= 3


def _all_caps(t: str) -> bool:
    letters = [c for c in t or "" if c.isalpha()]
    return len(letters) >= 3 and all(c.isupper() for c in letters)


class Kriyadocs:
    def __init__(self, an):
        self.an = an
        self.m = an.model
        self.doc = an.doc
        self.stats: dict = {}
        self._uris: dict[int, str] | None = None

    # ---------------------------------------------------------- geometry
    def disp(self, pno: int, box) -> pymupdf.Rect:
        from .analyzer import to_disp
        return to_disp(self.doc[pno], box)

    def pdf(self, pno: int, box) -> tuple:
        from .analyzer import to_pdf
        return to_pdf(self.doc[pno], box)

    def height(self, pno: int) -> float:
        from .analyzer import frame
        return frame(self.doc[pno]).height

    def box_of(self, n: StructNode):
        """Display box of a node, or of its first descendant with a box."""
        if n.bbox is not None and n.page >= 0:
            return n.page, self.disp(n.page, n.bbox)
        for d in self.m.descendants(n.id):
            if d.bbox is not None and d.page >= 0:
                return d.page, self.disp(d.page, d.bbox)
        return None, None

    def lines_in(self, n: StructNode, pno: int | None = None) -> list:
        """The printed lines inside the node's zones (display space, top down)."""
        out = []
        pages = sorted(n.pages()) if pno is None else [pno]
        for p in pages:
            rects = [self.disp(p, r) for r in n.rects_on(p)]
            if not rects or p >= len(self.an.page_lines):
                continue
            for l in self.an.page_lines[p]:
                cx, cy = (l.x0 + l.x1) / 2, (l.y0 + l.y1) / 2
                if any(r.x0 - 1 <= cx <= r.x1 + 1 and r.y0 - 1 <= cy <= r.y1 + 1 for r in rects):
                    out.append((p, l))
        out.sort(key=lambda t: (t[0], round(t[1].y0, 1), t[1].x0))
        return out

    def uri(self, n: StructNode) -> str:
        if self._uris is None:
            self._uris = {}
            try:
                for pg in self.an.pdf.pages:
                    for a in pg.obj.get("/Annots", []) or []:
                        try:
                            if not a.is_indirect:
                                continue
                            act = a.get("/A")
                            u = str(act.get("/URI", "")) if act is not None else ""
                            if u:
                                self._uris[a.objgen[0]] = u
                        except Exception:
                            continue
            except Exception:
                pass
            if not self._uris and self.doc is not None:
                # an edit has no pikepdf file open: MuPDF knows the links too
                try:
                    for pg in self.doc:
                        for lk in pg.get_links():
                            if lk.get("uri") and lk.get("xref"):
                                self._uris[int(lk["xref"])] = str(lk["uri"])
                except Exception:
                    pass
        for a in n.annots or []:
            if a in self._uris:
                return self._uris[a]
        return ""

    def is_web(self, ln: StructNode) -> bool:
        """A web address or an e-mail address (V4.17: a plain Link tag, no
        Reference — it is no cross-reference to another part of the article)."""
        x = (ln.style or {}).get("xref") or {}
        if x.get("kind") in ("web", "email") or x.get("uri"):
            return True
        u = self.uri(ln).lower()
        return u.startswith(("http:", "https:", "mailto:", "www.", "ftp:"))

    def ubox(self, n: StructNode, pno: int):
        """Display box around every zone of the node on that page."""
        box = None
        for r in n.rects_on(pno):
            d = self.disp(pno, r)
            box = d if box is None else box | d
        return box if box is not None else self.box_of(n)[1]

    def roots_on(self, pno: int) -> list[StructNode]:
        return [self.m.nodes[r] for r in self.m.root_children
                if r in self.m.nodes and self.m.nodes[r].page == pno]

    def bump(self, key: str, n: int = 1) -> None:
        if n:
            self.stats[key] = self.stats.get(key, 0) + n

    def to_artifact(self, n: StructNode, why: str, sub: str = "") -> None:
        if n.parent is not None and n.parent in self.m.nodes:
            self.m.detach(n.id)
            self.m.add(n, None)
        n.artifact = True
        n.role = "Artifact"
        n.artifact_subtype = "Pagination" if sub else "Layout"
        n.pagination_subtype = sub
        n.note = why

    def new_node(self, role: str, pno: int, disp_box=None, text: str = "", **kw) -> StructNode:
        n = StructNode(id=self.m.new_id("ky"), role=role, page=pno,
                       bbox=self.pdf(pno, disp_box) if disp_box is not None else None,
                       text=text, source="auto", **kw)
        self.m.nodes[n.id] = n
        return n

    def replace(self, old: StructNode, new: list[StructNode]) -> None:
        """Put `new` where `old` stands; `old` goes (its children were moved)."""
        sib = self.m.siblings(old.id)
        at = sib.index(old.id)
        for k, n in enumerate(new):
            n.parent = old.parent
            sib.insert(at + k, n.id)
        self.m.remove(old.id, promote_children=False)

    # ------------------------------------------------------------ the title
    def title(self) -> StructNode | None:
        for n in self.m.ordered_nodes():
            if n.role == "H1" and not n.artifact and n.page == 0:
                return n
        return None

    def journal(self) -> str:
        texts = [n.text for n in self.m.nodes.values() if n.text and n.page <= 1]
        for t in texts:
            mm = JOURNAL_RE.search(_norm(t))
            if mm:
                j = mm.group("j").strip()
                return BMJ_JOURNALS.get(j, j)
        return ""

    # ============================================================== early
    def early(self) -> dict:
        """Before the reference list and the notes are tagged: the section
        headings the analysis read as text, and what it took for footnotes
        under them."""
        for step in (self.section_heads, self.back_heads, self.reference_notes):
            try:
                step()
            except Exception as exc:
                self.m.warnings.append(f"house style ({step.__name__}): {exc}")
        return self.stats

    def section_heads(self) -> None:
        """"REFERENCES" set like the other section heads but read as a P: H2."""
        m = self.m
        # the section heads the analysis did find, set in capitals like these
        h2 = [n for n in m.nodes.values() if n.role == "H2" and not n.artifact and _all_caps(n.text)]
        size = statistics.median([float((n.style or {}).get("size") or 0) for n in h2]) if h2 else 0
        for n in list(m.nodes.values()):
            if n.artifact or n.role not in ("P", "H3", "H4", "H5", "H6", "H?") or n.children \
                    or not SECTION_HEAD_RE.match(_norm(n.text)):
                continue
            par = m.nodes.get(n.parent) if n.parent else None
            if par is not None and par.role not in ("Sect", "Document", "Div", "Part", "Art"):
                continue
            st = n.style or {}
            if size and abs(float(st.get("size") or size) - size) > 1.2:
                continue
            if not size and n.role == "P" and not st.get("bold"):
                continue
            n.role = "H2"
            n.note = "section heading (house style)"
            self.bump("section_heads")

    def back_heads(self) -> None:
        m = self.m
        for n in list(m.nodes.values()):
            if n.artifact or n.id not in m.nodes:
                continue
            t = _norm(n.text)
            if n.role in ("P", "H2", "H4", "H5", "H6") and BACK_HEAD_RE.fullmatch(t):
                n.role = "H3"
                n.note = "back-matter heading (house style)"
                self.bump("back_headings")
        # the affiliations under "Author affiliations": the analysis took them
        # for footnotes and read them at the foot of the page
        for h in [n for n in m.ordered_nodes() if n.is_heading and re.fullmatch(
                AFFIL_HEAD, _norm(n.text), re.I)]:
            p, hb = self.box_of(h)
            if hb is None:
                continue
            col = []
            for n in m.nodes.values():
                if n.role not in ("Note", "P") or n.artifact or n.page != p or n.bbox is None \
                        or n.parent is not None and m.nodes.get(n.parent) is not None \
                        and m.nodes[n.parent].role not in ("Sect", "Document", "Div"):
                    continue
                d = self.disp(p, n.bbox)
                if abs(d.x0 - hb.x0) < 12 and d.y0 >= hb.y1 - 1:
                    col.append((d.y0, n, d))
            col.sort(key=lambda t: t[0])
            near = []
            bottom = hb.y1
            for y0, n, d in col:
                if y0 - bottom > 14:
                    break
                near.append((y0, n))
                bottom = max(bottom, d.y1)
            if not near:
                continue
            for _, n in reversed(near):
                m.detach(n.id)
                sib = m.siblings(h.id)
                m.add(n, h.parent, sib.index(h.id) + 1)
            self.bump("affiliations_moved", len(near))

    def reference_notes(self) -> None:
        """Entries of the reference list printed at the foot of a page and
        taken for footnotes: text of the list again."""
        m = self.m
        heads = [n for n in m.ordered_nodes() if n.is_heading and REF_HEAD_RE.fullmatch(_norm(n.text))]
        if not heads:
            return
        hp = heads[-1].page
        fixed = 0
        for n in list(m.nodes.values()):
            if n.role != "Note" or n.artifact or n.page < hp:
                continue
            if n.page == hp:
                _, hb = self.box_of(heads[-1])
                d = self.disp(n.page, n.bbox) if n.bbox else None
                if d is None or hb is None or d.y0 < hb.y1 - 2 or abs(d.x0 - hb.x0) > 40:
                    continue
            if (n.style or {}).get("house"):
                continue
            n.role = "P"
            n.note = "reference list entry (house style)"
            fixed += 1
        if fixed:
            # in printed order under the heading, so the list is found there
            sib = m.siblings(heads[-1].id)
            moved = [m.nodes[x] for x in list(sib) if m.nodes[x].note == "reference list entry (house style)"]
            for n in moved:
                m.detach(n.id)
            moved.sort(key=lambda n: (n.page, self.disp(n.page, n.bbox).x0 // 150, self.disp(n.page, n.bbox).y0))
            at = m.siblings(heads[-1].id).index(heads[-1].id) + 1
            for k, n in enumerate(moved):
                m.add(n, heads[-1].parent, at + k)
        self.bump("reference_notes", fixed)

    # ================================================================ pre
    def pre(self) -> dict:
        m = self.m
        if not m.page_count:
            return self.stats
        h1 = self.title()
        if h1 is not None:
            try:
                self.front_matter(h1)
            except Exception as exc:
                m.warnings.append(f"house style (front matter): {exc}")
        for step in (self.captions, self.merge_continued, self.continued, self.table_notes,
                     self.back_matter, self.back_paragraphs, self.formulas, self.lists, self.cell_lead_ins,
                     self.stamp_links, self.join_runs):
            try:
                step()
            except Exception as exc:
                m.warnings.append(f"house style ({step.__name__}): {exc}")
        self.note_ids()
        return self.stats

    # ------------------------------------------------------- front matter
    def front_matter(self, h1: StructNode) -> None:
        m = self.m
        H = self.height(0)
        _, hb = self.box_of(h1)
        tops = self.header_ps()
        logo = self.masthead(hb)
        authors = self.authors(h1, hb)
        self.orcid(authors)
        side = self.sidebar(hb, logo)
        side = self.fix_sidebar(side)
        for n in side:
            n.style = dict(n.style or {}, house=n.style.get("house", "sidebar") if n.style else "sidebar")
        self.move_boxes()
        foot = self.footer_logo(H)
        # page 1 in Kriyadocs order: article type, logo, title, authors,
        # the sidebar top to bottom, then the rest as read
        lead = tops + ([logo] if logo else []) + [h1] + ([authors] if authors else []) + side
        lead_ids: list[str] = []
        for n in lead:
            if n is not None and n.id in m.nodes and n.id not in lead_ids:
                lead_ids.append(n.id)
        for nid in lead_ids:
            n = m.nodes[nid]
            if n.parent is not None:
                m.detach(nid)
                n.parent = None
        lead_set = set(lead_ids)
        rest = [r for r in m.root_children if r not in lead_set]
        at = next((i for i, r in enumerate(rest) if m.nodes[r].page == 0 and not m.nodes[r].artifact),
                  len(rest))
        m.root_children = rest[:at] + lead_ids + rest[at:]
        if foot:
            self.place_after_page(foot, 0)

    def header_ps(self) -> list[StructNode]:
        """The words in page 1's top band ("Open access", "Original research"):
        a P each, read first, left to right."""
        H = self.height(0)
        out = []
        for n in self.roots_on(0):
            if not n.bbox or not (n.text or "").strip() or n.children or n.role in ("H1", "Figure", "Table"):
                continue
            d = self.disp(0, n.bbox)
            # "Open access" and "Original research": a P each on page 1 (the
            # running "Open access" of the later pages stays pagination)
            if d.y1 > 0.075 * H or re.fullmatch(r"[\divxlc]+", n.text.strip(), re.I):
                continue
            n.artifact = False
            n.role = "P"
            n.pagination_subtype = ""
            n.note = "article type printed on top of page 1 (house style)"
            out.append(n)
        out.sort(key=lambda n: self.disp(0, n.bbox).x0)
        self.bump("article_type", len(out))
        return out

    def masthead(self, hb) -> StructNode | None:
        """The journal logo left of the title: a Figure named after the journal."""
        page = self.doc[0]
        H = self.height(0)
        area = pymupdf.Rect(0, 0.04 * H, hb.x0 - 3, hb.y1 + 6)
        boxes = []
        try:
            for im in page.get_image_info():
                r = pymupdf.Rect(im["bbox"])
                if r.intersects(area) and area.contains(r.tl) and r.y0 < hb.y0 + 0.6 * (hb.y1 - hb.y0):
                    boxes.append(r)
        except Exception:
            pass
        try:
            for dr in page.get_drawings():
                r = pymupdf.Rect(dr["rect"])
                if dr.get("fill") is not None and r.width < 0.3 * page.rect.width and r.height < 40 \
                        and area.contains(r) and r.y0 < hb.y0 + 0.6 * (hb.y1 - hb.y0):
                    boxes.append(r)
        except Exception:
            pass
        texts = [n for n in self.roots_on(0) if not n.artifact and n.bbox is not None and n.role != "H1"
                 and area.contains(self.disp(0, n.bbox))]
        for n in texts:
            boxes.append(self.disp(0, n.bbox))
        if not boxes:
            return None
        box = boxes[0]
        for b in boxes[1:]:
            box = box | b
        name = self.journal() or " ".join(_norm(n.text) for n in texts) or "Journal logo"
        fig = None
        for n in texts:
            if n.role == "Figure" and fig is None:
                fig = n
        if fig is None:
            fig = self.new_node("Figure", 0, box)
            self.m.add(fig, None, 0)
        fig.bbox = self.pdf(0, box)
        fig.alt = name
        fig.text = ""
        fig.note = "journal logo (house style)"
        for n in texts:
            if n is not fig and n.id in self.m.nodes:
                self.m.absorb(fig.id, n.id)
        self.bump("logo")
        return fig

    def authors(self, h1: StructNode, hb) -> StructNode | None:
        """The line of authors under the title: a P (not a heading)."""
        seq = [self.m.nodes[r] for r in self.m.root_children]
        cands = []
        for n in seq:
            if n.artifact or n.page != 0 or n is h1 or n.bbox is None:
                continue
            d = self.disp(0, n.bbox)
            if hb.y1 - 2 <= d.y0 <= hb.y1 + 70 and d.x0 >= hb.x0 - 25 and \
                    ("," in n.text or " and " in n.text):
                cands.append((d.y0, n))
        if not cands:
            return None
        cands.sort(key=lambda t: t[0])
        n = cands[0][1]
        # the names run over two lines, cut into pieces by the ORCID icons and
        # the raised affiliation numbers: every piece in the band is the one P
        band = self.disp(0, n.bbox)
        lh = max(8.0, band.y1 - band.y0) if band.y1 - band.y0 < 20 else 12.0
        pieces = []
        for x in seq:
            if x is n or x.artifact or x.page != 0 or x is h1 or x.bbox is None or x.role in ("Figure", "Table"):
                continue
            d = self.disp(0, x.bbox)
            if d.x0 >= hb.x0 - 25 and d.y0 >= hb.y1 - 2 and not _all_caps(x.text):
                pieces.append((d.y0, x, d))
        pieces.sort(key=lambda t: t[0])
        for _, x, d in pieces:
            if d.y0 > band.y1 + 0.8 * lh:
                break
            band |= d
            if x.role in ("Link", "Annot", "Figure", "Form") or x.annots:
                # a link keeps its annotation: it goes inside, it is not folded in
                self.m.detach(x.id)
                self.m.add(x, n.id)
            else:
                self.m.absorb(n.id, x.id)
        lines = self.lines_in(n, 0)
        if lines:
            n.text = _norm(" ".join(l.text for _, l in sorted(lines, key=lambda t: (round(t[1].cy / 4), t[1].x0))))
        if n.role != "P":
            n.role = "P"
            n.note = "authors (house style)"
        n.style = dict(n.style or {}, house="authors")
        return n

    def orcid(self, authors: StructNode | None) -> None:
        """Each ORCID icon next to a name: Figure /Alt "Orcid icon", then the
        icon's link, inside the authors' P."""
        if authors is None:
            return
        page = self.doc[0]
        ab = None
        for r in authors.rects_on(0):
            d = self.disp(0, r)
            ab = d if ab is None else ab | d
        links = [n for n in self.m.nodes.values() if n.role == "Link" and n.page == 0
                 and "orcid.org" in self.uri(n).lower()]
        try:
            drawings = [pymupdf.Rect(d["rect"]) for d in page.get_drawings() if d.get("fill") is not None]
        except Exception:
            drawings = []
        made = 0
        for ln in links:
            lb = self.disp(0, ln.bbox)
            if ab is not None and not lb.intersects(ab + (-4, -4, 4, 4)):
                continue
            icon = [r for r in drawings if lb.contains(r) or (r & lb).get_area() > 0.6 * max(r.get_area(), 1e-6)]
            # what the link holds: no text, the icon's figure instead
            for c in list(ln.children):
                cn = self.m.nodes.get(c)
                if cn is not None and cn.role == "Figure":
                    self.m.detach(c)
                    self.m.nodes.pop(c, None)
            if ln.parent != authors.id:
                self.m.detach(ln.id)
                self.m.add(ln, authors.id)
            ln.text = ""
            # no /Alt on the Link (the client's links have none); the
            # annotation still says where it goes
            ln.style = dict(ln.style or {}, contents="ORCID iD")
            if icon:
                box = icon[0]
                for r in icon[1:]:
                    box = box | r
                fig = self.new_node("Figure", 0, box, alt="Orcid icon",
                                    note="ORCID icon (house style)")
                kids = authors.children
                kids.insert(kids.index(ln.id), fig.id)
                fig.parent = authors.id
                made += 1
        self.sort_inline(authors)
        self.bump("orcid_icons", made)

    def sort_inline(self, n: StructNode) -> None:
        """Children in printed order: line by line, left to right."""
        kids = [self.m.nodes[c] for c in n.children if c in self.m.nodes]
        if len(kids) < 2:
            return

        lines = self.lines_in(n)

        def key(c):
            p, b = self.box_of(c)
            if b is None:
                return (10 ** 6, 0, 0)
            cy = (b.y0 + b.y1) / 2
            row = [i for i, (pp, l) in enumerate(lines) if pp == p and l.y0 - 1 <= cy <= l.y1 + 1]
            if row:
                li = row[0]
            elif lines:
                li = min(range(len(lines)), key=lambda i: (lines[i][0] != p, abs(lines[i][1].cy - cy)))
            else:
                li = round(cy / 6)
            return (p, li, b.x0)
        kids.sort(key=key)
        n.children = [k.id for k in kids]

    def sidebar(self, hb, logo) -> list[StructNode]:
        """The narrow column left of the abstract, under the logo: To cite,
        dates, licence, affiliations, correspondence."""
        out = []
        lb = self.disp(0, logo.bbox) if logo is not None and logo.bbox else None
        for rid in list(self.m.root_children):
            n = self.m.nodes[rid]
            if n.artifact or n is logo or n.page != 0:
                continue
            p, b = self.box_of(n)
            if b is None or p != 0:
                continue
            if b.x1 <= hb.x0 - 3 and (lb is None or b.y0 >= lb.y1 - 1) and b.y0 > hb.y0 \
                    and b.y0 < 0.93 * self.height(0):
                out.append(n)
        # links and figures the analysis left at the top level of that column
        return out

    def fix_sidebar(self, side: list[StructNode]) -> list[StructNode]:
        m = self.m
        out: list[StructNode] = []
        for n in side:
            if n.id not in m.nodes:
                continue
            if n.is_heading:
                # the sidebar has no headings: "Correspondence to" set bold is
                # the start of its paragraph
                n.role = "P"
                n.note = "sidebar text (house style)"
            if n.role == "L":
                p = self.arrow_item(n)
                if p is not None:
                    out.append(p)
                    continue
            if n.role == "Link" and "crossmark" in self.uri(n).lower():
                out.append(self.crossmark(n))
                continue
            out.append(n)
        # a crossmark link the analysis put inside another paragraph
        for ln in [x for x in m.nodes.values() if x.role == "Link" and x.page == 0
                   and "crossmark" in self.uri(x).lower() and x.parent is not None]:
            host = m.nodes.get(ln.parent)
            if host is not None and host.role == "P" and host.text:
                out.append(self.crossmark(ln))
        out = [n for n in out if n.id in m.nodes]
        out.sort(key=lambda n: (self.box_of(n)[1].y0 if self.box_of(n)[1] is not None else 10 ** 6))
        # one paragraph the analysis cut at a short line ("… under CC BY." /
        # "Published by BMJ Group."): the sidebar parts its paragraphs with space
        k = 0
        while k + 1 < len(out):
            a, b = out[k], out[k + 1]
            if a.role in ("P", "Note") and b.role in ("P", "Note") and a.bbox is not None and b.bbox is not None \
                    and a.page == b.page and a.text and b.text \
                    and not (DATE_LINE_RE.match(a.text) or DATE_LINE_RE.match(b.text)):
                da = self.ubox(a, a.page)
                db = self.ubox(b, b.page)
                la = [l for _, l in self.lines_in(a, a.page)]
                lb = [l for _, l in self.lines_in(b, b.page)]
                lh = statistics.median([l.y1 - l.y0 for l in la]) if la else 9.0
                # a block that opens one of the sidebar's own paragraphs, or the
                # first affiliation after the licence, is not a continuation
                fresh = SIDEBAR_START_RE.match(_norm(b.text)) or (
                    lb and self.sup_label(lb[0]) is not None and not (la and self.sup_label(la[0]) is not None))
                if not fresh and 0 <= db.y0 - da.y1 < 0.45 * lh and abs(da.x0 - db.x0) < 3:
                    m.absorb(a.id, b.id)
                    a.text = _norm(f"{a.text} {b.text}")
                    out.pop(k + 1)
                    self.bump("sidebar_joined")
                    continue
            k += 1
        # then the blocks that are several things: the dates, the affiliations
        split: list[StructNode] = []
        for n in out:
            if n.role in ("P", "Note") and n.id in m.nodes:
                split.extend(self.split_dates(n) or self.split_affiliations(n) or [n])
            else:
                split.append(n)
        out = [n for n in split if n.id in m.nodes]
        # a paragraph broken over two blocks, "Correspondence to" + the name
        k = 0
        while k + 1 < len(out):
            a, b = out[k], out[k + 1]
            if a.role == "P" and b.role == "P" and a.bbox is not None and b.bbox is not None and \
                    (_norm(a.text).lower() in ("correspondence to", "correspondence", "correspondence to:")
                     or (not a.text.rstrip().endswith(OPEN_END) and b.text.lstrip()[:1].islower())):
                m.absorb(a.id, b.id)
                a.text = _norm(f"{a.text} {b.text}")
                out.pop(k + 1)
                self.bump("sidebar_joined")
                continue
            k += 1
        return out

    def arrow_item(self, L: StructNode) -> StructNode | None:
        """"► Prepublication history and …": one item set with an arrow is a
        paragraph; the arrow is decoration."""
        lis = [self.m.nodes[c] for c in L.children if c in self.m.nodes and self.m.nodes[c].role == "LI"]
        if len(lis) != 1:
            return None
        kids = [self.m.nodes[c] for c in lis[0].children if c in self.m.nodes]
        lbl = next((k for k in kids if k.role == "Lbl"), None)
        body = next((k for k in kids if k.role == "LBody"), None)
        if lbl is None or body is None or (lbl.text or "").strip() not in ARROW_LABELS:
            return None
        p = self.new_node("P", body.page, None, text=body.text or "")
        p.bbox, p.parts = body.bbox, [list(x) for x in body.parts]
        for c in list(body.children):
            self.m.nodes[c].parent = p.id
            p.children.append(c)
        body.children = []
        lbl_art = lbl
        self.m.detach(lbl.id)
        self.replace(L, [p])
        lbl_art.parent = None
        self.m.nodes[lbl_art.id] = lbl_art
        self.m.root_children.append(lbl_art.id)
        lbl_art.artifact, lbl_art.role, lbl_art.note = True, "Artifact", "arrow before a paragraph (house style)"
        lbl_art.children = []
        self.bump("arrow_paragraphs")
        return p

    def crossmark(self, ln: StructNode) -> StructNode:
        """Check for updates: P > Link, the badge itself decoration."""
        m = self.m
        for c in list(ln.children):
            cn = m.nodes.get(c)
            if cn is not None and cn.role == "Figure":
                self.to_artifact(cn, "Check for updates badge (house style)")
        host = m.nodes.get(ln.parent) if ln.parent else None
        if host is not None and host.role == "P" and len(host.children) == 1 and not (host.text or "").strip():
            return host
        if ln.id in m.siblings(ln.id):
            m.detach(ln.id)
        p = self.new_node("P", ln.page, None, note="Check for updates (house style)")
        m.add(p, None)
        m.add(ln, p.id)
        ln.text = ""
        ln.style = dict(ln.style or {}, contents="Check for updates")
        self.bump("crossmark")
        return p

    def split_dates(self, n: StructNode) -> list[StructNode]:
        """"Received 20 October 2022 / Accepted 18 October 2023": a P each."""
        lines = self.lines_in(n, n.page)
        if len(lines) < 2 or not all(DATE_LINE_RE.match(l.text) for _, l in lines):
            return []
        made = []
        for p, l in lines:
            q = self.new_node("P", p, pymupdf.Rect(l.bbox), text=_norm(l.text), note="date line (house style)")
            made.append(q)
        for c in list(n.children):
            cn = self.m.nodes[c]
            _, cb = self.box_of(cn)
            home = made[0]
            if cb is not None:
                for q in made:
                    if pymupdf.Rect(self.disp(q.page, q.bbox)).contains(pymupdf.Point((cb.x0 + cb.x1) / 2, (cb.y0 + cb.y1) / 2)):
                        home = q
            self.m.detach(c)
            self.m.add(cn, home.id)
        self.replace(n, made)
        self.bump("date_lines", len(made))
        return made

    # ------------------------------------------------------ affiliations
    @staticmethod
    def sup_label(l):
        """(label, label box, rest box) when the line opens with a raised
        number ("¹Department …", "1Department …"), else None."""
        chars = [(c, b) for c, b in l.chars if c.strip()]
        k = 0
        while k < len(chars) and chars[k][0].isdigit():
            k += 1
        if k == 0 or k > 2 or k >= len(chars):
            return None
        lab, rest = chars[:k], chars[k:]
        hs = [b[3] - b[1] for _, b in rest[:12]]
        lh = max(b[3] - b[1] for _, b in lab)
        raised = statistics.median([b[3] for _, b in rest[:12]]) - max(b[3] for _, b in lab)
        med = statistics.median(hs) if hs else lh
        if not (lh < 0.85 * med or raised > 0.15 * med or rest[0][0].isupper()):
            return None
        lb = (min(b[0] for _, b in lab), min(b[1] for _, b in lab),
              max(b[2] for _, b in lab), max(b[3] for _, b in lab))
        rb = (min(b[0] for _, b in rest), min(b[1] for _, b in rest),
              max(b[2] for _, b in rest), max(b[3] for _, b in rest))
        return "".join(c for c, _ in lab), lb, rb

    def split_affiliations(self, n: StructNode) -> list[StructNode]:
        """"1Department of … 2Department of …": one Note per affiliation, its
        number at its start."""
        text = _norm(n.text)
        if not re.match(r"^\d{1,2}\s?[A-Z]", text) or not AFFIL_WORDS.search(text):
            return []
        lines = self.lines_in(n)
        groups: list[list] = []
        for p, l in lines:
            lab = self.sup_label(l)
            if lab is not None and (not groups or int(lab[0]) == int(groups[-1][0][0]) + 1 or int(lab[0]) == 1):
                groups.append([lab, p, [(l, lab[2])]])
            elif groups:
                groups[-1][2].append((l, l.bbox))
            else:
                return []
        if not groups:
            return []
        made = []
        for lab, p, ls in groups:
            box = pymupdf.Rect(ls[0][1])
            for l, b in ls[1:]:
                box |= pymupdf.Rect(b)
            body = _norm(" ".join(l.text for l, _ in ls))
            body = re.sub(r"^\d{1,2}\s?", "", body)
            # Note > Lbl "1" + the affiliation, as the client tags it
            note = self.new_node("Note", p, box | pymupdf.Rect(lab[1]), text=body,
                                 note=f"affiliation {lab[0]} (house style)")
            lbl = self.new_node("Lbl", p, pymupdf.Rect(lab[1]), text=lab[0], priority=1)
            self.m.add(lbl, note.id)
            made.append(note)
        for c in list(n.children):
            cn = self.m.nodes[c]
            self.m.detach(c)
            _, cb = self.box_of(cn)
            home = made[0]
            if cb is not None:
                for q in made:
                    if self.disp(q.page, q.bbox).intersects(cb):
                        home = q
            self.m.add(cn, home.id)
        self.replace(n, made)
        self.bump("affiliations", len(made))
        return made

    # ---------------------------------------------------------- the box
    def move_boxes(self) -> None:
        """"STRENGTHS AND LIMITATIONS OF THIS STUDY" (or "WHAT IS ALREADY KNOWN
        …"): the box's heading and its list follow the abstract."""
        m = self.m
        roots = m.root_children
        ab = next((i for i, r in enumerate(roots) if m.nodes[r].is_heading
                   and _key(m.nodes[r].text) == "abstract"), None)
        if ab is None:
            return
        nxt = next((i for i in range(ab + 1, len(roots)) if m.nodes[roots[i]].is_heading
                    and m.nodes[roots[i]].role <= m.nodes[roots[ab]].role
                    and m.nodes[roots[i]].page <= 1), None)
        if nxt is None:
            return
        box: list[str] = []
        i = 0
        while i < len(roots):
            n = m.nodes[roots[i]]
            boxhead = n.is_heading or (n.role == "P" and (n.style or {}).get("bold") and len(n.text.split()) <= 14)
            if n.page == 0 and boxhead and n.role != "H1" and _all_caps(n.text) and \
                    i + 1 < len(roots) and m.nodes[roots[i + 1]].role == "L":
                j = i + 1
                while j < len(roots) and m.nodes[roots[j]].role == "L":
                    j += 1
                box.extend(roots[i:j])
                i = j
                continue
            i += 1
        if not box:
            return
        anchor = roots[nxt]
        if anchor in box:
            return
        rest = [r for r in roots if r not in set(box)]
        at = rest.index(anchor)
        m.root_children = rest[:at] + box + rest[at:]
        for r in box:
            n = m.nodes[r]
            if n.role != "L":
                n.role = "H3"
        self.bump("boxes_moved", sum(1 for r in box if m.nodes[r].is_heading))

    def join_runs(self) -> None:
        """A paragraph cut by the box or a column break: the halves are next to
        each other again. The first half ends a column (nothing printed under
        it) without its full stop; the second goes on in the next column or on
        the next page."""
        m = self.m
        k = 0
        roots = m.root_children
        floats = ("Figure", "Caption", "Table", "Formula")
        while k + 1 < len(roots):
            a = m.nodes[roots[k]]
            # the text goes on past what floats on the next page (the figure at
            # its top, the footer logo): the sentence is read through first
            j = k + 1
            fl: list = []                     # what floats, and the notes printed under it
            while j < len(roots):
                x = m.nodes[roots[j]]
                if x.artifact or (x.style or {}).get("house") == "footer" or x.role in ("Link", "Annot"):
                    j += 1
                    continue
                if x.role in floats:
                    for pg in sorted(x.pages()):
                        for r in x.rects_on(pg):
                            fl.append((pg, self.disp(pg, r)))
                    j += 1
                    continue
                small = float((x.style or {}).get("size") or 99) < 0.95 * (self.m.body_size or 10.0)
                if fl and x.bbox is not None and (x.role == "Note" or (
                        x.role == "P" and small and not x.text.lstrip()[:1].islower())):
                    xb = self.disp(x.page, x.bbox)
                    if any(pg == x.page and min(xb.x1, fb.x1) - max(xb.x0, fb.x0) > 0.5 * xb.width
                           and fb.y1 - 2 <= xb.y0 <= fb.y1 + 90 for pg, fb in fl):
                        fl.append((x.page, xb))
                        j += 1
                        continue
                break
            if j >= len(roots):
                break
            b = m.nodes[roots[j]]
            low = b.text.lstrip()[:1].islower()
            closed = a.text.rstrip().endswith((".", "?", "!", ":", ";", "”", '"'))
            ends_open = not closed and (a.text.rstrip()[-1:].isalnum() or low)
            if a.role == "P" and b.role == "P" and not a.artifact and not b.artifact and a.bbox and b.bbox \
                    and not (a.style or {}).get("house") and not (b.style or {}).get("house") \
                    and len(a.text.split()) >= 6 and len(b.text.split()) >= 3 and ends_open \
                    and (j == k + 1 or b.page > a.page or b.text.lstrip()[:1].islower()):
                pa = max(a.pages())
                # the zone read last: parts are added in reading order
                last = [p for p in a.parts if int(p[0]) == pa]
                da = self.disp(pa, tuple(last[-1][1:5]) if last else a.bbox)
                db = self.disp(b.page, b.bbox)
                moved_on = b.page > pa or (b.page == pa and abs(da.x0 - db.x0) > 20)
                if moved_on and self.column_ends(a, pa, da):
                    m.absorb(a.id, b.id)
                    a.text = _norm(f"{a.text} {b.text}")
                    self.bump("paragraphs_joined")
                    continue
            k += 1

    def column_ends(self, a: StructNode, pno: int, da) -> bool:
        """Nothing of the text is printed under `da` in its column (a table or
        figure set at the foot of the page, with its notes, floats)."""
        floats = ("Table", "Figure", "Caption", "Formula")
        boxes = []
        float_top = 10 ** 6
        for n in self.m.nodes.values():
            if n is a or n.artifact:
                continue
            for r in n.rects_on(pno):
                d = self.disp(pno, r)
                ov = min(d.x1, da.x1) - max(d.x0, da.x0)
                if ov <= 0.5 * min(d.width, da.width) or d.y0 <= da.y1 + 1:
                    continue
                if n.role in floats or any(x.role in floats for x in self.m.ancestors(n.id)):
                    float_top = min(float_top, d.y0)
                    continue
                if n.role in ("Note", "Link", "Reference", "Span", "Lbl", "LBody", "LI", "TD", "TH", "TR"):
                    continue
                boxes.append(d)
        return not any(d.y0 < float_top for d in boxes)

    def footer_logo(self, H: float) -> StructNode | None:
        """The publisher's logo in page 1's footer: Figure "BMJ Group Logo",
        then its link as P > Link."""
        page = self.doc[0]
        zone = pymupdf.Rect(0, 0.93 * H, 0.35 * page.rect.width, H)
        boxes = []
        try:
            for dr in page.get_drawings():
                r = pymupdf.Rect(dr["rect"])
                if dr.get("fill") is not None and zone.contains(r) and r.width < 120:
                    boxes.append(r)
            for im in page.get_image_info():
                r = pymupdf.Rect(im["bbox"])
                if zone.contains(r):
                    boxes.append(r)
        except Exception:
            pass
        if not boxes:
            return None
        box = boxes[0]
        for b in boxes[1:]:
            box |= b
        fig = self.new_node("Figure", 0, box, alt="BMJ Group Logo", note="publisher logo (house style)")
        fig.style = {"house": "footer"}
        out = [fig]
        for ln in [x for x in self.m.nodes.values() if x.role == "Link" and x.page == 0 and x.bbox]:
            lb = self.disp(0, ln.bbox)
            if (lb & box).get_area() > 0.3 * max(lb.get_area(), 1e-6):
                if ln.parent is not None:
                    self.m.detach(ln.id)
                elif ln.id in self.m.root_children:
                    self.m.root_children.remove(ln.id)
                p = self.new_node("P", 0, None, note="footer logo link (house style)")
                p.style = {"house": "footer"}
                self.m.nodes[p.id] = p
                p.children.append(ln.id)
                ln.parent = p.id
                ln.text = ""
                ln.style = dict(ln.style or {}, contents="BMJ Group")
                ln.style = dict(ln.style or {}, no_reference=True)
                out.append(p)
        self.bump("footer_logo")
        fig._house_group = out          # type: ignore[attr-defined]
        return fig

    def place_after_page(self, fig: StructNode, pno: int) -> None:
        m = self.m
        group = getattr(fig, "_house_group", [fig])
        for g in group:
            if g.id in m.root_children:
                m.root_children.remove(g.id)
        last = None
        for i, r in enumerate(m.root_children):
            n = m.nodes[r]
            if not n.artifact and n.page == pno:
                last = i
        at = len(m.root_children) if last is None else last + 1
        for k, g in enumerate(group):
            g.parent = None
            m.nodes[g.id] = g
            m.root_children.insert(at + k, g.id)

    # ------------------------------------------------------------ tables
    def captions(self) -> None:
        """A table's caption stands before it (Caption, its text direct); a
        figure's caption after it, as Caption > P."""
        m = self.m
        moved = 0
        for t in [n for n in m.ordered_nodes() if n.role in ("Table", "Figure") and not n.artifact]:
            caps = [m.nodes[c] for c in t.children if c in m.nodes and m.nodes[c].role == "Caption"]
            for cap in caps:
                m.detach(cap.id)
                sib = m.siblings(t.id)
                at = sib.index(t.id)
                m.add(cap, t.parent, at if t.role == "Table" else at + 1)
                moved += 1
            if t.role == "Figure":
                sib = m.siblings(t.id)
                at = sib.index(t.id)
                if at + 1 < len(sib):
                    cap = m.nodes[sib[at + 1]]
                    if cap.role == "Caption" and cap.bbox is not None and not any(
                            m.nodes[c].role == "P" for c in cap.children if c in m.nodes):
                        p = StructNode(id=m.new_id("ky"), role="P", page=cap.page, bbox=cap.bbox,
                                       parts=[list(x) for x in cap.parts], text=cap.text, source="auto")
                        for c in list(cap.children):
                            m.nodes[c].parent = p.id
                            p.children.append(c)
                        cap.children = []
                        cap.bbox, cap.parts, cap.text = None, [], ""
                        m.add(p, cap.id)
        self.bump("captions_moved", moved)

    def table_notes(self) -> None:
        """The lines under a table that open with a mark ("*T-test.",
        "†χ2 test.") are its notes: Note, the mark at its start."""
        m = self.m
        made = 0
        for t in [n for n in m.ordered_nodes() if n.role == "Table" and not n.artifact]:
            sib = m.siblings(t.id)
            i = sib.index(t.id) + 1
            while i < len(sib):
                n = m.nodes[sib[i]]
                if n.artifact or n.role in ("Link", "Annot"):
                    i += 1
                    continue
                if n.role not in ("P", "Note") or n.is_heading or n.page not in t.pages():
                    break
                mm = NOTE_MARK_RE.match(n.text or "")
                has_lbl = any(m.nodes[c].role == "Lbl" for c in n.children if c in m.nodes)
                if mm and n.bbox is not None and not has_lbl:
                    # Note > Lbl "*" + the note, as the client tags it
                    mark = mm.group("m")
                    lab = None
                    lines = self.lines_in(n, n.page)
                    if lines:
                        chars = [(c, b) for c, b in lines[0][1].chars if c.strip()]
                        if chars and "".join(c for c, _ in chars[:len(mark)]) == mark:
                            lab = pymupdf.Rect(chars[0][1])
                            for _, b in chars[1:len(mark)]:
                                lab |= pymupdf.Rect(b)
                    n.role = "Note"
                    n.note = f"table note {mark} (house style)"
                    if lab is not None:
                        n.text = _norm(n.text[mm.end():])
                        lbl = self.new_node("Lbl", n.page, lab, text=mark, priority=1)
                        m.add(lbl, n.id, 0)
                    made += 1
                i += 1
        self.bump("table_notes", made)

    def merge_continued(self) -> None:
        """"Table 3 Continued" in the next column or on the next page is the
        same table: its rows join Table 3, the header printed again is a
        running heading (the way InDesign exports a table split over frames)."""
        m = self.m
        num_re = re.compile(r"^\s*(?:table|tab\.)\s*(?P<n>[\dIVX]+[A-Za-z]?)\b", re.I)
        by_num: dict[str, StructNode] = {}
        merged = 0
        for t in [n for n in m.ordered_nodes() if n.role == "Table" and not n.artifact and n.parent is None]:
            sib = m.root_children
            at = sib.index(t.id)
            prev = m.nodes[sib[at - 1]] if at > 0 else None
            cap = prev if prev is not None and prev.role == "Caption" else None
            label = cap.text if cap is not None else ""
            if not label:
                # a continued-table title the analysis already set aside as a
                # running title, printed just above this part
                tb = self.disp(t.page, t.bbox) if t.bbox else None
                for a in m.nodes.values():
                    if a.artifact and a.page == t.page and a.bbox and tb is not None:
                        ab = self.disp(a.page, a.bbox)
                        if 0 <= tb.y0 - ab.y1 < 30 and ab.x0 < tb.x1 and ab.x1 > tb.x0 \
                                and TABLE_CONTINUED_RE.match(a.text or ""):
                            label = a.text
                            break
            mm = num_re.match(label or "")
            if not mm:
                continue
            key = mm.group("n").upper()
            if not TABLE_CONTINUED_RE.match(label):
                by_num[key] = t
                continue
            first = by_num.get(key)
            if first is None or first.id not in m.nodes:
                continue
            if self.join_table(first, t):
                if cap is not None:
                    self.to_artifact(cap, "continued table title (house style)", "Header")
                merged += 1
        self.bump("tables_continued", merged)

    def rows_of(self, t: StructNode) -> list[StructNode]:
        out = []
        for c in t.children:
            n = self.m.nodes.get(c)
            if n is None:
                continue
            if n.role == "TR":
                out.append(n)
            elif n.role in ("THead", "TBody", "TFoot"):
                out.extend(self.m.nodes[x] for x in n.children if x in self.m.nodes and self.m.nodes[x].role == "TR")
        return out

    def join_table(self, first: StructNode, t: StructNode) -> bool:
        m = self.m
        rows, base = self.rows_of(t), self.rows_of(first)
        if not base:
            return False
        width = lambda tr: sum(max(1, m.nodes[c].colspan) for c in tr.children if c in m.nodes)
        if rows and base and width(rows[-1]) != width(base[-1]):
            return False
        head = []
        for tr in rows:
            cells = [m.nodes[c] for c in tr.children if c in m.nodes]
            if cells and all(c.role == "TH" or not (c.text or "").strip() for c in cells) \
                    and any(c.role == "TH" for c in cells):
                head.append(tr)
            else:
                break
        for tr in head:
            boxes = [d.bbox for d in [tr] + m.descendants(tr.id) if d.bbox is not None and d.page == tr.page]
            if boxes:
                box = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                       max(b[2] for b in boxes), max(b[3] for b in boxes))
                art = StructNode(id=m.new_id("kyart"), role="Artifact", page=tr.page, bbox=box,
                                 text=" | ".join(m.nodes[c].text for c in tr.children if c in m.nodes),
                                 artifact=True, artifact_subtype="Pagination", pagination_subtype="Header",
                                 note="table header printed again (house style)")
                m.add(art)
            m.detach(tr.id)
            m._drop_subtree(tr.id)
        host = first
        holder = next((m.nodes[c] for c in reversed(first.children) if c in m.nodes
                       and m.nodes[c].role == "TBody"), None)
        if holder is not None:
            host = holder
        for tr in rows[len(head):]:
            m.detach(tr.id)
            m.add(tr, host.id)
        if t.bbox is not None:
            first.parts = list(first.parts) + [[t.page, *t.bbox]] + [list(p) for p in t.parts]
        first.note = (first.note + "; " if first.note else "") + f"continues on page {t.page + 1} (house style)"
        m.remove(t.id, promote_children=False)
        return True

    def stamp_links(self) -> None:
        """The link in a download stamp printed up the margin ("Downloaded from
        http://bmjopen.bmj.com/"): the stamp is the website's, not the
        article's — its words are artifacts and its link annotation is taken
        out of the built file, so no stray Link is read at the end of a page."""
        m = self.m
        stamps = [a for a in m.nodes.values() if a.artifact and a.bbox and a.pagination_subtype == "Watermark"]
        if not stamps:
            return
        moved = 0
        for ln in [n for n in m.nodes.values() if n.role == "Link" and n.annots and n.bbox]:
            hit = sum(max(0.0, min(ln.bbox[2], a.bbox[2]) - max(ln.bbox[0], a.bbox[0])) *
                      max(0.0, min(ln.bbox[3], a.bbox[3]) - max(ln.bbox[1], a.bbox[1]))
                      for a in stamps if a.page == ln.page)
            area = max((ln.bbox[2] - ln.bbox[0]) * (ln.bbox[3] - ln.bbox[1]), 1e-6)
            if hit < 0.5 * area:
                continue
            drop = m.info.setdefault("drop_annots", [])
            drop.extend(a for a in ln.annots if a not in drop)
            host = m.nodes.get(ln.parent) if ln.parent else None
            m.remove(ln.id, promote_children=False)
            if host is not None and host.role == "Reference" and not host.children:
                m.remove(host.id)
            moved += 1
        self.bump("stamp_links_removed", moved)

    def continued(self) -> None:
        """"Continued" under a table that runs on is pagination; a continued
        table that only repeats its header row is a running heading."""
        m = self.m
        for n in list(m.nodes.values()):
            if n.artifact or n.role not in ("P", "Caption") or n.children:
                continue
            if CONTINUED_RE.match(n.text or ""):
                self.to_artifact(n, "table continues (house style)", "Footer")
                self.bump("continued_marks")
        for t in [n for n in m.nodes.values() if n.role == "Table" and not n.artifact]:
            cells = [d for d in m.descendants(t.id) if d.role in ("TD", "TH")]
            if cells and all(c.role == "TH" for c in cells):
                sib = m.siblings(t.id)
                at = sib.index(t.id)
                prev = m.nodes[sib[at - 1]] if at > 0 else None
                if prev is not None and prev.role == "Caption" and TABLE_CONTINUED_RE.match(prev.text or ""):
                    self.to_artifact(prev, "continued table heading (house style)", "Header")
                    t.artifact = True
                    t.note = "repeated table header (house style)"
                    for d in m.descendants(t.id):
                        d.artifact = True
                    self.bump("repeated_headers")

    # ------------------------------------------------------- back matter
    def back_matter(self) -> None:
        m = self.m
        for n in list(m.nodes.values()):
            if n.artifact or n.id not in m.nodes:
                continue
            t = _norm(n.text)
            if n.role in ("P", "H4", "H5", "H6") and re.fullmatch(r"(ORCID iDs?|Author affiliations?)", t, re.I):
                n.role = "H3"
                self.bump("back_headings")
        # the affiliations after "Author affiliations"
        for h in [n for n in m.ordered_nodes() if n.is_heading and re.fullmatch(
                AFFIL_HEAD, _norm(n.text), re.I)]:
            sib = m.siblings(h.id)
            i = sib.index(h.id) + 1
            while i < len(sib):
                n = m.nodes[sib[i]]
                if n.is_heading:
                    break
                if n.role in ("P", "Note"):
                    got = self.split_affiliations(n)
                    if not got:
                        break
                    sib = m.siblings(got[0].id)
                    i = sib.index(got[-1].id) + 1
                    continue
                i += 1

    def back_paragraphs(self) -> None:
        """The back matter as Kriyadocs tags it: one P per run-in paragraph
        ("Data availability statement …" with the line under it), one P per
        name under "ORCID iDs"."""
        m = self.m
        roots = m.root_children
        k = 0
        while k + 1 < len(roots):
            a, b = m.nodes[roots[k]], m.nodes[roots[k + 1]]
            if a.role == "P" and b.role == "P" and a.bbox is not None and b.bbox is not None \
                    and RUN_IN_RE.match(_norm(a.text)) and not RUN_IN_RE.match(_norm(b.text)) \
                    and a.page == b.page:
                pa, da = self.box_of(a)
                db = self.disp(b.page, b.bbox)
                la = [l for _, l in self.lines_in(a, a.page)]
                lh = statistics.median([l.y1 - l.y0 for l in la]) if la else 9.0
                last = max((self.disp(a.page, r) for r in a.rects_on(a.page)), key=lambda r: r.y1)
                if abs(last.x0 - db.x0) < 3 and 0 <= db.y0 - last.y1 < 0.45 * lh:
                    m.absorb(a.id, b.id)
                    a.text = _norm(f"{a.text} {b.text}")
                    self.bump("back_joined")
                    continue
            k += 1
        for h in [n for n in m.ordered_nodes() if n.is_heading and re.fullmatch(r"ORCID iDs?", _norm(n.text), re.I)]:
            sib = m.siblings(h.id)
            i = sib.index(h.id) + 1
            while i < len(sib):
                n = m.nodes[sib[i]]
                if n.is_heading or n.role != "P":
                    break
                lines = self.lines_in(n, n.page)
                if len(lines) > 1:
                    made = []
                    for p, l in lines:
                        made.append(self.new_node("P", p, pymupdf.Rect(l.bbox), text=_norm(l.text),
                                                  note="ORCID iD (house style)"))
                    for c in list(n.children):
                        cn = m.nodes[c]
                        _, cb = self.box_of(cn)
                        home = made[0]
                        if cb is not None:
                            for q in made:
                                if self.disp(q.page, q.bbox).contains(pymupdf.Point((cb.x0 + cb.x1) / 2,
                                                                                    (cb.y0 + cb.y1) / 2)):
                                    home = q
                        m.detach(c)
                        m.add(cn, home.id)
                    self.replace(n, made)
                    self.bump("orcid_lines", len(made))
                    sib = m.siblings(h.id)
                    i = sib.index(made[-1].id) + 1
                    continue
                i += 1

    def formulas(self) -> None:
        """A display equation the analysis read in pieces (a Formula and the
        bits of its fraction and sums around it as P): one Formula. The
        definitions after "where:" are a P each, as the client tags them."""
        m = self.m
        # "ln (output)i = ln(HFi) + … where:" read as one paragraph: the
        # equation is a Formula, "where:" a P of its own
        for n in [x for x in list(m.nodes.values()) if x.role == "P" and not x.artifact and not x.children]:
            if n.id not in m.nodes or n.bbox is None or n.parts:
                continue
            lines = self.lines_in(n, n.page)
            if not lines:
                continue
            tail = lines[-1][1] if re.fullmatch(r"where:?", _norm(lines[-1][1].text), re.I) else None
            eq = lines[:-1] if tail is not None else lines
            if not eq or not all(_equation(l.text) for _, l in eq):
                continue
            from .mathspeak import speak
            box = pymupdf.Rect(eq[0][1].bbox)
            for _, l in eq[1:]:
                box |= pymupdf.Rect(l.bbox)
            text = _norm(" ".join(l.text for _, l in eq))
            f = self.new_node("Formula", n.page, box, text=text, alt=speak(text) or text,
                              note="equation (house style) — check its /Alt")
            made = [f]
            if tail is not None:
                made.append(self.new_node("P", n.page, pymupdf.Rect(tail.bbox), text=_norm(tail.text)))
            self.replace(n, made)
            self.bump("equations")
        roots = m.root_children
        for f in [m.nodes[r] for r in list(roots) if m.nodes[r].role == "Formula" and not m.nodes[r].artifact]:
            if f.id not in m.nodes or f.bbox is None:
                continue
            fb = self.disp(f.page, f.bbox)
            h = max(8.0, fb.height)
            before = -1
            while before != self.stats.get("formula_pieces", 0):
              before = self.stats.get("formula_pieces", 0)
              for step in (-1, 1):
                while True:
                    sib = m.root_children
                    i = sib.index(f.id) + step
                    if not (0 <= i < len(sib)):
                        break
                    n = m.nodes[sib[i]]
                    if n.artifact or n.page != f.page or n.role not in ("P", "Formula") or n.children:
                        break
                    p, nb = self.box_of(n)
                    if nb is None:
                        if not (n.text or "").strip():
                            m.remove(n.id)
                            continue
                        break
                    t = _norm(n.text)
                    words = re.findall(r"[a-z]{3,}", t)           # prose, not the symbols
                    if re.match(r"^\s*where\b", t, re.I) or len(words) > 3:
                        break
                    near = (nb.y0 - fb.y1 < 1.2 * h and fb.y0 - nb.y1 < 1.2 * h
                            and nb.x0 < fb.x1 + 3 * h and nb.x1 > fb.x0 - 3 * h)
                    if not near:
                        break
                    m.absorb(f.id, n.id)
                    fb |= nb
                    self.bump("formula_pieces")
            if self.stats.get("formula_pieces"):
                f.confidence = min(f.confidence, 0.5)
                f.note = (f.note + "; " if f.note else "") + "equation joined from its pieces — check its /Alt"
        # where: + one P per definition
        for n in [x for x in list(m.nodes.values()) if x.role == "P" and re.fullmatch(r"where:?", _norm(x.text), re.I)]:
            if n.id not in m.nodes:
                continue
            while True:
                sib = m.siblings(n.id)
                # the paragraph after the last definition made so far
                i = sib.index(n.id) + 1
                while i < len(sib) and (m.nodes[sib[i]].note or "") == "definition (house style)":
                    i += 1
                if i >= len(sib):
                    break
                d = m.nodes[sib[i]]
                if d.role != "P" or d.artifact:
                    break
                lines = self.lines_in(d, d.page)
                term = [bool(re.match(r"^\s*\S.{0,30}?\s?[-–—:]\s", l.text)) for _, l in lines]
                if not lines or not term[0]:
                    break
                groups: list[list] = []
                for (p, l), t in zip(lines, term):
                    if t or not groups:
                        groups.append([p, pymupdf.Rect(l.bbox), [l.text]])
                    else:
                        groups[-1][1] |= pymupdf.Rect(l.bbox)
                        groups[-1][2].append(l.text)
                made = [self.new_node("P", p, box, text=_norm(" ".join(ts)), note="definition (house style)")
                        for p, box, ts in groups]
                for c in list(d.children):
                    cn = m.nodes[c]
                    _, cb = self.box_of(cn)
                    home = made[0]
                    if cb is not None:
                        for q in made:
                            if self.disp(q.page, q.bbox).intersects(cb):
                                home = q
                    m.detach(c)
                    m.add(cn, home.id)
                self.replace(d, made)
                self.bump("definitions", len(made))
        for f in [x for x in m.nodes.values() if x.role == "Formula" and not x.artifact]:
            self.speak_formula(f)

    def speak_formula(self, f: StructNode) -> None:
        """The client's Formula says the equation in words, as /ActualText
        ("CCI equals one-fifth times …") — read from where the glyphs sit, so a
        stacked fraction is "over" and a lowered i is "sub i"."""
        from .mathspeak import speak, speak_region
        if f.bbox is None or f.page < 0:
            return
        m = self.m
        rects = [self.disp(f.page, r) for r in f.rects_on(f.page)]
        if not rects:
            return
        region = pymupdf.Rect(rects[0])
        for r in rects[1:]:
            region |= r
        mine = {f.id} | {d.id for d in m.descendants(f.id)}
        other = []
        for n in m.nodes.values():
            if n.id in mine or n.page != f.page or n.bbox is None or n.role in ("Document", "Sect", "Part", "Art", "Div"):
                continue
            mid = pymupdf.Point((region.x0 + region.x1) / 2, (region.y0 + region.y1) / 2)
            for r in n.rects_on(f.page):
                d = self.disp(f.page, r)
                # a line of the text above or below touching the equation's
                # box is left out; a tag spread over the equation is not
                if d.intersects(region) and not d.contains(mid) and not any(d.contains(x) for x in rects):
                    other.append(d)
        said, linear = speak_region(self.doc[f.page], region, exclude=other)
        if not said:
            said = speak(f.text or "")
        if not said:
            return
        f.alt = said
        f.actual_text = said
        if linear:
            f.text = linear
        self.bump("formulas_spoken")

    def cell_lead_ins(self) -> None:
        """A list inside a table cell: the line before it is the cell's own
        text (TD "A composite variable …" > L), not a P of its own."""
        m = self.m
        for n in [x for x in m.nodes.values() if x.role == "P" and not x.children
                  and x.note == "lead-in line of the list inside this cell"]:
            cell = m.nodes.get(n.parent) if n.parent else None
            if cell is None or cell.role not in ("TD", "TH"):
                continue
            m.remove(n.id)
            self.bump("cell_lead_ins")

    def lists(self) -> None:
        """A bulleted list says so: ListNumbering Disc."""
        for n in self.m.nodes.values():
            if n.role != "L" or n.artifact or (n.style or {}).get("bibliography"):
                continue
            lbls = [d for d in self.m.descendants(n.id) if d.role == "Lbl"]
            if lbls and all((d.text or "").strip()[:1] in BULLET_LABELS for d in lbls):
                n.list_numbering = "Disc"

    def label_note(self, n: StructNode) -> bool:
        """A Note the editor made ("1 Department of …", "*T-test."): the mark
        it opens with becomes its Lbl, as the client tags every Note."""
        m = self.m
        if any(m.nodes[c].role == "Lbl" for c in n.children if c in m.nodes) or n.bbox is None:
            return False
        t = _norm(n.text)
        mm = re.match(r"^(\d{1,3}(?![\d.,:])|[*\u2020\u2021\u00a7\u00b6#]{1,3})\s*(?=\S)", t)
        if not mm:
            return False
        mark = mm.group(1)
        lab = None
        lines = self.lines_in(n, n.page)
        if lines:
            chars = [(c[0], c[1]) for c in lines[0][1].chars if c[0].strip()]
            if chars and "".join(c for c, _ in chars[:len(mark)]) == mark:
                lab = pymupdf.Rect(chars[0][1])
                for _, b in chars[1:len(mark)]:
                    lab |= pymupdf.Rect(b)
        if lab is None:
            return False
        n.text = _norm(t[mm.end():])
        lbl = self.new_node("Lbl", n.page, lab, text=mark, priority=1)
        m.add(lbl, n.id, 0)
        self.bump("notes_labelled")
        return True

    def relink(self, ids: set) -> int:
        """Cross-references and note marks inside the tags `ids`, linked again."""
        from .analyzer import to_pdf as _to_pdf
        from .xref import Scanner, apply as xapply
        m = self.m
        pages = {m.nodes[i].page for i in ids if m.nodes[i].page is not None and m.nodes[i].page >= 0}
        if not pages:
            return 0
        lines = [self.an.page_lines[p] if p in pages else [] for p in range(len(self.doc))]
        sc = Scanner(m, lines, self.an.opts, self.doc)
        found = [mn for mn in sc.find(lambda pno, box: _to_pdf(self.doc[pno], box))
                 if mn.owner in ids and mn.kind not in ("footnote_back", "caption_back")]
        if not found:
            return 0
        st = xapply(m, found, self.an.opts)
        self.bump("relinked", st.get("linked", 0))
        return st.get("linked", 0)

    def note_text_p(self) -> None:
        """Note > Lbl (the number or mark) + P (the text), as the client tags
        every Note: the words of a Note that has its Lbl go in a P of their
        own, and the links / references printed in them go inside that P."""
        m = self.m
        made = 0
        for n in [x for x in m.ordered_nodes() if x.role == "Note" and not x.artifact]:
            if n.id not in m.nodes or n.bbox is None:
                continue
            kids = [m.nodes[c] for c in n.children if c in m.nodes]
            if not any(c.role == "Lbl" for c in kids):
                continue                      # no mark found: nothing to split off
            body = next((c for c in kids if c.role == "P" and not c.artifact), None)
            if body is None:
                body = self.new_node("P", n.page, None, text=n.text or "",
                                     note="Note text (house style)")
                body.bbox = n.bbox
                body.parts = [list(q) for q in n.parts]
                body.src_keys, n.src_keys = list(n.src_keys), []     # imported content items go with the words
                body.lang = n.lang
                m.add(body, n.id)
                made += 1
            # whatever else sits in the Note (Reference > Link, Span …) is part of its text
            lbl = next(c for c in kids if c.role == "Lbl")
            for c in kids:
                if c.id == body.id or c.role in ("Lbl", "P"):
                    continue
                # a link printed over the mark itself (the way back to the text) stays in the Lbl
                inside = lbl.bbox is not None and c.bbox is not None and c.page == lbl.page \
                    and pymupdf.Rect(c.bbox).get_area() > 0 \
                    and (pymupdf.Rect(c.bbox) & pymupdf.Rect(lbl.bbox)).get_area() > 0.5 * pymupdf.Rect(c.bbox).get_area()
                m.detach(c.id)
                m.add(c, lbl.id if inside else body.id)
        self.bump("note_text_p", made)

    def renumber_notes(self) -> None:
        """NOTE 1, NOTE 2 … in reading order (the client's IDs), after an edit."""
        k = 0
        for n in self.m.ordered_nodes():
            if n.role == "Note" and not n.artifact:
                k += 1
                if not n.id_attr or re.fullmatch(r"NOTE \d+", n.id_attr):
                    n.id_attr = f"NOTE {k}"

    def note_ids(self) -> None:
        k = 0
        for n in self.m.ordered_nodes():
            if n.role == "Note" and not n.artifact:
                k += 1
                if not n.id_attr:
                    n.id_attr = f"NOTE {k}"

    # =============================================================== post
    def post(self) -> dict:
        m = self.m
        try:
            self.stray_note_links()
            self.note_link_names()
            self.bare_references()
            self.merge_links()
            self.split_citations()
            self.wrap_links()
            self.unwrap_web_links()
            self.note_text_p()
            # the marks the search linked, in printed order with the icons
            for n in list(m.nodes.values()):
                if (n.style or {}).get("house") == "authors":
                    self.sort_inline(n)
        except Exception as exc:
            m.warnings.append(f"house style (links): {exc}")
        return self.stats

    def stray_note_links(self) -> None:
        """A raised number in the body linked to an affiliation: only the
        authors' marks point at the affiliations."""
        m = self.m
        gone = 0
        for ln in [n for n in m.nodes.values() if n.role == "Link" and not n.annots]:
            x = (ln.style or {}).get("xref") or {}
            if x.get("kind") != "footnote":
                continue
            tgt = m.nodes.get(x.get("node") or "")
            if tgt is None or not (tgt.note or "").startswith("affiliation"):
                continue
            if any((a.style or {}).get("house") == "authors" for a in m.ancestors(ln.id)):
                continue
            host = m.nodes.get(ln.parent) if ln.parent else None
            m.remove(ln.id, promote_children=True)
            if host is not None and host.role == "Reference" and not host.children:
                m.remove(host.id)
            gone += 1
        self.bump("stray_note_links", gone)

    def note_link_names(self) -> None:
        """A note mark's link says where it goes ("Affiliation 1", "Table note
        †: T-test.") — a screen reader would only say "1" or "dagger"."""
        m = self.m
        named = 0
        for ln in [n for n in m.nodes.values() if n.role == "Link" and not n.artifact]:
            x = (ln.style or {}).get("xref") or {}
            if x.get("kind") != "footnote" or (ln.style or {}).get("contents"):
                continue
            tgt = m.nodes.get(x.get("node") or "")
            if tgt is None:
                continue
            lbl = next((m.nodes[c] for c in tgt.children if c in m.nodes and m.nodes[c].role == "Lbl"), None)
            mark = _norm(lbl.text if lbl is not None else (ln.text or "")) or _norm(ln.text)
            body = _norm(tgt.text)
            if len(body) > 90:
                body = body[:90].rsplit(" ", 1)[0] + " …"
            if (tgt.note or "").startswith("affiliation"):
                said = f"Affiliation {mark}: {body}" if body else f"Affiliation {mark}"
            elif (tgt.note or "").startswith("table note"):
                said = f"Table note {mark}: {body}" if body else f"Table note {mark}"
            else:
                said = f"Note {mark}: {body}" if body else f"Note {mark}"
            ln.style = dict(ln.style or {}, contents=said)
            named += 1
        self.bump("note_links_named", named)

    def bare_references(self) -> None:
        """A Reference is the wrapper of a Link here; a mention the search
        found but did not link (a note's own number) stays plain text."""
        m = self.m
        gone = 0
        for n in list(m.nodes.values()):
            if n.role == "Reference" and n.id in m.nodes and "xref_kind" in (n.style or {}) \
                    and not any(d.role == "Link" for d in m.descendants(n.id)):
                m.remove(n.id, promote_children=True)
                gone += 1
        self.bump("unlinked_mentions_plain", gone)

    def merge_links(self) -> None:
        """One address printed over two lines is one Link with both
        annotations."""
        m = self.m
        for parent in [None] + [n.id for n in m.nodes.values() if n.children]:
            kids = m.root_children if parent is None else m.nodes[parent].children
            k = 0
            while k + 1 < len(kids):
                a, b = m.nodes.get(kids[k]), m.nodes.get(kids[k + 1])
                if a is not None and b is not None and a.role == b.role == "Link" and a.annots and b.annots \
                        and a.page == b.page and self.uri(a) and self.uri(a) == self.uri(b):
                    m.absorb(a.id, b.id)
                    a.annots = list(a.annots) + [x for x in b.annots if x not in a.annots]
                    a.text = _norm(f"{a.text}{b.text}")
                    self.bump("links_merged")
                    continue
                k += 1

    def unwrap_web_links(self) -> None:
        """A web or e-mail address is a plain Link tag (V4.17): the Reference
        around it goes, the Link stays where it is printed."""
        m = self.m
        gone = 0
        for ln in [n for n in m.ordered_nodes() if n.role == "Link" and not n.artifact]:
            par = m.nodes.get(ln.parent) if ln.parent else None
            if par is None or par.role != "Reference" or not self.is_web(ln):
                continue
            if any(m.nodes[c].role == "Link" and not self.is_web(m.nodes[c])
                   for c in par.children if c in m.nodes):
                continue                       # a Reference shared with a cross-reference
            m.remove(par.id, promote_children=True)
            gone += 1
        self.bump("web_links_without_reference", gone)

    # ------------------------------------------------ citation numbers
    CITE_TOKEN = re.compile(r"\d{1,4}(?:\s*[\u2013\u2014\u2012\-]\s*\d{1,4})?")

    def _glyphs(self, pno: int, rect_disp) -> list:
        """The characters printed in a box on a page, in reading order (display
        space): [(char, Rect)]."""
        from .textflags import RAW_FLAGS
        page = self.doc[pno]
        clip = pymupdf.Rect(rect_disp) + (-1, -1, 1, 1)
        out = []
        try:
            raw = page.get_text("rawdict", clip=clip, flags=RAW_FLAGS)
        except Exception:
            return out
        for b in raw.get("blocks", []):
            for l in b.get("lines", []):
                for sp in l.get("spans", []):
                    for ch in sp.get("chars", []):
                        r = pymupdf.Rect(ch["bbox"])
                        c = pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
                        if pymupdf.Rect(rect_disp).contains(c):
                            out.append((ch["c"], r))
        out.sort(key=lambda t: (round((t[1].y0 + t[1].y1) / 8), t[1].x0))
        return out

    def _tokens(self, glyphs: list) -> list:
        """Citation numbers in a run of glyphs: "2 3", "35,36,37" → one each;
        "38–40" → one (a range is one citation). [(text, display Rect)]."""
        text = "".join(c for c, _ in glyphs)
        out = []
        for mt in self.CITE_TOKEN.finditer(text):
            rs = [glyphs[i][1] for i in range(mt.start(), mt.end()) if glyphs[i][0].strip()]
            if not rs:
                continue
            box = rs[0]
            for r in rs[1:]:
                box = box | r
            out.append((re.sub(r"\s+", "", mt.group(0)), box))
        rest = self.CITE_TOKEN.sub("", text)
        if re.search(r"[^\s,.;:()\[\]]", rest):
            return []                           # more than numbers: not a citation run
        return out

    def _bib_entries(self) -> dict:
        """Reference list entries by their number: "12" → the LI."""
        if getattr(self, "_bib", None) is not None:
            return self._bib
        m = self.m
        out: dict = {}
        for li in [n for n in m.nodes.values() if n.role == "LI" and not n.artifact]:
            lbl = next((m.nodes[c] for c in li.children if c in m.nodes and m.nodes[c].role == "Lbl"), None)
            if lbl is None:
                continue
            num = re.sub(r"[.\s\[\]()]", "", lbl.text or "")
            if num.isdigit() and num not in out:
                out[num] = li
        self._bib = out
        return out

    def split_citations(self) -> None:
        """Citation numbers printed one after another and linked as one ("2 3",
        "35,36,37" — the file's own annotation covers them all): one zone and
        one link per number, each to its own entry of the reference list. A
        range ("38–40") stays one zone and one link. The full stop a citation
        is set after is not part of its zone (V4.17).

        The file's annotation keeps the first number (its rectangle cut down to
        it at build, model.info["annot_rects"]); each further number gets a new
        Link that the build gives its own annotation. Safe to run again: the
        numbers already made are not made twice, and after a new search for
        links (which takes out the links it made) they are made again."""
        m = self.m
        bib = self._bib_entries()
        if not bib:
            return
        made = trimmed = 0
        info_rects = m.info.setdefault("annot_rects", {})
        for ln in [n for n in m.ordered_nodes() if n.role == "Link" and n.annots and not n.artifact]:
            if self.is_web(ln) or ln.page is None or ln.page < 0 or ln.page >= len(self.doc):
                continue
            st = dict(ln.style or {})
            rects = st.get("cite_rects") or [list(r) for r in ln.rects_on(ln.page)]
            if not rects:
                continue
            glyphs = []
            for r in rects:
                glyphs += self._glyphs(ln.page, self.disp(ln.page, r))
            toks = self._tokens(glyphs)
            if not toks:
                continue
            if any(t.split("\u2013")[0].split("-")[0].split("\u2014")[0] not in bib for t, _ in toks[1:]):
                continue                        # a number with no entry: leave the link as it is
            first_text, first_box = toks[0]
            new_box = self.pdf(ln.page, first_box)
            if len(toks) == 1:
                if ln.bbox is not None and any(abs(a - b) > 0.6 for a, b in zip(ln.bbox, new_box)) \
                        and not ln.parts:
                    st["cite_rects"] = rects
                    ln.style = st
                    ln.bbox = new_box
                    ln.text = first_text
                    trimmed += 1
                continue
            st["cite_rects"] = rects
            st["contents"] = f"Reference {first_text}"
            ln.style = st
            ln.bbox = new_box
            ln.parts = []
            ln.text = first_text
            for a in ln.annots[:1]:
                info_rects[str(a)] = list(new_box)
            # the further numbers, after the first (or after its Reference)
            host = m.nodes.get(ln.parent) if ln.parent else None
            anchor = host if host is not None and host.role == "Reference" else ln
            sib = m.siblings(anchor.id)
            at = sib.index(anchor.id) + 1
            have = {(x.style or {}).get("cite_token") for x in m.nodes.values()
                    if (x.style or {}).get("cite_of") == ln.id}
            for tok, box in toks[1:]:
                if tok in have:
                    at += 1
                    continue
                num = re.split(r"[\u2013\u2014\u2012\-]", tok)[0]
                li = bib[num]
                tp, tb = (li.page, li.bbox) if li.bbox is not None else (None, None)
                if tb is None:
                    for d in m.descendants(li.id):
                        if d.bbox is not None:
                            tp, tb = d.page, d.bbox
                            break
                if tb is None:
                    continue
                new = StructNode(id=m.new_id("cite"), role="Link", page=ln.page, bbox=self.pdf(ln.page, box),
                                 text=tok, source="auto", priority=max(1, ln.priority),
                                 note="citation number — its own link to its reference entry")
                new.style = {"xref": {"kind": "reference", "page": tp, "top": max(tb[1], tb[3]),
                                      "node": li.id, "key": f"reference {tok}",
                                      "label": f"reference {tok}"},
                             "cite_of": ln.id, "cite_token": tok,
                             "contents": f"Reference {tok}"}
                m.nodes[new.id] = new
                new.parent = anchor.parent
                sib.insert(at, new.id)
                at += 1
                made += 1
        self.bump("citations_split", made)
        self.bump("citation_zones_trimmed", trimmed)

    def wrap_links(self) -> None:
        """Every link in the text as Reference > Link."""
        m = self.m
        made = 0
        for ln in [n for n in m.ordered_nodes() if n.role == "Link" and not n.artifact]:
            if (ln.style or {}).get("no_reference") or self.is_web(ln):
                continue
            par = m.nodes.get(ln.parent) if ln.parent else None
            if par is not None and par.role == "Reference":
                continue
            if any(a.role in ("TOC", "TOCI", "Index") for a in m.ancestors(ln.id)):
                continue
            sib = m.siblings(ln.id)
            at = sib.index(ln.id)
            ref = StructNode(id=m.new_id("kyref"), role="Reference", page=ln.page, source="auto",
                             note="Reference > Link (house style)")
            sib[at] = ref.id
            m.nodes[ref.id] = ref
            ref.parent = ln.parent
            ln.parent = ref.id
            ref.children.append(ln.id)
            made += 1
        self.bump("references_wrapped", made)
