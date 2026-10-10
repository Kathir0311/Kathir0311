"""Approved files teach the tool; the AI and the client's structure (V4.21).

Through the app (Flask test client), with a stand-in Ollama:

* a Default file whose capital heads the analysis reads as P; a person makes
  them H2 and the admin approves it → it becomes a sample of "Default" by
  itself: the file, its tag model (in the database), what the tool proposed,
  what people changed (4 × P → H2), its details
* the next Default file: its capital heads (other wording too) are H2
* the editor's Client tab: an older file is compared with the samples and the
  suggestions are taken (undo works)
* sent back → the sample is switched off; approved again → on, brought up to date
* learning settings: with "approved files teach" off, approving makes no sample
* the AI: tags the samples cannot settle are asked with the client's examples
* the client's structure: captions put where the samples put them
* the sample drawer, the stored model, the database backup
"""
from __future__ import annotations

import json
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
PORT = 11531
with open(os.environ["PDFUA_CONFIG"], "w") as fh:
    json.dump({"ai": {"base_url": f"http://127.0.0.1:{PORT}", "enabled": True, "text_model": "llama3.1:8b"},
               "site_defaults": 99}, fh)

HEADS = [["KEY POINTS", "SUMMARY", "FINDINGS", "NEXT STEPS"]]
NEXT = [["OVERVIEW", "THE DATA", "WHAT WE SAW"]]


def wait(c, jid, n=480):
    for _ in range(n):
        st = c.get(f"/api/job/{jid}/status").json.get("stage")
        if st in ("review", "done", "error"):
            return st
        time.sleep(0.5)
    return "timeout"


def learned(c, n=240):
    for _ in range(n):
        rows = c.get("/api/admin/samples").json["samples"]
        if rows and all(x["status"] in ("ready", "error") for x in rows):
            return rows
        time.sleep(0.5)
    return c.get("/api/admin/samples").json["samples"]


