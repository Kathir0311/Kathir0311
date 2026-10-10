"""Colour contrast (WCAG 2.x SC 1.4.3 / 1.4.6): detection and optional correction.

Detection
  Each page is rendered twice: as-is, and from a copy where every text object
  is drawn in render mode 3 (invisible). The second render is the exact
  background behind every glyph — flat fills, gradients, photos. Pixels that
  differ between the two renders are the glyph pixels; comparing the declared
  text colour with the background at those pixels gives the contrast ratio.
  The 10th-percentile ratio is used, so text that is unreadable over part of
  a photo is still caught.

Correction (opt-in, per issue)
  The text colour is moved in lightness only — same hue and saturation — to
  the nearest value that reaches the required ratio (+ a small margin). The
  new colour is written in the same colour space (Gray / RGB / CMYK / ICC),
  set just before the affected text operator and restored right after it, so
  nothing else on the page changes. Spot colours, patterns and text inside
  packed form XObjects are reported, not changed.
"""
from __future__ import annotations

import colorsys
import os
import tempfile
from typing import Callable

import numpy as np
import pikepdf
import pymupdf
from pikepdf import Array, Dictionary, Name, Operator
from pikepdf import ContentStreamInstruction as CSI

from .pdftext import SHOW_OPS, Interpreter, num, page_box, page_resources
from .textflags import DICT_FLAGS


# ------------------------------------------------------------ colour maths
def _lin(c):
    c = np.asarray(c, dtype=np.float64) / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def luminance(rgb) -> np.ndarray | float:
    c = _lin(rgb)
    return c[..., 0] * 0.2126 + c[..., 1] * 0.7152 + c[..., 2] * 0.0722


def ratio(l1, l2):
    hi = np.maximum(l1, l2)
    lo = np.minimum(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def hexc(rgb) -> str:
    r, g, b = (int(round(max(0, min(255, v)))) for v in rgb)
    return f"#{r:02X}{g:02X}{b:02X}"


def from_hex(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def required_ratio(size: float, bold: bool, level: str = "AA",
                   large_rule: str = "pac") -> tuple[float, bool]:
    """large_rule: 'wcag' = 18pt, or 14pt bold · 'pac' = 18pt only (PAC 2024 does
    not credit bold that it cannot read from the font, so 14pt bold text is
    checked at 4.5:1 — using the same rule keeps our result equal to PAC's)."""
    large = size >= 18 or (large_rule == "wcag" and bold and size >= 14)
    if level == "AAA":
        return (4.5 if large else 7.0), large
    return (3.0 if large else 4.5), large


def suggest(fg: tuple, bg_lums: list[float], need: float,
            only: int = 0) -> tuple[int, int, int] | None:
    """Nearest colour (same hue/saturation, lightness moved) that meets `need`
    against every background luminance given (darkest and lightest parts).
    only: 0 = either way · -1 = only darker · +1 = only lighter."""
    target = need + 0.15
    h, l, s = colorsys.rgb_to_hls(*(v / 255 for v in fg))

    def ok(rgb, t):
        lt = float(luminance(np.array(rgb, dtype=float)))
        return all(ratio(lt, b) >= t for b in bg_lums)

    best = None
    for direction in ((-1, 1) if not only else (only,)):
        for k in range(1, 201):
            ll = l + direction * k / 200
            if not 0 <= ll <= 1:
                break
            rgb = tuple(v * 255 for v in colorsys.hls_to_rgb(h, ll, s))
            if ok(rgb, target):
                if best is None or k < best[0]:
                    best = (k, tuple(int(round(v)) for v in rgb))
                break
    if best:
        return best[1]
    for alt in ((0, 0, 0), (255, 255, 255)):
        if only and (sum(alt) > sum(fg)) != (only > 0):
            continue
        if ok(alt, need):
            return alt
    return None


# ------------------------------------------------------------ fix methods
METHODS = ("auto", "background", "text", "stroke")
# CMYK outlines use rich black 100/87/0/100 (the team's PitStop swatch): K-only black
# renders as dark grey (35,31,32) under colour management and misses 4.5:1 on mid
# tones, rich black renders (0,0,5). A little margin for ICC differences.
RICH_BLACK = [1, 0.87, 0, 1]
BLACK_LUM, WHITE_LUM = 0.002, 1.0


def stroke_colour(i: dict, choice: str = "black") -> str:
    """'black' | 'white' | 'auto' (whichever outline gives more contrast)."""
    if choice in ("black", "white"):
        return choice
    o = i.get("outline") or {}
    return "white" if o.get("white", 0) > o.get("black", 0) else "black"


def fix_method(i: dict, method: str = "auto") -> str:
    """How issue `i` is fixed with the chosen method: background | text |
    stroke, or '' when that method cannot fix it."""
    if i.get("zone") == "figure":
        return ""
    if method == "background":
        return "background" if i.get("suggested_bg") else ""
    if method == "text":
        return "text" if i.get("suggested") else ""
    if method == "stroke":
        return "stroke"
    p = i.get("prefer")
    if p is None:                                   # analysed by an older version
        p = "background" if i.get("suggested_bg") else ("text" if i.get("suggested") else "")
    return p


# --------------------------------------------------- invisible-text copy
def _hide_text(ins: list) -> list:
    out = []
    for i in ins:
        op = str(i.operator)
        if op == "Tr":
            out.append(CSI([3], Operator("Tr")))
            continue
        out.append(i)
        if op == "BT":
            out.append(CSI([3], Operator("Tr")))
    return out


def background_copy(src: str, dst: str) -> None:
    pdf = pikepdf.open(src)
    seen = set()

    def forms(res, depth=0):
        if depth > 8 or not isinstance(res, Dictionary):
            return
        xo = res.get("/XObject")
        if not isinstance(xo, Dictionary):
            return
        for _, x in xo.items():
            if isinstance(x, pikepdf.Stream) and x.get("/Subtype") == Name.Form and x.objgen not in seen:
                seen.add(x.objgen)
                try:
                    x.write(pikepdf.unparse_content_stream(_hide_text(list(pikepdf.parse_content_stream(x)))))
                except Exception:
                    pass
                forms(x.get("/Resources"), depth + 1)

    for page in pdf.pages:
        try:
            ins = list(pikepdf.parse_content_stream(page))
            page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(_hide_text(ins)))
        except Exception:
            continue
        forms(page_resources(page.obj))
    pdf.save(dst)
    pdf.close()


# ---------------------------------------------------------------- analyse
def _pix_array(page: pymupdf.Page, zoom: float) -> np.ndarray:
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False, colorspace=pymupdf.csRGB)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]


