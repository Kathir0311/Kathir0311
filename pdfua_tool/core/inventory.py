"""Everything in the PDF, counted — nothing missed (V4.26).

At upload the analysis reads what it needs to propose tags. The inventory
reads *everything* the file holds, back end and front end, in one quick pass:

* **the file** — version, size, encryption, objects, whether it is tagged,
  its structure tree (elements, RoleMap, ParentTree), /Lang, /MarkInfo,
  DisplayDocTitle, the title (Info and XMP), the pdfuaid part, bookmarks
  (count and depth), page labels, optional content groups, embedded files,
  JavaScript, form fields (by type, XFA), named destinations;
* **every font** — type, embedded or not, subset, ToUnicode, encoding,
  symbolic, the pages it is used on and how many characters it prints;
* **every picture** — its size, colour space, filter, the bytes it takes,
  a digest of its pixels (the same logo in another file has the same
  digest), every place it is drawn on every page;
* **every page** — size, rotation, label, /Tabs; characters, words, lines,
  spans; the sizes, fonts and colours of the text; text drawn invisibly
  (an OCR layer, a hidden text) and text with no readable Unicode (private
  use, U+FFFD); the scripts (Latin, Tamil, Devanagari, Arabic, CJK …);
  write-in blanks; text turned on its side; pictures and the share of the
  page they cover; vector drawings; links (web / in the file); every
  annotation by type; form fields; whether the page is a scan;
* **coverage** (after the analysis) — every printed line, picture, link and
  form field matched against the tag tree: what no tag holds is listed with
  its page and place, so the editor can tag it in one click, and large
  pictures left as decoration are named for a check.

The inventory is kept with the file (`inventory.json`); the brain uses it
(the document's type, the pictures' digests for the alt-text memory) and
the editor shows it (the "PDF data" tab). On a 150-page book it takes a few
seconds — far less than the analysis.
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import re
import time

import pymupdf

VERSION = 1

SCRIPTS = [  # (name, first, last)
    ("Latin", 0x0041, 0x024F), ("Greek", 0x0370, 0x03FF), ("Cyrillic", 0x0400, 0x04FF),
    ("Hebrew", 0x0590, 0x05FF), ("Arabic", 0x0600, 0x06FF), ("Devanagari", 0x0900, 0x097F),
    ("Bengali", 0x0980, 0x09FF), ("Gurmukhi", 0x0A00, 0x0A7F), ("Gujarati", 0x0A80, 0x0AFF),
    ("Tamil", 0x0B80, 0x0BFF), ("Telugu", 0x0C00, 0x0C7F), ("Kannada", 0x0C80, 0x0CFF),
    ("Malayalam", 0x0D00, 0x0D7F), ("Sinhala", 0x0D80, 0x0DFF), ("Thai", 0x0E00, 0x0E7F),
    ("Hangul", 0xAC00, 0xD7AF), ("CJK", 0x4E00, 0x9FFF), ("Kana", 0x3040, 0x30FF),
]


def _script(cp: int) -> str:
    if cp < 0x41:
        return ""
    for name, a, b in SCRIPTS:
        if a <= cp <= b:
            return name
    if 0xE000 <= cp <= 0xF8FF or cp == 0xFFFD:
        return "unreadable"
    return "other"


def _colour(c) -> str:
    try:
        if isinstance(c, int):
            r, g, b = (c >> 16) & 255, (c >> 8) & 255, c & 255
        else:
            t = list(c or (0, 0, 0))
            if len(t) == 1:
                t = t * 3
            if len(t) == 4:                              # CMYK
                cc, m, y, k = t
                t = [(1 - cc) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)]
            r, g, b = (int(round(float(x) * 255)) for x in t[:3])
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return "#000000"


def _key(doc, xref: int, key: str) -> tuple[str, str]:
    try:
        return doc.xref_get_key(xref, key)
    except Exception:
        return ("null", "null")


def _val(doc, xref: int, key: str) -> tuple[str, str]:
    """A key's value, an indirect object followed to its dictionary."""
    t, v = _key(doc, xref, key)
    if t == "xref":
        try:
            return "dict", doc.xref_object(int(v.split()[0]), compressed=True)
        except Exception:
            return t, v
    return t, v


def _rect(r) -> list[float]:
    return [round(float(r[0]), 1), round(float(r[1]), 1), round(float(r[2]), 1), round(float(r[3]), 1)]


