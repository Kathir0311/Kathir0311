"""Cross-reference links end to end (V4).

Builds a small book with a contents list, two numbered sections, a figure and a
table caption, a bibliography and an index, then checks that every kind of
mention is found, matched to the right target, and written as a real link
annotation inside a Link element."""
import os
import sys
import tempfile

import pikepdf
import pymupdf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.analyzer import analyze          # noqa: E402
from core.checker import check             # noqa: E402
from core.options import Options           # noqa: E402
from core.pipeline import process_file     # noqa: E402

PAGES = [
    ([("Quick Phonics Handbook", 20, True), ("Benchmark Education", 11, False)], None),
    ([("Contents", 15, True),
      ("Chapter 1  Getting Started .................. 3", 11, False),
      ("Chapter 2  Running the Test ............... 4", 11, False),
      ("Section 2.1  Scoring .......................... 4", 11, False),
      ("References ...................................... 5", 11, False)], "2"),
    ([("Chapter 1  Getting Started", 15, True),
      ("Read this first. The full guide is at https://www.example.org/guide", 11, False),
      ("and questions go to help@example.org any time.", 11, False),
      ("See Chapter 2 for the test itself, and Section 2.1 for scoring.", 11, False),
      ("The scores are on page 4 of this book.", 11, False),
      ("This method was first described in [1] and later in [2].", 11, False),
      ("", 11, False), ("Figure 1  The score sheet", 9, True)], "3"),
    ([("Chapter 2  Running the Test", 15, True),
      ("Hand the card to the student. Table 1 lists the skill sets.", 11, False),
      ("", 11, False), ("Table 1  Skill sets", 9, True),
      ("Section 2.1  Scoring", 13, True),
      ("Count the correct answers, as explained in Chapter 1.", 11, False),
      ("Smith (2019) reports the same result.", 11, False)], "4"),
    ([("References", 15, True),
      ("[1] Brown, A. (2015). Phonics in practice. Reading Press.", 10, False),
      ("[2] Smith, J. (2019). Measuring decoding skill. Journal of Reading.", 10, False)], "5"),
    ([("Index", 15, True), ("decoding 3, 4", 10, False), ("scoring 4", 10, False)], "6"),
]


def make(path: str) -> None:
    d = pymupdf.open()
    for lines, folio in PAGES:
        p = d.new_page(width=450, height=620)
        y = 60
        for txt, size, bold in lines:
            p.insert_text((50, y), txt, fontsize=size, fontname="hebo" if bold else "helv")
            y += size * 1.9
        if folio:
            p.insert_text((220, 595), folio, fontsize=9, fontname="helv")
    d.save(path)
    d.close()


ALL_ON = dict(link_sections=True, link_figures_tables=True, link_references=True,
              link_index=True, run_verapdf=False)