def _zone_rects(model, page_no: int) -> tuple[list, list]:
    arts, figs = [], []
    if model is None:
        return arts, figs
    art_anc = set()
    for n in model.ordered_nodes():
        if n.artifact or (n.parent in art_anc):
            art_anc.add(n.id)
    for n in model.nodes.values():
        for r in n.rects_on(page_no):
            if n.id in art_anc:
                arts.append(r)
            elif n.role in ("Figure", "Formula"):
                figs.append(r)
    return arts, figs


def _inside(pt, rects, pad=1.0) -> bool:
    x, y = pt
    return any(r[0] - pad <= x <= r[2] + pad and r[1] - pad <= y <= r[3] + pad for r in rects)


def _stroke_spans(page: pymupdf.Page) -> list[tuple]:
    """Outlined (stroked) text on the page: [(rect, rgb 0-255, line width)]."""
    out = []
    try:
        trace = page.get_texttrace()
    except Exception:
        return out
    for sp in trace:
        if sp.get("type") not in (1, 4) or not sp.get("linewidth"):
            continue
        if float(sp.get("opacity", 1.0) or 0) < 0.5:
            continue
        col = [float(v) for v in (sp.get("color") or ())]
        if len(col) == 1:
            rgb = (col[0] * 255,) * 3
        elif len(col) == 3:
            rgb = tuple(v * 255 for v in col)
        elif len(col) == 4:
            rgb = _mupdf_rgb(col, 4)
        else:
            continue
        out.append((pymupdf.Rect(sp["bbox"]), tuple(float(v) for v in rgb), float(sp["linewidth"])))
    return out


def _outline_of(bbox, strokes: list[tuple]):
    r = pymupdf.Rect(bbox)
    area = max(1e-6, r.get_area())
    for sr, rgb, lw in strokes:
        ov = r & sr
        if not ov.is_empty and ov.get_area() >= 0.5 * min(area, max(1e-6, sr.get_area())):
            return rgb, lw
    return None


def _contrast_chunk(path, bg_path, level, zoom, pages, model_json, large_rule) -> dict:
    """Worker process: measure some pages against a shared background copy."""
    from .model import DocModel
    model = DocModel.from_json(model_json) if model_json else None
    return analyze(path, level=level, zoom=zoom, pages=pages, model=model,
                   large_rule=large_rule, _bg_path=bg_path)


