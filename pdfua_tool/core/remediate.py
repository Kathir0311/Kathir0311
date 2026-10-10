"""Document-level PDF/UA fixes that need no human judgement.

Operates on an open pikepdf.Pdf so tagging + remediation are saved once.
"""
from __future__ import annotations

import re

import pikepdf
from pikepdf import Array, Dictionary, Name, String

from .model import DocModel
from .options import Options

PDFUA_NS = "http://www.aiim.org/pdfua/ns/id/"

try:
    pikepdf.models.PdfMetadata.register_xml_namespace(PDFUA_NS, "pdfuaid")
except Exception:
    pass


MARKUP_SUBTYPES = {"/Square", "/Circle", "/Line", "/Ink", "/Highlight", "/Underline", "/StrikeOut",
                   "/Squiggly", "/Polygon", "/PolyLine", "/Caret", "/Stamp", "/FreeText"}


def is_empty_markup(a, max_pt: float = 12.0) -> bool:
    """A leftover review mark: markup annotation, tiny, and no comment text."""
    try:
        if str(a.get("/Subtype", "")) not in MARKUP_SUBTYPES:
            return False
        if str(a.get("/Contents", "")).strip() or str(a.get("/RC", "")).strip() or "/Popup" in a and \
                str(a.Popup.get("/Contents", "")).strip():
            return False
        x0, y0, x1, y1 = (float(v) for v in a.Rect)
        return abs(x1 - x0) <= max_pt and abs(y1 - y0) <= max_pt
    except Exception:
        return False


def _clean(s: str, limit: int = 400) -> str:
    return re.sub(r"\s+", " ", s or "").strip()[:limit]


def set_metadata(pdf: pikepdf.Pdf, title: str, lang: str) -> list[str]:
    done = []
    try:
        with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
            if title:
                meta["dc:title"] = title
            if lang:
                meta["dc:language"] = [lang]
            meta["pdfuaid:part"] = "1"
        done.append("wrote XMP metadata (dc:title, pdfuaid:part=1)")
        return done
    except Exception as exc:
        done.append(f"XMP via pikepdf failed ({exc}); used raw XMP")
    # fallback: write a minimal packet ourselves
    t = (title or "").replace("&", "&amp;").replace("<", "&lt;")
    xmp = f"""<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title><rdf:Alt><rdf:li xml:lang="x-default">{t}</rdf:li></rdf:Alt></dc:title>
</rdf:Description>
<rdf:Description rdf:about="" xmlns:pdfuaid="{PDFUA_NS}"><pdfuaid:part>1</pdfuaid:part></rdf:Description>
</rdf:RDF></x:xmpmeta><?xpacket end="w"?>"""
    s = pdf.make_stream(xmp.encode("utf-8"))
    s.Type = Name.Metadata
    s.Subtype = Name.XML
    pdf.Root.Metadata = s
    return done


def _bookmarks(pdf: pikepdf.Pdf, model: DocModel, levels: int) -> int:
    heads = [n for n in model.ordered_nodes()
             if n.is_heading and not n.artifact and n.page >= 0
             and not any(a.artifact for a in model.ancestors(n.id))]
    heads = [h for h in heads if int(h.role[1]) <= max(1, levels)]
    if not heads:
        return 0
    count = 0
    with pdf.open_outline() as outline:
        outline.root.clear()
        stack: list[tuple[int, pikepdf.OutlineItem]] = []
        for h in heads:
            lvl = int(h.role[1])
            label = _clean(h.text, 160) or h.role
            top = h.bbox[3] if h.bbox else None
            item = pikepdf.OutlineItem(label, destination=h.page,
                                       page_location=pikepdf.PageLocation.FitH, top=top)
            while stack and stack[-1][0] >= lvl:
                stack.pop()
            (stack[-1][1].children if stack else outline.root).append(item)
            stack.append((lvl, item))
            count += 1
    return count


