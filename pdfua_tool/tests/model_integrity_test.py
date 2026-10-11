"""V4.33 — "Build PDF" never stops on a table or a row that is already gone, and no edit leaves the tags in a state a later step trips over.

The failure that was reported (Build PDF):

    ValueError: list.index(x): x not in list
      core/build.py  eff.detach(tb.id)   →   core/model.py  detach  →  sib.index(nid)

`repair.drop_duplicate_rows` took a table that had no rows out of the tree but left it in the model; the cleanup that follows in
build() met the same table a second time and detached it again. Found, kept from happening again, and the operations of the editor
that could leave the tree in a state like it (a tag hung twice, a tag left out of the tree) looked for with a fuzz:

* detach / remove / siblings do not stop on a node that is out of the tree already (they say so: -1) — nor on a parent that is gone
* drop_duplicate_rows and the cleanup of build() take what they drop out of the model, not only out of the tree
* Build PDF with an empty table next to a row that is dropped (the reported case), with a blank row on top of a real one, with a
  table that has a caption but no rows (the caption stays), and with a table that is in the model but in no list: all build, and
  the file passes qpdf, the structure checks and the marked-content balance
* "Group by headings" (convert_table_columns) on a table whose headings and words hang straight from it: every tag once
* "Reading position" of an artifact is refused with a reason, not with KeyError
* a fuzz of the editor's operations (roles, delete, wrap, lists, rows, merge, move, drop, levels, empty tags, remove tags, group by
  headings, zones drawn and deleted, geometry, split, rebuild, reading order …): after each one the tree is still a tree — no tag
  out of it, none hung twice, every parent right — nothing stops, and a file built from the result builds
"""
from __future__ import annotations

import collections
import os
import random
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import edit as E  # noqa: E402
from core.build import build  # noqa: E402
from core.edit import EditError  # noqa: E402
from core.model import DocModel, StructNode  # noqa: E402
from core.options import Options  # noqa: E402
from core.pipeline import load_model, process_file  # noqa: E402
from core.repair import drop_duplicate_rows  # noqa: E402
from tests.verify import mc_balance, qpdf_check, structure_integrity  # noqa: E402

CORPUS = os.path.join(ROOT, "tests", "corpus")


def tree_problems(m: DocModel) -> list[str]:
    """What is wrong with the tree, without mending it first."""
    out: list[str] = []
    reach: dict[str, str | None] = {}
    stack = [(r, None) for r in m.root_children]
    twice = 0
    while stack:
        nid, par = stack.pop()
        if nid not in m.nodes:
            out.append(f"{nid} is listed but is not in the model")
            continue
        if nid in reach:
            twice += 1
            continue
        reach[nid] = par
        n = m.nodes[nid]
        if n.parent != par:
            out.append(f"the parent of {nid} ({n.role}) says {n.parent!r}, it hangs from {par!r}")
        stack += [(c, nid) for c in n.children]
    if twice:
        out.append(f"{twice} tag(s) hang twice")
    loose = [i for i in m.nodes if i not in reach]
    if loose:
        out.append(f"{len(loose)} tag(s) are in the model but in no list: " + ", ".join(f"{i}:{m.nodes[i].role}" for i in loose[:4]))
    return out


