"""PDF/UA-1 (ISO 14289-1) machine checks, plus optional veraPDF.

Only conditions software can decide are reported pass/fail. Things that
need human judgement are listed as manual checkpoints.
"""
from __future__ import annotations

import collections
import json
import os
import re
import shutil
import subprocess
from typing import Any

import pikepdf
from pikepdf import Array, Dictionary, Name

from . import remediate as _reg  # noqa: F401  (registers the pdfuaid namespace)
from .compat import house
from .model import HEADINGS, STANDARD_ROLES, Finding
from .structure import _is_block_context
from .pdftext import (INLINE_IMAGE_OPS, SHOW_OPS, Interpreter, base_name, font_encoding,
                      glyph_unicode, num, page_box, page_resources)
from .preflight import iter_font_dicts

LANG_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*$")
SKIP_ANNOTS = {"/Popup", "/PrinterMark"}


class Collector:
    def __init__(self) -> None:
        self.items: dict[str, Finding] = {}

    def fail(self, code, title, clause, detail="", page=None, severity="error", fixable="manual", n=1):
        f = self.items.get(code)
        if f is None or f.severity == "pass":
            f = Finding(code, title, severity, clause, detail, [], 0, fixable)
            self.items[code] = f
        f.count += n
        if page is not None and page not in f.pages:
            f.pages.append(page)
        if detail and not f.detail:
            f.detail = detail

    def ok(self, code, title, clause, detail=""):
        if code not in self.items:
            self.items[code] = Finding(code, title, "pass", clause, detail)

    def info(self, code, title, clause, detail="", fixable="manual"):
        if code not in self.items:
            self.items[code] = Finding(code, title, "info", clause, detail, fixable=fixable)

    def list(self) -> list[Finding]:
        order = {"error": 0, "warning": 1, "info": 2, "pass": 3}
        out = list(self.items.values())
        for f in out:
            f.pages.sort()
        return sorted(out, key=lambda f: (order.get(f.severity, 9), f.code))


def _s(v) -> str:
    try:
        return str(v) if v is not None else ""
    except Exception:
        return ""


def _role(elem: Dictionary, rolemap) -> tuple[str, str]:
    s = _s(elem.get("/S")).lstrip("/")
    cur = s
    for _ in range(12):
        if cur in STANDARD_ROLES:
            return cur, s
        if isinstance(rolemap, Dictionary) and ("/" + cur) in rolemap:
            cur = _s(rolemap["/" + cur]).lstrip("/")
        else:
            return "", s
    return "", s


def _kids(e: Dictionary) -> list:
    k = e.get("/K")
    if isinstance(k, Array):
        return list(k)
    return [k] if k is not None else []


