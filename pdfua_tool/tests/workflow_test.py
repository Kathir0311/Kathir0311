"""The production workflow (V4.23) through the app with Flask's test client.

* users are made as Production, QA and Final QA (and admin)
* QA and Final QA do not upload, analyse, re-analyse, auto-tag or change the
  profile — they correct and check; they download the input and output PDFs
* Production → QA needs the built PDF and the PAC report (a failed PAC report
  is refused when the rule says so, and is not kept)
* QA takes the next file from the queue (it becomes theirs; the other QA
  user no longer sees it); comments are added, edited (marked "edited", the
  old text kept), deleted; only who wrote one edits it
* an open comment stops the approval; rework stage-1 goes back to production,
  production marks the comments fixed and sends the file back — to the same
  QA user; QA's own change needs a build before the approval
* QA approval → Final QA; Final QA rework stage-2 → straight back to Final
  QA after the fix; Final QA approval → Delivery → delivered
* every step is in the file's history; the queue badge, the admin's board,
  people / checker numbers, the CSV and the zip with the PAC reports
* the workflow rules on their own (checklist, Final QA off, auto-assign,
  the order of the queue)
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp()
os.environ["PDFUA_JOBS"] = os.path.join(TMP, "jobs")
os.environ["PDFUA_CONFIG"] = os.path.join(TMP, "config.json")
os.environ["PDFUA_USERS"] = os.path.join(TMP, "users.json")
os.environ["PDFUA_SAMPLES"] = os.path.join(TMP, "samples")
os.environ["PDFUA_WORKERS"] = "1"

import pymupdf  # noqa: E402

ADMIN = (os.environ.get("PDFUA_TEST_USER", "Kathir"), os.environ.get("PDFUA_TEST_PASS", "Kathir@051408"))


def pdf_bytes(title: str) -> bytes:
    d = pymupdf.open()
    p = d.new_page()
    p.insert_text((72, 72), title, fontsize=18)
    p.insert_text((72, 100), "Some words on the page for the check.", fontsize=11)
    b = d.tobytes()
    d.close()
    return b


def unit(bad) -> None:
    """The rules of core/workflow.py on plain dicts."""
    from core import workflow as WF

    def ok(cond, msg):
        if not cond:
            bad.append("unit: " + msg)
    p = {"username": "pat", "role": "production"}
    q = {"username": "quinn", "role": "qa"}
    f = {"username": "fred", "role": "final_qa"}
    a = {"username": "boss", "role": "admin"}
    m = {"id": "x", "owner": "pat", "assigned_to": "pat", "work": "assigned"}
    ok(WF.can_edit(m, p) and not WF.can_edit(m, q) and not WF.can_see(m, q), "production file")
    ok(WF.can_analyse(p) and not WF.can_analyse(q) and not WF.can_analyse(f) and WF.can_analyse(a), "who analyses")
    ok(WF.norm_role("user") == "production", "the old role 'user' is production")
    cfg = {**WF.DEFAULTS, "require_pac": False, "require_build": False, "final_qa": False, "require_checklist": True}
    WF.submit(m, p, cfg, None)
    ok(m["work"] == "qa" and WF.todo_kind(m, q) == "pick", "to QA")
    try:
        WF.approve(m, q, cfg)
        ok(False, "the checklist stops the approval")
    except WF.WorkflowError:
        pass
    for k, _ in WF.CHECKLIST:
        WF.set_check(m, q, k, "na" if k == "contrast" else "ok")
    WF.approve(m, q, cfg)
    ok(m["work"] == "approved", f"Final QA off: QA approval → Delivery ({m['work']})")
    ok(WF.todo_kind(m, f) == "", "Final QA has nothing to deliver they did not check")
    # the queue: urgent first, then the due date, then who waited longest
    jobs = [{"id": "a", "work": "qa", "priority": "normal", "due": "", "created": "2026-01-01"},
            {"id": "b", "work": "qa", "priority": "urgent", "due": "", "created": "2026-01-03"},
            {"id": "c", "work": "qa", "priority": "urgent", "due": "2026-02-01", "created": "2026-01-04"},
            {"id": "d", "work": "qa_progress", "wf": {"qa_user": "quinn"}, "priority": "low"}]
    ok(WF.next_for(q, jobs)["id"] == "d", "what you are checking comes first")
    ok(WF.next_for(q, jobs[:3])["id"] == "c", f"urgent + due first: {WF.next_for(q, jobs[:3])['id']}")
    ok(WF.my_queue(q, jobs) == {"start": 0, "work": 0, "rework": 0, "pick": 3, "check": 1, "deliver": 0, "total": 4},
       f"the queue {WF.my_queue(q, jobs)}")
    load = [{"work": "qa_progress", "wf": {"qa_user": "quinn"}, "pages": 30}, {"work": "qa", "wf": {"qa_user": "rita"}, "pages": 2}]
    ok(WF.pick_checker("qa", ["quinn", "rita"], load) == "rita", "auto-assign: the checker with the fewest pages")
    # comments: edit keeps the old text and marks it edited; only the writer edits
    m2 = {"id": "y", "assigned_to": "pat", "work": "qa"}
    c = WF.add_comment(m2, q, "Table 2 header", page=3, category="Tables", severity="Major")
    e = WF.edit_comment(m2, q, c["id"], text="Table 2: header row missing")
    ok(e["edited"] and e["versions"][0]["text"] == "Table 2 header", "edit marks edited")
    try:
        WF.edit_comment(m2, p, c["id"], text="no")
        ok(False, "production edits a QA comment")
    except WF.WorkflowError as exc:
        ok(exc.status == 403, "403")
    WF.rework(m2, q)
    ok(m2["work"] == "rework" and m2["wf"]["rework"] == 1 and m2["wf"]["comments"][0]["sent_round"] == 1, "rework stage-1")
    try:
        WF.submit(m2, p, {**WF.DEFAULTS, "require_pac": False, "require_build": False}, None)
        ok(False, "an open comment stops the rework from going back")
    except WF.WorkflowError as exc:
        ok("not marked fixed" in str(exc), str(exc))
    # hold keeps the step
    WF.hold(m2, a, True)
    ok(not WF.can_edit(m2, p) and "hold" in WF.read_only_reason(m2, p), "on hold: read only")
    WF.hold(m2, a, False)
    ok(m2["work"] == "rework", "off hold: back where it was")
    ok(WF.time_in_stages(m2).get("production", -1) >= 0, "time in steps")


def main() -> int:
    import app as A
    bad: list = []

    def ok(cond, msg):
        if not cond:
            bad.append(msg)

    unit(bad)

    def wait(jid, stages=("review", "done"), key="stage", t=90):
        t0 = time.time()
        while time.time() - t0 < t:
            m = A.read_meta(jid)
            if m.get(key) in stages:
                return m
            time.sleep(0.3)
        return A.read_meta(jid)

    def wait_built(jid, t=120):
        t0 = time.time()
        while time.time() - t0 < t:
            m = A.read_meta(jid)
            if m.get("stage") in ("done", "review", "error") and m.get("built") and not m.get("dirty"):
                return m
            time.sleep(0.3)
        return A.read_meta(jid)

    admin = A.app.test_client()
    admin.post("/login", data={"username": ADMIN[0], "password": ADMIN[1]})
    for u, role in (("prod1", "production"), ("qa1", "qa"), ("qa2", "qa"), ("fqa1", "final_qa")):
        r = admin.post("/api/admin/users", json={"username": u, "password": "secret12", "role": role})
        ok(r.status_code == 200 and r.json["user"]["role"] == role, f"create {u} as {role}: {r.json}")
    ok(admin.post("/api/admin/users", json={"username": "zz", "password": "secret12", "role": "boss"}).status_code == 400,
       "an unknown role")
    r = admin.post("/api/admin/users", json={"username": "plain", "password": "secret12"})
    ok(r.json["user"]["role"] == "production", "a new user is production unless said")

    def login(name):
        c = A.app.test_client()
        c.post("/login", data={"username": name, "password": "secret12"})
        return c
    P, Q, Q2, F = login("prod1"), login("qa1"), login("qa2"), login("fqa1")

    # QA does not upload
    r = Q.post("/upload", data={"profile": "Default", "pdf": [(io.BytesIO(pdf_bytes("Q")), "q.pdf")]},
               content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
    ok(r.status_code == 403, f"QA cannot upload ({r.status_code})")
    home = Q.get("/").data.decode()
    ok("Upload and analyse" not in home and "Take the next file" in home, "QA's home: the queue, no upload")
    ok("Upload and analyse" in P.get("/").data.decode(), "production's home has the upload")

    # production uploads and analyses
    r = P.post("/upload", data={"profile": "Default", "pdf": [(io.BytesIO(pdf_bytes("Annual report")), "report.pdf")]},
               content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
    jid = r.json["jobs"][0]
    m = wait(jid)
    ok(m.get("stage") in ("review", "done"), f"analysed: {m.get('stage')} {m.get('error')}")
    ok(Q.get(f"/job/{jid}").status_code == 403, "QA does not see a file in production")
    v = P.get(f"/api/job/{jid}/wf").json
    ok(v["stage"] == "production" and v["can"]["submit"] and any("build" in x for x in v["missing"]), f"wf view {v.get('missing')}")
    r = P.post(f"/api/job/{jid}/wf/submit", data={"note": "x"}, content_type="multipart/form-data")
    ok(r.status_code == 400 and "build" in r.json["error"], f"not built: {r.json}")
    P.post(f"/api/job/{jid}/build", json={})
    m = wait_built(jid)
    ok(m.get("built"), "built")
    ok(m.get("work") == "in_progress", f"production's first change starts the work ({m.get('work')})")
    r = P.post(f"/api/job/{jid}/wf/submit", data={"note": "x"}, content_type="multipart/form-data")
    ok(r.status_code == 400 and "PAC" in r.json["error"], f"no PAC report: {r.json}")
    r = P.post(f"/api/job/{jid}/wf/submit", data={"pac": (io.BytesIO(b"MZ"), "pac.exe")}, content_type="multipart/form-data")
    ok(r.status_code == 400, "a PAC report must be a report")
    admin.post("/api/admin/workflow", json={"pac_must_pass": True})
    r = P.post(f"/api/job/{jid}/wf/submit", data={"pac": (io.BytesIO(b"%PDF-1.4 failures"), "pac_report.pdf"), "result": "fail", "errors": "3"},
               content_type="multipart/form-data")
    ok(r.status_code == 400 and "failures" in r.json["error"], f"a failed PAC report is refused: {r.json}")
    ok(not os.listdir(os.path.join(A.job_dir(jid), "pac")), "the refused report is not kept")
    r = P.post(f"/api/job/{jid}/wf/submit", data={"pac": (io.BytesIO(b"%PDF-1.4 passed"), "PAC report.pdf"), "result": "pass", "errors": "0",
                                                  "note": "all tagged"}, content_type="multipart/form-data")
    ok(r.status_code == 200 and r.json["work"] == "qa" and len(r.json["pac"]) == 1, f"to QA: {r.json.get('error')}")
    ok(P.post(f"/api/job/{jid}/doc", json={"title": "x"}).status_code == 403, "production reads only while QA has it")
    ok("QA" in P.get(f"/api/job/{jid}/wf").json["readonly"], "the read-only reason")

    # QA takes it
    q2 = {j["id"]: j for j in Q2.get("/api/jobs").json}
    ok(q2.get(jid, {}).get("todo") == "pick", "in every QA user's queue")
    ok(Q.get("/api/wf/queue").json["pick"] == 1, "the badge")
    r = Q.post("/api/wf/next", json={})
    ok(r.json.get("id") == jid, f"take the next file {r.json}")
    m = A.read_meta(jid)
    ok(m["work"] == "qa_progress" and m["wf"]["qa_user"] == "qa1", "it is qa1's now")
    ok(Q2.get(f"/job/{jid}").status_code == 403, "the other QA user no longer sees it")
    ok(Q.get(f"/job/{jid}/pac/0").status_code == 200, "QA downloads the PAC report")
    ok(Q.get(f"/job/{jid}/download/original").status_code == 200, "QA downloads the input PDF")
    ok(Q.get(f"/job/{jid}/download/pdf").status_code == 200, "QA downloads the output PDF")
    for path, body in ((f"/job/{jid}/reanalyze", {}), (f"/api/job/{jid}/retag", {"pages": "1"}), (f"/api/job/{jid}/profile", {"profile": "Default"})):
        ok(Q.post(path, json=body).status_code == 403, f"QA cannot {path.rsplit('/', 1)[-1]}")
    ok(Q.post(f"/api/job/{jid}/doc", json={"title": "Annual report 2026"}).status_code == 200, "QA corrects")
    ed = Q.get(f"/job/{jid}").data.decode()
    ok("tab-review" in ed and "review.js" in ed and " readonly" not in ed.split("<body", 1)[1][:200], "QA's editor: Review tab, not read only")
    r = Q.post(f"/api/job/{jid}/wf/comment", json={"op": "add", "text": "Heading level", "page": 1, "category": "Headings", "severity": "Major"})
    c1 = r.json["comment"]["id"]
    r = Q.post(f"/api/job/{jid}/wf/comment", json={"op": "add", "text": "typo", "category": "Other"})
    c2 = r.json["comment"]["id"]
    r = Q.post(f"/api/job/{jid}/wf/comment", json={"op": "edit", "id": c1, "text": "H2 should be H1"})
    ok(r.json["comment"]["edited"] and r.json["comment"]["versions"][0]["text"] == "Heading level", "edited, the old text kept")
    ok(P.post(f"/api/job/{jid}/wf/comment", json={"op": "edit", "id": c1, "text": "no"}).status_code == 403, "only who wrote it edits")
    ok(P.post(f"/api/job/{jid}/wf/comment", json={"op": "delete", "id": c1}).status_code == 403, "only who wrote it deletes")
    r = Q.post(f"/api/job/{jid}/wf/comment", json={"op": "delete", "id": c2})
    ok(len(r.json["comments"]) == 1, "deleted")
    r = Q.post(f"/api/job/{jid}/wf", json={"action": "approve"})
    ok(r.status_code == 400 and "open" in r.json["error"], f"an open comment stops the approval: {r.json}")
    r = Q.post(f"/api/job/{jid}/wf", json={"action": "rework", "note": "see the comment"})
    ok(r.json["work"] == "rework" and r.json["rework"] == 1, f"rework stage-1 {r.json.get('error')}")
    ok(Q.post(f"/api/job/{jid}/doc", json={"title": "y"}).status_code == 403, "QA reads only during the rework")

    # production fixes
    v = P.get(f"/api/job/{jid}/wf").json
    ok(v["can"]["fix"] and v["can"]["submit"] and not v["readonly"], "production has it again")
    r = P.post(f"/api/job/{jid}/wf/submit", data={"pac": (io.BytesIO(b"%PDF"), "pac2.pdf"), "result": "pass"}, content_type="multipart/form-data")
    ok(r.status_code == 400 and ("fixed" in r.json["error"] or "build" in r.json["error"]), f"comments first: {r.json}")
    r = P.post(f"/api/job/{jid}/wf/comment", json={"op": "status", "id": c1, "status": "fixed", "reply": "made it H1"})
    ok(r.json["comment"]["status"] == "fixed" and r.json["comment"]["replies"][0]["text"] == "made it H1", "marked fixed")
    P.post(f"/api/job/{jid}/build", json={})
    wait_built(jid)
    r = P.post(f"/api/job/{jid}/wf/submit", data={"pac": (io.BytesIO(b"%PDF"), "pac2.pdf"), "result": "pass"}, content_type="multipart/form-data")
    ok(r.status_code == 200 and r.json["work"] == "qa" and r.json["qa_user"] == "qa1", f"back to the same QA user {r.json.get('error')}")
    ok(Q.get("/api/wf/queue").json["check"] == 1, "qa1's badge")
    ok(Q.post(f"/api/job/{jid}/doc", json={"title": "Annual report 2026."}).status_code == 200, "QA's own change")
    r = Q.post(f"/api/job/{jid}/wf", json={"action": "approve"})
    ok(r.status_code == 400 and "build" in r.json["error"], f"QA's change needs a build first: {r.json}")
    Q.post(f"/api/job/{jid}/build", json={})
    wait_built(jid)
    r = Q.post(f"/api/job/{jid}/wf", json={"action": "approve", "note": "good"})
    ok(r.status_code == 200 and r.json["work"] == "final_qa", f"QA approved → Final QA {r.json.get('error')}")
    ok(r.json["comments"][0]["status"] == "closed", "the fixed comment is closed")
    ok(Q.post(f"/api/job/{jid}/doc", json={"title": "z"}).status_code == 403, "QA reads only now")

    # Final QA
    ok(F.get("/api/jobs").json[0]["todo"] == "pick", "Final QA's queue")
    r = F.post(f"/api/job/{jid}/wf", json={"action": "start"})
    ok(r.json["work"] == "fqa_progress" and r.json["fqa_user"] == "fqa1", "Final QA starts")
    F.post(f"/api/job/{jid}/wf/comment", json={"op": "add", "text": "alt text of the logo", "category": "Alt text", "severity": "Critical"})
    r = F.post(f"/api/job/{jid}/wf", json={"action": "rework"})
    ok(r.json["rework"] == 2 and r.json["rework_from"] == "final_qa", "rework stage-2 from Final QA")
    cid = [c for c in r.json["comments"] if c["status"] == "open"][0]["id"]
    P.post(f"/api/job/{jid}/wf/comment", json={"op": "status", "id": cid, "status": "fixed"})
    r = P.post(f"/api/job/{jid}/wf/submit", data={"pac": (io.BytesIO(b"%PDF"), "pac3.pdf"), "result": "pass"}, content_type="multipart/form-data")
    ok(r.status_code == 200 and r.json["work"] == "final_qa", f"straight back to Final QA {r.json.get('error')}")
    r = F.post(f"/api/job/{jid}/wf", json={"action": "approve"})
    ok(r.json["work"] == "approved" and r.json["can"]["deliver"], f"Final QA approved → Delivery {r.json.get('error')}")
    r = F.post(f"/api/job/{jid}/wf", json={"action": "deliver"})
    ok(r.json["work"] == "delivered", "delivered")
    hist = [e["action"] for e in r.json["history"]]
    for a in ("sent to QA", "QA started", "rework stage-1", "QA approved", "Final QA started", "rework stage-2",
              "sent to Final QA", "Final QA approved", "delivered"):
        ok(a in hist, f"history has {a!r}: {hist}")
    ok(len(r.json["pac"]) == 3, "three PAC reports, one per round")

    # the admin's view
    w = admin.get("/api/admin/workflow").json
    ok(w["stages"].get("delivered") == 1 and w["rework_files"] == 1 and w["first_pass_rate"] == 0, f"board {w['stages']}")
    ck = {c["user"]: c for c in w["checkers"]}
    ok(ck.get("qa1", {}).get("approved") == 1 and ck["qa1"]["rework"] == 1 and ck.get("fqa1", {}).get("rework") == 1, f"checkers {ck}")
    ok(w["categories"].get("Headings") == 1 and w["categories"].get("Alt text") == 1, f"categories {w['categories']}")
    csv = admin.get("/api/admin/export.csv").data.decode("utf-8-sig")
    ok("rework rounds" in csv and ",2,qa1,fqa1," in csv, "the CSV has the workflow")
    z = admin.get(f"/api/admin/files/download.zip?reports=1&original=1&ids={jid}")
    names = zipfile.ZipFile(io.BytesIO(z.data)).namelist()
    ok(any(n.endswith("_PAC.pdf") for n in names) and any("_PAC_round1" in n for n in names) and any(n.endswith("_input.pdf") for n in names),
       f"the zip {names}")
    # admin moves, holds, sends to rework in bulk
    admin.post("/api/admin/files/update", json={"ids": [jid], "work": "rework", "note": "client asked a change"})
    m = A.read_meta(jid)
    ok(m["work"] == "rework" and m["wf"]["rework"] == 3, f"admin rework from delivered {m['work']} {m['wf']['rework']}")
    admin.post("/api/admin/files/update", json={"ids": [jid], "work": "on_hold"})
    ok(A.read_meta(jid)["work"] == "on_hold" and P.post(f"/api/job/{jid}/doc", json={"title": "h"}).status_code == 403, "on hold")
    admin.post(f"/api/job/{jid}/wf", json={"action": "unhold"})
    ok(A.read_meta(jid)["work"] == "rework", "off hold")
    log = {e["action"] for e in admin.get("/api/admin/log?limit=300").json}
    for a in ("sent to QA", "QA approved", "Final QA approved", "delivered", "comment add", "comment edit", "comment fixed"):
        ok(a in log, f"the activity log has {a!r}")
    # the rules page
    r = admin.post("/api/admin/workflow", json={"auto_assign": True})
    ok(r.json["cfg"]["auto_assign"] and "auto_assign" in r.json["setting_labels"], "a rule saved")
    ok(Q.post("/api/admin/workflow", json={"final_qa": False}).status_code == 403, "QA cannot change the rules")
    ok("Workflow" in admin.get("/admin").data.decode(), "the admin page has the Workflow tab")

    if bad:
        print("workflow test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("workflow test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