def main() -> int:
    from tests import fake_ollama
    srv = fake_ollama.Server(("127.0.0.1", PORT), fake_ollama.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    import app as A
    from tests.samples_gen import make
    bad: list = []

    def ok(cond, msg):
        if not cond:
            bad.append(msg)

    c = A.app.test_client()
    c.post("/login", data={"username": "Kathir", "password": "Kathir@051408"})
    c.post("/api/admin/learning", json={"use_ai": False})        # the AI is tested on its own below

    def upload(heads, profile="Default", name="f.pdf"):
        p = os.path.join(TMP, name)
        make(p, heads, volume=5, title="A file of the client")
        r = c.post("/upload", data={"profile": profile, "pdf": [(open(p, "rb"), name)]},
                   content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
        jid = r.json["jobs"][0]
        ok(wait(c, jid) == "review", f"{name}: analysed")
        return jid

    def roles(jid):
        m = c.get(f"/api/job/{jid}/model").json
        return {(n.get("text") or ""): n["role"] for n in m["nodes"].values() if (n.get("text") or "").isupper()}, m

    j1 = upload(HEADS, name="first.pdf")
    j_old = upload(NEXT, name="older.pdf")             # analysed before anything was learned
    got, m1 = roles(j1)
    ok(set(got.values()) == {"P"}, f"no samples yet: {got}")
    for n in m1["nodes"].values():
        if (n.get("text") or "").isupper():
            c.post(f"/api/job/{j1}/node/{n['id']}", json={"role": "H2"})
    r = c.post(f"/api/job/{j1}/work", json={"action": "approve"})
    ok(r.json.get("work") == "approved" and r.json.get("sample"), f"approved → sample {r.json}")
    rows = learned(c)
    s1 = next((x for x in rows if x.get("job") == j1), None)
    ok(s1 and s1["kind"] == "approved" and s1["status"] == "ready" and s1["client"] == "Default", f"the approved sample {s1}")
    det = c.get(f"/api/admin/samples/{s1['id']}/detail").json if s1 else {}
    ok(det.get("model") and det["model"]["size"] > 0, f"the model is in the database {det.get('model')}")
    ok(len(det.get("corrections") or []) >= 4 and all(x["from_role"] == "P" and x["role"] == "H2" for x in det["corrections"]),
       f"what people changed {det.get('corrections')}")
    ok((det.get("details") or {}).get("approved_by") == "Kathir" and (det["details"].get("model") or {}).get("hand_edits", 0) >= 0
       and det["details"].get("pages") == 1, f"details {det.get('details')}")
    r = c.get(f"/admin/samples/{s1['id']}/model.json")
    ok(r.status_code == 200 and json.loads(r.data).get("nodes"), "the stored model downloads")
    r = c.get(f"/admin/samples/{s1['id']}/model.json?which=auto")
    ok(r.status_code == 200 and json.loads(r.data).get("nodes"), "what the tool proposed downloads")
    k = c.get("/api/admin/samples/knowledge?client=Default").json
    ok(any(f["from"] == "P" and f["to"] == "H2" and f["n"] >= 4 for f in k.get("fixes", [])), f"fixes in the knowledge {k.get('fixes')}")
    ok(k.get("kinds", {}).get("approved") == 1, f"kinds {k.get('kinds')}")

    # the next file of the client
    j2 = upload(NEXT, name="next.pdf")
    got, _ = roles(j2)
    ok(got and set(got.values()) == {"H2"}, f"learned from the approved file: {got}")

    # the editor's Client tab on the older file
    cmp_ = c.get(f"/api/job/{j_old}/samples/compare").json
    sug = [x for x in cmp_.get("nodes", []) if x["says"] == "H2"]
    ok(len(sug) == 3, f"compare suggests {cmp_.get('nodes')}")
    r = c.post(f"/api/job/{j_old}/samples/apply", json={"items": [{"id": x["id"], "role": x["says"]} for x in sug]})
    ok(r.json.get("changed") == 3, f"apply {r.json}")
    got, _ = roles(j_old)
    ok(set(got.values()) == {"H2"}, f"taken {got}")
    c.post(f"/api/job/{j_old}/undo")
    got, _ = roles(j_old)
    ok(set(got.values()) == {"P"}, f"undo {got}")

    # sent back / approved again
    c.post(f"/api/job/{j1}/work", json={"action": "return", "text": "one more fix"})
    rows = c.get("/api/admin/samples").json["samples"]
    ok(next(x for x in rows if x.get("job") == j1)["enabled"] == 0, "sent back → switched off")
    c.post(f"/api/job/{j1}/work", json={"action": "approve"})
    rows = learned(c)
    ok(sum(1 for x in rows if x.get("job") == j1) == 1 and next(x for x in rows if x.get("job") == j1)["enabled"] == 1,
       "approved again → the same sample, on")

    # learning settings
    r = c.post("/api/admin/learning", json={"auto_approved": False})
    ok(r.json["learning"]["auto_approved"] is False and r.json["db"]["models"] >= 1, f"settings {r.json}")
    c.post("/api/admin/files/update", json={"ids": [j2], "work": "approved"})
    ok(not any(x.get("job") == j2 for x in c.get("/api/admin/samples").json["samples"]), "off → no sample")
    c.post("/api/admin/learning", json={"auto_approved": True})
    c.post("/api/admin/files/update", json={"ids": [j2], "work": "approved"})
    ok(any(x.get("job") == j2 for x in c.get("/api/admin/samples").json["samples"]), "bulk approve → sample")
    learned(c)
    r = c.get("/admin/samples/backup.db")
    ok(r.status_code == 200 and r.data[:15] == b"SQLite format 3", "database backup")

    # the AI, shown the client's examples, settles what the samples cannot
    from core import learn
    from core.ai import AIConfig
    from core.pipeline import load_model
    model = load_model(os.path.join(os.environ["PDFUA_JOBS"], j_old))
    for n in model.nodes.values():
        if (n.text or "").isupper():
            n.confidence = 0.5
    real_vote = learn.Knowledge.vote
    learn.Knowledge.vote = lambda self, f, k=9: [("P", 0.5, 0.3), ("H2", 0.5, 0.3)]
    try:
        cfg = AIConfig(base_url=f"http://127.0.0.1:{PORT}", text_model="llama3.1:8b", enabled=True)
        out = learn.apply_samples(model, os.path.join(os.environ["PDFUA_JOBS"], j_old, "working.pdf"), "Default",
                                  ai_cfg=cfg)
    finally:
        learn.Knowledge.vote = real_vote
    ok(out.get("ai") == 3, f"AI answers {out.get('ai')} {out.get('ai_error')}")
    ok(all(n.role == "H2" and "samples + AI" in n.note for n in model.nodes.values() if (n.text or "").isupper()),
       "AI notes")
    prompt = next((x.get("prompt", "") for x in fake_ollama.Server.seen if "the way one client" in x.get("prompt", "")), "")
    ok("KEY POINTS" in prompt and "tool said P" in prompt, "the question carries the client's examples and fixes")

    # the client's structure: where captions go
    from core.model import DocModel, StructNode
    m = DocModel(path="", page_count=1)
    cap = m.add(StructNode(id="c1", role="Caption", page=0, text="Table 1 Scores"))
    t = m.add(StructNode(id="t1", role="Table", page=0))
    m.add(StructNode(id="tb", role="TBody", page=0), "t1")
    know = learn.Knowledge("nobody")
    know.captions[("Table", "inside_first")] = 5
    know.captions[("Table", "before")] = 1
    ok(learn.place_captions(m, know) == 1 and t.children[0] == "c1" and cap.parent == "t1", "caption moved inside first")
    know.captions[("Table", "before")] = 50
    ok(learn.place_captions(m, know) == 1 and m.root_children[0] == "c1", "caption moved before")

    if bad:
        print("learning test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("learning test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
