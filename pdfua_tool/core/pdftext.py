"""Glyph-level interpretation of PDF content streams.

Computes where every glyph, path, image and form XObject lands on the page,
in PDF user space. The tagger uses this to decide, glyph by glyph, which
structure element owns a piece of content, and to split text-showing
operators where ownership changes mid-string.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

import pikepdf
from pikepdf import Name

Matrix = tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
Rect = tuple[float, float, float, float]

PAINT_OPS = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "n"}
PATH_OPS = {"m", "l", "c", "v", "y", "h", "re"}
CLIP_OPS = {"W", "W*"}
SHOW_OPS = {"Tj", "TJ", "'", '"'}
INLINE_IMAGE_OPS = {"INLINE IMAGE", "INLINE_IMAGE", "BI"}

BASE14 = {
    "Helvetica": "helv", "Helvetica-Bold": "hebo", "Helvetica-Oblique": "heit",
    "Helvetica-BoldOblique": "hebi", "Times-Roman": "tiro", "Times-Bold": "tibo",
    "Times-Italic": "tiit", "Times-BoldItalic": "tibi", "Courier": "cour",
    "Courier-Bold": "cobo", "Courier-Oblique": "coit", "Courier-BoldOblique": "cobi",
    "Symbol": "symb", "ZapfDingbats": "zadb",
    # common aliases
    "Arial": "helv", "Arial,Bold": "hebo", "Arial-BoldMT": "hebo", "ArialMT": "helv",
    "Arial,Italic": "heit", "Arial-ItalicMT": "heit", "Arial,BoldItalic": "hebi",
    "Arial-BoldItalicMT": "hebi", "TimesNewRoman": "tiro", "TimesNewRomanPSMT": "tiro",
    "TimesNewRoman,Bold": "tibo", "TimesNewRomanPS-BoldMT": "tibo",
    "TimesNewRoman,Italic": "tiit", "TimesNewRomanPS-ItalicMT": "tiit",
    "TimesNewRoman,BoldItalic": "tibi", "TimesNewRomanPS-BoldItalicMT": "tibi",
    "CourierNew": "cour", "CourierNewPSMT": "cour", "CourierNew,Bold": "cobo",
    "CourierNewPS-BoldMT": "cobo",
}


# ------------------------------------------------------------------ matrices
def mmul(m: Matrix, n: Matrix) -> Matrix:
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (a * a2 + b * c2, a * b2 + b * d2,
            c * a2 + d * c2, c * b2 + d * d2,
            e * a2 + f * c2 + e2, e * b2 + f * d2 + f2)


def apply(m: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def rect_of_points(pts: Iterable[tuple[float, float]]) -> Rect | None:
    pts = list(pts)
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def intersect(a: Rect | None, b: Rect | None) -> Rect | None:
    if a is None:
        return b
    if b is None:
        return a
    r = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    if r[0] > r[2] or r[1] > r[3]:
        return (r[0], r[1], r[0], r[1])
    return r


def num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def base_name(n: Any) -> str:
    s = str(n or "")
    s = s[1:] if s.startswith("/") else s
    return s.split("+", 1)[1] if re.match(r"^[A-Z]{6}\+", s) else s


# ------------------------------------------------------------ resources
def page_resources(page_obj: pikepdf.Dictionary) -> pikepdf.Dictionary:
    """/Resources of a page, honouring inheritance from the page tree."""
    node = page_obj
    for _ in range(64):
        if node is None:
            break
        r = node.get("/Resources")
        if isinstance(r, pikepdf.Dictionary):
            return r
        node = node.get("/Parent")
    return pikepdf.Dictionary()


def _get(d: Any, key: str) -> Any:
    try:
        return d.get(key) if isinstance(d, pikepdf.Dictionary) else None
    except Exception:
        return None


# ------------------------------------------------------------ encodings
_STD = None


def _standard_encoding() -> list[str]:
    global _STD
    if _STD is None:
        try:
            from fontTools.encodings.StandardEncoding import StandardEncoding
            _STD = list(StandardEncoding)
        except Exception:
            _STD = [".notdef"] * 256
    return _STD


def _codec_names(codec: str) -> list[str]:
    try:
        from fontTools import agl
    except Exception:
        agl = None
    out = []
    for c in range(256):
        try:
            ch = bytes([c]).decode(codec)
        except Exception:
            out.append(".notdef")
            continue
        if agl is not None:
            name = agl.UV2AGL.get(ord(ch)) or f"uni{ord(ch):04X}"
        else:
            name = f"uni{ord(ch):04X}"
        out.append(name)
    return out


_ENC_CACHE: dict[str, list[str]] = {}


def base_encoding(name: str) -> list[str]:
    name = name.lstrip("/")
    if name in _ENC_CACHE:
        return _ENC_CACHE[name]
    if name == "WinAnsiEncoding":
        enc = _codec_names("cp1252")
        enc[0x27] = "quotesingle"
        enc[0x60] = "grave"
    elif name == "MacRomanEncoding":
        enc = _codec_names("mac_roman")
    elif name == "PDFDocEncoding":
        enc = _codec_names("latin-1")
    else:
        enc = _standard_encoding()
    _ENC_CACHE[name] = enc
    return enc


def font_encoding(fdict: pikepdf.Dictionary) -> tuple[list[str], bool]:
    """Code -> glyph name for a simple font. Returns (names, explicit)."""
    enc = fdict.get("/Encoding")
    explicit = enc is not None
    if isinstance(enc, pikepdf.Name):
        names = list(base_encoding(str(enc)))
    elif isinstance(enc, pikepdf.Dictionary):
        be = enc.get("/BaseEncoding")
        names = list(base_encoding(str(be)) if be is not None else _standard_encoding())
        diffs = enc.get("/Differences")
        if isinstance(diffs, pikepdf.Array):
            code = 0
            for item in diffs:
                if isinstance(item, pikepdf.Name):
                    if 0 <= code < 256:
                        names[code] = str(item)[1:]
                    code += 1
                else:
                    code = int(num(item))
    else:
        names = list(_standard_encoding())
    return names, explicit


def glyph_unicode(name: str) -> str:
    try:
        from fontTools import agl
        return agl.toUnicode(name) or ""
    except Exception:
        return ""


# ------------------------------------------------------------ font metrics
@dataclass
class FontMetrics:
    subtype: str = "/Type1"
    name: str = ""
    multibyte: bool = False
    vertical: bool = False
    scale: float = 0.001
    widths: dict[int, float] = field(default_factory=dict)   # code or cid -> glyph units
    default_width: float = 500.0
    ascent: float = 0.8
    descent: float = -0.2
    codespace: list[tuple[int, int, int]] = field(default_factory=lambda: [(1, 0, 255)])
    cid_map: dict[int, int] | None = None     # code -> cid (Type0, non-identity)
    embedded: bool = False
    blank: frozenset = frozenset()            # codes that show white space

    def split(self, data: bytes) -> list[tuple[int, int, int]]:
        """-> [(code, byte_start, byte_end)]"""
        if not self.multibyte:
            return [(b, i, i + 1) for i, b in enumerate(data)]
        out = []
        i = 0
        n = len(data)
        lengths = sorted({c[0] for c in self.codespace}) or [2]
        while i < n:
            taken = False
            for ln in lengths:
                if i + ln > n:
                    continue
                code = int.from_bytes(data[i:i + ln], "big")
                for (cl, lo, hi) in self.codespace:
                    if cl == ln and lo <= code <= hi:
                        out.append((code, i, i + ln))
                        i += ln
                        taken = True
                        break
                if taken:
                    break
            if not taken:
                ln = lengths[0] if i + lengths[0] <= n else 1
                out.append((int.from_bytes(data[i:i + ln], "big"), i, i + ln))
                i += ln
        return out

    def width(self, code: int) -> float:
        """Horizontal displacement in text-space units per 1pt font size."""
        key = code
        if self.multibyte and self.cid_map is not None:
            key = self.cid_map.get(code, code)
        w = self.widths.get(key, self.default_width)
        return w * self.scale

    def is_space(self, code: int, nbytes: int) -> bool:
        return nbytes == 1 and code == 32


_BASE14_FONTS: dict[str, Any] = {}


def _base14_advance(fontname: str, names: list[str]) -> dict[int, float]:
    import pymupdf
    key = BASE14.get(fontname)
    if key is None:
        stem = fontname.split(",")[0].split("-")[0].lower()
        if "times" in stem or "serif" in stem:
            key = "tiro"
        elif "courier" in stem or "mono" in stem:
            key = "cour"
        else:
            key = "helv"
        low = fontname.lower()
        bold = "bold" in low or "black" in low or "heavy" in low
        ital = "italic" in low or "oblique" in low
        key = {"helv": ["helv", "hebo", "heit", "hebi"],
               "tiro": ["tiro", "tibo", "tiit", "tibi"],
               "cour": ["cour", "cobo", "coit", "cobi"]}[key][bold + 2 * ital if not (bold and ital) else 3]
    font = _BASE14_FONTS.get(key)
    if font is None:
        font = pymupdf.Font(key)
        _BASE14_FONTS[key] = font
    out: dict[int, float] = {}
    for code in range(256):
        name = names[code] if code < len(names) else ".notdef"
        if key in ("symb", "zadb"):
            try:
                out[code] = font.glyph_advance(code) * 1000
            except Exception:
                out[code] = 500
            continue
        u = glyph_unicode(name) if name and name != ".notdef" else ""
        if not u:
            continue
        try:
            out[code] = font.glyph_advance(ord(u[0])) * 1000
        except Exception:
            out[code] = 500
    return out


def _parse_w_array(w: Any) -> dict[int, float]:
    out: dict[int, float] = {}
    if not isinstance(w, pikepdf.Array):
        return out
    items = list(w)
    i = 0
    while i < len(items):
        try:
            first = int(num(items[i]))
            nxt = items[i + 1]
            if isinstance(nxt, pikepdf.Array):
                for k, v in enumerate(nxt):
                    out[first + k] = num(v)
                i += 2
            else:
                last = int(num(nxt))
                v = num(items[i + 2])
                if last - first < 70000:
                    for c in range(first, last + 1):
                        out[c] = v
                i += 3
        except Exception:
            break
    return out


_HEX = re.compile(rb"<([0-9A-Fa-f]+)>")


def parse_cmap_stream(data: bytes) -> tuple[list[tuple[int, int, int]], dict[int, int]]:
    """Codespace ranges and code->CID from an embedded CMap stream."""
    codespace: list[tuple[int, int, int]] = []
    cids: dict[int, int] = {}
    for block in re.findall(rb"begincodespacerange(.*?)endcodespacerange", data, re.S):
        hexes = _HEX.findall(block)
        for lo, hi in zip(hexes[0::2], hexes[1::2]):
            codespace.append((len(lo) // 2, int(lo, 16), int(hi, 16)))
    for block in re.findall(rb"begincidrange(.*?)endcidrange", data, re.S):
        for lo, hi, cid in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\d+)", block):
            a, b, c = int(lo, 16), int(hi, 16), int(cid)
            if b - a < 70000:
                for k in range(b - a + 1):
                    cids[a + k] = c + k
    for block in re.findall(rb"begincidchar(.*?)endcidchar", data, re.S):
        for code, cid in re.findall(rb"<([0-9A-Fa-f]+)>\s*(\d+)", block):
            cids[int(code, 16)] = int(cid)
    return codespace, cids


_BLANK_UNI = {0x09, 0x20, 0xA0, 0x3000, 0x202F, 0x205F} | set(range(0x2000, 0x200C))
_BLANK_NAMES = {"space", "nbspace", "nonbreakingspace", "uni00A0", "uni0020", "uni2009", "uni2002",
                "uni2003", "uni200A", "uni202F", "thinspace", "enspace", "emspace"}


def _blank_codes(fdict: pikepdf.Dictionary, simple: bool) -> frozenset:
    """The codes of a font that show white space, from its ToUnicode map
    (or, for a simple font without one, its encoding)."""
    out: set[int] = set()
    seen: set[int] = set()
    tu = fdict.get("/ToUnicode")
    if isinstance(tu, pikepdf.Stream):
        try:
            data = tu.read_bytes()
        except Exception:
            data = b""
        for block in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.S):
            for code, uni in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
                c = int(code, 16)
                seen.add(c)
                if uni and len(uni) <= 8 and int(uni, 16) in _BLANK_UNI:
                    out.add(c)
        for block in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.S):
            for lo, hi, uni in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
                a, b, u = int(lo, 16), int(hi, 16), int(uni, 16)
                if b - a > 70000 or len(uni) > 8:
                    continue
                for k in range(b - a + 1):
                    seen.add(a + k)
                    if u + k in _BLANK_UNI:
                        out.add(a + k)
    if simple:
        try:
            names, _ = font_encoding(fdict)
        except Exception:
            names = []
        for c, nm in enumerate(names):
            if c not in seen and nm in _BLANK_NAMES:
                out.add(c)
    return frozenset(out)


def load_font(fdict: Any) -> FontMetrics:
    fm = FontMetrics()
    if not isinstance(fdict, pikepdf.Dictionary):
        return fm
    try:
        fm.blank = _blank_codes(fdict, str(fdict.get("/Subtype", "/Type1")) not in ("/Type0", "/Type3"))
    except Exception:
        pass
    fm.subtype = str(fdict.get("/Subtype", "/Type1"))
    fm.name = base_name(fdict.get("/BaseFont"))
    desc = fdict.get("/FontDescriptor")

    if fm.subtype == "/Type0":
        fm.multibyte = True
        fm.codespace = [(2, 0, 0xFFFF)]
        enc = fdict.get("/Encoding")
        if isinstance(enc, pikepdf.Name):
            en = str(enc)
            fm.vertical = en.endswith("-V")
            if "Identity" not in en:
                fm.cid_map = None      # predefined CMap: widths fall back to DW
        elif isinstance(enc, pikepdf.Stream):
            try:
                cs, cids = parse_cmap_stream(enc.read_bytes())
                if cs:
                    fm.codespace = cs
                if cids:
                    fm.cid_map = cids
                wmode = enc.get("/WMode")
                fm.vertical = bool(wmode and int(num(wmode)) == 1)
            except Exception:
                pass
        kids = fdict.get("/DescendantFonts")
        desc_font = kids[0] if isinstance(kids, pikepdf.Array) and len(kids) else None
        if isinstance(desc_font, pikepdf.Dictionary):
            fm.default_width = num(desc_font.get("/DW"), 1000.0)
            fm.widths = _parse_w_array(desc_font.get("/W"))
            desc = desc_font.get("/FontDescriptor")
    elif fm.subtype == "/Type3":
        fmx = fdict.get("/FontMatrix")
        try:
            fm.scale = abs(num(fmx[0], 0.001)) or 0.001
            yscale = abs(num(fmx[3], 0.001)) or 0.001
        except Exception:
            yscale = 0.001
        first = int(num(fdict.get("/FirstChar"), 0))
        for k, w in enumerate(fdict.get("/Widths") or []):
            fm.widths[first + k] = num(w)
        fm.default_width = 0.0
        bb = fdict.get("/FontBBox")
        try:
            fm.ascent = num(bb[3]) * yscale or 0.8
            fm.descent = num(bb[1]) * yscale
        except Exception:
            pass
        fm.embedded = True
        return fm
    else:
        first = int(num(fdict.get("/FirstChar"), 0))
        widths = fdict.get("/Widths")
        if isinstance(widths, pikepdf.Array) and len(widths):
            for k, w in enumerate(widths):
                fm.widths[first + k] = num(w)
            fm.default_width = num(_get(desc, "/MissingWidth"), 0.0) or 0.0
        else:
            names, _ = font_encoding(fdict)
            try:
                fm.widths = _base14_advance(fm.name, names)
            except Exception:
                fm.widths = {}
            fm.default_width = 500.0

    if isinstance(desc, pikepdf.Dictionary):
        fm.embedded = any(k in desc for k in ("/FontFile", "/FontFile2", "/FontFile3"))
        asc = num(desc.get("/Ascent"), 0.0)
        dsc = num(desc.get("/Descent"), 0.0)
        if 200 <= asc <= 1500:
            fm.ascent = asc / 1000
        if -700 <= dsc <= 0:
            fm.descent = dsc / 1000 if dsc < 0 else -0.2
    return fm


# ------------------------------------------------------------ interpreter
@dataclass
class Glyph:
    elem: int          # index into TJ array (0 for Tj)
    b0: int            # byte offsets inside that string
    b1: int
    cx: float          # centre, user space
    cy: float
    box: Rect
    blank: bool = False   # shows white space only


@dataclass
class OpInfo:
    kind: str                         # text | path | image | form | shading | inline
    glyphs: list[Glyph] = field(default_factory=list)
    bbox: Rect | None = None
    name: str = ""
    path_start: int = -1              # first construction op of a painted path
    clip_only: bool = False
    font_size: float = 0.0            # effective size in user space
    ctm: Matrix | None = None         # text: CTM in effect (line width scale)
    tr: int = 0                       # text: render mode in effect


@dataclass
class GState:
    ctm: Matrix = IDENTITY
    clip: Rect | None = None
    tc: float = 0.0
    tw: float = 0.0
    th: float = 1.0
    tl: float = 0.0
    font: str = ""
    fs: float = 0.0
    ts: float = 0.0
    tr: int = 0

    def copy(self) -> "GState":
        return GState(self.ctm, self.clip, self.tc, self.tw, self.th, self.tl,
                      self.font, self.fs, self.ts, self.tr)


class Interpreter:
    """Walks a content stream and reports where things are drawn."""

    def __init__(self, resources: pikepdf.Dictionary, page_box: Rect | None = None,
                 base_ctm: Matrix = IDENTITY) -> None:
        self.resources = resources
        self.fonts_dict = _get(resources, "/Font") or pikepdf.Dictionary()
        self.xobjects = _get(resources, "/XObject") or pikepdf.Dictionary()
        self.font_cache: dict[str, FontMetrics] = {}
        self.page_box = page_box
        self.base_ctm = base_ctm
        # the text state in effect before each show operator:
        # (Tm, Tlm, font size, horizontal scale, render mode, vertical font)
        self.text_state: dict[int, tuple] = {}
        # the leading (TL) a T*, ' or " moved down by
        self.leading: dict[int, float] = {}

    def font(self, name: str) -> FontMetrics:
        fm = self.font_cache.get(name)
        if fm is None:
            try:
                fm = load_font(self.fonts_dict.get(name))
            except Exception:
                fm = FontMetrics()
            self.font_cache[name] = fm
        return fm

    def xobject_subtype(self, name: str) -> str:
        try:
            obj = self.xobjects.get(name)
            return str(obj.get("/Subtype", "")).lstrip("/") if obj is not None else ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    def run(self, instructions: list) -> list[OpInfo | None]:
        infos: list[OpInfo | None] = [None] * len(instructions)
        gs = GState(ctm=self.base_ctm, clip=self.page_box)
        stack: list[GState] = []
        tm: Matrix = IDENTITY
        tlm: Matrix = IDENTITY
        path_pts: list[tuple[float, float]] = []
        path_start = -1
        pending_clip = False

        for i, ins in enumerate(instructions):
            op = str(ins.operator)
            ops = ins.operands

            if op in PATH_OPS:
                if path_start < 0:
                    path_start = i
                ctm = gs.ctm
                if op == "re" and len(ops) == 4:
                    x, y, w, h = (num(v) for v in ops)
                    for px, py in ((x, y), (x + w, y), (x, y + h), (x + w, y + h)):
                        path_pts.append(apply(ctm, px, py))
                elif op != "h":
                    vals = [num(v) for v in ops]
                    for k in range(0, len(vals) - 1, 2):
                        path_pts.append(apply(ctm, vals[k], vals[k + 1]))
                continue
            if op in CLIP_OPS:
                pending_clip = True
                if path_start < 0:
                    path_start = i
                continue
            if op in PAINT_OPS:
                bbox = rect_of_points(path_pts)
                info = OpInfo("path", bbox=intersect(bbox, gs.clip) if bbox else None,
                              path_start=path_start if path_start >= 0 else i,
                              clip_only=(op == "n"))
                infos[i] = info
                if pending_clip and bbox is not None:
                    gs.clip = intersect(gs.clip, bbox)
                path_pts = []
                path_start = -1
                pending_clip = False
                continue

            if op == "q":
                stack.append(gs.copy())
            elif op == "Q":
                gs = stack.pop() if stack else GState(ctm=self.base_ctm, clip=self.page_box)
            elif op == "cm" and len(ops) == 6:
                gs.ctm = mmul(tuple(num(v) for v in ops), gs.ctm)   # type: ignore[arg-type]
            elif op == "BT":
                tm = tlm = IDENTITY
            elif op == "Tc" and ops:
                gs.tc = num(ops[0])
            elif op == "Tw" and ops:
                gs.tw = num(ops[0])
            elif op == "Tz" and ops:
                gs.th = num(ops[0], 100.0) / 100.0
            elif op == "TL" and ops:
                gs.tl = num(ops[0])
            elif op == "Ts" and ops:
                gs.ts = num(ops[0])
            elif op == "Tr" and ops:
                gs.tr = int(num(ops[0]))
            elif op == "Tf" and len(ops) >= 2:
                gs.font = str(ops[0])
                gs.fs = num(ops[1])
            elif op == "Td" and len(ops) == 2:
                tlm = mmul((1, 0, 0, 1, num(ops[0]), num(ops[1])), tlm)
                tm = tlm
            elif op == "TD" and len(ops) == 2:
                gs.tl = -num(ops[1])
                tlm = mmul((1, 0, 0, 1, num(ops[0]), num(ops[1])), tlm)
                tm = tlm
            elif op == "Tm" and len(ops) == 6:
                tlm = tm = tuple(num(v) for v in ops)   # type: ignore[assignment]
            elif op == "T*":
                self.leading[i] = gs.tl
                tlm = mmul((1, 0, 0, 1, 0, -gs.tl), tlm)
                tm = tlm
            elif op in SHOW_OPS:
                self.text_state[i] = (tm, tlm, gs.fs, gs.th, gs.tr, self.font(gs.font).vertical)
                if op == "'":
                    self.leading[i] = gs.tl
                    tlm = mmul((1, 0, 0, 1, 0, -gs.tl), tlm)
                    tm = tlm
                    elems = [ops[0]] if ops else []
                elif op == '"':
                    self.leading[i] = gs.tl
                    if len(ops) == 3:
                        gs.tw = num(ops[0])
                        gs.tc = num(ops[1])
                        elems = [ops[2]]
                    else:
                        elems = []
                    tlm = mmul((1, 0, 0, 1, 0, -gs.tl), tlm)
                    tm = tlm
                elif op == "Tj":
                    elems = [ops[0]] if ops else []
                else:
                    arr = ops[0] if ops else []
                    elems = list(arr) if isinstance(arr, pikepdf.Array) else []
                info, tm = self._show(elems, gs, tm, op == "TJ")
                infos[i] = info
            elif op == "Do" and ops:
                name = str(ops[0])
                kind = self.xobject_subtype(name)
                box = self._unit_box(gs.ctm)
                if kind == "Form":
                    try:
                        xo = self.xobjects.get(name)
                        bb = xo.get("/BBox")
                        mx = xo.get("/Matrix")
                        m = tuple(num(v) for v in mx) if isinstance(mx, pikepdf.Array) and len(mx) == 6 else IDENTITY
                        full = mmul(m, gs.ctm)   # type: ignore[arg-type]
                        x0, y0, x1, y1 = (num(v) for v in bb)
                        box = rect_of_points([apply(full, x0, y0), apply(full, x1, y0),
                                              apply(full, x0, y1), apply(full, x1, y1)])
                    except Exception:
                        pass
                infos[i] = OpInfo("form" if kind == "Form" else "image",
                                  bbox=intersect(box, gs.clip), name=name)
            elif op in INLINE_IMAGE_OPS:
                infos[i] = OpInfo("inline", bbox=intersect(self._unit_box(gs.ctm), gs.clip))
            elif op == "sh":
                infos[i] = OpInfo("shading", bbox=gs.clip)
        return infos

    @staticmethod
    def _unit_box(ctm: Matrix) -> Rect:
        return rect_of_points([apply(ctm, 0, 0), apply(ctm, 1, 0),
                               apply(ctm, 0, 1), apply(ctm, 1, 1)])  # type: ignore[return-value]

    def _show(self, elems: list, gs: GState, tm: Matrix,
              is_tj: bool) -> tuple[OpInfo, Matrix]:
        fm = self.font(gs.font)
        fs = gs.fs
        th = gs.th
        A = mmul(tm, gs.ctm)
        # effective font size in user space (for tolerances)
        sx = (A[0] ** 2 + A[1] ** 2) ** 0.5
        sy = (A[2] ** 2 + A[3] ** 2) ** 0.5
        info = OpInfo("text", font_size=abs(fs) * max(sx, sy), ctm=gs.ctm, tr=gs.tr)
        x_adv = 0.0
        y_adv = 0.0
        asc, desc = fm.ascent, fm.descent
        for ei, el in enumerate(elems):
            if isinstance(el, pikepdf.String):
                data = bytes(el)
                for code, b0, b1 in fm.split(data):
                    w0 = fm.width(code)
                    if fm.vertical:
                        gx0, gy0 = x_adv - 0.5 * fs * th, y_adv - fs
                        gx1, gy1 = x_adv + 0.5 * fs * th, y_adv
                        cxt, cyt = x_adv, y_adv - 0.5 * fs
                    else:
                        gx0 = x_adv
                        gx1 = x_adv + w0 * fs * th
                        gy0 = desc * fs + gs.ts
                        gy1 = asc * fs + gs.ts
                        cxt = (gx0 + gx1) / 2
                        cyt = (gy0 + gy1) / 2
                    cx, cy = apply(A, cxt, cyt)
                    box = rect_of_points([apply(A, gx0, gy0), apply(A, gx1, gy0),
                                          apply(A, gx0, gy1), apply(A, gx1, gy1)])
                    info.glyphs.append(Glyph(ei if is_tj else 0, b0, b1, cx, cy, box,  # type: ignore[arg-type]
                                             code in fm.blank))
                    spacing = gs.tc + (gs.tw if fm.is_space(code, b1 - b0) else 0.0)
                    if fm.vertical:
                        y_adv -= fs + spacing
                    else:
                        x_adv += (w0 * fs + spacing) * th
            else:
                adj = num(el) / 1000.0 * fs
                if fm.vertical:
                    y_adv -= adj
                else:
                    x_adv -= adj * th
        new_tm = mmul((1, 0, 0, 1, x_adv, y_adv), tm)
        return info, new_tm


def page_box(page_obj: pikepdf.Dictionary) -> Rect:
    for key in ("/CropBox", "/MediaBox"):
        node = page_obj
        for _ in range(32):
            if node is None:
                break
            b = node.get(key)
            if isinstance(b, pikepdf.Array) and len(b) == 4:
                x0, y0, x1, y1 = (num(v) for v in b)
                return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
            node = node.get("/Parent")
    return (0.0, 0.0, 612.0, 792.0)


def contains_text(stream_obj: Any, depth: int = 0) -> bool:
    """Does a form XObject (or anything it draws) show text?"""
    if depth > 6 or not isinstance(stream_obj, pikepdf.Stream):
        return False
    try:
        ins = pikepdf.parse_content_stream(stream_obj)
    except Exception:
        return False
    res = stream_obj.get("/Resources")
    xo = _get(res, "/XObject") if res is not None else None
    for i in ins:
        op = str(i.operator)
        if op in SHOW_OPS:
            return True
        if op == "Do" and xo is not None and i.operands:
            child = xo.get(str(i.operands[0]))
            if isinstance(child, pikepdf.Stream) and str(child.get("/Subtype")) == "/Form":
                if contains_text(child, depth + 1):
                    return True
    return False
