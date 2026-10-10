"""A row of labels with write-in lines: "Name: ____  Date: ____" (V4.9).

  * analysed: each label and its line is its own P, only the underscores are
    the blank-line Span, and the ":" of "Date:" stays in its P; a sentence with
    two blanks ("The ____ ran to the ____.") stays one P
  * a file tagged before with the row as ONE P and the "Date:" colon inside the
    blank's Span (what an older build produced, and what a re-import showed as
    "Name:Blank DateBlank"): imported, the row is rebuilt from the page —
    printed glyphs, not their /ActualText — into the same two P tags
  * a zone drawn by hand over the row gives the same two P tags
"""
import os
import shutil
import sys
import tempfile

import pymupdf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import edit                                         # noqa: E402
from core.checker import check                                # noqa: E402
from core.options import Options                              # noqa: E402
from core.pipeline import finish, load_model, process_file, save_model  # noqa: E402
from core.repair import prompt_rows, split_prompt_rows        # noqa: E402
from tests.verify import mc_balance, structure_integrity       # noqa: E402

FS = 10.0


def form(path: str) -> None:
    d = pymupdf.open()
    p = d.new_page(width=612, height=792)
    p.insert_text((36, 70), "Level 1 Fiction (Teacher Form)", fontsize=17, fontname="hebo")
    w = lambda t: pymupdf.get_text_length(t, fontname="helv", fontsize=FS)   # noqa: E731
    x = 36
    for t in ("Name:", "_" * 60, " Date", ":", "_" * 22):     # printed piece by piece, like the book
        p.insert_text((x, 110), t, fontsize=FS, fontname="helv")
        x += w(t)
    x = 36
    for t in ("Correct Words per Minute", "_" * 30, " National Norm of Words per Minute ", "_" * 20):
        p.insert_text((x, 132), t, fontsize=FS, fontname="helv")
        x += w(t)
    p.insert_text((36, 170), "The _______ ran to the _______ in the park.", fontsize=FS, fontname="helv")
    p.insert_text((36, 200), "Read the story aloud and fill in the two blanks with the words you hear.",
                  fontsize=FS, fontname="helv")
    d.save(path)
    d.close()


def p_tags(m):
    out = []
    for n in m.ordered_nodes():
        if n.role == "P" and n.page == 0 and "_" in (n.text or "") + "".join(
                m.nodes[c].text or "" for c in n.children if c in m.nodes):
            spans = [m.nodes[c] for c in n.children if c in m.nodes and m.nodes[c].role == "Span"]
            out.append((n, spans))
    return out


def check_rows(bad, m, label, imported=False):
    rows = p_tags(m)
    labels = [n.text.replace("_", "").strip() for n, _ in rows]
    want = ["Name:", "Date:", "Correct Words per Minute", "National Norm of Words per Minute",
            "The ran to the in the park."]
    got = [" ".join(t.split()) for t in labels]
    if got != want:
        bad.append(f"{label}: P tags {got}")
        return
    for n, spans in rows[:4]:
        if len(spans) != 1:
            bad.append(f"{label}: {n.text[:20]!r} has {len(spans)} blank Span(s)")
        # P "Name:" > Span "______": the line is the Span's, not the P's text too
        if "_" in n.text:
            bad.append(f"{label}: the P's own text still shows the line: {n.text[:30]!r}")
        elif spans and set(spans[0].text.strip()) != {"_"}:
            bad.append(f"{label}: the Span of {n.text!r} is {spans[0].text[:20]!r}")
    if len(rows[4][1]) != 2:
        bad.append(f"{label}: the sentence has {len(rows[4][1])} blank(s), expected 2")
    if imported:
        # read back from the built file: the glyphs under each tag
        for n, spans in rows[:4]:
            for sp in spans:
                if sp.text.replace("_", "").strip():
                    bad.append(f"{label}: the blank of {n.text[:12]!r} holds {sp.text.replace('_', '')!r}")


