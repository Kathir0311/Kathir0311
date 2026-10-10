"""Marks (V4.19.1): Ctrl+N on a table or list marks it #N for this file; the
next ones clicked (or a zone drawn) are read again and tagged the same way;
Esc lets the mark go. Through the app, with Flask's test client.

* a cell of the first table is selected and marked #4 (the table's structure
  is read, not the cell's)
* the untagged table on page 2 (a zone) is given #4: 3 columns, the header
  row as TH, the words in the right cells
* a list item marked #2 makes the list the mark; the second list gets it
* letting #4 go leaves no mark; applying it again is refused
"""
from __future__ import annotations

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

import pymupdf  # noqa: E402


def wait(c, jid):
    for _ in range(240):
        st = c.get(f"/api/job/{jid}/status").json.get("stage")
        if st in ("review", "done", "error"):
            return st
        time.sleep(0.5)
    return "timeout"


def main() -> int:
    import app as A
    from tests.list_edit_test import make as make_lists
    from tests.table_template_test import make as make_tables
    bad: list = []
    c = A.app.test_client()
    c.post("/login", data={"username": "Kathir", "password": "Kathir@051408"})
    src = os.path.join(TMP, "t.pdf")
    make_tables(src)
    r = c.post("/upload", data={"profile": "Default", "pdf": [(open(src, "rb"), "t.pdf")]},
               content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
    jid = r.json["jobs"][0]
    if wait(c, jid) != "review":
        print("marks test: FAILED — the file was not analysed")
        return 1
    m = c.get(f"/api/job/{jid}/model").json
    t1 = next(n for n in m["nodes"].values() if n["role"] == "Table" and n["page"] == 0)
    cell = next(n for n in m["nodes"].values() if n["role"] == "TD" and n["page"] == 0)
    r = c.post(f"/api/job/{jid}/marks", json={"id": cell["id"], "number": 4})
    if not r.json.get("ok") or r.json.get("kind") != "table":
        bad.append(f"mark #4: {r.json}")
    elif r.json["source"] not in (t1["id"],) and m["nodes"][r.json["source"]]["role"] != "Table":
        bad.append(f"the mark is not the table: {r.json['source']}")
    d = pymupdf.open(src)
    lee, hull = d[1].search_for("Leeds")[0], d[1].search_for("Hull")[0]
    d.close()
    zone = [36, lee.y0 - 26, 40 + 3 * 70, hull.y1 + 6]
    r = c.post(f"/api/job/{jid}/templates/apply", json={"kind": "table", "number": 4, "mark": True, "page": 1, "rect": zone})
    if not r.json.get("ok") or (r.json.get("rows"), r.json.get("cols")) != (4, 3):
        bad.append(f"apply #4 to a zone: {r.json}")
    else:
        v = c.get(f"/api/job/{jid}/table/{r.json['id']}/grid").json
        t = {(x["r"], x["c"]): (x["text"], x["role"]) for x in v["cells"]}
        if t.get((0, 0)) != ("Group", "TH") or t.get((1, 0), ("",))[0] != "Leeds" or t.get((3, 2), ("",))[0] != "3.8":
            bad.append(f"#4 on page 2: {t}")
    c.delete(f"/api/job/{jid}/marks", json={"number": 4})
    if c.get(f"/api/job/{jid}/marks").json.get("table"):
        bad.append("Esc left the mark")
    r = c.post(f"/api/job/{jid}/templates/apply", json={"kind": "table", "number": 4, "mark": True, "page": 1, "rect": zone})
    if r.status_code != 400:
        bad.append("a released mark was applied")

    # lists
    src2 = os.path.join(TMP, "l.pdf")
    make_lists(src2)
    r = c.post("/upload", data={"profile": "Default", "pdf": [(open(src2, "rb"), "l.pdf")]},
               content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
    j2 = r.json["jobs"][0]
    wait(c, j2)
    m = c.get(f"/api/job/{j2}/model").json
    tops = [n for n in m["nodes"].values() if n["role"] == "L" and not any(
        m["nodes"].get(a, {}).get("role") == "L" for a in [n["parent"]] if a)]
    li = next(n for n in m["nodes"].values() if n["role"] == "LI")
    r = c.post(f"/api/job/{j2}/marks", json={"id": li["id"], "number": 2})
    if r.json.get("kind") != "list":
        bad.append(f"list mark: {r.json}")
    if len(tops) >= 2:
        last = sorted(tops, key=lambda n: ((n.get("rects") or [[n["page"], 0, 0]])[0][0],
                                            (n.get("rects") or [[0, 0, 0]])[0][2]))[-1]
        r = c.post(f"/api/job/{j2}/templates/apply", json={"kind": "list", "number": 2, "mark": True, "id": last["id"]})
        if not r.json.get("ok"):
            bad.append(f"list #2 applied: {r.json}")
    if bad:
        print("marks test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("marks test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
