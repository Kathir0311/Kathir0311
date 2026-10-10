"""Generate a varied corpus of untagged test PDFs."""
from __future__ import annotations

import os

import pymupdf

OUT = os.path.join(os.path.dirname(__file__), "corpus")
LOREM = ("Accessible documents let assistive technology present content in the "
         "order the author intended. Structure describes headings, paragraphs, lists "
         "and tables independently of how the page happens to be drawn, so the same "
         "file can be read aloud, reflowed or navigated by heading. ")


def para(page, x, y, w, text, fs=9.5, font="helv", color=(0, 0, 0)) -> float:
    r = pymupdf.Rect(x, y, x + w, y + 400)
    left = page.insert_textbox(r, text, fontsize=fs, fontname=font, color=color, align=0)
    used = 400 - left if left >= 0 else 400
    return y + used + fs * 0.6


# ---------------------------------------------------------------- article
def article(path):
    doc = pymupdf.open()
    W, H = 595, 842
    pending = []
    for pno in range(3):
        page = doc.new_page(width=W, height=H)
        page.insert_text((50, 34), "Journal of Accessible Testing · Volume 4", fontsize=7.5, fontname="helv")
        page.insert_text((W - 60, 818), str(pno + 1), fontsize=8, fontname="helv")
        if pno == 0:
            page.insert_text((50, 80), "Structure First: Tagging PDFs at Scale", fontsize=19, fontname="hebo")
            page.insert_text((50, 100), "K. Author and A. Reviewer", fontsize=10, fontname="heit")
            page.insert_text((50, 128), "Abstract", fontsize=11, fontname="hebo")
            y = para(page, 50, 134, 495, LOREM + "We report on a tool that tags documents automatically.", 9.5)
            # two columns
            colw, gap = 237, 21
            yl = y + 10
            page.insert_text((50, yl + 10), "1 Introduction", fontsize=12, fontname="hebo")
            yl = para(page, 50, yl + 16, colw, LOREM * 2)
            page.insert_text((50, yl + 12), "1.1 Background", fontsize=10.5, fontname="hebo")
            yl = para(page, 50, yl + 18, colw, LOREM + "The survey1 covered 400 files.")
            # nested list in left column
            items = [(0, "• Headings must not skip levels."),
                     (0, "• Figures need alternative text:"),
                     (1, "◦ describe the content, not the look"),
                     (1, "◦ keep it short"),
                     (0, "• Tables need header cells.")]
            for lvl, t in items:
                page.insert_text((56 + 14 * lvl, yl + 10), t, fontsize=9.5, fontname="helv")
                yl += 14
            # right column
            xr = 50 + colw + gap
            yr = y + 10
            yr = para(page, xr, yr, colw, LOREM * 2)
            page.insert_text((xr, yr + 12), "2 Method", fontsize=12, fontname="hebo")
            yr = para(page, xr, yr + 18, colw, "Details are available at the project website "
                      "and in the appendix. " + LOREM)
            # link on "project website" (approximate location: search)
            for r in page.search_for("project website"):
                page.insert_link({"kind": pymupdf.LINK_URI, "from": r, "uri": "https://example.org/project"})
            pending = [r for r in page.search_for("appendix")]
            # footnote
            page.draw_line((50, 772), (150, 772), width=0.5)
            page.insert_text((50, 784), "1 The survey is described in the supplementary material.", fontsize=7, fontname="helv")
        elif pno == 1:
            page.insert_text((50, 70), "3 Results", fontsize=12, fontname="hebo")
            y = para(page, 50, 78, 495, LOREM)
            page.insert_text((50, y + 14), "Table 1. Sales by region and year.", fontsize=9, fontname="heit")
            # table with merged header cells
            x = [50, 180, 300, 420]
            yy = [y + 22, y + 42, y + 62, y + 82, y + 102]
            L = lambda a, b: page.draw_line(a, b, width=0.7)
            L((x[0], yy[0]), (x[3], yy[0]))
            L((x[1], yy[1]), (x[3], yy[1]))
            for k in (2, 3, 4):
                L((x[0], yy[k]), (x[3], yy[k]))
            L((x[0], yy[0]), (x[0], yy[4]))
            L((x[1], yy[0]), (x[1], yy[4]))
            L((x[2], yy[1]), (x[2], yy[4]))
            L((x[3], yy[0]), (x[3], yy[4]))
            page.insert_text((x[0] + 6, yy[0] + 25), "Region", fontsize=9, fontname="hebo")
            page.insert_text((x[1] + 80, yy[0] + 14), "Sales", fontsize=9, fontname="hebo")
            page.insert_text((x[1] + 6, yy[1] + 14), "2025", fontsize=9, fontname="hebo")
            page.insert_text((x[2] + 6, yy[1] + 14), "2026", fontsize=9, fontname="hebo")
            for r, (a, b, c) in enumerate([("North", "12", "15"), ("South", "9", "11")]):
                page.insert_text((x[0] + 6, yy[2 + r] + 14), a, fontsize=9, fontname="helv")
                page.insert_text((x[1] + 6, yy[2 + r] + 14), b, fontsize=9, fontname="helv")
                page.insert_text((x[2] + 6, yy[2 + r] + 14), c, fontsize=9, fontname="helv")
            # vector bar chart
            cy = yy[4] + 40
            page.draw_line((70, cy + 150), (330, cy + 150), width=1)
            page.draw_line((70, cy), (70, cy + 150), width=1)
            for k, (h, lab) in enumerate([(90, "Q1"), (120, "Q2"), (60, "Q3"), (140, "Q4")]):
                bx = 90 + k * 60
                page.draw_rect(pymupdf.Rect(bx, cy + 150 - h, bx + 36, cy + 150),
                               color=None, fill=(0.2, 0.4, 0.8))
                page.insert_text((bx + 10, cy + 162), lab, fontsize=7, fontname="helv")
            page.insert_text((70, cy + 182), "Figure 1. Quarterly sales across all regions.",
                             fontsize=9, fontname="heit")
            para(page, 50, cy + 196, 495, LOREM)
        else:
            page.insert_text((50, 70), "4 Discussion", fontsize=12, fontname="hebo")
            y = para(page, 50, 78, 495, LOREM * 3)
            page.insert_text((50, y + 16), "Appendix", fontsize=12, fontname="hebo")
            para(page, 50, y + 24, 495, LOREM)
    for r in pending:
        doc[0].insert_link({"kind": pymupdf.LINK_GOTO, "from": r, "page": 2, "to": pymupdf.Point(50, 60)})
    doc.set_metadata({"title": ""})
    doc.save(path)