def analyze(path: str, *, level: str = "AA", zoom: float = 2.0, pages: list[int] | None = None,
            model=None, progress: Callable | None = None, large_rule: str = "pac",
            _bg_path: str | None = None) -> dict:
    progress = progress or (lambda f, m: None)
    if _bg_path is None:
        # big files: measure page chunks on several CPU cores
        from .parallel import MIN_PAGES, chunks, page_procs, run_chunks
        try:
            with pymupdf.open(path) as d0:
                total = d0.page_count
        except Exception:
            total = 0
        todo_all = pages if pages is not None else list(range(total))
        procs = page_procs()
        if procs > 1 and len(todo_all) >= MIN_PAGES:
            fd, bg = tempfile.mkstemp(suffix=".pdf")
            os.close(fd)
            try:
                background_copy(path, bg)
                mj = model.to_json() if model is not None else None
                parts = chunks(list(todo_all), procs)
                progress(0.02, f"contrast: {len(todo_all)} pages on {procs} CPU cores")
                res = run_chunks(_contrast_chunk,
                                 [(path, bg, level, zoom, p, mj, large_rule) for p in parts], procs,
                                 lambda d, t: progress(d / t, f"contrast: {d} of {t} page groups"))
                progress(1.0, "contrast done")
                return {"level": level, "large_rule": large_rule,
                        "checked": sum(r["checked"] for r in res), "pages": len(todo_all),
                        "issues": [i for r in res for i in r["issues"]]}
            except Exception:
                pass                          # fall back to one process below
            finally:
                try:
                    os.remove(bg)
                except OSError:
                    pass
    if _bg_path is None:
        fd, bg_path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        background_copy(path, bg_path)
    else:
        bg_path = _bg_path
    try:
        full = pymupdf.open(path)
        bgd = pymupdf.open(bg_path)
        todo = pages if pages is not None else list(range(full.page_count))
        issues = []
        checked = 0
        for k, pno in enumerate(todo):
            if k % 5 == 0:
                progress(k / max(1, len(todo)), f"contrast page {pno + 1}")
            page, bpage = full[pno], bgd[pno]
            F = _pix_array(page, zoom)
            B = _pix_array(bpage, zoom)
            if F.shape != B.shape:
                continue
            to_px = page.rotation_matrix * pymupdf.Matrix(zoom, zoom)
            to_pdf = ~page.transformation_matrix
            arts, figs = _zone_rects(model, pno)
            strokes = _stroke_spans(page)
            groups: dict[tuple, dict] = {}
            for bi, blk in enumerate(page.get_text("dict", flags=DICT_FLAGS).get("blocks", [])):
                if blk.get("type") != 0:
                    continue
                for li, line in enumerate(blk.get("lines", [])):
                    for span in line.get("spans", []):
                        text = (span.get("text") or "").strip()
                        if not text:
                            continue
                        r = pymupdf.Rect(span["bbox"]) * to_px
                        x0, y0 = max(0, int(r.x0) - 1), max(0, int(r.y0) - 1)
                        x1, y1 = min(F.shape[1], int(r.x1) + 2), min(F.shape[0], int(r.y1) + 2)
                        if x1 - x0 < 2 or y1 - y0 < 2:
                            continue
                        Fs = F[y0:y1, x0:x1].astype(np.int16)
                        Bs = B[y0:y1, x0:x1].astype(np.int16)
                        diff = np.abs(Fs - Bs).sum(axis=2)
                        mask = diff > 36
                        if int(mask.sum()) < 4:
                            continue                       # invisible (OCR layer, white-on-white)
                        checked += 1
                        bgpix = Bs[mask].astype(np.float64)
                        Lb = luminance(bgpix)
                        c = int(span.get("color", 0))
                        fg = np.array([(c >> 16) & 255, (c >> 8) & 255, c & 255], dtype=np.float64)
                        alpha = float(span.get("alpha", 255)) / 255.0
                        size = float(span.get("size", 10))
                        # declared colour vs what was actually painted (stroke-only, patterns …)
                        source = "declared"
                        if size * zoom >= 18:
                            d = diff[mask]
                            core = Fs[mask][d >= np.percentile(d, 70)].astype(np.float64)
                            if len(core):
                                painted = np.median(core, axis=0)
                                if abs(float(luminance(painted)) - float(luminance(fg))) > 0.3:
                                    fg, source = painted, "rendered"
                        if alpha < 0.999:
                            fgp = alpha * fg + (1 - alpha) * bgpix
                            Lt = luminance(fgp)
                        else:
                            Lt = np.full(Lb.shape, float(luminance(fg)))
                        rs = ratio(Lt, Lb)
                        # outlined text (Tr 1/2): a thin outline is part of the letter,
                        # so each glyph pixel counts with the better of fill and outline
                        outline = ""
                        st = _outline_of(span["bbox"], strokes) if strokes else None
                        if st is not None:
                            srgb = np.array(st[0], dtype=np.float64)
                            near = np.sqrt(((Fs[mask].astype(np.float64) - srgb) ** 2).sum(axis=1)) < 60
                            if float(near.mean()) >= 0.08:            # the outline is really visible
                                rs = np.maximum(rs, ratio(float(luminance(srgb)), Lb))
                                outline = hexc(srgb)
                        worst = float(np.percentile(rs, 10))
                        ob = float(np.percentile(np.maximum(rs, ratio(BLACK_LUM, Lb)), 10))
                        ow = float(np.percentile(np.maximum(rs, ratio(WHITE_LUM, Lb)), 10))
                        med = float(np.median(rs))
                        bold = bool(span.get("flags", 0) & 16) or "bold" in (span.get("font") or "").lower()
                        need, large = required_ratio(size, bold, level, large_rule)
                        if worst >= need - 0.005:
                            continue
                        bg_lo, bg_hi = float(np.percentile(Lb, 5)), float(np.percentile(Lb, 95))
                        pdf_box = pymupdf.Rect(span["bbox"]) * to_pdf
                        pdf_box.normalize()
                        cx, cy = (pdf_box.x0 + pdf_box.x1) / 2, (pdf_box.y0 + pdf_box.y1) / 2
                        zone = "figure" if _inside((cx, cy), figs) else ("artifact" if _inside((cx, cy), arts) else "content")
                        key = (bi, li, hexc(fg), round(need, 1))
                        g = groups.get(key)
                        box = [round(pdf_box.x0, 2), round(pdf_box.y0, 2), round(pdf_box.x1, 2), round(pdf_box.y1, 2)]
                        if g is None:
                            groups[key] = {
                                "page": pno, "bbox": box, "text": text, "size": round(size, 1),
                                "bold": bold, "large": large, "fg": hexc(fg), "fg_source": source,
                                "alpha": round(alpha, 2),
                                "bg": hexc(np.median(bgpix, axis=0)), "ratio": round(worst, 2),
                                "ratio_median": round(med, 2), "required": need,
                                "uniform": float(Lb.std()) < 0.03, "zone": zone,
                                "has_outline": outline,
                                "_lo": bg_lo, "_hi": bg_hi, "_fg": tuple(fg), "_ob": ob, "_ow": ow,
                                "_bg": tuple(np.median(bgpix, axis=0)),
                            }
                        else:
                            g["bbox"] = [min(g["bbox"][0], box[0]), min(g["bbox"][1], box[1]),
                                         max(g["bbox"][2], box[2]), max(g["bbox"][3], box[3])]
                            g["text"] = (g["text"] + " " + text)[:300]
                            g["ratio"] = min(g["ratio"], round(worst, 2))
                            g["uniform"] = g["uniform"] and float(Lb.std()) < 0.03
                            g["_ob"] = min(g["_ob"], ob)
                            g["_ow"] = min(g["_ow"], ow)
                            g["_lo"] = min(g["_lo"], bg_lo)
                            g["_hi"] = max(g["_hi"], bg_hi)
            for g in groups.values():
                lo, hi = g.pop("_lo"), g.pop("_hi")
                g["bg_lum"] = [round(lo, 5), round(hi, 5)]
                fg_rgb, bg_rgb = g.pop("_fg"), g.pop("_bg")
                sug = suggest(fg_rgb, [lo, hi] if hi - lo > 0.01 else [lo], g["required"])
                g["suggested"] = hexc(sug) if sug else ""
                # solid box behind the text (badges, banners, shaded cells): the box
                # colour can move instead of the text — darker behind light text,
                # lighter behind dark text; the text keeps its colour
                g["suggested_bg"] = ""
                fl = float(luminance(np.array(fg_rgb, dtype=float)))
                light_text = fl > hi
                if g["uniform"] and (fl > hi or fl < lo):
                    sb = suggest(bg_rgb, [fl], g["required"], only=-1 if light_text else 1)
                    if sb is not None:
                        g["suggested_bg"] = hexc(sb)
                        g["fg_lum"] = round(fl, 5)
                # a thin outline (stroke) around the letters, like PitStop Fill & Stroke:
                # predicted ratio with a black / a white outline (same pixels as above)
                ob, ow = g.pop("_ob"), g.pop("_ow")
                g["outline"] = {"black": round(ob, 2), "white": round(ow, 2)}
                outline_ok = max(ob, ow) >= g["required"]
                if g["suggested_bg"] and light_text:
                    g["prefer"] = "background"
                elif sug and g["suggested_bg"]:
                    # dark text on a box: the smaller change wins; never flip the text
                    # from darker-than-box to lighter-than-box when the box can move
                    lt = float(luminance(np.array(sug, dtype=float)))
                    flips = lt > hi
                    d_text = abs(colorsys.rgb_to_hls(*(v / 255 for v in sug))[1] -
                                 colorsys.rgb_to_hls(*(v / 255 for v in fg_rgb))[1])
                    sbg = from_hex(g["suggested_bg"])
                    d_box = abs(colorsys.rgb_to_hls(*(v / 255 for v in sbg))[1] -
                                colorsys.rgb_to_hls(*(v / 255 for v in bg_rgb))[1])
                    g["prefer"] = "background" if flips or d_box < d_text else "text"
                elif sug:
                    g["prefer"] = "text"
                elif g["suggested_bg"]:
                    g["prefer"] = "background"
                elif outline_ok:
                    g["prefer"] = "stroke"
                else:
                    g["prefer"] = ""
                if not sug and not g["suggested_bg"]:
                    g["note"] = ("background varies too much for one colour to work — an outline "
                                 "(stroke) around the text fixes it" if outline_ok else
                                 "background varies too much for a colour change to work — "
                                 "fix in the design (scrim, outline or a solid box behind the text)")
                g["id"] = f"c{pno}_{len(issues)}"
                g["apply"] = False
                g["status"] = "open"
                # PAC measures artifacts (page numbers, running heads) too
                g["severity"] = ("error" if g["zone"] in ("content", "artifact") and g["uniform"] else
                                 "warning" if g["zone"] in ("content", "artifact") else "info")
                issues.append(g)
        full.close()
        bgd.close()
        progress(1.0, "contrast done")
        return {"level": level, "large_rule": large_rule, "checked": checked, "pages": len(todo),
                "issues": issues}
    finally:
        if _bg_path is None:
            try:
                os.remove(bg_path)
            except OSError:
                pass


