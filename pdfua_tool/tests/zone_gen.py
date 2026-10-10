"""Files for the zone-learning tests: a publisher whose paragraphs are set apart by a small gap
(no indent, and a paragraph does not end with a full stop), so the analysis' own rule — a gap
over 0.62 em, or a short last line that ends with a full stop — cannot see where one ends."""
from __future__ import annotations

import random

import pymupdf

WORDS = ("accessible documents let assistive technology present content in the order the author intended "
         "structure describes headings paragraphs lists and tables independently of how the page happens to be "
         "drawn so the same file can be read aloud reflowed or navigated by heading and every reader hears "
         "the same thing whatever the device").split()


def _wrap(text: str, width: float, size: float, font: str = "helv") -> list[str]:
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if pymupdf.get_text_length(t, fontname=font, fontsize=size) <= width:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def make_pub(path: str, seed: int = 1, pages: int = 2, per_page: int = 6, gap_pt: float = 4.0,
             indent: float = 0.0, size: float = 10.0, title: str = "A publisher's book",
             pitch: float = 12.5) -> list[list[tuple]]:
    """Writes the PDF; returns, for every page, the true paragraphs as display rectangles
    [(x0, y0, x1, y1, first words)]."""
    rnd = random.Random(seed)
    d = pymupdf.open()
    truth: list[list[tuple]] = []
    for pno in range(pages):
        p = d.new_page(width=595, height=842)
        y = 90.0
        if pno == 0:
            p.insert_text((60, 60), title, fontsize=18, fontname="hebo")
        out = []
        for k in range(per_page):
            n_words = rnd.randint(34, 64)
            text = " ".join(rnd.choice(WORDS) for _ in range(n_words)).capitalize() + " (see page %d)" % (k + 3)
            lines = _wrap(text, 470 - indent, size)
            top = y
            for i, ln in enumerate(lines):
                x = 60 + (indent if i == 0 else 0)
                p.insert_text((x, y + size), ln, fontsize=size, fontname="helv")
                y += pitch
            out.append((60.0, top, 530.0, y - (pitch - size) + 0.5, text[:30]))
            y += gap_pt
        truth.append(out)
        p.insert_text((290, 815), str(pno + 1), fontsize=8, fontname="helv")
    d.save(path)
    d.close()
    return truth