# =================================================================== check
def check(path: str) -> list[Finding]:
    c = Collector()
    pdf = pikepdf.open(path)
    root = pdf.Root
    npages = len(pdf.pages)
    page_ids = {p.objgen: i for i, p in enumerate(pdf.pages)}

    # ---------------------------------------------------------- catalog
    mi = root.get("/MarkInfo")
    marked = isinstance(mi, Dictionary) and mi.get("/Marked") is True
    tree = root.get("/StructTreeRoot")
    if marked and isinstance(tree, Dictionary):
        c.ok("UA-01", "Document is tagged", "7.1", "MarkInfo/Marked true and a structure tree exists")
    else:
        c.fail("UA-01", "Document is not tagged", "7.1",
               ("no StructTreeRoot. " if not isinstance(tree, Dictionary) else "") +
               ("MarkInfo/Marked is not true." if not marked else ""), fixable="auto")
    if isinstance(mi, Dictionary) and mi.get("/Suspects") is True:
        c.fail("UA-02", "MarkInfo/Suspects is true", "7.1", "Tag reliability is flagged as suspect", fixable="auto")

    lang = _s(root.get("/Lang"))
    if lang and LANG_RE.match(lang):
        c.ok("UA-03", "Document language is set", "7.2", f"/Lang = {lang}")
    else:
        c.fail("UA-03", "Document language missing or invalid", "7.2",
               f"/Lang is {lang!r}", fixable="auto")

    vp = root.get("/ViewerPreferences")
    if isinstance(vp, Dictionary) and vp.get("/DisplayDocTitle") is True:
        c.ok("UA-04", "Window shows the document title", "7.1")
    else:
        c.fail("UA-04", "DisplayDocTitle is not true", "7.1", fixable="auto")

    title = ua = ""
    has_xmp = "/Metadata" in root
    try:
        with pdf.open_metadata() as meta:
            title = _s(meta.get("dc:title"))
            ua = _s(meta.get("pdfuaid:part"))
    except Exception:
        pass
    if not has_xmp:
        c.fail("UA-05", "No XMP metadata stream", "7.1 / 5", fixable="auto")
    else:
        if ua == "1":
            c.ok("UA-05", "PDF/UA identifier present", "5", "pdfuaid:part = 1")
        else:
            c.fail("UA-05", "PDF/UA identifier missing", "5", f"pdfuaid:part = {ua!r}", fixable="auto")
        if title.strip():
            c.ok("UA-06", "Document title in XMP metadata", "7.1", title[:80])
        else:
            c.fail("UA-06", "No dc:title in XMP metadata", "7.1", fixable="auto")

    if pdf.is_encrypted:
        try:
            if not pdf.allow.accessibility:
                c.fail("UA-07", "Encryption blocks assistive technology", "7.16", fixable="auto")
        except Exception:
            pass

    # ------------------------------------------------------ structure tree
    elements: list[tuple[Dictionary, str, str, str]] = []   # elem, role, orig, parent role
    rolemap = tree.get("/RoleMap") if isinstance(tree, Dictionary) else None
    if isinstance(rolemap, Dictionary):
        for k, v in rolemap.items():
            if k.lstrip("/") in STANDARD_ROLES:
                c.fail("UA-10", "A standard structure type is role-mapped", "7.1",
                       f"{k} -> {v}", fixable="manual")
    claimed: dict[tuple[int, int], Dictionary] = {}
    objr_annots: dict[tuple, Dictionary] = {}
    if isinstance(tree, Dictionary):
        stack = [(k, "StructTreeRoot") for k in _kids(tree)]
        seen = set()
        while stack:
            e, prole = stack.pop()
            if not isinstance(e, Dictionary) or "/S" not in e:
                continue
            try:
                if e.objgen in seen:
                    continue
                seen.add(e.objgen)
            except Exception:
                pass
            std, orig = _role(e, rolemap)
            elements.append((e, std, orig, prole))
            if not std:
                c.fail("UA-11", "Structure type is not standard and not role-mapped", "7.1",
                       f"/{orig}", fixable="review")
            pg = e.get("/Pg")
            for k in _kids(e):
                if isinstance(k, Dictionary) and "/S" in k:
                    stack.append((k, std or orig))
                elif isinstance(k, Dictionary) and k.get("/Type") == Name.MCR:
                    kp = k.get("/Pg") or pg
                    if kp is not None and "/Stm" not in k:
                        claimed[(page_ids.get(kp.objgen, -1), int(num(k.get("/MCID"), -1)))] = e
                elif isinstance(k, Dictionary) and k.get("/Type") == Name.OBJR:
                    obj = k.get("/Obj")
                    try:
                        objr_annots[obj.objgen] = e
                    except Exception:
                        pass
                elif not isinstance(k, Dictionary) and k is not None:
                    if pg is None:
                        c.fail("UA-12", "Element holds MCIDs but has no /Pg", "7.1",
                               f"{std or orig}", fixable="auto")
                    else:
                        try:
                            claimed[(page_ids.get(pg.objgen, -1), int(k))] = e
                        except Exception:
                            pass

    roles = [r for _, r, _, _ in elements]
    by_role = collections.Counter(roles)

    # figures / formulas
    for role, code, clause in (("Figure", "UA-20", "7.3"), ("Formula", "UA-21", "7.7")):
        els = [e for e, r, _, _ in elements if r == role]
        missing = [e for e in els if not _s(e.get("/Alt")).strip() and not _s(e.get("/ActualText")).strip()]
        pages = sorted({page_ids.get(e.Pg.objgen, 0) for e in missing if e.get("/Pg") is not None})
        if missing:
            c.fail(code, f"{role} without alternative text", clause,
                   f"{len(missing)} of {len(els)} {role} elements lack /Alt", fixable="review")
            c.items[code].count = len(missing)
            c.items[code].pages = pages
        elif els:
            c.ok(code, f"Every {role} has alternative text", clause, f"{len(els)} checked")
    if not by_role.get("Figure"):
        c.info("UA-20", "No Figure elements", "7.3",
               "Either the document has no meaningful images, or they are not tagged")

    # headings
    hs = [(e, r) for e, r, _, _ in elements if r in HEADINGS or r == "H"]
    if any(r == "H" for _, r in hs) and any(r in HEADINGS for _, r in hs):
        c.fail("UA-22", "Both H and H1–H6 headings are used", "7.4.4", severity="warning", fixable="review")
    num_hs = [int(r[1]) for _, r in _doc_order(tree, elements, rolemap) if r in HEADINGS] if hs else []
    if num_hs:
        probs = []
        if num_hs[0] != 1:
            probs.append(f"first heading is H{num_hs[0]}")
        for a, b in zip(num_hs, num_hs[1:]):
            if b > a + 1:
                probs.append(f"H{a} → H{b}")
        if probs:
            c.fail("UA-23", "Heading levels are skipped", "7.4.2",
                   "; ".join(dict.fromkeys(probs))[:300], fixable="review", n=len(probs) - 1)
        else:
            c.ok("UA-23", "Heading hierarchy is well formed", "7.4.2", f"{len(num_hs)} headings")
    elif npages >= 3:
        c.fail("UA-23", "No headings", "7.4 / WCAG 2.4.6", "Documents of any length need headings",
               severity="warning", fixable="review")

    # tables
    tables = [e for e, r, _, _ in elements if r == "Table"]
    for t in tables:
        _check_table(c, t, rolemap, page_ids)
    if tables and "UA-30" not in c.items and "UA-31" not in c.items:
        c.ok("UA-30", "Tables have header cells and regular structure", "7.5", f"{len(tables)} tables")

    # lists
    for e, r, _, prole in elements:
        if r == "L":
            kids = [k for k in _kids(e) if isinstance(k, Dictionary) and "/S" in k]
            bad = [_role(k, rolemap)[0] for k in kids if _role(k, rolemap)[0] not in ("LI", "Caption", "L")]
            if bad:
                c.fail("UA-40", "List contains something other than list items", "7.6",
                       f"L > {', '.join(sorted(set(bad)))}", fixable="review")
            if any(_role(g, rolemap)[0] == "Lbl" for k in kids for g in _kids(k)
                   if isinstance(g, Dictionary) and "/S" in g):
                if not _attr(e, "/ListNumbering"):
                    c.fail("UA-41", "List with labels has no ListNumbering attribute", "7.6",
                           severity="warning", fixable="auto")
        elif r == "LI":
            kids = [k for k in _kids(e) if isinstance(k, Dictionary) and "/S" in k]
            bad = [_role(k, rolemap)[0] for k in kids if _role(k, rolemap)[0] not in ("Lbl", "LBody")]
            if bad:
                c.fail("UA-42", "List item contains something other than Lbl/LBody", "7.6",
                       f"LI > {', '.join(sorted(set(bad)))}", fixable="review")
        elif r in ("Lbl", "LBody") and prole != "LI" and not (r == "Lbl" and prole == "Note" and house()):
            # a note's own mark in a Lbl (Note > Lbl, PDF 2.0) is not a list label
            c.fail("UA-43", "Lbl/LBody outside a list item", "7.6", fixable="review")
        elif r == "TOCI" and prole not in ("TOC",):
            c.fail("UA-44", "TOCI outside a TOC", "ISO 32000-1 14.8.4.3", severity="warning", fixable="review")

    # notes
    ids = collections.Counter()
    for e, r, _, _ in elements:
        if "/ID" in e:
            ids[_s(e.ID)] += 1
        if r == "Note" and not _s(e.get("/ID")):
            c.fail("UA-45", "Note without an ID", "7.9", fixable="auto")
    dup = [i for i, n in ids.items() if n > 1]
    if dup:
        c.fail("UA-46", "Duplicate structure element IDs", "7.9", ", ".join(dup[:5]), fixable="auto")

    # Kriyadocs only (compat.house()): Note / Figure / Formula are
    # inline-level types (ISO 32000-1 Table 333). Standing on their own
    # (not nested in running text) they must declare Placement: Block, or
    # PAC reports "Possibly inappropriate use of a Note/Figure structure
    # element" (axes4 7371524021266 / 7371484607634). The V4/Academain
    # engine's own Note/Figure output is untouched by this — see structure.py.
    if house():
        for e, r, _, prole in elements:
            if r not in ("Note", "Figure", "Formula"):
                continue
            placement = _s(_attr(e, "/Placement"))
            want_block = _is_block_context(prole)
            if want_block and placement != "/Block":
                c.fail("UA-49", "Note/Figure/Formula used as a block needs Placement: Block",
                       "7.1", f"{r} under {prole or 'root'}", fixable="auto")
            elif not want_block and placement == "/Block":
                c.fail("UA-49", "Note/Figure/Formula inline in running text should not say Placement: Block",
                       "7.1", f"{r} under {prole}", fixable="auto")

    # elements not associated with content (PAC "Associated with content"):
    # no MCID / MCR / OBJR anywhere below them
    memo: dict = {}

    def has_content(e, depth=0) -> bool:
        key = e.objgen if e.is_indirect else id(e)
        if key in memo:
            return memo[key]
        memo[key] = False
        ok = False
        if depth < 200:
            for k in _kids(e):
                if isinstance(k, int):
                    ok = True
                elif isinstance(k, Dictionary):
                    t = k.get("/Type")
                    if t in (Name.MCR, Name.OBJR) or ("/MCID" in k and "/S" not in k):
                        ok = True
                    elif "/S" in k and has_content(k, depth + 1):
                        ok = True
                if ok:
                    break
        memo[key] = ok
        return ok

    empty = [r for e, r, _, _ in elements if not has_content(e)]
    empty_th = sum(1 for r in empty if r == "TH")
    # an empty data cell left silent on purpose (Text for empty table cells
    # left empty, as some publishers' files are) is reported, not failed
    silent_td = sum(1 for r in empty if r in ("TD", "TH")) if house() else 0
    if silent_td and all(r in ("TD", "TH") for r in empty):
        # a house style keeps its empty cells silent, the header-row ones as TH
        c.fail("UA-47", "Empty table cells with no content", "ISO 14289-1 7.1 / PAC",
               f"TD/TH×{silent_td} — left empty (no text, no description). PAC may list them as "
               "not associated with content; 'Text for empty table cells' gives each one a word",
               severity="warning", fixable="auto", n=silent_td - 1)
        empty = []
        empty_th = 0
    if empty_th:
        c.fail("UA-36", "Empty header cell (TH) — an empty header must be a TD", "7.5",
               f"{empty_th} empty TH cell(s); rebuild with the tool to change them to TD",
               fixable="auto", n=empty_th - 1)
    if empty:
        cells = sum(1 for r in empty if r in ("TD", "TH"))
        hint = (f" — {cells} empty table cells: build with 'Empty-cell method' = text"
                if cells else "")
        c.fail("UA-47", "Structure elements not associated with content", "ISO 14289-1 7.1 / PAC",
               ", ".join(f"{k}×{v}" for k, v in collections.Counter(empty).most_common(6)) + hint,
               fixable="auto", n=len(empty) - 1)
    elif elements:
        c.ok("UA-47", "Every structure element is associated with content", "7.1",
             f"{len(elements)} elements")

    # /ActualText belongs on a Span inside the tag, not on the tag itself
    # (every client, V4.16 — see core/structure.py). A Figure or Formula is
    # described by it and keeps it; a tag with nothing in it has nothing to
    # wrap and is reported as UA-47 instead.
    on_tag = collections.Counter()
    for e, r, _, _ in elements:
        if r in ("Span", "Figure", "Formula") or not _s(e.get("/ActualText")).strip():
            continue
        if not has_content(e):
            continue
        on_tag[r] += 1
    if on_tag:
        n = sum(on_tag.values())
        c.fail("UA-58", "/ActualText on the tag instead of on a Span inside it",
               "ISO 32000-1 14.9.4 / client house rule",
               ", ".join(f"{k}\u00d7{v}" for k, v in on_tag.most_common(6))
               + " \u2014 the editor's Checks tab has \u201cPut /ActualText on a Span\u201d, "
                 "or build with 'ActualText on a Span' on",
               severity="warning", fixable="auto", n=n - 1)
    elif any(_s(e.get("/ActualText")).strip() for e, _, _, _ in elements):
        c.ok("UA-58", "Every /ActualText sits on a Span inside its tag",
             "ISO 32000-1 14.9.4 / client house rule")

    # links
    for e, r, _, _ in elements:
        if r == "Link" and not any(isinstance(k, Dictionary) and k.get("/Type") == Name.OBJR
                                   for k in _kids(e)):
            c.fail("UA-48", "Link element without a link annotation", "7.18.5", fixable="review")

    # ------------------------------------------------------------ content
    _check_content(c, pdf, claimed, tree)

    # -------------------------------------------------------- annotations
    ptree = {}
    if isinstance(tree, Dictionary):
        pt = tree.get("/ParentTree")
        nums = pt.get("/Nums") if isinstance(pt, Dictionary) else None
        if isinstance(nums, Array):
            for i in range(0, len(nums) - 1, 2):
                ptree[int(num(nums[i]))] = nums[i + 1]
    tabs_missing = []
    for pno, page in enumerate(pdf.pages):
        annots = page.obj.get("/Annots")
        if not isinstance(annots, Array) or not len(annots):
            continue
        relevant = False
        for a in annots:
            if not isinstance(a, Dictionary):
                continue
            sub = _s(a.get("/Subtype"))
            if sub == "/TrapNet":
                c.fail("UA-50", "TrapNet annotation present", "7.18.2", page=pno)
                continue
            if sub in SKIP_ANNOTS:
                continue
            try:
                if int(num(a.get("/F"), 0)) & 2:
                    continue
            except Exception:
                pass
            relevant = True
            owner = None
            try:
                owner = objr_annots.get(a.objgen)
            except Exception:
                pass
            if owner is None:
                c.fail("UA-51", "Annotation is not in the structure tree", "7.18.1",
                       f"{sub[1:]} annotations need an OBJR in a Link/Form/Annot element",
                       page=pno, fixable="auto")
            else:
                orole = _role(owner, rolemap)[0]
                want = {"/Link": "Link", "/Widget": "Form"}.get(sub, "Annot")
                if orole != want and not (want == "Annot" and orole in ("Annot",)):
                    c.fail("UA-52", "Annotation nested in the wrong element type", "7.18",
                           f"{sub[1:]} inside {orole}, expected {want}", page=pno, fixable="auto")
                sp = a.get("/StructParent")
                if sp is None or int(num(sp, -1)) not in ptree:
                    c.fail("UA-53", "Annotation StructParent missing from the parent tree", "7.18.1",
                           page=pno, fixable="auto")
            if sub == "/Link":
                if not _s(a.get("/Contents")).strip():
                    c.fail("UA-54", "Link annotation without a description (/Contents)", "7.18.5",
                           page=pno, fixable="auto")
            elif sub == "/Widget":
                field = a if ("/T" in a or "/FT" in a) else a.get("/Parent")
                tu = _s(field.get("/TU")) if isinstance(field, Dictionary) else ""
                if not tu.strip():
                    c.fail("UA-55", "Form field without a tooltip (/TU)", "7.18.4 / WCAG 4.1.2",
                           page=pno, fixable="review")
            else:
                alt = _s(owner.get("/Alt")) if isinstance(owner, Dictionary) else ""
                if not _s(a.get("/Contents")).strip() and not alt.strip():
                    c.fail("UA-56", "Annotation without /Contents or /Alt", "7.18.1",
                           f"{sub[1:]}", page=pno, fixable="review")
        if relevant and page.obj.get("/Tabs") != Name.S:
            tabs_missing.append(pno)
    for p in tabs_missing:
        c.fail("UA-57", "Page with annotations has no /Tabs /S", "7.18.3", page=p, fixable="auto")
    if "UA-51" not in c.items and "UA-54" not in c.items and objr_annots:
        c.ok("UA-51", "Annotations are tagged", "7.18", f"{len(objr_annots)} annotations")

    # ------------------------------------------------------------- fonts
    _check_fonts(c, pdf)

    # ------------------------------------------ reading order vs. pages
    try:
        _check_page_order(c, pdf)
    except Exception:
        pass

    # ------------------------------------- backgrounds hidden under text
    try:
        from .normalize import stacked_backgrounds
        stacked = {p: stacked_backgrounds(pdf, p) for p in range(len(pdf.pages))}
        bad = {p: n for p, n in stacked.items() if n}
        if bad:
            pages_ = sorted(bad)
            c.fail("UA-C2", "Text over a hidden background that fails contrast", "WCAG 1.4.3 / PAC",
                   f"{sum(bad.values())} text run(s) on pages {', '.join(str(p + 1) for p in pages_[:12])}"
                   f"{'…' if len(pages_) > 12 else ''} — renders fine, but PAC may measure against the "
                   "hidden panel; Build option 'Clean up hidden backgrounds' removes it",
                   severity="warning", fixable="auto")
    except Exception:
        pass

    # -------------------------------------------------- misc catalog items
    ocp = root.get("/OCProperties")
    if isinstance(ocp, Dictionary):
        cfgs = [ocp.get("/D")] + list(ocp.get("/Configs") or [])
        for cfg in cfgs:
            if isinstance(cfg, Dictionary):
                if not _s(cfg.get("/Name")).strip():
                    c.fail("UA-60", "Optional content configuration without /Name", "7.10", fixable="auto")
                if "/AS" in cfg:
                    c.fail("UA-61", "Optional content configuration has /AS", "7.10", fixable="auto")
    names = root.get("/Names")
    efs = names.get("/EmbeddedFiles") if isinstance(names, Dictionary) else None
    if isinstance(efs, Dictionary):
        arr = efs.get("/Names")
        if isinstance(arr, Array):
            for i in range(1, len(arr), 2):
                spec = arr[i]
                if isinstance(spec, Dictionary) and ("/F" not in spec or "/UF" not in spec):
                    c.fail("UA-62", "Embedded file without /F and /UF", "7.11", fixable="auto")
    for pno, page in enumerate(pdf.pages):
        xo = page_resources(page.obj).get("/XObject")
        if isinstance(xo, Dictionary):
            for _, x in xo.items():
                if isinstance(x, pikepdf.Stream):
                    if "/Ref" in x:
                        c.fail("UA-63", "Reference XObject present", "7.20", page=pno)
                    if x.get("/Subtype") == Name.PS or x.get("/Subtype2") == Name.PS:
                        c.fail("UA-64", "PostScript XObject present", "7.20", page=pno)

    # navigation
    has_outline = "/Outlines" in root and isinstance(root.Outlines, Dictionary) and \
        root.Outlines.get("/First") is not None
    if npages >= 20 and not has_outline:
        c.fail("UA-70", "No bookmarks in a long document", "WCAG 2.4.5",
               f"{npages} pages", severity="warning", fixable="auto")
    elif has_outline:
        c.ok("UA-70", "Bookmarks present", "WCAG 2.4.5")

    pdf.close()

    # scanned pages
    try:
        import pymupdf
        d = pymupdf.open(path)
        scanned = [i for i, p in enumerate(d) if not p.get_text().strip() and p.get_images()]
        d.close()
        for p in scanned:
            c.fail("UA-71", "Page has images but no text", "7.3 / WCAG 1.4.5",
                   "Looks scanned — run OCR", page=p, fixable="auto")
    except Exception:
        pass

    for code, t, clause, d in (
            ("M-01", "Colour contrast is at least 4.5:1", "WCAG 1.4.3", "Check visually"),
            ("M-02", "Alternative text is meaningful and accurate", "WCAG 1.1.1", "Presence is checked, quality is not"),
            ("M-03", "Reading order matches the visual order", "7.2 / WCAG 1.3.2", "Use the reading-order view"),
            ("M-04", "Colour is not the only way information is conveyed", "WCAG 1.4.1", "Charts, legends, required fields"),
            ("M-05", "Tag types match what the content is", "7.1", "e.g. visual headings tagged as headings"),
            ("M-06", "Decorative content is marked as artifact", "7.1", "Borders, ornaments, repeated logos")):
        c.info(code, t, clause, d)
    return c.list()