def check_pdf(bad, out, label):
    errs = [f"{f.code}: {f.detail[:50]}" for f in check(out) if f.severity == "error"]
    if errs:
        bad.append(f"{label}: checker errors {errs}")
    if structure_integrity(out):
        bad.append(f"{label}: structure integrity {structure_integrity(out)[:3]}")
    if mc_balance(out):
        bad.append(f"{label}: marked content unbalanced")


def reimport(tmp, pdf, name):
    job = os.path.join(tmp, name)
    process_file(pdf, job, Options(existing_tags="import", run_verapdf=False))
    return job


def main() -> int:
    bad: list = []
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "form.pdf")
    form(src)

    # 1. analysed
    job = os.path.join(tmp, "retag")
    process_file(src, job, Options(existing_tags="retag", run_verapdf=False))
    check_rows(bad, load_model(job), "analysed")
    check_pdf(bad, os.path.join(job, "accessible.pdf"), "analysed")
    back = reimport(tmp, os.path.join(job, "accessible.pdf"), "back")
    check_rows(bad, load_model(back), "analysed, read back", imported=True)

    # 2. the old, wrong tagging: one P for the row, the colon in the Span
    m = load_model(job)
    rows = p_tags(m)
    name, date = rows[0][0], rows[1][0]
    span = [s for s in rows[1][1]][0]
    colon_x = span.bbox[0] - 3.5
    span.bbox = (colon_x, span.bbox[1], span.bbox[2], span.bbox[3])
    name.bbox = (name.bbox[0], min(name.bbox[1], date.bbox[1]), date.bbox[2], max(name.bbox[3], date.bbox[3]))
    for c in list(date.children):
        m.detach(c)
        m.add(m.nodes[c], name.id)
    m.remove(date.id, promote_children=False)
    save_model(job, m)
    finish(job, Options(existing_tags="retag", run_verapdf=False), filename="form.pdf")
    old = os.path.join(tmp, "old_tagged.pdf")
    shutil.copyfile(os.path.join(job, "accessible.pdf"), old)
    probe = reimport(tmp, old, "probe")
    pm = load_model(probe)
    # the import itself rebuilds the row; check that it was needed and is right
    check_rows(bad, pm, "old file, imported")
    check_pdf(bad, os.path.join(probe, "accessible.pdf"), "old file, imported")
    again = reimport(tmp, os.path.join(probe, "accessible.pdf"), "again")
    check_rows(bad, load_model(again), "old file, rebuilt and read back", imported=True)

    # the repair on its own, on the old structure kept as it was imported
    raw = os.path.join(tmp, "raw")
    process_file(old, raw, Options(existing_tags="import", run_verapdf=False, detect_blank_lines=False))
    rm = load_model(raw)
    found = prompt_rows(rm, os.path.join(raw, "working.pdf"), Options())
    if len(found) < 1:
        bad.append(f"the one-P row with the colon in its blank was not found ({found})")
    st = split_prompt_rows(rm, os.path.join(raw, "working.pdf"), Options())
    if st.get("tags", 0) < 2:
        bad.append(f"rebuilding the old row: {st}")

    # 3. a zone drawn over the row
    m3 = load_model(back)
    work = os.path.join(back, "working.pdf")
    d = pymupdf.open(work)
    r = pymupdf.Rect(30, 98, 600, 114)
    d.close()
    edit.create_from_rect(m3, work, 0, (r.x0, r.y0, r.x1, r.y1), "P", True, Options())
    drawn = [n for n in m3.ordered_nodes() if n.role == "P" and n.source == "user" and n.page == 0]
    if [n.text for n in drawn] != ["Name:", "Date:"]:
        bad.append(f"zone over the row: {[n.text[:20] for n in drawn]}")
    elif any(not [c for c in n.children if m3.nodes[c].role == "Span"] for n in drawn):
        bad.append("zone over the row: a P has no blank-line Span")

    if bad:
        print("prompt rows test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("prompt rows test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
