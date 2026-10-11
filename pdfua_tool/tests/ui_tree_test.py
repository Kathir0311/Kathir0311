"""Browser test of the tag tree on a big file (V4.33): server must be running on :$PORT (default 8000) with its folders in
the folder given as argv[2] (its PDFUA_JOBS) — the test makes a file of ~6 000 tags the way perf_test.py does.

The reported lag ("even scrolling is slow, like it hangs"): the tree was every tag of the file in the page — a book of 15 000 tags
is 80 000 elements, laid out and hit-tested at every wheel step (6 pictures a second). Now only the rows near the screen are in the
page, between two spacers.

* a file of ~6 000 tags: a few dozen rows and a few thousand elements in the page, `aria-setsize` says how many the tree has,
  the scroll bar is as long as the whole list (rows are 30px each)
* wheel-scrolling the tree: no picture is held up (budgets are generous: a slow machine passes, the old tree did not)
* the middle and the end of the list are reached and show the right rows; the arrow keys keep the selected row on the screen
  (up and down); a search finds the one tag in 6 000, clearing it brings the list back; "Select all shown" selects them all
* clicking a box on the page selects its tag and brings its row on to the screen, however far down it is
* collapse all / expand all; changing a tag's type (key 2 → H2) is shown in its row; "the tree was every row" never again
* no script errors
"""
import copy
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PORT = int(os.environ.get("PORT", 8000))
B = f"http://127.0.0.1:{PORT}"
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
JOBS = sys.argv[2] if len(sys.argv) > 2 else os.path.join(OUT, "jobs")

from playwright.sync_api import sync_playwright  # noqa: E402


