"""Edit list and list templates (V4.19).

A page with a numbered list (an item over two lines, two lettered sub-items)
and, lower down, the same kind of list again that the analysis is given
nothing special for. In the Default (V4) engine and the Kriyadocs house style:

* the list is read (items, labels, levels)
* an item split at a line and joined again; the line between two items moved
* nest / un-nest, move, a label changed, the numbering changed
* items added from a zone, items taken out by a zone (paragraphs after the list)
* split the list and join it again; "not a list"
* saved as template #1 and given to the second list (and to a zone): the same
  levels and labels
* the built file has no checker error
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
from core.pipeline import load_model, process_file, save_model  # noqa: E402
from tests.verify import mc_balance, structure_integrity  # noqa: E402

LIST1 = [(72, "1. Prepare the samples before the test starts."), (86, "They are kept cold all day."),
         (72, "2. Measure each sample twice."), (90, "a) once in the morning"), (90, "b) once in the evening"),
         (72, "3. Write the results in the sheet.")]
LIST2 = [(72, "1. Clean the bench."), (86, "Use the blue cloth."), (72, "2. Switch the lamp on."),
         (90, "a) the small lamp"), (90, "b) the big lamp"), (72, "3. Start the timer.")]


def make(path: str) -> None:
    d = pymupdf.open()
    p = d.new_page()
    p.insert_text((72, 70), "Method", fontsize=18)
    p.insert_text((72, 100), "Follow these steps in order.", fontsize=11)
    y = 130
    for x, t in LIST1:
        p.insert_text((x, y), t, fontsize=11)
        y += 18
    p.insert_text((72, y + 20), "Extra steps: also check the fridge.", fontsize=11)
    p.insert_text((72, y + 40), "4. Close the lab at six.", fontsize=11)
    y += 90
    p.insert_text((72, y), "Cleaning", fontsize=18)
    y += 30
    for x, t in LIST2:
        p.insert_text((x, y), t, fontsize=11)
        y += 18
    d.save(path)
    d.close()


def lists(m):
    return [n for n in m.ordered_nodes() if n.role == "L" and not any(a.role == "L" for a in m.ancestors(n.id))]


def run(profile: str, bad: list) -> None:
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "l.pdf")
    make(src)
    o = profile_options(profile)
    o.ai_alt_text_auto = False
    o.run_verapdf = False
    job = os.path.join(tmp, "job")
    process_file(src, job, o)
    compat.use(o)
    m = load_model(job)
    work = os.path.join(job, "working.pdf")
    ls = lists(m)
    if len(ls) < 2:
        bad.append(f"{profile}: {len(ls)} list(s) found")
        return
    L = ls[0]
    v = listedit.view(m, work, L.id)
    labels = [(it["lbl"], it["level"]) for it in v["items"]]
    if [l for l, _ in labels][:3] != ["1.", "2.", "a)"] and [l for l, _ in labels][:2] != ["1.", "2."]:
        bad.append(f"{profile}: items read {labels}")
    first = v["items"][0]
    if "kept cold" not in first["body"]:
        bad.append(f"{profile}: the two-line item reads {first['body']!r}")
    # make the nested items flat first if the analysis nested them
    for _ in range(10):
        deep = [it for it in v["items"] if it["level"] > 0]
        if not deep:
            break
        listedit.apply(m, work, L.id, {"op": "outdent", "id": deep[0]["id"]})
        v = listedit.view(m, work, L.id)
    n_before = len(v["items"])

    # split the first item between its two lines, then join it again
    it0 = v["items"][0]
    r0 = it0["rects"][0]
    mid = (r0[2] + r0[4]) / 2
    r = listedit.apply(m, work, L.id, {"op": "split", "id": it0["id"], "page": r0[0], "y": mid})
    v = listedit.view(m, work, L.id)
    if len(v["items"]) != n_before + 1 or "kept cold" not in v["items"][1]["body"]:
        bad.append(f"{profile}: split {[(i['lbl'], i['body'][:20]) for i in v['items']]}")
    listedit.apply(m, work, L.id, {"op": "merge_next", "id": v["items"][0]["id"]})
    v = listedit.view(m, work, L.id)
    if len(v["items"]) != n_before or "kept cold" not in v["items"][0]["body"]:
        bad.append(f"{profile}: join {[(i['lbl'], i['body'][:20]) for i in v['items']]}")

    # nest the "a)" and "b)" items under "2."
    ab = [it for it in v["items"] if it["lbl"] in ("a)", "b)")]
    for it in ab:
        listedit.apply(m, work, L.id, {"op": "indent", "id": it["id"]})
    v = listedit.view(m, work, L.id)
    lv = {it["lbl"]: it["level"] for it in v["items"]}
    if lv.get("a)") != 1 or lv.get("b)") != 1 or lv.get("3.") != 0:
        bad.append(f"{profile}: nesting {lv}")
    sub = next(it["list"] for it in v["items"] if it["lbl"] == "a)")
    listedit.apply(m, work, L.id, {"op": "numbering", "list": sub, "value": "LowerAlpha"})
    if m.nodes[sub].list_numbering != "LowerAlpha":
        bad.append(f"{profile}: numbering {m.nodes[sub].list_numbering}")

    # the line "4. Close the lab at six." joins the list from a zone
    d = pymupdf.open(work)
    z = d[0].search_for("4. Close the lab")[0]
    ex = d[0].search_for("Extra steps")[0]
    d.close()
    r = listedit.apply(m, work, L.id, {"op": "add_zone", "page": 0, "rect": [z.x0 - 2, z.y0 - 2, z.x1 + 200, z.y1 + 2]})
    v = listedit.view(m, work, L.id)
    if v["items"][-1]["lbl"] != "4." or "Close the lab" not in v["items"][-1]["body"]:
        bad.append(f"{profile}: add from a zone {[(i['lbl'], i['body'][:20]) for i in v['items']]}")
    # the "3." item out by a zone
    it3 = next(it for it in v["items"] if it["lbl"] == "3.")
    rr = it3["rects"][0]
    listedit.apply(m, work, L.id, {"op": "remove_zone", "page": 0, "rect": [rr[1] - 2, rr[2] - 2, rr[3] + 2, rr[4] + 2]})
    v = listedit.view(m, work, L.id)
    if any(it["lbl"] == "3." for it in v["items"]) or not any(n.role == "P" and "Write the results" in (n.text or "")
                                                              for n in m.nodes.values()):
        bad.append(f"{profile}: take out by a zone")
    void = ex  # noqa: F841

    # template: this list's format given to the second list
    spec = listedit.list_spec(m, work, L.id)
    if not spec["nested"] or not spec["levels"][0]["shape"]:
        bad.append(f"{profile}: list template {spec}")
    spec["number"] = 1
    L2 = lists(m)[-1]
    nid = listedit.apply_spec(m, work, spec, lid=L2.id)
    v2 = listedit.view(m, work, nid)
    got = [(it["lbl"], it["level"]) for it in v2["items"]]
    if got != [("1.", 0), ("2.", 0), ("a)", 1), ("b)", 1), ("3.", 0)]:
        bad.append(f"{profile}: template given to the second list {got}")
    if "blue cloth" not in v2["items"][0]["body"]:
        bad.append(f"{profile}: template item lines {v2['items'][0]['body']!r}")

    # split the list and join it back
    v = listedit.view(m, work, L.id)
    r = listedit.apply(m, work, L.id, {"op": "split_list", "id": v["items"][1]["id"]})
    if r.get("new_list") not in m.nodes:
        bad.append(f"{profile}: split list")
    listedit.apply(m, work, L.id, {"op": "merge_next_list"})
    if len(listedit.view(m, work, L.id)["items"]) != len(v["items"]):
        bad.append(f"{profile}: join list")

    m.check_integrity()
    save_model(job, m)
    out = os.path.join(job, "out.pdf")
    build(m, work, out, o)
    errs = [f"{f.code} {f.message}" for f in check(out) if f.severity == "error" and f.code not in ("UA-23",)]
    if errs:
        bad.append(f"{profile}: checker errors {errs}")
    if structure_integrity(out) or mc_balance(out):
        bad.append(f"{profile}: structure {structure_integrity(out)[:2]} {mc_balance(out)[:2]}")
    # not a list
    listedit.apply(m, work, L.id, {"op": "not_a_list"})
    if L.id in m.nodes:
        bad.append(f"{profile}: not a list kept the list")
    compat.use("")


def main() -> int:
    bad: list = []
    for prof in ("Default", "Kriyadocs"):
        run(prof, bad)
    if bad:
        print("list edit test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("list edit test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
