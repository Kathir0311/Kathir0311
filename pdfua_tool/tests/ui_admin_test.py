"""Browser test of the Admin page (V4.30): server must be running on :$PORT (default 8000).

* Admin → Files: filters that can be seen and cleared — the chips of what is on, "↺ Reset filters" (disabled when nothing
  is on, clears every filter and the sort), the empty state with its own reset, the panel of more filters, a saved view,
  the filters kept for next time, the working time of every file, 200 rows at a time
* Admin → Users: the Rights dialog (Role default / Allow / Deny, what is in force), saved, shown in the table
* Admin → Time: the report; Admin → Workflow: the break limit; Admin → Activity: a reset
* no script errors
"""
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

PORT = int(os.environ.get("PORT", 8000))
B = f"http://127.0.0.1:{PORT}"
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
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
        api.post(B + "/api/admin/users", data={"username": "anna", "password": "secret-anna", "role": "production"})
        api.post(B + "/api/admin/users", data={"username": "raj", "password": "secret-raj", "role": "qa"})
        jids = []
        for name in ("article.pdf", "book.pdf", "tables.pdf"):
            with open(os.path.join(ROOT, "tests", "corpus", name), "rb") as fh:
                up = api.post(B + "/upload", multipart={"profile": "Default", "pdf": {"name": name, "mimeType": "application/pdf", "buffer": fh.read()}},
                              headers={"X-Requested-With": "fetch"})
            jids.append(up.json()["jobs"][0])
        for jid in jids:
            ok(until(lambda: api.get(f"{B}/api/job/{jid}/status").json().get("stage") == "review", 120), f"analysed {jid}")
        api.post(f"{B}/api/admin/files/assign", data={"ids": [jids[0]], "to": "anna"})
        # some working time on the first file: the editor's heartbeat, as a person at work sends it
        api.get(f"{B}/job/{jids[0]}")
        api.get(f"{B}/api/job/{jids[0]}/presence?rev=&page=0&hold=1&active=1&w=6.0")
        time.sleep(1.5)
        api.get(f"{B}/api/job/{jids[0]}/presence?rev=&page=0&hold=1&active=1&w=1.5")
        pg.goto(B + "/admin#files")
        pg.evaluate("() => { try { localStorage.clear(); } catch (e) {} }")
        pg.goto(B + "/admin")
        pg.click("button[data-tab=files]")
        pg.wait_for_selector("#fTable tbody tr.frow", timeout=20000)

        # ---------------------------------------------------------------- files: filters and reset
        ok(pg.locator("#fTable thead th", has_text="Time").count() == 1, "the table has a Time column")
        ok(pg.is_disabled("#fReset"), "nothing is on: Reset is disabled")
        ok(pg.locator("#fChips .fchip").count() == 0, "no chips")
        n_all = pg.locator("#fTable tbody tr.frow").count()
        ok(n_all >= 3, f"the files are listed ({n_all})")
        pg.fill("#fSearch", "book")
        ok(until(lambda: pg.locator("#fTable tbody tr.frow").count() == 1), "search narrows the list")
        ok(pg.locator("#fChips .fchip").count() == 1 and "book" in pg.inner_text("#fChips"), f"a chip says what is on: {pg.inner_text('#fChips')!r}")
        ok(pg.is_enabled("#fReset"), "Reset is enabled")
        pg.select_option("#fUser", "anna")
        ok(until(lambda: pg.is_visible("#fEmpty")), "no file matches: the empty state shows")
        ok(pg.is_visible("#fEmptyReset"), "…with its own reset")
        pg.screenshot(path=f"{OUT}/admin_empty.png")
        pg.click("#fEmptyReset")
        ok(until(lambda: pg.locator("#fTable tbody tr.frow").count() == n_all), "the reset in the empty state brings every file back")
        ok(pg.input_value("#fSearch") == "" and pg.input_value("#fUser") == "" and pg.is_disabled("#fReset"), "…and clears the filters")
        # a chip's own ✕
        pg.select_option("#fUser", "anna")
        ok(until(lambda: pg.locator("#fTable tbody tr.frow").count() == 1), "given to anna: one file")
        pg.click("#fChips .fchip button")
        ok(until(lambda: pg.locator("#fTable tbody tr.frow").count() == n_all) and pg.locator("#fChips .fchip").count() == 0, "the ✕ of a chip removes that filter")
        # more filters
        pg.click("#fAdv")
        ok(pg.is_visible("#fAdvPanel") and pg.get_attribute("#fAdv", "aria-expanded") == "true", "the panel of more filters opens")
        pg.fill("#fWorkMin", "0")
        pg.check("#fNoTime")
        ok(until(lambda: pg.locator("#fTable tbody tr.frow").count() == n_all - 1), "“nobody has worked on it” leaves out the file with time")
        ok(pg.inner_text("#fAdvN") == "2" or pg.inner_text("#fAdvN") == "1", f"the panel button counts what is on: {pg.inner_text('#fAdvN')}")
        pg.uncheck("#fNoTime")
        pg.fill("#fWorkMin", "")
        pg.fill("#fWorkMin", "0")
        pg.check("#fLocked")
        ok(until(lambda: pg.locator("#fTable tbody tr.frow").count() <= 1), "being edited now: only the file open in the editor")
        pg.click("#fReset")
        ok(until(lambda: pg.locator("#fTable tbody tr.frow").count() == n_all) and not pg.is_checked("#fLocked") and pg.input_value("#fWorkMin") == "",
           "Reset clears the advanced filters as well")
        # sort, and the reset of the sort
        pg.select_option("#fSort", "time")
        ok(until(lambda: "article.pdf" in pg.inner_text("#fTable tbody tr.frow:first-child")), "sorted by working time: the file with time first")
        ok(pg.is_enabled("#fReset"), "a sort other than the default can be reset too")
        pg.click("#fReset")
        ok(pg.input_value("#fSort") == "new" and pg.is_disabled("#fReset"), "Reset puts the sort back")
        # the working time of a file
        row = pg.locator("#fTable tbody tr.frow", has_text="article.pdf").first
        ok("s" in row.locator("td.timecell").inner_text() or "min" in row.locator("td.timecell").inner_text(), f"the file shows its working time: {row.locator('td.timecell').inner_text()!r}")
        row.locator(".xbtn").click()
        ok(until(lambda: pg.locator(".fdetail .tmbox").count() == 1 and "Kathir" in pg.inner_text(".fdetail .tmbox")), "the details say who spent the time")
        row.locator(".xbtn").click()
        # a view, and the filters kept for next time
        pg.select_option("#fProfile", "Default")
        pg.fill("#fSearch", "tables")
        pg.once("dialog", lambda d: d.accept("tables only"))
        pg.click("#fSave")
        ok(until(lambda: pg.locator("#fViews option", has_text="tables only").count() == 1), "a view is kept")
        pg.click("#fReset")
        pg.select_option("#fViews", label="tables only")
        ok(until(lambda: pg.input_value("#fSearch") == "tables" and pg.locator("#fTable tbody tr.frow").count() == 1), "choosing the view puts its filters back")
        pg.reload()
        pg.click("button[data-tab=files]")
        pg.wait_for_selector("#fTable tbody tr.frow", timeout=20000)
        ok(until(lambda: pg.input_value("#fSearch") == "tables"), "the filters are kept for the next visit")
        ok(pg.locator("#fViews option", has_text="tables only").count() == 1, "…and so are the views")
        pg.screenshot(path=f"{OUT}/admin_files_filtered.png")
        pg.click("#fReset")
        ok(pg.input_value("#fSearch") == "", "reset again")
        pg.click("#fExport")
        # the filter bar stays in view
        pg.mouse.wheel(0, 400)
        pg.screenshot(path=f"{OUT}/admin_files.png")

        # ---------------------------------------------------------------- users: rights
        pg.click("button[data-tab=users]")
        pg.wait_for_selector("#uTable tbody tr", timeout=15000)
        ok(pg.locator("#uTable thead th", has_text="Rights").count() == 1 and pg.locator("#uTable thead th", has_text="Time").count() == 1, "Users has Time and Rights")
        arow = pg.locator("#uTable tbody tr", has_text="anna")
        arow.locator(".rightsbtn").click()
        pg.wait_for_selector("#rightsDlg[open]", timeout=8000)
        ok(pg.locator("#rgList .rgrow").count() >= 8, f"the rights are listed ({pg.locator('#rgList .rgrow').count()})")
        deletes = pg.locator("#rgList .rgrow", has_text="Delete files")
        deletes.locator("button", has_text="Deny").click()
        ok(deletes.locator(".chip").inner_text() == "may not", "Deny: the chip says she may not")
        pg.screenshot(path=f"{OUT}/admin_rights.png")
        pg.click("#rgSave")
        ok(until(lambda: "1 custom" in pg.inner_text("#uTable tbody")), "the table shows the custom right")
        users = api.get(B + "/api/admin/users").json()
        ok(next(u for u in users if u["username"] == "anna")["rights"] == {"delete_files": False}, "saved on the server")
        arow = pg.locator("#uTable tbody tr", has_text="anna")
        arow.locator(".rightsbtn").click()
        pg.wait_for_selector("#rightsDlg[open]", timeout=8000)
        pg.click("#rgReset")
        pg.click("#rgSave")
        ok(until(lambda: "1 custom" not in pg.inner_text("#uTable tbody")), "back to the role's defaults")

        # ---------------------------------------------------------------- time, workflow, activity
        pg.click("button[data-tab=time]")
        ok(until(lambda: pg.locator("#tmTable tbody tr.frow").count() >= 1), "the time report lists who worked")
        ok("Kathir" in pg.inner_text("#tmTable"), "…Kathir")
        ok(pg.locator("#tmTiles .tile").count() == 4, "four tiles")
        pg.click("#tmRange button[data-d='30']")
        ok(until(lambda: pg.locator("#tmDays .sday").count() == 30), "30 days drawn")
        ok(pg.is_enabled("#tmReset"), "Reset is enabled")
        pg.click("#tmReset")
        ok(until(lambda: pg.locator("#tmDays .sday").count() == 7) and pg.is_disabled("#tmReset"), "Reset: 7 days, everybody")
        pg.screenshot(path=f"{OUT}/admin_time.png")
        pg.click("button[data-tab=workflow]")
        ok(until(lambda: pg.input_value("#wfIdle") == "120"), "Workflow: the break limit is 120 s")
        pg.fill("#wfIdle", "200")
        pg.click("#wfIdleSave")
        ok(until(lambda: api.get(B + "/api/admin/workflow").json()["cfg"]["work_idle_s"] == 200), "the break limit is saved")
        pg.fill("#wfIdle", "120")
        pg.click("#wfIdleSave")
        pg.click("button[data-tab=activity]")
        pg.wait_for_selector("#logList li", timeout=10000)
        ok(pg.is_disabled("#logReset"), "Activity: Reset is disabled while nothing is on")
        pg.fill("#logSearch", "uploaded")
        ok(pg.is_enabled("#logReset"), "…enabled when a filter is on")
        pg.click("#logReset")
        ok(pg.input_value("#logSearch") == "" and pg.is_disabled("#logReset"), "…and clears it")
        pg.click("button[data-tab=overview]")
        ok(until(lambda: "worked today" in pg.inner_text("#aTiles").lower()), "Overview: a tile for the time worked today")
        pg.screenshot(path=f"{OUT}/admin_overview.png")
        br.close()

    ok(not errors, f"script errors: {errors[:4]}")
    if bad:
        print("admin page (browser) test: FAILED")
        for b_ in bad:
            print("   ", b_)
        return 1
    print("admin page (browser) test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
