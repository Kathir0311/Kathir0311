"""List items with a second text in a column of its own (V4.21.4).

A spelling form: "1. city" on the left and, at a tab stop on the right, the
sentence "Traffic is heavy in the city." — printed so the sentences are a
text column apart from the numbered words. In every engine:

* each item is Lbl "1." + LBody > P "city" + P "Traffic is heavy in the city."
  (no sentence left as a paragraph outside the list, two sentences never in
  one paragraph)
* a list zone drawn over both columns, and one drawn over the words only,
  give the same items; no text is dropped from the tags
* a list with a paragraph beside it in another column is left alone
* the built file is sound
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymupdf  # noqa: E402

from core import compat, edit  # noqa: E402
from core.build import build  # noqa: E402
from core.options import profile_options  # noqa: E402
from core.pipeline import load_model, process_file  # noqa: E402
from tests.verify import mc_balance, structure_integrity  # noqa: E402

ROWS = [("city", "Traffic is heavy in the city."), ("box", "The big box was heavy to carry."),
        ("knelt", "He knelt down to pick up the pebble."), ("ledge", "Don't stand too close to the ledge."),
        ("fix", "The mechanic tried to fix the truck."), ("wreck", "Five cars got into a big wreck."),
        ("gnaw", "The puppy likes to gnaw on the furniture."), ("latch", "Please latch the door before you leave.")]


def make(path: str) -> None:
    d = pymupdf.open()
    p = d.new_page()
    p.insert_text((72, 60), "Spelling Task Form", fontsize=20)
    y = 100
    for k, (w, s) in enumerate(ROWS, 1):
        p.insert_text((90, y), f"{k}. {w}", fontsize=11)
        y += 20
    # the sentences: a text block of their own, at a tab stop
    tw = pymupdf.TextWriter(p.rect)
    y = 100
    for _, s in ROWS:
        tw.append((190, y), s, fontsize=11)
        y += 20
    tw.write_text(p)
    # a short list with a paragraph in the other column of the page
    p.insert_text((72, 300), "Notes", fontsize=16)
    for k, w in enumerate(["red", "blue", "green"], 1):
        p.insert_text((72, 330 + 18 * k), f"{k}. {w}", fontsize=11)
    for k, s in enumerate(["The colours are used in the charts of this report and",
                           "in the slides; each one has a meaning of its own that",
                           "the legend explains on the first page of each part."]):
        p.insert_text((330, 344 + 13 * k), s, fontsize=11)
    d.save(path)
    d.close()


def items_ok(m, bad: list, tag: str) -> None:
    lists = [n for n in m.nodes.values() if n.role == "L" and len([c for c in n.children if m.nodes[c].role == "LI"]) >= 6]
    if len(lists) != 1:
        bad.append(f"{tag}: {len(lists)} spelling list(s)")
        return
    L = lists[0]
    got = []
    for c in L.children:
        li = m.nodes[c]
        body = next(m.nodes[x] for x in li.children if m.nodes[x].role == "LBody")
        got.append(tuple(m.nodes[x].text for x in body.children if m.nodes[x].role == "P"))
    want = [(w, s) for w, s in ROWS]
    if got != want:
        bad.append(f"{tag}: items {got[:3]} …")
    loose = [n.text for n in m.nodes.values() if n.role == "P" and any(n.text == s for _, s in ROWS)
             and not any(a.role == "L" for a in m.ancestors(n.id))]
    if loose:
        bad.append(f"{tag}: sentences outside the list {loose[:2]}")
    other = [n for n in m.nodes.values() if "printed on its row" in (n.note or "") and "colours" in (n.text or "")]
    if other:
        bad.append(f"{tag}: the paragraph beside the colour list went into it")


def main() -> int:
    bad: list = []
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "form.pdf")
    make(src)
    for prof in ("Default", "Kriyadocs", "Academain"):
        o = profile_options(prof)
        o.ai_alt_text_auto = False
        o.run_verapdf = False
        o.use_samples = False
        job = os.path.join(tmp, prof)
        process_file(src, job, o)
        compat.use(o)
        m = load_model(job)
        items_ok(m, bad, prof)
        if prof == "Default":
            work = os.path.join(job, "working.pdf")
            text_before = sorted(" ".join((n.text or "").split()) for n in m.nodes.values() if (n.text or "").strip())
            for name, rect in (("zone over both", (80, 85, 450, 255)), ("zone over the words", (80, 85, 160, 255))):
                mz = load_model(job)
                edit.create_from_rect(mz, work, 0, list(rect), "L", opts=o)
                items_ok(mz, bad, f"{prof} {name}")
                mz.check_integrity()
                words = " ".join((n.text or "") for n in mz.nodes.values())
                if any(s not in words for _, s in ROWS):
                    bad.append(f"{name}: a sentence was dropped")
            out = os.path.join(job, "out.pdf")
            build(m, work, out, o)
            if structure_integrity(out) or mc_balance(out):
                bad.append(f"structure {structure_integrity(out)[:2]} {mc_balance(out)[:2]}")
            del text_before
    if bad:
        print("list columns test: FAILED")
        for x in bad:
            print("   ", x)
        return 1
    print("list columns test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
