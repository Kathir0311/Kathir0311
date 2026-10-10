"""A small Teacher's Guide in the Academain template (V4.24 test file).

Four pages set in the template's own type faces (the fonts are pymupdf's
built-in ones, embedded and renamed to the faces the book uses, so the file
reads exactly like the client's): a cover, the contents, a lesson's first
page (margin column, a ruled box of three columns, a blending grid, the
running foot, the page number, the lesson tab on the page edge) and a page
with a Vocabulary Booster box and the "Make Informed Decisions" box.
"""
from __future__ import annotations

import os
import sys

import pikepdf
import pymupdf

FACES = {
    # template name: built-in font it is made from
    "FiraSans-Bold": "hebo",
    "FiraSansCondensed-ExtraB": "hebo",
    "FiraSansExtraCondensed-B": "hebo",
    "FiraSansExtraCondensed-R": "helv",
    "FiraSansCondensed-Bold": "hebo",
    "FiraSans-Light": "helv",
    "FiraSans-ExtraBold": "hebo",
    "FiraSans-Regular": "helv",
    "RockwellStd-Bold": "tibo",
    "RockwellStd-Light": "tiro",
    "BECAGHeavy": "hebo",
    "Urbane-Bold": "hebo",
    "ITCAvantGardeStd-BoldCn": "hebo",
    "HelveticaNeue-CondensedB": "hebo",
}
RED = (0.71, 0.19, 0.16)
BLUE = (0.14, 0.30, 0.60)
BLACK = (0.14, 0.12, 0.13)


def _alias(name: str) -> str:
    return f"TG{list(FACES).index(name)}"


