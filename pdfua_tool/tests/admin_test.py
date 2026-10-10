"""Users, roles and allocation (V4.19), through the app with Flask's test client.

* nothing without signing in; a wrong password is refused
* the admin (Kathir) creates users; uploads a folder (a user may not)
* the admin's files wait in the pool, are given to a user, moved to another
  (reallocate), put back (revert), taken back (unassign); shared out by pages
* a user sees only their own files and the files given to them; another
  user's file, the admin page and the settings are closed to them
* a user starts a file and sends it for checking (QA, V4.23); the admin sends
  it to rework and approves it (QA, then Final QA); notes; the work log has it all
* deleting a user gives their files to another user
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp()
os.environ["PDFUA_JOBS"] = os.path.join(TMP, "jobs")
os.environ["PDFUA_CONFIG"] = os.path.join(TMP, "config.json")
os.environ["PDFUA_USERS"] = os.path.join(TMP, "users.json")
os.environ["PDFUA_SAMPLES"] = os.path.join(TMP, "samples")     # V4.25: an approval learns — never into the real database
os.environ["PDFUA_WORKERS"] = "1"

import pymupdf  # noqa: E402

ADMIN = (os.environ.get("PDFUA_TEST_USER", "Kathir"), os.environ.get("PDFUA_TEST_PASS", "Kathir@051408"))


def pdf_bytes(title: str) -> bytes:
    d = pymupdf.open()
    p = d.new_page()
    p.insert_text((72, 72), title, fontsize=18)
    p.insert_text((72, 100), "Some words on the page.", fontsize=11)
    b = d.tobytes()
    d.close()
    return b


def main() -> int:
    import app as A
    bad: list = []

    def ok(cond, msg):
        if not cond:
            bad.append(msg)

    c = A.app.test_client()
    ok(c.get("/").status_code == 302, "the files page without signing in")
    ok(c.get("/api/jobs").status_code == 401, "the API without signing in")
    r = c.post("/login", data={"username": ADMIN[0], "password": "wrong"})
    ok(b"do not match" in r.data, "a wrong password")
    r = c.post("/login", data={"username": ADMIN[0], "password": ADMIN[1], "next": "/admin"})
    ok(r.status_code == 302 and r.headers["Location"].endswith("/admin"), f"admin sign-in {r.status_code}")
    ok(c.get("/admin").status_code == 200, "the admin page")
    for u in ("ravi", "meena"):
        r = c.post("/api/admin/users", json={"username": u, "password": "secret12", "name": u.title()})
        ok(r.status_code == 200 and r.json.get("ok"), f"create {u}: {r.json}")
    ok(c.post("/api/admin/users", json={"username": "ravi", "password": "secret12"}).status_code == 400, "a user twice")

    # a folder uploaded by the admin: two files, in the pool
    data = {"profile": "Default", "folder": "Batch A", "paths_json": json.dumps(["Batch A/a.pdf", "Batch A/sub/b.pdf"]),
            "pdf": [(io.BytesIO(pdf_bytes("A")), "a.pdf"), (io.BytesIO(pdf_bytes("B")), "b.pdf")]}
    r = c.post("/upload", data=data, content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
    ok(r.status_code == 200 and len(r.json.get("jobs", [])) == 2, f"folder upload {r.status_code} {r.json}")
    ja, jb = r.json["jobs"]
    m = A.read_meta(ja)
    ok(m["owner"] == ADMIN[0] and m["assigned_to"] == "" and m["batch_name"] == "Batch A", f"pool {m.get('owner')} {m.get('assigned_to')}")
    ok(A.read_meta(jb)["rel_path"] == "Batch A/sub/b.pdf", "the path in the folder")

    # give, reallocate, revert, unassign
    r = c.post("/api/admin/files/assign", json={"ids": [ja, jb], "to": "ravi"})
    ok(r.json.get("ok") and A.read_meta(ja)["assigned_to"] == "ravi" and A.read_meta(ja)["work"] == "assigned", "allocate")
    c.post("/api/admin/files/assign", json={"ids": [jb], "to": "meena"})
    ok(A.read_meta(jb)["assigned_to"] == "meena", "reallocate")
    c.post("/api/admin/files/revert", json={"ids": [jb]})
    ok(A.read_meta(jb)["assigned_to"] == "ravi", f"revert {A.read_meta(jb)['assigned_to']}")
    c.post("/api/admin/files/unassign", json={"ids": [jb]})
    ok(A.read_meta(jb)["assigned_to"] == "", "unassign")
    r = c.post("/api/admin/files/assign", json={"ids": [jb], "users": ["ravi", "meena"], "mode": "balance"})
    ok(r.json.get("plan", {}).get(jb) == "meena", f"balance gives the file to who has least {r.json}")
    c.post("/api/admin/files/update", json={"ids": [ja], "priority": "urgent", "due": "2030-01-31"})
    ok(A.read_meta(ja)["priority"] == "urgent" and A.read_meta(ja)["due"] == "2030-01-31", "priority and due")

    # a user
    u = A.app.test_client()
    u.post("/login", data={"username": "ravi", "password": "secret12"})
    ids = {j["id"] for j in u.get("/api/jobs").json}
    ok(ids == {ja}, f"ravi sees {ids}")
    ok(u.get(f"/api/job/{jb}/info").status_code == 403, "another user's file is closed")
    ok(u.get(f"/job/{jb}").status_code == 403, "another user's editor is closed")
    ok(u.get("/admin").status_code == 403, "the admin page is closed to a user")
    ok(u.post("/api/settings", json={}).status_code == 403, "settings are closed to a user")
    r = u.post("/upload", data={"profile": "Default", "folder": "X", "pdf": [(io.BytesIO(pdf_bytes("X")), "x.pdf")]},
               content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
    ok(r.status_code == 403, "a user cannot upload a folder")
    r = u.post("/upload", data={"profile": "Default", "pdf": [(io.BytesIO(pdf_bytes("C")), "c.pdf")]},
               content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
    jc = r.json["jobs"][0]
    ok(A.read_meta(jc)["owner"] == "ravi" and A.read_meta(jc)["assigned_to"] == "ravi", "a user's own upload")
    ok({j["id"] for j in u.get("/api/jobs").json} == {ja, jc}, "ravi sees his upload too")
    r = u.post(f"/api/job/{ja}/work", json={"action": "start"})
    ok(r.json.get("work") == "in_progress", "start")
    r = u.post(f"/api/job/{ja}/work", json={"action": "submit", "text": "all tables done"})
    ok(r.status_code == 400, "QA needs the built PDF and the PAC report (V4.23)")
    c.post("/api/admin/workflow", json={"require_pac": False, "require_build": False})
    r = u.post(f"/api/job/{ja}/work", json={"action": "submit", "text": "all tables done"})
    ok(r.json.get("work") == "qa", f"send for checking {r.json}")
    ok(u.post(f"/api/job/{ja}/work", json={"action": "approve"}).status_code == 403, "a user cannot approve")
    ok(u.post(f"/job/{ja}/delete").status_code == 403, "a user cannot delete a file given to them")
    ok(u.post("/api/me", json={"old": "secret12", "new": "secret34"}).json.get("ok"), "change own password")

    # the admin checks it
    r = c.post(f"/api/job/{ja}/work", json={"action": "return", "text": "table 2 header is wrong"})
    ok(r.json.get("work") == "rework", "send back (rework)")
    r = u.post(f"/api/job/{ja}/work", json={"action": "submit", "text": "fixed"})
    ok(r.json.get("work") == "qa", "back to QA")
    r = c.post(f"/api/job/{ja}/work", json={"action": "approve"})
    ok(r.json.get("work") == "final_qa", "QA approved → Final QA")
    r = c.post(f"/api/job/{ja}/work", json={"action": "approve"})
    ok(r.json.get("work") == "approved" and len(r.json.get("notes", [])) >= 2, "approve")
    ov = c.get("/api/admin/overview").json
    rv = next(x for x in ov["users"] if x["username"] == "ravi")
    ok(rv["files"] == 2 and rv["done"] == 1, f"overview {rv['files']} {rv['done']}")
    log = c.get("/api/admin/log?limit=100").json
    acts = {e["action"] for e in log}
    for a in ("allocated", "reallocated", "revert", "unassigned", "sent to QA", "approved", "sent to rework", "created user"):
        ok(a in acts, f"the log has no {a!r}: {sorted(acts)}")
    ok(c.get("/api/admin/export.csv").status_code == 200, "export")

    # switching a user off, deleting one
    c.post("/api/admin/users/meena", json={"active": False})
    m2 = A.app.test_client()
    r = m2.post("/login", data={"username": "meena", "password": "secret12"})
    ok(b"do not match" in r.data, "a switched-off user cannot sign in")
    r = c.delete("/api/admin/users/ravi", json={"give_to": "Kathir"})
    ok(r.json.get("ok") and A.read_meta(ja)["assigned_to"] == "Kathir", f"delete a user {r.json}")
    ok(c.delete("/api/admin/users/Kathir", json={}).status_code == 400, "the last admin stays")
    ok(u.get("/api/jobs").status_code == 401, "a deleted user is signed out")
    c.get("/logout")
    ok(c.get("/api/jobs").status_code == 401, "signed out")
    # Basic auth for scripts
    import base64
    h = {"Authorization": "Basic " + base64.b64encode(f"{ADMIN[0]}:{ADMIN[1]}".encode()).decode()}
    ok(A.app.test_client().get("/api/jobs", headers=h).status_code == 200, "basic auth")

    if bad:
        print("admin test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("admin test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
