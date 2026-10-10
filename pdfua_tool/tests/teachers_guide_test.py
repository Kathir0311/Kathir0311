"""The Academain Teacher's Guide structure (V4.24), on a small guide made in
the book's template (tests/tg_gen.py).

* the guide is recognised ("Teacher's Guide" + "Lesson N (20–30 MIN.)"); the
  client's other books and the test corpus are not
* the Academain profile (teachers_guide = auto) gives the client's structure:
  H1 the cover title · H2 Table of Contents and "Lesson 1 (20–30 MIN.)" ·
  H3 the parts (Materials, Warm-Up …, Vocabulary Booster) · H4 the sub-heads
  (Additional Materials, Reread to Build Fluency, Blend Words, Read Aloud …) ·
  H5 the heads of the ruled box's columns — the box is no table: head + P,
  column after column · each word of the blending grid a P ("ran", "man") ·
  a sentence one P · "_____" a P read "Blank line" · bullets L > LI > Lbl +
  LBody ("Sound-Spelling Card Rr" read "… uppercase R, lowercase r") · the
  page number a P read "Page 2", read last · the running foot and the lesson
  tab on the page edge artifacts · the unit badge one P "UNIT 15 Rr"
* the Default profile leaves the guide to the V4 engine (no change there)
* built: one Sect holds the whole book, the pages print exactly as before
"""
from __future__ import annotations

