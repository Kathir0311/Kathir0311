"""Browser test of what people see of zone learning and the load numbers (V4.28): server on :$PORT (default 8000).

A publisher sets its paragraphs apart by a small gap, so the tool takes a page for one zone. A person draws two
zones by hand; the editor then offers “Cut all N” for the other places of the file that look the same (the
card shows where, in the first words of the line below the cut); one click cuts them. Admin → System shows
how busy the server is, Admin → Dez AI has the zones card.
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests.zone_gen import make_pub  # noqa: E402

PORT = int(os.environ.get("PORT", 8000))
B = f"http://127.0.0.1:{PORT}"
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
AUTH = "Basic " + base64.b64encode(b"Kathir:Kathir@051408").decode()


def api(method, path, body=None):
    r = urllib.request.Request(B + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                               headers={"Content-Type": "application/json", "Authorization": AUTH})
    try:
        with urllib.request.urlopen(r) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"http": e.code, "body": e.read()[:200].decode(errors="replace")}


def main() -> int:
    bad: list = []
    errors: list = []

    def ok(cond, msg):
        if not cond:
            bad.append(msg)

    pdf = os.path.join(OUT, "ui_zone_pub.pdf")
    truth = make_pub(pdf, seed=77, pages=2)
    with sync_playwright() as p:
        br = p.chromium.launch(executable_path=os.environ.get("PW_CHROMIUM") or None)
        pg = br.new_context(viewport={"width": 1500, "height": 950}).new_page()
        pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        pg.goto(B + "/login")
        pg.fill("input[name=username]", "Kathir")
        pg.fill("input[name=password]", "Kathir@051408")
        pg.click("button[type=submit]")
        pg.wait_for_selector("#fileInput", state="attached", timeout=20000)
        pg.goto(B + "/")
        pg.set_input_files("#fileInput", pdf)
        pg.click("button[type=submit]")
        pg.wait_for_selector("#tree .node", timeout=90000)
        time.sleep(1.0)
        jid = pg.evaluate("location.pathname").rstrip("/").split("/")[-1]
        total = sum(len(t) for t in truth)

        def zones():
            m = api("GET", f"/api/job/{jid}/model")
            return [n for n in m["nodes"].values() if n["role"] == "P" and len(n.get("text") or "") > 80]
        ok(len(zones()) < total / 2, f"the tool takes the paragraphs for few zones ({len(zones())} of {total})")

        # the big zone of page 1 goes, two paragraphs are drawn
        pg.select_option("#zoom", "1.0")
        pg.fill("#pageNo", "1")
        pg.locator("#pageNo").dispatch_event("change")
        pg.eval_on_selector("#pageNo", "e => e.blur()")
        time.sleep(0.6)
        big = [n for n in zones() if n["page"] == 0]
        if big:
            pg.locator(f'#tree .node[data-id="{big[0]["id"]}"]').first.click()
            pg.keyboard.press("Delete")
            time.sleep(1.0)
        pg.keyboard.press("d")
        ov = pg.locator("#overlay").bounding_box()
        for k in (0, 1):
            x0, y0, x1, y1, _ = truth[0][k]
            pg.mouse.move(ov["x"] + x0 - 2, ov["y"] + y0 - 2)
            pg.mouse.down()
            pg.mouse.move(ov["x"] + x1 + 2, ov["y"] + y1 + 2, steps=6)
            pg.mouse.up()
            time.sleep(0.5)
            pg.keyboard.press("p")
            time.sleep(0.8)
        t0 = time.time()
        while time.time() - t0 < 15 and not pg.is_visible(".simcard"):
            time.sleep(0.4)
        ok(pg.is_visible(".simcard"), "the tool offers the same cut elsewhere")
        if pg.is_visible(".simcard"):
            txt = pg.inner_text(".simcard")
            ok("Learned from your zone fix" in txt and "more place(s)" in txt, f"the card: {txt[:120]}")
            ok("p2" in txt and "“" in txt, "it shows where: the first words of the line below the cut")
            pg.screenshot(path=f"{OUT}/zone_offer.png")
            before = len(zones())
            pg.locator(".simcard button.primary").click()
            time.sleep(2.0)
            ok(len(zones()) > before + 2, f"one click cuts them all ({before} → {len(zones())})")

        # Admin → System and Dez AI
        pg.goto(B + "/admin#system")
        pg.wait_for_selector("#sysInfo tbody tr", timeout=20000)
        time.sleep(1.0)
        txt = pg.inner_text("#sysInfo")
        ok("One PDF at a time" in txt and "In the editor now" in txt and "Files held by an editor" in txt, f"Admin → System: {txt[-260:]!r}")
        pg.goto(B + "/admin#brain")
        time.sleep(2.0)
        ok(pg.locator("#zoneCard").count() == 1, "Admin → Dez AI has the zones card")
        ok("Dez AI" in pg.inner_text(".atabs"), "the admin tab is called Dez AI")
        pg.screenshot(path=f"{OUT}/zone_admin_brain.png")
        br.close()

    ok(not errors, f"script errors: {errors[:4]}")
    if bad:
        print("zone offer (browser) test: FAILED")
        for b_ in bad:
            print("   ", b_)
        return 1
    print("zone offer (browser) test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