def _attr(e: Dictionary, key: str):
    a = e.get("/A")
    for d in ([a] if isinstance(a, Dictionary) else (list(a) if isinstance(a, Array) else [])):
        if isinstance(d, Dictionary) and key in d:
            return d[key]
    return None


def _doc_order(tree, elements, rolemap):
    """Elements in logical (depth-first) order."""
    out = []
    if not isinstance(tree, Dictionary):
        return out
    stack = list(reversed(_kids(tree)))
    seen = set()
    while stack:
        e = stack.pop()
        if not isinstance(e, Dictionary) or "/S" not in e:
            continue
        try:
            if e.objgen in seen:
                continue
            seen.add(e.objgen)
        except Exception:
            pass
        out.append((e, _role(e, rolemap)[0]))
        stack.extend(reversed([k for k in _kids(e) if isinstance(k, Dictionary) and "/S" in k]))
    return out


def _check_table(c: Collector, t: Dictionary, rolemap, page_ids) -> None:
    pg = t.get("/Pg")
    pno = page_ids.get(pg.objgen) if pg is not None else None
    rows = []
    for k in _kids(t):
        if not isinstance(k, Dictionary) or "/S" not in k:
            continue
        r = _role(k, rolemap)[0]
        if r == "TR":
            rows.append(k)
        elif r in ("THead", "TBody", "TFoot"):
            for g in _kids(k):
                if isinstance(g, Dictionary) and _role(g, rolemap)[0] == "TR":
                    rows.append(g)
                elif isinstance(g, Dictionary) and "/S" in g:
                    c.fail("UA-32", "Table section contains something other than rows", "7.5",
                           page=pno, fixable="review")
        elif r != "Caption":
            c.fail("UA-32", "Table contains something other than rows", "7.5",
                   f"Table > {r}", page=pno, fixable="review")
    th = 0
    th_noscope = 0
    td_headers = 0
    occupied: dict[tuple[int, int], bool] = {}
    widths = []
    spans_all = False
    for ri, row in enumerate(rows):
        col = 0
        for cell in _kids(row):
            if not isinstance(cell, Dictionary) or "/S" not in cell:
                continue
            r = _role(cell, rolemap)[0]
            if r not in ("TH", "TD"):
                c.fail("UA-33", "Row contains something other than TH/TD", "7.5",
                       f"TR > {r}", page=pno, fixable="review")
                continue
            while occupied.get((ri, col)):
                col += 1
            rs = int(num(_attr(cell, "/RowSpan"), 1) or 1)
            cs = int(num(_attr(cell, "/ColSpan"), 1) or 1)
            if len(rows) >= 3 and rs >= len(rows):
                spans_all = True
            for dr in range(rs):
                for dc in range(cs):
                    occupied[(ri + dr, col + dc)] = True
            col += cs
            if r == "TH":
                th += 1
                if _attr(cell, "/Scope") is None:
                    th_noscope += 1
            elif _attr(cell, "/Headers") is not None:
                td_headers += 1
        # columns in this row = own cells' spans + cells spanning down from above
        widths.append(sum(1 for (rr, cc) in occupied if rr == ri))
    overflow = any(rr >= len(rows) for (rr, cc) in occupied)
    if not th:
        # Not every table has a header: a word grid in a phonics workbook, a
        # layout grid of pictures. Marking a row TH that is not a header
        # announces a heading that is not there, so this is for a person to
        # judge — PAC and Adobe both leave it to the human too.
        c.fail("UA-30", "Table without header cells", "7.5",
               "no TH in this table — right for a plain grid; add header cells if it does have a "
               "header row or column", severity="warning", page=pno, fixable="review")
    if widths and (len(set(widths)) > 1 or overflow):
        c.fail("UA-31", "Irregular table (rows have different numbers of columns)", "7.5 / veraPDF 7.2-43",
               (f"row widths {sorted(set(widths))}" if len(set(widths)) > 1 else "") +
               (" a RowSpan reaches past the last row" if overflow else ""), page=pno, fixable="review")
    if spans_all:
        c.fail("UA-35", "Table structure possibly incomplete (a cell spans every row)", "7.5 / PAC",
               "usually a background panel read as a cell — remove it or re-tag the table",
               severity="warning", page=pno, fixable="review")
    if th_noscope and not td_headers:
        c.fail("UA-34", "Header cells without Scope and data cells without Headers", "7.5",
               severity="warning", page=pno, fixable="auto")