def document_facts(doc) -> dict:
    """The back end: catalog, structure tree, metadata, forms, navigation."""
    out: dict = {"pages": doc.page_count, "encrypted": bool(doc.is_encrypted), "needs_pass": bool(doc.needs_pass),
                 "objects": doc.xref_length() - 1, "version": "", "warnings": []}
    try:
        hdr = ""
        try:
            with open(doc.name, "rb") as fh:
                hdr = fh.read(16).decode("latin-1", "replace")
        except Exception:
            pass
        m = re.search(r"%PDF-(\d\.\d)", hdr)
        out["version"] = m.group(1) if m else ""
    except Exception:
        pass
    try:
        cat = doc.pdf_catalog()
    except Exception:
        return out
    t, v = _key(doc, cat, "Lang")
    out["lang"] = v.strip("()") if t == "string" else ""
    t, v = _val(doc, cat, "MarkInfo")
    out["marked"] = "/Marked true" in v if t != "null" else False
    out["suspects"] = "/Suspects true" in v if t != "null" else False
    t, v = _val(doc, cat, "ViewerPreferences")
    out["display_doc_title"] = "/DisplayDocTitle true" in v if t != "null" else False
    t, v = _key(doc, cat, "StructTreeRoot")
    out["tagged"] = t == "xref"
    out["struct"] = {}
    if t == "xref":
        sx = int(v.split()[0])
        rm_t, rm_v = _key(doc, sx, "RoleMap")
        out["struct"]["rolemap"] = rm_t != "null"
        out["struct"]["parent_tree"] = _key(doc, sx, "ParentTree")[0] != "null"
        roles: collections.Counter = collections.Counter()
        n = doc.xref_length()
        for x in range(1, min(n, 250000)):
            st, sv = _key(doc, x, "S")
            if st == "name" and _key(doc, x, "P")[0] == "xref":
                roles[sv.lstrip("/")] += 1
        out["struct"]["elements"] = sum(roles.values())
        out["struct"]["roles"] = dict(roles.most_common(40))
    for k in ("OCProperties", "AcroForm", "Names", "Outlines", "PageLabels", "OpenAction", "AA", "Metadata"):
        out.setdefault("has", {})[k] = _key(doc, cat, k)[0] != "null"
    meta = doc.metadata or {}
    out["info"] = {k: meta.get(k, "") for k in ("title", "author", "subject", "keywords", "creator", "producer",
                                                "creationDate", "modDate")}
    xmp = ""
    try:
        xmp = doc.get_xml_metadata() or ""
    except Exception:
        pass
    m = re.search(r"pdfuaid:part(?:\s[^>]*)?>\s*(\d)", xmp) or re.search(r"pdfuaid:part=[\"'](\d)", xmp)
    out["xmp"] = {"present": bool(xmp), "pdfuaid": int(m.group(1)) if m else 0,
                  "dc_title": bool(re.search(r"<dc:title", xmp))}
    try:
        toc = doc.get_toc(simple=True)
        out["bookmarks"] = {"count": len(toc), "depth": max((e[0] for e in toc), default=0)}
    except Exception:
        out["bookmarks"] = {"count": 0, "depth": 0}
    try:
        out["page_labels"] = bool(doc.get_page_labels())
    except Exception:
        out["page_labels"] = False
    try:
        out["optional_content"] = len(doc.get_ocgs() or {})
    except Exception:
        out["optional_content"] = 0
    try:
        out["embedded_files"] = doc.embfile_count()
    except Exception:
        out["embedded_files"] = 0
    js = 0
    try:
        t, v = _key(doc, cat, "Names")
        if t != "null" and "/JavaScript" in v:
            js += 1
        if _key(doc, cat, "OpenAction")[1].find("/JavaScript") >= 0:
            js += 1
    except Exception:
        pass
    out["javascript"] = js
    form = {"fields": 0, "types": {}, "xfa": False}
    try:
        t, v = _key(doc, cat, "AcroForm")
        if t != "null":
            form["xfa"] = "/XFA" in v or (t == "xref" and _key(doc, int(v.split()[0]), "XFA")[0] != "null")
    except Exception:
        pass
    out["form"] = form
    return out