def summarize(result: dict) -> dict:
    iss = result.get("issues", [])
    open_ = [i for i in iss if i.get("status") != "fixed"]
    return {"level": result.get("level", "AA"), "checked": result.get("checked", 0),
            "issues": len(iss), "open": len(open_),
            "errors": sum(1 for i in open_ if i["severity"] == "error"),
            "fixed": sum(1 for i in iss if i.get("status") == "fixed"),
            "pages": sorted({i["page"] for i in open_})}


# ----------------------------------------------------------------- correct
def _rgb_to_space(rgb: tuple, space: str, n: int) -> list[float]:
    r, g, b = (v / 255 for v in rgb)
    if space == "gray" or n == 1:
        lum = float(luminance(np.array(rgb, dtype=float)))
        v = 12.92 * lum if lum <= 0.0031308 else 1.055 * lum ** (1 / 2.4) - 0.055
        return [round(max(0.0, min(1.0, v)), 4)]
    if space == "cmyk" or n == 4:
        k = 1 - max(r, g, b)
        if k >= 1:
            return [0.0, 0.0, 0.0, 1.0]
        return [round((1 - r - k) / (1 - k), 4), round((1 - g - k) / (1 - k), 4),
                round((1 - b - k) / (1 - k), 4), round(k, 4)]
    return [round(r, 4), round(g, 4), round(b, 4)]