def main() -> int:
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "xref.pdf")
    make(src)
    bad = []

    m = analyze(src, Options(**ALL_ON))
    found = m.info.get("xrefs") or []
    kinds = {}
    for x in found:
        kinds[x["kind"]] = kinds.get(x["kind"], 0) + 1
    for kind, least in (("web", 1), ("email", 1), ("toc", 4), ("section", 3),
                        ("figure", 1), ("reference", 2), ("index", 3), ("page", 1)):
        if kinds.get(kind, 0) < least:
            bad.append(f"{kind}: found {kinds.get(kind, 0)}, expected at least {least}")
    if m.info["xref"]["unresolved"]:
        bad.append(f"{m.info['xref']['unresolved']} mention(s) without a target")

    # a heading, caption or entry naming itself is not a reference to itself
    for x in found:
        t = x.get("target") or {}
        if t.get("node") and t["node"] == x["owner"]:
            bad.append(f"self-reference kept: {x['text']!r}")

    # printed page 4 is the fourth sheet here, but it must come from the folio map
    toc = [x for x in found if x["kind"] == "toc" and x["text"] == "5"]
    if not toc or (toc[0]["target"] or {}).get("page") != 4:
        bad.append(f"contents entry 5 -> {toc and toc[0]['target']}")

    # a project that reads its page numbers: the links still land, and the
    # page number itself is not taken for a reference
    mp = analyze(src, Options(page_numbers="P", **ALL_ON))
    tp = sorted((x["text"], (x.get("target") or {}).get("page"))
                for x in (mp.info.get("xrefs") or []) if x["kind"] == "toc")
    ta = sorted((x["text"], (x.get("target") or {}).get("page"))
                for x in found if x["kind"] == "toc")
    if tp != ta:
        bad.append(f"with page numbers read as P the contents links changed: {tp} vs {ta}")

    # a file analysed before the search existed: the editor can search it now,
    # without re-analysing anything
    m2 = analyze(src, Options(detect_cross_references=False, run_verapdf=False))
    if (m2.info.get("xrefs") or []):
        bad.append("the search ran even though it was switched off")
    from core.xref import rescan
    st = rescan(m2, src, Options(**ALL_ON))
    if st.get("linked", 0) < 10:
        bad.append(f"searching an already-analysed file made {st.get('linked')} link(s)")
    toc2 = [x for x in (m2.info.get("xrefs") or []) if x["kind"] == "toc"]
    if len(toc2) < 4 or any(not x.get("target") for x in toc2):
        bad.append(f"contents entries after the search: {len(toc2)}, "
                   f"{sum(1 for x in toc2 if not x.get('target'))} without a target")
    if not [n for n in m2.nodes.values() if n.role == "Link"]:
        bad.append("the search put no Link tag in the structure")

    job = os.path.join(tmp, "job")
    r = process_file(src, job, Options(**ALL_ON))
    if r.get("status") != "done":
        bad.append(f"build failed: {r.get('error')}")
    else:
        made = (r["stats"].get("xref_links") or {}).get("created", 0)
        if made < 12:
            bad.append(f"only {made} link annotation(s) created")
        out = os.path.join(job, "accessible.pdf")
        pdf = pikepdf.open(out)
        annots = [a for p in pdf.pages for a in (p.obj.get("/Annots") or [])]
        links = [a for a in annots if str(a.get("/Subtype")) == "/Link"]
        if len(links) != made:
            bad.append(f"{made} created but {len(links)} link annotations in the file")
        for a in links:
            if not str(a.get("/Contents", "")).strip():
                bad.append("a link annotation has no /Contents")
                break
            if int(a.get("/F", 0)) & 4 == 0:
                bad.append("a link annotation has no Print flag")
                break
            if "/StructParent" not in a:
                bad.append("a link annotation is not in the structure tree")
                break
        errs = [f for f in check(out) if f.severity == "error"]
        if errs:
            bad.append(f"checker errors: {[f.code for f in errs]}")

        # every top-level heading opens a Sect that holds it and what follows
        import pikepdf as _pk

        def kids_of(obj):
            """/K is a single element, an array, or absent — always give a list."""
            k = obj.get("/K") if isinstance(obj, _pk.Dictionary) else None
            if k is None:
                return []
            if isinstance(k, _pk.Array):
                items = list(k)
            elif isinstance(k, _pk.Dictionary):
                items = [k]
            else:
                return []
            return [x for x in items if isinstance(x, _pk.Dictionary) and "/S" in x]

        root = kids_of(pdf.Root.StructTreeRoot)
        doc = next((x for x in root if str(x.S) == "/Document"), root[0] if root else None)
        top = kids_of(doc)
        if not top or any(str(x.S) != "/Sect" for x in top):
            bad.append(f"document children are {[str(x.S) for x in top][:6]}, expected Sect")
        for sct in top:
            inside = [str(y.S) for y in list(sct.get("/K")) if isinstance(y, _pk.Dictionary) and "/S" in y]
            if not inside or inside[0] not in ("/H1", "/H2", "/H3"):
                bad.append(f"a Sect starts with {inside[:1]}, expected a heading")
            if sum(1 for r in inside if r == "/H1") > 1:
                bad.append("a Sect holds more than one H1")

    if bad:
        print("xref test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print(f"xref test: OK ({len(found)} mentions, {kinds})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
