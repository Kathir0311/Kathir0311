"""V4.31 — Ollama teaches Dez AI, against a stand-in Ollama whose answers follow rules the students can find.

* the gate: a question Ollama answered is a lesson; asked again (even with other words around it) Dez AI answers it
  itself; a new one goes to Ollama; the switches (lessons / answers alone / when Ollama is off) and the level of a
  task are obeyed
* a student for yes / no and choice questions (is row 1 a header? does the table go on? a list's item starts, a
  reference list's entry starts) learns the stand-in's rule from its answers, is proven on questions it had not
  seen, and then answers new ones alone — with 1 in N asked to Ollama as well (the audit); a student whose audits
  go wrong is taken off duty
* a rule for the label of a form field (the text to the left)
* the same picture gets the same alt text, also at another size; another picture does not
* people outrank the teacher: what a person left in a file is a lesson of its own, the lessons it confirms are
  marked, the ones it changes are never used again
* Ollama's tags for the brain's doubtful blocks are kept with their layout, train the model with a low weight until
  a person has judged them, and stop training once judged
* Ollama off: Dez AI answers what it learned, the rest stays unanswered
* the admin's page: status, settings (checked), study, forget, lessons, read the finished files; admins only
* the app's AI helpers say who answered; many processes write lessons at the same time without losing one
"""
from __future__ import annotations

import json
import os
import re
import subprocess
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
os.environ["PDFUA_NO_TEACHER_THREAD"] = "1"
PORT = 11561

from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402

CALLS: list[str] = []
PROMPTS: list[str] = []


