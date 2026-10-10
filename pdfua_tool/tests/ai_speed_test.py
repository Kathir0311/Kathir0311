"""The AI is asked once, quickly, and never while the job is locked (V4.16).

Drawing a zone with "use the AI for this zone" used to freeze the whole editor
for a minute: the model was loaded from disk again for every question, the
picture sent to it was the full crop at 2.2x, the answer was not capped, and the
job stayed locked for the whole wait. This checks each of those:

* every request carries keep_alive (the model stays in memory) and a cap on how
  long the answer may be, and a thinking model is told not to think;
* the picture is shrunk to the size set in Settings;
* what a model can do is asked for once per model, not every minute;
* the answers are fetched BEFORE the edit (edit.zone_ai_answers) and the edit
  itself asks the model nothing — so the rest of the editor keeps answering.
"""
import base64
import os
import sys
import tempfile
import threading

import pymupdf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_ollama                                      # noqa: E402
import core.ai as ai_mod                                # noqa: E402
from core import edit                                   # noqa: E402
from core.ai import AIConfig, shrink_png, zone_role      # noqa: E402
from core.options import Options                        # noqa: E402
from core.pipeline import load_model, process_file      # noqa: E402

PORT = 11493


def page_png(w=2400, h=900) -> bytes:
    d = pymupdf.open()
    p = d.new_page(width=w / 2, height=h / 2)
    p.insert_text((20, 40), "a picture of a page", fontsize=18)
    png = p.get_pixmap(matrix=pymupdf.Matrix(2, 2)).tobytes("png")
    d.close()
    return png


def side(png: bytes) -> int:
    pix = pymupdf.Pixmap(png)
    return max(pix.width, pix.height)


def make(path: str) -> None:
    """A page with a title, a ruled table and a paragraph."""
    d = pymupdf.open()
    p = d.new_page(width=420, height=520)
    p.insert_text((40, 50), "Results of the survey", fontsize=17)
    rules = [70, 90, 110, 130]
    for y in rules:
        p.draw_line((40, y), (380, y), width=0.6)
    for k, row in enumerate([("Group", "Before", "After"), ("A", "12", "19"), ("B", "9", "14")]):
        for x, t in zip((46, 170, 290), row):
            p.insert_text((x, rules[k] + 14), t, fontsize=9)
    y = 180
    for _ in range(12):
        p.insert_text((40, y), "the ordinary running text of the report, line after line", fontsize=9)
        y += 12
    d.save(path)
    d.close()


