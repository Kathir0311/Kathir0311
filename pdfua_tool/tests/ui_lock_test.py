"""Browser test of "one person at a time in a file" (V4.29): server must be running on :$PORT (default 8000).

Two people (two admins) in two browsers, one file:
* the first one's editor has the file ("You are editing"); the second one's is a view — a bar says who has it,
  the tools that change something are gone, a hotkey that would change a tag says so and changes nothing
* the second one (an admin) takes the file over; the first one sees it by itself and can only look now
* the one who has it lets go ("You are editing" → click); the other is told the file is free and takes it
* closing the editor lets go; the file list shows who has a file and opens it as a view
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
AUTH = {"Kathir": "Kathir@051408", "boss": "secret-boss"}


def main() -> int:
    bad: list = []
    errors: list = []

    def ok(cond, msg):
        if not cond:
            bad.append(msg)

    def until(fn, secs=16, step=0.4):
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
        ctxs, pgs = {}, {}
        for who in AUTH:
            ctxs[who] = br.new_context(viewport={"width": 1500, "height": 950})
            pgs[who] = ctxs[who].new_page()
            pgs[who].on("pageerror", lambda e, w=who: errors.append(f"{w} pageerror: {e}"))
            pgs[who].on("console", lambda m, w=who: errors.append(f"{w} console.{m.type}: {m.text}") if m.type == "error" and "423" not in m.text else None)
        # the second admin is made through the first one's session
        pgs["Kathir"].goto(B + "/login")
        pgs["Kathir"].fill("input[name=username]", "Kathir")
        pgs["Kathir"].fill("input[name=password]", AUTH["Kathir"])
        pgs["Kathir"].click("button[type=submit]")
        pgs["Kathir"].wait_for_selector("#fileInput", state="attached", timeout=20000)
        r = ctxs["Kathir"].request.post(B + "/api/admin/users", data={"username": "boss", "password": AUTH["boss"], "role": "admin"})
        pgs["boss"].goto(B + "/login")
        pgs["boss"].fill("input[name=username]", "boss")
        pgs["boss"].fill("input[name=password]", AUTH["boss"])
        pgs["boss"].click("button[type=submit]")
        pgs["boss"].wait_for_selector("#fileInput", state="attached", timeout=20000)
        # a file
        with open(os.path.join(ROOT, "tests", "corpus", "article.pdf"), "rb") as fh:
            up = ctxs["Kathir"].request.post(B + "/upload", multipart={"profile": "Default", "pdf": {"name": "article.pdf", "mimeType": "application/pdf", "buffer": fh.read()}},
                                             headers={"X-Requested-With": "fetch"})
        jid = up.json()["jobs"][0]
        ok(until(lambda: ctxs["Kathir"].request.get(f"{B}/api/job/{jid}/status").json().get("stage") == "review", 90), "file analysed")
        K, S = pgs["Kathir"], pgs["boss"]

        # ---- the first one has it
        K.goto(f"{B}/job/{jid}")
        K.wait_for_selector("#tree .node", timeout=60000)
        ok(K.is_visible("#lockChip") and "You are editing" in K.inner_text("#lockChip"), f"the first person's chip: {K.inner_text('#lockChip')}")
        ok(not K.evaluate("document.body.classList.contains('lockedview')"), "an editor, not a view")
        ok(K.locator("#lockBar").count() == 0, "no bar")

        # ---- the second one looks
        S.goto(f"{B}/job/{jid}")
        S.wait_for_selector("#tree .node", timeout=60000)
        ok(S.evaluate("document.body.classList.contains('lockedview')"), "the second person's page is a view")
        ok(S.locator("#lockBar").count() == 1 and "Kathir is working on this file now" in S.inner_text("#lockBar"), f"the bar says who has it: {S.inner_text('#lockBar') if S.locator('#lockBar').count() else '-'}")
        ok("View only" in S.inner_text("#lockChip") and "Kathir" in S.inner_text("#lockChip"), f"the chip: {S.inner_text('#lockChip')}")
        ok(not S.is_visible("#buildBtn") and not S.is_visible("#undoBtn"), "the tools that change something are gone")
        ok(S.locator("#lockBar button", has_text="Take over").count() == 1, "an admin may take the file over")
        S.screenshot(path=f"{OUT}/lock_view.png")
        # a hotkey that would change a tag
        m = ctxs["boss"].request.get(f"{B}/api/job/{jid}/model").json()
        pid = next(i for i, n in m["nodes"].items() if n["role"] == "P")
        S.locator(f'#tree .node[data-id="{pid}"]').first.click()
        S.keyboard.press("3")
        time.sleep(0.8)
        ok(S.locator(".toast").count() >= 1 and "working on this file" in " ".join(S.locator(".toast").all_inner_texts()), "a change is refused with the reason")
        ok(ctxs["boss"].request.get(f"{B}/api/job/{jid}/model").json()["nodes"][pid]["role"] == "P", "and nothing changed")
        # the file list
        S.goto(B + "/")
        S.wait_for_selector("#fileTable .lockbadge, .lockbadge", timeout=15000)
        ok("Kathir" in S.inner_text(".lockbadge"), f"the file list shows who has it: {S.inner_text('.lockbadge')}")
        ok("view=1" in S.get_attribute(f'a[href*="/job/{jid}"]', "href"), "and opens it as a view")
        S.goto(f"{B}/job/{jid}")
        S.wait_for_selector("#tree .node", timeout=60000)

        # ---- the admin takes it over
        S.once("dialog", lambda d: d.accept())
        S.click("#lockBar button:has-text('Take over')")
        ok(until(lambda: "You are editing" in S.inner_text("#lockChip"), 8), "the second person has the file now")
        ok(not S.evaluate("document.body.classList.contains('lockedview')"), "and the page is an editor")
        ok(until(lambda: K.evaluate("document.body.classList.contains('lockedview')"), 16), "the first person's page turns into a view by itself")
        ok(until(lambda: "boss is working on this file now" in (K.inner_text("#lockBar") if K.locator("#lockBar").count() else ""), 8), "and says who has it now")
        K.screenshot(path=f"{OUT}/lock_taken_over.png")
        # the first one cannot change anything now
        pid2 = next(i for i, n in m["nodes"].items() if n["role"] == "P" and i != pid)
        K.locator(f'#tree .node[data-id="{pid2}"]').first.click()
        K.keyboard.press("2")
        time.sleep(0.8)
        ok(ctxs["Kathir"].request.get(f"{B}/api/job/{jid}/model").json()["nodes"][pid2]["role"] == "P", "the first person cannot change a tag any more")
        # the second one can
        S.locator(f'#tree .node[data-id="{pid2}"]').first.click()
        S.keyboard.press("2")
        ok(until(lambda: ctxs["boss"].request.get(f"{B}/api/job/{jid}/model").json()["nodes"][pid2]["role"] == "H2", 6), "the one who has it can change a tag")
        # and the first one sees the change by itself
        ok(until(lambda: K.locator("#tree .node .role", has_text="H2").count() >= 1, 16), "the view shows the changes of the one who works")

        # a viewer who is typing (in the search box) is not reloaded under their fingers: a bar offers the changes
        pid3 = next(i for i, n in m["nodes"].items() if n["role"] == "P" and i not in (pid, pid2))
        K.focus("#search")
        S.locator(f'#tree .node[data-id="{pid3}"]').first.click()
        S.keyboard.press("3")
        ok(until(lambda: K.locator(".stalebar").count() == 1 and K.is_visible(".stalebar"), 16), "a viewer who is typing gets a bar, not a reload")
        ok("boss changed the tags" in (K.inner_text(".stalebar") if K.locator(".stalebar").count() else ""), "…that says who changed them")
        K.screenshot(path=f"{OUT}/lock_stale_bar.png")
        K.click(".stalebar button")
        ok(until(lambda: K.locator("#tree .node .role", has_text="H3").count() >= 1 and K.locator(".stalebar").count() == 0, 8), "“Show their changes” shows them")
        K.eval_on_selector("#search", "e => e.blur()")

        # ---- let go, take it again
        S.click("#lockChip")
        ok(until(lambda: "View only" in S.inner_text("#lockChip"), 6), "the chip: let go")
        ok(S.locator("#lockBar button", has_text="Edit this file").count() == 1, "…and offers the file again")
        ok(until(lambda: K.locator("#lockBar button", has_text="Edit this file").count() == 1, 16), "the other person is told the file is free")
        ok("free" in K.inner_text("#lockBar"), f"…in words: {K.inner_text('#lockBar')}")
        K.click("#lockBar button:has-text('Edit this file')")
        ok(until(lambda: "You are editing" in K.inner_text("#lockChip"), 8), "and takes it")
        ok(not K.evaluate("document.body.classList.contains('lockedview')"), "an editor again")

        # ---- leaving lets go
        K.goto(B + "/")
        ok(until(lambda: ctxs["boss"].request.get(f"{B}/api/job/{jid}/lock").json()["free"], 8), "a closed editor lets go of the file")
        ok(until(lambda: S.locator("#lockBar button", has_text="Edit this file").count() == 1 or "free" in (S.inner_text("#lockBar") if S.locator("#lockBar").count() else ""), 16), "the one who was looking is told")
        for who in AUTH:
            ctxs[who].close()
        br.close()

    ok(not errors, f"script errors: {errors[:4]}")
    if bad:
        print("file lock (browser) test: FAILED")
        for b_ in bad:
            print("   ", b_)
        return 1
    print("file lock (browser) test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
