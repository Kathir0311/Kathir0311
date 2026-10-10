"""V4.26 — the learning engine writes its own code; nothing is missed; nothing leaves the server.

Through the app, with a stand-in Ollama:

* the privacy guard: a public address and a public DNS name are refused (and
  logged); the local Ollama answers; every page carries a Content-Security-Policy
* the inventory of an uploaded file and its coverage: a line no tag holds is
  found, tagged in one click and the coverage is whole again
* four files corrected by people (capital heads → H2, a licence stamp →
  artifact), built and approved: the learning engine writes a rule for each
  as Python, checks it (whitelist), tests it on the database and switches it
  on; the code is in sample_files/learned_code/
* the brain uses the rules: a new file's stamp is an artifact by learned
  code, its heads H2 with the code's voice in the decision
* the checks: imports, attributes, builtins, lambdas, loops are refused; the
  AI's proposal is data, a bad feature in it is dropped
* a rule people keep undoing retires itself
* memory: a picture described in one file gets the same alt text in the
  next; a reading set once is used again
* one fix → the brain offers the tags that look the same; live learning
  records the fix without a build
* the offline rule book: search, a house rule
"""
from __future__ import annotations

import json
import os
import socket
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
os.environ.pop("PDFUA_NETGUARD", None)
PORT = 11563
with open(os.environ["PDFUA_CONFIG"], "w") as fh:
    json.dump({"ai": {"base_url": f"http://127.0.0.1:{PORT}", "enabled": True, "text_model": "llama3.1:8b",
                      "timeout": 60}, "site_defaults": 99, "learning": {"use_ai": False}}, fh)

HEADS = [[["KEY POINTS", "BACKGROUND"], ["METHODS", "RESULTS"]], [["OVERVIEW", "THE PLAN"], ["OUTCOMES"]],
         [["INTRODUCTION", "AIMS"], ["DATA", "ANALYSIS"]], [["CONTEXT", "APPROACH"], ["EVIDENCE"]]]
STAMP = "Licensed copy {n} - do not share"


def logo_png() -> bytes:
    import pymupdf
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 40), 0)
    pix.set_rect(pix.irect, (200, 30, 30))
    pix.set_rect(pymupdf.IRect(10, 10, 30, 30), (20, 20, 200))
    return pix.tobytes("png")


def make_file(path: str, heads, n: int, logo: bool = False) -> None:
    import pymupdf

    from tests.samples_gen import make
    tmp = path + ".src.pdf"
    make(tmp, heads, volume=n)
    d = pymupdf.open(tmp)
    for p in d:
        p.insert_text((330, 112), STAMP.format(n=1000 + n), fontsize=6, fontname="helv")
    if logo:
        d[0].insert_image(pymupdf.Rect(400, 600, 520, 680), stream=logo_png())
    d.save(path)
    d.close()


def wait(c, jid, n=600):
    for _ in range(n):
        st = c.get(f"/api/job/{jid}/status").json.get("stage")
        if st in ("review", "done", "error"):
            return st
        time.sleep(0.4)
    return "timeout"


def idle(c, n=800):
    for _ in range(n):
        d = c.get("/api/admin/brain").json
        if not d.get("running"):
            return d
        time.sleep(0.4)
    return c.get("/api/admin/brain").json


