"""V4.30 — a big file stays quick: what an edit changed is written, told and shown — and nothing more.

* core/modelsync.py: the copy of the tags (Shadow) finds every kind of change — a field, a list or a dictionary changed
  in place, a tag added, a tag gone, the file's own fields — and the file written from the kept pieces is, byte for
  byte as data, the file DocModel.to_json() writes
* through the app: every answer to a change carries a patch (base → rev: the tags changed, the tags gone, the reading
  numbers that moved, the file's own fields that changed); an editor that applies the patches holds EXACTLY what a
  new GET /model says — after a role, an alt text, a language, a move, an indent, a delete, a zone, a bulk change, the
  levels, the title, undo and redo
* too much (a whole page re-tagged) is told as "reload", and a change made outside mutate() is found at the next one
* GET /model and /lint: built once for each version of the file, 304 for a browser that holds it, gzip when it accepts it
* the lists of files (core/jobindex.py): a change of a file is in the next list, a deleted file is gone, nothing is
  read again that did not change
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp()
os.environ["PDFUA_JOBS"] = os.path.join(TMP, "jobs")
os.environ["PDFUA_CONFIG"] = os.path.join(TMP, "config.json")
os.environ["PDFUA_USERS"] = os.path.join(TMP, "users.json")
os.environ["PDFUA_SAMPLES"] = os.path.join(TMP, "samples")


def apply_patch(held: dict, p: dict) -> None:
    """What static/editor.js does with a patch (applyChain)."""
    for nid, n in p["nodes"].items():
        held["nodes"][nid] = n
    for nid in p["removed"]:
        held["nodes"].pop(nid, None)
    for nid, v in p["derived"].items():
        if nid in held["nodes"]:
            held["nodes"][nid]["num"], held["nodes"][nid]["block"] = v[0], v[1]
    held.update(p["top"])
    held["can_undo"], held["can_redo"], held["rev"] = p["can_undo"], p["can_redo"], p["rev"]
    k = 0
    stack = list(reversed(held["roots"]))
    while stack:
        n = held["nodes"].get(stack.pop())
        if n is None:
            continue
        k += 1
        n["order"] = k
        stack.extend(reversed(n["children"]))


def main() -> int:
    import app as A
    from core.modelsync import Shadow, TOP_FIELDS
    from core.model import DocModel
    bad: list = []

    def ok(cond, msg):
        if not cond:
            bad.append(msg)

    # ------------------------------------------------------------------ 1. the copy and the file written from it
    A.ACCOUNTS.create("anna", "secret-anna", role="production")
    k = A.app.test_client()
    k.post("/login", data={"username": "Kathir", "password": "Kathir@051408"})
    jids = {}
    for name in ("article.pdf", "book.pdf"):
        with open(os.path.join(ROOT, "tests", "corpus", name), "rb") as fh:
            r = k.post("/upload", data={"profile": "Default", "pdf": [(fh, name)]},
                       content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
        jids[name] = r.json["jobs"][0]
    for name, jid in jids.items():
        for _ in range(400):
            if k.get(f"/api/job/{jid}/status").json["stage"] == "review":
                break
            time.sleep(0.3)
    jid = jids["article.pdf"]
    path = os.path.join(A.JOBS, jid, "model.json")
    from core.pipeline import load_model
    m0 = load_model(os.path.join(A.JOBS, jid))
    sh = Shadow(m0, m0)
    ok(sh.diff(m0) == ([], [], []), "a fresh copy sees no change")
    ok(json.loads(sh.dump(m0)) == json.loads(m0.to_json()), "the file written from the pieces is the file DocModel writes")
    ids = list(m0.nodes)
    a, b = m0.nodes[ids[3]], m0.nodes[ids[5]]
    a.role = "Zz" + a.role                              # (certainly another role than it had)
    ch, gone, tops = sh.diff(m0)
    ok(ch == [a.id] and not gone and not tops, f"a field: {ch}")
    sh.commit(m0, ch, gone, tops)
    b.style["zzz"] = 1                                  # a dictionary changed in place
    b.children.append("n-not-there")                    # a list changed in place
    b.parts.append([0, 1.0, 2.0, 3.0, 4.0])
    ch, gone, tops = sh.diff(m0)
    ok(ch == [b.id], f"in-place changes: {ch}")
    sh.commit(m0, ch, gone, tops)
    del m0.nodes[ids[7]]
    m0.title = "A new title"
    m0.info["x"] = {"deep": [1, 2]}
    ch, gone, tops = sh.diff(m0)
    ok(gone == [ids[7]] and "title" in tops and "info" in tops, f"a tag gone, the file's fields: {gone} {tops}")
    sh.commit(m0, ch, gone, tops)
    m0.info["x"]["deep"].append(3)                       # one more, deep inside
    ch2, gone2, tops2 = sh.diff(m0)
    ok(tops2 == ["info"], f"a change deep in the file's own fields: {tops2}")
    sh.commit(m0, ch2, gone2, tops2)
    ok(sh.diff(m0) == ([], [], []), "committed: no difference left")
    ok(json.loads(sh.dump(m0)) == json.loads(m0.to_json()), "after all that the file is still what DocModel writes")
    from core.model import StructNode
    nn = StructNode(id="zz1", role="P", page=0)
    m0.add(nn, None)
    ch, gone, tops = sh.diff(m0)
    ok("zz1" in ch and "root_children" in tops, f"a new tag: {ch} {tops}")
    sh.commit(m0, ch, gone, tops)
    ok(json.loads(sh.dump(m0)) == json.loads(m0.to_json()), "a new tag is in the file")
    re = DocModel.from_json(sh.dump(m0))
    ok(set(re.nodes) == set(m0.nodes), "the file written from the pieces is read back")

    # ------------------------------------------------------------------ 2. the patches, through the app
    def fresh():
        return k.get(f"/api/job/{jid}/model").json

    def same(held: dict, fresh_: dict, what: str) -> None:
        if held["nodes"] != fresh_["nodes"]:
            diff_ids = [i for i in set(held["nodes"]) | set(fresh_["nodes"]) if held["nodes"].get(i) != fresh_["nodes"].get(i)]
            i = diff_ids[0]
            h, f = held["nodes"].get(i) or {}, fresh_["nodes"].get(i) or {}
            keys = [kk for kk in set(h) | set(f) if h.get(kk) != f.get(kk)]
            bad.append(f"{what}: the patched tags differ from a new load in {len(diff_ids)} tag(s), e.g. {i}: " +
                       ", ".join(f"{kk}: {h.get(kk)!r} ≠ {f.get(kk)!r}" for kk in keys[:3]))
        for key in ("roots", "title", "lang", "mode", "warnings", "info", "can_undo", "can_redo", "rev", "geom", "pages", "page_sizes"):
            if held.get(key) != fresh_.get(key):
                bad.append(f"{what}: {key} differs: {held.get(key)!r} ≠ {fresh_.get(key)!r}")

    held = fresh()
    ok(held.get("rev") and held.get("geom"), f"/model has rev and geom: {held.get('rev')} {held.get('geom')}")
    applied = {"n": 0, "reload": 0, "none": 0}

    def step(what: str, method: str, url: str, body: dict | None = None, expect_ok: bool = True):
        nonlocal held
        r = k.post(url, json=body or {})
        d = r.json
        if r.status_code != 200 or d.get("error"):
            if expect_ok:
                bad.append(f"{what}: {r.status_code} {str(d)[:120]}")
            held = fresh()
            return d
        p = d.get("patch")
        if p is None:
            applied["none"] += 1
            held = fresh()
        elif p.get("reload"):
            applied["reload"] += 1
            held = fresh()
        else:
            ok(p["base"] == held["rev"], f"{what}: the patch starts at the version the editor holds ({p['base']} vs {held['rev']})")
            apply_patch(held, p)
            applied["n"] += 1
            same(held, fresh(), what)
        return d

    nodes = held["nodes"]
    paras = [i for i, n in nodes.items() if n["role"] == "P" and not n["artifact"] and n["text"]]
    figs = [i for i, n in nodes.items() if n["role"] == "Figure"]
    lists = [i for i, n in nodes.items() if n["role"] == "L"]
    tables = [i for i, n in nodes.items() if n["role"] == "Table"]
    heads = [i for i, n in nodes.items() if n["role"].startswith("H") and n["role"][1:].isdigit()]
    p0, p1, p2, p3 = paras[0], paras[1], paras[2], paras[3]
    J = f"/api/job/{jid}"
    step("a role", "POST", f"{J}/node/{p0}", {"role": "H2"})
    step("a role back", "POST", f"{J}/node/{p0}", {"role": "P"})
    step("a language", "POST", f"{J}/node/{p1}", {"lang": "ta-IN"})
    if figs:
        step("alt text", "POST", f"{J}/node/{figs[0]}", {"alt": "A picture of a bar chart"})
    step("the title", "POST", f"{J}/doc", {"title": "A patched title", "lang": "en-GB"})
    step("move down", "POST", f"{J}/move", {"id": p2, "action": "down"})
    step("move up", "POST", f"{J}/move", {"id": p2, "action": "up"})
    step("indent", "POST", f"{J}/move", {"id": p3, "action": "indent"}, expect_ok=False)
    step("outdent", "POST", f"{J}/move", {"id": p3, "action": "outdent"}, expect_ok=False)
    step("bulk role", "POST", f"{J}/bulk", {"ids": [p0, p1], "action": "role", "role": "Caption"})
    step("undo", "POST", f"{J}/undo")
    step("undo again", "POST", f"{J}/undo")
    step("redo", "POST", f"{J}/redo")
    step("a zone", "POST", f"{J}/create", {"page": 0, "rect": [60, 600, 300, 640], "role": "P", "absorb": True, "split": "paragraphs"})
    step("undo the zone", "POST", f"{J}/undo")
    if heads:
        step("heading levels", "POST", f"{J}/levels", {"ids": [heads[-1]], "action": "heading_down"}, expect_ok=False)
    step("a delete", "POST", f"{J}/move", {"id": p3, "action": "delete"})
    step("undo the delete", "POST", f"{J}/undo")
    step("delete with children", "POST", f"{J}/move", {"id": paras[4], "action": "delete_subtree"})
    step("undo that", "POST", f"{J}/undo")
    step("bulk delete", "POST", f"{J}/bulk", {"ids": [paras[5], paras[6]], "action": "delete"})
    step("undo bulk", "POST", f"{J}/undo")
    if lists:
        step("a list item", "POST", f"{J}/move", {"id": lists[0], "action": "down"}, expect_ok=False)
    if tables:
        step("table headers", "POST", f"{J}/table/{tables[0]}", {"header_rows": 2, "header_cols": 0, "mode": "auto"}, expect_ok=False)
    step("zone delete", "POST", f"{J}/zone/delete", {"page": 0, "rect": [40, 40, 560, 120]}, expect_ok=False)
    step("undo zone delete", "POST", f"{J}/undo", expect_ok=False)
    ok(applied["n"] >= 14, f"most changes came as patches: {applied}")
    ok(applied["none"] <= 3, f"hardly any change without a patch: {applied}")

    # the file on disk is the file of the tags in memory, after all those changes
    on_disk = json.load(open(path, encoding="utf-8"))
    live = json.loads(A.get_model(jid).to_json())
    ok(on_disk == live, "model.json is exactly what the tags in memory say")
    re = load_model(os.path.join(A.JOBS, jid))
    ok(json.loads(re.to_json()) == on_disk, "and reads back the same")

    # ------------------------------------------------------------------ 3. too much, and changes outside mutate()
    r = k.post(f"{J}/tags/remove", json={"all": True})
    p = r.json.get("patch") or {}
    ok(r.status_code == 200 and (p.get("reload") or len(p.get("removed", [])) > 0), f"a whole file cleared: {str(p)[:100]}")
    held = fresh()
    k.post(f"{J}/undo")
    held = fresh()
    with A.app.test_request_context():
        A.g.user = {"username": "Kathir", "role": "admin"}
        with A.mutate(jid) as model:                                # an edit function that changes things in place
            n = model.nodes[paras[0]]
            n.style["inplace"] = "yes"
            n.parts = [list(x) for x in n.parts]
    ok(json.load(open(path, encoding="utf-8"))["nodes"][paras[0]]["style"].get("inplace") == "yes", "an in-place change is written")
    A.get_model(jid).nodes[paras[1]].alt = "changed outside mutate()"      # (something that forgot to go through mutate)
    with A.app.test_request_context():
        A.g.user = {"username": "Kathir", "role": "admin"}
        with A.mutate(jid) as model:
            pass
    ok(json.load(open(path, encoding="utf-8"))["nodes"][paras[1]]["alt"] == "changed outside mutate()",
       "…and is found and written by the next change")

    # ------------------------------------------------------------------ 4. the answers are built once, 304, gzip
    r1 = k.get(f"{J}/model", headers={"Accept-Encoding": "gzip"})
    ok(r1.headers.get("Content-Encoding") == "gzip" and r1.headers.get("ETag"), f"the tags travel compressed with an ETag: {dict(r1.headers)}")
    data = json.loads(gzip.decompress(r1.data))
    ok(data["rev"], "…and unpack to the tags")
    r2 = k.get(f"{J}/model", headers={"If-None-Match": r1.headers["ETag"]})
    ok(r2.status_code == 304 and not r2.data, f"a browser that holds this version gets a 304: {r2.status_code}")
    k.post(f"{J}/node/{paras[2]}", json={"role": "H3"})
    r3 = k.get(f"{J}/model", headers={"If-None-Match": r1.headers["ETag"]})
    ok(r3.status_code == 200 and r3.headers["ETag"] != r1.headers["ETag"], "after a change it is a new version")
    l1 = k.get(f"{J}/lint", headers={"Accept-Encoding": "gzip"})
    l2 = k.get(f"{J}/lint", headers={"If-None-Match": l1.headers["ETag"]})
    ok(l1.status_code == 200 and l2.status_code == 304, f"the issues likewise {l1.status_code}/{l2.status_code}")
    plain = k.get(f"{J}/model")
    ok(plain.headers.get("Content-Encoding") is None and json.loads(plain.data)["rev"], "without gzip it is plain")
    cs = k.get("/static/editor.js", headers={"Accept-Encoding": "gzip"})
    ok(cs.headers.get("Content-Encoding") == "gzip" and b"function" in gzip.decompress(cs.data), "scripts travel compressed")
    ok("immutable" in (k.get("/static/editor.js?v=1").headers.get("Cache-Control") or ""), "a versioned script is kept by the browser")

    # ------------------------------------------------------------------ 5. the lists of files
    names = [m["filename"] for m in A.list_jobs()]
    ok(set(names) >= {"article.pdf", "book.pdf"}, f"both files are listed {names}")
    A.update_meta(jids["book.pdf"], work="in_progress", priority="urgent")
    row = next(m for m in A.list_jobs() if m["id"] == jids["book.pdf"])
    ok(row.get("priority") == "urgent", "a change of a file is in the next list")
    admin_rows = {r["id"]: r for r in k.get("/api/admin/files").json}
    ok(admin_rows[jids["book.pdf"]]["priority"] == "urgent", "also in Admin → Files")
    e1 = k.get("/api/admin/files")
    e2 = k.get("/api/admin/files", headers={"If-None-Match": e1.headers["ETag"]})
    ok(e2.status_code == 304, f"the list is not sent again when nothing changed: {e2.status_code}")
    k.post(f"/job/{jids['book.pdf']}/delete")
    ok(jids["book.pdf"] not in {m["id"] for m in A.list_jobs()}, "a deleted file is gone from the list")
    parsed = A.JOBINDEX.stats["parsed"]
    for _ in range(5):
        A.list_jobs()
    ok(A.JOBINDEX.stats["parsed"] == parsed, "listing again reads nothing again")

    if bad:
        print("patch test: FAILED")
        for b_ in bad[:30]:
            print("   ", b_)
        return 1
    print(f"patch test: OK  ({applied['n']} changes as patches, {applied['reload']} reload, {applied['none']} without)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
