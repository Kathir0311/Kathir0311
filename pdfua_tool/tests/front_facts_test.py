"""V4.32 — the front page tells only what the tool can show: every number it quotes is counted in the tool, every claim has
its proof in the code, the README or a measurement made here, and the page itself is built the way it asks of a PDF.

No server and no browser needed (the browser half is tests/ui_front_test.py).

* the numbers: the machine checks, options, profiles and undo steps on the page are the tool's own (`site_facts()`), the
  steps of the workflow, the zoom, the password hashing, "300 pages in under a minute" and "4,000 tags" are the figures the
  code, the README and tests/perf_test.py give
* "nothing leaves your network" is real: a connection to a public address is refused before a byte is sent, and logged;
  the Content-Security-Policy of the page lets it talk to its own server only
* the "before → after" table of the reference files is measured again here — the six files are run through the studio, and
  the errors found before and after must be the numbers on the page (the bars too); each file is what its label says
* the page is built as the PDFs it asks for: one h1, no heading level skipped, every link to a part of the page finds it,
  no id twice, every aria-labelledby finds its heading, a real table with caption and headers, no handler written into the
  HTML, nothing loaded from another server; every id the page's script looks for is in the page
* nothing a page like this should not say without proof: no testimonials, logos of others, certificates or guarantees
* (V4.32.1) the model service the studio asks is called only "the Online AI" on the page — no product name in the page, its
  error variant, its script or its style — and the first thing the page says about it is where it runs: on your own network
"""
from __future__ import annotations

import html
import os
import re
import sys
import tempfile
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp()
os.environ["PDFUA_JOBS"] = os.path.join(TMP, "jobs")
os.environ["PDFUA_CONFIG"] = os.path.join(TMP, "config.json")
os.environ["PDFUA_USERS"] = os.path.join(TMP, "users.json")
os.environ["PDFUA_SAMPLES"] = os.path.join(TMP, "samples")


