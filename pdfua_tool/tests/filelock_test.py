"""V4.29 — one person at a time in a file; the others can look and cannot change anything.

* the lock itself (core/editlock.py): taken, renewed, refused to anybody else, forced by an admin,
  let go of, ended when the editor stops asking (ttl) or the person does nothing (idle); ten threads
  asking at the same moment — exactly one gets it
* through the app: the editor page of the first person takes the file; the second person's page is a
  view (class lockedview, window.LOCK) — they can read the tags, the lint, the pictures, the summary —
  and every change (a role, a build, undo, an AI draft, re-analyse, delete, a profile, a zone …) is
  refused with 423 and the name of who has the file; an admin takes a file over; the file list and
  Admin → Files show who has it; an admin can unlock it; the file moving on in the workflow lets go;
  a person who looks only (?view=1) takes nothing; the switch in Admin → Workflow turns it all off
"""
from __future__ import annotations

import os
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
    from core.editlock import EditLocks
    import app as A
    bad: list = []

    def ok(cond, msg):
        if not cond:
            bad.append(msg)

    # ------------------------------------------------------------------ 1. the lock
    L = EditLocks(ttl=0.4, idle=0.8)
    ok(L.holder("f") is None, "free at first")
    got, h = L.acquire("f", "Anna")
    ok(got and h["user"] == "Anna", f"Anna takes it {h}")
    got, h = L.acquire("f", "anna")
    ok(got, "the same person again (any case) renews")
    got, h = L.acquire("f", "Ben")
    ok(not got and h["user"] == "Anna", f"Ben is refused and told who has it {h}")
    got, h = L.renew("f", "Ben")
    ok(not got, "an editor that thinks it has the file does not take it from the holder")
    got, h = L.acquire("f", "Ben", force=True)
    ok(got and L.holder("f")["user"] == "Ben" and L.stats["taken_over"] == 1, "an admin takes it over")
    ok(not L.release("f", "Anna") and L.holder("f") is not None, "only the holder lets go")
    ok(L.release("f", "Ben") and L.holder("f") is None, "the holder lets go")
    L.acquire("f", "Anna")
    time.sleep(0.5)
    ok(L.holder("f") is None, "an editor that stops asking loses the file (ttl)")
    ok(L.stats["expired"] >= 1, "counted")
    L.acquire("g", "Anna")
    for _ in range(5):
        time.sleep(0.2)
        L.renew("g", "Anna", active=False)               # the tab is open and asks, the person does nothing
    ok(L.holder("g") is None, "a person who does nothing loses the file (idle) although the tab is open")
    L.acquire("g", "Anna")
    for _ in range(6):
        time.sleep(0.2)
        L.renew("g", "Anna", active=True)
    ok(L.holder("g") is not None, "a person who works keeps it")
    L2 = EditLocks(ttl=30, idle=30)
    wins: list = []
    ts = [threading.Thread(target=lambda i=i: wins.append(L2.acquire("race", f"u{i}")[0])) for i in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    ok(sum(wins) == 1, f"ten at the same moment: {sum(wins)} wins")

    # ------------------------------------------------------------------ 2. through the app
    A.ACCOUNTS.create("boss", "secret-boss", role="admin")
    A.ACCOUNTS.create("anna", "secret-anna", role="production")
    A.ACCOUNTS.create("raj", "secret-raj", role="qa")

    def client(u, p):
        c_ = A.app.test_client()
        c_.post("/login", data={"username": u, "password": p})
        return c_

    k, b = client("Kathir", "Kathir@051408"), client("boss", "secret-boss")
    an = client("anna", "secret-anna")
    with open(os.path.join(ROOT, "tests", "corpus", "article.pdf"), "rb") as fh:
        r = k.post("/upload", data={"profile": "Default", "pdf": [(fh, "article.pdf")]},
                   content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
    jid = r.json["jobs"][0]
    for _ in range(300):
        if k.get(f"/api/job/{jid}/status").json["stage"] == "review":
            break
        time.sleep(0.3)
    m = k.get(f"/api/job/{jid}/model").json
    paras = [i for i, n in m["nodes"].items() if n["role"] == "P"]
    nid = paras[0]

    ok(k.get(f"/api/job/{jid}/lock").json["free"], "nobody has the file")
    page = k.get(f"/job/{jid}").get_data(as_text=True)
    ok("lockedview" not in page.split("<body")[1][:300] and "window.LOCK" in page, "the first person's page is an editor")
    lk = k.get(f"/api/job/{jid}/lock").json
    ok(lk["holder"] == "Kathir" and lk["mine"], f"opening the editor took the file {lk}")

    # the second person: a view
    pg = b.get(f"/job/{jid}").get_data(as_text=True)
    ok("lockedview" in pg.split("<body")[1][:300], "the second person's page is a view")
    ok('"holder":"Kathir"' in pg.replace(" ", "") and '"mine":false' in pg.replace(" ", ""), "and carries who has the file")
    for name, fn in (("tags", lambda: b.get(f"/api/job/{jid}/model")), ("lint", lambda: b.get(f"/api/job/{jid}/lint")),
                     ("summary", lambda: b.get(f"/api/job/{jid}/summary")), ("picture", lambda: b.get(f"/job/{jid}/page/0.png?z=1.2")),
                     ("status", lambda: b.get(f"/api/job/{jid}/status")), ("presence", lambda: b.get(f"/api/job/{jid}/presence?rev=&page=0")),
                     ("inventory", lambda: b.post(f"/api/job/{jid}/inventory", json={})),
                     ("the original", lambda: b.get(f"/job/{jid}/download/original"))):
        r = fn()
        ok(r.status_code == 200, f"the second person can look: {name} {r.status_code}")
    changes = (("a role", "POST", f"/api/job/{jid}/node/{nid}", {"role": "H2"}),
               ("a move", "POST", f"/api/job/{jid}/move", {"id": nid, "action": "delete"}),
               ("undo", "POST", f"/api/job/{jid}/undo", {}),
               ("a build", "POST", f"/api/job/{jid}/build", {}),
               ("the document", "POST", f"/api/job/{jid}/doc", {"title": "x"}),
               ("a zone", "POST", f"/api/job/{jid}/create", {"page": 0, "rect": [60, 100, 300, 150], "role": "P"}),
               ("an AI draft", "POST", f"/api/job/{jid}/ai/headings", {}),
               ("a zone cut offer", "POST", f"/api/job/{jid}/zones/similar/apply", {"action": "cut", "pairs": [0]}),
               ("a profile", "POST", f"/api/job/{jid}/profile", {"profile": "Default"}),
               ("re-analyse", "POST", f"/job/{jid}/reanalyze", {}),
               ("delete", "POST", f"/job/{jid}/delete", {}))
    for name, method, path, body in changes:
        r = b.post(path, json=body)
        ok(r.status_code == 423 and r.json.get("locked") and r.json.get("by") == "Kathir", f"the second person cannot change {name}: {r.status_code} {r.get_data(as_text=True)[:100]}")
    ok(k.get(f"/api/job/{jid}/model").json["nodes"][nid]["role"] == "P", "nothing was changed")
    r = k.post(f"/api/job/{jid}/node/{nid}", json={"role": "H2"})
    ok(r.status_code == 200 and r.json.get("ok"), f"the holder changes it {r.status_code}")
    ok(k.post(f"/api/job/{jid}/node/{nid}", json={"role": "P"}).status_code == 200, "and back")

    # the editor's "I am still here"
    pr = b.get(f"/api/job/{jid}/presence?rev=&page=0").json
    ok(pr["lock"] and pr["lock"]["holder"] == "Kathir" and not pr["lock"]["mine"], f"presence tells the viewer who has it {pr.get('lock')}")
    pr = k.get(f"/api/job/{jid}/presence?rev=&page=0&hold=1&active=1").json
    ok(pr["lock"]["mine"], "and the holder that it is theirs")
    ok(b.post(f"/api/job/{jid}/lock", json={"action": "acquire"}).json["ok"] is False, "a request to take it is refused")
    r = b.post(f"/api/job/{jid}/lock", json={"action": "bogus"})
    ok(r.status_code == 400, "unknown step")

    # the file lists
    jl = {j["id"]: j for j in k.get("/api/jobs").json}
    ok(jl[jid].get("locked_by") == "Kathir" and jl[jid].get("lock_mine"), f"the holder's list {jl[jid].get('locked_by')}")
    jl = {j["id"]: j for j in b.get("/api/jobs").json}
    ok(jl[jid].get("locked_by") == "Kathir" and not jl[jid].get("lock_mine"), "the other person's list shows who has it")
    af = {j["id"]: j for j in b.get("/api/admin/files").json}
    ok(af[jid].get("locked_by") == "Kathir", "Admin → Files shows it")
    sysl = b.get("/api/admin/system").json["load"]["file_locks"]
    ok(sysl["on"] and jid in sysl["held"], f"Admin → System counts it {sysl['locked']}")

    # an admin takes the file; the person who had it can look only
    r = b.post(f"/api/job/{jid}/lock", json={"action": "takeover"})
    ok(r.json["ok"] and r.json["mine"], "an admin takes the file over")
    r = k.post(f"/api/job/{jid}/node/{nid}", json={"role": "H2"})
    ok(r.status_code == 423 and r.json["by"] == "boss", f"the person who had it is held back now {r.status_code}")
    ok(an.post(f"/api/job/{jid}/lock", json={"action": "takeover"}).status_code in (403, 400), "a production user cannot take a file over")
    # let go through the beacon (JSON body, as the editor sends it on leaving)
    r = b.post(f"/api/job/{jid}/lock", data='{"action": "release"}', content_type="application/json")
    ok(r.json["free"], "the editor lets go when it is closed")
    ok(k.post(f"/api/job/{jid}/node/{nid}", json={"role": "P"}).status_code == 200, "free: anybody may change it (a script that never opened the editor takes nothing)")
    ok(A.EDITLOCK.holder(jid) is None, "…and holds nothing after")
    # an editor whose hold ran out takes the free file again with its next change
    r = k.post(f"/api/job/{jid}/node/{nid}", json={"role": "P"}, headers={"X-UA-Edit": "1"})
    ok(r.status_code == 200 and A.EDITLOCK.holder(jid)["user"] == "Kathir", "an editor's change takes a free file")
    r = b.post(f"/api/job/{jid}/node/{nid}", json={"role": "H2"}, headers={"X-UA-Edit": "1"})
    ok(r.status_code == 423, "and keeps it from the others")

    # the admin's steps for many files leave a file somebody works in alone
    k.get(f"/job/{jid}")                                  # Kathir has it
    r = b.post("/api/admin/files/reanalyze", json={"ids": [jid]})
    ok(r.json["started"] == 0 and len(r.json["skipped"]) == 1 and "Kathir" in r.json["skipped"][0], f"re-analyse leaves it alone {r.json}")
    r = b.post("/api/admin/files/build", json={"ids": [jid]})
    ok(r.json["started"] == 0 and len(r.json["skipped"]) == 1, f"build leaves it alone {r.json}")
    r = b.post("/api/admin/files/profile", json={"ids": [jid], "profile": "Book"})
    ok(r.json.get("changed") == 0 and len(r.json["skipped"]) == 1, f"a new profile leaves it alone {r.json}")
    r = b.post("/api/admin/files/delete", json={"ids": [jid]})
    ok(r.json["count"] == 0 and len(r.json["skipped"]) == 1 and os.path.isdir(os.path.join(A.JOBS, jid)), f"delete leaves it alone {r.json}")
    ok(k.post("/api/admin/files/build", json={"ids": [jid]}).json["skipped"] == [], "the one who has the file may build it")
    for _ in range(300):
        if k.get(f"/api/job/{jid}/status").json["stage"] in ("review", "done"):
            break
        time.sleep(0.3)
    # an admin frees a file nobody should hold
    r = b.post("/api/admin/files/unlock", json={"ids": [jid]})
    ok(r.json["unlocked"] == 1 and A.EDITLOCK.holder(jid) is None, "an admin unlocks the file")

    # looking only (?view=1) takes nothing; "Edit this file" takes it
    pg = b.get(f"/job/{jid}?view=1").get_data(as_text=True)
    ok(A.EDITLOCK.holder(jid) is None and '"view_only":true' in pg.replace(" ", ""), "?view=1 takes nothing")
    ok("lockedview" in pg.split("<body")[1][:300], "and is a view")
    r = b.post(f"/api/job/{jid}/lock", json={"action": "acquire"})
    ok(r.json["ok"] and r.json["mine"], "Edit this file takes it")
    A.EDITLOCK.release(jid, force=True)

    # the lock ends by itself when the editor goes quiet
    A.EDITLOCK.ttl = 1.0
    k.get(f"/job/{jid}")
    ok(b.post(f"/api/job/{jid}/node/{nid}", json={"role": "H2"}).status_code == 423, "held while the editor is there")
    time.sleep(1.3)
    ok(b.post(f"/api/job/{jid}/node/{nid}", json={"role": "H2"}).status_code == 200, "free after the editor went quiet")
    A.EDITLOCK.ttl = 40.0
    A.EDITLOCK.release(jid, force=True)

    # the file moves on in the workflow: nobody has it
    k.get(f"/job/{jid}")
    A._work_changed(jid, "qa")
    ok(A.EDITLOCK.holder(jid) is None, "sent on: the person who had it is done")

    # a person who cannot change the file at this step gets no lock at all (the workflow decides)
    A.ACCOUNTS.update("anna", role="production")
    ok(an.get(f"/api/job/{jid}/lock").status_code == 403, "a file that is not theirs stays closed")

    # the switch
    r = k.post("/api/admin/workflow", json={"file_lock": False})
    ok(r.status_code == 200, "the switch is saved")
    A._LOCK_CFG[0] = 0
    k.get(f"/job/{jid}")
    ok(A.EDITLOCK.holder(jid) is None, "off: opening the editor takes nothing")
    ok(b.post(f"/api/job/{jid}/node/{nid}", json={"role": "P"}).status_code == 200, "off: everybody may change")
    pg = b.get(f"/job/{jid}").get_data(as_text=True)
    ok("lockedview" not in pg.split("<body")[1][:300], "off: no view-only page")
    sw = k.get("/api/admin/workflow").json
    ok("file_lock" in sw["cfg"] and "file_lock" in sw["setting_order"], "the switch is listed in Admin → Workflow")
    k.post("/api/admin/workflow", json={"file_lock": True})
    A._LOCK_CFG[0] = 0

    # the static pieces
    js = open(os.path.join(ROOT, "static", "editor.js"), encoding="utf-8").read()
    ok("function setLock" in js and "lockedView" in js and "pagehide" in js, "the editor handles the lock")
    cj = open(os.path.join(ROOT, "static", "common.js"), encoding="utf-8").read()
    ok("X-UA-Edit" in cj and "lockedview" in cj and "423" in cj, "every change of the editor says it is the editor and stops when it is only looking")

    if bad:
        print("file lock test: FAILED")
        for b_ in bad:
            print("   ", b_)
        return 1
    print("file lock test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
