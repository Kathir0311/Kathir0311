"""The self-learning AI brain (V4.25), end to end through the app with a
stand-in Ollama.

* an older config.json whose upload default was Academain / Best quality now
  defaults to "Smart AI (all samples)" (the AI settings kept)
* sample files of one client ("Journal X", heads in capitals at the body
  size, which the analysis reads as paragraphs) are learned in the worker:
  each gets a document (type, fingerprint), a memory of its texts (hashed and
  AI vectors), the AI's study (the client's rules, the document type) — and
  the brain's own model is trained, with its accuracy measured on held-out files
* a file uploaded with no profile chosen gets Smart AI: every client's
  samples, the most similar first — its heads become H2 although the file is
  not "Journal X"'s; the brain's summary is kept with the file
* the brain alone (no sample pass) decides the heads; the running foot is
  not made a heading; with nothing learned, a doubtful tag goes to the AI
  with the nearest examples and the client's rules in the question
* the editor's popover shows what each source says; Think again runs in the
  queue and can be undone
* a built file's fixes are remembered (feedback); an approved file updates
  the trust of every source and the first-pass quality (the learning curve)
* the admin's brain page: model, trust, history, insights, style map; Train now
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
PORT = 11547
# an installation of V4.24: its upload default was the client's profile
with open(os.environ["PDFUA_CONFIG"], "w") as fh:
    json.dump({"ai": {"base_url": f"http://127.0.0.1:{PORT}", "enabled": True, "text_model": "llama3.1:8b",
                      "timeout": 120},
               "default_profile": "Academain", "site_defaults": 3}, fh)

SETS = [[["KEY POINTS", "BACKGROUND", "METHODS"], ["RESULTS", "DISCUSSION"]],
        [["OVERVIEW", "THE PLAN"], ["WHAT WE DID", "OUTCOMES", "LIMITS"]],
        [["INTRODUCTION", "AIMS"], ["DATA", "ANALYSIS", "SUMMARY"]]]
NEW = [["FINDINGS", "WHERE TO GO", "OPEN QUESTIONS"]]


def wait(c, jid, n=600):
    for _ in range(n):
        st = c.get(f"/api/job/{jid}/status").json.get("stage")
        if st in ("review", "done", "error"):
            return st
        time.sleep(0.5)
    return "timeout"


def brain_idle(c, n=600):
    for _ in range(n):
        d = c.get("/api/admin/brain").json
        if not d.get("running"):
            return d
        time.sleep(0.5)
    return c.get("/api/admin/brain").json


def main() -> int:
    from tests import fake_ollama
    srv = fake_ollama.Server(("127.0.0.1", PORT), fake_ollama.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    import app as A
    from core import brain, learn
    from core.options import SMART_PROFILE, profile_options
    from tests.samples_gen import make, make_tagged
    bad: list = []

    def ok(cond, msg):
        if not cond:
            bad.append(msg)

    # 1. the upload default
    cfg = A.load_config()
    ok(cfg["default_profile"] == SMART_PROFILE, f"default profile {cfg['default_profile']}")
    ok(cfg["ai"]["base_url"].endswith(str(PORT)) and cfg["ai"]["timeout"] >= 600, f"AI settings kept {cfg['ai']}")
    o = profile_options(SMART_PROFILE)
    ok(o.samples_scope == "all" and o.ai_brain and o.teachers_guide == "auto" and o.house_style == "none",
       "the Smart AI profile")

    c = A.app.test_client()
    c.post("/login", data={"username": "Kathir", "password": "Kathir@051408"})

    # 2. samples of one client, learned, studied by the AI, the model trained
    files = []
    for k, heads in enumerate(SETS):
        d = os.path.join(TMP, f"mk{k}")
        os.makedirs(d)
        p = os.path.join(TMP, f"journal_x_{k}.pdf")
        make_tagged(p, d, heads=heads)
        files.append((open(p, "rb"), os.path.basename(p)))
    r = c.post("/api/admin/samples/upload", data={"client": "__new__", "new_client": "Journal X", "make_profile": "on",
                                                  "base_profile": "Default", "pdf": files},
               content_type="multipart/form-data")
    ok(r.status_code == 200 and len(r.json.get("ids") or []) == 3, f"samples uploaded {r.json}")
    c.get("/api/admin/samples")                              # starts the learner
    st = brain_idle(c)
    rows = c.get("/api/admin/samples").json["samples"]
    ok(rows and all(x["status"] == "ready" for x in rows), f"learned {[(x['filename'], x['status'], x.get('error')) for x in rows]}")
    m = st.get("model") or {}
    ok(m.get("n_train", 0) >= 20 and set(m.get("classes") or []) >= {"P", "H2"}, f"the brain's model {m}")
    ok(m.get("accuracy") is not None and m["accuracy"] >= 0.8, f"model accuracy on held-out files {m.get('accuracy')}")
    ok(st.get("memory", 0) >= 15 and st.get("embedded", 0) >= 15, f"memory {st.get('memory')} / {st.get('embedded')}")
    ok(any(s_["role"] == "H2" and s_["caps"] for s_ in st.get("style_map") or []), f"style map {st.get('style_map')}")
    ins = [x for x in st.get("insights") or [] if x.get("rules")]
    ok(len(ins) == 3 and ins[0]["doc_type"] == "journal_article" and "capitals" in ins[0]["rules"][0]["rule"],
       f"the AI studied the samples {st.get('insights')}")
    ok(st.get("doc_types", {}).get("journal_article") == 3, f"document types {st.get('doc_types')}")

    # 3. a file uploaded with no profile chosen: Smart AI, every client's samples
    src = os.path.join(TMP, "other_client.pdf")
    make(src, NEW, volume=9, title="A file of another client")
    r = c.post("/upload", data={"pdf": [(open(src, "rb"), "other_client.pdf")]},
               content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
    jid = r.json["jobs"][0]
    ok(wait(c, jid) == "review", "analysed")
    meta = c.get(f"/api/job/{jid}/status").json
    model = c.get(f"/api/job/{jid}/model").json
    heads = {n["text"]: n["role"] for n in model["nodes"].values() if (n.get("text") or "") in sum(NEW, [])}
    ok(heads and set(heads.values()) == {"H2"}, f"another client's file learned from Journal X: {heads}")
    foot = [n for n in model["nodes"].values() if (n.get("text") or "").startswith("Journal of Testing")]
    ok(all(n.get("artifact") or n["role"] == "P" for n in foot), f"the running foot is no heading {foot}")
    b = c.get(f"/api/job/{jid}/brain").json
    ok(b.get("on") and b["brain"].get("version") == 1 and b["brain"].get("quality") is not None
       and b["brain"].get("doc_type") == "journal_article", f"the brain's summary {b}")
    ok(any(s_["client"] == "Journal X" for s_ in b["brain"].get("similar") or []), "the most similar samples are named")
    a_meta = json.load(open(os.path.join(os.environ["PDFUA_JOBS"], jid, "meta.json")))
    ok(a_meta.get("profile") == SMART_PROFILE and (a_meta.get("brain") or {}).get("candidates"), "meta: profile + brain")

    # 4. the brain alone (no sample pass), and the AI with the nearest examples
    from core.ai import AIConfig
    from core.pipeline import load_model, prepare
    job2 = os.path.join(TMP, "direct")
    os.makedirs(job2)
    src2 = os.path.join(job2, "original.pdf")
    make(src2, NEW, volume=11)
    od = profile_options("Default")
    od.use_samples = False
    od.ai_alt_text_auto = False
    od.existing_tags = "retag"
    prepare(job2, od)
    mdl = load_model(job2)
    out = brain.think(mdl, os.path.join(job2, "working.pdf"), profile_options(SMART_PROFILE), client="Nobody")
    got = {n.text: n.role for n in mdl.nodes.values() if n.text in sum(NEW, [])}
    ok(set(got.values()) == {"H2"} and out["changed"] >= 3, f"the brain decides by itself {got} {out.get('sources')}")
    ok(all("brain:" in n.note for n in mdl.nodes.values() if n.text in sum(NEW, [])), "notes say brain")
    ok(not any(n.role.startswith("H") and (n.text or "").startswith("Journal of Testing") for n in mdl.nodes.values()),
       "the brain never makes the running foot a heading")
    # nothing learned (samples off): the doubtful tags go to the AI
    mdl = load_model(job2)
    for n in mdl.nodes.values():
        if n.text in sum(NEW, []):
            n.confidence = 0.5
    aic = AIConfig(base_url=f"http://127.0.0.1:{PORT}", text_model="llama3.1:8b", enabled=True)
    fake_ollama.Server.seen.clear()
    out = brain.think(mdl, os.path.join(job2, "working.pdf"), profile_options(SMART_PROFILE), ai_cfg=aic,
                      client="Journal X", use_samples=False)
    got = {n.text: n.role for n in mdl.nodes.values() if n.text in sum(NEW, [])}
    ok(out["ai_asked"] >= 3 and out["ai_changed"] == 3 and set(got.values()) == {"H2"},
       f"the AI decided the doubtful tags {out.get('ai_asked')} {out.get('ai_changed')} {got} {out.get('ai_error')}")
    # V4.31: the tags Ollama chose are Dez AI's lessons, with the layout of each block
    from core import teacher
    with learn.connect() as c_:
        taught = [dict(r) for r in c_.execute("SELECT text, role, accepted, size_rel, caps FROM teacher_roles WHERE client='Journal X'")]
        ok(len(taught) >= 3 and all(t["role"] == "H2" and t["accepted"] == 1 for t in taught if t["text"] in sum(NEW, [])),
           f"Ollama's tags are kept as lessons with their layout {taught[:3]}")
        ok(len(teacher.role_rows(c_)) >= 3, "…and train the brain's model (little weight, until a person has judged them)")
    ok(out.get("taught", 0) >= 3, f"the brain's summary says how many it was taught {out.get('taught')}")
    # with the samples, the question carries the nearest finished examples and the client's rules
    mdl = load_model(job2)
    for n in mdl.nodes.values():
        if n.text in sum(NEW, []):
            n.confidence = 0.5
    real = brain.combine
    brain.combine = lambda ctx, ops: ({"P": 1.0, "H2": 1.0}, "P", 0.5, 0.0)       # the sources cannot settle it
    try:
        fake_ollama.Server.seen.clear()
        out = brain.think(mdl, os.path.join(job2, "working.pdf"), profile_options(SMART_PROFILE), ai_cfg=aic,
                          client="Journal X")
    finally:
        brain.combine = real
    prompt = next((x.get("prompt", "") for x in fake_ollama.Server.seen
                   if "tagging brain" in x.get("prompt", "")), "")
    ok("similar finished texts" in prompt and "→ H2" in prompt, "the question carries the nearest examples")
    ok("capitals at the body size are H2" in prompt, "the question carries the client's rules (AI study)")
    ok(any(x.get("path") == "/api/embed" for x in fake_ollama.Server.seen), "the question used the AI's vectors")

    # 5. the editor's popover
    nid = next(n["id"] for n in model["nodes"].values() if n.get("text") == "WHERE TO GO")
    r = c.get(f"/api/job/{jid}/samples/suggest?id={nid}").json
    br = r.get("brain") or {}
    ok(br.get("brain") and br["brain"][0]["role"] == "H2" and "model" in (br.get("sources") or {}),
       f"what each source says {br}")

    # 6. Think again — queued, undoable
    r = c.post(f"/api/job/{jid}/brain/think", json={})
    ok(r.json.get("queued"), f"think again {r.json}")
    for _ in range(240):
        t = c.get(f"/api/job/{jid}/brain/think").json
        if not t.get("running"):
            break
        time.sleep(0.5)
    ok((t.get("result") or {}).get("candidates"), f"think again ran {t}")
    ok(c.get(f"/api/job/{jid}/model").json.get("can_undo"), "think again can be undone")

    # 7. a person fixes a tag, builds (feedback), the admin approves (trust, first-pass quality)
    model = c.get(f"/api/job/{jid}/model").json
    sub = next(n for n in model["nodes"].values() if n.get("text") == "OPEN QUESTIONS")
    c.post(f"/api/job/{jid}/node/{sub['id']}", json={"role": "H3"})
    r = c.post(f"/api/job/{jid}/build", json={})
    ok(r.status_code == 200, f"build {r.json}")
    ok(wait(c, jid) == "done", "built")
    with learn.connect() as db:
        fb = [dict(x) for x in db.execute("SELECT from_role, role, text FROM feedback WHERE job=?", (jid,))]
        hist = [dict(x) for x in db.execute("SELECT * FROM history WHERE job=?", (jid,))]
    ok(any(x["role"] == "H3" and x["text"] == "OPEN QUESTIONS" for x in fb), f"feedback {fb}")
    ok(any(h["kind"] == "built" and h["tags"] and h["quality"] is not None and h["quality"] < 100 for h in hist),
       f"history at build {hist}")
    r = c.post(f"/api/job/{jid}/work", json={"action": "approve"})
    ok(r.json.get("sample"), f"approved → sample {r.json}")
    st = brain_idle(c)
    with learn.connect() as db:
        fb = db.execute("SELECT COUNT(*) n FROM feedback WHERE job=?", (jid,)).fetchone()["n"]
        trust = {(x["source"], x["role"]): (x["ok"], x["n"]) for x in db.execute("SELECT * FROM trust")}
        hist = [dict(x) for x in db.execute("SELECT * FROM history WHERE job=? AND kind='approved'", (jid,))]
        learned_flags = {x["learned"] for x in db.execute("SELECT learned FROM decisions WHERE job=?", (jid,))}
    ok(fb == 0, "the approved sample replaces the feedback")
    ok(trust.get(("model", "*"), (0, 0))[1] > 0 and trust.get(("rules", "*"), (0, 0))[1] > 0, f"trust {trust}")
    ok(hist and hist[0]["quality"] is not None and 0 < hist[0]["quality"] < 100, f"first-pass quality {hist}")
    ok(learned_flags == {1}, f"decisions counted once {learned_flags}")
    ok(any(t["source"] == "model" and t["n"] for t in st.get("trust") or []), "the trust is on the brain page")
    ok(any(h.get("file") == "other_client.pdf" and h["kind"] == "approved" for h in st.get("history") or []),
       "the history names the file")

    # 8. Train now
    before = (st.get("model") or {}).get("id")
    r = c.post("/api/admin/brain/train", json={})
    ok(r.json.get("ok"), f"train {r.json}")
    time.sleep(0.5)
    st = brain_idle(c)
    ok((st.get("model") or {}).get("id", 0) > (before or 0), f"trained again {st.get('model')} {st.get('job')}")
    page = c.get("/admin")
    ok(page.status_code == 200 and b"pane-brain" in page.data, "the admin page has the brain tab")

    # 9. learning settings: the brain switched off → no brain for the next file
    c.post("/api/admin/learning", json={"brain": False})
    r = c.post("/upload", data={"pdf": [(open(src, "rb"), "again.pdf")]},
               content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
    j3 = r.json["jobs"][0]
    ok(wait(c, j3) == "review", "analysed without the brain")
    ok(not (c.get(f"/api/job/{j3}/brain").json.get("brain") or {}).get("version"), "brain off → not run")
    c.post("/api/admin/learning", json={"brain": True})

    # 10. the command line
    import io
    from contextlib import redirect_stdout

    import cli
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli.main(["--brain-status"])
    ok("model: #" in buf.getvalue() and "trust model" in buf.getvalue(), f"cli {buf.getvalue()[:300]}")

    if bad:
        print("brain test: FAILED")
        for b_ in bad:
            print("   ", b_)
        return 1
    print("brain test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
