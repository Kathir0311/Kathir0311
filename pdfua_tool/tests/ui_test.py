"""Browser test of the editor UI (server must be running on :$PORT, default 8000)."""
import os
import sys
import time

from playwright.sync_api import sync_playwright

PORT = int(__import__("os").environ.get("PORT", 8000))
B = f"http://127.0.0.1:{PORT}"
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
ROOT = __file__.rsplit("/tests/", 1)[0]


def main():
    errors = []
    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page(viewport={"width": 1500, "height": 950})
        pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        # V4.19: sign in first
        pg.goto(B + "/login")
        pg.fill("input[name=username]", os.environ.get("PDFUA_TEST_USER", "Kathir"))
        pg.fill("input[name=password]", os.environ.get("PDFUA_TEST_PASS", "Kathir@051408"))
        pg.click("button[type=submit]")
        pg.wait_for_selector("#fileInput", state="attached", timeout=20000)
        pg.goto(B + "/")
        pg.set_input_files("#fileInput", f"{ROOT}/tests/corpus/article.pdf")
        pg.click("details.opts summary")
        pg.screenshot(path=f"{OUT}/ui_index.png", full_page=True)
        pg.click("button[type=submit]")
        pg.wait_for_selector("#tree .node", timeout=60000)
        time.sleep(0.8)
        pg.screenshot(path=f"{OUT}/ui_editor.png")
        print("tree rows:", pg.locator("#tree .node").count(), "boxes:", pg.locator("#overlay .box").count())

        # select the figure through the Figures filter
        pg.select_option("#filter", "figures")
        pg.locator("#tree .node", has_text="Figure").first.click()
        time.sleep(0.6)
        print("figure page:", pg.input_value("#pageNo"))
        pg.fill("#tab-props textarea", "Bar chart of quarterly sales; Q4 is highest")
        pg.locator("#tab-props textarea").dispatch_event("change")
        time.sleep(0.6)
        pg.screenshot(path=f"{OUT}/ui_figure.png")

        # hotkey: select first heading and press 2
        pg.select_option("#filter", "all")
        time.sleep(0.3)
        pg.locator("#tree .node", has_text="Abstract").first.click()
        pg.keyboard.press("3")
        time.sleep(0.6)
        role = pg.locator("#tree .node.focus .role").inner_text()
        print("role after hotkey 3:", role)
        pg.keyboard.press("Control+z")
        time.sleep(0.6)
        print("role after undo:", pg.locator("#tree .node.focus .role").inner_text())

        # draw mode: drag a box around the footnote on page 1 and pick Note
        pg.select_option("#zoom", "1.0")
        pg.fill("#pageNo", "1")
        pg.locator("#pageNo").dispatch_event("change")
        pg.eval_on_selector("#pageNo", "e => e.blur()")
        time.sleep(0.5)
        pg.keyboard.press("d")
        ov = pg.locator("#overlay").bounding_box()
        pv = pg.locator("#pageview").bounding_box()
        y0 = min(ov["y"] + 774, pv["y"] + pv["height"] - 40)
        pg.mouse.move(ov["x"] + 45, y0)
        pg.mouse.down()
        pg.mouse.move(ov["x"] + 330, y0 + 16, steps=5)
        pg.mouse.up()
        time.sleep(0.3)
        visible = pg.is_visible("#rolePop")
        print("role popover visible:", visible, "· key hints:",
              pg.locator("#popRoles kbd").count(), "· AI box:", pg.is_visible("#popAi"),
              "· AI box ticked:", pg.is_checked("#popAi"), "(must be False — V4.16)")
        if pg.is_checked("#popAi"):
            errors.append("the zone AI box starts ticked (it must start off — V4.16)")
        if visible:
            pg.keyboard.press("n")              # the key, not a click: N = Note
            time.sleep(1.2)
            print("zone key N made:", pg.locator("#tree .node.focus .role").inner_text(),
                  "· popover closed:", not pg.is_visible("#rolePop"))
        # a second zone, then Escape: the popover closes and nothing is made
        pg.mouse.move(ov["x"] + 45, y0 - 60)
        pg.mouse.down()
        pg.mouse.move(ov["x"] + 330, y0 - 44, steps=5)
        pg.mouse.up()
        time.sleep(0.3)
        if pg.is_visible("#rolePop"):
            pg.keyboard.press("Escape")
            print("Escape closes the popover:", not pg.is_visible("#rolePop"))
        pg.keyboard.press("Escape")

        # reading-order mode on page 3
        pg.fill("#pageNo", "3")
        pg.locator("#pageNo").dispatch_event("change")
        pg.eval_on_selector("#pageNo", "e => e.blur()")
        time.sleep(0.5)
        pg.keyboard.press("o")
        boxes = pg.locator("#overlay .box:not(.art)")
        n = boxes.count()
        boxes.nth(n - 1).click(force=True)
        boxes.nth(0).click(force=True)
        print("order picks:", pg.inner_text("#orderCount"))
        pg.screenshot(path=f"{OUT}/ui_order.png")
        pg.click("#orderApply")
        time.sleep(0.8)

        # issues tab, preview dialog
        pg.click(".tabs button[data-tab=issues]")
        time.sleep(0.4)
        print("issues listed:", pg.locator("#tab-issues .issue").count())
        pg.click("#previewBtn")
        time.sleep(0.3)
        print("preview lines:", pg.locator("#srBody .u").count())
        pg.screenshot(path=f"{OUT}/ui_preview.png")
        pg.keyboard.press("Escape")

        # build
        pg.click("#buildBtn")
        pg.click("#buildGo")
        pg.wait_for_selector("#buildResult .big", timeout=60000)
        print("build result:", pg.inner_text("#buildResult .big"))
        pg.screenshot(path=f"{OUT}/ui_build.png")
        pg.keyboard.press("Escape")
        pg.click(".tabs button[data-tab=checks]")
        time.sleep(1.5)
        print("checks buttons:", [b.strip() for b in pg.locator("#tab-issues button").all_inner_texts()][:8])
        pg.screenshot(path=f"{OUT}/ui_checks.png")

        # the Reader (V4.21.4): what NVDA reads, played with the words shown
        pg.click(".tabs button[data-tab=reader]")
        time.sleep(1)
        n_read = pg.evaluate("window.READER.items.length")
        pg.click("#rdPlay")
        time.sleep(2.5)
        shown = pg.locator("#rdSpeech .w.now, #rdSpeech .w.said").count()
        pg.click("#rdPlay")
        pg.focus("#tab-reader")
        pg.keyboard.press("h")
        time.sleep(0.4)
        said = pg.evaluate("window.READER.items[window.READER.cur].say")
        print("reader:", n_read, "items ·", shown, "word(s) shown ·", said[:50])
        if not n_read or not shown:
            errors.append("the Reader read nothing")
        pg.screenshot(path=f"{OUT}/ui_reader.png")

        # table inspector
        pg.select_option("#zoom", "1.25")
        pg.fill("#pageNo", "2")
        pg.locator("#pageNo").dispatch_event("change")
        pg.click(".tabs button[data-tab=props]")
        pg.select_option("#filter", "tables")
        time.sleep(0.3)
        pg.locator("#tree .node", has_text="Table").first.click()
        time.sleep(0.8)
        pg.screenshot(path=f"{OUT}/ui_table.png")

        # dark mode render
        pg.emulate_media(color_scheme="dark")
        time.sleep(0.3)
        pg.screenshot(path=f"{OUT}/ui_dark.png")
        br.close()
    print("JS errors:", errors or "none")


if __name__ == "__main__":
    main()
