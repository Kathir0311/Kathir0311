"""Reference lists and the citations that point at them (V4.7).

Built from the bibliography of an assessment handbook:

  * a page of running text citing "(Hasbrouck, 2010)" and, broken over two
    lines, "(Moats," / "2005/2006)"
  * a "Bibliography" page whose entries hang (first line at the margin, the
    rest set in); the Kame'enui entry's first line ends "Coyne, M. D." — a full
    stop — which layout analysis read as the end of a paragraph
  * a "References" page with numbered entries ("1.", "2." …)

Each entry has to come out as one BibEntry (L > LI > [Lbl] + LBody > BibEntry),
the citations linked to their entry, in a file tagged by the tool and in one
whose tags were imported — and the editor's fix has to do the same to a file
whose entries are still paragraphs.
"""
import os
import shutil
import sys
import tempfile

import pymupdf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import biblio, xref                       # noqa: E402
from core.checker import check                      # noqa: E402
from core.options import Options                    # noqa: E402
from core.pipeline import load_model, process_file  # noqa: E402
from tests.verify import mc_balance, structure_integrity  # noqa: E402

FS, LH = 10.5, 13.0
NOTO = pymupdf.Font("cjk")
Q = "’"

HANGING = [
    ["Adams, M. J. (1990). Beginning to read: Thinking and learning about print.",
     "Cambridge, MA: MIT Press."],
    ["Hasbrouck, J. (2010). Educators as physicians, using RTI data for effective",
     "decision-making. Wellesley Hills, MA: Gibson Hasbrouck & Associates."],
    [f"Kame{Q}enui, E. J., Carnine, D. W., Dixon, R. C., Simmons, D. C., & Coyne, M. D.",
     "(2002). Effective teaching strategies that accommodate diverse learners.",
     "Upper Saddle River, NJ: Prentice Hall."],
    ["Moats, L. C. (Winter 2005/2006). How spelling supports reading. American",
     "Educator, 29(4), 12–22, 42–43."],
    ["Shaywitz, S. (2003). Overcoming dyslexia. New York, NY: Knopf."],
]
NUMBERED = [
    ["1. Snow, C. E., Burns, M. S., & Griffin, P. (1998). Preventing reading",
     "difficulties in young children. Washington, DC: National Academy Press."],
    ["2. Vaughn, S., & Linan-Thompson, S. (2004). Research-based methods of",
     "reading instruction. Alexandria, VA: ASCD."],
    ["3. Treiman, R., & Kessler, B. (2016). Measures of kindergarten spelling."],
]


def write(p, x, y, lines, hang=0.0, size=FS):
    if "noto" not in [f[3] for f in p.get_fonts()]:
        p.insert_font(fontname="noto", fontbuffer=NOTO.buffer)
    for k, t in enumerate(lines):
        p.insert_text((x + (hang if k else 0.0), y), t, fontsize=size, fontname="noto")
        y += LH
    return y


def book(path: str) -> None:
    d = pymupdf.open()
    p = d.new_page(width=612, height=792)
    p.insert_text((54, 70), "Spelling and Reading", fontsize=20, fontname="hebo")
    y = write(p, 54, 110, [
        "Knowing how well a student spells tells the teacher about decoding",
        "(Hasbrouck, 2010). Spelling supports reading in several ways",
        "that teachers can use every day in class (Moats,",
        "2005/2006). Students who spell well usually read well too."])
    p.insert_text((300, 760), "4", fontsize=9, fontname="helv")
    q = d.new_page(width=612, height=792)
    q.insert_text((54, 70), "Bibliography", fontsize=20, fontname="hebo")
    y = 110
    for e in HANGING:
        y = write(q, 54, y, e, hang=14) + 6
    q.insert_text((300, 760), "5", fontsize=9, fontname="helv")
    r = d.new_page(width=612, height=792)
    r.insert_text((54, 70), "References", fontsize=20, fontname="hebo")
    y = 110
    for e in NUMBERED:
        y = write(r, 54, y, e, hang=14) + 6
    r.insert_text((300, 760), "6", fontsize=9, fontname="helv")
    d.save(path)
    d.close()


def entries(m, head_text):
    head = next(n for n in m.nodes.values() if n.is_heading and n.text.strip() == head_text)
    sibs = m.siblings(head.id)
    out = []
    for sid in sibs[sibs.index(head.id) + 1:]:
        s = m.nodes[sid]
        if s.is_heading:
            break
        out += [d for d in [s] + m.descendants(sid) if d.role == "BibEntry"]
    return out


