"""V4.28 — many people at the same time.

* FairRLock: re-entrant, serves the waiting in the order they came, times out, counts the waiting
* FairQueue: one file of each person in turn, an urgent file first, a file made urgent moves up
* config.json: a change by one admin never wipes out another's, and templates / privacy settings stay
* through the app (PDFUA_LOCK_AUDIT=1: a request that opens a PDF without the lock fails):
  - a person who uploads four files does not keep the others waiting (queue positions)
  - while one thing holds MuPDF for seconds, reading and changing the tags, the summary, the status and
    a picture already drawn do not wait for it
  - eight people work at once, two of them in the same file: no error, no lost change, sound tags
  - the second person sees that the first changed the file (version + who is here)
  - twelve admins save twelve profiles at the same moment: all twelve are there
  - Admin → System shows how busy the server is
"""
from __future__ import annotations

import json
import os
import queue
import random
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
os.environ["PDFUA_LOCK_AUDIT"] = "1"
os.environ["PDFUA_LIVE_DELAY"] = "0.5"


def main() -> int:
    import app as A
    from core.fairqueue import FairQueue
    from core.locks import FairRLock
    bad: list = []

    def ok(cond, msg):
        if not cond:
            bad.append(msg)

    # ------------------------------------------------------------------ 1. the lock
    lk = FairRLock()
    lk.acquire()
    lk.acquire()
    ok(lk._is_owned(), "owned")
    lk.release()
    ok(lk._is_owned(), "still owned after one release (re-entrant)")
    lk.release()
    ok(not lk._is_owned(), "released")
    try:
        lk.release()
        ok(False, "a release without acquire is refused")
    except RuntimeError:
        pass
    order: list[int] = []
    lk.acquire()
    ts = []
    for i in range(6):
        def w(i=i):
            with lk:
                order.append(i)
                time.sleep(0.01)
        t = threading.Thread(target=w)
        t.start()
        ts.append(t)
        while lk.waiting() < i + 1:                     # the next one comes only when this one waits
            time.sleep(0.005)
    lk.release()
    [t.join() for t in ts]
    ok(order == list(range(6)), f"served in the order they came: {order}")
    lk.acquire()
    got = []
    t = threading.Thread(target=lambda: got.append(lk.acquire(timeout=0.2)))
    t.start()
    t.join()
    ok(got == [False] and lk.snapshot()["timeouts"] == 1, f"a wait that times out: {got} {lk.snapshot()}")
    t = threading.Thread(target=lambda: got.append(lk.acquire(blocking=False)))
    t.start()
    t.join()
    ok(got[-1] is False, "a non-blocking acquire fails while held")
    lk.release()
    snap = lk.snapshot()
    ok(snap["requests"] >= 8 and snap["waited"] >= 5, f"the waiting is counted {snap}")

    # ------------------------------------------------------------------ 2. the queue
    who = {f"a{i}": "A" for i in range(8)}
    who.update({"b1": "B", "b2": "B", "c1": "C"})
    rank = {"c1": 0.0}
    q = FairQueue(key=lambda it: (who[it[0]], rank.get(it[0], 2.0)))
    for j in who:
        q.put((j, "prepare"))
    ok([j for j, _ in q.queue][:5] == ["c1", "a0", "b1", "a1", "b2"], f"people in turn: {[j for j, _ in q.queue][:6]}")
    ok(q.waiting_by() == {"A": 8, "B": 2, "C": 1}, f"who is waiting {q.waiting_by()}")
    who["b2"] = "B"
    rank["b2"] = 0.0
    q.rekey(lambda it: it[0] == "b2")
    ok([j for j, _ in q.queue][0] == "b2", f"a file made urgent goes first {[j for j, _ in q.queue][:3]}")
    got = [q.get()[0] for _ in range(len(who))]
    ok(got[0] == "b2" and got[1] == "c1" and sorted(got) == sorted(who), f"served {got}")
    try:
        q.get(timeout=0.05)
        ok(False, "an empty queue times out")
    except queue.Empty:
        pass
    for _ in range(len(who)):
        q.task_done()
    q.join()
    ok(q.snapshot()["unfinished"] == 0, "all done")

    # ------------------------------------------------------------------ 3. config.json
    cfgp = os.environ["PDFUA_CONFIG"]
    with open(cfgp, "w") as fh:
        json.dump({"templates": {"table": {"1": {"name": "T"}}, "list": {}}, "privacy": {"guard": True, "allow": ["x.local"]},
                   "site_defaults": 99}, fh)
    with A.config_edit() as cfg:
        cfg["verapdf"] = "/opt/verapdf"
    disk = json.load(open(cfgp))
    ok("templates" in disk and "privacy" in disk and disk["verapdf"] == "/opt/verapdf", f"templates and privacy survive a save {sorted(disk)}")

    def add(i):
        with A.config_edit() as c_:
            c_["profiles"][f"P{i}"] = {"n": i}
    ts = [threading.Thread(target=add, args=(i,)) for i in range(40)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    disk = json.load(open(cfgp))
    ok(len(disk["profiles"]) == 40, f"40 changes at once, all kept ({len(disk['profiles'])})")
    ok(not [f for f in os.listdir(TMP) if f.endswith(".tmp")], "no temporary file left")

    # ------------------------------------------------------------------ 4. through the app
    names = [f"user{i}" for i in range(1, 9)]
    for n_ in names:
        A.ACCOUNTS.create(n_, "secret-" + n_, role="production")
    A.ACCOUNTS.create("boss", "secret-boss", role="admin")

    def client(user, pw):
        c_ = A.app.test_client()
        r_ = c_.post("/login", data={"username": user, "password": pw})
        ok(r_.status_code in (200, 302), f"{user} signs in {r_.status_code}")
        return c_

    admin = client("Kathir", "Kathir@051408")
    boss = client("boss", "secret-boss")
    users = {n_: client(n_, "secret-" + n_) for n_ in names}

    def upload(c_, fname="article.pdf", profile="Default"):
        with open(os.path.join(ROOT, "tests", "corpus", fname), "rb") as fh:
            r_ = c_.post("/upload", data={"profile": profile, "pdf": [(fh, fname)]},
                         content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
        return r_.json["jobs"][0]

    def wait(c_, jid, n=900):
        for _ in range(n):
            st = c_.get(f"/api/job/{jid}/status").json.get("stage")
            if st in ("review", "done", "error"):
                return st
            time.sleep(0.3)
        return "timeout"

    # ---- the queue: the workers sleep, four files of user1 and one each of three others
    A.TARGET_FILES = 0
    time.sleep(1.2)
    ok(isinstance(A.TASKS, FairQueue), "the app uses the fair queue")
    first = [upload(users["user1"]) for _ in range(4)]
    later = {n_: upload(users[n_]) for n_ in ("user2", "user3", "user4")}
    pos = {j: users["user1"].get(f"/api/job/{j}/status").json["queue"].get("position") if True else None for j in first}
    posl = {n_: users[n_].get(f"/api/job/{j}/status").json["queue"].get("position") for n_, j in later.items()}
    ok(posl == {"user2": 2, "user3": 3, "user4": 4}, f"others are not behind all four files: {posl} (user1: {list(pos.values())})")
    # an admin makes the last one urgent: it moves up
    r = admin.post("/api/admin/files/update", json={"ids": [later["user4"]], "priority": "urgent"})
    ok(r.json.get("ok"), f"priority set {r.json}")
    p4 = users["user4"].get(f"/api/job/{later['user4']}/status").json["queue"].get("position")
    ok(p4 == 1, f"an urgent file is first in line: {p4}")
    A.TARGET_FILES = 2
    for j in first + list(later.values()):
        ok(wait(admin, j) == "review", f"{j} analysed")
    ok(A.TASKS.qsize() == 0, "the queue is empty")

    # ---- a long MuPDF job does not freeze the reading and changing of tags
    job1 = first[0]
    m = users["user1"].get(f"/api/job/{job1}/model").json
    pid = [k for k, n in m["nodes"].items() if n["role"] == "P"]
    pg0 = users["user1"].get(f"/job/{job1}/page/0.png?z=1.5")
    ok(pg0.status_code == 200 and pg0.headers.get("ETag"), "a page picture is drawn once")
    etag = pg0.headers.get("ETag")
    hold = threading.Event()

    def hold_mupdf():
        with A.MUPDF_LOCK:
            hold.set()
            time.sleep(2.5)
    th = threading.Thread(target=hold_mupdf)
    th.start()
    hold.wait()
    t0 = time.time()
    quick = {}
    c1 = users["user1"]

    def timed(name, fn, want=(200,)):
        t_ = time.time()
        r_ = fn()
        quick[name] = time.time() - t_
        ok(r_.status_code in want, f"{name}: {r_.status_code} {r_.get_data(as_text=True)[:120]}")
        return r_

    timed("summary", lambda: c1.get(f"/api/job/{job1}/summary"))
    timed("lint", lambda: c1.get(f"/api/job/{job1}/lint"))
    timed("status", lambda: c1.get(f"/api/job/{job1}/status"))
    timed("brain", lambda: c1.get(f"/api/job/{job1}/brain"))
    timed("set role", lambda: c1.post(f"/api/job/{job1}/node/{pid[0]}", json={"role": "H2"}))
    timed("set role back", lambda: c1.post(f"/api/job/{job1}/node/{pid[0]}", json={"role": "P"}))
    timed("undo", lambda: c1.post(f"/api/job/{job1}/undo"))
    timed("presence", lambda: c1.get(f"/api/job/{job1}/presence?rev=&page=0"))
    timed("picture again", lambda: c1.get(f"/job/{job1}/page/0.png?z=1.5", headers={"If-None-Match": etag}), want=(304,))
    slow = {k: round(v, 2) for k, v in quick.items() if v > 1.2}
    ok(not slow, f"these waited for the MuPDF lock: {slow}")
    t_ = time.time()
    r = users["user2"].get(f"/job/{later['user2']}/page/1.png?z=2.25")        # not drawn yet: needs the lock
    waited = time.time() - t_
    ok(r.status_code == 200 and waited > 1.0, f"a picture that must be drawn waits its turn: {waited:.1f}s")
    th.join()

    # ---- the second person sees that the first changed the file
    shared = upload(admin)
    ok(wait(admin, shared) == "review", "shared file analysed")
    sm = admin.get(f"/api/job/{shared}/model").json
    rev0 = sm["rev"]
    ok(bool(rev0), "the model has a version")
    hb = boss.get(f"/api/job/{shared}/presence?rev={rev0}&page=2").json
    ok(hb["changed"] is False and hb["here"] == [], f"nothing changed yet, nobody else here: {hb}")
    sp = [k for k, n in sm["nodes"].items() if n["role"] == "P"]
    admin.post(f"/api/job/{shared}/node/{sp[0]}", json={"role": "H2"})
    hb = boss.get(f"/api/job/{shared}/presence?rev={rev0}&page=2").json
    ok(hb["changed"] is True and hb["by"] == "Kathir", f"the other person sees the change and who made it: {hb}")
    hb2 = admin.get(f"/api/job/{shared}/presence?rev={rev0}&page=0").json
    ok([h["user"] for h in hb2["here"]] == ["boss"] and hb2["here"][0]["page"] == 3, f"and who is here: {hb2['here']}")
    sm2 = boss.get(f"/api/job/{shared}/model").json
    hb = boss.get(f"/api/job/{shared}/presence?rev={sm2['rev']}&page=2").json
    ok(hb["changed"] is False, "after loading the change is shown no more")
    ok(A.editing_now().get(shared) == ["Kathir", "boss"], f"{A.editing_now()}")
    # the history belongs to the file: undoing a change somebody else just made asks first
    admin.post(f"/api/job/{shared}/node/{sp[0]}", json={"role": "P"})
    r = boss.post(f"/api/job/{shared}/undo")
    ok(r.status_code == 409 and r.json.get("confirm") and r.json.get("by") == "Kathir", f"undo of somebody else's change asks first: {r.status_code} {r.json}")
    ok(boss.get(f"/api/job/{shared}/model").json["nodes"][sp[0]]["role"] == "P", "and nothing was undone")
    r = boss.post(f"/api/job/{shared}/undo", json={"force": True})
    ok(r.status_code == 200 and r.json.get("ok"), f"the confirmed undo works: {r.status_code} {r.json}")
    r = boss.post(f"/api/job/{shared}/redo")
    ok(r.status_code == 200 and r.json.get("ok"), f"one's own undo is redone without asking: {r.status_code} {r.json}")
    # production cannot look into a file that is not theirs
    r = users["user5"].get(f"/api/job/{shared}/presence?rev=&page=0")
    ok(r.status_code == 403, f"not theirs: {r.status_code}")

    # ---- eight people work at once, two of them in the same file
    mine = {n_: j for n_, j in zip(names[:4], first[:1] + list(later.values()) + [None])}
    mine = {"user1": first[0], "user2": later["user2"], "user3": later["user3"], "user4": later["user4"]}
    for n_ in names[4:]:
        mine[n_] = upload(users[n_])
    for n_ in names[4:]:
        ok(wait(admin, mine[n_]) == "review", f"{n_}'s file analysed")
    nodes_of = {}
    pages_of = {}
    for n_, j in mine.items():
        mm = users[n_].get(f"/api/job/{j}/model").json
        nodes_of[j] = [k for k, n in mm["nodes"].items() if n["role"] == "P"][:12]
        pages_of[j] = mm["pages"]
    sp_a, sp_b = sp[1:7], sp[7:13]                       # the two admins change different tags of the shared file
    errors: list = []
    last: dict = {}
    lock = threading.Lock()
    stop = time.time() + 9.0

    def worker_user(n_, seed):
        rnd = random.Random(seed)
        c_ = users[n_]
        j = mine[n_]
        while time.time() < stop:
            for name, fn in (
                ("model", lambda: c_.get(f"/api/job/{j}/model")),
                ("lint", lambda: c_.get(f"/api/job/{j}/lint")),
                ("png", lambda: c_.get(f"/job/{j}/page/{rnd.randrange(pages_of[j])}.png?z=1.5")),
                ("role", lambda: c_.post(f"/api/job/{j}/node/{rnd.choice(nodes_of[j])}", json={"role": rnd.choice(["P", "H2"])})),
                ("summary", lambda: c_.get(f"/api/job/{j}/summary")),
                ("presence", lambda: c_.get(f"/api/job/{j}/presence?rev=1&page=0")),
            ):
                r_ = fn()
                if r_.status_code >= 400:
                    with lock:
                        errors.append((n_, name, r_.status_code, r_.get_data(as_text=True)[:150]))
                time.sleep(rnd.random() * 0.03)

    def worker_admin(c_, who_, ids, seed):
        rnd = random.Random(seed)
        while time.time() < stop:
            nid = rnd.choice(ids)
            role = rnd.choice(["P", "H2", "H3"])
            r_ = c_.post(f"/api/job/{shared}/node/{nid}", json={"role": role})
            with lock:
                if r_.status_code != 200 or r_.json.get("error"):
                    errors.append((who_, "role", r_.status_code, r_.get_data(as_text=True)[:150]))
                else:
                    last[nid] = role
            c_.get(f"/api/job/{shared}/presence?rev=1&page=0")
            c_.get(f"/job/{shared}/page/0.png?z=1.5")
            time.sleep(rnd.random() * 0.03)

    ts = [threading.Thread(target=worker_user, args=(n_, i)) for i, n_ in enumerate(names)]
    ts += [threading.Thread(target=worker_admin, args=(admin, "Kathir", sp_a, 101)),
           threading.Thread(target=worker_admin, args=(boss, "boss", sp_b, 102))]
    maxrun = [0]

    def watch():
        while time.time() < stop + 3:
            maxrun[0] = max(maxrun[0], A.LIVE_STATS["running"])
            time.sleep(0.05)
    tw = threading.Thread(target=watch)
    tw.start()
    [t.start() for t in ts]
    [t.join() for t in ts]
    ok(not errors, f"{len(errors)} request(s) failed: {errors[:4]}")
    final = admin.get(f"/api/job/{shared}/model").json["nodes"]
    lost = {nid: (role, final[nid]["role"]) for nid, role in last.items() if final[nid]["role"] != role}
    ok(last and not lost, f"no change of either admin is lost ({len(last)} tags): {dict(list(lost.items())[:3])}")
    from core.pipeline import load_model
    for n_, j in list(mine.items()) + [("shared", shared)]:
        try:
            load_model(os.path.join(A.JOBS, j)).check_integrity()
        except Exception as exc:
            ok(False, f"{n_}'s tags are not sound: {exc}")
    ok(not A.LOCK_NOTES, f"every request that opens a PDF took the lock: {A.LOCK_NOTES}")

    # the learner processes: coalesced, never more than PDFUA_LEARNERS at once, none failed
    for _ in range(120):
        if not A.LIVE_STATS["running"] and not A.LIVE_STATS["waiting"] and not A._LIVE:
            break
        time.sleep(0.5)
    tw.join()
    ls = dict(A.LIVE_STATS)
    ok(ls["runs"] >= 1 and ls["failed"] == 0, f"learner runs {ls}")
    ok(maxrun[0] <= 2, f"at most two learner processes at once: {maxrun[0]}")
    ok(ls["runs"] < 60, f"the edits were learned in batches, not one process for each ({ls['runs']})")

    # ---- twelve admins save twelve profiles at the same moment
    errs2: list = []

    def save_profile(i):
        c_ = admin if i % 2 else boss
        r_ = c_.post("/api/profiles", json={"name": f"Client {i}", "options": {"use_samples": False}})
        if r_.status_code != 200:
            errs2.append((i, r_.status_code, r_.get_data(as_text=True)[:100]))
    ts = [threading.Thread(target=save_profile, args=(i,)) for i in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    names_now = set(admin.get("/api/settings").json["profiles"])
    ok(not errs2 and all(f"Client {i}" in names_now for i in range(12)), f"twelve profiles kept: {errs2} {sorted(names_now)[:14]}")
    ok("templates" not in admin.get("/api/settings").json, "settings do not carry the templates")
    disk = json.load(open(cfgp))
    ok("templates" in disk and "privacy" in disk, "templates and privacy still in config.json")

    # ---- Admin → System shows how busy the server is
    st = admin.get("/api/admin/system").json
    ld = st.get("load") or {}
    ok(ld.get("mupdf", {}).get("requests", 0) > 20 and "queue" in ld and "editing" in ld, f"load statistics {sorted(ld)}")
    ok(ld["mupdf"]["max_hold_ms"] >= 2000, f"the long hold is counted: {ld['mupdf']}")

    if bad:
        print("multi-user test: FAILED")
        for b_ in bad:
            print("   ", b_)
        return 1
    print("multi-user test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
