"""The back of a book: endnotes and the index (V4.8).

Endnotes — a "Notes" section whose entries start with the note's number — are
Note tags, so the marks in the text can jump to them and they can jump back
(core/xref.py does the linking).

The index becomes Index > L > LI > LBody, one item per entry, a sub-entry as a
list inside its entry's LBody, and a line that only wraps ("…, 45," / "67, 89")
kept with the entry it belongs to. Letter dividers ("A", "B") stay as they are,
each one opening its own list. Every page number in an entry is then a link to
that printed page (link_index).
"""
from __future__ import annotations

import re

from .biblio import (BibLine, _chunks, _join, _rects_by_page, _shrunk, _union,
                     section_lines, subtree_text)
from .model import DocModel, StructNode

NOTES_HEAD_RE = re.compile(r"^\s*(?:chapter\s+\w+\s+)?(?:end\s*notes|notes|chapter\s+notes|"
                           r"notes\s+(?:and|&)\s+references|footnotes)\s*:?\s*$", re.I)
INDEX_HEAD_RE = re.compile(r"^\s*(?:general\s+|subject\s+|author\s+|name\s+|topic\s+)?index"
                           r"(?:\s+of\s+[\w ]{2,30})?\s*:?\s*$", re.I)
LETTER_RE = re.compile(r"^\s*[A-Z](?:\s*[–—-]\s*[A-Z])?\s*$")
MARK_RE = re.compile(r"^\s*(\d{1,3}|[*†‡§¶])(?=[\s.):]|[A-Z(“\"'])")
PAGES_TAIL_RE = re.compile(r"(\d{1,4}|[ivxlc]{1,6})\s*[,;–—-]?\s*$", re.I)
STOP_ROLES = {"Table", "Figure", "Formula", "TOC", "Form", "Part", "Art"}


def _heading(n: StructNode) -> bool:
    return n.is_heading or n.role == "H"


def _level(n: StructNode) -> int:
    return int(n.role[1]) if n.is_heading else (1 if n.role == "H" else 9)


def _run_after(model: DocModel, head: StructNode, keep_letters: bool = False) -> list[str]:
    """The blocks after a heading, up to the next heading of its level or higher."""
    sibs = model.siblings(head.id)
    out = []
    for sid in sibs[sibs.index(head.id) + 1:]:
        s = model.nodes.get(sid)
        if s is None or s.artifact or (s.style or {}).get("page_number"):
            continue
        if _heading(s):
            if keep_letters and LETTER_RE.match(s.text or ""):
                out.append(sid)
                continue
            if _level(s) <= _level(head) or not keep_letters:
                break
            out.append(sid)
            continue
        if s.role in STOP_ROLES:
            break
        out.append(sid)
    return out


# ---------------------------------------------------------------- endnotes
def tag_endnotes(model: DocModel) -> int:
    """Entries of a Notes section that start with their number become Note tags."""
    changed = 0
    for n in list(model.ordered_nodes()):
        if n.artifact or not _heading(n) or not NOTES_HEAD_RE.match(n.text or ""):
            continue
        for sid in _run_after(model, n):
            s = model.nodes[sid]
            if s.role == "P" and MARK_RE.match(s.text or "") and not s.children:
                s.role = "Note"
                s.note = (s.note + "; " if s.note else "") + "endnote (under the Notes heading)"
                changed += 1
            elif s.role == "L":
                # a numbered Notes list: each item is a note
                for li in [model.nodes[c] for c in s.children if c in model.nodes]:
                    if li.role == "LI":
                        li.style = dict(li.style or {}, endnote=True)
    return changed


# ------------------------------------------------------------------- index
def index_sections(model: DocModel) -> list[dict]:
    out = []
    for n in model.ordered_nodes():
        if n.artifact or not _heading(n) or len((n.text or "").strip()) > 40 \
                or not INDEX_HEAD_RE.match(n.text or ""):
            continue
        members = _run_after(model, n, keep_letters=True)
        if not members:
            continue
        if any(model.nodes[m].role == "Index" for m in members):
            continue                                  # already tagged
        out.append({"head": n.id, "members": members})
    return out