# ------------------------------------------------------------------ book
def book(path):
    doc = pymupdf.open()
    W, H = 432, 648
    chapters = [("1 Getting Started", ["1.1 Installing", "1.2 First run"]),
                ("2 Tagging", ["2.1 Headings", "2.2 Lists"])]
    toc = []
    # page 1: title, page 2: contents
    p = doc.new_page(width=W, height=H)
    p.insert_text((50, 200), "Accessible Publishing", fontsize=24, fontname="tibo")
    p.insert_text((50, 230), "A practical handbook", fontsize=13, fontname="tiit")
    p = doc.new_page(width=W, height=H)
    p.insert_text((50, 80), "Contents", fontsize=16, fontname="tibo")
    y = 110
    lines = [("1 Getting Started", 3), ("1.1 Installing", 3), ("1.2 First run", 4),
             ("2 Tagging", 5), ("2.1 Headings", 5), ("2.2 Lists", 6)]
    for t, pg in lines:
        indent = 14 if "." in t.split()[0] else 0
        dots = "." * max(3, 60 - len(t) - indent // 3)
        p.insert_text((50 + indent, y), f"{t} {dots} {pg}", fontsize=10, fontname="tiro")
        y += 16
    toc.append([1, "Contents", 2])
    pageno = 2
    carry = ""
    for ci, (ch, secs) in enumerate(chapters):
        for si, sec in enumerate(secs):
            p = doc.new_page(width=W, height=H)
            pageno += 1
            run = "Accessible Publishing" if pageno % 2 == 0 else ch[2:]
            p.insert_text((50, 30), run, fontsize=7.5, fontname="tiit")
            p.insert_text((W / 2 - 4, 625), str(pageno), fontsize=8, fontname="tiro")
            y = 70
            if carry:
                y = para(p, 50, y - 8, 330, carry, 10.5, "tiro") + 14
                carry = ""
            if si == 0:
                p.insert_text((50, y), ch, fontsize=18, fontname="tibo")
                toc.append([1, ch, pageno])
                y += 30
            p.insert_text((50, y), sec, fontsize=13, fontname="tibo")
            toc.append([2, sec, pageno])
            y += 8
            y = para(p, 50, y + 4, 330, LOREM * 2, 10.5, "tiro")
            # last paragraph deliberately breaks mid-sentence across pages
            text = LOREM * 7
            r = pymupdf.Rect(50, y + 4, 380, 600)
            words = text.split()
            fit = []
            for k in range(len(words), 0, -1):
                trial = " ".join(words[:k])
                tmp = pymupdf.open()
                tp = tmp.new_page(width=W, height=H)
                if tp.insert_textbox(r, trial, fontsize=10.5, fontname="tiro") >= 0:
                    fit = words[:k]
                    break
            p.insert_textbox(r, " ".join(fit), fontsize=10.5, fontname="tiro")
            rest = " ".join(words[len(fit):])
            carry = rest[0].lower() + rest[1:] if rest else ""
    doc.set_toc(toc)
    doc.save(path)


# ----------------------------------------------------------------- forms
def forms(path):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 70), "Registration form", fontsize=18, fontname="hebo")
    page.insert_text((50, 110), "Full name:", fontsize=10, fontname="helv")
    page.insert_text((50, 140), "Email address:", fontsize=10, fontname="helv")
    page.insert_text((70, 172), "Subscribe to the newsletter", fontsize=10, fontname="helv")
    for name, rect, kind in [("Text1", pymupdf.Rect(140, 98, 400, 114), pymupdf.PDF_WIDGET_TYPE_TEXT),
                             ("Text2", pymupdf.Rect(140, 128, 400, 144), pymupdf.PDF_WIDGET_TYPE_TEXT),
                             ("Check1", pymupdf.Rect(50, 162, 62, 174), pymupdf.PDF_WIDGET_TYPE_CHECKBOX)]:
        w = pymupdf.Widget()
        w.field_type = kind
        w.field_name = name
        w.rect = rect
        if kind == pymupdf.PDF_WIDGET_TYPE_TEXT:
            w.text_font = "Helv"
            w.text_fontsize = 10
        page.add_widget(w)
    page.add_text_annot((450, 70), "Reviewer: check the field labels")
    doc.save(path)