def _mupdf_rgb(comps: list[float], n: int) -> tuple[int, int, int]:
    """Colour as MuPDF renders it (uses its colour management, like the page render)."""
    cs = {1: pymupdf.csGRAY, 3: pymupdf.csRGB, 4: pymupdf.csCMYK}[n]
    pix = pymupdf.Pixmap(cs, pymupdf.IRect(0, 0, 1, 1), False)
    pix.set_pixel(0, 0, tuple(int(round(max(0.0, min(1.0, v)) * 255)) for v in comps))
    return tuple(pymupdf.Pixmap(pymupdf.csRGB, pix).pixel(0, 0)[:3])


def _search_native(comps: list[float], n: int, bg_lums: list[float], need: float,
                   darker: bool) -> list[float] | None:
    """Smallest move in the native colour space that reaches `need` (+margin)."""
    target = need + 0.15

    def move(t):
        if n == 4:
            c, m, y, k = comps
            return [c, m, y, k + t * (1 - k)] if darker else [c * (1 - t), m * (1 - t), y * (1 - t), k * (1 - t)]
        if darker:
            return [v * (1 - t) for v in comps]
        return [v + t * (1 - v) for v in comps]

    def good(t):
        lt = float(luminance(np.array(_mupdf_rgb(move(t), n), dtype=float)))
        return all(ratio(lt, b) >= target for b in bg_lums)

    if not good(1.0):
        return None
    lo, hi = 0.0, 1.0
    for _ in range(18):
        mid = (lo + hi) / 2
        if good(mid):
            hi = mid
        else:
            lo = mid
    return [round(v, 4) for v in move(hi)]


def _comps_to_rgb(comps: list[float], n: int) -> tuple[float, float, float]:
    if n == 1:
        return (comps[0] * 255,) * 3
    if n == 4:
        c, m, y, k = comps
        return ((1 - c) * (1 - k) * 255, (1 - m) * (1 - k) * 255, (1 - y) * (1 - k) * 255)
    return tuple(v * 255 for v in comps[:3])


def _cs_kind(res: Dictionary, name: str) -> tuple[str, int]:
    """-> (kind, n) where kind is gray|rgb|cmyk|icc or '' when not fixable."""
    dev = {"/DeviceGray": ("gray", 1), "/DeviceRGB": ("rgb", 3), "/DeviceCMYK": ("cmyk", 4),
           "/G": ("gray", 1), "/RGB": ("rgb", 3), "/CMYK": ("cmyk", 4)}
    if name in dev:
        return dev[name]
    try:
        cs = res.get("/ColorSpace").get(name)
    except Exception:
        return "", 0
    if isinstance(cs, Name) and str(cs) in dev:
        return dev[str(cs)]
    if isinstance(cs, Array) and len(cs):
        fam = str(cs[0])
        if fam == "/ICCBased":
            try:
                n = int(num(cs[1].get("/N"), 0))
                return ("icc", n) if n in (1, 3, 4) else ("", 0)
            except Exception:
                return "", 0
        if fam == "/CalRGB":
            return "icc", 3
        if fam == "/CalGray":
            return "icc", 1
    return "", 0


FILL_PAINT = {"f", "F", "f*", "B", "B*", "b", "b*"}


def _apply_bg_fixes(pdf: pikepdf.Pdf, issues: list[dict]) -> set:
    """Darken/lighten the solid fill drawn behind the text (badge, banner)."""
    done: set = set()
    by_page: dict[int, list[dict]] = {}
    for i in issues:
        if i.get("suggested_bg"):
            by_page.setdefault(i["page"], []).append(i)
    for pno, items in by_page.items():
        page = pdf.pages[pno]
        res = page_resources(page.obj)
        try:
            ins = list(pikepdf.parse_content_stream(page))
        except Exception:
            continue
        box = page_box(page.obj)
        parea = max(1.0, (box[2] - box[0]) * (box[3] - box[1]))
        infos = Interpreter(res, box).run(ins)
        state = {"kind": "gray", "n": 1, "comps": [0.0], "col_ins": None, "op": "g"}
        stack = []
        paths = []                          # (start, paint_idx, bbox, state)
        for idx, instr in enumerate(ins):
            op = str(instr.operator)
            ops = list(instr.operands)
            if op == "q":
                stack.append(dict(state))
            elif op == "Q":
                state = stack.pop() if stack else state
            elif op == "g" and ops:
                state = {"kind": "gray", "n": 1, "comps": [num(ops[0])], "col_ins": instr, "op": "g"}
            elif op == "rg" and len(ops) == 3:
                state = {"kind": "rgb", "n": 3, "comps": [num(v) for v in ops], "col_ins": instr, "op": "rg"}
            elif op == "k" and len(ops) == 4:
                state = {"kind": "cmyk", "n": 4, "comps": [num(v) for v in ops], "col_ins": instr, "op": "k"}
            elif op == "cs" and ops:
                kind, n = _cs_kind(res, str(ops[0]))
                state = {"kind": kind, "n": n, "comps": [0.0] * max(1, n), "col_ins": None, "op": "sc",
                         "cs_ins": instr}
            elif op in ("sc", "scn") and ops:
                if isinstance(ops[-1], Name):
                    state = dict(state, kind="", col_ins=instr, op=op)
                else:
                    state = dict(state, comps=[num(v) for v in ops], col_ins=instr, op=op)
            info = infos[idx]
            if op in FILL_PAINT and info is not None and info.kind == "path" and info.bbox \
                    and info.path_start >= 0:
                paths.append((info.path_start, idx, tuple(info.bbox), dict(state)))
        before: dict[int, list] = {}
        after: dict[int, list] = {}
        for it in items:
            x0, y0, x1, y1 = it["bbox"]
            ia = max(1e-6, (x1 - x0) * (y1 - y0))
            best = None
            for start, pidx, b, st in paths:
                a = (b[2] - b[0]) * (b[3] - b[1])
                if a > 0.2 * parea or not st["kind"]:
                    continue
                ov = max(0.0, min(b[2], x1) - max(b[0], x0)) * max(0.0, min(b[3], y1) - max(b[1], y0))
                if ov < 0.8 * ia:
                    continue
                if best is None or a < best[0]:
                    best = (a, start, pidx, st)
            if best is None:
                continue
            _, start, pidx, st = best
            if start in before:
                done.add(it["id"])
                continue
            if st["kind"] in ("cmyk", "icc"):
                darker = float(luminance(np.array(from_hex(it["suggested_bg"]), dtype=float))) < \
                    float(luminance(np.array(from_hex(it.get("bg") or "#ffffff"), dtype=float)))
                new = _search_native(list(st["comps"]), st["n"], [it.get("fg_lum", 1.0)],
                                     it["required"], darker)
                if new is None:
                    continue
            else:
                new = _rgb_to_space(from_hex(it["suggested_bg"]), st["kind"], st["n"])
            opname = {"gray": "g", "rgb": "rg", "cmyk": "k"}.get(st["kind"])
            if opname is None:
                opname = st["op"] if st["op"] in ("sc", "scn") else "sc"
            before[start] = [CSI(new, Operator(opname))]
            restore = []
            if st.get("col_ins") is not None:
                restore = [st["col_ins"]]
            else:
                restore = [CSI(st["comps"], Operator(opname))]
            after[pidx] = restore
            done.add(it["id"])
        if before:
            out = []
            for idx, instr in enumerate(ins):
                out.extend(before.get(idx, []))
                out.append(instr)
                out.extend(after.get(idx, []))
            page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(out))
    return done


