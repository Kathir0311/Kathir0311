"""Build a small untagged PDF that exercises the pipeline."""
import pymupdf

OUT = "/home/claude/pdfua_tool/samples/untagged_sample.pdf"


def main() -> None:
    doc = pymupdf.open()

    for pno in range(2):
        page = doc.new_page(width=595, height=842)
        y = 60

        # running head (should become an artifact)
        page.insert_text((60, 40), "Journal of Applied Accessibility",
                         fontsize=8, fontname="helv")

        if pno == 0:
            page.insert_text((60, y), "Designing Accessible Documents",
                             fontsize=20, fontname="hebo")
            y += 36
            page.insert_text((60, y), "1. Introduction", fontsize=14, fontname="hebo")
            y += 24
            body = ("Accessible documents allow assistive technology to present "
                    "content in the order an author intended. A tagged PDF carries "
                    "a logical structure tree that describes headings, paragraphs, "
                    "lists and tables independently of how the page is drawn.")
            for line in _wrap(body, 78):
                page.insert_text((60, y), line, fontsize=10, fontname="helv")
                y += 14
            y += 10
            page.insert_text((60, y), "1.1 Why structure matters",
                             fontsize=11.5, fontname="hebo")
            y += 20
            for item in ("Screen readers follow the tag tree, not the visual layout.",
                         "Reflow on small screens depends on paragraph boundaries.",
                         "Tables need header cells so data can be announced in context."):
                page.insert_text((70, y), "• " + item, fontsize=10, fontname="helv")
                y += 16
            y += 14

            # a ruled table
            cols = [60, 200, 340, 480]
            rows = [y, y + 22, y + 44, y + 66]
            data = [["Element", "Purpose", "Required"],
                    ["H1-H6", "Navigation", "Yes"],
                    ["Figure", "Alternative text", "Yes"]]
            for r in rows:
                page.draw_line(pymupdf.Point(cols[0], r), pymupdf.Point(cols[-1], r))
            for c in cols:
                page.draw_line(pymupdf.Point(c, rows[0]), pymupdf.Point(c, rows[-1]))
            for ri, row in enumerate(data):
                for ci, cell in enumerate(row):
                    page.insert_text((cols[ci] + 6, rows[ri] + 15), cell,
                                     fontsize=9,
                                     fontname="hebo" if ri == 0 else "helv")
            y = rows[-1] + 30
        else:
            page.insert_text((60, y), "2. Remediation workflow",
                             fontsize=14, fontname="hebo")
            y += 24
            body = ("Automated tools can supply document metadata, tab order and a "
                    "first pass at the structure tree. Judgement calls such as "
                    "alternative text and reading order still need a person to "
                    "confirm them before the file is published.")
            for line in _wrap(body, 78):
                page.insert_text((60, y), line, fontsize=10, fontname="helv")
                y += 14
            y += 20

            # an image
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 120))
            pix.set_rect(pix.irect, (40, 90, 160))
            pix.set_rect(pymupdf.IRect(20, 20, 180, 60), (240, 200, 60))
            page.insert_image(pymupdf.Rect(60, y, 260, y + 120), pixmap=pix)
            y += 132
            page.insert_text((60, y), "Figure 1. Colour blocks used as a placeholder.",
                             fontsize=9, fontname="heit")

        # folio
        page.insert_text((295, 800), str(pno + 1), fontsize=9, fontname="helv")

    doc.save(OUT)
    doc.close()
    print("wrote", OUT)


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines


if __name__ == "__main__":
    main()
