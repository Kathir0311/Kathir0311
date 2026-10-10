"""Test files with bullets set in a symbol font, the way Word and PowerPoint
write them (used by symbol_bullets_test.py).

A small TrueType font named like Wingdings is built here (a box, an arrow, a
check mark, a dot) with the symbol cmap (3,0) at U+F0xx, the way Wingdings
has it. The page uses it three ways, each one a way real files go wrong:

* "❑" (code 0x71) through a simple TrueType font whose ToUnicode says U+F071
  (Private Use)                         → the text layer has ""
* "❑" through a Type0 (Identity-H) font whose ToUnicode says U+FFFD
                                        → the text layer has "�"
* "➢" (code 0xD8) through a simple font whose ToUnicode says "¾"
                                        → the text layer has "¾"

The text after each bullet is Helvetica, set at the hanging indent (a tab in
Word), and one item runs over two lines.
"""
from __future__ import annotations

import io

import pikepdf
from pikepdf import Array, Dictionary, Name


def font_bytes(family: str = "Wingdings") -> bytes:
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    def poly(*pts):
        pen = TTGlyphPen(None)
        for ring in pts:
            pen.moveTo(ring[0])
            for p in ring[1:]:
                pen.lineTo(p)
            pen.closePath()
        return pen.glyph()

    names = [".notdef", "box", "arrow", "check", "dot"]
    glyphs = {
        ".notdef": poly([(0, 0), (0, 700), (500, 700), (500, 0)]),
        "box": poly([(80, 0), (80, 620), (700, 620), (700, 0)], [(140, 60), (640, 60), (640, 560), (140, 560)]),
        "arrow": poly([(60, 0), (760, 330), (60, 660), (220, 330)]),
        "check": poly([(40, 300), (300, 0), (760, 640), (680, 700), (300, 160), (120, 380)]),
        "dot": poly([(150, 150), (150, 550), (550, 550), (550, 150)]),
    }
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(names)
    fb.setupCharacterMap({0xF071: "box", 0xF0D8: "arrow", 0xF0FC: "check", 0xF09F: "dot"})
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics({n: (800, 0) for n in names})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": family, "styleName": "Regular"})
    fb.setupOS2(sTypoAscender=800, usWinAscent=800, usWinDescent=200)
    fb.setupPost()
    # Wingdings carries the symbol cmap (3,0) only
    from fontTools.ttLib.tables._c_m_a_p import cmap_format_4
    sub = cmap_format_4(4)
    sub.platformID, sub.platEncID, sub.language = 3, 0, 0
    sub.cmap = {0xF071: "box", 0xF0D8: "arrow", 0xF0FC: "check", 0xF09F: "dot"}
    fb.font["cmap"].tables = [sub]
    b = io.BytesIO()
    fb.save(b)
    return b.getvalue()


def _cmap(pairs: dict[int, str], two: bool) -> bytes:
    w = 4 if two else 2
    body = "\n".join(f"<{c:0{w}X}> <{u.encode('utf-16-be').hex().upper()}>" for c, u in sorted(pairs.items()))
    return ("/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            "/CMapName /Adobe-Identity-UCS def /CMapType 2 def\n"
            f"1 begincodespacerange <{'0' * w}> <{'F' * w}> endcodespacerange\n"
            f"{len(pairs)} beginbfchar\n{body}\nendbfchar\n"
            "endcmap CMapName currentdict /CMap defineresource pop end end").encode()


ITEMS1 = ["The central nervous system (CNS) is the brain and the spinal cord.",
          ("The nerves that leave them form the peripheral nervous", "system (PNS)."),
          "Reflexes are handled in the spinal cord."]
ITEMS2 = ["Sensory neurons carry signals in.", "Motor neurons carry signals out."]
ITEMS3 = ["Check the sample.", "Write the result."]