def _apply_text_fixes(pdf: pikepdf.Pdf, issues: list[dict]) -> dict:
    """Recolour the text itself ('suggested'), lightness only."""
    stats = {"applied": [], "skipped": {}}
    by_page: dict[int, list[dict]] = {}
    for i in issues:
        if i.get("suggested"):
            by_page.setdefault(i["page"], []).append(i)
    for pno, items in by_page.items():
        page = pdf.pages[pno]
        res = page_resources(page.obj)
        try:
            ins = list(pikepdf.parse_content_stream(page))
        except Exception:
            for it in items:
                stats["skipped"][it["id"]] = "content stream could not be parsed"
            continue
        infos = Interpreter(res, page_box(page.obj)).run(ins)
        state = {"kind": "gray", "n": 1, "comps": [0.0], "cs_ins": None, "col_ins": None, "op": "g"}
        stack = []
        out = []
        touched = set()
        for idx, instr in enumerate(ins):
            op = str(instr.operator)
            ops = list(instr.operands)
            if op == "q":
                stack.append(dict(state))
            elif op == "Q":
                state = stack.pop() if stack else state
            elif op == "g" and ops:
                state = {"kind": "gray", "n": 1, "comps": [num(ops[0])], "cs_ins": None, "col_ins": instr, "op": "g"}
            elif op == "rg" and len(ops) == 3:
                state = {"kind": "rgb", "n": 3, "comps": [num(v) for v in ops], "cs_ins": None, "col_ins": instr, "op": "rg"}
            elif op == "k" and len(ops) == 4:
                state = {"kind": "cmyk", "n": 4, "comps": [num(v) for v in ops], "cs_ins": None, "col_ins": instr, "op": "k"}
            elif op == "cs" and ops:
                kind, n = _cs_kind(res, str(ops[0]))
                state = {"kind": kind, "n": n, "comps": [0.0] * max(1, n), "cs_ins": instr,
                         "col_ins": None, "op": "sc"}
            elif op in ("sc", "scn") and ops:
                if isinstance(ops[-1], Name):
                    state = dict(state, kind="", col_ins=instr, op=op)       # pattern
                else:
                    state = dict(state, comps=[num(v) for v in ops], col_ins=instr, op=op)

            info = infos[idx]
            if op in SHOW_OPS and info is not None and info.glyphs:
                hit = None
                for it in items:
                    x0, y0, x1, y1 = it["bbox"]
                    inside = sum(1 for g in info.glyphs
                                 if x0 - 1.5 <= g.cx <= x1 + 1.5 and y0 - 1.5 <= g.cy <= y1 + 1.5)
                    if inside >= 0.7 * len(info.glyphs):
                        hit = it
                        break
                if hit is not None:
                    reason = ""
                    if info.tr in (1, 5):
                        reason = "the text is drawn as an outline only — use the Outline fix"
                    elif not state["kind"]:
                        reason = "spot colour, pattern or unsupported colour space"
                    else:
                        cur = _comps_to_rgb(state["comps"], state["n"])
                        want = from_hex(hit["fg"])
                        if max(abs(a - b) for a, b in zip(cur, want)) > 70 and hit.get("fg_source") == "declared":
                            reason = "the colour in the file does not match the analysed colour"
                    if reason:
                        stats["skipped"].setdefault(hit["id"], reason)
                    else:
                        if state["kind"] in ("cmyk", "icc") and state["n"] in (1, 3, 4):
                            cur_l = float(luminance(np.array(_mupdf_rgb(state["comps"], state["n"]), dtype=float)))
                            sug_l = float(luminance(np.array(from_hex(hit["suggested"]), dtype=float)))
                            new = _search_native(list(state["comps"]), state["n"],
                                                 hit.get("bg_lum") or [0.0], hit["required"], sug_l < cur_l)
                            if new is None:
                                stats["skipped"].setdefault(hit["id"], "no colour in this colour space reaches the ratio")
                                out.append(instr)
                                continue
                        else:
                            new = _rgb_to_space(from_hex(hit["suggested"]), state["kind"], state["n"])
                        if state["kind"] == "gray":
                            out.append(CSI(new, Operator("g")))
                        elif state["kind"] == "rgb":
                            out.append(CSI(new, Operator("rg")))
                        elif state["kind"] == "cmyk":
                            out.append(CSI(new, Operator("k")))
                        else:
                            out.append(CSI(new, Operator(state["op"] if state["op"] in ("sc", "scn") else "sc")))
                        out.append(instr)
                        # restore exactly what was in effect
                        if state["kind"] in ("gray", "rgb", "cmyk") and state["col_ins"] is not None:
                            out.append(state["col_ins"])
                        elif state["col_ins"] is not None:
                            out.append(state["col_ins"])
                        else:
                            out.append(CSI(state["comps"], Operator("sc")))
                        touched.add(hit["id"])
                        continue
            out.append(instr)
        if touched:
            page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(out))
        for it in items:
            if it["id"] in touched:
                stats["applied"].append(it["id"])
            elif it["id"] not in stats["skipped"]:
                stats["skipped"][it["id"]] = "text not found in the page content (packed form XObject?)"
    return stats


