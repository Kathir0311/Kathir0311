"""V4.30 — speed: a file of thousands of tags and a server of thousands of files stay quick.

Budgets are generous (a slow machine passes); they exist to catch the kind of regression that made one changed tag
take seconds — a whole-file save, a whole-file reload, a list that reads every file again.

* a file of ~4000 tags: an edit (role, alt text, delete, undo) answers in well under a second and its answer carries the
  patch (a few KB, not the file); GET /model the second time is a 304 or the same bytes; the issues likewise
* edits of one file while others read it (the editor's heartbeat, the issues, the tags): nobody waits for the edit to end
  — the heartbeat, which takes no lock, answers while a change is being made
* a server of 1500 files: the list is built once, listed again for nothing; the queue badge, the file list of the Users
  page and Admin → Files answer in a blink the second time (and with a 304)
* the learner of live learning runs at low priority; model.json is never written in place (a hard-linked undo copy stays whole)
"""
from __future__ import annotations

import copy
import json
import os
import statistics
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp()
os.environ["PDFUA_JOBS"] = os.path.join(TMP, "jobs")
os.environ["PDFUA_CONFIG"] = os.path.join(TMP, "config.json")
os.environ["PDFUA_USERS"] = os.path.join(TMP, "users.json")
os.environ["PDFUA_SAMPLES"] = os.path.join(TMP, "samples")


