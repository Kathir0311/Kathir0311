"""Browser test of Admin → Dez AI → "Ollama teaches Dez AI" (V4.31): server must be running on :$PORT (default 8000); pass the
folder of its sample database as argv[2] (the server's PDFUA_SAMPLES) — the test puts lessons into it, as Ollama's answers would.

* the card shows what was learned: tiles, a row for every kind of question, the level of each, the student's marks
* Study now makes the students (and says so); the level of a task can be held back, the switches and numbers are kept
* a task's lessons can be opened and forgotten
* no script errors
"""
import os
import random
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PORT = int(os.environ.get("PORT", 8000))
B = f"http://127.0.0.1:{PORT}"
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
SAMPLES = sys.argv[2] if len(sys.argv) > 2 else os.path.join(OUT, "samples")
os.environ["PDFUA_SAMPLES"] = SAMPLES

from playwright.sync_api import sync_playwright  # noqa: E402


def main() -> int:
    from core import teacher
    bad: list = []
    errors: list = []

    def ok(cond, msg):
        if not cond:
            bad.append(msg)

    def until(fn, secs=15, step=0.25):
        t0 = time.time()
        while time.time() - t0 < secs:
            try:
                if fn():
                    return True
            except Exception:
                pass
            time.sleep(step)
        return False

    # what Ollama taught: 80 table questions (header iff the first row has no digits), field labels, links
    rnd = random.Random(5)
    words = ["Name", "Year", "Total", "Region", "Score", "Item", "Class", "Grade"]
    sp = teacher.SPECS["table_headers"]
    for _ in range(80):
        if rnd.random() < 0.5:
            r1, r2 = rnd.sample(words, 3), [str(rnd.randint(1, 99)) for _ in range(3)]
        else:
            r1, r2 = [str(rnd.randint(1, 99)) for _ in range(3)], [str(rnd.randint(1, 99)) for _ in range(3)]
        qd = sp.ask(r1, r2)
        hdr = (not re.search(r"\d", " ".join(r1))) and bool(re.search(r"\d", " ".join(r2)))
        teacher.record("table_headers", qd["key"], qd["q"], hdr, label="true" if hdr else "false", model="llama3.2:latest", job="uiJob", client="Client A")
    for i, dest in enumerate(["https://a.test/x.pdf", "https://b.test/y", "https://c.test/z"]):
        qd = teacher.SPECS["links"].ask("click here", dest, "")
        teacher.record("links", qd["key"], qd["q"], f"About {dest[8:]}", model="llama3.2:latest", job="uiJob", client="Client A")
    qd = teacher.SPECS["links"].ask("here", "https://d.test/h", "")
    teacher.record("links", qd["key"], qd["q"], "Handbook", source="human", verdict=1, job="uiJob", client="Client A")
    teacher._count("table_headers", "dez", saved=12.0, n=5)
    teacher._count("table_headers", "ollama", secs=40.0, n=80)
    teacher._count("links", "ollama", secs=9.0, n=3)
    teacher.flush_counts()

    with sync_playwright() as p:
        br = p.chromium.launch(executable_path=os.environ.get("PW_CHROMIUM") or None)
        ctx = br.new_context(viewport={"width": 1500, "height": 1000})
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        pg.on("dialog", lambda d: d.accept())
        pg.goto(B + "/login")
        pg.fill("input[name=username]", "Kathir")
        pg.fill("input[name=password]", "Kathir@051408")
        pg.click("button[type=submit]")
        pg.wait_for_selector("#fileInput", state="attached", timeout=20000)
        api = ctx.request
        pg.goto(B + "/admin")
        # the overview has a tile for it, which opens the Dez AI tab
        ok(until(lambda: "answered by dez ai alone" in pg.inner_text("#aTiles").lower()), "Overview: a tile for what Dez AI answers alone")
        pg.locator("#aTiles .tile", has_text="Dez AI alone").first.click()
        ok(until(lambda: pg.is_visible("#pane-brain")), "…it opens the Dez AI tab")
        pg.wait_for_selector("#tcTable tbody tr.tcrow", timeout=20000)

        # ---------------------------------------------------------------- what the card shows
        n_rows = pg.locator("#tcTable tbody tr.tcrow").count()
        ok(n_rows == len(teacher.SPECS), f"a row for every kind of question ({n_rows} of {len(teacher.SPECS)})")
        ok(until(lambda: "Ollama is teaching" in pg.inner_text("#tcState") or "Ollama is off" in pg.inner_text("#tcState")), f"the state: {pg.inner_text('#tcState')!r}")
        tiles = pg.inner_text("#tcTiles")
        ok(all(w in tiles.lower() for w in ("lessons kept", "dez ai answers alone", "students proven")), f"the tiles: {tiles[:160]!r}")
        row = pg.locator("#tcTable tbody tr.tcrow", has_text="Is row 1 of a table a header").first
        ok("80" in row.inner_text(), f"the lessons of the header question are counted: {row.inner_text()[:160]!r}")
        ok(re.search(r"Dez AI \d+ · Ollama \d+", row.inner_text()) is not None, f"…and who answered: {row.inner_text()[:200]!r}")
        sel = row.locator("select.tclevel")
        ok("Automatic" in sel.evaluate("e => e.options[e.selectedIndex].text"), "its level is automatic")
        pg.screenshot(path=f"{OUT}/teacher_card_before.png")

        # ---------------------------------------------------------------- study now
        pg.click("#tcTrain")
        ok(until(lambda: "studying" in pg.inner_text("#tcState") or pg.locator("#tcTable tbody tr.tcrow", has_text="Is row 1 of a table a header").first.locator(".chip.ok").count() == 1, 20), "study now starts")
        ok(until(lambda: pg.locator("#tcTable tbody tr.tcrow", has_text="Is row 1 of a table a header").first.locator(".chip.ok").count() == 1, 60),
           "the student is proven after studying")
        row = pg.locator("#tcTable tbody tr.tcrow", has_text="Is row 1 of a table a header").first
        ok("proven" in row.inner_text() and "right on unseen" in row.inner_text(), f"the student's marks: {row.inner_text()[:240]!r}")
        ok(until(lambda: "Automatic (now: Solo)" in row.locator("select.tclevel").evaluate("e => e.options[e.selectedIndex].text")), "…and its level is now solo")
        ok(until(lambda: pg.locator("#tcTiles .tile", has_text="Students proven").inner_text().strip().startswith("1")), f"the tile counts it: {pg.inner_text('#tcTiles')[:200]!r}")
        pg.screenshot(path=f"{OUT}/teacher_card_after.png")

        # ---------------------------------------------------------------- hold a task back, switches, numbers
        lrow = pg.locator("#tcTable tbody tr.tcrow", has_text="Link: what to say").first
        lrow.locator("select.tclevel").select_option("off")
        ok(until(lambda: api.get(B + "/api/admin/teacher").json()["settings"]["levels"].get("links") == "off"), "a task can be switched off")
        pg.reload()
        pg.click("button[data-tab=brain]")
        pg.wait_for_selector("#tcTable tbody tr.tcrow", timeout=20000)
        lrow = pg.locator("#tcTable tbody tr.tcrow", has_text="Link: what to say").first
        ok(lrow.locator("select.tclevel").input_value() == "off", "…and it stays off after a reload")
        lrow.locator("select.tclevel").select_option("auto")
        ok(until(lambda: "links" not in api.get(B + "/api/admin/teacher").json()["settings"]["levels"]), "…and back to automatic")
        pg.locator("[data-teach=solo]").scroll_into_view_if_needed(); pg.evaluate("() => window.scrollBy(0, -140)"); pg.locator("label.switch", has=pg.locator("[data-teach=solo]")).click()
        ok(until(lambda: api.get(B + "/api/admin/teacher").json()["settings"]["solo"] is False), "answering alone can be switched off")
        pg.locator("label.switch", has=pg.locator("[data-teach=solo]")).click()
        ok(until(lambda: api.get(B + "/api/admin/teacher").json()["settings"]["solo"] is True), "…and on")
        pg.fill("[data-teachn=audit]", "7")
        pg.dispatch_event("[data-teachn=audit]", "change")
        pg.fill("[data-teachn=agree]", "90")
        pg.dispatch_event("[data-teachn=agree]", "change")
        ok(until(lambda: (lambda s: s["audit"] == 7 and abs(s["agree"] - 0.9) < 1e-9)(api.get(B + "/api/admin/teacher").json()["settings"])), "the audit and the agreement are kept")

        # ---------------------------------------------------------------- the lessons of a task
        lrow = pg.locator("#tcTable tbody tr.tcrow", has_text="Link: what to say").first
        lrow.locator("button[aria-expanded]").click()
        ok(until(lambda: pg.locator("#tcTable tr.tcmore .tclesson").count() >= 3), "the lessons of a task open")
        ok("person" in pg.inner_text("#tcTable tr.tcmore") and "Ollama" in pg.inner_text("#tcTable tr.tcmore"), "…each says who taught it")
        pg.screenshot(path=f"{OUT}/teacher_lessons.png")
        pg.locator("#tcTable tbody tr.tcrow", has_text="Link: what to say").first.locator("button.danger").click()    # confirm → forget all
        ok(until(lambda: next(t for t in api.get(B + "/api/admin/teacher").json()["tasks"] if t["task"] == "links")["lessons"] == 0), "a task's lessons can be forgotten")
        br.close()

    ok(not errors, f"script errors: {errors[:4]}")
    if bad:
        print("teacher card (browser) test: FAILED")
        for b_ in bad:
            print("   ", b_)
        return 1
    print("teacher card (browser) test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