def make(path: str) -> str:
    doc = pymupdf.open()
    for _ in range(4):
        doc.new_page(width=612, height=792)
    pages = [doc[i] for i in range(4)]
    for pg in pages:
        for name, base in FACES.items():
            pg.insert_font(fontname=_alias(name), fontbuffer=pymupdf.Font(base).buffer)

    def t(pg, x, y, text, face, size, color=BLACK, rotate=0):
        pg.insert_text((x, y), text, fontname=_alias(face), fontsize=size, color=color, rotate=rotate)

    def pic(pg, rect, rgb):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 30), False)
        pix.set_rect(pix.irect, rgb)
        for k in range(0, 40, 4):                     # not one flat colour
            pix.set_rect(pymupdf.IRect(k, 0, k + 2, 30), (255, 255, 255))
        pg.insert_image(pymupdf.Rect(rect), pixmap=pix)

    def foot(pg, num, left_number):
        t(pg, 87, 760, "© Benchmark Education Company LLC", "FiraSans-Light", 8)
        t(pg, 480, 760, "Unit 15 • Rr", "FiraSans-Light", 8)
        t(pg, 40 if left_number else 560, 762, str(num), "FiraSans-ExtraBold", 14)

    # ---- 1 cover
    p = pages[0]
    pic(p, (36, 30, 80, 72), (40, 90, 200))
    t(p, 86, 58, "P H O N I C S", "Urbane-Bold", 17.9, RED)
    p.draw_rect(pymupdf.Rect(380, 20, 600, 120), color=None, fill=BLUE)
    t(p, 392, 40, "Technology and Society", "HelveticaNeue-CondensedB", 14, (1, 1, 1))
    t(p, 540, 40, "Level 1", "HelveticaNeue-CondensedB", 14, (1, 1, 1))
    t(p, 392, 70, "UNIT", "ITCAvantGardeStd-BoldCn", 16, (1, 1, 1))
    t(p, 392, 108, "15", "ITCAvantGardeStd-BoldCn", 34, (1, 1, 1))
    t(p, 450, 108, "Rr", "ITCAvantGardeStd-BoldCn", 34, (1, 1, 1))
    t(p, 36, 180, "Teacher’s Guide", "FiraSans-Bold", 42)
    pic(p, (36, 200, 576, 700), (200, 120, 60))

    # ---- 2 contents
    p = pages[1]
    t(p, 156, 180, "Table of Contents", "FiraSans-Bold", 34)
    for k in range(1, 4):
        t(p, 156, 220 + 40 * k, f"Lesson {k}  . . . . . . . . . . . . . . . . . . . . . . . {2 + 4 * (k - 1)}",
          "FiraSans-Regular", 14)

    # ---- 3 a lesson's first page
    p = pages[2]
    t(p, 36, 50, "Lesson 1", "RockwellStd-Light", 30, RED)
    t(p, 150, 50, "(20–30 MIN.)", "RockwellStd-Bold", 10, RED)
    t(p, 360, 40, "Access the Phonics Intervention library for", "FiraSansExtraCondensed-B", 10)
    t(p, 360, 52, "interactive lesson support and enhancements.", "FiraSansExtraCondensed-B", 10)
    p.draw_rect(pymupdf.Rect(36, 70, 140, 90), color=None, fill=RED)
    t(p, 54, 85, "Materials", "RockwellStd-Bold", 13, (1, 1, 1))
    pic(p, (36, 96, 130, 210), (60, 160, 90))
    t(p, 36, 222, "Student Book, Unit 15", "FiraSansExtraCondensed-B", 10)
    t(p, 36, 250, "Additional Materials", "FiraSansExtraCondensed-B", 12, RED)
    for k, item in enumerate(["Sound-Spelling Card Rr", "Student Observation Form", "Letter Cards"]):
        t(p, 36, 270 + 17 * k, "• " + item, "FiraSansExtraCondensed-B", 10)
    p.draw_line((200, 70), (200, 740), color=(0.7, 0.7, 0.7), width=0.5)
    t(p, 214, 85, "Warm-Up Review and Repetition (5 MIN.)", "RockwellStd-Bold", 17, RED)
    t(p, 214, 112, "Reread to Build Fluency", "FiraSansCondensed-ExtraB", 14)
    t(p, 214, 128, "Have students whisper-read the story from Lesson 9 to build fluency.", "FiraSansExtraCondensed-R", 11)
    # a ruled box of three columns
    box = pymupdf.Rect(214, 140, 576, 250)
    p.draw_rect(box, color=(0.3, 0.3, 0.3), width=0.6)
    for x in (334, 455):
        p.draw_line((x, 140), (x, 250), color=(0.3, 0.3, 0.3), width=0.6)
    cols = [("Provide Corrective", "Feedback", ["Circulate, listen in, and", "provide corrective feedback."]),
            ("Use Student", "Observation Form", ["Note high-frequency words", "for students who need help."]),
            ("Extend Practice", "", ["Include and highlight these", "words in the next lessons."])]
    for k, (h1, h2, body) in enumerate(cols):
        x = 220 + 121 * k
        t(p, x, 158, h1, "FiraSansExtraCondensed-B", 13)
        y = 158
        if h2:
            y += 15
            t(p, x, y, h2, "FiraSansExtraCondensed-B", 13)
        y += 18
        for ln in body:
            t(p, x, y, ln, "FiraSansExtraCondensed-R", 11)
            y += 14
    # a blending grid
    t(p, 214, 280, "Blend Words", "FiraSansCondensed-ExtraB", 14)
    t(p, 214, 296, "Model how to blend words with /r/ spelled r.", "FiraSansExtraCondensed-R", 11)
    p.draw_rect(pymupdf.Rect(214, 304, 440, 420), color=None, fill=(0.85, 0.93, 0.98))
    for r, row in enumerate([["r", "b", "h"], ["ran", "man", "pan"], ["_____", "_____", "_____"]]):
        for c, w in enumerate(row):
            t(p, 222 + 60 * c, 322 + 18 * r, w, "BECAGHeavy", 12)
    t(p, 222, 390, "My cat can run.", "BECAGHeavy", 12)
    t(p, 222, 406, "I see a rock and a robot.", "BECAGHeavy", 12)
    t(p, 596, 300, "Lesson 1", "FiraSans-Bold", 14, (1, 1, 1), rotate=90)
    foot(p, 2, True)

    # ---- 4 a booster box and a decision box
    p = pages[3]
    p.draw_rect(pymupdf.Rect(36, 60, 576, 230), color=RED, width=1, dashes="[3] 0")
    t(p, 50, 85, "Vocabulary Booster (10 MIN.)", "RockwellStd-Bold", 17, RED)
    t(p, 50, 108, "Read Aloud: “Robots in Real Life”", "FiraSansExtraCondensed-B", 13)
    for k, item in enumerate(["Display the pictures on the Read-Aloud Card as you read.",
                              "Use the prompts to highlight the words metallic and patient.",
                              "Ask students to share their wonderings and questions."]):
        t(p, 50, 130 + 18 * k, "• " + item, "FiraSansExtraCondensed-R", 11)
    t(p, 50, 300, "Make Informed Decisions", "FiraSans-Bold", 14, BLUE)
    t(p, 50, 320, "Use the assessments to decide which skills need more focus in upcoming lessons.",
      "FiraSansExtraCondensed-R", 14)
    foot(p, 3, False)

    tmp = path + ".tmp.pdf"
    doc.save(tmp)
    doc.close()
    # the faces get the template's names (what a reader of the file sees)
    pdf = pikepdf.open(tmp)
    names = {_alias(n): n for n in FACES}
    made: dict = {}

    def copy(d):
        return pdf.make_indirect(pikepdf.Dictionary({k: v for k, v in d.items()}))
    for page in pdf.pages:
        fonts = page.Resources.get("/Font")
        if fonts is None:
            continue
        for key in list(fonts.keys()):
            want = names.get(str(key)[1:])
            if not want:
                continue
            if want not in made:
                # a font object of its own for each face (the built-in ones are shared)
                f = copy(fonts[key])
                f.BaseFont = pikepdf.Name("/" + want)
                if "/DescendantFonts" in f:
                    dfs = []
                    for df in f.DescendantFonts:
                        nd = copy(df)
                        nd.BaseFont = pikepdf.Name("/" + want)
                        if "/FontDescriptor" in nd:
                            fd = copy(nd.FontDescriptor)
                            fd.FontName = pikepdf.Name("/" + want)
                            nd.FontDescriptor = fd
                        dfs.append(nd)
                    f.DescendantFonts = pikepdf.Array(dfs)
                if "/FontDescriptor" in f:
                    fd = copy(f.FontDescriptor)
                    fd.FontName = pikepdf.Name("/" + want)
                    f.FontDescriptor = fd
                made[want] = f
            fonts[key] = made[want]
    pdf.save(path)
    pdf.close()
    os.remove(tmp)
    return path


if __name__ == "__main__":
    print(make(sys.argv[1] if len(sys.argv) > 1 else "/tmp/tg_sample.pdf"))