# -------------------------------------------------------------- scanned
def scanned(path, src_article):
    src = pymupdf.open(src_article)
    pix = src[0].get_pixmap(dpi=200, clip=pymupdf.Rect(0, 40, 595, 300))
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=260)
    page.insert_image(page.rect, pixmap=pix)
    doc.save(path)


# ---------------------------------------------------- forms xobj + OC
def formxobj(path, src_article):
    src = pymupdf.open(src_article)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 50), "Placed content below", fontsize=14, fontname="hebo")
    page.show_pdf_page(pymupdf.Rect(40, 70, 555, 800), src, 0)
    oc = doc.add_ocg("Reviewer notes", on=True)
    page.insert_text((60, 830), "Layer: internal review copy", fontsize=8, fontname="helv", oc=oc)
    doc.save(path)


# -------------------------------------------------------- unicode/Tamil
def unicode_doc(path):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    ff = "/usr/share/fonts/truetype/freefont/FreeSans.ttf"
    page.insert_font(fontname="FS", fontfile=ff)
    page.insert_font(fontname="FSB", fontfile="/usr/share/fonts/truetype/freefont/FreeSansBold.ttf")
    page.insert_text((50, 80), "Multilingual sample", fontsize=18, fontname="FSB")
    y = para(page, 50, 100, 495, LOREM, 10, "FS")
    page.insert_text((50, y + 16), "தமிழ் மொழி ஒரு செம்மொழி ஆகும்", fontsize=12, fontname="FS")
    page.insert_text((50, y + 40), "Ελληνικά κείμενα για δοκιμή", fontsize=12, fontname="FS")
    para(page, 50, y + 56, 495, "Back to English: " + LOREM, 10, "FS")
    doc.save(path)


