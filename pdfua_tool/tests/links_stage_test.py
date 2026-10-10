"""The Links stage (V4.18): links set by hand, the links of the file, the check.

A three-page file with links of its own: a web address whose annotation opens
another site than the one printed, an e-mail address without "mailto:", a
"Figure 1" that jumps to the wrong page and a link that lands on empty paper.
In the Default (V4) engine and in the Kriyadocs house style:

* the check finds each of those, and "Fix all" mends them (the file's own
  annotations keep their object, with a new action at build)
* links set by hand: to a heading (one-way), to a note (two-way: the note's
  first words link back), to a spot on a page, to a page, to a web address and
  to an e-mail address; a link over two lines (two boxes)
* a new search for links keeps the hand-set links; a found link taken out by
  hand stays out
* a link of the file taken out leaves the built file; the built file has the
  annotations where they should go, and no checker error
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pikepdf  # noqa: E402
import pymupdf  # noqa: E402

from core import compat, links, xref  # noqa: E402
from core.build import build  # noqa: E402
from core.checker import check  # noqa: E402
from core.options import profile_options  # noqa: E402
from core.pipeline import load_model, process_file, save_model  # noqa: E402
from tests.verify import mc_balance, structure_integrity  # noqa: E402


def make(path: str) -> None:
    d = pymupdf.open()
    p = d.new_page()
    p.insert_text((72, 80), "A short report", fontsize=20)
    rows = ["The results are discussed in the next part of this report, where",
            "the method is set out in full. A note explains the sample size.",
            "Further reading is listed at the end of the report, and",
            "Figure 1 shows the plan. More at www.example.org today.",
            "Write to info@example.org with questions about the",
            "study, or see the summary chapter for the short version."]
    y = 120
    for r in rows:
        p.insert_text((72, y), r, fontsize=11)
        y += 18
    q = d.new_page()
    q.insert_text((72, 80), "Results and discussion", fontsize=16)
    q.insert_text((72, 110), "The figures below were collected over three years of work.", fontsize=11)
    q.insert_text((72, 700), "1 The sample size was chosen to give a power of 0.8.", fontsize=8)
    r3 = d.new_page()
    r3.insert_text((72, 80), "Summary", fontsize=16)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 30), False)
    pix.set_rect(pix.irect, (30, 110, 190))
    r3.insert_image(pymupdf.Rect(72, 120, 300, 300), pixmap=pix)
    r3.insert_text((72, 318), "Figure 1  The plan of the study", fontsize=9.5)
    # the file's own links
    p = d[0]
    fig = p.search_for("Figure 1")[0]
    web = p.search_for("www.example.org")[0]
    mail = p.search_for("info@example.org")[0]
    summ = p.search_for("summary chapter")[0]
    p.insert_link({"kind": pymupdf.LINK_GOTO, "from": fig, "page": 1, "to": pymupdf.Point(72, 100)})
    p.insert_link({"kind": pymupdf.LINK_URI, "from": web, "uri": "https://other.example.com/"})
    p.insert_link({"kind": pymupdf.LINK_URI, "from": mail, "uri": "info@example.org"})
    p.insert_link({"kind": pymupdf.LINK_GOTO, "from": summ, "page": 2, "to": pymupdf.Point(72, 600)})
    d.save(path)
    d.close()


def built_links(out: str) -> dict:
    pdf = pikepdf.open(out)
    pages = {p.objgen: i for i, p in enumerate(pdf.pages)}
    got = {}
    for i, pg in enumerate(pdf.pages):
        for a in pg.obj.get("/Annots", []) or []:
            if a.get("/Subtype") != pikepdf.Name.Link:
                continue
            act = a.get("/A") or {}
            dd = act.get("/D")
            key = (i, round(float(a.Rect[0])), round(float(a.Rect[1])))
            if isinstance(dd, pikepdf.Array):
                got[key] = ("page", pages.get(dd[0].objgen), float(dd[3]) if len(dd) > 3 else None,
                            str(a.get("/Contents", "")))
            elif act.get("/URI") is not None:
                got[key] = ("uri", str(act.get("/URI")), None, str(a.get("/Contents", "")))
    pdf.close()
    return got


def find(got: dict, page: int, r):
    """The annotations on the words in display box r (an A4 page)."""
    return [v for (pg, ax, ay), v in got.items()
            if pg == page and abs(ax - r.x0) <= 2 and abs(ay - (842 - r.y1)) <= 3]


def run(profile: str, bad: list) -> None:
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "r.pdf")
    make(src)
    o = profile_options(profile)
    o.ai_alt_text_auto = False
    o.run_verapdf = False
    job = os.path.join(tmp, "job")
    process_file(src, job, o)
    compat.use(o)
    m = load_model(job)
    work = os.path.join(job, "working.pdf")
    d = pymupdf.open(work)
    w = {k: d[0].search_for(k)[0] for k in ("discussed", "note", "Further reading", "Figure 1",
                                             "www.example.org", "info@example.org", "summary chapter",
                                             "questions", "the method", "results")}
    w["about the"] = d[0].search_for("about the")[0]
    w["study,"] = d[0].search_for("study,")[0]
    d.close()

    # ---- the check finds what is wrong with the file's own links
    data = links.list_links(m, work)
    codes = {c["code"] for it in data["links"] for c in it["problems"]}
    for want in ("printed_differs", "mailto", "mismatch", "lands_empty"):
        if want not in codes:
            bad.append(f"{profile}: the check did not find {want} ({sorted(codes)})")
    if data["counts"]["file"] < 4:
        bad.append(f"{profile}: file links listed {data['counts']}")
    r = links.fix_all(m, work, o)
    data = links.list_links(m, work)
    left = sorted({c["code"] for it in data["links"] for c in it["problems"] if c["level"] != "info"})
    if left:
        bad.append(f"{profile}: after Fix all still {left} "
                   f"{[(it['text'], it['problems']) for it in data['links'] if it['problems']][:3]} {r}")

    # ---- links set by hand
    head = next(n for n in m.ordered_nodes() if (n.text or "").startswith("Results and discussion"))
    note = next(n for n in m.ordered_nodes() if "sample size was chosen" in (n.text or ""))
    r1 = links.set_link(m, work, 0, list(w["discussed"]), {"node": head.id}, False, None, o)
    r2 = links.set_link(m, work, 0, list(w["note"]), {"node": note.id}, True, None, o)
    r3 = links.set_link(m, work, 0, list(w["Further reading"]), {"page": 1, "y": 100}, False, None, o)
    r4 = links.set_link(m, work, 0, list(w["questions"]), {"uri": "www.example.org/faq"}, False, None, o)
    r5 = links.set_link(m, work, 0, list(w["the method"]), {"page": 2}, False, None, o)
    r6 = links.set_link(m, work, 0, list(w["about the"]), {"email": "study@example.org"}, False, None, o,
                        more_rects=[[0, *w["study,"]]])
    try:
        links.set_link(m, work, 0, list(w["results"]), {"uri": "https://x.org"}, True, None, o)
        bad.append(f"{profile}: a two-way web link was allowed")
    except links.LinkError:
        pass
    fwd = m.nodes[r1["id"]]
    if fwd.text != "discussed":
        bad.append(f"{profile}: the link's words are {fwd.text!r}")
    want_ref = profile == "Kriyadocs"
    if (m.nodes[fwd.parent].role == "Reference") != want_ref:
        bad.append(f"{profile}: the link sits in {m.nodes[fwd.parent].role}")
    if m.nodes[m.nodes[r4['id']].parent].role == "Reference" and want_ref:
        bad.append(f"{profile}: a web link set by hand got a Reference")
    if len(m.nodes[r6["id"]].parts) != 1:
        bad.append(f"{profile}: the link over two lines has {len(m.nodes[r6['id']].parts)} more zones")
    back = m.nodes[r2["back_id"]]
    if back.page != 1 or not (back.text or "").strip():
        bad.append(f"{profile}: the way back: page {back.page} {back.text!r}")
    listed = {it["id"]: it for it in links.list_links(m, work)["links"]}
    if not listed[r2["id"]]["two_way"] or listed[r1["id"]]["two_way"]:
        bad.append(f"{profile}: two-way flags {listed[r2['id']]['two_way']} {listed[r1['id']]['two_way']}")
    if listed[r4["id"]]["target"].get("uri") != "https://www.example.org/faq":
        bad.append(f"{profile}: web target {listed[r4['id']]['target']}")
    if listed[r6["id"]]["target"].get("uri") != "mailto:study@example.org":
        bad.append(f"{profile}: e-mail target {listed[r6['id']]['target']}")
    if listed[r5["id"]]["target"].get("page") != 2:
        bad.append(f"{profile}: page target {listed[r5['id']]['target']}")
    if "Summary" not in (listed[r5["id"]]["target"].get("label") or ""):
        bad.append(f"{profile}: page target lands on {listed[r5['id']]['target'].get('label')}")
    for rid in (r1["id"], r2["id"], r3["id"], r4["id"], r5["id"], r6["id"]):
        probs = [p for p in listed[rid]["problems"] if p["level"] != "info"]
        if probs:
            bad.append(f"{profile}: hand-set link {listed[rid]['text']!r} has problems {probs}")

    # a one-way link made two-way afterwards
    links.make_two_way(m, work, r1["id"], None, o)
    pairs = [n for n in m.nodes.values() if n.role == "Link" and (n.style or {}).get("manual") == m.nodes[r1["id"]].style["manual"]]
    if len(pairs) != 2:
        bad.append(f"{profile}: make two-way made {len(pairs)} links")

    # ---- a new search keeps the hand-set links; a found link taken out stays out
    o.link_figures_tables = True
    xref.rescan(m, work, o)
    if sum(1 for n in m.nodes.values() if n.role == "Link" and (n.style or {}).get("manual")) < 8:
        bad.append(f"{profile}: a new search for links took hand-set ones out")
    found = [n for n in m.nodes.values() if n.role == "Link" and not n.annots
             and not (n.style or {}).get("manual") and isinstance((n.style or {}).get("xref"), dict)]
    if found:
        f0 = found[0]
        box = (f0.page, tuple(f0.bbox))
        links.remove_link(m, f0.id)
        xref.rescan(m, work, o)
        again = [n for n in m.nodes.values() if n.role == "Link" and n.page == box[0] and n.bbox
                 and abs(n.bbox[0] - box[1][0]) < 1 and abs(n.bbox[1] - box[1][1]) < 1]
        if again:
            bad.append(f"{profile}: a link taken out by hand came back with the search")

    # ---- a link of the file taken out leaves the built file
    sumlink = next(it for it in links.list_links(m, work, check=False)["links"]
                   if it["origin"] == "file" and "summary" in it["text"])
    links.remove_link(m, sumlink["id"])

    save_model(job, m)
    out = os.path.join(job, "out.pdf")
    build(m, work, out, o)
    got = built_links(out)
    expect = {"discussed": ("page", 1), "note": ("page", 1), "Further reading": ("page", 1),
              "questions": ("uri", "https://www.example.org/faq"), "the method": ("page", 2),
              "about the": ("uri", "mailto:study@example.org"),
              "www.example.org": ("uri", "https://www.example.org"),
              "info@example.org": ("uri", "mailto:info@example.org"), "Figure 1": ("page", 2)}
    for word, (kind, where) in expect.items():
        hit = find(got, 0, w[word])
        if not hit or hit[0][0] != kind or hit[0][1] != where:
            bad.append(f"{profile}: the link on {word!r} goes to {hit}")
    if find(got, 0, w["summary chapter"]):
        bad.append(f"{profile}: the link taken out is still in the file")
    if not find(got, 0, w["study,"]):
        bad.append(f"{profile}: the second line of the e-mail link has no annotation")
    backs = [v for (pg, _, _), v in got.items() if pg == 1 and v[3].startswith("Back to")]
    if len(backs) < 2 or any(b[1] != 0 for b in backs):
        bad.append(f"{profile}: ways back {backs}")
    # (the figure has no alternative text and the headings of this little file
    # skip a level: not what this test is about)
    errs = [f.code for f in check(out) if f.severity == "error" and f.code not in ("UA-20", "UA-23")]
    if errs:
        bad.append(f"{profile}: checker errors {errs}")
    if structure_integrity(out) or mc_balance(out):
        bad.append(f"{profile}: structure {structure_integrity(out)[:2]} {mc_balance(out)[:2]}")
    n_before = sum(1 for n in m.nodes.values() if n.role == "Link")
    links.remove_link(m, r2["id"], both=True)
    if n_before - sum(1 for n in m.nodes.values() if n.role == "Link") != 2:
        bad.append(f"{profile}: taking out a two-way link")
    compat.use("")


def main() -> int:
    bad: list = []
    for prof in ("Default", "Kriyadocs"):
        run(prof, bad)
    if bad:
        print("links stage test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("links stage test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