def index_problems(model: DocModel) -> list[str]:
    """Index headings whose entries are not in an Index structure yet."""
    return [s["head"] for s in index_sections(model)
            if sum(1 for m in s["members"] if model.nodes[m].role in ("P", "L", "LBody")) >= 2]


def _levels(lines: list[BibLine]) -> list[int]:
    """Indent level of each line: 0 at the column's margin, then each step in."""
    marks: list[float] = []
    for x in sorted(l.indent for l in lines):
        if not marks or x - marks[-1] > max(2.0, 0.25 * (getattr(lines[0].line, "size", 10.0) or 10.0)):
            marks.append(x)
    out = []
    for l in lines:
        out.append(min(range(len(marks)), key=lambda k: abs(marks[k] - l.indent)))
    return out


def plan_entries(lines: list[BibLine]) -> list[dict]:
    """[{kind: letter|entry, level, lines}] in reading order."""
    levels = _levels(lines)
    out: list[dict] = []
    for l, lv in zip(lines, levels):
        t = l.text.strip()
        if LETTER_RE.match(t):
            out.append({"kind": "letter", "level": 0, "lines": [l]})
            continue
        prev = next((e for e in reversed(out) if e["kind"] == "entry"), None)
        last = out[-1] if out else None
        cont = False
        if last is not None and last["kind"] == "entry":
            ptxt = last["lines"][-1].text.rstrip()
            # a line that only wraps: page numbers, or the text after a comma /
            # dash, set further in than the entry it continues
            if (t[:1].isdigit() or re.match(r"^[ivxlc]+[,\s]", t)) and lv >= last["level"]:
                cont = True
            elif lv > last["level"] + 1:
                cont = True
            elif lv > last["level"] and ptxt.endswith(("–", "—", "-", " and", " see")):
                cont = True
        if cont:
            last["lines"].append(l)
            continue
        level = lv if prev is None else min(lv, prev["level"] + 1)
        out.append({"kind": "entry", "level": level, "lines": [l]})
    return out


def _zone(entry_lines: list[BibLine], to_pdf):
    zones = []
    for ch in _chunks(entry_lines):
        box = _union([_shrunk(tuple(l.line.bbox), getattr(l.line, "size", 10.0)) for l in ch])
        zones.append((ch[0].page, to_pdf(ch[0].page, box)))
    return zones[0][0], zones[0][1], [[p, *b] for p, b in zones[1:]]


def build_index(model: DocModel, plan: list[dict], to_pdf, letter_roles: dict | None = None) -> StructNode:
    idx = StructNode(id=model.new_id("idx"), role="Index", page=plan[0]["lines"][0].page,
                     source="auto", note="index — one item per entry, sub-entries nested")
    idx.style = {"index": True}
    model.nodes[idx.id] = idx
    stack: list[tuple[int, StructNode]] = []         # (level, L)
    boxes = []

    def new_list(parent: StructNode, page: int) -> StructNode:
        L = StructNode(id=model.new_id("idxl"), role="L", page=page, list_numbering="None",
                       source="auto")
        model.add(L, parent.id)
        return L

    for e in plan:
        if e["kind"] == "letter":
            page, bbox, parts = _zone(e["lines"], to_pdf)
            role = (letter_roles or {}).get(e["lines"][0].text.strip(), "P")
            p = StructNode(id=model.new_id("idxh"), role=role, page=page, bbox=bbox, parts=parts,
                           text=e["lines"][0].text.strip(), source="auto", priority=1,
                           note="index letter")
            model.add(p, idx.id)
            stack = []
            continue
        page, bbox, parts = _zone(e["lines"], to_pdf)
        boxes.append((page, bbox))
        lv = e["level"]
        while stack and stack[-1][0] > lv:
            stack.pop()
        if not stack or stack[-1][0] < lv:
            if lv == 0 or not stack:
                L = new_list(idx, page)
                stack = [(0, L)]
                lv = 0
            else:
                # a sub-entry: a list inside the LBody of the item above
                parent_li = model.nodes[stack[-1][1].children[-1]] if stack[-1][1].children else None
                body = None
                if parent_li is not None:
                    body = next((model.nodes[c] for c in parent_li.children
                                 if model.nodes[c].role == "LBody"), None)
                if body is None:
                    L = stack[-1][1]
                    lv = stack[-1][0]
                else:
                    L = new_list(body, page)
                    stack.append((lv, L))
        L = stack[-1][1]
        li = StructNode(id=model.new_id("idxi"), role="LI", page=page, bbox=bbox,
                        parts=[list(x) for x in parts], source="auto")
        model.add(li, L.id)
        model.add(StructNode(id=model.new_id("idxb"), role="LBody", page=page, bbox=bbox,
                             parts=[list(x) for x in parts], text=_join([l.text for l in e["lines"]]),
                             source="auto", priority=1), li.id)
    # containers show where they are in the editor
    for n in [idx] + model.descendants(idx.id):
        if n.role in ("Index", "L") and n.bbox is None:
            kids = [d for d in model.descendants(n.id) if d.bbox is not None and d.page == n.page]
            if kids:
                n.bbox = _union([d.bbox for d in kids])
    return idx


