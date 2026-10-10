"""A table that runs over several pages must become ONE Table tag.

Header row = TH on the first page only; the header printed again on the next
pages and the repeated page title are artifacts. A new title starts a new table."""
import os
import sys
import tempfile

import pymupdf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.analyzer import analyze          # noqa: E402
from core.options import Options           # noqa: E402

HEAD = ["Statement", "Expectation", "Breakout", "Guide", "Book"]
XS = [40, 150, 260, 370, 480, 570]


def draw_page(page, title, rows):
    page.insert_text((40, 30), title, fontsize=14, fontname="hebo")
    y0 = 50
    grid = [HEAD] + rows
    for r, row in enumerate(grid):
        for c, txt in enumerate(row):
            rect = pymupdf.Rect(XS[c], y0 + r * 40, XS[c + 1], y0 + (r + 1) * 40)
            page.draw_rect(rect, color=(0, 0, 0), width=0.6)
            page.insert_textbox(rect + (3, 3, -3, -3), txt, fontsize=8,
                                fontname="hebo" if r == 0 else "helv")
    page.insert_text((40, 830), "footer", fontsize=8)


def make(path):
    d = pymupdf.open()
    for i in range(3):
        draw_page(d.new_page(), "Level 1 Alignment", [[f"s{i}{r}{c}" for c in range(5)] for r in range(5)])
    for i in range(2):
        draw_page(d.new_page(), "Level 2 Alignment", [[f"t{i}{r}{c}" for c in range(5)] for r in range(5)])
    d.save(path)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        pdf = os.path.join(tmp, "mp.pdf")
        make(pdf)
        m = analyze(pdf, Options())
        nodes = m.ordered_nodes()
        tables = [n for n in nodes if n.role == "Table"]
        assert len(tables) == 2, f"expected 2 tables, got {len(tables)}"
        ths = [n for n in nodes if n.role == "TH"]
        assert len(ths) == 10 and {n.page for n in ths} == {0, 3}, "TH only on the first page of each table"
        heads = [n for n in nodes if n.role.startswith("H") and n.role[1:].isdigit()]
        assert [n.page for n in heads] == [0, 3], "one heading per table"
        arts = [n for n in nodes if n.artifact and n.pagination_subtype == "Header"]
        assert len(arts) == 6, f"running heads missing ({len(arts)})"
        # the rows sit in THead / TBody (table_thead_tbody, on by default)
        rows = [n for n in m.descendants(tables[0].id) if n.role == "TR"]
        assert len(rows) == 1 + 5 * 3, f"rows: {len(rows)}"
        off = analyze(pdf, Options(merge_continued_tables=False))
        assert len([n for n in off.nodes.values() if n.role == "Table"]) == 5
    print("multipage table test: OK")


if __name__ == "__main__":
    main()
