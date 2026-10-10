"""V4.28 — zones, through the app: a cut by hand is offered at the other places and learned live.

A new agency sets its paragraphs apart by a small gap; the tool takes a page for one zone. A person
draws two zones; then, through the app:

* the tool offers the same cut at the other places of the file (the same space between the lines);
  applied, every paragraph has a zone of its own; Ctrl+Z puts the whole offer back, Ctrl+Y again
* a few seconds after the last edit a learner process (not the web app) writes what the person did
  to the learning database — no build needed
* built and approved, the file becomes a sample with its zone rows; the agency's model earns its place
* the next upload of the agency has every paragraph in a zone of its own, untouched
* the switch in Admin turns the learning off and on; the admin page shows the zones
"""
from __future__ import annotations

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
os.environ["PDFUA_LIVE_DELAY"] = "0.6"


def main() -> int:
    from tests.zone_gen import make_pub
    import app as A
    from core import learn, zonelearn
    bad: list = []

    def ok(cond, msg):
        if not cond:
            bad.append(msg)

    c = A.app.test_client()
    c.post("/login", data={"username": "Kathir", "password": "Kathir@051408"})
    r = c.post("/api/profiles", json={"name": "Agency", "options": {"use_samples": True}})
    ok(r.status_code == 200, f"profile saved {r.status_code}")

    def wait(jid, n=900):
        for _ in range(n):
            st = c.get(f"/api/job/{jid}/status").json.get("stage")
            if st in ("review", "done", "error"):
                return st
            time.sleep(0.3)
        return "timeout"

    def upload(path, profile="Agency"):
        r = c.post("/upload", data={"profile": profile, "pdf": [(open(path, "rb"), os.path.basename(path))]},
                   content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
        return r.json["jobs"][0]

    def bodies(jid):
        mm_ = c.get(f"/api/job/{jid}/model").json
        return [n for n in mm_["nodes"].values() if n["role"] == "P" and len(n.get("text") or "") > 80], mm_

    p1 = os.path.join(TMP, "agency1.pdf")
    truth1 = make_pub(p1, seed=31, pages=3)
    j1 = upload(p1)
    ok(wait(j1) == "review", "agency file analysed")
    nodes, mdl = bodies(j1)
    total = sum(len(t) for t in truth1)
    ok(len(nodes) < total / 2, f"the tool joins the paragraphs of a new agency: {len(nodes)} zones")
    ok(bool(mdl.get("rev")), "the model carries its version")
    sim = c.get(f"/api/job/{j1}/zones/similar").json
    ok(sim.get("ok") and sim["corrections"] == 0, f"nothing to offer before a fix {sim}")

    # the person cuts one place: the first paragraph of page 1 gets a zone of its own, the rest another
    big0 = [n for n in nodes if n["page"] == 0][0]
    ok(c.post(f"/api/job/{j1}/move", json={"id": big0["id"], "action": "delete"}).json.get("ok", True), "merged zone removed")
    x0, y0, x1, y1, _ = truth1[0][0]
    r1 = c.post(f"/api/job/{j1}/create", json={"page": 0, "rect": [x0 - 1, y0 - 1, x1 + 1, y1 + 1], "role": "P", "split": False})
    ry1 = truth1[0][-1][3]
    r2 = c.post(f"/api/job/{j1}/create", json={"page": 0, "rect": [60, truth1[0][1][1] - 1, 530, ry1 + 1], "role": "P", "split": False})
    ok(r1.json.get("ok") and r2.json.get("ok"), f"the two zones drawn {r1.json} {r2.json}")
    sim = c.get(f"/api/job/{j1}/zones/similar").json
    ok(sim["cut"]["n"] >= 5, f"the same cut offered at the other places: {sim['cut']['n']}")
    ap = c.post(f"/api/job/{j1}/zones/similar/apply", json={"action": "cut", "pairs": sim["cut"]["pairs"]})
    ok(ap.json.get("changed", 0) >= 5, f"applied {ap.json}")
    nodes, mdl = bodies(j1)
    ok(len(nodes) == total, f"every paragraph in its own zone after the offer: {len(nodes)} of {total}")
    inv = c.post(f"/api/job/{j1}/inventory", json={}).json
    ok(inv["summary"]["coverage"]["missed"] == 0, f"no line is left without a tag after the cuts {inv['summary']['coverage']}")
    # the links inside the paragraphs (the \"see page N\" references) went with the paragraph they are printed in
    mdl_now = c.get(f"/api/job/{j1}/model").json
    links = [n for n in mdl_now["nodes"].values() if n["role"] == "Link"]
    ok(links and all(mdl_now["nodes"][n["parent"]]["role"] == "P" and mdl_now["nodes"][n["parent"]]["page"] == n["page"]
                     for n in links), f"every link is inside a paragraph of its own page ({len(links)})")
    # undo puts the whole apply back at once
    c.post(f"/api/job/{j1}/undo")
    nodes_u, _ = bodies(j1)
    ok(len(nodes_u) < total, f"undo of the offer: {len(nodes_u)}")
    c.post(f"/api/job/{j1}/redo")
    nodes, mdl = bodies(j1)
    ok(len(nodes) == total, f"redo {len(nodes)}")

    # the learner process writes the rows a few seconds after the last edit
    n = 0
    for _ in range(80):
        with learn.connect() as db:
            n = db.execute("SELECT COUNT(*) n FROM zone_rows WHERE ref=?", ("fb:" + j1,)).fetchone()["n"]
        if n:
            break
        time.sleep(0.5)
    ok(n > 10, f"live learning wrote {n} rows without a build")
    for _ in range(60):
        if A.LIVE_STATS["runs"] >= 1 and not A.LIVE_STATS["running"]:
            break
        time.sleep(0.5)
    ok(A.LIVE_STATS["runs"] >= 1 and A.LIVE_STATS["failed"] == 0, f"learner runs {A.LIVE_STATS}")

    # build and approve: the file becomes a sample with its zone rows
    c.post(f"/api/job/{j1}/build", json={})
    ok(wait(j1) in ("done", "review"), "built")
    built = os.path.join(A.JOBS, j1, "accessible.pdf")
    ok(os.path.exists(built), "the accessible PDF exists")
    if os.path.exists(built):
        from tests.verify import mc_balance, structure_integrity
        ok(not structure_integrity(built) and not mc_balance(built), f"the built tags are sound {structure_integrity(built)[:2]}")
        import pikepdf
        pdf = pikepdf.open(built)
        ps, stack = [], [pdf.Root.StructTreeRoot.K]
        while stack:
            k = stack.pop()
            if isinstance(k, pikepdf.Array):
                stack.extend(list(k))
            elif isinstance(k, pikepdf.Dictionary) and "/S" in k:
                if str(k.S) == "/P":
                    ps.append(k)
                if "/K" in k:
                    stack.append(k.K)
        ok(len(ps) >= total, f"the file has a P tag for every paragraph ({len(ps)} of {total})")
    r = c.post(f"/api/job/{j1}/work", json={"action": "approve"})
    ok(r.json.get("ok", True) is not False, f"approved {r.json}")
    rows = []
    for _ in range(160):
        rows = c.get("/api/admin/samples").json["samples"]
        if rows and all(x["status"] in ("ready", "error") for x in rows):
            break
        time.sleep(0.5)
    ok(rows and rows[0]["status"] == "ready", f"the approved file became a sample {[(x['status'], x.get('error')) for x in rows]}")
    time.sleep(1.0)
    st = zonelearn.status()
    ag = [m for m in st["models"] if m["scope"] == "Agency"]
    ok(st["by_kind"] and sum(st["by_kind"].values()) > 0, f"zone rows by kind {st['by_kind']}")
    ok(not ag or ag[0]["status"] in ("active", "waiting", "off"), f"Agency model {ag}")

    # the next file of the agency
    p2 = os.path.join(TMP, "agency2.pdf")
    truth2 = make_pub(p2, seed=32, pages=3)
    j2 = upload(p2)
    ok(wait(j2) == "review", "second agency file analysed")
    nodes2, _ = bodies(j2)
    ok(len(nodes2) == sum(len(t) for t in truth2), f"the next file of the agency: {len(nodes2)} of {sum(len(t) for t in truth2)}")

    # the other way round: a double-spaced manuscript — every line a zone of its own; a person joins the lines of one
    # paragraph, the tool offers to join the lines of the others
    p4 = os.path.join(TMP, "manuscript1.pdf")
    truth4 = make_pub(p4, seed=51, pages=2, pitch=24.0, size=12.0, gap_pt=14.0)
    j4 = upload(p4)
    ok(wait(j4) == "review", "manuscript analysed")

    def lines_of(jid, min_len=20):
        mm_ = c.get(f"/api/job/{jid}/model").json
        return [n for n in mm_["nodes"].values() if n["role"] == "P" and len(n.get("text") or "") > min_len]

    lines = lines_of(j4)
    paras4 = sum(len(t) for t in truth4)
    ok(len(lines) > 2 * paras4, f"the rule takes every line for a zone: {len(lines)} zones, {paras4} paragraphs")
    x0, y0, x1, y1, _ = truth4[0][0]
    first = sorted([n for n in lines if n["page"] == 0 and n["rects"] and y0 - 2 <= n["rects"][0][2] <= y1 + 2],
                   key=lambda n: n["rects"][0][2])
    ok(len(first) >= 3, f"the first paragraph is {len(first)} zones")
    for _ in range(len(first) - 1):
        r = c.post(f"/api/job/{j4}/move", json={"id": first[0]["id"], "action": "merge_next"})
        ok(r.json.get("ok", True) and not r.json.get("error"), f"merged {r.json}")
    sim = c.get(f"/api/job/{j4}/zones/similar").json
    ok(sim["join"]["n"] >= 10, f"the same join offered at the other places: {sim['join']['n']} (corrections {sim['corrections']})")
    before_apply = len(lines_of(j4))
    ap = c.post(f"/api/job/{j4}/zones/similar/apply", json={"action": "join", "pairs": sim["join"]["pairs"]})
    ok(ap.json.get("changed", 0) >= 10, f"joined {ap.json}")
    lines = lines_of(j4)
    ok(len(lines) == paras4, f"every paragraph is one zone now: {len(lines)} of {paras4}")
    c.post(f"/api/job/{j4}/undo")
    ok(len(lines_of(j4)) == before_apply, f"one undo puts all the joins back: {len(lines_of(j4))} of {before_apply}")

    # the switch, the admin page
    b = c.get("/api/admin/brain").json
    ok("zones" in b and "live" in b, f"admin brain shows zones and the learner {sorted(b)[:12]}")
    ok(c.post("/api/admin/learning", json={"zones": False}).json["learning"]["zones"] is False, "zones switch off")
    p3 = os.path.join(TMP, "agency3.pdf")
    truth3 = make_pub(p3, seed=33, pages=3)
    j3 = upload(p3)
    ok(wait(j3) == "review", "analysed with zone learning off")
    nodes3, _ = bodies(j3)
    ok(len(nodes3) < sum(len(t) for t in truth3) / 2, f"zone learning off → the rule alone: {len(nodes3)}")
    ok(c.post("/api/admin/learning", json={"zones": True}).json["learning"]["zones"] is True, "zones switch on")
    tr = c.post("/api/admin/zones/train", json={})
    ok(tr.json.get("ok"), f"train by hand {tr.json}")
    page = c.get("/admin")
    ok(page.status_code == 200 and b"zoneCard" in page.data and b'data-learn="zones"' in page.data, "the admin page has the zones card")

    # a file that is not the person's cannot be reached through the zone endpoints
    c2 = A.app.test_client()
    ok(c2.get(f"/api/job/{j1}/zones/similar").status_code in (401, 403), "sign in first")

    if bad:
        print("zone learning (app) test: FAILED")
        for b_ in bad:
            print("   ", b_)
        return 1
    print("zone learning (app) test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
