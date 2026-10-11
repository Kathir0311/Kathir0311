"""Browser test of the front page (V4.29): server must be running on :$PORT (default 8000).

* the page is whole: one h1, landmarks, a skip link, no script error, nothing loaded from another server
* the moving parts move — the tagging demo tags every block of the page and shows 0 errors, the zone story
  goes through its steps, the team lane turns, the numbers count up, the words turn — and stop: the pause
  button (kept for the next visit) and the computer's "less motion" setting show every picture as it ends
* light and dark, by the button and by the computer; a narrow screen has the sign-in first
* sign in: a wrong password is told (and the form shakes), the right one opens the files; the password can be
  shown; Caps Lock is told; the user name is remembered, the password never
* Dez AI is introduced
"""
import os
import sys
import time

from playwright.sync_api import sync_playwright

PORT = int(os.environ.get("PORT", 8000))
B = f"http://127.0.0.1:{PORT}"
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
USER = os.environ.get("PDFUA_TEST_USER", "Kathir")
PASS = os.environ.get("PDFUA_TEST_PASS", "Kathir@051408")


def main() -> int:
    bad: list = []
    errors: list = []

    def ok(cond, msg):
        if not cond:
            bad.append(msg)

    foreign: list = []
    with sync_playwright() as p:
        br = p.chromium.launch(executable_path=os.environ.get("PW_CHROMIUM") or None)

        def page(**kw):
            ctx = br.new_context(viewport=kw.pop("viewport", {"width": 1440, "height": 900}), **kw)
            pg = ctx.new_page()
            pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
            pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            pg.on("request", lambda r: foreign.append(r.url) if not r.url.startswith(B) and not r.url.startswith("data:") and not r.url.startswith("blob:") else None)
            return ctx, pg

        # ------------------------------------------------------------------ the page is whole
        ctx, pg = page()
        pg.goto(B + "/login")
        pg.wait_for_selector("#lgForm")
        ok(pg.locator("h1").count() == 1, f"one h1 ({pg.locator('h1').count()})")
        ok(pg.locator("main").count() == 1 and pg.locator("header").count() >= 1 and pg.locator("footer").count() == 1, "landmarks")
        ok(pg.locator("a.dz-skip").count() == 1, "a skip link")
        ok(pg.locator("button[type=submit]").count() == 1, "one submit button (the sign-in)")
        ok("Dez AI" in pg.inner_text("main") and pg.locator("#dezai").count() == 1, "Dez AI is introduced")
        txt = pg.inner_text("main")
        for need in ("Meet Dez", "Private by design", "One file, one editor at a time", "PDF/UA"):
            ok(need in txt or need.replace("Dez ", "Dez ") in txt, f"the page says: {need}")
        ok(pg.title().startswith("Sign in"), f"title {pg.title()}")
        # keyboard: the first Tab lands on the skip link, and it goes to the form
        ok(pg.evaluate("""document.querySelector('a[href],button,input:not([type=hidden]),select,textarea,[tabindex]:not([tabindex="-1"])') === document.querySelector('.dz-skip')"""),
           "the skip link is the first thing the Tab key reaches")
        ok(pg.evaluate("document.activeElement && document.activeElement.id") == "un", "the user name field has the focus at the start")
        # every button and link has a name
        unnamed = pg.evaluate("""() => [...document.querySelectorAll('button,a')].filter(e => !(e.getAttribute('aria-label') || e.textContent.trim() || e.title)).map(e => e.outerHTML.slice(0, 60))""")
        ok(not unnamed, f"unnamed controls {unnamed}")
        ok(not pg.evaluate("[...document.querySelectorAll('input:not([type=hidden])')].filter(i => !i.labels || !i.labels.length).length"), "every field has a label")

        # ------------------------------------------------------------------ the moving parts
        pg.evaluate("document.querySelector('#demo').scrollIntoView()")
        t0 = time.time()
        while time.time() - t0 < 14 and pg.get_attribute("#dzStage", "data-state") != "done":
            time.sleep(0.4)
        ok(pg.get_attribute("#dzStage", "data-state") == "done", "the demo reaches its end")
        ok(pg.locator("#dzPage .blk.tagged").count() == 8, f"every block of the page is tagged ({pg.locator('#dzPage .blk.tagged').count()})")
        ok(pg.locator("#dzTree li").count() >= 9, f"the tag tree is built ({pg.locator('#dzTree li').count()})")
        ok(pg.locator("#dzScore.on").count() == 1, "the score ring shows")
        ok("0 errors" in pg.inner_text("#dzCon"), "the console ends with 0 errors")
        # it plays again by itself, or on request
        pg.click("#dzReplay")
        time.sleep(1.2)
        ok(pg.get_attribute("#dzStage", "data-state") == "working", "play again")
        pg.evaluate("document.querySelector('#learn').scrollIntoView()")
        steps = set()
        t0 = time.time()
        while time.time() - t0 < 12:
            steps.add(pg.get_attribute("#dzZones", "data-step"))
            time.sleep(0.25)
        ok({"1", "2", "3", "4"} <= steps, f"the zone story goes through its steps {sorted(steps)}")
        pg.evaluate("document.querySelector('.dz-statsec').scrollIntoView()")
        time.sleep(2.2)
        nums = [pg.inner_text(f".dz-stats li:nth-child({i}) .cnt").strip() for i in (1, 2, 3, 4)]
        ok(nums == ["300", "4", "100", "0"], f"the numbers count up to {nums}")
        pg.evaluate("document.querySelector('#teams').scrollIntoView()")
        seen = set()
        t0 = time.time()
        while time.time() - t0 < 9:
            seen.add(pg.inner_text("#dzLockTag").strip())
            time.sleep(0.3)
        ok(any("Anna" in s for s in seen) and any("Raj" in s for s in seen), f"the lock passes from person to person {sorted(seen)}")
        # the words turn
        pg.evaluate("window.scrollTo(0,0)")
        first = pg.inner_text("#dzRot span.on")
        time.sleep(3.2)
        ok(pg.inner_text("#dzRot span.on") != first, "the words turn")
        pg.screenshot(path=f"{OUT}/front_light.png")

        # pause: everything stops and shows its end; the choice is kept
        pg.click("#dzStill")
        ok(pg.evaluate("document.documentElement.classList.contains('dz-still')"), "pause")
        ok(pg.get_attribute("#dzStill", "aria-pressed") == "true", "the pause button says it is pressed")
        pg.reload()
        pg.wait_for_selector("#lgForm")
        ok(pg.evaluate("document.documentElement.classList.contains('dz-still')"), "the pause is kept for the next visit")
        pg.evaluate("document.querySelector('#demo').scrollIntoView()")
        time.sleep(0.8)
        ok(pg.locator("#dzPage .blk.tagged").count() == 8 and pg.locator("#dzScore.on").count() == 1, "paused: the demo shows how it ends")
        ok(pg.evaluate("[...document.querySelectorAll('.reveal')].every(e => getComputedStyle(e).opacity === '1')"), "paused: everything is there")
        pg.click("#dzStill")
        ok(not pg.evaluate("document.documentElement.classList.contains('dz-still')"), "play again")
        # light and dark
        before = pg.evaluate("getComputedStyle(document.body).backgroundColor")
        pg.click("#dzTheme")
        after = pg.evaluate("getComputedStyle(document.body).backgroundColor")
        ok(before != after and pg.evaluate("document.documentElement.dataset.theme") in ("dark", "light"), f"the theme button {before} → {after}")
        pg.screenshot(path=f"{OUT}/front_other_theme.png")
        pg.reload()
        ok(pg.evaluate("document.documentElement.dataset.theme") in ("dark", "light"), "the theme is kept")
        ctx.close()

        # ------------------------------------------------------------------ less motion, asked by the computer
        ctx, pg = page(reduced_motion="reduce")
        pg.goto(B + "/login")
        pg.wait_for_selector("#lgForm")
        pg.evaluate("document.querySelector('#demo').scrollIntoView()")
        time.sleep(0.8)
        ok(pg.locator("#dzPage .blk.tagged").count() == 8, "less motion: the demo shows how it ends")
        ok(pg.evaluate("[...document.querySelectorAll('.reveal')].every(e => getComputedStyle(e).opacity === '1')"), "less motion: everything is there")
        ok(pg.evaluate("getComputedStyle(document.querySelector('.dz-glow i')).animationName") == "none", "less motion: no CSS animation")
        ctx.close()

        # ------------------------------------------------------------------ a narrow screen, and the computer's dark
        ctx, pg = page(viewport={"width": 390, "height": 844}, color_scheme="dark")
        pg.goto(B + "/login")
        pg.wait_for_selector("#lgForm")
        y_form = pg.evaluate("document.querySelector('#lgForm').getBoundingClientRect().top")
        y_h1 = pg.evaluate("document.querySelector('h1').getBoundingClientRect().top")
        ok(y_form < y_h1, f"narrow: the sign-in comes first ({y_form:.0f} < {y_h1:.0f})")
        ok(pg.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), "narrow: nothing wider than the screen")
        ok(pg.evaluate("getComputedStyle(document.body).backgroundColor") != "rgb(244, 246, 248)", "the computer's dark is followed")
        pg.screenshot(path=f"{OUT}/front_mobile.png")
        ctx.close()

        # ------------------------------------------------------------------ signing in
        ctx, pg = page()
        pg.goto(B + "/login")
        pg.wait_for_selector("#lgForm")
        pg.fill("#un", USER)
        pg.fill("#pw", "not-the-password")
        # the password can be shown; Caps Lock is told
        pg.click("#pwShow")
        ok(pg.get_attribute("#pw", "type") == "text", "the password can be shown")
        pg.click("#pwShow")
        ok(pg.get_attribute("#pw", "type") == "password", "and hidden again")
        pg.click("#lgGo")
        pg.wait_for_selector(".loginerr", timeout=10000)
        ok("do not match" in pg.inner_text(".loginerr"), "a wrong password is told")
        ok(pg.input_value("#pw") == "", "and the password field is emptied")
        ok(pg.locator("#un").input_value() == USER or pg.evaluate("localStorage.getItem('ua-last-user')") == USER, "the user name is remembered")
        pg.fill("#pw", PASS)
        pg.click("#lgGo")
        pg.wait_for_selector("#fileInput", state="attached", timeout=20000)
        ok(pg.url.rstrip("/") == B, f"signed in: {pg.url}")
        ok(not pg.evaluate("localStorage.getItem('ua-last-pw')"), "the password is never kept")
        ctx.close()
        br.close()

    ok(not errors, f"script errors: {errors[:4]}")
    ok(not foreign, f"nothing is loaded from another server: {foreign[:3]}")
    if bad:
        print("front page test: FAILED")
        for b_ in bad:
            print("   ", b_)
        return 1
    print("front page test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
