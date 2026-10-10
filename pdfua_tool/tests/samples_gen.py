"""Files for samples_test.py: a client ("Journal of Testing") whose section
heads are set in capitals at the body size — the analysis reads them as
paragraphs — and whose pages carry a running foot."""
from __future__ import annotations

import pymupdf

BODY = ("The study looked at how the samples behave over a long time and found that most of them "
        "stayed stable while a few changed in ways that matter for the next steps of the work.")


def make(path: str, heads: list[list[str]], volume: int = 3, title: str = "A study of stable samples") -> None:
    d = pymupdf.open()
    for pno, page_heads in enumerate(heads):
        p = d.new_page(width=595, height=842)
        y = 80
        if pno == 0:
            p.insert_text((60, y), title, fontsize=18, fontname="hebo")
            y += 40
        for h in page_heads:
            p.insert_text((60, y), h, fontsize=10, fontname="helv")
            y += 26
            r = pymupdf.Rect(60, y - 10, 535, y + 70)
            p.insert_textbox(r, BODY + " " + BODY, fontsize=10, fontname="helv")
            y += 100
        p.insert_text((60, 815), f"Journal of Testing · Volume {volume} · page {pno + 1}", fontsize=7.5, fontname="helv")
    d.save(path)
    d.close()


HEADS = [["KEY POINTS", "BACKGROUND", "METHODS"], ["RESULTS", "DISCUSSION", "KEY POINTS"], ["LIMITATIONS", "CONCLUSION"]]
TARGET = [["KEY POINTS", "SUMMARY", "FINDINGS", "NEXT STEPS"]]


def make_tagged(path: str, tmp: str, heads=None) -> None:
    """The client's own finished file: made with the tool, heads set to H2."""
    import os

    from core import compat
    from core.build import build
    from core.options import profile_options
    from core.pipeline import load_model, process_file, save_model
    src = os.path.join(tmp, "sample_src.pdf")
    make(src, heads or HEADS)
    o = profile_options("Default")
    o.ai_alt_text_auto = False
    o.run_verapdf = False
    o.use_samples = False
    job = os.path.join(tmp, "samplejob")
    process_file(src, job, o)
    compat.use(o)
    m = load_model(job)
    for n in m.nodes.values():
        if n.role == "P" and n.text.isupper():
            n.role = "H2"
    save_model(job, m)
    build(m, os.path.join(job, "working.pdf"), path, o)
