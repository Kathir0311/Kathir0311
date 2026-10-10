"""Sample files and the profile of a file (V4.20), through the app.

A client, "Journal X", sets its section heads in capitals at the body size —
the analysis reads them as paragraphs — and prints a running foot. One sample
file tagged the way the client wants (heads = H2, foot = artifact):

* uploaded for a new client, with a profile made for it; learned in the
  worker; the knowledge shows the wording, the foot, the tag shapes
* a new file of the client: its heads (other wording too) become H2, its foot
  an artifact, each with a note "samples: …"
* the same file with another profile is left as the analysis made it
* the editor's "samples say" for a tag
* the profile of a file changed (and analysed again) by its user; a user may
  not change another user's file; an admin changes many at once
* a sample switched off is not used; a finished file added as a sample;
  a sample deleted
* the admin overview: profiles, the last 14 days, due dates
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

HEADS = [["KEY POINTS", "BACKGROUND", "METHODS"], ["RESULTS", "DISCUSSION", "KEY POINTS"], ["LIMITATIONS", "CONCLUSION"]]
TARGET = [["KEY POINTS", "SUMMARY", "FINDINGS", "NEXT STEPS"]]


def wait(c, jid, n=480):
    for _ in range(n):
        st = c.get(f"/api/job/{jid}/status").json.get("stage")
        if st in ("review", "done", "error"):
            return st
        time.sleep(0.5)
    return "timeout"


def tagged_sample(path: str) -> None:
    from tests.samples_gen import make_tagged
    make_tagged(path, TMP)


def roles(c, jid):
    m = c.get(f"/api/job/{jid}/model").json
    out = {}
    for n in m["nodes"].values():
        t = (n.get("text") or "").strip()
        if t in ("KEY POINTS", "SUMMARY", "FINDINGS", "NEXT STEPS") or t.startswith("Journal of Testing"):
            out[t[:18]] = "Artifact" if n.get("artifact") else n["role"]
    return out, m


def main() -> int:
    import app as A
    from tests.samples_gen import make
    bad: list = []

    def ok(cond, msg):
        if not cond:
            bad.append(msg)

    c = A.app.test_client()
    c.post("/login", data={"username": "Kathir", "password": "Kathir@051408"})
    sample = os.path.join(TMP, "journal_x_sample.pdf")
    tagged_sample(sample)
    r = c.post("/api/admin/samples/upload", data={"client": "__new__", "new_client": "Journal X", "make_profile": "on",
                                                  "base_profile": "Default", "pdf": [(open(sample, "rb"), "journal_x_sample.pdf")]},
               content_type="multipart/form-data")
    ok(r.status_code == 200 and r.json.get("profile_made") == "Journal X", f"upload a sample {r.json}")
    sid = (r.json.get("ids") or [""])[0]
    for _ in range(240):
        rows = c.get("/api/admin/samples").json["samples"]
        st = next((x["status"] for x in rows if x["id"] == sid), "")
        if st in ("ready", "error"):
            break
        time.sleep(0.5)
    ok(st == "ready", f"the sample was learned: {st} {rows[0].get('error') if rows else ''}")
    k = c.get("/api/admin/samples/knowledge?client=Journal X").json
    ok(k.get("files") == 1 and k["roles"].get("H2", 0) >= 6, f"knowledge roles {k.get('roles')}")
    ok(any(w["text"] == "KEY POINTS" and w["role"] == "H2" for w in k.get("words", [])), f"wording {k.get('words')}")
    ok(any("journal of testing" in a["pattern"] and a["band"] == "foot" for a in k.get("artifacts", [])), f"foot {k.get('artifacts')}")
    ok("Journal X" in A.all_profiles(), "the client's profile")

    # a new file of the client
    tgt = os.path.join(TMP, "target.pdf")
    make(tgt, TARGET, volume=4, title="Another look at samples")

    def upload(profile, client=None):
        cl = client or c
        r = cl.post("/upload", data={"profile": profile, "pdf": [(open(tgt, "rb"), "target.pdf")]},
                    content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
        jid = r.json["jobs"][0]
        ok(wait(cl, jid) == "review", f"{profile}: analysed")
        return jid

    jx = upload("Journal X")
    got, m = roles(c, jx)
    ok(all(got.get(h) == "H2" for h in ("KEY POINTS", "SUMMARY", "FINDINGS", "NEXT STEPS")), f"Journal X heads {got}")
    ok(got.get("Journal of Testing") == "Artifact", f"Journal X foot {got}")
    ok(any("samples:" in (n.get("note") or "") for n in m["nodes"].values()), "the notes say samples")
    ok((A.read_meta(jx).get("samples") or {}).get("changed", 0) >= 4, f"meta samples {A.read_meta(jx).get('samples')}")

    jd = upload("Default")
    got, m = roles(c, jd)
    ok(got.get("SUMMARY") == "P" and got.get("KEY POINTS") == "P", f"Default untouched {got}")
    p = next(n for n in m["nodes"].values() if (n.get("text") or "") == "SUMMARY")
    s = c.get(f"/api/job/{jd}/samples/suggest?id={p['id']}").json
    ok(s.get("files") == 0, f"Default has no samples {s}")
    s = c.get(f"/api/job/{jx}/samples/suggest?id={next(n['id'] for n in roles(c, jx)[1]['nodes'].values() if n.get('text') == 'SUMMARY')}").json
    ok(s.get("files") == 1 and s["votes"] and s["votes"][0]["role"] == "H2", f"samples say {s}")

    # the profile of a file, changed by its user
    ok(c.post("/api/admin/users", json={"username": "ravi", "password": "secret12"}).json.get("ok"), "user")
    u = A.app.test_client()
    u.post("/login", data={"username": "ravi", "password": "secret12"})
    ju = upload("Default", u)
    r = u.post(f"/api/job/{ju}/profile", json={"profile": "Journal X", "reanalyse": True})
    ok(r.json.get("ok") and r.json.get("from") == "Default", f"the user changes the profile {r.json}")
    ok(wait(u, ju) == "review", "analysed again")
    got, _ = roles(u, ju)
    ok(got.get("SUMMARY") == "H2", f"after the change {got}")
    ok(A.read_meta(ju)["profile"] == "Journal X" and A.read_meta(ju)["profile_history"], "profile kept in meta")
    ok(u.post(f"/api/job/{jd}/profile", json={"profile": "Journal X"}).status_code == 403, "another user's file")
    r = u.post(f"/api/job/{ju}/profile", json={"profile": "Kriyadocs", "reanalyse": False})
    ok(r.json.get("ok") and not r.json.get("reanalysed"), "change for the build only")
    ok(A.read_meta(ju)["options"]["house_style"] == "kriyadocs", "the options follow the profile")
    ok(u.post(f"/api/job/{ju}/profile", json={"profile": "Nope"}).status_code == 400, "an unknown profile")
    r = c.post("/api/admin/files/profile", json={"ids": [jd, jx], "profile": "Academain", "reanalyse": False})
    ok(r.json.get("changed") == 2 and A.read_meta(jd)["profile"] == "Academain", f"admin bulk {r.json}")
    ok(u.post("/api/admin/files/profile", json={"ids": [ju], "profile": "Default"}).status_code == 403, "bulk is admin only")
    files = c.get("/api/admin/files").json
    ok({f["profile"] for f in files} >= {"Academain", "Kriyadocs"}, "files carry the profile (filter)")

    # a sample switched off is not used
    c.post(f"/api/admin/samples/{sid}", json={"enabled": False})
    j2 = upload("Journal X")
    got, _ = roles(c, j2)
    ok(got.get("SUMMARY") == "P", f"switched off {got}")
    c.post(f"/api/admin/samples/{sid}", json={"enabled": True})

    # a finished file as a sample; then deleted
    r = c.post("/api/admin/samples/from_jobs", json={"ids": [jx]})
    ok(r.json.get("added") == 1, f"from jobs {r.json}")
    for _ in range(240):
        rows = c.get("/api/admin/samples").json["samples"]
        if all(x["status"] in ("ready", "error") for x in rows):
            break
        time.sleep(0.5)
    fj = next((x for x in rows if x.get("kind") == "file"), None)
    ok(fj and fj["status"] == "ready" and fj["client"] == "Academain" or (fj and fj["status"] == "ready"), f"file sample {fj}")
    ok(c.delete(f"/api/admin/samples/{fj['id']}").json.get("ok") if fj else False, "delete a sample")
    ok(len(c.get("/api/admin/samples").json["samples"]) == 1, "one sample left")

    # overview
    ov = c.get("/api/admin/overview").json
    ok("Journal X" in ov.get("profiles", {}) or "Academain" in ov.get("profiles", {}), f"overview profiles {list(ov.get('profiles', {}))}")
    ok(len(ov.get("trend", [])) == 14 and ov["trend"][-1]["uploaded"] >= 4, f"trend {ov.get('trend', [])[-1:]}")
    ok("due" in ov and "fixed" in ov and ov.get("samples", {}).get("clients"), "overview parts")
    ok(c.get("/admin").status_code == 200 and b"pane-samples" in c.get("/admin").data, "the admin page")
    ok(u.get("/api/admin/samples").status_code == 403, "samples are admin only")

    if bad:
        print("samples test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("samples test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
