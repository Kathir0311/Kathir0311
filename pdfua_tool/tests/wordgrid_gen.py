"""Test files: a phonics word grid (V4.21.2) and a two-column list."""
import pymupdf

ROW1 = ["litch", "mudge", "vux", "quam", "cep"]
ROW2 = ["gen", "knaz", "wrop", "saten", "qui"]
XS = [110, 215, 305, 410, 500]


def make_grid(path: str) -> None:
    d = pymupdf.open()
    p = d.new_page(width=612, height=792)
    p.insert_text((60, 60), "Phonics Task Form", fontsize=16, fontname="hebo")
    p.insert_text((60, 100), "Read each word aloud. Mark the words read correctly.", fontsize=11)
    p.insert_text((100, 150), "8.", fontsize=16)
    p.draw_circle((122, 185), 14, color=(0.5, 0.5, 0.5), fill=(0.6, 0.6, 0.6))
    p.insert_text((116, 191), "A", fontsize=16, color=(1, 1, 1))
    for y, row in ((240, ROW1), (290, ROW2)):
        for x, w in zip(XS, row):
            p.insert_text((x, y), w, fontsize=16)
    p.draw_line((100, 320), (560, 320), color=(0.7, 0.7, 0.7))
    p.insert_text((60, 360), "Score: count the words read correctly in one minute.", fontsize=11)
    d.save(path)
    d.close()


PAIRS = [("duck", "The duck is swimming on the pond."), ("wing", "The bird's wing was broken."),
         ("ship", "The big ship is on the ocean."), ("long", "Michael took a long trip."),
         ("pack", "Pack a lunch for the picnic!")]


def make_pairs(path: str) -> None:
    d = pymupdf.open()
    p = d.new_page(width=612, height=792)
    p.insert_text((60, 60), "Word practice", fontsize=16, fontname="hebo")
    y = 100
    for k, (w, s) in enumerate(PAIRS, 1):
        p.insert_text((60, y), f"{k}. {w}", fontsize=14)
        p.insert_text((150, y), s, fontsize=14)
        y += 30
    p.insert_text((60, y + 20), "Read them again tomorrow.", fontsize=11)
    d.save(path)
    d.close()