def check_model(bad, m, label):
    got = entries(m, "Bibliography")
    if len(got) != len(HANGING):
        bad.append(f"{label}: {len(got)} BibEntry under Bibliography, expected {len(HANGING)}: "
                   f"{[g.text[:30] for g in got]}")
    else:
        kame = got[2]
        if "Upper Saddle River" not in kame.text or not kame.text.startswith("Kame"):
            bad.append(f"{label}: the Kame'enui entry is not one entry: {kame.text!r}")
        for g in got:
            par = m.nodes.get(g.parent)
            li = m.nodes.get(par.parent) if par else None
            if not (par and par.role == "LBody" and li and li.role == "LI"):
                bad.append(f"{label}: BibEntry not in L > LI > LBody ({par.role if par else None})")
                break
    num = entries(m, "References")
    if len(num) != len(NUMBERED):
        bad.append(f"{label}: {len(num)} numbered entries, expected {len(NUMBERED)}")
    else:
        lbls = []
        for g in num:
            li = m.nodes[m.nodes[g.parent].parent]
            lbls += [m.nodes[c].text for c in li.children if m.nodes[c].role == "Lbl"]
        if lbls != ["1.", "2.", "3."]:
            bad.append(f"{label}: numbered entries' labels are {lbls}")
        if num[0].text.startswith("1."):
            bad.append(f"{label}: the number is still in the entry text")
    links = [n for n in m.nodes.values() if n.role == "Link"
             and ((n.style or {}).get("xref") or {}).get("kind") == "reference"]
    keys = sorted(((n.style or {}).get("xref") or {}).get("key") for n in links)
    if "ref hasbrouck 2010" not in keys:
        bad.append(f"{label}: (Hasbrouck, 2010) is not linked: {keys}")
    if "ref moats 2005" not in keys:
        bad.append(f"{label}: the citation broken over two lines (Moats, 2005/2006) is not linked: {keys}")
    else:
        moats = next(n for n in links if n.style["xref"]["key"] == "ref moats 2005")
        if not moats.parts:
            bad.append(f"{label}: the two-line citation has no zone on its second line")
        tgt = m.nodes.get(moats.style["xref"].get("node"))
        if tgt is None or tgt.role != "BibEntry" or not tgt.text.startswith("Moats"):
            bad.append(f"{label}: the Moats citation points at {tgt.role if tgt else None}")
    for n in links:
        owner = m.nodes.get(n.parent)
        if owner is not None and owner.role == "BibEntry":
            bad.append(f"{label}: an entry links to itself: {n.text!r}")


def check_pdf(bad, out, label):
    errs = [f"{f.code}: {f.detail[:60]}" for f in check(out) if f.severity == "error"]
    if errs:
        bad.append(f"{label}: checker errors {errs}")
    if structure_integrity(out):
        bad.append(f"{label}: structure integrity {structure_integrity(out)[:3]}")
    if mc_balance(out):
        bad.append(f"{label}: marked content unbalanced")


def main() -> int:
    bad: list = []
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "biblio.pdf")
    book(src)
    opts = dict(run_verapdf=False, link_references=True)

    # 1. tagged by the tool: the analysis tags the reference lists itself
    job = os.path.join(tmp, "retag")
    r = process_file(src, job, Options(existing_tags="retag", **opts))
    if r.get("status") != "done":
        bad.append(f"build failed: {r.get('error')}")
    else:
        m = load_model(job)
        check_model(bad, m, "tagged by the tool")
        check_pdf(bad, os.path.join(job, "accessible.pdf"), "tagged by the tool")

        # 2. a file tagged somewhere else, entries as paragraphs: the import fixes it
        tagged = os.path.join(tmp, "plain_tagged.pdf")
        plain = os.path.join(tmp, "plain")
        # no citation links either: the import has to make them
        process_file(src, plain, Options(existing_tags="retag", detect_bibliography=False,
                                         run_verapdf=False, link_references=False))
        pm = load_model(plain)
        if not biblio.problems(pm)["sections"]:
            bad.append("with the option off, no reference list is reported as still to tag")
        shutil.copyfile(os.path.join(plain, "accessible.pdf"), tagged)
        job2 = os.path.join(tmp, "import")
        r2 = process_file(tagged, job2, Options(existing_tags="import", **opts))
        if r2.get("mode") != "import":
            bad.append(f"the tagged file was handled as {r2.get('mode')}")
        else:
            m2 = load_model(job2)
            check_model(bad, m2, "imported")
            check_pdf(bad, os.path.join(job2, "accessible.pdf"), "imported")

        # 3. the editor's fix on a model whose entries are still paragraphs
        pm = load_model(plain)
        st = biblio.fix_bibliography(pm, os.path.join(plain, "working.pdf"), Options(**opts))
        xref.rescan(pm, os.path.join(plain, "working.pdf"), Options(**opts))
        if st.get("entries") != len(HANGING) + len(NUMBERED):
            bad.append(f"editor fix: {st}")
        check_model(bad, pm, "editor fix")
        if biblio.problems(pm)["sections"]:
            bad.append("editor fix: a reference list is still reported as to tag")
        again = biblio.fix_bibliography(pm, os.path.join(plain, "working.pdf"), Options(**opts))
        if again.get("entries"):
            bad.append(f"running the fix twice changed the list again: {again}")

    # 4. entry splitting on its own: no indent, no numbers, only the spacing
    class L:
        def __init__(self, t, y, size=10.0):
            self.text, self.bbox, self.size = t, (50, y, 400, y + 10), size
            self.chars = []
    lines = []
    y = 100
    for e in (["Adams, M. J. (1990). Beginning to read.", "Cambridge, MA: MIT Press."],
              ["Brown, A. (2001). A second book.", "Boston: Beacon."], ["Clark, B. (2005). Third."]):
        for k, t in enumerate(e):
            lines.append(biblio.BibLine(0, L(t, y), t, 0, 0.0, None if not lines else (y - prev)))
            prev = y + 10
            y += 12
        y += 8
    starts, how = biblio.entry_starts(lines)
    if starts != [0, 2, 4]:
        bad.append(f"spacing only: starts {starts} ({how}), expected [0, 2, 4]")

    if bad:
        print("biblio test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("biblio test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
