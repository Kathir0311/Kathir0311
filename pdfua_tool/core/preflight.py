"""Preflight: make a working copy that tags cleanly.

Steps (each optional, each logged):
  * annotations made indirect (needed for OBJR)
  * text-bearing form XObjects unpacked into the page stream
  * non-embedded standard fonts embedded (metric-compatible URW fonts)
  * ToUnicode maps added where they can be derived with certainty
  * OCR text layer for image-only pages (Tesseract)
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
from typing import Any

import pikepdf
import pymupdf
from pikepdf import Array, Dictionary, Name, Operator, String
from pikepdf import ContentStreamInstruction as CSI

from .options import Options
from .pdftext import BASE14, base_name, contains_text, font_encoding, glyph_unicode, num, page_resources

RES_CATS = ("/Font", "/XObject", "/ExtGState", "/ColorSpace", "/Pattern",
            "/Shading", "/Properties")
DEVICE_CS = {"/DeviceRGB", "/DeviceGray", "/DeviceCMYK", "/Pattern"}


# ================================================================= helpers
def _own_resources(page: pikepdf.Page) -> Dictionary:
    res = page.obj.get("/Resources")
    if isinstance(res, Dictionary):
        return res
    inherited = page_resources(page.obj)
    copy = Dictionary()
    for k, v in inherited.items():
        copy[k] = v
    page.obj.Resources = copy
    return copy


def _gs_params(res: Dictionary, name) -> dict:
    out = {}
    try:
        g = res.get("/ExtGState").get(name)
    except Exception:
        return out
    if not isinstance(g, Dictionary):
        return out
    for k in ("/CA", "/ca"):
        if k in g:
            out[k] = num(g[k], 1.0)
    if "/BM" in g:
        bm = g["/BM"]
        if isinstance(bm, Array):
            bm = bm[0] if len(bm) else Name.Normal
        out["/BM"] = str(bm)
    if "/SMask" in g:
        out["/SMask"] = str(g["/SMask"]) if isinstance(g["/SMask"], Name) else "dict"
    return out


def _form_uses_only_normal(form: pikepdf.Stream, depth: int = 0) -> bool:
    if depth > 6:
        return False
    res = form.get("/Resources")
    if not isinstance(res, Dictionary):
        return True
    egs = res.get("/ExtGState")
    if isinstance(egs, Dictionary):
        for _, g in egs.items():
            if not isinstance(g, Dictionary):
                continue
            bm = g.get("/BM")
            if isinstance(bm, Array):
                bm = bm[0] if len(bm) else Name.Normal
            if bm is not None and str(bm) not in ("/Normal", "/Compatible"):
                return False
            sm = g.get("/SMask")
            if sm is not None and not (isinstance(sm, Name) and str(sm) == "/None"):
                return False
    xo = res.get("/XObject")
    if isinstance(xo, Dictionary):
        for _, x in xo.items():
            if isinstance(x, pikepdf.Stream) and x.get("/Subtype") == Name.Form:
                if not _form_uses_only_normal(x, depth + 1):
                    return False
    return True


def _form_eligible(form: Any, outer: dict) -> tuple[bool, str]:
    if not isinstance(form, pikepdf.Stream) or form.get("/Subtype") != Name.Form:
        return False, "not a form"
    if "/Ref" in form:
        return False, "reference XObject"
    if "/OC" in form:
        return False, "optional content on form"
    res = form.get("/Resources")
    if isinstance(res, Dictionary) and isinstance(res.get("/Pattern"), Dictionary) \
            and len(res.get("/Pattern")):
        return False, "uses patterns"
    if not contains_text(form):
        return False, "no text"
    grp = form.get("/Group")
    if isinstance(grp, Dictionary):
        if grp.get("/K") is True:
            return False, "knockout group"
        if outer.get("/ca", 1.0) < 1 or outer.get("/CA", 1.0) < 1:
            return False, "group drawn with transparency"
        if outer.get("/BM", "/Normal") not in ("/Normal", "/Compatible"):
            return False, "group drawn with blend mode"
        if outer.get("/SMask", "/None") != "/None":
            return False, "group drawn with soft mask"
        if not _form_uses_only_normal(form):
            return False, "group uses blend modes"
    return True, ""


class FormFlattener:
    def __init__(self, pdf: pikepdf.Pdf, import_mode: bool) -> None:
        self.pdf = pdf
        self.import_mode = import_mode
        self.count = 0
        self.skipped: dict[str, int] = {}

    def _unique(self, cat_dict: Dictionary, base: str, obj) -> str:
        cand = base
        k = 2
        while cand in cat_dict:
            try:
                if cat_dict[cand].objgen == obj.objgen and obj.is_indirect:
                    return cand
            except Exception:
                pass
            cand = f"{base}_{k}"
            k += 1
        return cand

    def _rename_map(self, page_res: Dictionary, fres: Dictionary, tag: str) -> dict:
        ren: dict[tuple[str, str], Name] = {}
        for cat in RES_CATS:
            d = fres.get(cat)
            if not isinstance(d, Dictionary):
                continue
            target = page_res.get(cat)
            if not isinstance(target, Dictionary):
                target = Dictionary()
                page_res[cat] = target
            for name, obj in d.items():
                new = self._unique(target, f"{name}_{tag}", obj)
                target[new] = obj
                ren[(cat, name)] = Name(new)
        return ren

    def _rename(self, ins: CSI, ren: dict) -> CSI:
        if not ren:
            return ins
        op = str(ins.operator)
        ops = list(ins.operands)

        def r(cat, v):
            if isinstance(v, Name) and (cat, str(v)) in ren:
                return ren[(cat, str(v))]
            return v
        if op == "Tf" and ops:
            ops[0] = r("/Font", ops[0])
        elif op == "Do" and ops:
            ops[0] = r("/XObject", ops[0])
        elif op == "gs" and ops:
            ops[0] = r("/ExtGState", ops[0])
        elif op in ("cs", "CS") and ops and str(ops[0]) not in DEVICE_CS:
            ops[0] = r("/ColorSpace", ops[0])
        elif op in ("scn", "SCN") and ops and isinstance(ops[-1], Name):
            ops[-1] = r("/Pattern", ops[-1])
        elif op == "sh" and ops:
            ops[0] = r("/Shading", ops[0])
        elif op in ("BDC", "DP") and len(ops) > 1 and isinstance(ops[1], Name):
            ops[1] = r("/Properties", ops[1])
        else:
            return ins
        return CSI(ops, ins.operator)

    def inline(self, form: pikepdf.Stream, page_res: Dictionary, depth: int) -> list[CSI]:
        fres = form.get("/Resources")
        own = isinstance(fres, Dictionary)
        tag = f"x{form.objgen[0]}"
        ren = self._rename_map(page_res, fres, tag) if own else {}
        lookup_res = fres if own else page_res
        out: list[CSI] = [CSI([], Operator("q"))]
        mx = form.get("/Matrix")
        if isinstance(mx, Array) and len(mx) == 6:
            vals = [num(v) for v in mx]
            if vals != [1, 0, 0, 1, 0, 0]:
                out.append(CSI(list(mx), Operator("cm")))
        bb = form.get("/BBox")
        if isinstance(bb, Array) and len(bb) == 4:
            x0, y0, x1, y1 = (num(v) for v in bb)
            out.append(CSI([min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)], Operator("re")))
            out.append(CSI([], Operator("W")))
            out.append(CSI([], Operator("n")))
        body = self._walk(list(pikepdf.parse_content_stream(form)), lookup_res, page_res,
                          ren, depth + 1, stm=form.objgen[0])
        out.extend(body)
        out.append(CSI([], Operator("Q")))
        self.count += 1
        return out

    def _walk(self, ins: list[CSI], lookup_res: Dictionary, page_res: Dictionary,
              ren: dict, depth: int, stm: int | None = None) -> list[CSI]:
        out: list[CSI] = []
        gstack: list[dict] = []
        gcur: dict = {}
        xo = lookup_res.get("/XObject") if isinstance(lookup_res, Dictionary) else None
        for i in ins:
            op = str(i.operator)
            if op == "q":
                gstack.append(dict(gcur))
            elif op == "Q":
                gcur = gstack.pop() if gstack else {}
            elif op == "gs" and i.operands:
                gcur.update(_gs_params(lookup_res, i.operands[0]))
            if op == "Do" and i.operands and isinstance(xo, Dictionary) and depth <= 8:
                child = xo.get(str(i.operands[0]))
                ok, why = _form_eligible(child, gcur)
                if ok:
                    out.extend(self.inline(child, page_res, depth))
                    continue
                if isinstance(child, pikepdf.Stream) and child.get("/Subtype") == Name.Form \
                        and why not in ("no text", "not a form"):
                    self.skipped[why] = self.skipped.get(why, 0) + 1
            if stm is not None and op == "BDC" and len(i.operands) > 1 \
                    and isinstance(i.operands[1], Dictionary) and "/MCID" in i.operands[1]:
                props = Dictionary()
                for k, v in i.operands[1].items():
                    props[k] = v
                props.PDFUA_Stm = stm
                i = CSI([i.operands[0], props], i.operator)
            out.append(self._rename(i, ren))
        return out

    def run(self) -> None:
        for page in self.pdf.pages:
            res = page_resources(page.obj)
            xo = res.get("/XObject")
            if not isinstance(xo, Dictionary) or not len(xo):
                continue
            has_form_text = any(
                isinstance(x, pikepdf.Stream) and x.get("/Subtype") == Name.Form and contains_text(x)
                for _, x in xo.items())
            if not has_form_text:
                continue
            page_res = _own_resources(page)
            try:
                ins = list(pikepdf.parse_content_stream(page))
            except Exception:
                continue
            before = self.count
            new = self._walk(ins, page_res, page_res, {}, 0)
            if self.count != before:
                page.obj.Contents = self.pdf.make_stream(pikepdf.unparse_content_stream(new))


# ============================================================ font helpers
def iter_font_dicts(pdf: pikepdf.Pdf):
    """Every font dictionary used by pages, forms, annotation appearances and AcroForm."""
    seen: set = set()

    def fonts_in(res, depth=0):
        if not isinstance(res, Dictionary) or depth > 8:
            return
        fonts = res.get("/Font")
        if isinstance(fonts, Dictionary):
            for _, f in fonts.items():
                if isinstance(f, Dictionary):
                    key = f.objgen if f.is_indirect else id(f)
                    if key not in seen:
                        seen.add(key)
                        yield f
        xo = res.get("/XObject")
        if isinstance(xo, Dictionary):
            for _, x in xo.items():
                if isinstance(x, pikepdf.Stream) and x.get("/Subtype") == Name.Form:
                    key = ("x", x.objgen)
                    if key in seen:
                        continue
                    seen.add(key)
                    yield from fonts_in(x.get("/Resources"), depth + 1)

    for page in pdf.pages:
        yield from fonts_in(page_resources(page.obj))
        for a in (page.obj.get("/Annots") or []):
            if not isinstance(a, Dictionary):
                continue
            ap = a.get("/AP")
            if not isinstance(ap, Dictionary):
                continue
            for key in ("/N", "/R", "/D"):
                s = ap.get(key)
                streams = [s] if isinstance(s, pikepdf.Stream) else (
                    [v for _, v in s.items()] if isinstance(s, Dictionary) else [])
                for st in streams:
                    if isinstance(st, pikepdf.Stream):
                        yield from fonts_in(st.get("/Resources"))
    af = pdf.Root.get("/AcroForm")
    if isinstance(af, Dictionary):
        yield from fonts_in(af.get("/DR"))


def _base14_key(fontname: str) -> str | None:
    if fontname in BASE14:
        return BASE14[fontname]
    stem = fontname.replace(" ", "")
    for k, v in BASE14.items():
        if stem.lower() == k.lower():
            return v
    return None


def embed_standard_fonts(pdf: pikepdf.Pdf) -> tuple[int, list[str]]:
    embedded = 0
    left: list[str] = []
    cache: dict[str, Any] = {}
    for f in iter_font_dicts(pdf):
        sub = f.get("/Subtype")
        if sub not in (Name.Type1, Name.TrueType, Name.MMType1):
            if sub == Name.Type0:
                df = (f.get("/DescendantFonts") or [None])[0]
                fd = df.get("/FontDescriptor") if isinstance(df, Dictionary) else None
                if not (isinstance(fd, Dictionary) and any(k in fd for k in ("/FontFile", "/FontFile2", "/FontFile3"))):
                    left.append(base_name(f.get("/BaseFont")))
            continue
        fd = f.get("/FontDescriptor")
        if isinstance(fd, Dictionary) and any(k in fd for k in ("/FontFile", "/FontFile2", "/FontFile3")):
            continue
        name = base_name(f.get("/BaseFont"))
        key = _base14_key(name)
        if key is None:
            left.append(name)
            continue
        if sub == Name.TrueType and not isinstance(f.get("/Encoding"), (Name, Dictionary)):
            left.append(name)
            continue
        try:
            font = cache.get(key) or pymupdf.Font(key)
            cache[key] = font
            data = font.buffer
        except Exception:
            left.append(name)
            continue
        symbolic = key in ("symb", "zadb")
        ff = pdf.make_stream(bytes(data))
        ff.Subtype = Name.Type1C
        bold = key in ("hebo", "hebi", "tibo", "tibi", "cobo", "cobi")
        ital = key in ("heit", "hebi", "tiit", "tibi", "coit", "cobi")
        flags = (4 if symbolic else 32) | (64 if ital else 0) | \
            (1 if key.startswith("co") else 0) | (2 if key.startswith("ti") else 0)
        bb = font.bbox
        newfd = Dictionary(
            Type=Name.FontDescriptor, FontName=Name("/" + name.replace(" ", "")),
            Flags=flags,
            FontBBox=Array([int(bb.x0 * 1000), int(bb.y0 * 1000), int(bb.x1 * 1000), int(bb.y1 * 1000)]),
            ItalicAngle=-12 if ital else 0,
            Ascent=int(font.ascender * 1000) if font.ascender < 1.2 else 718,
            Descent=int(font.descender * 1000) if font.descender > -0.5 else -207,
            CapHeight=718 if key.startswith("he") else (662 if key.startswith("ti") else 562),
            StemV=140 if bold else 88,
            FontFile3=ff)
        if isinstance(fd, Dictionary):
            for k in ("/Ascent", "/Descent", "/CapHeight", "/StemV", "/XHeight"):
                if k in fd:
                    newfd[k] = fd[k]
        # existing /Widths must agree with the embedded program (7.21.5)
        if "/Widths" in f and not symbolic:
            names, _ = font_encoding(f)
            first = int(num(f.get("/FirstChar"), 0))
            off = 0
            for k, w in enumerate(f.Widths):
                code = first + k
                if not 0 <= code < 256 or names[code] == ".notdef":
                    continue
                u = glyph_unicode(names[code])
                if not u:
                    continue
                try:
                    if abs(num(w) - font.glyph_advance(ord(u[0])) * 1000) > 1.5:
                        off += 1
                except Exception:
                    pass
            if off:
                left.append(f"{name} (embedded, but {off} declared glyph width(s) differ from the "
                            f"substitute font — veraPDF 7.21.5 may flag it; embed the original font at source)")
        f.FontDescriptor = pdf.make_indirect(newfd)
        if sub == Name.TrueType:
            f.Subtype = Name.Type1
        if "/Widths" not in f:
            if symbolic:
                codes = range(32, 256)
                widths = []
                for c in codes:
                    try:
                        widths.append(round(font.glyph_advance(c) * 1000))
                    except Exception:
                        widths.append(0)
            else:
                names, _ = font_encoding(f)
                codes = range(32, 256)
                widths = []
                for c in codes:
                    u = glyph_unicode(names[c]) if names[c] != ".notdef" else ""
                    try:
                        widths.append(round(font.glyph_advance(ord(u[0])) * 1000) if u else 0)
                    except Exception:
                        widths.append(0)
                if "/Encoding" not in f:
                    f.Encoding = Name.WinAnsiEncoding if not symbolic else f.get("/Encoding")
            f.FirstChar = 32
            f.LastChar = 255
            f.Widths = Array(widths)
        embedded += 1
    return embedded, sorted(set(left))


# ------------------------------------------------------------ ToUnicode
def _cmap_stream(pairs: dict[int, str], two_byte: bool) -> bytes:
    width = 4 if two_byte else 2
    lines = ["/CIDInit /ProcSet findresource begin", "12 dict begin", "begincmap",
             "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
             "/CMapName /Adobe-Identity-UCS def", "/CMapType 2 def",
             "1 begincodespacerange",
             "<0000> <FFFF>" if two_byte else "<00> <FF>", "endcodespacerange"]
    items = sorted(pairs.items())
    for i in range(0, len(items), 100):
        chunk = items[i:i + 100]
        lines.append(f"{len(chunk)} beginbfchar")
        for code, text in chunk:
            u16 = text.encode("utf-16-be").hex().upper()
            lines.append(f"<{code:0{width}X}> <{u16}>")
        lines.append("endbfchar")
    lines += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return "\n".join(lines).encode("ascii")


def _usable(u: str) -> bool:
    if not u:
        return False
    cp = ord(u[0])
    return not (0xE000 <= cp <= 0xF8FF or cp == 0xFFFD or cp < 0x20)


def _tt_font(fd: Dictionary):
    try:
        from fontTools.ttLib import TTFont
        ff = fd.get("/FontFile2")
        if not isinstance(ff, pikepdf.Stream):
            return None
        return TTFont(io.BytesIO(ff.read_bytes()), lazy=True, fontNumber=0)
    except Exception:
        return None


def repair_tounicode(pdf: pikepdf.Pdf) -> tuple[int, list[str]]:
    fixed = 0
    unresolved: list[str] = []
    for f in iter_font_dicts(pdf):
        if "/ToUnicode" in f:
            continue
        sub = f.get("/Subtype")
        name = base_name(f.get("/BaseFont"))
        try:
            if sub == Name.Type0:
                enc = f.get("/Encoding")
                if not (isinstance(enc, Name) and str(enc) in ("/Identity-H", "/Identity-V")):
                    continue
                df = (f.get("/DescendantFonts") or [None])[0]
                if not isinstance(df, Dictionary) or df.get("/Subtype") != Name.CIDFontType2:
                    unresolved.append(name)
                    continue
                tt = _tt_font(df.get("/FontDescriptor") or Dictionary())
                if tt is None:
                    unresolved.append(name)
                    continue
                order = tt.getGlyphOrder()
                gid_uni: dict[int, str] = {}
                try:
                    best = tt.getBestCmap() or {}
                    name_to_gid = {n: i for i, n in enumerate(order)}
                    for cp, gname in best.items():
                        gid = name_to_gid.get(gname)
                        if gid is not None and gid not in gid_uni:
                            gid_uni[gid] = chr(cp)
                except Exception:
                    pass
                if not gid_uni:
                    for gid, gname in enumerate(order):
                        u = glyph_unicode(gname)
                        if _usable(u):
                            gid_uni[gid] = u
                c2g = df.get("/CIDToGIDMap")
                if isinstance(c2g, pikepdf.Stream):
                    raw = c2g.read_bytes()
                    gid_cid = {}
                    for cid in range(len(raw) // 2):
                        g = int.from_bytes(raw[2 * cid:2 * cid + 2], "big")
                        if g and g not in gid_cid:
                            gid_cid[g] = cid
                    pairs = {gid_cid[g]: u for g, u in gid_uni.items() if g in gid_cid and _usable(u)}
                else:
                    pairs = {g: u for g, u in gid_uni.items() if _usable(u)}
                if pairs:
                    f.ToUnicode = pdf.make_stream(_cmap_stream(pairs, True))
                    fixed += 1
                else:
                    unresolved.append(name)
            elif sub in (Name.Type1, Name.TrueType, Name.MMType1, Name.Type3):
                names, explicit = font_encoding(f)
                fd = f.get("/FontDescriptor")
                flags = int(num(fd.get("/Flags"), 0)) if isinstance(fd, Dictionary) else 0
                symbolic = bool(flags & 4) and not bool(flags & 32)
                first = int(num(f.get("/FirstChar"), 0))
                last = int(num(f.get("/LastChar"), 255))
                if explicit and not symbolic:
                    # standard encodings / AGL Differences are already mappable
                    if all(glyph_unicode(names[c]) or names[c] == ".notdef"
                           for c in range(max(0, first), min(255, last) + 1)):
                        continue
                pairs: dict[int, str] = {}
                if sub == Name.TrueType and isinstance(fd, Dictionary) and not explicit:
                    tt = _tt_font(fd)
                    if tt is not None:
                        order = tt.getGlyphOrder()
                        cmap = None
                        for rec in tt["cmap"].tables:
                            if (rec.platformID, rec.platEncID) in ((3, 0), (1, 0)):
                                cmap = rec.cmap
                                break
                        uni_rev = {}
                        try:
                            for cp, g in (tt.getBestCmap() or {}).items():
                                uni_rev.setdefault(g, chr(cp))
                        except Exception:
                            pass
                        for code in range(max(0, first), min(255, last) + 1):
                            g = None
                            if cmap:
                                g = cmap.get(code) or cmap.get(0xF000 + code)
                            if g is None:
                                continue
                            u = uni_rev.get(g) or glyph_unicode(g)
                            if _usable(u):
                                pairs[code] = u
                else:
                    for code in range(max(0, first), min(255, last) + 1):
                        u = glyph_unicode(names[code]) if names[code] != ".notdef" else ""
                        if _usable(u):
                            pairs[code] = u
                if pairs:
                    f.ToUnicode = pdf.make_stream(_cmap_stream(pairs, False))
                    fixed += 1
                elif symbolic or not explicit:
                    unresolved.append(name)
        except Exception:
            unresolved.append(name)
    return fixed, sorted(set(unresolved))


# ================================================================== OCR
def tesseract_info() -> dict:
    exe = shutil.which("tesseract")
    if not exe:
        return {"available": False}
    try:
        r = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, timeout=20)
        text = r.stdout + r.stderr
        m = re.search(r'"(.+?)"', text)
        tessdata = m.group(1) if m else os.environ.get("TESSDATA_PREFIX", "")
        langs = [l.strip() for l in text.splitlines()[1:] if l.strip() and " " not in l.strip()]
        return {"available": True, "exe": exe, "tessdata": tessdata, "langs": langs}
    except Exception as exc:
        return {"available": False, "error": str(exc)}


UNICODE_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "C:/Windows/Fonts/Nirmala.ttf", "C:/Windows/Fonts/arialuni.ttf",
    "C:/Windows/Fonts/latha.ttf", "C:/Windows/Fonts/arial.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
]


def _find_font_for(text: str) -> str | None:
    if all(ord(c) < 256 for c in text):
        return None
    tamil = any(0x0B80 <= ord(c) <= 0x0BFF for c in text)
    cands = []
    if tamil:
        for root in ("/usr/share/fonts", "C:/Windows/Fonts"):
            if os.path.isdir(root):
                for dp, _, fs in os.walk(root):
                    for fn in fs:
                        if "tamil" in fn.lower() or fn.lower() in ("latha.ttf", "nirmala.ttf"):
                            cands.append(os.path.join(dp, fn))
    cands += UNICODE_FONT_CANDIDATES
    for c in cands:
        if os.path.exists(c):
            return c
    return None


def image_only_pages(doc: pymupdf.Document, force: bool) -> list[int]:
    out = []
    for i, page in enumerate(doc):
        if page.get_text("text").strip():
            continue
        if force:
            out.append(i)
            continue
        cover = 0.0
        pa = page.rect.width * page.rect.height or 1
        for im in page.get_image_info():
            r = pymupdf.Rect(im["bbox"]) & page.rect
            cover += r.width * r.height
        if cover / pa >= 0.3:
            out.append(i)
    return out


def ocr_pages(path_in: str, path_out: str, opts: Options) -> dict:
    info = tesseract_info()
    doc = pymupdf.open(path_in)
    pages = image_only_pages(doc, opts.ocr == "force")
    result = {"pages": [], "words": 0, "skipped_words": 0, "available": info.get("available", False)}
    if not pages or not info.get("available"):
        doc.close()
        return result
    fonts_loaded: dict[str, str] = {}
    for pno in pages:
        page = doc[pno]
        try:
            tp = page.get_textpage_ocr(flags=0, language=opts.ocr_lang, dpi=int(opts.ocr_dpi),
                                       full=True, tessdata=info.get("tessdata") or None)
            words = page.get_text("words", textpage=tp)
        except Exception as exc:
            result.setdefault("errors", []).append(f"page {pno + 1}: {exc}")
            continue
        for w in words:
            x0, y0, x1, y1, text = w[:5]
            if not text.strip() or x1 - x0 < 1 or y1 - y0 < 1:
                continue
            fontfile = _find_font_for(text)
            fontname = "helv"
            if fontfile:
                fontname = fonts_loaded.get(fontfile)
                if fontname is None:
                    fontname = f"OCR{len(fonts_loaded)}"
                    fonts_loaded[fontfile] = fontname
            elif any(ord(c) > 255 for c in text):
                result["skipped_words"] += 1
                continue
            try:
                fnt = pymupdf.Font(fontfile=fontfile) if fontfile else pymupdf.Font("helv")
                h = y1 - y0
                fs = max(2.0, h / max(0.5, fnt.ascender - fnt.descender))
                baseline = y1 + fnt.descender * fs
                tl = fnt.text_length(text, fontsize=fs) or 1
                sx = (x1 - x0) / tl
                page.insert_text(pymupdf.Point(x0, baseline), text, fontsize=fs,
                                 fontname=fontname, fontfile=fontfile, render_mode=3,
                                 morph=(pymupdf.Point(x0, baseline), pymupdf.Matrix(sx, 1)))
                result["words"] += 1
            except Exception:
                result["skipped_words"] += 1
        result["pages"].append(pno)
    doc.save(path_out, garbage=0, deflate=True)
    doc.close()
    return result


# ================================================================= main
def preflight(src: str, dst: str, opts: Options, import_mode: bool = False) -> dict:
    log: list[str] = []
    warnings: list[str] = []
    info: dict = {"log": log, "warnings": warnings}

    try:
        pdf = pikepdf.open(src)
    except pikepdf.PasswordError:
        raise RuntimeError("The PDF is password protected. Remove the open password first.")
    if pdf.is_encrypted:
        log.append("encryption removed from the working copy")

    # annotations must be indirect objects for OBJR
    made = 0
    for page in pdf.pages:
        annots = page.obj.get("/Annots")
        if isinstance(annots, Array):
            for i in range(len(annots)):
                a = annots[i]
                if isinstance(a, Dictionary) and not a.is_indirect:
                    annots[i] = pdf.make_indirect(a)
                    made += 1
            if not annots.is_indirect:
                pass
    if made:
        log.append(f"{made} annotation(s) made indirect")

    if opts.flatten_forms:
        ff = FormFlattener(pdf, import_mode)
        ff.run()
        if ff.count:
            log.append(f"{ff.count} text-bearing form XObject(s) unpacked into page content")
        for why, n in ff.skipped.items():
            warnings.append(f"{n} form XObject(s) left packed ({why}); their text is tagged as one unit")
        info["forms_unpacked"] = ff.count

    if opts.embed_fonts:
        n, left = embed_standard_fonts(pdf)
        if n:
            log.append(f"{n} non-embedded standard font(s) embedded (URW metric-compatible)")
        if left:
            warnings.append("Fonts that could not be embedded here (fix in the source file): "
                            + ", ".join(left[:12]))
        info["fonts_left"] = left

    if opts.repair_tounicode:
        # bullets and signs set in Wingdings / Symbol / ZapfDingbats (V4.19.4)
        from .symbolfonts import repair as repair_symbols
        ns, sym_names = repair_symbols(pdf)
        if ns:
            log.append(f"symbol font(s) given their real characters (bullets, ticks, arrows): "
                       + ", ".join(sym_names[:8]))
        info["symbol_fonts"] = sym_names
        n, unresolved = repair_tounicode(pdf)
        if n:
            log.append(f"ToUnicode map added to {n} font(s)")
        if unresolved:
            warnings.append("No reliable Unicode mapping could be derived for: "
                            + ", ".join(unresolved[:12]))

    tmp = dst + ".pre.pdf"
    pdf.save(tmp)
    pdf.close()

    if opts.ocr != "off":
        try:
            r = ocr_pages(tmp, dst, opts)
        except Exception as exc:
            r = {"pages": [], "error": str(exc), "available": True}
        info["ocr"] = r
        if r.get("pages"):
            log.append(f"OCR text layer added to {len(r['pages'])} page(s), {r['words']} words")
            os.remove(tmp)
            return info
        if not r.get("available"):
            doc = pymupdf.open(tmp)
            scanned = image_only_pages(doc, False)
            doc.close()
            if scanned:
                warnings.append(f"{len(scanned)} page(s) look scanned but Tesseract is not installed")
        if r.get("error"):
            warnings.append(f"OCR failed: {r['error']}")
    os.replace(tmp, dst)
    return info
