"""Reference lists: one entry, one BibEntry, and every citation linked to it.

A reference list set as ordinary paragraphs comes out of layout analysis as P
tags cut wherever a line break looks like a paragraph break. In the sample book
the entry

    Kame'enui, E. J., Carnine, D. W., Dixon, R. C., Simmons, D. C., & Coyne, M. D.
        (2002). Effective teaching strategies that accommodate diverse learners …
        Upper Saddle River, NJ: Prentice Hall.

ends its first line with "D." — a full stop — so it was read as three
paragraphs, and NVDA said "Upper Saddle River, NJ: Prentice Hall." as a
paragraph of its own.

Here the entries are found from the printed lines themselves, not from where
the paragraph breaks fell:

* a hanging indent — an entry's first line starts at the margin, the lines
  after it are set in (APA, Chicago, MLA);
* a first-line indent — the other way round;
* a label — "1.", "[1]", "(1)" (numbered / Vancouver styles);
* the space between entries — wider than the space between lines of one entry;
* the author pattern — "Surname, A. B." after a line that ended an entry;
* and, when none of these settles it and the AI is on, the text model.

Each entry becomes L > LI > [Lbl] + LBody > BibEntry ("list", the default:
NVDA announces "list with 12 items" and moves item by item), or one BibEntry
straight under the section ("entries"). Its zones are the entry's own lines,
so the tagger hands it exactly those glyphs, in a file tagged by the tool and
in one whose tags were imported.
"""
from __future__ import annotations

import collections
import re
import statistics
from dataclasses import dataclass

from .compat import house
from .model import DocModel, StructNode

REF_HEAD_RE = re.compile(
    r"^\s*(?:(?:chapter|appendix|part|unit|section)\s+[\w.]+\s*[:.–—-]?\s*|"
    r"\d+(?:\.\d+)*\.?\s+|[IVXLC]+\.\s+)?"
    r"(?:references?(?:\s+(?:list|cited))?|bibliography|selected\s+bibliography|"
    r"annotated\s+bibliography|works\s+cited|works\s+consulted|literature\s+cited|"
    r"cited\s+literature|sources(?:\s+cited|\s+consulted)?|further\s+reading|"
    r"suggested\s+readings?|recommended\s+readings?|professional\s+references|"
    r"research\s+references|bibliographie|literatur(?:verzeichnis)?|"
    r"r[ée]f[ée]rences(?:\s+bibliographiques)?)\s*:?\s*$", re.I)
NUM_LABEL_RE = re.compile(r"^\s*(?P<lbl>\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}[.)])(?=\s|[A-Z‘“\"'])")
# a bare number before the entry ("6  Steward DL, …" — BMJ, Vancouver style):
# only taken as the label when the numbers count up one by one
BARE_NUM_RE = re.compile(r"^\s*(?P<lbl>\d{1,3})(?=\s+[A-Z‘“\"'])")
NAME = r"[A-Z][A-Za-zÀ-ſ'’\-]+"
AUTHOR_START_RE = re.compile(
    rf"^\s*(?:{NAME}(?:\s+{NAME}){{0,2}},\s+(?:[A-Z]\.|{NAME})"          # Surname, A.  /  Surname, Anne
    rf"|{NAME}(?:\s+[\w&'’\-]+){{0,12}}\.\s*\((?:\d{{4}}|n\.d\.)"   # Organisation. (2000)
    rf"|{NAME}\s+[A-Z]{{1,3}}[,.]\s)")                                   # Surname AB,  (Vancouver)
YEAR_RE = re.compile(r"\b(?:1[5-9]\d\d|20\d\d)[a-z]?\b|\bn\.d\.|\bin press\b", re.I)
BIB_HINT_RE = re.compile(r"\(Eds?\.\)|\bpp?\.\s*\d|\bdoi\b|https?://|\bvol\.|\bJournal\b|"
                         r"\bPress\b|\bPublish|\bRetrieved\b|\bUniversity\b", re.I)