def _check_content(c: Collector, pdf: pikepdf.Pdf, claimed: dict, tree) -> None:
    untagged_pages = collections.Counter()
    nested_art = collections.Counter()
    nested_real = collections.Counter()
    orphan = collections.Counter()
    ptree = {}
    if isinstance(tree, Dictionary):
        pt = tree.get("/ParentTree")
        nums = pt.get("/Nums") if isinstance(pt, Dictionary) else None
        if isinstance(nums, Array):
            for i in range(0, len(nums) - 1, 2):
                ptree[int(num(nums[i]))] = nums[i + 1]
    for pno, page in enumerate(pdf.pages):
        try:
            ins = list(pikepdf.parse_content_stream(page))
        except Exception:
            c.fail("UA-15", "Page content stream cannot be parsed", "7.1", page=pno)
            continue
        infos = Interpreter(page_resources(page.obj), page_box(page.obj)).run(ins)
        stack: list[str] = []
        sp = page.obj.get("/StructParents")
        parr = ptree.get(int(num(sp, -1))) if sp is not None else None
        for i, instr in enumerate(ins):
            op = str(instr.operator)
            if op in ("BDC", "BMC"):
                tag = _s(instr.operands[0]) if instr.operands else ""
                props = instr.operands[1] if op == "BDC" and len(instr.operands) > 1 else None
                if tag == "/Artifact":
                    if "real" in stack:
                        nested_art[pno] += 1
                    stack.append("art")
                elif isinstance(props, Dictionary) and "/MCID" in props:
                    if "art" in stack:
                        nested_real[pno] += 1
                    m = int(num(props.MCID, -1))
                    if (pno, m) not in claimed or parr is None or not isinstance(parr, Array) \
                            or m >= len(parr) or parr[m] is None:
                        orphan[pno] += 1
                    stack.append("real")
                else:
                    stack.append("other")
                continue
            if op == "EMC":
                if stack:
                    stack.pop()
                continue
            info = infos[i]
            if info is None:
                continue
            if info.kind == "path" and info.clip_only:
                continue
            if "art" not in stack and "real" not in stack:
                untagged_pages[pno] += 1
    if untagged_pages:
        c.fail("UA-13", "Content that is neither tagged nor an artifact", "7.1",
               f"{sum(untagged_pages.values())} text/graphics operators", fixable="auto")
        c.items["UA-13"].pages = sorted(untagged_pages)
        c.items["UA-13"].count = sum(untagged_pages.values())
    else:
        c.ok("UA-13", "All content is tagged or marked as artifact", "7.1")
    if nested_art:
        c.fail("UA-14", "Artifact nested inside tagged content", "7.1", fixable="auto",
               n=sum(nested_art.values()) - 1)
        c.items["UA-14"].pages = sorted(nested_art)
    if nested_real:
        c.fail("UA-16", "Tagged content nested inside an artifact", "7.1", fixable="auto",
               n=sum(nested_real.values()) - 1)
        c.items["UA-16"].pages = sorted(nested_real)
    if orphan:
        c.fail("UA-17", "Marked content not connected to the structure tree", "7.1",
               f"{sum(orphan.values())} MCIDs", fixable="auto", n=sum(orphan.values()) - 1)
        c.items["UA-17"].pages = sorted(orphan)


