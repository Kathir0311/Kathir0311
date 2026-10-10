"""The index: Index > L > LI, sub-entries nested, every page number a link (V4.8).

A four-page book (folios 1–4 printed at the foot) with a two-column index:
letter dividers, entries, sub-entries set in one step, and a line that only
wraps page numbers ("4") set in further. Each entry has to be one LI, each
sub-entry an LI in a list inside its entry's LBody, the wrapped line kept with
its entry, and every page number a link to that printed page — in the built
file, with no checker errors.
"""
import os
import sys
import tempfile

import pymupdf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.checker import check                      # noqa: E402
from core.options import Options                    # noqa: E402
from core.pipeline import load_model, process_file  # noqa: E402
from tests.verify import mc_balance, structure_integrity  # noqa: E402

LEFT = [("A", 0, True), ("assessment, 1, 2", 0, False), ("of phonics, 2", 1, False),
        ("of spelling, 3,", 1, False), ("4", 2, False), ("alphabet, 1", 0, False),
        ("B", 0, True), ("blending, 3", 0, False)]
RIGHT = [("D", 0, True), ("decoding, 2, 4", 0, False), ("digraphs, 4", 1, False),
         ("dictation, 3", 0, False)]


def book(path: str) -> None:
    d = pymupdf.open()
    for k in range(4):
        p = d.new_page(width=612, height=792)
        p.insert_text((60, 70), f"Chapter {k + 1}", fontsize=18, fontname="hebo")
        p.insert_text((60, 110), "Some running text about reading and spelling.", fontsize=11,
                      fontname="helv")
        p.insert_text((300, 770), str(k + 1), fontsize=9, fontname="helv")
    q = d.new_page(width=612, height=792)
    q.insert_text((60, 70), "Index", fontsize=18, fontname="hebo")
    for x0, rows in ((60, LEFT), (320, RIGHT)):
        y = 110
        for t, lv, letter in rows:
            q.insert_text((x0 + 12 * lv, y), t, fontsize=12 if letter else 10,
                          fontname="hebo" if letter else "helv")
            y += 18 if letter else 14
    q.insert_text((300, 770), "5", fontsize=9, fontname="helv")
    d.save(path)
    d.close()


def main() -> int:
    bad: list = []
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "index.pdf")
    book(src)
    job = os.path.join(tmp, "job")
    r = process_file(src, job, Options(existing_tags="retag", run_verapdf=False))
    if r.get("status") != "done":
        print("index test: FAILED —", r.get("error"))
        return 1
    m = load_model(job)
    idx = [n for n in m.nodes.values() if n.role == "Index"]
    if len(idx) != 1:
        bad.append(f"{len(idx)} Index tag(s)")
    else:
        bodies = {n.text: n for n in m.descendants(idx[0].id) if n.role == "LBody"}
        want = ["assessment, 1, 2", "of phonics, 2", "of spelling, 3, 4", "alphabet, 1",
                "blending, 3", "decoding, 2, 4", "digraphs, 4", "dictation, 3"]
        if sorted(bodies) != sorted(want):
            bad.append(f"index entries: {sorted(bodies)}")
        else:
            sub = bodies["of spelling, 3, 4"]
            host = m.ancestors(sub.id)
            # LBody < LI < L < LBody(assessment)
            if len(host) < 3 or host[2].role != "LBody" or not host[2].text.startswith("assessment"):
                bad.append(f"'of spelling' is not a sub-entry of 'assessment': {[h.role for h in host[:4]]}")
            dg = m.ancestors(bodies["digraphs, 4"].id)
            if len(dg) < 3 or not dg[2].text.startswith("decoding"):
                bad.append("'digraphs' is not a sub-entry of 'decoding'")
            links = [(c.text, (c.style or {}).get("xref", {}).get("page"))
                     for c in m.descendants(idx[0].id) if c.role == "Link"]
            if len(links) != 11:
                bad.append(f"{len(links)} page-number link(s), expected 11: {links}")
            if any(t.isdigit() and pg != int(t) - 1 for t, pg in links):
                bad.append(f"a page number points at the wrong sheet: {links}")
            sp = [m.nodes[c].text for c in sub.children if m.nodes[c].role == "Link"]
            if sp != ["3", "4"]:
                bad.append(f"links of 'of spelling, 3, 4' (wrapped line) in order: {sp}")
        letters = [n.text for n in m.descendants(idx[0].id) if n.text in ("A", "B", "D")]
        if letters != ["A", "B", "D"]:
            bad.append(f"letter dividers: {letters}")

    out = os.path.join(job, "accessible.pdf")
    errs = [f"{f.code}: {f.detail[:60]}" for f in check(out) if f.severity == "error"]
    if errs:
        bad.append(f"checker errors {errs}")
    if structure_integrity(out):
        bad.append(f"structure integrity {structure_integrity(out)[:3]}")
    if mc_balance(out):
        bad.append("marked content unbalanced")

    if bad:
        print("index test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("index test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
