"""V4.21.2: a word grid reads row by row (Academain / V4); an item set in two
columns keeps its two texts apart (every client); Edit list: a label from a box.

* the phonics grid "litch mudge vux quam cep / gen knaz wrop saten qui":
  Academain and Default read it row by row, the line under it after it;
  Kriyadocs keeps its own order
* "1. duck      The duck is swimming on the pond." … in Default and
  Kriyadocs: one list of 5 items, each Lbl "1." … + LBody > P "duck" +
  P "The duck is …"; the built file has no checker error
* a box drawn down the column of numbers gives every item its Lbl again
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymupdf  # noqa: E402

from core import compat, listedit  # noqa: E402
from core.build import build  # noqa: E402
from core.checker import check  # noqa: E402
from core.options import profile_options  # noqa: E402
from core.pipeline import load_model, process_file  # noqa: E402
from tests.verify import mc_balance, structure_integrity  # noqa: E402
from tests.wordgrid_gen import PAIRS, ROW1, ROW2, make_grid, make_pairs  # noqa: E402


def run(make, profile):
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "f.pdf")
    make(src)
    o = profile_options(profile)
    o.ai_alt_text_auto = False
    o.run_verapdf = False
    o.use_samples = False
    job = os.path.join(tmp, "job")
    process_file(src, job, o)
    compat.use(o)
    return job, o, load_model(job)


def main() -> int:
    bad: list = []
    words = set(ROW1 + ROW2)
    for prof in ("Academain", "Default", "Kriyadocs"):
        _, _, m = run(make_grid, prof)
        order = [n.text for n in m.ordered_nodes() if n.text in words or n.text.startswith("Score")]
        want = ROW1 + ROW2 + ["Score: count the words read correctly in one minute."]
        if prof != "Kriyadocs" and order != want:
            bad.append(f"{prof}: grid read {order}")
        if prof == "Kriyadocs" and order == want:
            bad.append("Kriyadocs: the grid order changed (it was not asked for)")
    for prof in ("Default", "Kriyadocs"):
        job, o, m = run(make_pairs, prof)
        Ls = [n for n in m.ordered_nodes() if n.role == "L"]
        lis = [m.nodes[c] for c in Ls[0].children if m.nodes[c].role == "LI"] if Ls else []
        got = []
        for li in lis:
            lbl = next((m.nodes[c].text for c in li.children if m.nodes[c].role == "Lbl"), "")
            body = next((m.nodes[c] for c in li.children if m.nodes[c].role == "LBody"), None)
            ps = [m.nodes[c].text for c in body.children] if body is not None else []
            got.append((lbl, tuple(ps)))
        want = [(f"{k}.", (w, s_)) for k, (w, s_) in enumerate(PAIRS, 1)]
        if len(Ls) != 1 or got != want:
            bad.append(f"{prof}: two-column items {got}")
        out = os.path.join(job, "out.pdf")
        build(m, os.path.join(job, "working.pdf"), out, o)
        errs = [f"{f.code} {f.message}" for f in check(out) if f.severity == "error" and f.code not in ("UA-23",)]
        if errs or structure_integrity(out) or mc_balance(out):
            bad.append(f"{prof}: built {errs[:3]} {structure_integrity(out)[:2]} {mc_balance(out)[:2]}")
        if prof == "Default" and Ls:
            # the labels again, from a box down the column of numbers
            work = os.path.join(job, "working.pdf")
            for li in lis:
                for c in list(li.children):
                    if m.nodes[c].role == "Lbl":
                        m.remove(c, promote_children=False)
            d = pymupdf.open(work)
            boxes = [d[0].search_for(f"{k}.")[0] for k in range(1, 6)]
            d.close()
            rect = [min(b.x0 for b in boxes) - 1, boxes[0].y0 - 1, max(b.x1 for b in boxes) + 1, boxes[-1].y1 + 1]
            r = listedit.apply(m, work, Ls[0].id, {"op": "label_zone", "page": 0, "rect": rect})
            v = listedit.view(m, work, Ls[0].id)
            if len(r.get("touched", [])) != 5 or [i["lbl"] for i in v["items"]] != ["1.", "2.", "3.", "4.", "5."] \
                    or not v["items"][0]["body"].startswith("duck"):
                bad.append(f"label zone {r} {[(i['lbl'], i['body'][:20]) for i in v['items']]}")
            try:
                listedit.apply(m, work, Ls[0].id, {"op": "label_zone", "page": 0, "rect": [400, 700, 450, 720]})
                bad.append("a box over nothing was taken")
            except listedit.ListEditError:
                pass
    if bad:
        print("word grid test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("word grid test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