def _check_page_order(c: "Collector", pdf: pikepdf.Pdf) -> None:
    """Top-level blocks should be read page by page. A block from page 2 that
    is read after page-30 content is almost always a tagging mistake."""
    root = pdf.Root.get("/StructTreeRoot")
    if not isinstance(root, Dictionary):
        return
    page_no = {p.objgen: i for i, p in enumerate(pdf.pages)}

    def elems(e):
        return [k for k in _kids(e) if isinstance(k, Dictionary) and "/S" in k]

    def first_page(e, depth=0):
        pg = e.get("/Pg")
        if pg is not None and getattr(pg, "objgen", None) in page_no:
            return page_no[pg.objgen]
        if depth > 40:
            return None
        for k in _kids(e):
            if isinstance(k, Dictionary):
                if "/Pg" in k and k.get("/Type") in (Name.MCR, Name.OBJR):
                    return page_no.get(k.Pg.objgen)
                if "/S" in k:
                    p = first_page(k, depth + 1)
                    if p is not None:
                        return p
        return None

    blocks = elems(root)
    while len(blocks) == 1 and _s(blocks[0].get("/S")).lstrip("/") in ("Document", "Part", "Art", "Sect", "Div"):
        blocks = elems(blocks[0])
    seq = [(b, first_page(b)) for b in blocks]
    seq = [(b, p) for b, p in seq if p is not None]
    if len(seq) < 2:
        return
    import bisect
    tails, tails_idx, prev = [], [], [-1] * len(seq)
    for k, (_, p) in enumerate(seq):
        j = bisect.bisect_right(tails, p)
        if j == len(tails):
            tails.append(p)
            tails_idx.append(k)
        else:
            tails[j], tails_idx[j] = p, k
        prev[k] = tails_idx[j - 1] if j else -1
    keep, k = set(), tails_idx[-1]
    while k >= 0:
        keep.add(k)
        k = prev[k]
    bad = [seq[k][1] for k in range(len(seq)) if k not in keep]
    if bad:
        c.fail("UA-O1", "Reading order jumps between pages", "WCAG 1.3.2 / 2.4.3",
               f"{len(bad)} tag(s) read out of page order (pages {', '.join(str(p + 1) for p in sorted(set(bad))[:10])})"
               " — use Fix reading order in the editor (ignore if the jump is intended, e.g. a continued article)",
               severity="warning", fixable="auto")
    else:
        c.ok("UA-O1", "Reading order follows the pages", "WCAG 1.3.2", f"{len(seq)} blocks")