def main() -> int:
    bad: list = []
    work = tempfile.mkdtemp()

    def ok(cond, msg):
        if not cond:
            bad.append(msg)

    # ------------------------------------------------------------------ the model's own operations
    m = DocModel(path="x.pdf", page_count=1, page_sizes=[(595, 842)])
    a = m.add(StructNode(id="a", role="Table", page=0), None)
    b = m.add(StructNode(id="b", role="TR", page=0), "a")
    c = m.add(StructNode(id="c", role="TD", page=0), "b")
    ok(m.detach("b") == 0 and m.detach("b") == -1, "detach says where the node was, and -1 when it was out of the tree already")
    ok(m.detach("a") == 0 and m.detach("a") == -1, "…also for a table that is out of the tree")
    ok(m.attached_ids() == set(), "nothing is attached any more")
    m.remove("b")                                                      # out of the tree already: nothing to promote into
    ok("b" not in m.nodes and "c" not in m.nodes, "remove of a tag that is out of the tree takes it (and what hangs from it) out of the model")
    m2 = DocModel(path="x.pdf", page_count=1, page_sizes=[(595, 842)])
    m2.add(StructNode(id="p", role="Sect", page=0), None)
    m2.add(StructNode(id="q", role="P", page=0), "p")
    del m2.nodes["p"]                                                  # the parent is gone
    ok(m2.siblings("q") is m2.root_children and m2.detach("q") == -1, "a node whose parent is gone hangs from the root, and detaching it does not raise")
    m3 = DocModel(path="x.pdf", page_count=1, page_sizes=[(595, 842)])
    m3.add(StructNode(id="s", role="Sect", page=0), None)
    m3.add(StructNode(id="t", role="Table", page=0), "s")
    m3.add(StructNode(id="u", role="Caption", page=0, text="Table 1"), "t")
    m3.remove("t", promote_children=True)
    ok(m3.nodes["u"].parent == "s" and m3.nodes["s"].children == ["u"] and "t" not in m3.nodes, "remove with promote keeps what hangs from the node, where the node was")
    m4 = DocModel(path="x.pdf", page_count=1, page_sizes=[(595, 842)])
    for i in range(3):
        m4.add(StructNode(id=f"h{i}", role="Table", page=0, source="auto"), None)
    m4.add(StructNode(id="orphan-husk", role="Table", page=0), None)
    m4.root_children.remove("orphan-husk")                             # in the model, in no list: the shape the bug left behind
    ok(any("no list" in x for x in tree_problems(m4)), "the checker of this test sees a tag that is in no list")
    fixed = m4.check_integrity()
    ok(not tree_problems(m4) and any("orphan" in f for f in fixed), f"check_integrity mends it ({fixed})")

    # ------------------------------------------------------------------ "Group by headings" on a table whose tags hang straight from it
    g = DocModel(path="x.pdf", page_count=1, page_sizes=[(595, 842)])
    g.add(StructNode(id="T", role="Table", page=0, bbox=(40, 40, 400, 200)), None)
    for k, (x0, name) in enumerate([(50, "Initial"), (220, "Final")]):
        g.add(StructNode(id=f"H{k}", role="TH", page=0, bbox=(x0, 50, x0 + 100, 70), text=name), "T")
        for j, word in enumerate(["ace", "age", "ape"]):
            g.add(StructNode(id=f"W{k}{j}", role="P", page=0, bbox=(x0, 90 + 20 * j, x0 + 60, 105 + 20 * j), text=word), "T")
    try:
        r = E.convert_table_columns(g, "T")
        ok(not tree_problems(g), f"group by headings, with the headings and words straight under the table: {tree_problems(g)}")
        ok(r["groups"] == 2 and r["cells"] == 6, f"…two groups, six words ({r})")
    except Exception as exc:  # noqa: BLE001
        ok(False, f"group by headings stopped: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ Build PDF
    def prepared(pdf="tables.pdf"):
        job = os.path.join(work, pdf[:-4] + str(len(os.listdir(work))))
        r = process_file(os.path.join(CORPUS, pdf), job, Options(fix_contrast="off"))
        assert r.get("status") == "done", r
        return job, load_model(job)

    def built(model, job, label):
        out = os.path.join(job, f"out_{label}.pdf")
        try:
            stats = build(model, os.path.join(job, "working.pdf"), out, Options(fix_contrast="off"))
        except Exception as exc:  # noqa: BLE001
            ok(False, f"{label}: Build PDF stopped: {type(exc).__name__}: {exc}")
            return None, None
        ok(qpdf_check(out) == "ok", f"{label}: qpdf --check: {qpdf_check(out)}")
        ok(not structure_integrity(out), f"{label}: the structure is whole: {structure_integrity(out)[:3]}")
        ok(not mc_balance(out), f"{label}: marked content is balanced: {mc_balance(out)[:3]}")
        return stats, out

    # the reported case: a table with no rows + a row nobody can give content to, which build() drops
    job, mod = prepared()
    tb = next(n for n in mod.nodes.values() if n.role == "Table")
    mod.add(StructNode(id=mod.new_id("tb"), role="Table", page=0, source="user"), None)
    tr = StructNode(id=mod.new_id("tr"), role="TR", page=0, source="user")
    mod.add(tr, tb.id)
    mod.add(StructNode(id=mod.new_id("td"), role="TD", page=0, source="user", bbox=None), tr.id)
    stats, out = built(mod, job, "empty table + a row that goes")
    ok(stats and stats.get("empty_rows_dropped") == 1, f"the row without content went: {stats and stats.get('empty_rows_dropped')}")

    # a blank row printed on top of a real one + an empty table
    job, mod = prepared()
    tb = next(n for n in mod.nodes.values() if n.role == "Table")
    mod.add(StructNode(id=mod.new_id("tb"), role="Table", page=0, source="user"), None)
    tr = StructNode(id=mod.new_id("tr"), role="TR", page=0, source="user", bbox=(40, 700, 300, 720))
    mod.add(tr, tb.id)
    mod.add(StructNode(id=mod.new_id("td"), role="TD", page=0, source="user", bbox=(40, 700, 300, 720)), tr.id)
    stats, out = built(mod, job, "blank row on top of a real one")

    # a table with a caption but no rows: the caption stays
    job, mod = prepared()
    cap = StructNode(id=mod.new_id("cp"), role="Caption", page=0, source="user", text="Table 9. Nothing here", bbox=(50, 60, 300, 75))
    husk = mod.add(StructNode(id=mod.new_id("tb"), role="Table", page=0, source="user"), None)
    mod.add(cap, husk.id)
    tr = StructNode(id=mod.new_id("tr"), role="TR", page=0, source="user")
    mod.add(tr, next(n for n in mod.nodes.values() if n.role == "Table" and n.id != husk.id).id)
    mod.add(StructNode(id=mod.new_id("td"), role="TD", page=0, source="user", bbox=None), tr.id)
    eff_ids = []
    stats, out = built(mod, job, "table with a caption and no rows")
    # (the model itself is not changed by a build)
    ok(husk.id in mod.nodes, "the build works on a copy: the model keeps its tags")

    # a table that is in the model but in no list at all (what an older edit left behind)
    job, mod = prepared()
    orphan = StructNode(id=mod.new_id("tb"), role="Table", page=0, source="user")
    mod.nodes[orphan.id] = orphan
    ok(any("no list" in x for x in tree_problems(mod)), "(the orphan is there)")
    tr = StructNode(id=mod.new_id("tr"), role="TR", page=0, source="user")
    mod.add(tr, next(n for n in mod.nodes.values() if n.role == "Table" and n.id != orphan.id).id)
    mod.add(StructNode(id=mod.new_id("td"), role="TD", page=0, source="user", bbox=None), tr.id)
    built(mod, job, "a table in no list")

    # drop_duplicate_rows alone leaves nothing behind
    job, mod = prepared()
    tb = next(n for n in mod.nodes.values() if n.role == "Table")
    mod.add(StructNode(id=mod.new_id("tb"), role="Table", page=0, source="user"), None)
    tr = StructNode(id=mod.new_id("tr"), role="TR", page=0, source="user", bbox=(40, 700, 300, 720))
    mod.add(tr, tb.id)
    mod.add(StructNode(id=mod.new_id("td"), role="TD", page=0, source="user", bbox=(40, 700, 300, 720)), tr.id)
    before = len(mod.nodes)
    n_gone = drop_duplicate_rows(mod)
    ok(n_gone == 1 and not tree_problems(mod), f"drop_duplicate_rows: {n_gone} row(s), the tree after: {tree_problems(mod)}")
    ok(len(mod.nodes) < before - 1, f"…what it took out of the tree is out of the model ({before} → {len(mod.nodes)} tags)")

    # ------------------------------------------------------------------ the reading position of an artifact
    job, mod = prepared("article.pdf")
    art = next(n for n in mod.nodes.values() if n.artifact and n.parent is None)
    try:
        E.set_order(mod, art.id, 3)
        ok(False, "an artifact was given a reading position")
    except EditError as exc:
        ok("artifact" in str(exc), f"an artifact has no reading position: {exc}")
    except Exception as exc:  # noqa: BLE001
        ok(False, f"…but it stopped with {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ the fuzz of the editor's operations
    def fuzz(seed: int, files: list[str], trials: int, with_build: bool) -> collections.Counter:
        rnd = random.Random(seed)
        found: collections.Counter = collections.Counter()
        o = Options(fix_contrast="off")
        for fn in files:
            job_, base = prepared(fn)
            wpdf = os.path.join(job_, "working.pdf")
            for trial in range(trials):
                mm = DocModel.from_json(base.to_json())
                for k in range(rnd.randint(1, 4)):
                    ids = [n.id for n in mm.ordered_nodes()]
                    if not ids:
                        break
                    pick = lambda c=1: rnd.sample(ids, min(c, len(ids)))  # noqa: E731
                    op = rnd.choice(["delete", "artifact", "role", "wrap", "make_list", "table_rows", "merge", "move", "drop", "levels",
                                     "create_empty", "clear_pages", "tidy", "convert_cols", "table_headers", "zone_create", "zone_create",
                                     "zone_delete", "geometry", "split_at", "rebuild", "merge_parts", "reorder", "set_order", "fix_order"])
                    try:
                        if op == "delete":
                            E.bulk(mm, pick(rnd.randint(1, 3)), "delete")
                        elif op == "artifact":
                            E.bulk(mm, pick(2), "artifact")
                        elif op == "role":
                            E.bulk(mm, pick(2), "role", rnd.choice(["P", "H2", "Table", "TR", "TD", "L", "LI", "Figure", "Caption", "Sect", "Div"]))
                        elif op == "wrap":
                            E.bulk(mm, pick(rnd.randint(1, 3)), "wrap", rnd.choice(["Sect", "Div", "BlockQuote", "L", "Table"]))
                        elif op == "make_list":
                            E.bulk(mm, pick(rnd.randint(1, 4)), "make_list")
                        elif op == "table_rows":
                            E.bulk(mm, pick(rnd.randint(1, 3)), "table_rows")
                        elif op == "merge":
                            E.bulk(mm, pick(2), "merge")
                        elif op == "move":
                            E.move(mm, pick()[0], rnd.choice(["up", "down", "outdent", "indent"]))
                        elif op == "drop":
                            x, y = pick(2)
                            E.drop(mm, [x], y, rnd.choice(["before", "after", "into"]))
                        elif op == "levels":
                            E.levels(mm, pick(rnd.randint(1, 3)), rnd.choice(["up", "down", "indent", "outdent"]))
                        elif op == "create_empty":
                            E.create_empty(mm, rnd.choice(["P", "TD", "TR", "LI", "Lbl", "LBody", "Figure"]), pick()[0], rnd.choice(["after", "before", "into"]), "x")
                        elif op == "clear_pages":
                            E.clear_pages(mm, [rnd.randrange(max(1, mm.page_count))])
                        elif op == "tidy":
                            E.tidy_tables(mm)
                        elif op == "convert_cols":
                            tbs = [n.id for n in mm.nodes.values() if n.role == "Table"]
                            if tbs:
                                E.convert_table_columns(mm, rnd.choice(tbs), rnd.choice(["cells", "rows"]))
                        elif op == "table_headers":
                            tbs = [n.id for n in mm.nodes.values() if n.role == "Table"]
                            if tbs:
                                E.table_headers(mm, rnd.choice(tbs), rnd.randint(0, 2), rnd.randint(0, 1))
                        else:
                            boxed = [n for n in mm.nodes.values() if n.bbox is not None]
                            if not boxed:
                                continue
                            nd = rnd.choice(boxed)
                            pad = rnd.choice([0, 2, 6, 14])
                            rect = [max(0, nd.bbox[0] - pad), max(0, nd.bbox[1] - pad), nd.bbox[2] + pad, nd.bbox[3] + pad]
                            if rnd.random() < 0.3:
                                b2 = rnd.choice(boxed)
                                if b2.page == nd.page:
                                    rect = [min(rect[0], b2.bbox[0]), min(rect[1], b2.bbox[1]), max(rect[2], b2.bbox[2]), max(rect[3], b2.bbox[3])]
                            if op == "zone_create":
                                E.create_from_rect(mm, wpdf, nd.page, rect, rnd.choice(["P", "H2", "Table", "L", "Figure", "Artifact", "Caption", "Link", "Span", "Note"]),
                                                   absorb=rnd.random() < 0.5, opts=o, deep=rnd.random() < 0.3, split=rnd.choice([True, False, "paragraphs", "lines"]),
                                                   inside=rnd.random() < 0.5)
                            elif op == "zone_delete":
                                E.delete_in_zone(mm, wpdf, nd.page, rect)
                            elif op == "geometry":
                                E.set_geometry(mm, wpdf, nd.id, None, nd.page, rect)
                            elif op == "split_at":
                                E.split_at(mm, wpdf, nd.id, [(nd.bbox[1] + nd.bbox[3]) / 2])
                            elif op == "rebuild":
                                E.rebuild_structured(mm, wpdf, nd.id, o)
                            elif op == "merge_parts":
                                E.merge_parts(mm, [x.id for x in rnd.sample(boxed, min(2, len(boxed)))], None, "never")
                            elif op == "reorder":
                                E.reorder_blocks(mm, nd.page, [x.id for x in E.page_blocks(mm, nd.page)][::-1][:rnd.randint(2, 5)])
                            elif op == "set_order":
                                E.set_order(mm, nd.id, rnd.randint(1, 8))
                            elif op == "fix_order":
                                E.fix_page_order(mm, o)
                    except EditError:
                        continue                          # a refusal with a reason is what an operation may answer
                    except Exception as exc:  # noqa: BLE001
                        found[f"{fn}: {op} stopped: {type(exc).__name__}: {str(exc)[:70]}"] += 1
                        break
                    pr = tree_problems(mm)
                    if pr:
                        found[f"{fn}: after {op}: {pr[0][:100]}"] += 1
                        break
                    if with_build and rnd.random() < 0.3:
                        try:
                            build(DocModel.from_json(mm.to_json()), wpdf, os.path.join(job_, "fz.pdf"), o)
                        except Exception as exc:  # noqa: BLE001
                            found[f"{fn}: Build PDF after {op} stopped: {type(exc).__name__}: {str(exc)[:70]}"] += 1
                            break
        return found

    found = fuzz(7, ["tables.pdf", "article.pdf", "book.pdf", "forms.pdf"], 45, with_build=True)
    found += fuzz(8, ["tables.pdf", "article.pdf"], 45, with_build=False)
    for key, n in found.most_common(6):
        ok(False, f"fuzz: {n} × {key}")

    shutil.rmtree(work, ignore_errors=True)
    if bad:
        print("model integrity / Build PDF test: FAILED")
        for b_ in bad:
            print("   ", b_)
        return 1
    print("model integrity / Build PDF test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