def main() -> int:
    bad: list = []
    srv = fake_ollama.Server(("127.0.0.1", PORT), fake_ollama.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.seen.clear()
    cfg = AIConfig(base_url=f"http://127.0.0.1:{PORT}", enabled=True,
                   text_model="llama3.1:8b", vision_model="qwen2.5vl:7b", timeout=30)

    # ---- every request: the model stays loaded, the answer is capped
    zone_role(cfg, page_png(), "Results of the survey", "18 pt bold")
    gen = [r for r in srv.seen if r["path"] == "/api/generate"]
    if not gen:
        print("ai speed test: FAILED — the fake server was not asked anything")
        return 1
    q = gen[-1]
    if q.get("keep_alive") != "30m":
        bad.append(f"no keep_alive on the request: {q.get('keep_alive')!r}")
    cap = (q.get("options") or {}).get("num_predict")
    if not cap or cap > 1000:
        bad.append(f"the answer is not capped: num_predict={cap}")
    if "think" in q:
        bad.append("thinking switched off for a model that does not think")
    # the picture is no bigger than Settings asks for
    got = side(base64.b64decode(q["images"][0]))
    if got > cfg.max_image_px:
        bad.append(f"the picture sent to the model is {got} px, over {cfg.max_image_px}")

    # a thinking model is told not to think about a one-line question
    srv.seen.clear()
    think_cfg = AIConfig(base_url=cfg.base_url, enabled=True, vision_model="qwen2.5vl:7b",
                         text_model="qwen3:8b", timeout=30)
    fake_ollama.MODELS.append("qwen3:8b")
    ai_mod._CACHE.clear()
    try:
        ai_mod.heading_levels(think_cfg, [{"i": 1, "level": 1, "text": "One", "page": 0}])
        q2 = [r for r in srv.seen if r["path"] == "/api/generate"][-1]
        if q2.get("think") is not False:
            bad.append(f"a thinking model was not told to skip thinking: {q2.get('think')!r}")
    finally:
        fake_ollama.MODELS.remove("qwen3:8b")
        ai_mod._CACHE.clear()

    # ---- the picture: shrunk by whole halvings until it fits
    big = page_png(2400, 900)
    if side(big) <= 1100:
        bad.append("the test picture is not big enough to tell anything")
    if side(shrink_png(big, 1100)) > 1100:
        bad.append(f"shrink_png left {side(shrink_png(big, 1100))} px")
    small = page_png(600, 400)
    if shrink_png(small, 1100) != small:
        bad.append("a picture that already fits was re-encoded")

    # ---- what a model can do is asked once, not every minute
    ai_mod._CACHE.clear()
    ai_mod._CAPS.clear()
    srv.seen.clear()
    ai_mod.installed_models(cfg, refresh=True)
    ai_mod.installed_models(cfg, refresh=True)
    shows = [r for r in srv.seen if r["path"] == "/api/show"]
    names = {r.get("model") for r in shows}
    if len(shows) != len(names):
        bad.append(f"/api/show asked {len(shows)} times for {len(names)} model(s)")

    # ---- the answers come before the edit, the edit asks nothing
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "r.pdf")
    make(src)
    o = Options()
    o.ai_alt_text_auto = False
    o.run_verapdf = False
    job = os.path.join(tmp, "job")
    res = process_file(src, job, o)
    if res.get("status") != "done":
        bad.append(f"the test file did not analyse: {res.get('error')}")
    else:
        work = os.path.join(job, "working.pdf")
        d = pymupdf.open(work)
        page = d[0]
        title = page.search_for("Results of the survey")[0]
        whole = edit._zone_png(page, [0, 0, page.rect.width, page.rect.height],
                               max_px=edit._ai_px(cfg))
        d.close()
        if side(whole) > cfg.max_image_px:
            bad.append(f"a whole-page zone is sent at {side(whole)} px")
        # display coordinates of the zones (the editor sends these)
        head = [title.x0 - 4, title.y0 - 4, title.x1 + 4, title.y1 + 4]
        table = [36, 62, 384, 136]
        for name, rect, role, asked in (("Auto", head, "Auto", 1), ("Table", table, "Table", 1)):
            m = load_model(job)
            srv.seen.clear()
            answers = edit.zone_ai_answers(m, work, 0, rect, role, o, cfg)
            before = len([r for r in srv.seen if r["path"] == "/api/generate"])
            if before < asked or not answers:
                bad.append(f"{name}: the zone was not read by the model ({before} question(s), {answers})")
            srv.seen.clear()
            try:
                nid = edit.create_from_rect(m, work, 0, rect, role, True, o,
                                            deep=True, ai_cfg=cfg, ai_answers=answers)
            except edit.EditError as exc:
                bad.append(f"{name}: the zone could not be tagged ({exc})")
                continue
            after = [r for r in srv.seen if r["path"] == "/api/generate"]
            if after:
                bad.append(f"{name}: the edit itself asked the model {len(after)} time(s) "
                           "— the job is locked while it waits")
            got = m.nodes[nid].role
            want = "H2" if role == "Auto" else "Table"
            if got != want:
                bad.append(f"{name}: the zone became {got}, not {want}")
            if role == "Auto" and "by the AI" not in (m.nodes[nid].note or ""):
                bad.append(f"Auto: the note does not say the AI chose it ({m.nodes[nid].note!r})")

    srv.shutdown()
    if bad:
        print("ai speed test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("ai speed test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