def _font_usage(pdf: pikepdf.Pdf) -> tuple[set, set]:
    """Font keys used with visible and with invisible (Tr 3) text, across pages,
    form XObjects and annotation appearance streams."""
    visible: set = set()
    invisible: set = set()
    seen: set = set()

    def scan(stream_owner, res, depth=0):
        if depth > 8 or not isinstance(res, Dictionary):
            return
        fonts = res.get("/Font")
        xo = res.get("/XObject")
        try:
            ins = pikepdf.parse_content_stream(stream_owner)
        except Exception:
            return
        tr, cur, stack = 0, None, []
        for i in ins:
            op = str(i.operator)
            if op == "q":
                stack.append((tr, cur))
            elif op == "Q" and stack:
                tr, cur = stack.pop()
            elif op == "Tr" and i.operands:
                tr = int(num(i.operands[0]))
            elif op == "Tf" and i.operands:
                cur = _s(i.operands[0])
            elif op in SHOW_OPS and cur is not None and isinstance(fonts, Dictionary):
                f = fonts.get(cur)
                if isinstance(f, Dictionary):
                    key = f.objgen if f.is_indirect else id(f)
                    (invisible if tr == 3 else visible).add(key)
            elif op == "Do" and i.operands and isinstance(xo, Dictionary):
                x = xo.get(_s(i.operands[0]))
                if isinstance(x, pikepdf.Stream) and x.get("/Subtype") == Name.Form:
                    k = ("x", x.objgen)
                    if k not in seen:
                        seen.add(k)
                        scan(x, x.get("/Resources") or res, depth + 1)

    for page in pdf.pages:
        scan(page, page_resources(page.obj))
        for a in (page.obj.get("/Annots") or []):
            ap = a.get("/AP") if isinstance(a, Dictionary) else None
            if not isinstance(ap, Dictionary):
                continue
            n = ap.get("/N")
            streams = [n] if isinstance(n, pikepdf.Stream) else (
                [v for _, v in n.items()] if isinstance(n, Dictionary) else [])
            for st in streams:
                if isinstance(st, pikepdf.Stream):
                    scan(st, st.get("/Resources"))
    return visible, invisible