def tag_index(model: DocModel, doc, page_lines: list, opts=None) -> dict:
    from .analyzer import to_pdf as _to_pdf

    def to_pdf(pno, box):
        return _to_pdf(doc[pno], box)

    stats = {"sections": 0, "entries": 0, "sub_entries": 0}
    for sec in index_sections(model):
        members = [m for m in sec["members"] if m in model.nodes]
        if not members or not any(subtree_text(model, m) for m in members):
            continue
        lines = section_lines(model, doc, page_lines, members, to_pdf)
        if len(lines) < 2:
            continue
        plan = plan_entries(lines)
        if not any(e["kind"] == "entry" for e in plan):
            continue
        # a letter divider tagged as a heading stays a heading (H jumps letter to letter)
        letters = {(model.nodes[m].text or "").strip(): model.nodes[m].role for m in members
                   if _heading(model.nodes[m]) and LETTER_RE.match(model.nodes[m].text or "")}
        first = model.nodes[members[0]]
        parent = first.parent
        sibs = model.siblings(first.id)
        at = sibs.index(first.id)
        rescued = [d for m in members for d in [model.nodes[m]] + model.descendants(m)
                   if d.role in ("Link", "Annot") and d.annots]
        for d in rescued:
            try:
                model.detach(d.id)
            except (ValueError, KeyError):
                pass
        gone = {d.id for d in rescued}
        for m in members:
            if m in model.nodes and m not in gone:
                model.remove(m, promote_children=False)
        idx = build_index(model, plan, to_pdf, letters)
        sibs = model.root_children if parent is None else model.nodes[parent].children
        sibs.insert(min(at, len(sibs)), idx.id)
        idx.parent = parent
        bodies = [d for d in model.descendants(idx.id) if d.role == "LBody"]
        for d in rescued:
            d.children = [c for c in d.children if c in model.nodes]
            home = None
            if d.bbox is not None:
                cx, cy = (d.bbox[0] + d.bbox[2]) / 2, (d.bbox[1] + d.bbox[3]) / 2
                home = next((b for b in bodies for r in b.rects_on(d.page)
                             if r[0] - 2 <= cx <= r[2] + 2 and r[1] - 2 <= cy <= r[3] + 2), None)
            model.add(d, home.id if home else idx.id)
        stats["sections"] += 1
        stats["entries"] += sum(1 for e in plan if e["kind"] == "entry")
        stats["sub_entries"] += sum(1 for e in plan if e["kind"] == "entry" and e["level"] > 0)
    return stats


def fix_backmatter(model: DocModel, working_pdf: str, opts=None) -> dict:
    """Editor: endnotes as Note, the index as Index > L > LI."""
    import pymupdf

    from .analyzer import collect_lines
    out = {"endnotes": tag_endnotes(model)}
    doc = pymupdf.open(working_pdf)
    try:
        for p in doc:
            if p.rotation:
                p.set_rotation(0)
        page_lines = [[] for _ in range(doc.page_count)]
        need = set()
        for sec in index_sections(model):
            for m in sec["members"]:
                need |= set(_rects_by_page(model, m))
        for pno in need:
            if 0 <= pno < doc.page_count:
                page_lines[pno] = collect_lines(doc[pno])
        out["index"] = tag_index(model, doc, page_lines, opts)
    finally:
        doc.close()
    return out