def _stroke_colour_ops(kind: str, n: int, cs_name: str, white: bool) -> list:
    """Stroke colour in the same colour-space family as the text fill."""
    v = 1.0 if white else 0.0
    if kind == "rgb":
        return [CSI([v, v, v], Operator("RG"))]
    if kind == "cmyk":
        return [CSI([0, 0, 0, 0] if white else RICH_BLACK, Operator("K"))]
    if kind == "icc" and cs_name and n in (1, 3, 4):
        comps = [v] if n == 1 else ([v, v, v] if n == 3 else ([0, 0, 0, 0] if white else RICH_BLACK))
        return [CSI([Name(cs_name)], Operator("CS")), CSI(comps, Operator("SC"))]
    return [CSI([v], Operator("G"))]


def _apply_stroke_fixes(pdf: pikepdf.Pdf, issues: list[dict], width: float = 0.5,
                        colour: str = "black") -> dict:
    """Outline the letters (text render mode 2 = fill + stroke) with a thin
    black or white stroke — the PitStop 'Fill and Stroke' fix. Width is in
    points on the page whatever the drawing scale; stroke state (Tr, w, j,
    dash, stroke colour) is restored right after each changed text operator."""
    stats = {"applied": [], "skipped": {}}
    width = max(0.05, min(3.0, float(width or 0.5)))
    by_page: dict[int, list[dict]] = {}
    for i in issues:
        by_page.setdefault(i["page"], []).append(i)
    for pno, items in by_page.items():
        page = pdf.pages[pno]
        res = page_resources(page.obj)
        try:
            ins = list(pikepdf.parse_content_stream(page))
        except Exception:
            for it in items:
                stats["skipped"][it["id"]] = "content stream could not be parsed"
            continue
        infos = Interpreter(res, page_box(page.obj)).run(ins)
        egs = res.get("/ExtGState") if isinstance(res, Dictionary) else None
        fill = {"kind": "gray", "n": 1, "cs": ""}
        st = {"stroke": [], "w": 1.0, "j": 0, "dash": None}
        stack = []
        out = []
        touched = set()
        for idx, instr in enumerate(ins):
            op = str(instr.operator)
            ops = list(instr.operands)
            if op == "q":
                stack.append((dict(fill), dict(st)))
            elif op == "Q":
                if stack:
                    fill, st = stack.pop()
            elif op == "g":
                fill = {"kind": "gray", "n": 1, "cs": ""}
            elif op == "rg":
                fill = {"kind": "rgb", "n": 3, "cs": ""}
            elif op == "k":
                fill = {"kind": "cmyk", "n": 4, "cs": ""}
            elif op == "cs" and ops:
                kind, n = _cs_kind(res, str(ops[0]))
                fill = {"kind": kind, "n": n, "cs": str(ops[0])}
            elif op in ("G", "RG", "K", "CS"):
                st = dict(st, stroke=[instr])
            elif op in ("SC", "SCN"):
                keep = [x for x in st["stroke"] if str(x.operator) == "CS"][:1]
                st = dict(st, stroke=keep + [instr])
            elif op == "w" and ops:
                st = dict(st, w=num(ops[0], 1.0))
            elif op == "j" and ops:
                st = dict(st, j=int(num(ops[0])))
            elif op == "d" and len(ops) == 2:
                st = dict(st, dash=(list(ops[0]) if isinstance(ops[0], Array) else [], num(ops[1])))
            elif op == "gs" and ops and isinstance(egs, Dictionary):
                g = egs.get(str(ops[0]))
                if isinstance(g, Dictionary):
                    if "/LW" in g:
                        st = dict(st, w=num(g.get("/LW"), 1.0))
                    if "/LJ" in g:
                        st = dict(st, j=int(num(g.get("/LJ"))))
                    d = g.get("/D")
                    if isinstance(d, Array) and len(d) == 2:
                        st = dict(st, dash=(list(d[0]) if isinstance(d[0], Array) else [], num(d[1])))
            info = infos[idx]
            if op in SHOW_OPS and info is not None and info.glyphs:
                hit = None
                for it in items:
                    x0, y0, x1, y1 = it["bbox"]
                    inside = sum(1 for g_ in info.glyphs
                                 if x0 - 1.5 <= g_.cx <= x1 + 1.5 and y0 - 1.5 <= g_.cy <= y1 + 1.5)
                    if inside >= 0.7 * len(info.glyphs):
                        hit = it
                        break
                if hit is not None:
                    tr = int(info.tr)
                    if tr in (3, 7):
                        stats["skipped"].setdefault(hit["id"], "the text is invisible (render mode 3)")
                        out.append(instr)
                        continue
                    new_tr = {0: 2, 4: 6}.get(tr, tr)
                    a, b, c, d = (info.ctm or (1, 0, 0, 1, 0, 0))[:4]
                    scale = abs(a * d - b * c) ** 0.5 or 1.0
                    white = stroke_colour(hit, colour) == "white"
                    pre = [CSI([new_tr], Operator("Tr"))] if new_tr != tr else []
                    pre += [CSI([round(width / scale, 5)], Operator("w")), CSI([1], Operator("j"))]
                    if st["dash"] and st["dash"][0]:
                        pre.append(CSI([Array([]), 0], Operator("d")))
                    pre += _stroke_colour_ops(fill["kind"], fill["n"], fill["cs"], white)
                    post = [CSI([tr], Operator("Tr"))] if new_tr != tr else []
                    post += [CSI([st["w"]], Operator("w")), CSI([st["j"]], Operator("j"))]
                    if st["dash"] and st["dash"][0]:
                        post.append(CSI([Array(st["dash"][0]), st["dash"][1]], Operator("d")))
                    post += list(st["stroke"]) or [CSI([0], Operator("G"))]
                    out.extend(pre)
                    out.append(instr)
                    out.extend(post)
                    touched.add(hit["id"])
                    hit["_stroke"] = "white" if white else "black"
                    continue
            out.append(instr)
        if touched:
            page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(out))
        for it in items:
            if it["id"] in touched:
                stats["applied"].append(it["id"])
            elif it["id"] not in stats["skipped"]:
                stats["skipped"][it["id"]] = "text not found in the page content (packed form XObject?)"
    return stats