def main() -> int:
    from core.pipeline import load_model, save_model
    bad: list = []
    errors: list = []

    def ok(cond, msg):
        if not cond:
            bad.append(msg)

    def until(fn, secs=12, step=0.25):
        t0 = time.time()
        while time.time() - t0 < secs:
            try:
                if fn():
                    return True
            except Exception:
                pass
            time.sleep(step)
        return False

    with sync_playwright() as p:
        br = p.chromium.launch(executable_path=os.environ.get("PW_CHROMIUM") or None)
        ctx = br.new_context(viewport={"width": 1500, "height": 950})
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        pg.goto(B + "/login")
        pg.fill("input[name=username]", "Kathir")
        pg.fill("input[name=password]", "Kathir@051408")
        pg.click("button[type=submit]")
        pg.wait_for_selector("#fileInput", state="attached", timeout=20000)
        api = ctx.request

        # ---------------------------------------------------------------- a big file
        with open(os.path.join(ROOT, "tests", "corpus", "article.pdf"), "rb") as fh:
            buf = fh.read()
        r = api.post(B + "/upload", multipart={"profile": "Default", "pdf": {"name": "big_tree.pdf", "mimeType": "application/pdf", "buffer": buf}},
                     headers={"X-Requested-With": "fetch"})
        jid = r.json()["jobs"][0]
        ok(until(lambda: api.get(B + f"/api/job/{jid}/status").json().get("stage") == "review", 120, 0.5), "the file is analysed")
        d = os.path.join(JOBS, jid)
        m = load_model(d)
        paras = [n for n in m.nodes.values() if n.role == "P" and not n.artifact]
        i = 0
        while len(m.nodes) < 6000:
            n = copy.deepcopy(paras[i % len(paras)])
            n.id, n.children, n.parent = f"zz{i}", [], None
            n.text = f"{n.text} · copy {i}"
            m.add(n, None)
            i += 1
        needle = m.nodes[f"zz{i - 1}"]
        needle.text = "zzzneedle in the last tag of the file"
        save_model(d, m)
        far = m.nodes["zz3999"]                                         # far down the list, and on a page of the file
        far_page = far.rects_on(far.page)[0] if far.rects_on(far.page) else None

        pg.goto(B + f"/job/{jid}")
        pg.wait_for_selector("#tree .node", timeout=60000)
        time.sleep(1.5)

        def info():
            return pg.evaluate("""() => { const t = document.querySelector('#tree'); const rows = [...t.querySelectorAll('.node')];
              return { rows: rows.length, first: rows.length ? Number(rows[0].getAttribute('aria-posinset')) : 0,
                       last: rows.length ? Number(rows[rows.length - 1].getAttribute('aria-posinset')) : 0, set: rows.length ? Number(rows[0].getAttribute('aria-setsize')) : 0,
                       top: t.scrollTop, height: t.scrollHeight, h: t.clientHeight, dom: t.querySelectorAll('*').length,
                       rowH: rows.length ? rows[0].getBoundingClientRect().height : 0 }; }""")

        a = info()
        ok(a["set"] >= 5900, f"the tree has all its rows: {a['set']}")
        ok(a["rows"] <= 150, f"…but only the rows near the screen are in the page: {a['rows']}")
        ok(a["dom"] < 1500, f"the tree holds {a['dom']} elements (was ~5 per tag, 30 000 here)")
        ok(a["rowH"] == 30 and abs(a["height"] - (a["set"] * 30 + 8)) <= 4, f"every row is 30px and the scroll bar is as long as the list: {a['height']} for {a['set']} rows")

        # ---------------------------------------------------------------- scrolling
        box = pg.locator("#tree").bounding_box()
        pg.mouse.move(box["x"] + 100, box["y"] + 200)
        pg.evaluate("""() => { window.__dts = []; window.__run = true; let last = performance.now();
          (function f(t) { window.__dts.push(t - last); last = t; if (window.__run) requestAnimationFrame(f); })(performance.now()); }""")
        for _ in range(50):
            pg.mouse.wheel(0, 220)
            time.sleep(0.03)
        time.sleep(0.4)
        dts = sorted(pg.evaluate("window.__run = false; window.__dts")[2:])
        p95, worst = dts[int(len(dts) * 0.95) - 1], dts[-1]
        ok(p95 <= 50 and worst <= 200, f"wheel scrolling: p95 {p95:.0f} ms, worst {worst:.0f} ms over {len(dts)} pictures (the old tree: 50 ms+ at 3 000 tags, 170 ms at 15 000)")
        b = info()
        ok(b["top"] > 3000 and b["rows"] <= 150 and b["first"] <= (b["top"] // 30) + 2 and b["last"] >= (b["top"] + b["h"]) // 30 - 2,
           f"scrolled: rows {b['first']}–{b['last']} are in the page, the screen is at {b['top']}px")
        pg.evaluate("document.querySelector('#tree').scrollTop = 90000")
        time.sleep(0.5)
        c = info()
        ok(abs(c["first"] - (90000 // 30 - 24)) <= 6 and c["rows"] <= 150, f"the middle of the list shows its own rows: {c['first']}–{c['last']} at 90 000px")
        pg.evaluate("document.querySelector('#tree').scrollTop = 1e9")
        time.sleep(0.5)
        e = info()
        ok(e["last"] == e["set"], f"the end of the list is reached: row {e['last']} of {e['set']}")
        pg.screenshot(path=f"{OUT}/tree_big_end.png")

        # ---------------------------------------------------------------- keys keep the selected row on the screen
        pg.evaluate("document.querySelector('#tree').scrollTop = 0")
        time.sleep(0.4)
        pg.locator("#tree .node").nth(3).click()
        time.sleep(0.3)
        vis = """() => { const r = document.querySelector('#tree .node.focus'); if (!r) return false; const a = r.getBoundingClientRect(), b = document.querySelector('#tree').getBoundingClientRect(); return a.top >= b.top - 1 && a.bottom <= b.bottom + 1; }"""
        for _ in range(70):
            pg.keyboard.press("ArrowDown")
        time.sleep(0.5)
        ok(pg.evaluate(vis), "70 × ArrowDown: the selected row is on the screen")
        f1 = info()
        ok(f1["top"] > 1500, f"…and the tree followed it ({f1['top']}px)")
        for _ in range(30):
            pg.keyboard.press("ArrowUp")
        time.sleep(0.4)
        ok(pg.evaluate(vis), "30 × ArrowUp: the selected row is on the screen")

        # ---------------------------------------------------------------- search, select all shown
        pg.fill("#search", "zzzneedle")
        ok(until(lambda: info()["set"] == 1), f"a search finds the one tag in {a['set']}: {info()['set']}")
        ok("zzzneedle" in pg.locator("#tree .node .ntext").first.inner_text(), "…and shows it")
        pg.fill("#search", "")
        ok(until(lambda: info()["set"] >= 6000), "clearing the search brings the whole list back")
        pg.click("#selAllShown")
        ok(until(lambda: pg.locator(".toast", has_text=f"{info()['set']} tag(s) selected").count() > 0), "Select all shown selects every tag of the list, not only the rows in the page")
        pg.keyboard.press("Escape")

        # ---------------------------------------------------------------- a box on the page brings its row on to the screen
        if far_page is not None:
            pg.evaluate("document.querySelector('#tree').scrollTop = 0")
            pg.fill("#pageNo", str(far.page + 1))
            pg.press("#pageNo", "Enter")
            time.sleep(1.2)
            box_el = pg.locator(f'#overlay .box[data-id="{far.id}"]')
            if box_el.count():
                box_el.first.dispatch_event("click")                    # (the copies lie on top of each other: a real click may reach another one)
                time.sleep(0.8)
                g = info()
                pos = int(pg.locator("#tree .node.focus").first.get_attribute("aria-posinset") or 0) if pg.locator("#tree .node.focus").count() else 0
                ok(pos > 3500 and g["first"] <= pos <= g["last"] and pg.evaluate(vis), f"a click on a box far down the list: its row ({pos}) is in the page ({g['first']}–{g['last']}) and on the screen")
            else:
                ok(False, "the far tag has no box on its page")

        # ---------------------------------------------------------------- collapse / expand, an edit
        pg.click("#collapseAll")
        time.sleep(0.8)
        h = info()
        ok(h["set"] < a["set"] and h["rows"] <= 150, f"collapse all: {h['set']} rows ({h['rows']} in the page)")
        pg.click("#expandAll")
        time.sleep(0.8)
        ok(info()["set"] >= 6000, "expand all brings every row back")
        pg.evaluate("document.querySelector('#tree').scrollTop = 0")
        time.sleep(0.4)
        pg.locator("#tree .node", has_text="Abstract").first.click()
        time.sleep(0.3)
        pg.keyboard.press("2")
        ok(until(lambda: pg.locator("#tree .node.focus .role").first.inner_text().strip() == "H2"), "a change of type (key 2) shows in its row")
        ok(info()["rows"] <= 150, "…and the tree is still a window")
        br.close()

    ok(not errors, f"script errors: {errors[:4]}")
    if bad:
        print("tag tree (big file) test: FAILED")
        for b_ in bad:
            print("   ", b_)
        return 1
    print("tag tree (big file) test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
