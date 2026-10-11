"""Browser test of the Users page — the file list (V4.30): server must be running on :$PORT (default 8000) with its
folders in $PDFUA_JOBS (default: the one next to app.py is NOT assumed — pass the folder of the server as argv[2]).

* the filters in use are chips, "↺ Reset" is disabled when nothing is on and clears the search, the filters, the step
  and the sort; Esc clears the search; the empty state has its own reset
* the rows are kept between draws (a row is made again only when its file changed) — filtering and sorting do not
  replay the entrance of every row
* a long list is drawn 150 rows at a time
* the upload card shows the chosen files as chips, each can be taken out
* the person's working time on a file is on its row
* no script errors
"""
import json
import os
import shutil
import sys
import time

from playwright.sync_api import sync_playwright

PORT = int(os.environ.get("PORT", 8000))
B = f"http://127.0.0.1:{PORT}"
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
JOBS = sys.argv[2] if len(sys.argv) > 2 else os.path.join(OUT, "jobs")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
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
        jids = []
        for name in ("article.pdf", "book.pdf", "tables.pdf"):
            with open(os.path.join(ROOT, "tests", "corpus", name), "rb") as fh:
                up = api.post(B + "/upload", multipart={"profile": "Default", "pdf": {"name": name, "mimeType": "application/pdf", "buffer": fh.read()}},
                              headers={"X-Requested-With": "fetch"})
            jids.append(up.json()["jobs"][0])
        for jid in jids:
            ok(until(lambda: api.get(f"{B}/api/job/{jid}/status").json().get("stage") == "review", 120), f"analysed {jid}")
        # some working time on one file, as the editor's heartbeat sends it
        api.get(f"{B}/job/{jids[0]}")
        api.get(f"{B}/api/job/{jids[0]}/presence?rev=&page=0&hold=1&active=1&w=6.0")
        pg.evaluate("() => { try { localStorage.clear(); } catch (e) {} }")
        pg.goto(B + "/")
        pg.wait_for_selector("#jobs tbody tr.jobrow", timeout=20000)

        # ---------------------------------------------------------------- filters and reset
        n_all = pg.locator("#jobs tbody tr.jobrow").count()
        ok(n_all >= 3, f"the files are listed ({n_all})")
        ok(pg.is_disabled("#jobReset"), "nothing is on: Reset is disabled")
        ok("file(s)" in pg.inner_text("#jobCount"), f"the count: {pg.inner_text('#jobCount')!r}")
        pg.evaluate("() => { document.querySelector('#jobs tbody tr.jobrow').dataset.mark = 'kept'; }")
        pg.fill("#jobSearch", "book")
        ok(until(lambda: pg.locator("#jobs tbody tr.jobrow").count() == 1), "search narrows the list")
        ok(pg.locator("#jobChips .fchip").count() == 1 and "book" in pg.inner_text("#jobChips"), f"a chip: {pg.inner_text('#jobChips')!r}")
        ok(pg.is_enabled("#jobReset"), "Reset is enabled")
        pg.select_option("#jobStage", "error")
        ok(until(lambda: pg.is_visible("#emptyJobs")), "no file matches: the empty state")
        ok("No file matches" in pg.inner_text("#jobEmptyMsg") and pg.is_visible("#jobEmptyReset"), "…with its own reset")
        pg.screenshot(path=f"{OUT}/users_empty.png")
        pg.click("#jobEmptyReset")
        ok(until(lambda: pg.locator("#jobs tbody tr.jobrow").count() == n_all), "the reset brings every file back")
        ok(pg.input_value("#jobSearch") == "" and pg.input_value("#jobStage") == "" and pg.is_disabled("#jobReset"), "…and clears the filters")
        ok(pg.evaluate("() => document.querySelectorAll('#jobs tbody tr.jobrow[data-mark=kept]').length") == 1,
           "the rows were kept, not drawn again, through all of that")
        pg.fill("#jobSearch", "tables")
        ok(until(lambda: pg.locator("#jobs tbody tr.jobrow").count() == 1), "search again")
        pg.keyboard.press("Escape")
        ok(until(lambda: pg.input_value("#jobSearch") == "" and pg.locator("#jobs tbody tr.jobrow").count() == n_all), "Esc clears the search")
        pg.select_option("#jobSort", "name")
        ok(pg.is_enabled("#jobReset"), "another sort can be reset too")
        first = pg.inner_text("#jobs tbody tr.jobrow:first-child td:nth-child(2)")
        ok(first.startswith("article"), f"sorted by name: {first[:30]!r}")
        pg.click("#jobReset")
        ok(pg.input_value("#jobSort") == "new" and pg.is_disabled("#jobReset"), "Reset puts the sort back")
        pg.keyboard.press("/")
        ok(pg.evaluate("() => document.activeElement && document.activeElement.id") == "jobSearch", "“/” goes to the search box")
        pg.keyboard.press("Escape")
        pg.evaluate("() => document.activeElement.blur()")
        # the working time is on the row
        row = pg.locator("#jobs tbody tr.jobrow", has_text="article.pdf").first
        ok("⏱" in row.inner_text(), f"the row shows the person's working time: {row.inner_text()[:120]!r}")

        # ---------------------------------------------------------------- a long list: 150 rows at a time
        src = os.path.join(JOBS, jids[0], "meta.json")
        meta = json.load(open(src, encoding="utf-8"))
        for k in range(320):
            jid = f"20250101-0000{k // 100:02d}-{k:06x}"
            os.makedirs(os.path.join(JOBS, jid), exist_ok=True)
            m = dict(meta, id=jid, filename=f"many_{k:03d}.pdf", created="2025-01-01T00:00:00", edited=None, time=None, work="", assigned_to="")
            json.dump(m, open(os.path.join(JOBS, jid, "meta.json"), "w", encoding="utf-8"))
        time.sleep(5.0)                                       # the server looks at the folder every few seconds
        pg.reload()
        pg.wait_for_selector("#jobs tbody tr.jobrow", timeout=20000)
        ok(until(lambda: pg.locator("#jobs tbody tr.jobrow").count() == 150), f"150 rows are drawn of a long list ({pg.locator('#jobs tbody tr.jobrow').count()})")
        ok(pg.is_visible("#jobMoreRow") and "left" in pg.inner_text("#jobMore"), f"…and the rest is offered: {pg.inner_text('#jobMore')!r}")
        t0 = time.time()
        pg.click("#jobMore")
        ok(until(lambda: pg.locator("#jobs tbody tr.jobrow").count() == 300), "“Show more” draws 150 more")
        pg.fill("#jobSearch", "many_3")
        ok(until(lambda: pg.locator("#jobs tbody tr.jobrow").count() == 20), f"searching a long list: {pg.locator('#jobs tbody tr.jobrow').count()} rows")
        pg.click("#jobReset")
        ok(until(lambda: pg.locator("#jobs tbody tr.jobrow").count() == 150), "Reset: the first 150 again")
        pg.screenshot(path=f"{OUT}/users_list.png")

        # ---------------------------------------------------------------- the upload card
        pdf1 = os.path.join(ROOT, "tests", "corpus", "article.pdf")
        pdf2 = os.path.join(ROOT, "tests", "corpus", "tables.pdf")
        pg.set_input_files("#fileInput", [pdf1, pdf2])
        ok(until(lambda: pg.locator("#fileList .fchip").count() == 2), "two files: two chips")
        ok("2 file(s)" in pg.inner_text("#fileList"), f"and the total: {pg.inner_text('#fileList')[-40:]!r}")
        pg.locator("#fileList .fchip button").first.click()
        ok(until(lambda: pg.locator("#fileList .fchip").count() == 1), "a file can be taken out")
        ok(pg.evaluate("() => document.getElementById('fileInput').files.length") == 1, "…and is not uploaded")
        pg.screenshot(path=f"{OUT}/users_upload.png")
        br.close()

    ok(not errors, f"script errors: {errors[:4]}")
    # tidy: the fake files go (the server forgets them at its next look)
    for k in range(320):
        shutil.rmtree(os.path.join(JOBS, f"20250101-0000{k // 100:02d}-{k:06x}"), ignore_errors=True)
    if bad:
        print("users page (browser) test: FAILED")
        for b_ in bad:
            print("   ", b_)
        return 1
    print("users page (browser) test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