def kind_of(pr: str, has_images: bool) -> str:
    for k, needle in (("header", "Does row 1 contain column headers"), ("link", "needs a description that makes sense"),
                      ("field", "no tooltip (/TU)"), ("bib", "lines of a reference list"), ("list", "printed lines of a list"),
                      ("continues", "CONTINUATION of the table"), ("summary", "Summarise what a data table holds"),
                      ("merge", "really one")):
        if needle in pr:
            return k
    return "alt" if has_images else "other"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send(200, {"models": [{"name": "llama3.2:latest", "size": 2e9}, {"name": "qwen2.5vl:7b", "size": 6e9}]})
        else:
            self._send(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/show":
            self._send(200, {"capabilities": ["completion", "vision"] if "vl" in str(body.get("model")) else ["completion"]})
            return
        if self.path != "/api/generate":
            self._send(404, {})
            return
        pr = body.get("prompt", "")
        imgs = body.get("images")
        k = kind_of(pr, bool(imgs))
        CALLS.append(k)
        PROMPTS.append(pr)
        if k == "header":
            r1 = pr.split("Row 1:")[1].split("\n")[0]
            r2 = pr.split("Row 2:")[1].split("\n")[0]
            self._send(200, {"response": json.dumps({"header": (not re.search(r"\d", r1)) and bool(re.search(r"\d", r2))})})
        elif k == "link":
            dest = pr.split("Where it goes:")[1].split("\n")[0].strip()
            self._send(200, {"response": "About " + re.sub(r"^https?://", "", dest)[:40]})
        elif k == "field":
            left = pr.split("Text to the left:")[1].split("\n")[0].strip()
            above = pr.split("Text above:")[1].split("\n")[0].strip()
            self._send(200, {"response": (left if left != "none" else above if above != "none" else "Field").rstrip(":")})
        elif k in ("bib", "list"):
            rows = [r.split(": ", 1)[-1] for r in pr.splitlines() if r[:1].isdigit()]
            pat = r"[A-Z][\w'’]+, [A-Z]\." if k == "bib" else r"(\d+[.)]|[•\-*]) "
            self._send(200, {"response": json.dumps({"starts": [i + 1 for i, r in enumerate(rows) if re.match(pat, r)]})})
        elif k == "continues":
            a = pr.split("PAGE A column header:")[1].split("\n")[0].strip()
            b = pr.split("PAGE B column header:")[1].split("\n")[0].strip()
            self._send(200, {"response": json.dumps({"same_table": a == b, "why": "same header" if a == b else "other header"})})
        elif k == "summary":
            cap = pr.split("Caption or heading:")[1].split("\n")[0].strip()
            self._send(200, {"response": f"A table of {cap[:50]}"})
        elif k == "merge":
            self._send(200, {"response": json.dumps({"same": True, "repeat_header": False, "why": "same columns"})})
        elif k == "alt":
            import hashlib
            self._send(200, {"response": "Picture " + hashlib.md5(str(imgs).encode()).hexdigest()[:6]})
        else:
            self._send(200, {"response": "[]"})


def serve():
    srv = HTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def png_of(draw, w=200, h=120, scale=1.0) -> bytes:
    import pymupdf
    d = pymupdf.open()
    p = d.new_page(width=w, height=h)
    draw(p, w, h)
    return p.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")


def logo(p, w, h):
    p.draw_rect(pymupdf.Rect(10, 10, 110, 60), color=(0, 0, 0), fill=(0.1, 0.2, 0.7))
    p.draw_circle((150, 80), 25, color=(0, 0, 0), fill=(0.9, 0.2, 0.1))
    p.insert_text((14, 100), "ACME", fontsize=14)


def chart(p, w, h):
    for i in range(6):
        p.draw_rect(pymupdf.Rect(15 + i * 28, h - 12 - (i * 13 + 10), 35 + i * 28, h - 12), color=(0, 0, 0), fill=(0.2, 0.6, 0.3))
    p.draw_line((10, 10), (10, h - 10))


import pymupdf  # noqa: E402


def main() -> int:
    srv = serve()
    with open(os.environ["PDFUA_CONFIG"], "w") as fh:
        json.dump({"ai": {"base_url": f"http://127.0.0.1:{PORT}", "enabled": True, "text_model": "llama3.2:latest",
                          "vision_model": "qwen2.5vl:7b", "timeout": 60}, "site_defaults": 3,
                   "teacher": {"audit": 0, "min_lessons": 30}}, fh)
    from core import ai, teacher
    from core.ai import AIConfig
    bad: list = []
    notes: list = []

    def ok(cond, msg):
        if not cond:
            bad.append(msg)

    def settings(**kw):
        with open(os.environ["PDFUA_CONFIG"]) as fh:
            cfg = json.load(fh)
        cfg["teacher"] = {**cfg.get("teacher", {}), **kw}
        with open(os.environ["PDFUA_CONFIG"], "w") as fh:
            json.dump(cfg, fh)
        teacher.settings(fresh=True)

    cfg = AIConfig(base_url=f"http://127.0.0.1:{PORT}", enabled=True, text_model="llama3.2:latest",
                   vision_model="qwen2.5vl:7b", timeout=30)
    off = AIConfig(base_url=f"http://127.0.0.1:{PORT}", enabled=False, text_model="llama3.2:latest")

    def count(kind):
        return sum(1 for c in CALLS if c == kind)

    # ------------------------------------------------------------------ 1. the gate: a lesson, memory, a new question
    ok(all(hasattr(getattr(ai, f), "dez_task") for f in teacher.GATED_FUNCTIONS.values()), "every task function is behind the gate")
    a1 = ai.link_description(cfg, "click here", "https://x.test/guide.pdf", "Read the guide.")
    n1 = count("link")
    a2 = ai.link_description(cfg, "click here", "https://x.test/guide.pdf", "Another sentence around it.")
    ok(a1 == a2 == "About x.test/guide.pdf" and n1 == 1 and count("link") == 1, f"a question asked again is answered from the lesson ({a1!r}, {a2!r}, calls {count('link')})")
    a3 = ai.link_description(cfg, "click here", "https://y.test/other.pdf", "")
    ok(a3 == "About y.test/other.pdf" and count("link") == 2, "a new question goes to Ollama")
    st = teacher.status()
    row = next(t for t in st["tasks"] if t["task"] == "links")
    ok(row["lessons"] == 2 and row["dez"] == 1 and row["ollama"] == 2 and row["level"] == "recall", f"counted: {row}")
    ok(st["totals"]["lessons"] == 2 and st["totals"]["alone"] == round(1 / 3, 3), f"totals {st['totals']}")

    # the switches
    settings(solo=False)
    ai.link_description(cfg, "click here", "https://x.test/guide.pdf", "")
    ok(count("link") == 3, "answers alone off: Ollama is asked again")
    settings(solo=True, teach=False)
    ai.link_description(cfg, "click here", "https://z.test/new.pdf", "")
    ok(teacher.status()["tasks"][[t["task"] for t in teacher.status()["tasks"]].index("links")]["lessons"] == 2, "lessons off: nothing is kept")
    settings(teach=True, levels={"links": "off"})
    ai.link_description(cfg, "click here", "https://x.test/guide.pdf", "")
    ok(count("link") == 5, "level off: nothing is answered or kept for this task")
    settings(levels={"links": "watch"})
    ai.link_description(cfg, "click here", "https://x.test/guide.pdf", "")
    ok(count("link") == 6, "level watch: Ollama answers, the answer is only kept")
    settings(levels={})
    before = count("link")
    ai.link_description(cfg, "click here", "https://x.test/guide.pdf", "")
    ok(count("link") == before, "automatic again: the lesson answers")
    # the direct door
    ai.link_description(cfg, "click here", "https://x.test/guide.pdf", "", _direct=True)
    ok(count("link") == before + 1, "_direct asks Ollama only")

    # ------------------------------------------------------------------ 2. a student: is row 1 a header?
    import random
    rnd = random.Random(7)
    words = ["Name", "Year", "Total", "Region", "Score", "Item", "Class", "Grade", "Unit", "Price", "Date", "Notes"]

    def table_q():
        if rnd.random() < 0.5:
            return rnd.sample(words, 3), [str(rnd.randint(1, 99)) for _ in range(3)]
        return [str(rnd.randint(1, 99)) for _ in range(3)], [str(rnd.randint(1, 99)) for _ in range(3)]

    def want(r1, r2):
        return (not re.search(r"\d", " ".join(r1))) and bool(re.search(r"\d", " ".join(r2)))
    for _ in range(70):
        r1, r2 = table_q()
        ai.table_has_header(cfg, r1, r2)
    ok(teacher.level_of("table_headers") == "recall", f"70 lessons are not a proven student yet: {teacher.level_of('table_headers')}")
    r = teacher.train_all()
    t0 = next((x for x in r["tasks"] if x["task"] == "table_headers"), {})
    ok(t0.get("ok") and t0.get("ready") and t0.get("acc", 0) >= 0.95 and t0.get("n", 0) >= 60, f"the student is proven on unseen questions: {t0}")
    ok(teacher.level_of("table_headers") == "solo", "level solo")
    n0 = count("header")
    good = 0
    for _ in range(40):
        r1, r2 = table_q()
        good += ai.table_has_header(cfg, r1, r2) == want(r1, r2)
    ok(good >= 39 and count("header") - n0 <= 2, f"new questions answered alone: {good}/40 right, {count('header') - n0} asked")
    # the audit: 1 in 2 is asked to Ollama too and compared
    settings(audit=2)
    n0 = count("header")
    for _ in range(30):
        r1, r2 = table_q()
        ai.table_has_header(cfg, r1, r2)
    aud = teacher.audit_stats("table_headers", fresh=True)
    ok(5 <= count("header") - n0 <= 25 and aud["n"] >= 5 and aud["agree"] >= 0.95, f"audits: {count('header') - n0} asked, {aud}")
    # a student that is caught out is taken off duty
    with teacher._db() as c:
        for i in range(30):
            c.execute("INSERT INTO teach_audit (task, day, agree, dez, ollama, conf, created) VALUES (?,?,?,?,?,?,?)",
                      ("table_headers", teacher._day(), 0, "true", "false", 0.95, teacher._now()))
    teacher.audit_stats("table_headers", fresh=True)
    ok(teacher.level_of("table_headers") == "recall", "a student whose audits disagree is taken off duty")
    with teacher._db() as c:
        c.execute("DELETE FROM teach_audit WHERE task='table_headers' AND agree=0")
    teacher.audit_stats("table_headers", fresh=True)
    ok(teacher.level_of("table_headers") == "solo", "…and is back once the audits agree again")
    settings(audit=0)
    # an admin can hold it back or push it
    settings(levels={"table_headers": "recall"})
    n0 = count("header")
    r1, r2 = table_q()
    ai.table_has_header(cfg, r1, r2)
    ok(count("header") == n0 + 1, "an admin can keep a task at recall")
    settings(levels={})

    # ------------------------------------------------------------------ 3. the label of a form field: a rule
    for i in range(45):
        left = rnd.choice(["First name", "Surname", "Date of birth", "Town", "Postcode", "Email address", "Phone", "School"]) + rnd.choice(["", ":", " :"])
        above = rnd.choice(["Section A", "Section B", "Pupil details"])
        if i % 3 == 0:
            ai.field_label(cfg, "text field", f"T{i}", left=left, right="", above=above)
        elif i % 3 == 1:
            ai.field_label(cfg, "text field", f"T{i}", left="", right="", above=above + f" {i}")
        else:
            ai.field_label(cfg, "text field", f"T{i}", left=left + f" {i}", right="", above="")
    r = teacher.train_all()
    f0 = next((x for x in r["tasks"] if x["task"] == "fields"), {})
    ok(f0.get("ok") and f0.get("ready") and f0.get("acc", 0) >= 0.95, f"the rule for field labels is proven: {f0}")
    n0 = count("field")
    got = ai.field_label(cfg, "text field", "Q99", left="Class teacher :", right="", above="Section Z")
    ok(got == "Class teacher" and count("field") == n0, f"a new field is labelled with the text to its left, alone: {got!r}")
    got = ai.field_label(cfg, "text field", "Q98", left="", right="", above="Part 7")
    ok(got == "Part 7" and count("field") == n0, f"…and with the text above when nothing is to the left: {got!r}")

    # ------------------------------------------------------------------ 4. lines: where an entry starts
    def bib_lines():
        n = rnd.randint(5, 9)
        out = []
        for _ in range(n):
            out.append(f"{rnd.choice(['Adams', 'Brown', 'Chen', 'Davis', 'Evans', 'Foster', 'Garcia'])}, {rnd.choice('ABCDEFGHJKLM')}. {rnd.choice('NPQRST')}. ({rnd.randint(1990, 2022)}).")
            for _ in range(rnd.randint(0, 2)):
                out.append(rnd.choice(["a study of reading and writing in the early years. Journal of Education, 12(3),",
                                       "pp. 45-67. Publisher Press, London.", "retrieved from the national archive of records."]))
        return out
    for _ in range(40):
        ai.bib_entry_starts(cfg, bib_lines())
    r = teacher.train_all()
    b0 = next((x for x in r["tasks"] if x["task"] == "bibliography"), {})
    ok(b0.get("ok") and b0.get("ready"), f"the student for reference lists is proven: {b0}")
    n0 = count("bib")
    right = 0
    for _ in range(10):
        ls = bib_lines()
        right += ai.bib_entry_starts(cfg, ls) == [i for i, x in enumerate(ls) if re.match(r"[A-Z][\w'’]+, [A-Z]\.", x)]
    ok(right >= 9 and count("bib") - n0 <= 1, f"new reference lists split alone: {right}/10 right, {count('bib') - n0} asked")

    # ------------------------------------------------------------------ 5. the table goes on over the page break
    def ctx():
        same = rnd.random() < 0.5
        ha = " | ".join(rnd.sample(words, 3))
        return {"a_title": rnd.choice(["Table 1", "Scores", ""]), "a_head": ha, "a_rows": ["1 | 2 | 3"], "a_trail": "",
                "b_title": "" if same else rnd.choice(["Table 2", "Results", "Other"]), "b_head": ha if same else " | ".join(rnd.sample(words, 3)),
                "b_rows": ["4 | 5 | 6"], "hints": f"score {rnd.randint(-2, 2)} (x); pages 1 and 2; titles: {'same' if same else 'different'}"}
    for _ in range(70):
        ai.table_continues(cfg, ctx())
    r = teacher.train_all()
    m0 = next((x for x in r["tasks"] if x["task"] == "table_merge"), {})
    ok(m0.get("ok") and m0.get("ready"), f"the student for 'the table goes on' is proven: {m0}")
    n0 = count("continues")
    right = 0
    for _ in range(20):
        c_ = ctx()
        a_ = ai.table_continues(cfg, c_)
        right += bool(a_ and a_["same"]) == (c_["a_head"] == c_["b_head"])
    ok(right >= 19 and count("continues") - n0 <= 2, f"new tables: {right}/20 right, {count('continues') - n0} asked")

    # ------------------------------------------------------------------ 6. pictures
    a = ai.alt_text(cfg, png_of(logo), "page 1")
    n_alt = count("alt")
    ok(a.startswith("Picture ") and n_alt == 1, f"a picture is described by Ollama once: {a!r}")
    b = ai.alt_text(cfg, png_of(logo), "another page")
    ok(b == a and count("alt") == 1, "the same picture is not described again")
    c_ = ai.alt_text(cfg, png_of(logo, scale=2.0), "bigger")
    ok(c_ == a and count("alt") == 1, "…also at another size")
    d_ = ai.alt_text(cfg, png_of(chart), "")
    ok(d_ != a and count("alt") == 2, "another picture is described by Ollama")
    ai.alt_text(cfg, b"\x89PNG not a picture", "")
    ok(count("alt") == 3, "something that is not a picture is just passed on")

    # ------------------------------------------------------------------ 7. Ollama off: Dez AI answers what it learned
    n0 = len(CALLS)
    ok(ai.link_description(off, "click here", "https://x.test/guide.pdf", "") == "About x.test/guide.pdf", "Ollama off: a learned answer is given")
    ok(ai.link_description(off, "click here", "https://never.test/asked", "") == "", "…an unlearned one stays unanswered")
    ok(ai.table_has_header(off, *table_q()) in (True, False), "…a proven student answers")
    ok(ai.alt_text(off, png_of(logo), "") == a, "…a known picture is described")
    ok(ai.xref_match(off, "Section 9", "see Section 9", ["Section 9 X"]) is None, "…an unlearned reference is not guessed")
    ok(len(CALLS) == n0, "…and Ollama was not asked")
    settings(offline=False)
    ok(ai.link_description(off, "click here", "https://x.test/guide.pdf", "") == "", "Ollama off and the admin said no: nothing")
    settings(offline=True)
    ok(teacher.offline_knows(), "offline_knows")

    # ------------------------------------------------------------------ 8. people outrank the teacher
    from core.analyzer import analyze
    from core.options import Options
    two = os.path.join(TMP, "two_tables.pdf")
    d_ = pymupdf.open()
    xs = [40, 190, 340, 490]
    for title, head, body in (("Table 1. Scores of the skill sets", ["Skill", "Task", "Score"], [["Reading", "Fluency", "12"], ["Writing", "Spelling", "9"], ["Maths", "Number", "15"]]),
                              ("Table 2. The budget for the year", ["Item", "Cost", "Notes"], [["Books", "400", "paid"], ["Paper", "120", "due"], ["Pens", "60", "paid"]])):
        pg = d_.new_page(width=595, height=842)
        pg.insert_text((40, 80), title, fontsize=11, fontname="hebo")
        for r_, row in enumerate([head] + body):
            for c_, txt in enumerate(row):
                rect = pymupdf.Rect(xs[c_], 100 + r_ * 30, xs[c_ + 1], 130 + r_ * 30)
                pg.draw_rect(rect, color=(0, 0, 0), width=0.6)
                pg.insert_textbox(rect + (4, 4, -4, -4), txt, fontsize=9, fontname="hebo" if r_ == 0 else "helv")
        pg.insert_text((40, 810), "footer text of the page", fontsize=8)
    d_.save(two)
    d_.close()
    m = analyze(two, Options())
    tables = [n for n in m.ordered_nodes() if n.role == "Table"]
    ok(len(tables) >= 2, f"tables in the corpus file ({len(tables)})")
    t_a, t_b = tables[0], tables[1]
    items = {id(x): x for x in teacher.harvest(m)}
    cap_a = cap_b = ""
    # Ollama summarised both
    rows_a, rows_b = teacher._table_rows(m, t_a), teacher._table_rows(m, t_b)
    for t_, rows_ in ((t_a, rows_a), (t_b, rows_b)):
        cap = next((m.nodes[c].text for c in t_.children if m.nodes[c].role == "Caption"), "")
        flat = [" | ".join(x[:40] for x in r_) for r_ in rows_[:6]]
        t_._flat, t_._cap = flat, cap
        t_._ans = ai.table_summary(cfg, cap, flat[0].split(" | "), flat[1:4])
    t_a.alt = t_a._ans                                           # a person kept Ollama's summary
    t_b.alt = "Scores for each skill set, one row per form"      # a person wrote their own
    res = teacher.learn_job(os.path.join(TMP, "j1"), "Client X", m, None, verified=True)
    ok(res["human"] >= 2 and res["confirmed"] >= 1 and res["corrected"] >= 1, f"human lessons: {res}")
    n0 = count("summary")
    got_a = ai.table_summary(cfg, t_a._cap, t_a._flat[0].split(" | "), t_a._flat[1:4])
    got_b = ai.table_summary(cfg, t_b._cap, t_b._flat[0].split(" | "), t_b._flat[1:4])
    ok(got_a == t_a.alt and got_b == t_b.alt and count("summary") == n0, f"the person's words are used, Ollama is not asked: {got_a!r} {got_b!r}")
    with teacher._db() as c:
        vs = [r["verdict"] for r in c.execute("SELECT verdict FROM lessons WHERE task='table_summary' AND source='ollama'")]
    ok(sorted(vs) == [-1, 1], f"Ollama's two lessons: one confirmed, one changed: {vs}")
    # header rows: what the file says is a lesson (an approved file: every table)
    ok(any(x[0] == "table_headers" for x in teacher.harvest(m)), "the header rows of the tables are harvestable")

    # ------------------------------------------------------------------ 9. the brain's tags
    from core import brain, learn
    items = [{"node": f"n{i}", "text": f"SECTION {i}", "f": {"size_rel": 1.0, "bold": 0, "caps": 1.0, "words": 2, "lines": 1, "font": "helv"},
              "proposed": "P", "role": "H2", "conf": 0.9, "accepted": i < 4, "why": "capitals"} for i in range(6)]
    ok(teacher.roles_taught("Journal X", "jobR", "journal_article", items) == 6, "the tags are kept")
    with learn.connect() as c:
        rows = teacher.role_rows(c)
        ok(len(rows) == 4 and all(r[2] == "H2" and 0 < r[3] <= 0.35 for r in rows), f"only the tags the brain went along with train, with little weight: {len(rows)}")
        tr = brain.training_rows(c)
        ok(sum(1 for r in tr if str(r[4]).startswith("tr:")) == 4, "the brain's training rows include them")
    teacher.roles_judged("jobR", {"n0": "H2", "n1": "H3", "n2": "H2", "n3": "H2"}, verified=False)
    with learn.connect() as c:
        v = {r["node"]: r["verdict"] for r in c.execute("SELECT node, verdict FROM teacher_roles WHERE job='jobR'")}
        ok(v["n1"] == -1 and v["n0"] == 0, f"a built file: a changed tag is wrong, silence is not consent {v}")
    teacher.roles_judged("jobR", {"n0": "H2", "n2": "H2", "n3": "H2"}, verified=True)
    with learn.connect() as c:
        v = {r["node"]: r["verdict"] for r in c.execute("SELECT node, verdict FROM teacher_roles WHERE job='jobR'")}
        ok(v["n0"] == 1 and v["n2"] == 1 and teacher.role_rows(c) == [], f"an approved file confirms, and judged tags no longer train by themselves {v}")

    # ------------------------------------------------------------------ 10. forget, lessons, prune, settings
    ok(len(teacher.lessons_list("links", 10)) >= 2, "the lessons can be listed")
    ok(teacher.forget("links", "corrected") == 0, "nothing was changed in the links")
    n_l = teacher.forget("links", "all")
    ok(n_l >= 2 and teacher.status()["tasks"][[t["task"] for t in teacher.status()["tasks"]].index("links")]["lessons"] == 0, f"forgotten {n_l}")
    s_ = teacher.clean_settings({"audit": 7, "agree": 90, "sure": 0.8, "min_lessons": 12, "levels": {"links": "solo", "nope": "x", "xref": "auto"}, "junk": 1}, {})
    ok(s_ == {"audit": 7, "agree": 0.9, "sure": 0.8, "min_lessons": 12, "levels": {"links": "solo"}}, f"settings are checked: {s_}")
    old_cap = teacher.LESSON_CAP
    teacher.LESSON_CAP = 10
    teacher.prune()
    with teacher._db() as c:
        n_hum = c.execute("SELECT COUNT(*) n FROM lessons WHERE source='human'").fetchone()["n"]
        n_tot = c.execute("SELECT COUNT(*) n FROM lessons WHERE task='table_headers'").fetchone()["n"]
    teacher.LESSON_CAP = old_cap
    ok(n_tot <= 10 and n_hum >= 2, f"the cap forgets the oldest unconfirmed ({n_tot}), never a person's ({n_hum})")

    # ------------------------------------------------------------------ 10b. Dez AI shows Ollama how people here worded things
    ai.link_description(cfg, "click here", "https://shop.test/a", "")
    ok("Examples of what people" not in PROMPTS[-1], "no examples while there are fewer than two approved answers")
    for i, (txt, dst, ans) in enumerate([("here", "https://shop.test/catalogue.pdf", "Catalogue of the shop (PDF)"),
                                         ("read more", "https://shop.test/returns", "Returns policy of the shop"),
                                         ("link", "https://other.org/x", "Something else entirely")]):
        teacher.record("links", teacher.key_of(txt, dst), {"text": txt, "dest": dst, "ctx": ""}, ans, source="human", verdict=1)
    ai.link_description(cfg, "click here", "https://shop.test/prices.pdf", "")
    last = PROMPTS[-1]
    ok("Examples of what people in this studio approved" in last and "Catalogue of the shop (PDF)" in last and "Returns policy of the shop" in last,
       f"the question carries the answers people approved for similar links: {last[-400:]!r}")
    settings(fewshot=False)
    ai.link_description(cfg, "click here", "https://shop.test/prices2.pdf", "")
    ok("Examples of what people" not in PROMPTS[-1], "the examples can be switched off")
    settings(fewshot=True)

    # ------------------------------------------------------------------ 11. many processes write lessons at the same time
    code = ("import sys, os; sys.path.insert(0, %r); from core import teacher; "
            "[teacher.record('links', 'k%%s_%%d' %% (sys.argv[1], i), {'text': 'x'}, 'answer %%d' %% i) for i in range(40)]; teacher.flush_counts()" % ROOT)
    ps = [subprocess.Popen([sys.executable, "-c", code, str(k)], env=dict(os.environ)) for k in range(4)]
    [p.wait(timeout=120) for p in ps]
    with teacher._db() as c:
        n_c = c.execute("SELECT COUNT(*) n FROM lessons WHERE task='links' AND qkey LIKE 'k%'").fetchone()["n"]
    ok(all(p.returncode == 0 for p in ps) and n_c == 160, f"four processes wrote 160 lessons together: {n_c}")

    # ------------------------------------------------------------------ 12. the app
    import app as A
    k = A.app.test_client()
    ok(k.get("/api/admin/teacher").status_code in (302, 401, 403), "the admin's page needs a sign-in")
    k.post("/login", data={"username": "Kathir", "password": "Kathir@051408"})
    d = k.get("/api/admin/teacher").json
    ok(len(d["tasks"]) == len(teacher.SPECS) and d["settings"]["teach"] and d["ollama"] and "totals" in d and d["levels"], f"status {list(d)}")
    r = k.post("/api/admin/teacher", json={"audit": 5, "agree": 97, "levels": {"links": "recall"}})
    ok(r.status_code == 200 and r.json["settings"]["audit"] == 5 and abs(r.json["settings"]["agree"] - 0.97) < 1e-9
       and r.json["settings"]["levels"] == {"links": "recall"}, f"settings saved {r.json.get('settings')}")
    r = k.post("/api/admin/teacher", json={"audit": "many"})
    ok(r.status_code == 400, f"a bad number is refused ({r.status_code})")
    k.post("/api/admin/teacher", json={"audit": 0, "levels": {"links": "auto"}})
    ok(k.post("/api/admin/teacher/forget", json={"task": "xref", "what": "junk"}).status_code == 400, "forget: a bad choice is refused")
    ok(k.get("/api/admin/teacher/lessons?task=table_headers").json["lessons"], "lessons of a task")
    r = k.post("/api/admin/teacher/train", json={})
    ok(r.json.get("started"), "study now starts")
    for _ in range(240):
        d = k.get("/api/admin/teacher").json
        if not d["running"]:
            break
        time.sleep(0.5)
    ok(not d["running"] and (d["job"]["result"] or {}).get("tasks") is not None, f"study finished {d['job']}")
    # a production user may not
    k.post("/api/admin/users", json={"username": "pat", "password": "Pat@12345678", "role": "production"})
    k2 = A.app.test_client()
    k2.post("/login", data={"username": "pat", "password": "Pat@12345678"})
    ok(k2.get("/api/admin/teacher").status_code in (401, 403), "a production user cannot read the teacher's page")
    ok(k2.post("/api/admin/teacher/forget", json={"task": "links"}).status_code in (401, 403), "…or forget lessons")

    # the editor's helpers say who answered
    with open(os.path.join(ROOT, "tests", "corpus", "tables.pdf"), "rb") as fh:
        up = k.post("/upload", data={"profile": "Default", "pdf": [(fh, "tables.pdf")]}, content_type="multipart/form-data",
                    headers={"X-Requested-With": "fetch"})
    jid = up.json["jobs"][0]
    for _ in range(300):
        if k.get(f"/api/job/{jid}/status").json.get("stage") == "review":
            break
        time.sleep(0.4)
    n0 = count("summary")
    r1 = k.post(f"/api/job/{jid}/ai/tables", json={}).json
    n1 = count("summary")
    r2 = k.post(f"/api/job/{jid}/ai/tables", json={}).json
    ok(r1.get("suggestions") and r1["by"]["ollama"] >= 1, f"the first time Ollama answers: {r1.get('by')}")
    ok(r2["by"]["ollama"] == 0 and r2["by"]["dez"] == r1["by"]["ollama"] + r1["by"]["dez"] and count("summary") == n1,
       f"the second time Dez AI answers by itself: {r2.get('by')}")
    ok(r1["suggestions"] == r2["suggestions"], "…with the same words")
    with teacher._db() as c:
        who = c.execute("SELECT job, client FROM lessons WHERE task='table_summary' AND job=?", (jid,)).fetchone()
    ok(who is not None and who["client"], f"the lesson knows its file and client: {dict(who) if who else None}")
    # Ollama switched off in Settings: the learned answers still come, the rest is said clearly
    with open(os.environ["PDFUA_CONFIG"]) as fh:
        full = json.load(fh)
    full["ai"]["enabled"] = False
    with open(os.environ["PDFUA_CONFIG"], "w") as fh:
        json.dump(full, fh)
    r3 = k.post(f"/api/job/{jid}/ai/tables", json={}).json
    ok(r3.get("suggestions") == r1["suggestions"] and r3["by"]["ollama"] == 0, f"Ollama off: Dez AI still summarises the tables it learned {r3.get('by')}")
    r4 = k.post(f"/api/job/{jid}/ai/links", json={}).json
    ok("suggestions" in r4 or r4.get("error"), f"Ollama off, nothing to ask: {r4}")
    full["ai"]["enabled"] = True
    with open(os.environ["PDFUA_CONFIG"], "w") as fh:
        json.dump(full, fh)

    # read the finished files of the server
    r = k.post("/api/admin/teacher/harvest", json={})
    ok(r.json.get("started"), "learn from the files here starts")
    for _ in range(240):
        d = k.get("/api/admin/teacher").json
        if not d["running"]:
            break
        time.sleep(0.5)
    res = (d["job"]["result"] or {})
    ok(res.get("files", 0) >= 1 and "students" in res, f"the finished files were read: {res}")

    for n_ in notes:
        print("   ·", n_)
    if bad:
        print("teacher test: FAILED")
        for b_ in bad:
            print("   ", b_)
        return 1
    print("teacher test: OK")
    srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