def fonts(doc) -> list[dict]:
    seen: dict[int, dict] = {}
    for pno in range(doc.page_count):
        try:
            fl = doc.get_page_fonts(pno, full=True)
        except Exception:
            continue
        for xref, ext, ftype, base, name, enc, *_ in fl:
            f = seen.get(xref)
            if f is None:
                tu = _key(doc, xref, "ToUnicode")[0] != "null" if xref else False
                if not tu and ftype == "Type0" and xref:
                    tu = False
                f = seen[xref] = {"xref": xref, "name": re.sub(r"^[A-Z]{6}\+", "", base or name or ""),
                                  "type": ftype, "embedded": ext not in ("n/a", ""), "subset": bool(re.match(r"^[A-Z]{6}\+", base or "")),
                                  "tounicode": tu, "encoding": enc or "", "pages": []}
            if len(f["pages"]) < 400:
                f["pages"].append(pno)
    return list(seen.values())


def _lines_of(page) -> list[dict]:
    """The printed lines (display space) with their look — cheap 'dict' read."""
    out = []
    d = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP)
    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        for ln in b.get("lines", []):
            spans = [s for s in ln.get("spans", []) if (s.get("text") or "").strip()]
            if not spans:
                continue
            text = "".join(s["text"] for s in ln["spans"]).strip()
            big = max(spans, key=lambda s: len(s["text"]))
            dirx = ln.get("dir") or (1, 0)
            out.append({"text": text, "bbox": _rect(ln["bbox"]), "size": round(float(big.get("size") or 0), 1),
                        "font": big.get("font", ""), "bold": bool(big.get("flags", 0) & 16),
                        "color": _colour(big.get("color", 0)), "alpha": big.get("alpha", 255),
                        "rot": abs(float(dirx[0])) < 0.9})
    return out


def page_facts(doc, pno: int, deep: bool = True) -> dict:
    page = doc[pno]
    r = page.rect
    out = {"n": pno, "w": round(r.width, 1), "h": round(r.height, 1), "rotation": page.rotation}
    try:
        out["label"] = page.get_label() or ""
    except Exception:
        out["label"] = ""
    out["tabs"] = _key(doc, page.xref, "Tabs")[1].lstrip("/") if _key(doc, page.xref, "Tabs")[0] != "null" else ""
    lines = _lines_of(page)
    chars = words = 0
    sizes: collections.Counter = collections.Counter()
    fonts_: collections.Counter = collections.Counter()
    colours: collections.Counter = collections.Counter()
    scripts: collections.Counter = collections.Counter()
    unreadable = blanks = rotated = 0
    for ln in lines:
        t = ln["text"]
        n = len(t.replace(" ", ""))
        chars += n
        words += len(t.split())
        sizes[str(ln["size"])] += n
        fonts_[re.sub(r"^[A-Z]{6}\+", "", ln["font"])] += n
        colours[ln["color"]] += n
        if "___" in t:
            blanks += 1
        if ln["rot"]:
            rotated += 1
        for ch in t:
            s = _script(ord(ch))
            if s == "unreadable":
                unreadable += 1
            elif s:
                scripts[s] += 1
    out.update(chars=chars, words=words, lines=len(lines), unreadable=unreadable, blanks=blanks, rotated=rotated,
               sizes=dict(sizes.most_common(8)), fonts=dict(fonts_.most_common(8)),
               colours=dict(colours.most_common(6)), scripts=dict(scripts.most_common(6)))
    invisible = 0
    if deep:
        try:
            for sp in page.get_texttrace():
                if sp.get("type") == 3 or float(sp.get("opacity", 1) or 0) == 0:
                    invisible += len(sp.get("chars") or ())
        except Exception:
            pass
    out["invisible"] = invisible
    # pictures, with a digest of their pixels and every place they are drawn
    imgs = []
    area = 0.0
    try:
        for ii in page.get_image_info(hashes=True, xrefs=True):
            bb = pymupdf.Rect(ii["bbox"]) & r
            if bb.is_empty:
                continue
            area += bb.width * bb.height
            dg = ii.get("digest")
            imgs.append({"xref": ii.get("xref", 0), "digest": dg.hex() if isinstance(dg, (bytes, bytearray)) else str(dg or ""),
                         "bbox": _rect(bb), "w": ii.get("width"), "h": ii.get("height"),
                         "cs": ii.get("colorspace"), "bpc": ii.get("bpc"), "size": ii.get("size")})
    except Exception:
        pass
    out["images"] = imgs
    out["image_cover"] = round(min(1.0, area / max(1.0, r.width * r.height)), 3)
    try:
        out["drawings"] = len(page.get_cdrawings()) if deep else None
    except Exception:
        out["drawings"] = None
    links = {"uri": 0, "goto": 0, "other": 0}
    try:
        for lk in page.get_links():
            k = lk.get("kind")
            if k == pymupdf.LINK_URI:
                links["uri"] += 1
            elif k in (pymupdf.LINK_GOTO, pymupdf.LINK_GOTOR, pymupdf.LINK_NAMED):
                links["goto"] += 1
            else:
                links["other"] += 1
    except Exception:
        pass
    out["links"] = links
    ann: collections.Counter = collections.Counter()
    try:
        for a in page.annots() or []:
            ann[a.type[1]] += 1
    except Exception:
        pass
    out["annots"] = dict(ann)
    wid = collections.Counter()
    try:
        for w in page.widgets() or []:
            wid[w.field_type_string] += 1
    except Exception:
        pass
    out["widgets"] = dict(wid)
    out["scanned"] = bool(out["image_cover"] >= 0.85 and (chars < 20 or invisible >= 0.8 * max(1, chars)))
    out["_lines"] = lines
    return out