def remediate(pdf: pikepdf.Pdf, model: DocModel, opts: Options,
              link_texts: dict[int, str] | None = None,
              field_labels: dict[int, str] | None = None,
              link_overrides: dict[int, str] | None = None) -> dict:
    applied: list[str] = []
    review: list[str] = []
    link_texts = link_texts or {}
    field_labels = field_labels or {}
    link_overrides = link_overrides or {}

    title = _clean(model.title) or _clean(str(pdf.docinfo.get("/Title", "")) if pdf.docinfo else "")
    lang = (model.lang or opts.lang or "en-US").strip()

    # language
    if str(pdf.Root.get("/Lang", "")) != lang:
        pdf.Root.Lang = String(lang)
        applied.append(f"document language set to {lang}")

    # title
    if title:
        try:
            pdf.docinfo["/Title"] = String(title)
        except Exception:
            pass
        applied.append("document title set")
    else:
        review.append("No document title — set one in the editor")

    vp = pdf.Root.get("/ViewerPreferences")
    if not isinstance(vp, Dictionary):
        vp = pdf.make_indirect(Dictionary())
        pdf.Root.ViewerPreferences = vp
    if vp.get("/DisplayDocTitle") is not True:
        vp.DisplayDocTitle = True
        applied.append("window shows document title")

    mi = pdf.Root.get("/MarkInfo")
    if isinstance(mi, Dictionary) and mi.get("/Suspects") is True:
        mi.Suspects = False
        applied.append("cleared MarkInfo/Suspects")

    # tab order
    tabs = 0
    for page in pdf.pages:
        if page.obj.get("/Tabs") != Name.S:
            page.obj.Tabs = Name.S
            tabs += 1
    if tabs:
        applied.append(f"tab order follows structure on {tabs} page(s)")

    # annotations
    link_fixed = tu_fixed = 0
    for page in pdf.pages:
        for a in (page.obj.get("/Annots") or []):
            if not isinstance(a, Dictionary):
                continue
            sub = a.get("/Subtype")
            if sub == Name.Link:
                try:
                    over = link_overrides.get(a.objgen[0], "")
                except Exception:
                    over = ""
                if over:
                    a.Contents = String(_clean(over, 200))
                    link_fixed += 1
                elif not str(a.get("/Contents", "")).strip():
                    uri = ""
                    act = a.get("/A")
                    if isinstance(act, Dictionary):
                        uri = str(act.get("/URI", ""))
                    text = ""
                    try:
                        text = link_texts.get(a.objgen[0], "")
                    except Exception:
                        pass
                    desc = (_clean(text, 200) if opts.link_contents == "text" and text else "") \
                        or uri or _clean(text, 200)
                    if not desc:
                        dest = a.get("/Dest") or (act.get("/D") if isinstance(act, Dictionary) else None)
                        desc = "Link within this document" if dest is not None else "Link"
                    a.Contents = String(desc)
                    link_fixed += 1
            elif sub == Name.Widget:
                field = a if "/T" in a or "/FT" in a else a.get("/Parent")
                if isinstance(field, Dictionary) and not str(field.get("/TU", "")).strip():
                    label = ""
                    try:
                        label = field_labels.get(a.objgen[0], "")
                    except Exception:
                        pass
                    if label and opts.form_tooltips_from_labels:
                        field.TU = String(_clean(label, 200))
                        tu_fixed += 1
                        review.append(f"Field tooltip taken from nearby label: “{_clean(label, 60)}”")
    if link_fixed:
        applied.append(f"{link_fixed} link(s) given a description")
    if tu_fixed:
        applied.append(f"{tu_fixed} form field(s) given a tooltip from their label")

    # optional content configuration names (7.10)
    ocp = pdf.Root.get("/OCProperties")
    if isinstance(ocp, Dictionary):
        n = 0
        d = ocp.get("/D")
        if isinstance(d, Dictionary) and not str(d.get("/Name", "")).strip():
            d.Name = String("Default")
            n += 1
        for k, cfg in enumerate(ocp.get("/Configs") or []):
            if isinstance(cfg, Dictionary) and not str(cfg.get("/Name", "")).strip():
                cfg.Name = String(f"Configuration {k + 1}")
                n += 1
            if isinstance(cfg, Dictionary) and "/AS" in cfg:
                del cfg["/AS"]
        if isinstance(d, Dictionary) and "/AS" in d:
            del d["/AS"]
            n += 1
        if n:
            applied.append("optional content configurations named (7.10)")

    # embedded files (7.11)
    ef_fixed = 0
    names = pdf.Root.get("/Names")
    efs = names.get("/EmbeddedFiles") if isinstance(names, Dictionary) else None

    def walk_nametree(node, depth=0):
        if depth > 20 or not isinstance(node, Dictionary):
            return
        arr = node.get("/Names")
        if isinstance(arr, Array):
            for i in range(1, len(arr), 2):
                yield arr[i - 1], arr[i]
        for kid in (node.get("/Kids") or []):
            yield from walk_nametree(kid, depth + 1)

    if isinstance(efs, Dictionary):
        for key, spec in walk_nametree(efs):
            if not isinstance(spec, Dictionary):
                continue
            fname = str(spec.get("/UF") or spec.get("/F") or key)
            if "/F" not in spec:
                spec.F = String(fname)
                ef_fixed += 1
            if "/UF" not in spec:
                spec.UF = String(fname)
                ef_fixed += 1
            if "/Desc" not in spec:
                spec.Desc = String(fname)
    if ef_fixed:
        applied.append("embedded file specifications given /F and /UF (7.11)")

    # CIDFontType2 needs CIDToGIDMap (7.21.3.2)
    cid_fixed = 0
    seen = set()
    for page in pdf.pages:
        res = page.obj.get("/Resources")
        fonts = res.get("/Font") if isinstance(res, Dictionary) else None
        if not isinstance(fonts, Dictionary):
            continue
        for _, f in fonts.items():
            try:
                if f.objgen in seen:
                    continue
                seen.add(f.objgen)
            except Exception:
                pass
            if not isinstance(f, Dictionary) or f.get("/Subtype") != Name.Type0:
                continue
            for df in (f.get("/DescendantFonts") or []):
                if isinstance(df, Dictionary) and df.get("/Subtype") == Name.CIDFontType2 \
                        and "/CIDToGIDMap" not in df:
                    fd = df.get("/FontDescriptor")
                    if isinstance(fd, Dictionary) and "/FontFile2" in fd:
                        df.CIDToGIDMap = Name.Identity
                        cid_fixed += 1
    if cid_fixed:
        applied.append(f"CIDToGIDMap made explicit on {cid_fixed} font(s) (7.21.3.2)")

    # bookmarks
    marks = 0
    if opts.bookmarks == "headings":
        marks = _bookmarks(pdf, model, opts.bookmark_levels)
        if marks:
            applied.append(f"{marks} bookmark(s) generated from headings")
            pdf.Root.PageMode = Name.UseOutlines
    elif opts.bookmarks == "off":
        if "/Outlines" in pdf.Root:
            del pdf.Root["/Outlines"]

    applied += set_metadata(pdf, title, lang)
    return {"applied": applied, "review": review, "bookmarks": marks}