def make(path: str, family: str = "Wingdings") -> None:
    pdf = pikepdf.new()
    ff = pdf.make_stream(font_bytes(family))
    ff.Length1 = len(font_bytes(family))

    def descriptor(name):
        return pdf.make_indirect(Dictionary(
            Type=Name.FontDescriptor, FontName=Name("/" + name), Flags=4,
            FontBBox=Array([0, -200, 800, 800]), ItalicAngle=0, Ascent=800, Descent=-200,
            CapHeight=700, StemV=80, FontFile2=ff))

    simple_pua = pdf.make_indirect(Dictionary(
        Type=Name.Font, Subtype=Name.TrueType, BaseFont=Name("/ABCDEF+" + family),
        FirstChar=0x71, LastChar=0xFC, Widths=Array([800] * (0xFC - 0x71 + 1)),
        FontDescriptor=descriptor("ABCDEF+" + family),
        ToUnicode=pdf.make_stream(_cmap({0x71: "", 0xFC: ""}, False))))
    simple_bad = pdf.make_indirect(Dictionary(
        Type=Name.Font, Subtype=Name.TrueType, BaseFont=Name("/GHIJKL+" + family),
        FirstChar=0x71, LastChar=0xFC, Widths=Array([800] * (0xFC - 0x71 + 1)),
        FontDescriptor=descriptor("GHIJKL+" + family),
        ToUnicode=pdf.make_stream(_cmap({0xD8: "¾", 0x9F: "¾"}, False))))
    cid = pdf.make_indirect(Dictionary(
        Type=Name.Font, Subtype=Name.CIDFontType2, BaseFont=Name("/MNOPQR+" + family),
        CIDSystemInfo=Dictionary(Registry=pikepdf.String("Adobe"), Ordering=pikepdf.String("Identity"), Supplement=0),
        FontDescriptor=descriptor("MNOPQR+" + family), DW=800, CIDToGIDMap=Name.Identity))
    type0 = pdf.make_indirect(Dictionary(
        Type=Name.Font, Subtype=Name.Type0, BaseFont=Name("/MNOPQR+" + family), Encoding=Name("/Identity-H"),
        DescendantFonts=Array([cid]), ToUnicode=pdf.make_stream(_cmap({1: "�", 2: "�"}, True))))
    helv = Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica, Encoding=Name.WinAnsiEncoding)

    ops = ["BT /H 18 Tf 72 760 Td (The nervous system) Tj ET",
           "BT /H 11 Tf 72 730 Td (It has two parts.) Tj ET"]
    y = 704

    def bullet(font, code_hex, y):
        ops.append(f"BT /{font} 10 Tf 72 {y} Td <{code_hex}> Tj ET")

    def text(t, y):
        t = t.replace("(", "\\(").replace(")", "\\)")
        ops.append(f"BT /H 11 Tf 90 {y} Td ({t}) Tj ET")

    for it in ITEMS1:                       # ❑ through U+F071
        bullet("W1", "71", y)
        for k, ln in enumerate(it if isinstance(it, tuple) else (it,)):
            text(ln, y)
            y -= 16
    ops.append(f"BT /H 11 Tf 72 {y - 14} Td (Each neuron has a cell body and an axon.) Tj ET")
    y -= 48
    for it in ITEMS2:                       # ❑ through a CID font with U+FFFD
        bullet("W0", "0001", y)
        text(it, y)
        y -= 16
    ops.append(f"BT /H 11 Tf 72 {y - 14} Td (Two steps follow.) Tj ET")
    y -= 48
    for it in ITEMS3:                       # ➢ through "¾"
        bullet("W2", "D8", y)
        text(it, y)
        y -= 16
    ops.append(f"BT /H 11 Tf 72 {y - 14} Td (That is all.) Tj ET")
    page = pdf.add_blank_page(page_size=(612, 792))
    page.obj.Resources = Dictionary(Font=Dictionary(H=helv, W1=simple_pua, W0=type0, W2=simple_bad))
    page.obj.Contents = pdf.make_stream("\n".join(ops).encode("latin-1"))
    pdf.save(path)