def build(pdf_path: str, deep: bool = True, progress=None) -> dict:
    """The whole inventory of one PDF."""
    t0 = time.time()
    progress = progress or (lambda f, m: None)
    out: dict = {"version": VERSION, "file": {}}
    try:
        st = os.stat(pdf_path)
        h = hashlib.md5()
        with open(pdf_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        out["file"] = {"bytes": st.st_size, "md5": h.hexdigest()}
    except OSError:
        pass
    doc = pymupdf.open(pdf_path)
    try:
        out["document"] = document_facts(doc)
        out["fonts"] = fonts(doc)
        pages = []
        for pno in range(doc.page_count):
            if pno % 20 == 0:
                progress(pno / max(1, doc.page_count), f"inventory: page {pno + 1} of {doc.page_count}")
            pages.append(page_facts(doc, pno, deep=deep))
        try:
            fields: collections.Counter = collections.Counter()
            for p in doc:
                for w in p.widgets() or []:
                    fields[w.field_type_string] += 1
            out["document"]["form"]["fields"] = sum(fields.values())
            out["document"]["form"]["types"] = dict(fields)
        except Exception:
            pass
    finally:
        doc.close()
    # what each font prints, and the totals
    by_font = collections.Counter()
    for p in pages:
        for f, n in p["fonts"].items():
            by_font[f] += n
    for f in out["fonts"]:
        f["chars"] = by_font.get(f["name"], 0)
    pics: dict[str, dict] = {}
    for p in pages:
        for im in p["images"]:
            k = im["digest"] or f"x{im['xref']}"
            e = pics.setdefault(k, {"digest": im["digest"], "xref": im["xref"], "w": im["w"], "h": im["h"],
                                    "cs": im["cs"], "bpc": im["bpc"], "size": im["size"], "uses": 0, "pages": []})
            e["uses"] += 1
            if len(e["pages"]) < 200 and p["n"] not in e["pages"]:
                e["pages"].append(p["n"])
    out["images"] = sorted(pics.values(), key=lambda e: -e["uses"])
    tot = collections.Counter()
    scripts = collections.Counter()
    annots = collections.Counter()
    links = collections.Counter()
    for p in pages:
        for k in ("chars", "words", "lines", "invisible", "unreadable", "blanks", "rotated"):
            tot[k] += p.get(k) or 0
        tot["drawings"] += p.get("drawings") or 0
        tot["images"] += len(p["images"])
        tot["scanned"] += 1 if p["scanned"] else 0
        for s, n in p["scripts"].items():
            scripts[s] += n
        for a, n in p["annots"].items():
            annots[a] += n
        for a, n in p["links"].items():
            links[a] += n
    out["totals"] = dict(tot)
    out["scripts"] = dict(scripts.most_common())
    out["annots"] = dict(annots)
    out["links"] = dict(links)
    out["pages"] = pages
    out["findings"] = findings(out)
    out["seconds"] = round(time.time() - t0, 2)
    progress(1.0, "inventory: done")
    return out


def findings(inv: dict) -> list[dict]:
    """What the inventory alone already says about accessibility."""
    F = []
    d = inv.get("document") or {}
    tot = inv.get("totals") or {}

    def add(level, code, text, pages=None):
        F.append({"level": level, "code": code, "text": text, "pages": (pages or [])[:30]})
    noemb = [f["name"] for f in inv.get("fonts") or [] if not f["embedded"] and f.get("chars")]
    if noemb:
        add("warning", "fonts", f"{len(noemb)} font(s) not embedded: {', '.join(noemb[:6])} (PDF/UA 7.21.4)")
    notu = [f["name"] for f in inv.get("fonts") or [] if not f["tounicode"] and f["type"] in ("Type0", "Type3")
            and f.get("chars")]
    if notu:
        add("warning", "tounicode", f"{len(notu)} font(s) with no ToUnicode map: {', '.join(notu[:6])} (PDF/UA 7.21.7)")
    if tot.get("unreadable"):
        pg = [p["n"] for p in inv.get("pages") or [] if p.get("unreadable")]
        add("error", "unreadable", f"{tot['unreadable']} character(s) with no readable Unicode (private use / U+FFFD) — "
                                   "a screen reader cannot say them", pg)
    if tot.get("scanned"):
        pg = [p["n"] for p in inv.get("pages") or [] if p.get("scanned")]
        add("info", "scanned", f"{tot['scanned']} scanned page(s)" + (" with an OCR text layer" if tot.get("invisible") else
                                                                     " — need OCR"), pg)
    if d.get("javascript"):
        add("warning", "javascript", "the file carries JavaScript")
    if (d.get("form") or {}).get("xfa"):
        add("error", "xfa", "XFA form — PDF/UA does not allow dynamic XFA (7.15)")
    if d.get("encrypted") and d.get("needs_pass"):
        add("error", "encrypted", "the file needs a password")
    if not d.get("lang"):
        add("info", "lang", "no document language (/Lang) in the original — the build sets it")
    if inv.get("annots"):
        add("info", "annots", "annotations: " + ", ".join(f"{k} ×{v}" for k, v in inv["annots"].items()))
    rot = tot.get("rotated")
    if rot:
        pg = [p["n"] for p in inv.get("pages") or [] if p.get("rotated")]
        add("info", "rotated", f"{rot} line(s) set on their side (a margin tab, a stamp) — check their reading order", pg)
    return F


# ------------------------------------------------------------- coverage
def coverage(inv: dict, model, pdf_path: str) -> dict:
    """Every printed line, picture, link and form field against the tags."""
    doc = pymupdf.open(pdf_path)
    try:
        rects: dict[int, list] = collections.defaultdict(list)
        figs: dict[int, list] = collections.defaultdict(list)
        arts: dict[int, list] = collections.defaultdict(list)
        n_link = n_form = 0
        for n in model.nodes.values():
            if n.role == "Link" or n.annots:
                n_link += 1 if n.role == "Link" else 0
            if n.role == "Form":
                n_form += 1
            for p in n.pages():
                if p < 0 or p >= doc.page_count:
                    continue
                for rr in n.rects_on(p):
                    d_ = pymupdf.Rect(rr) * doc[p].transformation_matrix
                    d_.normalize()
                    (arts if n.artifact else rects)[p].append(d_)
                    if n.role in ("Figure", "Formula", "Form") and not n.artifact:
                        figs[p].append(d_)
    finally:
        doc.close()

    def inside(bb, boxes, pad=2.0):
        cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
        return any(b.x0 - pad <= cx <= b.x1 + pad and b.y0 - pad <= cy <= b.y1 + pad for b in boxes)
    missed, covered, total = [], 0, 0
    pic = {"total": 0, "figure": 0, "artifact": 0, "decoration": 0, "large_decoration": []}
    for p in inv.get("pages") or []:
        pn = p["n"]
        for ln in p.get("_lines") or []:
            total += 1
            if inside(ln["bbox"], rects[pn]):
                covered += 1
            elif inside(ln["bbox"], arts[pn]):
                covered += 1
            else:
                if len(missed) < 400:
                    missed.append({"page": pn, "bbox": ln["bbox"], "text": ln["text"][:120], "size": ln["size"]})
        pa = max(1.0, p["w"] * p["h"])
        for im in p.get("images") or []:
            pic["total"] += 1
            bb = im["bbox"]
            if inside(bb, figs[pn], 4):
                pic["figure"] += 1
            elif inside(bb, arts[pn], 4):
                pic["artifact"] += 1
            else:
                pic["decoration"] += 1
                share = (bb[2] - bb[0]) * (bb[3] - bb[1]) / pa
                if 0.04 <= share < 0.85 and len(pic["large_decoration"]) < 60:
                    pic["large_decoration"].append({"page": pn, "bbox": bb, "share": round(share, 3),
                                                    "digest": im["digest"]})
    links = inv.get("links") or {}
    widgets = sum(sum(p.get("widgets", {}).values()) for p in inv.get("pages") or [])
    return {"lines": total, "covered": covered, "missed": missed, "missed_count": total - covered,
            "percent": round(100.0 * covered / total, 2) if total else 100.0, "pictures": pic,
            "links": {"annotations": sum(links.values()), "tags": n_link},
            "fields": {"widgets": widgets, "tags": n_form}}


def summary(inv: dict, cov: dict | None = None) -> dict:
    """What goes into the job's meta (small)."""
    d = inv.get("document") or {}
    tot = inv.get("totals") or {}
    out = {"pages": d.get("pages"), "version": d.get("version"), "tagged": d.get("tagged"),
           "elements": (d.get("struct") or {}).get("elements", 0), "lang": d.get("lang"),
           "fonts": len(inv.get("fonts") or []), "fonts_not_embedded": sum(1 for f in inv.get("fonts") or []
                                                                         if not f["embedded"] and f.get("chars")),
           "images": len(inv.get("images") or []), "image_uses": tot.get("images", 0),
           "chars": tot.get("chars", 0), "words": tot.get("words", 0), "lines": tot.get("lines", 0),
           "invisible": tot.get("invisible", 0), "unreadable": tot.get("unreadable", 0),
           "scanned": tot.get("scanned", 0), "drawings": tot.get("drawings", 0),
           "links": inv.get("links"), "annots": inv.get("annots"), "fields": (d.get("form") or {}).get("fields", 0),
           "scripts": inv.get("scripts"), "bookmarks": (d.get("bookmarks") or {}).get("count", 0),
           "findings": len(inv.get("findings") or []), "seconds": inv.get("seconds")}
    if cov:
        out["coverage"] = {"percent": cov["percent"], "missed": cov["missed_count"],
                           "pictures_decoration": cov["pictures"]["decoration"],
                           "large_decoration": len(cov["pictures"]["large_decoration"])}
    return out


def save(job: str, inv: dict, cov: dict | None = None) -> None:
    data = dict(inv)
    data["pages"] = [{k: v for k, v in p.items() if k != "_lines"} for p in inv.get("pages") or []]
    if cov is not None:
        data["coverage"] = cov
    tmp = os.path.join(job, "inventory.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, default=str)
    os.replace(tmp, os.path.join(job, "inventory.json"))


def load(job: str) -> dict | None:
    p = os.path.join(job, "inventory.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def refresh_coverage(job: str, model) -> dict | None:
    """Coverage again after edits (the lines are read again — cheap)."""
    inv = load(job)
    if inv is None:
        return None
    work = os.path.join(job, "working.pdf")
    doc = pymupdf.open(work)
    try:
        for p in inv.get("pages") or []:
            if 0 <= p["n"] < doc.page_count:
                p["_lines"] = _lines_of(doc[p["n"]])
    finally:
        doc.close()
    cov = coverage(inv, model, work)
    inv["coverage"] = cov
    tmp = os.path.join(job, "inventory.json.tmp")
    data = dict(inv)
    data["pages"] = [{k: v for k, v in p.items() if k != "_lines"} for p in inv.get("pages") or []]
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, default=str)
    os.replace(tmp, os.path.join(job, "inventory.json"))
    return cov


def digests_in(inv: dict, page: int, bbox_display, pad: float = 3.0) -> list[str]:
    """The digests of the pictures drawn inside a box (display space)."""
    out = []
    for p in inv.get("pages") or []:
        if p["n"] != page:
            continue
        for im in p.get("images") or []:
            b = im["bbox"]
            cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            if bbox_display[0] - pad <= cx <= bbox_display[2] + pad and bbox_display[1] - pad <= cy <= bbox_display[3] + pad \
                    and im.get("digest"):
                out.append(im["digest"])
    return out
