"""V4.30 — how long people really work on a file: only the time they are active, never a break.

* core/worktime.py: a report cannot hold more seconds than have passed since the last one (a clock that runs wild counts
  nothing extra), the first report of a person is small, what is held is added to what is written, the file's meta gets
  the totals and worktime.json the days; Working while the file is in production, QC with QA / Final QA
* through the app: the editor's heartbeat (`/presence?...&w=seconds`) counts for the person who is working in the file
  (the holder of the lock) — not for a person who only looks, and not for anybody when the lock is off and they may not
  change the file; the answer tells the person their time; Admin → Files has Working and QC and who spent them;
  /api/job/<id>/time (an admin sees every person and day, others their own); /api/admin/time by user and day; the CSV;
  the idle limit (Admin → Workflow, 30 … 900 s)
"""
from __future__ import annotations

import csv
import io
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


def main() -> int:
    from core.worktime import WorkTimes, fmt, kind_of
    import app as A
    bad: list = []

    def ok(cond, msg):
        if not cond:
            bad.append(msg)

    # ------------------------------------------------------------------ 1. the store
    ok(kind_of("production") == "work" and kind_of("qa") == "qc" and kind_of("final_qa") == "qc" and kind_of("delivery") == "work",
       "Working in production, QC with QA / Final QA")
    ok(fmt(45) == "45 s" and fmt(300) == "5 min" and fmt(3900) == "1 h 05 min", f"how a duration reads: {fmt(45)} {fmt(300)} {fmt(3900)}")
    meta_store: dict = {}
    saved: list = []
    day_path = os.path.join(TMP, "w.json")
    W = WorkTimes(lambda j: dict(meta_store.get(j) or {}) or None, lambda j, t: (meta_store.__setitem__(j, t), saved.append(j)),
                  lambda j: day_path)
    t0 = 1000.0
    got = W.add("f", "Anna", "work", 6.0, now=t0)
    ok(got == 6.0, f"the first report of a person is counted: {got}")
    got = W.add("f", "Anna", "work", 6.0, now=t0 + 6)
    ok(got == 6.0, f"a report for the time that passed is counted whole: {got}")
    got = W.add("f", "Anna", "work", 6.0, now=t0 + 7)
    ok(got <= 2.5, f"…but not more than has passed (1 s later): {got}")
    got = W.add("f", "Anna", "work", 900.0, now=t0 + 500)
    ok(got <= 20.0, f"a huge report is cut: {got}")
    got = W.add("f", "Ben", "qc", 100.0, now=t0 + 600)
    ok(got <= 12.0, f"the first report of another person is small: {got}")
    tot = W.totals("f")
    ok(tot["work"] > 10 and tot["qc"] > 0, f"held seconds count: {tot}")
    W.flush("f")
    ok(saved == ["f"] and meta_store["f"]["work"] > 0 and "Anna" in meta_store["f"]["by"] and "Ben" in meta_store["f"]["by"],
       f"flush writes the totals into the meta: {meta_store}")
    ok(W.pending("f") == {"work": 0.0, "qc": 0.0}, "nothing held after a flush")
    days = W.days("f")
    ok(len(days) == 1 and "Anna" in list(days.values())[0], f"worktime.json has the day: {days}")
    before = W.totals("f", "Anna")["work"]
    W.add("f", "Anna", "work", 3.0, now=t0 + 603)
    ok(abs(W.totals("f", "Anna")["work"] - before - 3.0) < 1.5, "written + held = the total")

    # ------------------------------------------------------------------ 2. through the app
    A.ACCOUNTS.create("boss", "secret-boss", role="admin")
    A.ACCOUNTS.create("anna", "secret-anna", role="production")
    A.ACCOUNTS.create("raj", "secret-raj", role="qa")

    def client(u, p):
        c_ = A.app.test_client()
        c_.post("/login", data={"username": u, "password": p})
        return c_

    k, b = client("Kathir", "Kathir@051408"), client("boss", "secret-boss")
    with open(os.path.join(ROOT, "tests", "corpus", "article.pdf"), "rb") as fh:
        r = k.post("/upload", data={"profile": "Default", "pdf": [(fh, "article.pdf")]},
                   content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
    jid = r.json["jobs"][0]
    for _ in range(300):
        if k.get(f"/api/job/{jid}/status").json["stage"] == "review":
            break
        time.sleep(0.3)
    page = k.get(f"/job/{jid}").get_data(as_text=True)                     # opening the editor takes the file
    ok("window.WORK" in page and '"idle_limit":120' in page.replace(" ", ""), "the editor page carries the idle limit")
    ok('id="workChip"' in page, "and has the chip that shows the time")

    pr = k.get(f"/api/job/{jid}/presence?rev=&page=0&hold=1&active=1&w=5.0").json
    ok(pr["time"]["counting"] and pr["time"]["kind"] == "work" and 4.0 <= pr["time"]["mine_s"] <= 6.0,
       f"the holder's seconds count: {pr.get('time')}")
    pr2 = b.get(f"/api/job/{jid}/presence?rev=&page=0&w=5.0").json
    ok(not pr2["time"]["counting"] and pr2["time"]["mine_s"] == 0, f"a person who only looks is not counted: {pr2.get('time')}")
    time.sleep(1.2)
    k.get(f"/api/job/{jid}/presence?rev=&page=0&hold=1&active=1&w=1.0")
    k.get(f"/api/job/{jid}/presence?rev=&page=0&hold=1&active=1&w=999")
    t = k.get(f"/api/job/{jid}/time").json
    ok(t["work_s"] < 20, f"a report of 999 s one second after the last is cut: {t}")
    A.TIMES.flush(jid)
    rows = {r_["id"]: r_ for r_ in k.get("/api/admin/files").json}
    row = rows[jid]
    ok(row["work_s"] >= 5 and row["qc_s"] == 0 and row["time_by"] and row["time_by"][0]["user"] == "Kathir",
       f"Admin → Files has Working time and who spent it: {row.get('work_s')} {row.get('time_by')}")

    # QC: the same file with QA
    A.update_meta(jid, work="qa")
    time.sleep(1.1)
    k.get(f"/api/job/{jid}/presence?rev=&page=0&hold=1&active=1&w=1.0")
    p3 = k.get(f"/api/job/{jid}/presence?rev=&page=0&hold=1&active=1").json
    ok(p3["time"]["kind"] == "qc", f"with QA it is QC time: {p3.get('time')}")
    A.TIMES.flush(jid)
    t = k.get(f"/api/job/{jid}/time").json
    ok(t["qc_s"] >= 1 and t["work_s"] >= 5, f"Working and QC are kept apart: {t}")
    ok(t.get("users") and t["users"][0]["user"] == "Kathir" and t.get("days"), "an admin sees every person and every day")
    A.update_meta(jid, work="")

    # the lock off: whoever may change the file is counted; a person who may not, is not
    r = b.post("/api/admin/workflow", json={"file_lock": False})
    ok(r.status_code == 200, "the lock switched off")
    A._LOCK_CFG[0] = 0.0
    time.sleep(1.1)
    p4 = b.get(f"/api/job/{jid}/presence?rev=&page=0&w=1.0").json
    ok(p4["time"]["counting"], f"with the lock off an admin who works counts: {p4.get('time')}")
    r = b.post("/api/admin/workflow", json={"file_lock": True})
    A._LOCK_CFG[0] = 0.0

    # the idle limit
    r = b.post("/api/admin/workflow", json={"work_idle_s": 300})
    ok(r.status_code == 200 and r.json["cfg"]["work_idle_s"] == 300, f"the idle limit is set: {r.status_code} {r.get_data(as_text=True)[:100]}")
    ok(b.post("/api/admin/workflow", json={"work_idle_s": 5}).json["cfg"]["work_idle_s"] == 30, "…never below 30 s")
    ok(b.post("/api/admin/workflow", json={"work_idle_s": 99999}).json["cfg"]["work_idle_s"] == 900, "…nor above 900 s")
    ok(b.post("/api/admin/workflow", json={"work_idle_s": "soon"}).status_code == 400, "…and a number")
    b.post("/api/admin/workflow", json={"work_idle_s": 120})
    ok(k.get(f"/api/job/{jid}/presence?rev=&page=0").json["time"]["idle_limit"] == 120, "the editor is told the limit")

    # reports
    rep = b.get("/api/admin/time?days=7").json
    ok(rep["users"] and rep["users"][0]["user"] == "Kathir" and rep["per_day"] and rep["work_s"] >= 5,
       f"the report by user and day: {str(rep)[:200]}")
    ok(not b.get("/api/admin/time?days=7&user=nobody").json["users"], "…for one person")
    csv_rows = list(csv.reader(io.StringIO(b.get("/api/admin/export.csv").get_data(as_text=True).lstrip("﻿"))))
    head = csv_rows[0]
    ok("working time (min)" in head and "QC time (min)" in head and "time by" in head, f"the CSV has the times: {head[-4:]}")
    one = csv_rows[1]
    ok(float(one[head.index("working time (min)")]) > 0 and "Kathir" in one[head.index("time by")], f"…and the figures: {one[-3:]}")
    a_client = client("anna", "secret-anna")
    ok(a_client.get("/api/admin/time").status_code == 403, "only an admin reads the report")

    if bad:
        print("work time test: FAILED")
        for b_ in bad[:20]:
            print("   ", b_)
        return 1
    print("work time test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