import glob
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main() -> int:
    from core import tguide
    from core.analyzer import analyze
    from core.options import Options, profile_options
    from tests.tg_gen import make
    bad: list = []

    def ok(cond, msg):
        if not cond:
            bad.append(msg)

    tmp = tempfile.mkdtemp()
    src = make(os.path.join(tmp, "guide.pdf"))
    ok(tguide.detect(src), "the guide is recognised")
    for p in glob.glob(os.path.join(ROOT, "tests", "corpus", "*.pdf")):
        ok(not tguide.detect(p), f"{os.path.basename(p)} is no Teacher's Guide")

    # the Default profile: the V4 engine as it was
    m0 = analyze(src, Options())
    ok("teachers_guide" not in m0.info, "Default profile: no Teacher's Guide pass")

    opts = profile_options("Academain")
    ok(opts.teachers_guide == "auto", "the Academain profile asks for it")
    m = analyze(src, opts)
    ok(bool(m.info.get("teachers_guide")), f"the pass ran {m.info.get('teachers_guide')}")
    nodes = [n for n in m.ordered_nodes() if not n.artifact]

    def role_of(text):
        for n in nodes:
            if " ".join((n.text or "").split()) == text:
                return n.role
        return None

    for text, role in [("Teacher’s Guide", "H1"), ("Table of Contents", "H2"), ("Lesson 1 (20–30 MIN.)", "H2"),
                       ("Materials", "H3"), ("Warm-Up Review and Repetition (5 MIN.)", "H3"),
                       ("Vocabulary Booster (10 MIN.)", "H3"),
                       ("Additional Materials", "H4"), ("Reread to Build Fluency", "H4"), ("Blend Words", "H4"),
                       ("Read Aloud: “Robots in Real Life”", "H4"),
                       ("Provide Corrective Feedback", "H5"), ("Use Student Observation Form", "H5"),
                       ("Extend Practice", "H5"),
                       ("Circulate, listen in, and provide corrective feedback.", "P"),
                       ("ran", "P"), ("man", "P"), ("My cat can run.", "P"), ("Technology and Society", "P"),
                       ("Student Book, Unit 15", "P"), ("UNIT 15 Rr", "P")]:
        ok(role_of(text) == role, f"{text!r}: {role_of(text)}, not {role}")
    ok(not any(n.role in ("Table", "TR", "TD", "TH") for n in nodes), "the ruled box is no table")
    ok(not any(n.role.startswith("H") and n.role[1:].isdigit() and int(n.role[1:]) == 6 for n in nodes), "no H6")
    blanks = [n for n in nodes if n.role == "P" and "____" in (n.text or "")]
    ok(len(blanks) == 3 and all(n.actual_text == "Blank line" for n in blanks), f"blank lines {[(n.text, n.actual_text) for n in blanks]}")
    body = next((n for n in nodes if n.role == "LBody" and (n.text or "").startswith("Sound-Spelling")), None)
    ok(body is not None and body.actual_text == "Sound-Spelling Card uppercase R, lowercase r",
       f"Rr read as letters: {body.actual_text if body else None}")
    lis = [n for n in nodes if n.role == "LI"]
    ok(len(lis) == 6 and all(any(m.nodes[c].role == "Lbl" for c in n.children) for n in lis), f"list items {len(lis)}")
    badge = next((n for n in nodes if n.text == "UNIT 15 Rr"), None)
    ok(badge is not None and badge.actual_text == "UNIT 15 uppercase R, lowercase r", "the unit badge")
    pn = [n for n in nodes if n.role == "P" and n.actual_text.startswith("Page ")]
    ok(sorted(n.actual_text for n in pn) == ["Page 2", "Page 3"], f"page numbers {[n.actual_text for n in pn]}")
    texts = " | ".join(n.text or "" for n in nodes)
    ok("Unit 15 • Rr" not in texts and "© Benchmark" not in texts, "the running foot is not read")
    ok(not any((n.text or "").strip() == "Lesson 1" for n in nodes), "the lesson tab is not read")
    ok(any(n.artifact and "Lesson 1" in (n.text or "") for n in m.nodes.values()), "the lesson tab is an artifact")
    mid = role_of("Make Informed Decisions")
    ok(mid in ("H2", "H3"), f"Make Informed Decisions {mid}")
    # reading order on the lesson page: lesson head, the access note, the
    # margin column, then the main column; the box column by column; the page
    # number last
    order = [" ".join((n.text or "").split()) for n in nodes if n.page == 2 and n.role != "Figure"]

    def at(t):
        return order.index(t) if t in order else -1
    seq = ["Lesson 1 (20–30 MIN.)", "Materials", "Additional Materials", "Warm-Up Review and Repetition (5 MIN.)",
           "Reread to Build Fluency", "Provide Corrective Feedback", "Circulate, listen in, and provide corrective feedback.",
           "Use Student Observation Form", "Extend Practice", "Blend Words", "ran", "man", "My cat can run.", "2"]
    pos = [at(t) for t in seq]
    ok(all(p >= 0 for p in pos) and pos == sorted(pos), f"reading order {list(zip(seq, pos))}")
    ok(order[-1] == "2", "the page number is read last")

    # built: one Sect for the whole guide, the pages unchanged
    from core.pipeline import process_file
    from tests.verify import render_diff, structure_integrity
    import pikepdf
    job = os.path.join(tmp, "job")
    o = profile_options("Academain")
    o.fix_contrast = "off"
    r = process_file(src, job, o)
    ok(r.get("status") == "done", f"built {r.get('error')}")
    if r.get("status") == "done":
        out = os.path.join(job, "accessible.pdf")
        pdf = pikepdf.open(out)
        k = pdf.Root.StructTreeRoot.K
        doc = k[0] if isinstance(k, pikepdf.Array) else k
        kids = doc.K if isinstance(doc.K, pikepdf.Array) else [doc.K]
        ok(str(doc.S) == "/Document" and len(kids) == 1 and str(kids[0].S) == "/Sect",
           f"Document > one Sect ({[str(x.get('/S')) for x in kids][:4]})")
        ok(max(render_diff(os.path.join(job, "working.pdf"), out, 1.0)) == 0, "the pages print as before")
        ok(not structure_integrity(out), "the structure is whole")
    shutil.rmtree(tmp, ignore_errors=True)

    if bad:
        print("teachers guide test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("teachers guide test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
