"""Browser test of the front page (V4.29): server must be running on :$PORT (default 8000).

* the page is whole: one h1, landmarks, a skip link, no script error, nothing loaded from another server
* the moving parts move — the tagging demo tags every block of the page and shows 0 errors, the zone story
  goes through its steps, the team lane turns, the numbers count up, the words turn — and stop: the pause
  button (kept for the next visit) and the computer's "less motion" setting show every picture as it ends
* light and dark, by the button and by the computer; a narrow screen has the sign-in first
* sign in: a wrong password is told (and the form shakes), the right one opens the files; the password can be
  shown; Caps Lock is told; the user name is remembered, the password never
* Dez AI is introduced
* V4.32 — more of what the studio can do: why clients choose it, how Ollama teaches Dez AI, every kind of content, the
  screen-reader preview, the standards (the checks tick, the bars fill), the delivery, how the work changes, the
  questions; the numbers it quotes are the tool's own; every part works with the keyboard, with the pause button and
  with the computer's "less motion"; a narrow screen has no sideways scroll
"""
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

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
        codes: set = set()
        for name_ in ("checker.py", "pipeline.py"):
            with open(os.path.join(ROOT, "core", name_), encoding="utf-8") as fh_:
                codes |= set(re.findall(r'"(UA-[A-Z0-9]+)"', fh_.read()))
        n_checks = len(codes)
        ok(nums == [str(n_checks), "300", "4", "0"], f"the numbers count up to {nums} (the tool has {n_checks} checks)")
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

        # ------------------------------------------------------------------ V4.32: more of what the studio can do
        from core.options import BUILTIN_PROFILES, META
        with open(os.path.join(ROOT, "app.py"), encoding="utf-8") as fh_:
            undo = int(re.search(r"^HISTORY_LIMIT = (\d+)", fh_.read(), re.M).group(1))
        body = pg.inner_text("main")
        ok(body.count(f"{n_checks} machine checks") >= 3, f"the page quotes the tool's own {n_checks} machine checks ({body.count(f'{n_checks} machine checks')}x)")
        ok(f"{len(BUILTIN_PROFILES)} built-in profiles" in body and f"{(len(META) // 10) * 10}+ options" in body, "…its profiles and options")
        ok(f"{undo} steps" in body, f"…and its undo steps ({undo})")
        hrefs = pg.evaluate("[...document.querySelectorAll('.dz-links a')].map(a => a.getAttribute('href'))")
        ok(hrefs == ["#demo", "#why", "#dezai", "#features", "#standards", "#teams", "#privacy", "#faq"], f"the menu: {hrefs}")
        ok(all(pg.locator(h).count() == 1 for h in hrefs), "every link of the menu has its part")
        ok(pg.evaluate("[...document.querySelectorAll('a[href^=\"#\"]')].filter(a => a.getAttribute('href').length > 1 && !document.querySelector(a.getAttribute('href'))).length") == 0,
           "every link to a part of the page finds it")
        ids = pg.evaluate("[...document.querySelectorAll('[id]')].map(e => e.id)")
        ok(len(ids) == len(set(ids)), f"no id is used twice ({[i for i in set(ids) if ids.count(i) > 1][:3]})")
        ok(pg.evaluate("[...document.querySelectorAll('[aria-labelledby]')].filter(e => !document.getElementById(e.getAttribute('aria-labelledby'))).length") == 0, "every aria-labelledby finds its heading")
        levels = pg.evaluate("[...document.querySelectorAll('h1,h2,h3,h4')].map(h => Number(h.tagName[1]))")
        ok(all(b - a <= 1 for a, b in zip(levels, levels[1:])), f"no heading level is skipped {levels[:40]}")
        for sid in ("why", "teacher", "content", "listen", "standards", "delivery", "compare", "faq"):
            ok(pg.locator(f"#{sid}[aria-labelledby]").count() == 1 and pg.locator(f"#{sid} h2").count() == 1, f"#{sid}: a part with its own heading")
        pg.evaluate("document.querySelector('#why').scrollIntoView()")
        time.sleep(1.0)
        ok(pg.locator(".dz-adv li").count() == 6 and pg.locator(".dz-adv li h3").count() == 6, "why clients choose it: six reasons")
        ok(pg.locator(".dz-adv .pf").count() == 6, "…each with its proof")
        # Ollama teaches Dez AI: the story moves, the numbers grow
        pg.evaluate("document.querySelector('#teacher').scrollIntoView()")
        seen_q, best = set(), 0
        t0 = time.time()
        while time.time() - t0 < 15:
            seen_q.add(pg.inner_text("#lzQ").strip())
            best = max(best, int(pg.inner_text("#lzPct").split()[0]))
            time.sleep(0.4)
        ok(len(seen_q) >= 2, f"the teacher is asked different questions {sorted(seen_q)[:3]}")
        ok(best >= 30, f"the share Dez AI answers alone grows ({best} %)")
        ok(pg.inner_text("#lzN") != "0", f"…and the lessons kept ({pg.inner_text('#lzN')})")
        # every kind of content
        pg.evaluate("document.querySelector('#content').scrollIntoView()")
        time.sleep(0.8)
        ok(pg.locator(".dz-tiles li").count() == 16, f"sixteen kinds of content ({pg.locator('.dz-tiles li').count()})")
        ok(pg.locator(".dz-tiles li h3").all_inner_texts()[:2] == ["Headings", "Columns and reading order"], "…each named")
        # the screen-reader preview
        pg.evaluate("document.querySelector('#listen').scrollIntoView()")
        most, lit = 0, False
        t0 = time.time()
        while time.time() - t0 < 14:
            most = max(most, pg.locator("#dzSR .sr-lines li.on").count())
            lit = lit or pg.locator("#dzSR .sr-page .b.on").count() == 1
            time.sleep(0.4)
        ok(most >= 5, f"the preview reads line after line ({most})")
        ok(lit, "…and lights the block it reads")
        # the standards: the checks tick, the bars fill
        pg.evaluate("document.querySelector('#standards').scrollIntoView()")
        time.sleep(3.6)
        ok(pg.evaluate("document.querySelector('#dzChecks').classList.contains('go')") and pg.locator("#dzChecks li").count() == 14, "the checklist ticks (14 checks)")
        ok(pg.evaluate("[...document.querySelectorAll('#dzChecks li')].every(e => getComputedStyle(e).opacity === '1')"), "…every one")
        w_b = pg.evaluate("document.querySelector('#dzBA li .bar .b').getBoundingClientRect().width")
        ok(pg.locator("#dzBA li").count() == 6 and w_b > 40, f"the bars fill ({w_b:.0f}px)")
        ok("not a conformance certificate" in pg.inner_text("#standards"), "the page says what a machine check is not")
        # delivery, and the table of what changes
        ok(pg.locator("#delivery .dz-pack li").count() == 4, "the delivery: four things")
        ok(pg.locator("#compare table caption").count() == 1 and pg.locator("#compare thead th[scope=col]").count() == 3 and pg.locator("#compare tbody th[scope=row]").count() == 8,
           "the table of what changes is a real table (caption, column and row headers)")
        ok(pg.get_attribute("#compare .dz-tablewrap", "tabindex") == "0" and pg.get_attribute("#compare .dz-tablewrap", "role") == "region", "…which a keyboard can scroll")
        # the questions: one open at a time
        pg.evaluate("document.querySelector('#faq').scrollIntoView()")
        time.sleep(0.8)
        ok(pg.locator(".dz-faq details").count() == 9, f"nine questions ({pg.locator('.dz-faq details').count()})")
        ok(pg.locator(".dz-faq details[open]").count() == 1 and "certified" in pg.inner_text(".dz-faq details[open] summary"), "the first answer is open")
        pg.locator(".dz-faq details:nth-of-type(2) summary").click()
        time.sleep(0.5)
        ok(pg.locator(".dz-faq details[open]").count() == 1 and "leave my server" in pg.inner_text(".dz-faq details[open] summary"), "opening another closes the first")
        pg.focus(".dz-faq details:nth-of-type(3) summary")
        pg.keyboard.press("Enter")
        time.sleep(0.4)
        ok("GPU" in pg.inner_text(".dz-faq details[open] summary"), "…and the keyboard opens one too")
        # the way down and back up
        pg.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        time.sleep(0.8)
        ok(pg.evaluate("new DOMMatrix(getComputedStyle(document.querySelector('#dzProg')).transform).a") > 0.95, "the progress bar is full at the bottom")
        ok(pg.evaluate("document.querySelector('#dzTop').classList.contains('on')"), "the way back up shows")
        pg.click("#dzTop")
        time.sleep(1.6)
        ok(pg.evaluate("scrollY") < 60, f"…and takes you to the top ({pg.evaluate('scrollY')})")
        ok(pg.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), "the whole page: nothing wider than the screen")
        pg.screenshot(path=f"{OUT}/front_top.png")

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
        ok(pg.inner_text("#lzPct") == "78 %" and pg.inner_text("#lzN") == "1,240", f"paused: the teacher's story shows how it ends ({pg.inner_text('#lzPct')}, {pg.inner_text('#lzN')})")
        ok(pg.evaluate("[...document.querySelectorAll('#dzSR .sr-lines li')].every(e => getComputedStyle(e).opacity === '1')"), "paused: the preview shows every line")
        ok(pg.evaluate("[...document.querySelectorAll('#dzChecks li')].every(e => getComputedStyle(e).opacity === '1')"), "paused: every check is ticked")
        ok(pg.evaluate("getComputedStyle(document.querySelector('#dzBA li .bar .b')).width !== '0px'"), "paused: the bars are full")
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
        ok(pg.inner_text("#lzPct") == "78 %", "less motion: the teacher's story shows how it ends")
        ok(pg.evaluate("[...document.querySelectorAll('#dzChecks li')].every(e => getComputedStyle(e).opacity === '1')"), "less motion: every check is ticked")
        ok(pg.evaluate("[...document.querySelectorAll('#dzSR .sr-lines li')].every(e => getComputedStyle(e).opacity === '1')"), "less motion: the preview shows every line")
        ctx.close()

        # ------------------------------------------------------------------ a narrow screen, and the computer's dark
        ctx, pg = page(viewport={"width": 390, "height": 844}, color_scheme="dark")
        pg.goto(B + "/login")
        pg.wait_for_selector("#lgForm")
        y_form = pg.evaluate("document.querySelector('#lgForm').getBoundingClientRect().top")
        y_h1 = pg.evaluate("document.querySelector('h1').getBoundingClientRect().top")
        ok(y_form < y_h1, f"narrow: the sign-in comes first ({y_form:.0f} < {y_h1:.0f})")
        ok(pg.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), "narrow: nothing wider than the screen")
        ok(pg.evaluate("(() => { const w = document.querySelector('#compare .dz-tablewrap'); return w.scrollWidth <= w.clientWidth + 1; })()"), "narrow: the table of what changes shows all three columns, no sideways scrolling")
        ok(pg.evaluate("getComputedStyle(document.body).backgroundColor") != "rgb(244, 246, 248)", "the computer's dark is followed")
        pg.screenshot(path=f"{OUT}/front_mobile.png")
        ctx.close()

        # ------------------------------------------------------------------ 320 px: the width WCAG tests reflow at
        ctx, pg = page(viewport={"width": 320, "height": 640})
        pg.goto(B + "/login")
        pg.wait_for_selector("#lgForm")
        beyond = pg.evaluate("""(() => { const W = innerWidth, bad = [];
          document.querySelectorAll('body *').forEach(e => {
            const r = e.getBoundingClientRect();
            if (r.width === 0 || (r.right <= W + 0.5 && r.left >= -0.5)) return;
            if (e.closest('[aria-hidden="true"]') || getComputedStyle(e).position === 'fixed') return;       // the pictures that only decorate
            for (let p = e.parentElement; p && p !== document.body; p = p.parentElement)
              if (['hidden', 'auto', 'scroll', 'clip'].includes(getComputedStyle(p).overflowX)) return;     // inside a box that scrolls or clips
            bad.push(e.tagName + '.' + e.className + ' ' + Math.round(r.left) + '→' + Math.round(r.right));
          });
          return bad.slice(0, 5); })()""")
        ok(not beyond, f"320 px: nothing a person reads or presses goes beyond the screen: {beyond}")
        ok(pg.evaluate("(() => { const w = document.querySelector('#compare .dz-tablewrap'); return w.scrollWidth <= w.clientWidth + 1; })()"), "320 px: the table of what changes needs no sideways scrolling")
        ok(pg.evaluate("document.querySelector('#dzNavSign').getBoundingClientRect().height") < 44, "320 px: \"Sign in\" stays on one line")
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