# -------------------------------------------------------------- contrast
def contrast_doc(path):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 34), "Contrast test document · running head", fontsize=7.5, fontname="helv",
                     color=(0.72, 0.72, 0.72))
    page.insert_text((50, 80), "Colour contrast samples", fontsize=18, fontname="hebo", color=(0, 0, 0))
    page.insert_text((50, 120), "Light grey body text on white fails the 4.5:1 minimum.", fontsize=11,
                     fontname="helv", color=(0.6, 0.6, 0.6))
    page.insert_text((50, 145), "Dark blue body text on white passes easily.", fontsize=11,
                     fontname="helv", color=(0.05, 0.15, 0.45))
    page.insert_text((50, 180), "Large grey heading passes at 3:1", fontsize=20, fontname="hebo",
                     color=(0.55, 0.55, 0.55))
    page.draw_rect(pymupdf.Rect(45, 200, 400, 230), color=None, fill=(1.0, 0.93, 0.55))
    page.insert_text((52, 220), "White text on a pale yellow box fails badly.", fontsize=11,
                     fontname="helv", color=(1, 1, 1))
    page.draw_rect(pymupdf.Rect(45, 240, 400, 270), color=None, fill=(0.05, 0.1, 0.3))
    page.insert_text((52, 260), "White text on a dark navy box passes.", fontsize=11,
                     fontname="helv", color=(1, 1, 1))
    page.insert_text((50, 300), "Light CMYK text in a print file fails.", fontsize=11,
                     fontname="helv", color=(0.0, 0.0, 0.0, 0.45))
    page.insert_text((50, 325), "Pale DeviceGray text fails too.", fontsize=11,
                     fontname="helv", color=(0.62,))
    page.draw_rect(pymupdf.Rect(45, 340, 400, 370), color=None, fill=(0.2, 0.6, 0.2))
    page.insert_text((52, 360), "Red text on a green box is hard to read.", fontsize=11,
                     fontname="helv", color=(0.85, 0.2, 0.2))
    # text over a photo-like gradient
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 60))
    for x in range(300):
        v = int(40 + 200 * x / 299)
        pix.set_rect(pymupdf.IRect(x, 0, x + 1, 60), (v, v, int(v * 0.9)))
    page.insert_image(pymupdf.Rect(45, 390, 345, 450), pixmap=pix)
    page.insert_text((52, 425), "Caption over a photograph", fontsize=12, fontname="hebo", color=(1, 1, 1))
    page.insert_text((50, 490), "Normal black paragraph text for reference.", fontsize=11, fontname="helv")
    page.insert_text((295, 800), "1", fontsize=9, fontname="helv", color=(0.75, 0.75, 0.75))
    doc.save(path)


def tables_doc(path):
    """Ruled table with empty cells (NVDA must announce them)."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 70), "Attendance register", fontsize=16, fontname="hebo")
    xs = [50, 170, 270, 370, 470]
    ys = [90 + 22 * k for k in range(6)]
    for y in ys:
        page.draw_line((xs[0], y), (xs[-1], y), width=0.7)
    for x in xs:
        page.draw_line((x, ys[0]), (x, ys[-1]), width=0.7)
    data = [["Name", "Mon", "Tue", "Wed"],
            ["Asha", "Present", "", "Present"],
            ["Bala", "", "", "Absent"],
            ["Chitra", "Present", "Present", ""],
            ["Dinesh", "", "Absent", "Present"]]
    for r, row in enumerate(data):
        for c, t in enumerate(row):
            if t:
                page.insert_text((xs[c] + 6, ys[r] + 15), t, fontsize=9.5,
                                 fontname="hebo" if r == 0 or c == 0 else "helv")
    doc.save(path)


def main():
    os.makedirs(OUT, exist_ok=True)
    a = os.path.join(OUT, "article.pdf")
    article(a)
    book(os.path.join(OUT, "book.pdf"))
    forms(os.path.join(OUT, "forms.pdf"))
    scanned(os.path.join(OUT, "scanned.pdf"), a)
    formxobj(os.path.join(OUT, "formxobj.pdf"), a)
    unicode_doc(os.path.join(OUT, "unicode.pdf"))
    contrast_doc(os.path.join(OUT, "contrast.pdf"))
    tables_doc(os.path.join(OUT, "tables.pdf"))
    print("corpus:", sorted(os.listdir(OUT)))


if __name__ == "__main__":
    main()
