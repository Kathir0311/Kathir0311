"""A sign-in ends when every tab of the app is closed (V4.21.4, static/tabguard.js).

Browser test (server must be running on :$PORT, default 8000):
* signed in: a reload, a link inside the tab and a second tab opened while the
  first is open all stay signed in
* every tab closed, the app opened again: the sign-in page ("the app was
  closed"), the session is gone on the server, the user name is remembered
* sign out in one tab: the other tab goes to the sign-in page too
"""
from __future__ import annotations

import os
import sys
import time

from playwright.sync_api import sync_playwright

PORT = int(os.environ.get("PORT", 8000))
B = f"http://127.0.0.1:{PORT}"
USER = os.environ.get("PDFUA_TEST_USER", "Kathir")
PASS = os.environ.get("PDFUA_TEST_PASS", "Kathir@051408")


def main() -> int:
    bad, errs = [], []
    with sync_playwright() as p:
        br = p.chromium.launch()
        ctx = br.new_context()
        p1 = ctx.new_page()
        p1.on("pageerror", lambda e: errs.append(str(e)))
        p1.goto(B + "/login")
        p1.fill("#un", USER)
        p1.fill("#pw", PASS)
        p1.click("#lgGo")
        p1.wait_for_url(B + "/", timeout=15000)
        p1.reload()
        time.sleep(1.5)
        if "/login" in p1.url:
            bad.append("signed out by a reload")
        p1.goto(B + "/admin")
        time.sleep(1.5)
        if "/login" in p1.url:
            bad.append("signed out by a link in the tab")
        p2 = ctx.new_page()
        p2.goto(B + "/")
        time.sleep(2)
        if "/login" in p2.url:
            bad.append("a second tab was signed out while the first was open")
        p2.close()
        p1.close()
        time.sleep(1)
        p3 = ctx.new_page()
        p3.goto(B + "/admin")
        try:
            p3.wait_for_url("**/login**", timeout=8000)
        except Exception:
            bad.append(f"still signed in after every tab was closed: {p3.url}")
        if "fresh=1" not in p3.url or not p3.locator(".logininfo").count():
            bad.append("no 'the app was closed' notice")
        if p3.evaluate("fetch('/api/me').then(r => r.status)") == 200:
            bad.append("the session is still alive on the server")
        if p3.input_value("#un") != USER:
            bad.append("the user name was not remembered")
        p3.fill("#pw", PASS)
        p3.click("#lgGo")
        time.sleep(2)
        p4 = ctx.new_page()
        p4.goto(B + "/")
        time.sleep(2)
        p3.click("#userBtn")
        p3.click("a[href='/logout']")
        try:
            p4.wait_for_url("**/login**", timeout=8000)
        except Exception:
            bad.append("the other tab stayed signed in after a sign-out")
        br.close()
    if errs:
        bad.append(f"JS errors {errs[:2]}")
    if bad:
        print("tab session test: FAILED")
        for x in bad:
            print("   ", x)
        return 1
    print("tab session test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
