"""V4.30 — rights: what an admin gives to a person or takes away, on top of the role.

* core/rights.py: the default of each role, the person's own Allow / Deny wins, an admin always may, null removes it
* Admin → Users: the rights are set with the user, listed with the user (and what is in force), logged; a wrong
  key is refused; only an admin sets them
* the server refuses what the person has no right to — upload, delete, build, download, the AI helpers — with the reason;
  see_all lets a person look at every file (and only look); take_over lets a person take a file from whoever has it;
  view_times lets a person see who spent the working time
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


def main() -> int:
    from core import rights as R
    import app as A
    bad: list = []

    def ok(cond, msg):
        if not cond:
            bad.append(msg)

    # ------------------------------------------------------------------ 1. the rules
    prod, qa, adm = {"role": "production"}, {"role": "qa"}, {"role": "admin", "rights": {"upload": False}}
    ok(R.allowed(prod, "upload") and R.allowed(prod, "delete_files") and not R.allowed(prod, "see_all"), "production: the defaults")
    ok(not R.allowed(qa, "upload") and R.allowed(qa, "build") and R.allowed(qa, "download"), "QA: the defaults")
    ok(R.allowed(adm, "upload") and R.allowed(adm, "see_all"), "an admin always may — even with a setting against it")
    ok(R.allowed({"role": "user"}, "upload"), "the old role name 'user' is production")
    ok(not R.allowed({"role": "production", "rights": {"upload": False}}, "upload"), "Deny wins over the role's default")
    ok(R.allowed({"role": "qa", "rights": {"see_all": True}}, "see_all"), "Allow wins over the role's default")
    ok(not R.allowed(None, "upload") and not R.allowed(prod, "nonsense"), "nobody / no such right: no")
    ok(R.clean({"build": False}, {"build": None, "upload": "deny", "see_all": True}) == {"upload": False, "see_all": True},
       "clean: null removes, deny / allow set")
    try:
        R.clean({}, {"nonsense": True})
        bad.append("an unknown right was accepted")
    except ValueError:
        pass
    ok(len(R.catalog()) == len(R.RIGHTS) and all("default" in c for c in R.catalog()), "the catalog")

    # ------------------------------------------------------------------ 2. Admin → Users
    for name, role in (("anna", "production"), ("tom", "production"), ("raj", "qa"), ("lead", "qa"), ("boss", "admin")):
        A.ACCOUNTS.create(name, f"secret-{name}", role=role)

    def client(u):
        c_ = A.app.test_client()
        c_.post("/login", data={"username": u, "password": f"secret-{u}" if u != "Kathir" else "Kathir@051408"})
        return c_

    k, boss = client("Kathir"), client("boss")
    anna, tom, raj, lead = client("anna"), client("tom"), client("raj"), client("lead")
    cat = boss.get("/api/admin/rights").json
    ok({r["key"] for r in cat["rights"]} == set(R.BY_KEY), "Admin lists the rights")
    ok(anna.get("/api/admin/rights").status_code == 403, "only an admin sees / sets them")
    r = boss.post("/api/admin/users/tom", json={"rights": {"upload": False, "delete_files": False, "build": False,
                                                          "download": False, "use_ai": False}})
    ok(r.status_code == 200 and r.json["user"]["rights"]["build"] is False and r.json["user"]["effective"]["build"] is False,
       f"set for a user: {r.status_code} {str(r.json)[:160]}")
    ok(boss.post("/api/admin/users/tom", json={"rights": {"nonsense": True}}).status_code == 400, "a wrong key is refused")
    r = boss.post("/api/admin/users/lead", json={"rights": {"see_all": True, "view_times": True, "take_over": True}})
    ok(r.json["user"]["effective"]["see_all"], "lead may look at every file")
    users = {u["username"]: u for u in boss.get("/api/admin/users").json}
    ok(users["tom"]["rights"]["upload"] is False and users["anna"]["rights"] == {} and users["lead"]["effective"]["take_over"],
       "the list carries what was set and what is in force")
    ok(any("rights:" in e.get("detail", "") for e in A.ACCOUNTS.recent(20)), "and it is logged")
    ok(anna.get("/api/me").json["effective"]["upload"], "a person sees their own rights")

    # ------------------------------------------------------------------ 3. the server refuses
    with open(os.path.join(ROOT, "tests", "corpus", "article.pdf"), "rb") as fh:
        r = tom.post("/upload", data={"profile": "Default", "pdf": [(fh, "article.pdf")]},
                     content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
    ok(r.status_code == 403 and r.json.get("right") == "upload" and "not been given the right" in r.json["error"],
       f"upload without the right: {r.status_code} {r.get_data(as_text=True)[:140]}")
    with open(os.path.join(ROOT, "tests", "corpus", "article.pdf"), "rb") as fh:
        r = anna.post("/upload", data={"profile": "Default", "pdf": [(fh, "article.pdf")]},
                      content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
    ok(r.status_code == 200, f"upload with the role's default: {r.status_code}")
    jid = r.json["jobs"][0]
    for _ in range(300):
        if k.get(f"/api/job/{jid}/status").json["stage"] == "review":
            break
        time.sleep(0.3)
    # give the file to tom as well as anna's: tom is the one assigned
    A.update_meta(jid, assigned_to="tom", owner="anna", work="assigned")
    time.sleep(0.2)
    r = tom.post(f"/api/job/{jid}/build", json={})
    ok(r.status_code == 403 and r.json.get("right") == "build", f"build without the right: {r.status_code} {r.get_data(as_text=True)[:100]}")
    r = tom.get(f"/job/{jid}/download/original")
    ok(r.status_code == 403, f"download without the right: {r.status_code}")
    r = tom.post(f"/api/job/{jid}/ai/headings", json={})
    ok(r.status_code == 403 and r.json.get("right") == "use_ai", f"AI without the right: {r.status_code} {r.get_data(as_text=True)[:100]}")
    r = tom.post(f"/job/{jid}/delete", headers={"X-Requested-With": "fetch"})
    ok(r.status_code == 403, f"delete without the right: {r.status_code}")
    page = tom.get("/").get_data(as_text=True)
    ok("noright-build" in page and "noright-download" in page and "noright-ai" in page, "the page does not offer them")
    anna_page = anna.get("/").get_data(as_text=True)
    ok("noright-build" not in anna_page, "…to the person who has them")
    r = anna.get(f"/job/{jid}/download/original")
    ok(r.status_code == 200, f"download with the role's default: {r.status_code}")
    # a QA person: the old message stays
    r = raj.post("/upload", data={"profile": "Default"}, content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
    ok(r.status_code == 403 and "do not upload or analyse" in r.json["error"], f"QA: {r.status_code} {r.get_data(as_text=True)[:120]}")
    # the person is given the rights back
    boss.post("/api/admin/users/tom", json={"rights": {"build": None, "download": "allow"}})
    ok(tom.get(f"/job/{jid}/download/original").status_code == 200, "a right given back (download)")
    ok(tom.post(f"/api/job/{jid}/build", json={}).status_code != 403, "…and the default again (build)")

    # ------------------------------------------------------------------ 4. see_all, take_over, view_times
    ids_raj = {m["id"] for m in raj.get("/api/jobs").json}
    ids_lead = {m["id"] for m in lead.get("/api/jobs").json}
    ok(jid not in ids_raj and jid in ids_lead, f"lead sees the file QA does not: {jid in ids_lead}/{jid in ids_raj}")
    ok(lead.get(f"/api/job/{jid}/model").status_code == 200, "…and can read its tags")
    paras = [i for i, n in k.get(f"/api/job/{jid}/model").json["nodes"].items() if n["role"] == "P"]
    r = lead.post(f"/api/job/{jid}/node/{paras[0]}", json={"role": "H2"})
    ok(r.status_code == 403 and r.json.get("readonly"), f"…but not change it: {r.status_code} {r.get_data(as_text=True)[:100]}")
    ok(raj.get(f"/api/job/{jid}/model").status_code == 403, "a QA person without the right does not")
    # take_over: tom holds the file, lead takes it
    tom.get(f"/job/{jid}")
    ok(A.EDITLOCK.holder(jid) and A.EDITLOCK.holder(jid)["user"] == "tom", "tom has the file")
    r = anna.post(f"/api/job/{jid}/lock", json={"action": "takeover"})
    ok(r.status_code == 403, f"a person without the right cannot take a file over: {r.status_code}")
    r = lead.post(f"/api/job/{jid}/lock", json={"action": "takeover"})
    ok(r.status_code == 200 and r.json.get("mine"), f"a person with the right can: {r.status_code} {r.get_data(as_text=True)[:120]}")
    # view_times
    A.TIMES.add(jid, "tom", "work", 8.0)
    A.TIMES.flush(jid)
    t_lead = lead.get(f"/api/job/{jid}/time").json
    t_anna = anna.get(f"/api/job/{jid}/time").json
    ok(t_lead.get("users") and not t_anna.get("users") and t_anna["work_s"] >= 8, f"who spent the time: {t_lead.get('users')} / {t_anna.get('users')}")

    if bad:
        print("rights test: FAILED")
        for b_ in bad[:25]:
            print("   ", b_)
        return 1
    print("rights test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
