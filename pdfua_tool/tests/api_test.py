"""End-to-end HTTP test of the web app (server must be running on :$PORT, default 8000)."""
import http.client
import json
import os
import time
import urllib.request
import uuid

PORT = int(__import__("os").environ.get("PORT", 8000))
B = f"http://127.0.0.1:{PORT}"
ROOT = __file__.rsplit("/tests/", 1)[0]


# V4.19: every call signs in (HTTP Basic auth, the admin account)
_USER = os.environ.get("PDFUA_TEST_USER", "Kathir")
_PASS = os.environ.get("PDFUA_TEST_PASS", "Kathir@051408")
AUTH = "Basic " + __import__("base64").b64encode(f"{_USER}:{_PASS}".encode()).decode()


def req(method, path, body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(B + path, data=data, method=method,
                               headers={"Content-Type": "application/json", "Authorization": AUTH})
    try:
        with urllib.request.urlopen(r) as resp:
            b = resp.read()
            return b if raw else json.loads(b)
    except urllib.error.HTTPError as e:
        return {"http": e.code, "body": e.read()[:300].decode(errors="replace")}


def upload(paths, fields):
    bnd = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{bnd}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for p in paths:
        parts.append(f'--{bnd}\r\nContent-Disposition: form-data; name="pdf"; filename="{os.path.basename(p)}"\r\n'
                     f'Content-Type: application/pdf\r\n\r\n'.encode() + open(p, 'rb').read() + b"\r\n")
    body = b"".join(parts) + f"--{bnd}--\r\n".encode()
    c = http.client.HTTPConnection("127.0.0.1", PORT)
    c.request("POST", "/upload", body, {"Content-Type": f"multipart/form-data; boundary={bnd}", "Authorization": AUTH})
    r = c.getresponse()
    return r.status, r.getheader("Location")


def wait(jid, stages=("review", "done", "error")):
    s = {}
    for _ in range(400):
        s = req("GET", f"/api/job/{jid}/status")
        if s["stage"] in stages:
            return s
        time.sleep(0.5)
    return s


def main():
    st, loc = upload([f"{ROOT}/tests/corpus/article.pdf"], {"profile": "Default", "options_json": "{}"})
    jid = loc.rsplit("/", 1)[-1]
    print("upload", st, jid)
    s = wait(jid)
    print("stage", s["stage"], "mode", s["mode"], "before", s["score_before"])
    m = req("GET", f"/api/job/{jid}/model")
    N = m["nodes"]
    print("model nodes", len(N), "pages", m["pages"], "can_undo", m["can_undo"])
    lint = req("GET", f"/api/job/{jid}/lint")
    print("lint", [(i["severity"], i["message"][:45]) for i in lint][:4])
    fig = [k for k, v in N.items() if v["role"] == "Figure"][0]
    print("set alt", req("POST", f"/api/job/{jid}/node/{fig}", {"alt": "Bar chart: Q4 highest at 140, Q3 lowest at 60"}))
    author = [k for k, v in N.items() if v["text"].startswith("K. Author")][0]
    print("move down", req("POST", f"/api/job/{jid}/move", {"id": author, "action": "down"}))
    print("undo", req("POST", f"/api/job/{jid}/undo"), "redo", req("POST", f"/api/job/{jid}/redo"),
          "undo", req("POST", f"/api/job/{jid}/undo"))
    note = [v for v in N.values() if v["role"] == "Note"][0]
    r = note["rects"][0]
    print("draw Note", req("POST", f"/api/job/{jid}/create",
                           {"page": 0, "rect": [r[1] - 2, r[2] - 2, r[3] + 2, r[4] + 2], "role": "Note", "absorb": True}))
    tbl = [k for k, v in N.items() if v["role"] == "Table"][0]
    print("table get", {k: v for k, v in req("GET", f"/api/job/{jid}/table/{tbl}").items() if k != "grid"})
    print("table set", req("POST", f"/api/job/{jid}/table/{tbl}", {"header_rows": 2, "header_cols": 1, "mode": "auto"}))
    m = req("GET", f"/api/job/{jid}/model"); N = m["nodes"]; roots = m["roots"]
    ps = [x for x in roots if N[x]["role"] == "P"][:2]
    print("wrap", req("POST", f"/api/job/{jid}/bulk", {"ids": ps, "action": "wrap", "role": "Sect"}))
    m = req("GET", f"/api/job/{jid}/model"); N = m["nodes"]; roots = m["roots"]
    ps = [x for x in roots if N[x]["role"] == "P"][-2:]
    print("make_list", req("POST", f"/api/job/{jid}/bulk", {"ids": ps, "action": "make_list"}))
    print("undo make_list", req("POST", f"/api/job/{jid}/undo"))
    m = req("GET", f"/api/job/{jid}/model"); N = m["nodes"]; roots = m["roots"]
    h1 = [x for x in roots if N[x]["role"] == "H1"][0]
    ab = [x for x in roots if N[x]["text"] == "Abstract"][0]
    print("drop", req("POST", f"/api/job/{jid}/move", {"action": "drop", "ids": [ab], "target": h1, "where": "after"}))
    m = req("GET", f"/api/job/{jid}/model"); N = m["nodes"]; roots = m["roots"]
    two = [x for x in roots if N[x]["page"] == 2 and not N[x]["artifact"]][:2]
    print("reorder", req("POST", f"/api/job/{jid}/reorder", {"page": 2, "ids": list(reversed(two))}))
    m2 = req("GET", f"/api/job/{jid}/model")
    print("  reordered correctly:", [x for x in m2["roots"] if x in two] == list(reversed(two)))
    print("draw Table", req("POST", f"/api/job/{jid}/create",
                            {"page": 1, "rect": N[tbl]["rects"][0][1:5], "role": "Table", "absorb": True}))
    print("doc", req("POST", f"/api/job/{jid}/doc", {"title": "Structure First", "lang": "en-GB"}))
    # the four one-click fixes an imported file usually needs
    print("headings fix", req("POST", f"/api/job/{jid}/headings/fix", {}))
    print("structure fix", req("POST", f"/api/job/{jid}/structure/fix", {}))
    print("blank lines", req("POST", f"/api/job/{jid}/blanklines/fix", {}))
    sp = req("GET", f"/api/job/{jid}/pages/spreads")
    before_pages = req("GET", f"/api/job/{jid}/model")["pages"]
    r_split = req("POST", f"/api/job/{jid}/pages/split", {"page": 0})
    after_pages = req("GET", f"/api/job/{jid}/model")["pages"]
    r_unsplit = req("POST", f"/api/job/{jid}/pages/unsplit", {})
    back_pages = req("GET", f"/api/job/{jid}/model")["pages"]
    print("split page", sp.get("pages"), r_split.get("split"), before_pages, "->", after_pages,
          "-> put back", back_pages, r_unsplit.get("ok"))
    assert after_pages == before_pages + 1 and back_pages == before_pages, "split / put back"
    zs = req("GET", f"/api/job/{jid}/zones/split")
    print("joined word grids", len(zs.get("ids", [])), req("POST", f"/api/job/{jid}/zones/split", {}).get("split"))
    pnum = req("GET", f"/api/job/{jid}/pagenumbers")
    print("page numbers", pnum.get("mode"), pnum.get("to_p"), pnum.get("to_artifact"),
          req("POST", f"/api/job/{jid}/pagenumbers", {"as": "P"}).get("changed"),
          req("POST", f"/api/job/{jid}/pagenumbers", {"as": "artifact"}).get("changed"))
    print("re-read cells", req("POST", f"/api/job/{jid}/cells/reread", {}))
    tl = req("GET", f"/api/job/{jid}/lists/tidy")
    print("lists to tidy", len(tl["stray"]), len(tl["junk"]),
          req("POST", f"/api/job/{jid}/lists/tidy", {}))
    print("empty tag", req("POST", f"/api/job/{jid}/create/empty",
                           {"role": "P", "target": fig, "where": "after", "text": "Blank Line"}))
    print("undo empty tag", req("POST", f"/api/job/{jid}/undo"))
    cl = req("GET", f"/api/job/{jid}/cells/lists")
    print("cells holding a list", len(cl["ids"]),
          req("POST", f"/api/job/{jid}/cells/lists", {}))
    bib = req("GET", f"/api/job/{jid}/bibliography")
    print("reference lists to tag", len(bib.get("sections", [])), "tag them",
          {k: v for k, v in req("POST", f"/api/job/{jid}/bibliography", {}).items() if k != "links"})
    pr = req("GET", f"/api/job/{jid}/prompts")
    print("prompt rows", len(pr.get("ids", [])), req("POST", f"/api/job/{jid}/prompts", {}))
    bm = req("GET", f"/api/job/{jid}/backmatter")
    print("index to tag", len(bm.get("index", [])), "tag it",
          {k: v for k, v in req("POST", f"/api/job/{jid}/backmatter", {}).items() if k != "links"})
    oa = req("POST", f"/api/job/{jid}/order/ai", {"page": 0})
    print("AI reading order without a server:", (oa.get("error") or oa.get("body") or "")[:60])
    mm = req("GET", f"/api/job/{jid}/model")
    tops = [k for k in mm["roots"] if mm["nodes"][k]["page"] == 0 and not mm["nodes"][k]["artifact"]][:2]
    print("apply an order", req("POST", f"/api/job/{jid}/order/apply", {"page": 0, "ids": list(reversed(tops))}),
          "undo", req("POST", f"/api/job/{jid}/undo"))
    m = req("GET", f"/api/job/{jid}/model"); N = m["nodes"]
    para = [v for v in N.values() if v["role"] == "P" and v["page"] == 0 and v["rects"]][0]
    auto = req("POST", f"/api/job/{jid}/create", {"page": 0, "rect": para["rects"][0][1:5],
                                                  "role": "Auto", "absorb": True})
    print("zone key I (auto)", auto.get("role"), auto.get("note", "")[-60:])
    assert auto.get("role"), auto
    print("undo auto zone", req("POST", f"/api/job/{jid}/undo"))
    caps = req("GET", f"/api/job/{jid}/alt/captions")
    print("alt from captions", len(caps["items"]), "offered",
          req("POST", f"/api/job/{jid}/alt/captions", {}))
    m = req("GET", f"/api/job/{jid}/model"); N = m["nodes"]
    cell = [k for k, v in N.items() if v["role"] in ("TD", "TH") and v["text"].strip()][0]
    sug = req("POST", f"/api/job/{jid}/actualtext/suggest", {"ids": [cell]})
    print("read as suggested", [i["suggestion"][:40] for i in sug["items"]])
    print("read as set", req("POST", f"/api/job/{jid}/actualtext",
                             {"ids": [cell], "text": "say this instead", "mode": "same"}))
    print("read as cleared", req("POST", f"/api/job/{jid}/actualtext",
                                 {"ids": [cell], "text": "", "mode": "clear"}))
    t2 = [k for k, v in N.items() if v["role"] == "Table"][0]
    print("table to paragraphs", req("POST", f"/api/job/{jid}/table/{t2}/columns",
                                     {"mode": "paragraphs"}))
    print("undo table convert", req("POST", f"/api/job/{jid}/undo"))
    print("crop", len(req("GET", f"/job/{jid}/crop/{fig}.png", raw=True)), "bytes")
    print("page png", len(req("GET", f"/job/{jid}/page/0.png?z=1", raw=True)), "bytes")
    print("ai without server", req("POST", f"/api/job/{jid}/ai/alt/{fig}"))
    print("build", req("POST", f"/api/job/{jid}/build", {"options": {"bookmarks": "headings"}}))
    time.sleep(0.5)
    s = wait(jid, ("done", "error"))
    print("built", s["stage"], s["score_before"]["errors"], "->", (s.get("score_after") or {}).get("errors"), s.get("stats"))
    if s["stage"] == "error":
        print(s.get("error"))
    for w in ("pdf", "report", "model", "working", "original"):
        print("  download", w, len(req("GET", f"/job/{jid}/download/{w}", raw=True)))
    f = req("GET", f"/api/job/{jid}/findings")
    print("remaining errors", [x["code"] for x in f["after"] if x["severity"] == "error"])
    with open(os.environ.get("JID_OUT", "/tmp/jid.txt"), "w") as fh:
        fh.write(jid)

    st, loc = upload([f"{ROOT}/tests/corpus/book.pdf", f"{ROOT}/tests/corpus/forms.pdf"],
                     {"profile": "Default", "options_json": "{}", "batch_auto": "on"})
    batch = loc.split("batch=")[-1]
    print("batch upload", st, batch)
    jobs = []
    for _ in range(200):
        jobs = req("GET", f"/api/jobs?batch={batch}")
        if jobs and all(j["stage"] in ("done", "error") for j in jobs):
            break
        time.sleep(0.5)
    print("batch jobs", [(j["filename"], j["stage"], (j["score_after"] or {}).get("errors")) for j in jobs])
    print("batch summary xlsx", len(req("GET", f"/batch/{batch}.xlsx", raw=True)), "bytes")
    pr = req("POST", "/api/profiles", {"name": "Apex books", "options": {"detect_toc": True, "bookmark_levels": 2}})
    print("profiles", list(pr["profiles"].keys()))
    print("settings", req("POST", "/api/settings", {"verapdf": "", "ai": {"enabled": True, "timeout": 600}}))
    cfg = req("GET", "/api/settings")
    print("site defaults", cfg["ai"]["enabled"], cfg["ai"]["timeout"], cfg.get("parallel"),
          cfg.get("default_profile"))


if __name__ == "__main__":
    main()