def _check_fonts(c: Collector, pdf: pikepdf.Pdf) -> None:
    visible, invisible = _font_usage(pdf)
    not_embedded, no_unicode, no_c2g = [], [], []
    for f in iter_font_dicts(pdf):
        key = f.objgen if f.is_indirect else id(f)
        if key not in visible and key not in invisible:
            continue                      # declared but never used
        name = base_name(f.get("/BaseFont"))
        sub = f.get("/Subtype")
        fd = f.get("/FontDescriptor")
        df = None
        if sub == Name.Type0:
            df = (f.get("/DescendantFonts") or [None])[0]
            fd = df.get("/FontDescriptor") if isinstance(df, Dictionary) else None
        embedded = sub == Name.Type3 or (isinstance(fd, Dictionary) and any(
            k in fd for k in ("/FontFile", "/FontFile2", "/FontFile3")))
        if not embedded and key in visible:
            not_embedded.append(name)
        if isinstance(df, Dictionary) and df.get("/Subtype") == Name.CIDFontType2 and embedded \
                and "/CIDToGIDMap" not in df:
            no_c2g.append(name)
        if "/ToUnicode" in f:
            continue
        if sub == Name.Type0:
            enc = _s(f.get("/Encoding"))
            info = df.get("/CIDSystemInfo") if isinstance(df, Dictionary) else None
            ordering = _s(info.get("/Ordering")) if isinstance(info, Dictionary) else ""
            reg = _s(info.get("/Registry")) if isinstance(info, Dictionary) else ""
            if not (reg == "Adobe" and ordering in ("GB1", "CNS1", "Japan1", "Korea1")
                    and "Identity" not in enc):
                no_unicode.append(name)
        else:
            names, explicit = font_encoding(f)
            flags = int(num(fd.get("/Flags"), 0)) if isinstance(fd, Dictionary) else 0
            symbolic = bool(flags & 4) and not bool(flags & 32)
            if sub == Name.TrueType and symbolic:
                no_unicode.append(name)
                continue
            if sub == Name.Type3 and not explicit:
                no_unicode.append(name)
                continue
            first = int(num(f.get("/FirstChar"), 0))
            last = int(num(f.get("/LastChar"), 255))
            if explicit:
                bad = [c for c in range(max(0, first), min(255, last) + 1)
                       if names[c] != ".notdef" and not glyph_unicode(names[c])]
                if bad:
                    no_unicode.append(name)
    if not_embedded:
        c.fail("UA-80", "Fonts not embedded", "7.21.4.1",
               ", ".join(sorted(set(not_embedded))[:10]), fixable="auto", n=len(set(not_embedded)) - 1)
    else:
        c.ok("UA-80", "All fonts used for rendering are embedded", "7.21.4.1")
    if no_unicode:
        c.fail("UA-81", "Fonts without a reliable Unicode mapping", "7.21.7",
               ", ".join(sorted(set(no_unicode))[:10]), fixable="auto", n=len(set(no_unicode)) - 1)
    else:
        c.ok("UA-81", "All fonts map to Unicode", "7.21.7")
    if no_c2g:
        c.fail("UA-82", "CIDFontType2 without CIDToGIDMap", "7.21.3.2",
               ", ".join(sorted(set(no_c2g))[:10]), fixable="auto")