def main() -> int:
    import app as A
    from core.pipeline import load_model, save_model
    bad: list = []
    notes: list = []

    def ok(cond, msg):
        if not cond:
            bad.append(msg)

    k = A.app.test_client()
    k.post("/login", data={"username": "Kathir", "password": "Kathir@051408"})
    with open(os.path.join(ROOT, "tests", "corpus", "article.pdf"), "rb") as fh:
        r = k.post("/upload", data={"profile": "Default", "pdf": [(fh, "article.pdf")]},
                   content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
    jid = r.json["jobs"][0]
    for _ in range(300):
        if k.get(f"/api/job/{jid}/status").json["stage"] == "review":
            break
        time.sleep(0.3)
    d = os.path.join(A.JOBS, jid)

    # ------------------------------------------------------------------ a big file: article.pdf's tags, many times
    m = load_model(d)
    paras = [n for n in m.nodes.values() if n.role == "P" and not n.artifact]
    i = 0
    while len(m.nodes) < 4000:
        src = paras[i % len(paras)]
        n = copy.deepcopy(src)
        n.id, n.children, n.parent = f"zz{i}", [], None
        m.add(n, None)
        i += 1
    save_model(d, m)
    A._model_cache.pop(jid, None)
    A._SHADOWS.pop(jid, None)
    J = f"/api/job/{jid}"

    t0 = time.perf_counter()
    r1 = k.get(f"{J}/model", headers={"Accept-Encoding": "gzip"})
    t_first = time.perf_counter() - t0
    nodes = len(k.get(f"{J}/model").json["nodes"])
    ok(nodes >= 4000, f"a big file: {nodes} tags")
    t0 = time.perf_counter()
    r2 = k.get(f"{J}/model", headers={"If-None-Match": r1.headers["ETag"]})
    t_304 = time.perf_counter() - t0
    ok(r2.status_code == 304 and t_304 < 0.25, f"the tags again: {r2.status_code} in {t_304 * 1000:.0f} ms (first {t_first * 1000:.0f} ms)")
    t0 = time.perf_counter()
    k.get(f"{J}/lint")
    t_lint1 = time.perf_counter() - t0
    t0 = time.perf_counter()
    r = k.get(f"{J}/lint", headers={"If-None-Match": k.get(f"{J}/lint").headers["ETag"]})
    t_lint2 = time.perf_counter() - t0
    ok(r.status_code == 304 and t_lint2 < max(0.1, t_lint1), f"the issues again: {r.status_code} {t_lint2 * 1000:.0f} ms (first {t_lint1 * 1000:.0f} ms)")

    held = k.get(f"{J}/model").json
    ids = [x for x, n in held["nodes"].items() if n["role"] == "P" and x.startswith("zz")]
    k.get(f"/job/{jid}")                                                        # the editor opens: the person has the file
    # the first edit after loading pays for the copy of the tags, made while the editor opens in real life
    k.post(f"{J}/node/{ids[0]}", json={"role": "H3"})
    ts, sizes, reloads = [], [], 0
    for j in range(12):
        nid = ids[10 + j]
        t0 = time.perf_counter()
        r = k.post(f"{J}/node/{nid}", json={"role": "H2" if j % 2 else "H3"})
        ts.append(time.perf_counter() - t0)
        sizes.append(len(r.data))
        p = r.json.get("patch") or {}
        if p.get("reload") or not p:
            reloads += 1
    med = statistics.median(ts) * 1000
    notes.append(f"edit of one tag in a file of {nodes} tags: median {med:.0f} ms, worst {max(ts) * 1000:.0f} ms, answer {max(sizes) / 1024:.1f} KB")
    ok(med < 600 and max(ts) < 1500, f"an edit is quick: median {med:.0f} ms, worst {max(ts) * 1000:.0f} ms")
    ok(max(sizes) < 30000, f"…and its answer is the change, not the file: {max(sizes)} bytes")
    ok(reloads == 0, f"…every answer carried a patch ({reloads} did not)")
    t0 = time.perf_counter()
    r = k.post(f"{J}/move", json={"id": ids[40], "action": "delete"})
    t_del = time.perf_counter() - t0
    ok(r.status_code == 200 and (r.json.get("patch") or {}).get("removed") == [ids[40]] and t_del < 1.5, f"a delete: {t_del * 1000:.0f} ms {str(r.json)[:80]}")
    t0 = time.perf_counter()
    r = k.post(f"{J}/undo", json={})
    t_undo = time.perf_counter() - t0
    ok(r.status_code == 200 and "patch" in r.json and t_undo < 2.0, f"undo: {t_undo * 1000:.0f} ms")
    # the undo copy is a whole, older model.json — never the file being written
    undo_dir = os.path.join(d, "history", "undo")
    files = sorted(os.listdir(undo_dir))
    ok(files and all(json.load(open(os.path.join(undo_dir, f), encoding="utf-8")).get("nodes") for f in files[-3:]), "the undo copies are whole files")

    # ------------------------------------------------------------------ somebody edits while others read
    stop = {"v": False}
    lat: dict[str, list] = {"presence": [], "edit": []}

    def editor():
        c = A.app.test_client()
        c.post("/login", data={"username": "Kathir", "password": "Kathir@051408"})
        n = 0
        while not stop["v"]:
            t0 = time.perf_counter()
            c.post(f"{J}/node/{ids[100 + n % 50]}", json={"role": "H2" if n % 2 else "H3"})
            lat["edit"].append(time.perf_counter() - t0)
            n += 1

    def beat():
        c = A.app.test_client()
        c.post("/login", data={"username": "Kathir", "password": "Kathir@051408"})
        while not stop["v"]:
            t0 = time.perf_counter()
            c.get(f"{J}/presence?rev=&page=0&hold=1&active=1&w=1.0")
            lat["presence"].append(time.perf_counter() - t0)
            time.sleep(0.02)

    ths = [threading.Thread(target=editor), threading.Thread(target=beat), threading.Thread(target=beat)]
    [t.start() for t in ths]
    time.sleep(3.0)
    stop["v"] = True
    [t.join() for t in ths]
    p95 = sorted(lat["presence"])[int(0.95 * (len(lat["presence"]) - 1))] * 1000 if lat["presence"] else 9999
    ok(len(lat["edit"]) >= 5, f"edits went on ({len(lat['edit'])})")
    ok(p95 < 400, f"the heartbeat answers while edits are made: p95 {p95:.0f} ms over {len(lat['presence'])} beats")
    notes.append(f"heartbeats during edits: p95 {p95:.0f} ms; {len(lat['edit'])} edits in 3 s")

    # ------------------------------------------------------------------ a server of many files
    n_files = 1500
    with open(os.path.join(d, "meta.json"), encoding="utf-8") as fh:
        meta = json.load(fh)
    for q in range(n_files):
        fid = f"2025{(q // 28) % 12 + 1:02d}{q % 28 + 1:02d}-000000-{q:06x}"
        os.makedirs(os.path.join(A.JOBS, fid), exist_ok=True)
        json.dump(dict(meta, id=fid, filename=f"f{q}.pdf", owner="Kathir", assigned_to="", work="", time=None),
                  open(os.path.join(A.JOBS, fid, "meta.json"), "w", encoding="utf-8"))
    A.JOBINDEX.touch()
    t0 = time.perf_counter()
    first = A.list_jobs(100000)
    t_first = time.perf_counter() - t0
    parsed = A.JOBINDEX.stats["parsed"]
    t0 = time.perf_counter()
    for _ in range(5):
        A.list_jobs(100000)
    t_again = (time.perf_counter() - t0) / 5
    ok(len(first) >= n_files and A.JOBINDEX.stats["parsed"] == parsed, f"{len(first)} files listed; listing again reads nothing")
    ok(t_again < 0.08 and t_again < t_first, f"a list of {n_files} files: first {t_first * 1000:.0f} ms, again {t_again * 1000:.1f} ms")
    notes.append(f"list of {n_files} files: first {t_first * 1000:.0f} ms, again {t_again * 1000:.1f} ms")
    for name, path in (("the queue badge", "/api/wf/queue"), ("the Users page list", "/api/jobs"), ("Admin → Files", "/api/admin/files")):
        r1 = k.get(path)
        t0 = time.perf_counter()
        r2 = k.get(path, headers={"If-None-Match": r1.headers.get("ETag", "")})
        t304 = time.perf_counter() - t0
        t0 = time.perf_counter()
        k.get(path)
        t_same = time.perf_counter() - t0
        ok(r2.status_code == 304 and t304 < 0.1, f"{name}: a 304 in {t304 * 1000:.0f} ms ({r2.status_code})")
        ok(t_same < 0.5, f"{name}: again in {t_same * 1000:.0f} ms")
        notes.append(f"{name}: 304 in {t304 * 1000:.0f} ms, again {t_same * 1000:.0f} ms")
    page = k.get("/")
    ok(page.status_code == 200 and len(page.data) < 400_000, f"the Users page with {n_files} files: {len(page.data) // 1024} KB")
    # one file changes: the next list has it, nothing else is read
    parsed = A.JOBINDEX.stats["parsed"]
    A.update_meta(jid, priority="urgent")
    row = next(m_ for m_ in A.list_jobs(100000) if m_["id"] == jid)
    ok(row["priority"] == "urgent" and A.JOBINDEX.stats["parsed"] == parsed + 1, f"one file changed, one file read ({A.JOBINDEX.stats['parsed'] - parsed})")

    # ------------------------------------------------------------------ the learner is nice
    import inspect
    src = inspect.getsource(A._live_run)
    ok("nice" in src and "BELOW_NORMAL_PRIORITY_CLASS" in src, "the learner runs below the web server's priority")

    for n_ in notes:
        print("   ·", n_)
    if bad:
        print("speed test: FAILED")
        for b_ in bad[:20]:
            print("   ", b_)
        return 1
    print("speed test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