STOP_ROLES = {"Table", "Figure", "Formula", "TOC", "Index", "Form", "Part", "Art", "Sect", "Div"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _is_heading(n: StructNode) -> bool:
    return n.is_heading or n.role == "H"


def _level(n: StructNode) -> int:
    if n.is_heading:
        return int(n.role[1])
    return 1 if n.role == "H" else 9


def subtree_text(model: DocModel, nid: str) -> str:
    n = model.nodes[nid]
    parts = [n.text] if (n.text or "").strip() else []
    for d in model.descendants(nid):
        if (d.text or "").strip() and not d.artifact:
            parts.append(d.text)
    return _norm(" ".join(parts))


def looks_bibliographic(text: str) -> bool:
    return bool(YEAR_RE.search(text) or BIB_HINT_RE.search(text))


def is_reference_heading(text: str) -> bool:
    t = _norm(text)
    return bool(t) and len(t) <= 60 and bool(REF_HEAD_RE.match(t))


# ------------------------------------------------------------- sections
def reference_sections(model: DocModel) -> list[dict]:
    """[{head, groups: [[member ids]]}] — the blocks after each reference heading.

    The run stops at the next heading of the same level or higher, at a table or
    figure, or when a later page starts with ordinary prose. A sub-heading
    ("Books", "Journal articles") is left as it is and starts a list of its own."""
    out = []
    for n in model.ordered_nodes():
        if n.artifact or not _is_heading(n) or not is_reference_heading(n.text):
            continue
        sibs = model.siblings(n.id)
        if n.id not in sibs:
            continue
        lvl = _level(n)
        groups: list[list[str]] = [[]]
        last_page = n.page
        for sid in sibs[sibs.index(n.id) + 1:]:
            s = model.nodes.get(sid)
            if s is None or s.artifact or (s.style or {}).get("page_number"):
                continue
            if _is_heading(s):
                if _level(s) <= lvl:
                    break
                groups.append([])
                continue
            if s.role in STOP_ROLES:
                break
            if house() and s.role in ("Link", "Annot") and s.annots and s.bbox is not None and \
                    not any(model.nodes[c].role not in ("Link", "Annot") for c in s.children if c in model.nodes):
                continue                      # a stray link (a margin stamp's URL) is not an entry
            txt = subtree_text(model, sid)
            if not txt:
                continue
            if s.page > last_page and len(txt) > 200 and not looks_bibliographic(txt):
                break                         # the next chapter's prose, no heading of its own
            groups[-1].append(sid)
            last_page = max([last_page, s.page] + [int(p[0]) for p in s.parts])
        groups = [g for g in groups if g]
        if groups:
            out.append({"head": n.id, "groups": groups})
    return out


def _done(model: DocModel, group: list[str]) -> bool:
    """Already one BibEntry per entry."""
    for sid in group:
        s = model.nodes[sid]
        if s.role == "BibEntry":
            continue
        if s.role == "L" and (s.style or {}).get("bibliography"):
            continue
        if s.role == "L":
            bodies = [d for d in model.descendants(sid) if d.role == "LBody"]
            if bodies and all(any(model.nodes[c].role == "BibEntry" for c in b.children
                                  if c in model.nodes) for b in bodies):
                continue
        return False
    return True


def problems(model: DocModel) -> dict:
    """Reference lists whose entries are not BibEntry tags yet."""
    secs = []
    paragraphs = 0
    for sec in reference_sections(model):
        todo = [g for g in sec["groups"] if not _done(model, g)]
        if not todo:
            continue
        secs.append(sec["head"])
        paragraphs += sum(1 for g in todo for sid in g if model.nodes[sid].role == "P")
    return {"sections": secs, "paragraphs": paragraphs}


# ----------------------------------------------------------------- lines
@dataclass
class BibLine:
    page: int
    line: object                 # analyzer.Line, display space
    text: str
    col: int = 0
    indent: float = 0.0
    gap: float | None = None     # space to the line above in the same column


def _rects_by_page(model: DocModel, nid: str) -> dict[int, list]:
    out: dict[int, list] = collections.defaultdict(list)
    for x in [model.nodes[nid]] + model.descendants(nid):
        if x.artifact:
            continue
        if x.bbox is not None and x.page >= 0:
            out[x.page].append(tuple(x.bbox))
        for p in x.parts:
            out[int(p[0])].append(tuple(p[1:5]))
    return out


def _columns(lines: list) -> list[int]:
    """Column number of each line: lines that overlap left to right share one."""
    order = sorted(range(len(lines)), key=lambda i: lines[i].x0)
    spans: list[list[float]] = []
    col_of = [0] * len(lines)
    for i in order:
        l = lines[i]
        for k, sp in enumerate(spans):
            if l.x0 < sp[1] - 2 and l.x1 > sp[0] + 2:
                sp[0], sp[1] = min(sp[0], l.x0), max(sp[1], l.x1)
                col_of[i] = k
                break
        else:
            spans.append([l.x0, l.x1])
            col_of[i] = len(spans) - 1
    rank = {k: r for r, k in enumerate(sorted(range(len(spans)), key=lambda k: spans[k][0]))}
    return [rank[c] for c in col_of]


def section_lines(model: DocModel, doc, page_lines: list, members: list[str], to_pdf) -> list[BibLine]:
    """The printed lines of these blocks, in page / column / top-to-bottom order."""
    want: dict[int, list] = collections.defaultdict(list)
    for sid in members:
        for pno, rects in _rects_by_page(model, sid).items():
            want[pno].extend(rects)
    out: list[BibLine] = []
    for pno in sorted(want):
        if pno >= len(page_lines) or not page_lines[pno]:
            continue
        rects = want[pno]
        mine = []
        for l in page_lines[pno]:
            if not (l.text or "").strip():
                continue
            b = to_pdf(pno, l.bbox)
            cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            if any(r[0] - 1 <= cx <= r[2] + 1 and r[1] - 1 <= cy <= r[3] + 1 for r in rects):
                mine.append(l)
        if not mine:
            continue
        cols = _columns(mine)
        lefts: dict[int, float] = {}
        for l, c in zip(mine, cols):
            lefts[c] = min(lefts.get(c, l.x0), l.x0)
        ordered = sorted(zip(mine, cols), key=lambda t: (t[1], round(t[0].y0, 1), t[0].x0))
        prev: dict[int, object] = {}
        for l, c in ordered:
            p = prev.get(c)
            gap = None if p is None else l.y0 - p.y1
            out.append(BibLine(pno, l, _norm(l.text), c, l.x0 - lefts[c], gap))
            prev[c] = l
    return out


# --------------------------------------------------------------- entries
def _ends_entry(text: str) -> bool:
    t = text.rstrip()
    return bool(t) and (t[-1] in ".)]" or bool(re.search(r"(\d+|https?://\S+|doi:\S+)$", t)))


def entry_starts(lines: list[BibLine], ai_cfg=None, use_ai: bool = False) -> tuple[list[int], str]:
    """Indexes of the lines that start an entry, and how that was decided."""
    n = len(lines)
    if n == 0:
        return [], ""
    if n == 1:
        return [0], "one line"
    size = statistics.median([getattr(l.line, "size", 10.0) or 10.0 for l in lines])
    tol = max(1.2, 0.12 * size)
    ind = [l.indent for l in lines]

    # numbered: "1.", "[1]", "(1)" at the margin — a one-digit number set
    # right-aligned with the two-digit ones sits half an em further in
    lab_tol = max(3 * tol, 0.9 * size) if house() else 3 * tol
    lab = [bool(NUM_LABEL_RE.match(l.text)) and l.indent <= lab_tol for l in lines]
    if lab[0] and sum(lab) >= 2:
        return [i for i in range(n) if lab[i]], "numbers"
    bare = bare_number_starts(lines, lab_tol) if house() else []
    if bare:
        return bare, "numbers"

    at_margin = [x <= tol for x in ind]
    set_in = [x >= 2.5 * tol for x in ind]
    # hanging indent: the first line at the margin, the rest set in
    if at_margin[0] and any(set_in) and sum(at_margin) >= 2:
        return [i for i in range(n) if at_margin[i]], "hanging indent"
    # first-line indent: the other way round
    if set_in[0] and any(at_margin) and sum(set_in) >= 2:
        return [i for i in range(n) if set_in[i]], "first-line indent"

    # the space between entries is wider than between the lines of one
    gaps = sorted(l.gap for l in lines[1:] if l.gap is not None)
    if len(gaps) >= 2:
        base = gaps[len(gaps) // 4]
        thr = base + max(1.5, 0.3 * size)
        big = [g > thr for g in gaps]
        if any(big) and not all(big):
            starts = [0]
            for i in range(1, n):
                g = lines[i].gap
                if (g is not None and g > thr) or \
                        (g is None and AUTHOR_START_RE.match(lines[i].text) and _ends_entry(lines[i - 1].text)):
                    starts.append(i)
            return starts, "space between entries"

    if use_ai and ai_cfg is not None:
        try:
            from .ai import bib_entry_starts
            got = bib_entry_starts(ai_cfg, [l.text for l in lines])
            if got:
                return got, "AI"
        except Exception:
            pass

    # the author pattern after a line that ended an entry
    starts = [0]
    for i in range(1, n):
        if (AUTHOR_START_RE.match(lines[i].text) or NUM_LABEL_RE.match(lines[i].text)) \
                and _ends_entry(lines[i - 1].text):
            starts.append(i)
    return starts, "author pattern"


def bare_number_starts(lines: list[BibLine], tol: float) -> list[int]:
    """Lines that open an entry with a bare number ("6 Steward DL"), when the
    numbers run on one by one from the first line."""
    if not lines:
        return []
    m = BARE_NUM_RE.match(lines[0].text)
    if not m or lines[0].indent > tol:
        return []
    out = [0]
    last = int(m.group("lbl"))
    seen = 0
    for i in range(1, len(lines)):
        mm = BARE_NUM_RE.match(lines[i].text)
        if not mm or lines[i].indent > tol:
            continue
        seen += 1
        if int(mm.group("lbl")) == last + 1:
            out.append(i)
            last += 1
    return out if len(out) >= 2 and len(out) - 1 >= 0.6 * seen else []


def _label_re(bare: bool):
    return BARE_NUM_RE if bare else NUM_LABEL_RE


def _join(texts: list[str]) -> str:
    out = ""
    for t in texts:
        t = t.strip()
        if not t:
            continue
        if out.endswith("-") and t[:1].islower() and not out.endswith(" -"):
            out = out[:-1] + t
        else:
            out = (out + " " + t).strip()
    return out


def _chunks(entry: list[BibLine]) -> list[list[BibLine]]:
    """Runs of the entry's lines on one page and in one column."""
    out: list[list[BibLine]] = []
    for l in entry:
        if out and out[-1][-1].page == l.page and out[-1][-1].col == l.col:
            out[-1].append(l)
        else:
            out.append([l])
    return out


def _shrunk(box, size: float):
    s = min(1.5, 0.12 * (size or 10.0))
    return (box[0], box[1] + s, box[2], box[3] - s) if box[3] - box[1] > 3 * s else tuple(box)


def _label_split(bl: BibLine, bare: bool = False):
    """(label text, label box, rest-of-line box) in display space, or None."""
    rx = _label_re(bare)
    m = rx.match(bl.text)
    if not m:
        return None
    try:
        from .xref import _span_box, index_line
        il = index_line(bl.line)
        mm = rx.match(il.text)
        if not mm:
            return None
        a, b = mm.span("lbl")
        rest_a = b
        while rest_a < len(il.text) and il.text[rest_a] == " ":
            rest_a += 1
        lb, rb = _span_box(il, a, b), _span_box(il, rest_a, len(il.text))
        if lb is None or rb is None:
            return None
        return mm.group("lbl"), lb, rb
    except Exception:
        return None


def _union(boxes):
    boxes = [b for b in boxes if b is not None]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _zones(entry: list[BibLine], to_pdf, skip_label_box=None):
    """bbox page, bbox, parts — one zone per run of lines (PDF space)."""
    zones = []
    for ch in _chunks(entry):
        boxes = []
        for k, l in enumerate(ch):
            box = tuple(l.line.bbox)
            if skip_label_box is not None and l is entry[0] and k == 0:
                box = skip_label_box
            boxes.append(_shrunk(box, getattr(l.line, "size", 10.0)))
        zones.append((ch[0].page, to_pdf(ch[0].page, _union(boxes))))
    first_page, first = zones[0]
    parts = [[p, *b] for p, b in zones[1:]]
    return first_page, first, parts


def _pdf_union(items):
    by: dict[int, list] = collections.defaultdict(list)
    for p, b in items:
        by[p].append(b)
    return {p: _union(bs) for p, bs in by.items()}


def build_entries(model: DocModel, lines: list[BibLine], starts: list[int], to_pdf,
                  mode: str = "list", method: str = "") -> list[StructNode]:
    """The new top-level node(s) for one run of entries (not yet placed)."""
    starts = sorted(set(s for s in starts if 0 <= s < len(lines)) | {0})
    entries = [lines[a:b] for a, b in zip(starts, starts[1:] + [len(lines)])]
    numbered = sum(1 for e in entries if NUM_LABEL_RE.match(e[0].text)) >= max(2, len(entries) // 2)
    bare = False
    if not numbered and method == "numbers" and house():
        bare = numbered = sum(1 for e in entries if BARE_NUM_RE.match(e[0].text)) >= max(2, len(entries) // 2)
    rx = _label_re(bare)
    made: list[StructNode] = []
    L = None
    if mode in ("list", "plain"):
        L = StructNode(id=model.new_id("bib"), role="L", page=entries[0][0].page,
                       list_numbering="Decimal" if numbered else "None", source="auto",
                       note=f"reference list — {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}"
                            + (f", split by {method}" if method else ""))
        L.style = {"bibliography": True}
        model.nodes[L.id] = L
        made.append(L)
    every: list = []
    for e in entries:
        split = _label_split(e[0], bare) if numbered else None
        page, bbox, parts = _zones(e, to_pdf, split[2] if split else None)
        text = _join([l.text for l in e])
        lbl_text = ""
        if split:
            lbl_text = split[0]
            text = _join([e[0].text[len(rx.match(e[0].text).group(0)):]] + [l.text for l in e[1:]])
        be = StructNode(id=model.new_id("bibe"), role="BibEntry", page=page, bbox=bbox,
                        parts=[list(p) for p in parts], text=text, source="auto", priority=1,
                        note="reference list entry")
        every.append((page, bbox))
        every.extend((int(p[0]), tuple(p[1:5])) for p in parts)
        if L is None:
            if split:          # one tag per entry: the number stays in it
                lb_pdf = to_pdf(e[0].page, split[1])
                be.bbox = _union([be.bbox, lb_pdf]) if e[0].page == page else be.bbox
                be.text = f"{lbl_text} {text}".strip()
            model.nodes[be.id] = be
            made.append(be)
            continue
        li = StructNode(id=model.new_id("bibi"), role="LI", page=page, bbox=bbox,
                        parts=[list(p) for p in parts], source="auto")
        model.add(li, L.id)
        if split:
            lp = e[0].page
            model.add(StructNode(id=model.new_id("bibl"), role="Lbl", page=lp,
                                 bbox=to_pdf(lp, split[1]), text=lbl_text, source="auto",
                                 priority=1), li.id)
        body = StructNode(id=model.new_id("bibb"), role="LBody", page=page, bbox=bbox,
                          parts=[list(p) for p in parts], source="auto", priority=0)
        model.add(body, li.id)
        if mode == "plain":
            # L > LI > Lbl + LBody: the entry's text straight in the LBody
            body.text, body.priority, body.note = text, 1, "reference list entry"
            model.nodes.pop(be.id, None)
            continue
        model.add(be, body.id)
    if L is not None:
        boxes = _pdf_union(every)
        L.bbox = boxes.get(L.page)
        L.parts = [[p, *b] for p, b in sorted(boxes.items()) if p != L.page]
    return made


# ----------------------------------------------------------------- apply
RESCUE_ROLES = ("Link", "Annot", "Form")


def _home_for(model: DocModel, made: list[StructNode], node: StructNode) -> StructNode | None:
    """The new entry whose zone holds this link."""
    if node.bbox is None:
        return None
    cx, cy = (node.bbox[0] + node.bbox[2]) / 2, (node.bbox[1] + node.bbox[3]) / 2
    pool = []
    for m in made:
        pool.extend([m] + model.descendants(m.id))
    for be in pool:
        if be.role != "BibEntry" and not (be.role == "LBody" and (be.text or "").strip()):
            continue                      # (plain lists: the entry's LBody)
        for r in be.rects_on(node.page):
            if r[0] - 2 <= cx <= r[2] + 2 and r[1] - 2 <= cy <= r[3] + 2:
                return be
    return None


def _dez_offline() -> bool:
    """Ollama is off — has Dez AI learned how reference lists are split? (V4.31)"""
    try:
        from .teacher import offline_knows
        return offline_knows()
    except Exception:
        return False


def tag_bibliography(model: DocModel, doc, page_lines: list, opts=None, ai_cfg=None,
                     heads: list[str] | None = None) -> dict:
    """Re-tag every reference list in the document (or the ones under `heads`)."""
    from .analyzer import to_pdf as _to_pdf

    def to_pdf(pno, box):
        return _to_pdf(doc[pno], box)

    mode = str(getattr(opts, "bibliography_as", "list") or "list") if opts is not None else "list"
    use_ai = bool(ai_cfg is not None and (getattr(ai_cfg, "enabled", True) or _dez_offline())
                  and (opts is None or getattr(opts, "ai_bibliography", False)))
    stats = {"sections": 0, "entries": 0, "replaced": 0, "methods": []}
    for sec in reference_sections(model):
        if heads and sec["head"] not in heads:
            continue
        did = False
        for group in sec["groups"]:
            group = [g for g in group if g in model.nodes]
            if not group or _done(model, group):
                continue
            lines = section_lines(model, doc, page_lines, group, to_pdf)
            if not lines:
                continue
            starts, method = entry_starts(lines, ai_cfg, use_ai)
            if not starts:
                continue
            first = model.nodes[group[0]]
            parent = first.parent
            sibs = model.siblings(first.id)
            at = sibs.index(first.id)
            # links that are real annotations go into the entry they sit in
            rescued = []
            for sid in group:
                for d in [model.nodes[sid]] + model.descendants(sid):
                    if d.role in RESCUE_ROLES and d.annots:
                        rescued.append(d)
            for d in rescued:
                try:
                    model.detach(d.id)
                except (ValueError, KeyError):
                    pass
            gone = {d.id for d in rescued}
            for sid in group:
                if sid in model.nodes and sid not in gone:
                    model.remove(sid, promote_children=False)
            for d in rescued:
                model.nodes.pop(d.id, None)
            made = build_entries(model, lines, starts, to_pdf, mode, method)
            sibs = model.root_children if parent is None else model.nodes[parent].children
            for k, nd in enumerate(made):
                sibs.insert(min(at + k, len(sibs)), nd.id)
                nd.parent = parent
            for d in rescued:
                d.children = [c for c in d.children if c in model.nodes]
                home = _home_for(model, made, d)
                if home is not None:
                    model.add(d, home.id)
                else:
                    model.add(d, parent, min(at + len(made), len(sibs)))
            n_entries = sum(1 for nd in made for x in [nd] + model.descendants(nd.id)
                            if x.role == "BibEntry")
            stats["entries"] += n_entries
            stats["replaced"] += len(group)
            stats["methods"].append(method)
            did = True
        if did:
            stats["sections"] += 1
    return stats


def fix_bibliography(model: DocModel, working_pdf: str, opts=None, ai_cfg=None,
                     heads: list[str] | None = None) -> dict:
    """Editor: tag the reference lists again from the page."""
    import pymupdf

    from .analyzer import collect_lines
    doc = pymupdf.open(working_pdf)
    try:
        for p in doc:
            if p.rotation:
                p.set_rotation(0)
        page_lines = [[] for _ in range(doc.page_count)]
        need = set()
        for sec in reference_sections(model):
            for g in sec["groups"]:
                for sid in g:
                    need |= set(_rects_by_page(model, sid))
        for pno in need:
            if 0 <= pno < doc.page_count:
                page_lines[pno] = collect_lines(doc[pno])
        return tag_bibliography(model, doc, page_lines, opts, ai_cfg, heads)
    finally:
        doc.close()