def score(findings: list[Finding]) -> dict:
    errors = sum(1 for f in findings if f.severity == "error")
    warnings = sum(1 for f in findings if f.severity == "warning")
    passes = sum(1 for f in findings if f.severity == "pass")
    checked = errors + warnings + passes
    return {"errors": errors, "warnings": warnings, "passes": passes, "checked": checked,
            "percent": round(100 * passes / checked) if checked else 0,
            "verapdf": next((f.detail for f in findings if f.code == "VERA"), "")}


# ================================================================ veraPDF
def find_verapdf(configured: str = "") -> str:
    for cand in (configured, shutil.which("verapdf") or "", shutil.which("verapdf.bat") or "",
                 os.path.expanduser("~/verapdf/verapdf"), "/opt/verapdf/verapdf",
                 "C:/verapdf/verapdf.bat", "C:/Program Files/veraPDF/verapdf.bat"):
        if cand and os.path.exists(cand):
            return cand
    return ""


def _walk_json(obj, key):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            yield from _walk_json(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_json(v, key)


def run_verapdf(path: str, exe: str, timeout: int = 600) -> tuple[list[Finding], dict]:
    """Run veraPDF (PDF/UA-1 profile) and convert failed rules into findings."""
    summary: dict[str, Any] = {"ran": False}
    if not exe:
        return [], summary
    cmd = [exe, "--flavour", "ua1", "--format", "json", "--maxfailuresdisplayed", "5", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        summary["error"] = str(exc)
        return [], summary
    out = r.stdout.strip()
    start = out.find("{")
    try:
        data = json.loads(out[start:]) if start >= 0 else {}
    except Exception as exc:
        summary["error"] = f"could not parse veraPDF output: {exc}"
        summary["raw"] = out[:2000]
        return [], summary
    summary["ran"] = True
    compliant = next((v for v in _walk_json(data, "compliant") if isinstance(v, bool)), None)
    if compliant is None:
        # batch-only summaries report counts: compliant=1 / nonCompliant=0
        counts = [v for v in _walk_json(data, "nonCompliant") if isinstance(v, int)]
        if counts:
            compliant = counts[0] == 0
    summary["compliant"] = compliant
    findings: list[Finding] = []
    for rules in _walk_json(data, "ruleSummaries"):
        for rule in rules or []:
            status = str(rule.get("ruleStatus") or rule.get("status") or "").lower()
            if status not in ("failed", "fail"):
                continue
            clause = str(rule.get("clause", ""))
            test = rule.get("testNumber", "")
            desc = str(rule.get("description", "")).strip()
            failed = int(rule.get("failedChecks", 0) or 0)
            obj = str(rule.get("object", ""))
            findings.append(Finding(
                code=f"VP-{clause}-{test}", title=desc[:160] or f"Rule {clause}-{test}",
                severity="error", clause=f"veraPDF {clause}-{test}",
                detail=f"{failed} failed check(s) on {obj}", count=failed or 1,
                fixable="manual", source="verapdf"))
    summary["failed_rules"] = len(findings)
    verdict = ("veraPDF: PDF/UA-1 compliant" if compliant else
               f"veraPDF: {len(findings)} rule(s) failed")
    findings.insert(0, Finding("VERA", "veraPDF PDF/UA-1 validation",
                               "pass" if compliant else "error", "ISO 14289-1", verdict,
                               source="verapdf"))
    return findings, summary