def apply_fixes(pdf: pikepdf.Pdf, issues: list[dict], method: str = "auto",
                stroke_width: float = 0.5, stroke_color: str = "black") -> dict:
    """Fix the given issues with one method:
      background — move the colour of the solid box behind the text
                   (darker behind light text, lighter behind dark text)
      text       — move the text colour (lightness only)
      stroke     — outline the letters (PitStop Fill & Stroke: 0.5 pt black)
      auto       — box behind light text, else text colour, else outline;
                   when one way cannot be written into the file the next is tried
    Returns applied ids, skipped {id: reason} and per-method id lists."""
    method = method if method in METHODS else "auto"
    stats = {"applied": [], "skipped": {}, "background": [], "text": [], "stroke": [], "method": {}}
    plan = {i["id"]: fix_method(i, method) for i in issues}
    for i in issues:
        if not plan[i["id"]]:
            stats["skipped"][i["id"]] = {
                "background": "no solid box behind this text — use Text colour or Outline",
                "text": "no text colour reaches the ratio on this background — use Outline or Box colour",
            }.get(method, "no automatic fix for this item")

    def done(ids, how):
        for x in ids:
            stats["applied"].append(x)
            stats[how].append(x)
            stats["method"][x] = how
            stats["skipped"].pop(x, None)

    bg_items = [i for i in issues if plan[i["id"]] == "background"]
    bg_done = _apply_bg_fixes(pdf, bg_items) if bg_items else set()
    done(sorted(bg_done), "background")
    for i in bg_items:
        if i["id"] not in bg_done:
            stats["skipped"][i["id"]] = "the box behind the text was not found in the page content"

    rest = {x for x in plan if x not in stats["method"]}
    text_items = [i for i in issues if i["id"] in rest and i.get("suggested") and (
        plan[i["id"]] == "text" or (method == "auto" and plan[i["id"]] == "background"))]
    if text_items:
        ts = _apply_text_fixes(pdf, text_items)
        done(ts["applied"], "text")
        stats["skipped"].update({k: v for k, v in ts["skipped"].items() if k not in stats["method"]})

    ol_ok = lambda i: max((i.get("outline") or {}).values() or [0]) >= i.get("required", 4.5)  # noqa: E731
    rest = {x for x in plan if x not in stats["method"]}
    stroke_items = [i for i in issues if i["id"] in rest and i.get("zone") != "figure" and (
        plan[i["id"]] == "stroke" or (method == "auto" and plan[i["id"]] and ol_ok(i)))]
    if stroke_items:
        # auto falls back to an outline: pick the colour that works best
        colour = stroke_color if method == "stroke" else "auto"
        ss = _apply_stroke_fixes(pdf, stroke_items, stroke_width, colour)
        done(ss["applied"], "stroke")
        stats["skipped"].update({k: v for k, v in ss["skipped"].items() if k not in stats["method"]})
        for i in stroke_items:
            if i.get("_stroke"):
                stats.setdefault("stroke_colour", {})[i["id"]] = i.pop("_stroke")
    return stats