def main() -> int:
    from tests import fake_ollama
    srv = fake_ollama.Server(("127.0.0.1", PORT), fake_ollama.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    import app as A
    from core import brain, codegen, kb, learn, netguard
    bad: list = []

    def ok(cond, msg):
        if not cond:
            bad.append(msg)

    c = A.app.test_client()
    c.post("/login", data={"username": "Kathir", "password": "Kathir@051408"})

    # 1. the privacy guard
    ok(netguard.status()["on"], "guard on")
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=2).close()
        ok(False, "a public address was reached")
    except PermissionError as exc:
        ok("privacy guard" in str(exc), f"public address refused: {exc}")
    except OSError as exc:
        ok(False, f"public address: not refused by the guard ({exc})")
    try:
        socket.getaddrinfo("example.com", 443)
        ok(False, "a public name was resolved")
    except socket.gaierror as exc:
        ok("privacy guard" in str(exc), f"public name refused: {exc}")
    r = c.post("/api/ai/ping", json={})
    ok(r.json.get("ok") or r.json.get("models"), f"the local Ollama answers {r.json}")
    pv = c.get("/api/admin/privacy").json
    ok(pv["on"] and any("8.8.8.8" in x["target"] for x in pv["recent"]) and any(x["target"] == "example.com" for x in pv["recent"]),
       f"blocked attempts logged {pv.get('recent')}")
    page = c.get("/")
    csp = page.headers.get("Content-Security-Policy", "")
    ok("connect-src 'self'" in csp and "default-src 'self'" in csp, f"CSP {csp}")

    # 2. four files corrected by people, built and approved
    jobs = []
    for k, heads in enumerate(HEADS):
        p = os.path.join(TMP, f"f{k}.pdf")
        make_file(p, heads, k, logo=(k == 0))
        r = c.post("/upload", data={"profile": "Default", "pdf": [(open(p, "rb"), f"f{k}.pdf")]},
                   content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
        jid = r.json["jobs"][0]
        ok(wait(c, jid) == "review", f"f{k} analysed")
        m = c.get(f"/api/job/{jid}/model").json
        stamps = [n for n in m["nodes"].values() if (n.get("text") or "").startswith("Licensed copy")]
        ok(stamps and all(not n.get("artifact") for n in stamps), f"f{k}: the stamp is read as text {stamps[:1]}")
        for n in m["nodes"].values():
            t = n.get("text") or ""
            if t.isupper() and len(t) < 30 and not n.get("artifact"):
                c.post(f"/api/job/{jid}/node/{n['id']}", json={"role": "H2"})
            elif t.startswith("Licensed copy"):
                c.post(f"/api/job/{jid}/node/{n['id']}", json={"role": "Artifact"})
            elif k == 0 and n["role"] == "Figure":
                c.post(f"/api/job/{jid}/node/{n['id']}", json={"alt": "Dezquare test logo"})
            elif k == 0 and t == "A study of stable samples":
                c.post(f"/api/job/{jid}/node/{n['id']}", json={"actual_text": "A study of samples that stay stable"})
        jobs.append(jid)
    # live learning: the fixes are remembered a few seconds after the last edit, with no build —
    # by a learner process of its own (V4.28), so wait for it instead of for a fixed time
    live = 0
    for _ in range(80):
        with learn.connect() as db:
            live = db.execute("SELECT COUNT(*) n FROM feedback WHERE job=?", (jobs[0],)).fetchone()["n"]
        if live >= 4:
            break
        time.sleep(0.5)
    ok(live >= 4, f"live learning without a build: {live} fix(es)")
    for jid in jobs:
        c.post(f"/api/job/{jid}/build", json={})
        ok(wait(c, jid) == "done", "built")
        r = c.post(f"/api/job/{jid}/work", json={"action": "approve"})
        ok(r.json.get("sample"), f"approved {r.json}")
    c.get("/api/admin/samples")
    st = idle(c)
    rules = c.get("/api/admin/code").json["rules"]
    h2 = [r for r in rules if r["target"] == "H2" and r["status"] == "active"]
    art = [r for r in rules if r["target"] == "Artifact" and r["status"] == "active"]
    ok(h2 and any(cn["f"] == "caps" for cn in h2[0]["conditions"]), f"a rule for the heads {rules}")
    ok(art and any(cn["f"] == "text" and cn["v"] == "licensed" for cn in art[0]["conditions"])
       and any(cn["f"] == "y" for cn in art[0]["conditions"]), f"a rule for the stamp {[(r['target'], r['conditions']) for r in rules]}")
    for r in (h2 + art)[:2]:
        ok("def match(f):" in r["source"] and r["precision"] >= 0.93, f"{r['id']} source / precision")
        codegen.validate(r["source"])
        ok(os.path.exists(os.path.join(os.environ["PDFUA_SAMPLES"], "learned_code", r["id"] + ".py")), "the .py file")
    ok(any(r["status"] == "active" for r in st.get("code") or []), "the brain page lists the code")

    # 3. the brain uses the learned code on a new file (no sample pass)
    from core.options import SMART_PROFILE, profile_options
    from core.pipeline import load_model, prepare
    job2 = os.path.join(TMP, "direct")
    os.makedirs(job2)
    make_file(os.path.join(job2, "original.pdf"), [["FINDINGS", "WHERE TO GO"]], 9)
    od = profile_options("Default")
    od.use_samples = False
    od.ai_alt_text_auto = False
    od.existing_tags = "retag"
    prepare(job2, od)
    mdl = load_model(job2)
    out = brain.think(mdl, os.path.join(job2, "working.pdf"), profile_options(SMART_PROFILE), client="Other", job="direct")
    stamp = [n for n in mdl.nodes.values() if (n.text or "").startswith("Licensed copy")]
    ok(stamp and all(n.artifact and "learned code" in n.note for n in stamp), f"the stamp by learned code {[(n.role, n.note) for n in stamp]}")
    heads = {n.text: n.role for n in mdl.nodes.values() if n.text in ("FINDINGS", "WHERE TO GO")}
    ok(set(heads.values()) == {"H2"}, f"heads {heads}")
    with learn.connect() as db:
        ops = [json.loads(r["opinions"]) for r in db.execute("SELECT opinions FROM decisions WHERE job='direct'")]
    ok(any("code" in o and len(o["code"]) == 3 for o in ops) and out.get("code_fired", 0) >= 1,
       f"the code's voice is recorded with its rule id {out.get('code_fired')}")

    # 4. the checks
    evil = [
        "import os\ndef match(f):\n    return True\n",
        "def match(f):\n    return f.__class__\n",
        "def match(f):\n    return open('/etc/passwd')\n",
        "def match(f):\n    return __import__('os')\n",
        "match = lambda f: True\n",
        "def match(f):\n    for x in f: pass\n    return True\n",
        "def match(f):\n    return len(f['text']) > 3\n",
        "def match(f):\n    return f['__class__'] == 1\n",
        "def match(f):\n    return 9 ** 9 ** 9\n",
        "def match(f, g=1):\n    return True\n",
        "X = 1\ndef match(f):\n    return True\n",
    ]
    for src in evil:
        try:
            codegen.validate(src)
            ok(False, f"not refused: {src!r}")
        except codegen.UnsafeCode:
            pass
    good = codegen.write_source("R9999", "H2", ["P"], "", [{"f": "caps", "op": ">=", "v": 0.8},
                                                          {"f": "text", "op": "starts", "v": "key'\"\"\" x"}], {"precision": 1})
    rule = codegen.compile_rule(good)
    ok(rule["match"]({"caps": 1.0, "text": "key'\"\"\" x points"}) is True, "a written rule runs (quotes escaped)")
    from core.ai import AIConfig
    aic = AIConfig(base_url=f"http://127.0.0.1:{PORT}", text_model="llama3.1:8b", enabled=True)
    pos = [codegen.features({"caps": 1.0, "words": 2, "size_rel": 1.0}, "KEY POINTS", role="P") | {"_truth": "H2"}] * 3
    prop = codegen._ask_ai(aic, "H2", ["P"], "", pos, [])
    ok(prop and all(x["f"] in codegen.FEATURES for x in prop) and len(prop) == 2, f"the AI's proposal, cleaned {prop}")

    # 5. a rule people keep undoing retires itself
    rid = art[0]["id"] if art else ""
    if rid:
        ds = [{"node": f"n{i}", "opinions": {"code": ["Artifact", 0.99, rid]}} for i in range(4)]
        codegen.record_hits("undo-job", ds, {f"n{i}": "P" for i in range(4)}, verified=True)
        st_ = {r["id"]: r for r in codegen.listing()}[rid]
        ok(st_["status"] == "retired" and st_["wrong"] == 4, f"retired {st_['status']} {st_['wrong']}")
        ok(rid not in {r["id"] for r in codegen.active_rules(refresh=True)}, "no longer used")

    # 6. memory: the logo's alt text and the reading come back in the next file
    p = os.path.join(TMP, "next.pdf")
    make_file(p, [["SUMMARY"]], 21, logo=True)
    r = c.post("/upload", data={"profile": "Default", "pdf": [(open(p, "rb"), "next.pdf")]},
               content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
    j5 = r.json["jobs"][0]
    ok(wait(c, j5) == "review", "next analysed")
    m = c.get(f"/api/job/{j5}/model").json
    figs = [n for n in m["nodes"].values() if n["role"] == "Figure"]
    ok(figs and figs[0].get("alt") == "Dezquare test logo" and figs[0].get("source") == "memory",
       f"alt text from memory {[(f.get('alt'), f.get('source')) for f in figs]}")
    title = [n for n in m["nodes"].values() if n.get("text") == "A study of stable samples"]
    ok(title and title[0].get("actual_text") == "A study of samples that stay stable", f"reading from memory {title[:1]}")

    # 7. one fix → the others that look the same
    m = c.get(f"/api/job/{j5}/model").json
    p6 = os.path.join(TMP, "six.pdf")
    make_file(p6, [["ONE HEAD", "TWO HEAD", "THREE HEAD"], ["FOUR HEAD"]], 31)
    r = c.post("/upload", data={"profile": "Default", "options_json": json.dumps({"use_samples": False}),
                                "pdf": [(open(p6, "rb"), "six.pdf")]},
               content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
    j6 = r.json["jobs"][0]
    ok(wait(c, j6) == "review", "six analysed")
    m = c.get(f"/api/job/{j6}/model").json
    hs = [n for n in m["nodes"].values() if (n.get("text") or "").endswith("HEAD")]
    ok(len(hs) == 4 and all(n["role"] == "P" for n in hs), f"heads as P {[n['role'] for n in hs]}")
    c.post(f"/api/job/{j6}/node/{hs[0]['id']}", json={"role": "H2"})
    sim = c.get(f"/api/job/{j6}/brain/similar?id={hs[0]['id']}&from=P").json["items"]
    ok({x["id"] for x in sim} == {n["id"] for n in hs[1:]}, f"the brain offers the other heads {sim}")
    r = c.post(f"/api/job/{j6}/brain/similar/apply", json={"ids": [x["id"] for x in sim], "role": "H2"})
    m = c.get(f"/api/job/{j6}/model").json
    ok(r.json.get("changed") == 3 and all(m["nodes"][n["id"]]["role"] == "H2" for n in hs), f"applied {r.json}")
    ok(m.get("can_undo"), "undoable")

    # 8. the inventory and coverage of a file; a line no tag holds is found and tagged
    inv = c.get(f"/api/job/{j6}/inventory").json
    ok(inv["summary"]["pages"] == 2 and inv["summary"]["chars"] > 500 and inv["summary"]["coverage"]["percent"] == 100,
       f"inventory {inv['summary']}")
    body = next(n for n in m["nodes"].values() if (n.get("text") or "").startswith("The study looked") and n["page"] == 0
                and len(n.get("text") or "") > 200)
    c.post("/api/job/%s/move" % j6, json={"id": body["id"], "action": "delete_subtree"})
    inv = c.post(f"/api/job/{j6}/inventory", json={}).json
    miss = inv["inventory"]["coverage"]["missed"]
    ok(inv["summary"]["coverage"]["missed"] >= 1 and miss and miss[0]["page"] == 0, f"the untagged lines are found {inv['summary']}")
    ok(len(miss) >= 3, f"every line of the paragraph is found {len(miss)}")
    r = c.post(f"/api/job/{j6}/coverage/tag", json={"role": "P", "items": [{"page": x["page"], "bbox": x["bbox"]} for x in miss]})
    ok(r.json.get("made") == 1, f"the lines are one zone {r.json}")
    inv = c.post(f"/api/job/{j6}/inventory", json={}).json
    ok(inv["summary"]["coverage"]["missed"] == 0, f"whole again {inv['summary']['coverage']}")
    m = c.get(f"/api/job/{j6}/model").json
    back = [n for n in m["nodes"].values() if n["page"] == 0 and (n.get("text") or "").startswith("The study looked")
            and len(n.get("text") or "") > 200]
    ok(len(back) == 3 and all(n["role"] == "P" for n in back), f"one paragraph again {[(n['role'], len(n.get('text') or '')) for n in back]}")
    full = inv["inventory"]
    ok(full["document"]["pages"] == 2 and full["fonts"] and "original" in full, "the file's facts, fonts, the original")

    # 9. the offline rule book
    r = c.get("/api/kb?q=heading+levels+skip").json
    ok(r["offline"] and r["results"] and r["results"][0]["id"] == "ua-headings-nest", f"rule book {r['results'][:1]}")
    r = c.post("/api/admin/kb", json={"title": "Licence stamps are artifacts", "text": "The licence stamp printed on "
                                      "every page is an artifact.", "roles": "Artifact"})
    ok(r.json.get("ok"), "house rule added")
    r = c.get("/api/kb?q=licence+stamp").json
    ok(r["results"] and r["results"][0]["source"] == "house rule", f"house rule found {r['results'][:1]}")
    ok(kb.for_roles(["H2"]), "rule book lines for the AI")

    if bad:
        print("self-learning test: FAILED")
        for b_ in bad:
            print("   ", b_)
        return 1
    print("self-learning test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