def read(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class Page(HTMLParser):
    """What matters about the page: ids, links, headings, names of parts, anything loaded, anything written into a tag."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list = []
        self.hrefs: list = []                # (tag, attr, value)
        self.headings: list = []             # (level, text)
        self.labelled: list = []             # aria-labelledby values
        self.handlers: list = []             # on…= attributes
        self.count: dict = {}
        self.sections: list = []             # attrs of every <section>
        self.lang = ""
        self.text: list = []
        self._h = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.count[tag] = self.count.get(tag, 0) + 1
        if tag == "html":
            self.lang = a.get("lang", "")
        if a.get("id"):
            self.ids.append(a["id"])
        if a.get("aria-labelledby"):
            self.labelled.append(a["aria-labelledby"])
        for k in a:
            if k.startswith("on"):
                self.handlers.append((tag, k))
        for k in ("href", "src", "action", "poster", "data", "srcset"):
            if a.get(k):
                self.hrefs.append((tag, k, a[k]))
        if tag == "section":
            self.sections.append(a)
        if re.fullmatch(r"h[1-6]", tag):
            self._h = [int(tag[1]), ""]
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if re.fullmatch(r"h[1-6]", tag) and self._h:
            self.headings.append((self._h[0], re.sub(r"\s+", " ", self._h[1]).strip()))
            self._h = None
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip:
            return
        self.text.append(data)
        if self._h is not None:
            self._h[1] += data


def main() -> int:
    import app as A
    from core import netguard
    from core.options import BUILTIN_PROFILES, META, Options
    from core.pipeline import process_file
    from tests.run_suite import roles
    bad: list = []

    def ok(cond, msg):
        if not cond:
            bad.append(msg)

    k = A.app.test_client()
    r = k.get("/login")
    ok(r.status_code == 200, f"the front page answers ({r.status_code})")
    src = r.get_data(as_text=True)
    pg = Page()
    pg.feed(src)
    body = re.sub(r"\s+", " ", html.unescape("".join(pg.text)))

    # ------------------------------------------------------------------ the tool's own numbers
    codes: set = set()
    for name in ("checker.py", "pipeline.py"):
        codes |= set(re.findall(r'"(UA-[A-Z0-9]+)"', read("core", name)))
    facts = A.site_facts()
    ok(facts["checks"] == len(codes) >= 50, f"machine checks: the page counts {facts['checks']}, the code has {len(codes)}")
    ok(facts["options"] == len(META) and facts["profiles"] == len(BUILTIN_PROFILES), f"options and profiles are the tool's lists: {facts}")
    undo = int(re.search(r"^HISTORY_LIMIT = (\d+)", read("app.py"), re.M).group(1))
    ok(facts["undo"] == undo, f"undo steps: {facts['undo']} vs {undo}")
    ok(body.count(f"{len(codes)} machine checks") >= 3, f"the page quotes {len(codes)} machine checks ({body.count(f'{len(codes)} machine checks')}x)")
    ok(f"{len(BUILTIN_PROFILES)} built-in profiles" in body and f"{(len(META) // 10) * 10}+ options" in body, "…its profiles and options")
    ok((len(META) // 10) * 10 <= len(META), "…'100+ options' is never more than the tool has")
    ok(f"{undo} steps" in body, f"…and its undo steps ({undo})")
    ok(not re.search(r"\{\{|\{%|\bNone\b|\bundefined\b", body), "no template left-over in the text")
    ok(re.search(r'data-to="%d"' % len(codes), src) is not None, "the counting number is the same count")
    ok(f"v{A.VERSION}" in body, f"the footer says the version ({A.VERSION})")

    from core import workflow
    ok(workflow.STAGES[:4] == ("production", "qa", "final_qa", "delivery"), f"the four workflow steps: {workflow.STAGES}")
    for w in ("Production", "QA", "Final QA", "Delivery"):
        ok(w in body, f"…{w} is named on the page")
    ok(workflow.DEFAULTS.get("require_pac") is True, "the PAC report is asked for before QA by default")
    readme = read("README.md")
    ok("300 pages process in under a minute" in readme and "300 pages in under a minute" in body, "'300 pages in under a minute' is the README's own figure")
    ok("About 300" not in body and "5,000 tags" not in body, "…and no bigger figure than the tests prove")
    m_big = re.search(r"while len\(m\.nodes\) < (\d+)", read("tests", "perf_test.py"))
    ok(m_big and int(m_big.group(1)) >= 4000 and "4,000 tags" in body, f"'a file of 4,000 tags' is what tests/perf_test.py makes ({m_big and m_big.group(1)})")
    ok(re.search(r"ZMAX = 8\.0", read("static", "editor.js")) and "Zoom up to 800 %" in body, "zoom up to 800 % is the editor's own limit")
    ok("pbkdf2:sha256" in read("core", "accounts.py") and "PBKDF2-SHA256" in body, "passwords: salted PBKDF2-SHA256 in the code")
    ok("Python 3.10+" in readme and "Python 3.10 or newer" in body and sys.version_info >= (3, 10), "Python 3.10 or newer is what the README says")

    # ------------------------------------------------------------------ nothing leaves the network
    import socket
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect(("8.8.8.8", 443))                     # a public address: refused before any packet leaves
        ok(False, "a connection to a public address was NOT refused")
    except netguard.Blocked:
        pass
    except OSError as e:
        ok(False, f"the guard did not stop it, the network did: {e}")
    finally:
        s.close()
    try:
        socket.getaddrinfo("example.com", 80)
        ok(False, "a public name was resolved")
    except socket.gaierror as e:
        ok("privacy guard" in str(e), f"a public name is refused by the guard: {e}")
    except Exception as e:  # noqa: BLE001
        ok(False, f"unexpected: {e!r}")
    ok(any("8.8.8.8" in str(x) for x in netguard.recent(10)), f"…and the attempt is in the guard's log: {netguard.recent(2)}")
    csp = r.headers.get("Content-Security-Policy", "")
    ok(csp and not re.search(r"https?:|\*", csp), f"the page may talk to its own server only: {csp[:120]}")
    ok("'self'" in csp and "default-src" in csp, "…the policy is set on the front page itself")

    # ------------------------------------------------------------------ the page is built as the PDFs it asks for
    ok(pg.lang == "en", f"the page names its language ({pg.lang!r})")
    ok(pg.count.get("h1") == 1 and pg.count.get("main") == 1 and pg.count.get("nav", 0) >= 1 and pg.count.get("footer", 0) >= 1 and pg.count.get("header", 0) >= 1,
       f"one h1, one main, a nav, a header and a footer: {[(t, pg.count.get(t)) for t in ('h1', 'main', 'nav', 'header', 'footer')]}")
    lv = [h[0] for h in pg.headings]
    ok(lv[:1] == [1] and all(b - a <= 1 for a, b in zip(lv, lv[1:])), f"no heading level is skipped {lv[:30]}")
    ok(all(h[1] for h in pg.headings), "every heading says something")
    ok(len(pg.ids) == len(set(pg.ids)), f"no id twice: {sorted({i for i in pg.ids if pg.ids.count(i) > 1})}")
    anchors = [h[2] for h in pg.hrefs if h[1] == "href" and h[2].startswith("#") and len(h[2]) > 1]
    ok(anchors and all(a[1:] in pg.ids for a in anchors), f"every link to a part of the page finds it: {[a for a in anchors if a[1:] not in pg.ids]}")
    ok(all(any(i in pg.ids for i in v.split()) for v in pg.labelled) and pg.labelled, f"every aria-labelledby finds its heading: {[v for v in pg.labelled if v not in pg.ids]}")
    named = [s_ for s_ in pg.sections if s_.get("aria-labelledby") or s_.get("aria-label")]
    ok(len(named) == len(pg.sections) and len(pg.sections) >= 12, f"every part has a name ({len(named)} of {len(pg.sections)})")
    ok(pg.count.get("caption") == 1 and pg.count.get("table") == 1 and pg.count.get("th", 0) >= 11, "the table of what changes is a real table (caption, headers)")
    ok(pg.count.get("details", 0) == pg.count.get("summary", 0) >= 9, "every question is a details with its summary")
    ok(not pg.handlers, f"no handler is written into the HTML: {pg.handlers[:3]}")
    loads = [h for h in pg.hrefs if not (h[0] == "a" and h[1] == "href")]
    ok(not [h for h in loads if re.match(r"(https?:)?//", h[2]) or h[2].lower().startswith("javascript:")],
       f"nothing is loaded from another server: {[h for h in loads if re.match(r'(https?:)?//', h[2])][:3]}")
    out_links = [h[2] for h in pg.hrefs if h[0] == "a" and re.match(r"(https?:)?//", h[2])]
    ok(all(re.match(r"https://www\.dezquare\.co\.in/?$", u) for u in out_links), f"the only way out is the company's own address: {out_links}")
    ok(not [h for h in pg.hrefs if h[2].lower().startswith("javascript:")], "no javascript: link")
    css = read("static", "landing.css")
    ok(not re.search(r"url\(\s*['\"]?(https?:)?//|@import", css), "the page's style loads nothing from outside")
    js = read("static", "landing.js")
    ok(not re.search(r"https?://|XMLHttpRequest|sendBeacon|WebSocket|EventSource", js), "the page's script talks to nobody")
    # every id the page's script looks for is on the page — a renamed id must not quietly switch an animation off
    wanted = set(re.findall(r"(?:\$\$?|querySelector(?:All)?)\(\s*['\"]#([A-Za-z][\w-]*)", js)) | set(re.findall(r"getElementById\(\s*['\"]([\w-]+)", js))
    bad_login = k.post("/login", data={"username": "nobody", "password": "wrong"})
    pe = Page()
    pe.feed(bad_login.get_data(as_text=True))                 # the page with its error (the script shakes the form for it)
    ids_all = set(pg.ids) | set(pe.ids)
    ok(wanted and wanted <= ids_all, f"the script finds everything it looks for: missing {sorted(wanted - ids_all)}")

    # ------------------------------------------------------------------ nothing a page like this cannot prove
    low = body.lower()
    for word in ("guarantee", "100%", "100 %", "trusted by", "testimonial", "award", "iso 27001", "soc 2", "gdpr", "hipaa", "certified by", "our customers say"):
        ok(word not in low, f"the page does not say '{word}'")
    ok("not a conformance certificate" in low, "the page says what a machine check is not")
    ok(low.count("certif") <= 5, f"'certified' only where the page says it is not ({low.count('certif')}x)")
    ok(pg.count.get("img", 0) == 0 or "alt=" in src, "pictures (if any) have alt text")

    # ------------------------------------------------------------------ the model service has no product name on the front page
    served = {"the page": src, "the page with an error": bad_login.get_data(as_text=True), "landing.js": js, "landing.css": css, "style.css": read("static", "style.css")}
    for what, txt in served.items():
        ok(not re.search(r"ollama", txt, re.I), f"{what} names no product of the model service ({len(re.findall('ollama', txt, re.I))}x)")
    mentions = [m.start() for m in re.finditer(r"Online AI", body)]
    ok(len(mentions) >= 10, f"the page speaks of the Online AI ({len(mentions)}x)")
    ok(mentions and "own network" in body[mentions[0]:mentions[0] + 120], f"the first thing it says about the Online AI is where it runs: {body[mentions[0]:mentions[0] + 120]!r}" if mentions else "no mention")
    for at in mentions:                                            # and never that it is a service in the cloud or on the internet
        near = body[at:at + 70]
        near = re.sub(r"no cloud service|nothing from the internet|without the internet", "", near)
        ok(not re.search(r"cloud|internet", near), f"the Online AI is not put in the cloud: {near!r}")
    ok("Online AI teaches." in body and "<b>Online AI</b><small>the teacher</small>" in src, "the story's heading and its teacher say Online AI")

    # ------------------------------------------------------------------ before → after: measured here, again
    rows = re.findall(r'<span class="nm">(.*?)</span><span class="bar" style="--b:(\d+)%;--a:(\d+)%">.*?<s>(\d+)</s> → <b>(\d+)</b>', src, re.S)
    page_rows = {n_: (int(b_), int(a_), int(x_), int(y_)) for n_, b_, a_, x_, y_ in rows}
    files = {"Journal article, two columns": "article.pdf", "Book with contents and bookmarks": "book.pdf", "Form with fields": "forms.pdf",
             "Attendance register with a table": "tables.pdf", "Tamil and Greek text": "unicode.pdf", "Rotated pages": "rotated.pdf"}
    ok(set(page_rows) == set(files), f"every row of the table is measured here: {sorted(set(page_rows) ^ set(files))}")
    worst = max((p[2] for p in page_rows.values()), default=1) or 1
    import pymupdf
    work = os.path.join(TMP, "ref")
    os.makedirs(work, exist_ok=True)
    rot = os.path.join(work, "rotated.pdf")
    d = pymupdf.open(os.path.join(ROOT, "tests", "corpus", "article.pdf"))
    for i, p in enumerate(d):
        p.set_rotation([90, 180, 270][i % 3])
    d.save(rot)
    d.close()
    for label, fn in files.items():
        if label not in page_rows:
            continue
        src_pdf = rot if fn == "rotated.pdf" else os.path.join(ROOT, "tests", "corpus", fn)
        res = process_file(src_pdf, os.path.join(work, fn[:-4]), Options(fix_contrast="off"))
        if res.get("status") != "done":
            ok(False, f"{label}: the file could not be processed: {res.get('error')}")
            continue
        before, after = res["score_before"]["errors"], res["score_after"]["errors"]
        b_, a_, x_, y_ = page_rows[label]
        ok((x_, y_) == (before, after), f"{label}: the page says {x_} → {y_}, the tool finds {before} → {after}")
        ok(b_ == round(100 * x_ / worst) and a_ == round(100 * y_ / worst), f"{label}: the bars ({b_} %, {a_} %) are those numbers on one scale of {worst}")
        out = os.path.join(work, fn[:-4], "accessible.pdf")
        doc = pymupdf.open(out)
        if fn == "book.pdf":
            ok(len(doc.get_toc()) >= 3, f"{label}: it has bookmarks ({len(doc.get_toc())})")
        elif fn == "forms.pdf":
            ok(sum(len(list(p.widgets())) for p in doc) >= 3, f"{label}: it has fields")
        elif fn == "tables.pdf":
            ok("Table" in roles(out), f"{label}: it has a table")
        elif fn == "unicode.pdf":
            txt = "".join(p.get_text() for p in doc)
            ok(re.search("[஀-௿]", txt) and re.search("[Ͱ-Ͽ]", txt), f"{label}: it has Tamil and Greek text")
        elif fn == "rotated.pdf":
            ok({p.rotation for p in doc} >= {90, 180, 270}, f"{label}: its pages are turned {sorted({p.rotation for p in doc})}")
        doc.close()
    ok("The one left is a picture" in body, "…and the page says what the one error left is")

    if bad:
        print("front page facts test: FAILED")
        for b_ in bad:
            print("   ", b_)
        return 1
    print("front page facts test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
