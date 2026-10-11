"""Doubtful page-to-page table joins: rules first, AI (fake Ollama) for the rest.

Titles here are plain body-size text (no H1 / no heading at all)."""
import os
import sys
import tempfile
import threading

import pymupdf

import tempfile as _tf  # noqa: E402
os.environ.setdefault("PDFUA_SAMPLES", _tf.mkdtemp(prefix="pdfua_samples_"))   # V4.31: Ollama's answers are kept as lessons — not in the project folder
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.ai import AIConfig                 # noqa: E402
from core.analyzer import analyze            # noqa: E402
from core.options import Options             # noqa: E402
import fake_ollama                           # noqa: E402

HEAD = ["Statement", "Expectation", "Breakout", "Guide", "Book"]
XS = [40, 150, 260, 370, 480, 570]


def page(d, title, tag):
    p = d.new_page()
    if title:
        p.insert_text((40, 85), title, fontsize=8, fontname="helv")     # body size: not a heading
    for r, row in enumerate([HEAD] + [[f"{tag}{r}{c}" for c in range(5)] for r in range(5)]):
        for c, txt in enumerate(row):
            rect = pymupdf.Rect(XS[c], 100 + r * 40, XS[c + 1], 100 + (r + 1) * 40)
            p.draw_rect(rect, color=(0, 0, 0), width=0.6)
            p.insert_textbox(rect + (3, 3, -3, -3), txt, fontsize=8, fontname="hebo" if r == 0 else "helv")
    p.insert_text((40, 830), "footer", fontsize=8)


def make(path, titles):
    d = pymupdf.open()
    for i, t in enumerate(titles):
        page(d, t, f"s{i}")
    d.save(path)


def tables(m):
    return [n for n in m.ordered_nodes() if n.role == "Table"]


def main():
    srv = fake_ollama.Server(("127.0.0.1", 11571), fake_ollama.H)
    srv.calls = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = AIConfig(base_url="http://127.0.0.1:11571", enabled=True)
    with tempfile.TemporaryDirectory() as tmp:
        # 1. title printed once, header repeats -> one table, no AI needed
        f = os.path.join(tmp, "a.pdf")
        make(f, ["Alignment to Grade K", None, None])
        m = analyze(f, Options())
        assert len(tables(m)) == 1, "title only on page 1 should still merge"
        # 2. no title anywhere -> one table
        make(f, [None, None, None])
        assert len(tables(analyze(f, Options()))) == 1
        # 3. plain-text title changes on page 3, same columns: doubtful
        make(f, ["Alignment A", "Alignment A", "Alignment B", "Alignment B"])
        m = analyze(f, Options())
        assert len(tables(m)) == 2, f"rules only: title change starts a new table ({len(tables(m))})"
        assert any("check:" in (t.note or "") for t in tables(m)), "doubtful split must be flagged"
        srv.calls.clear()
        m = analyze(f, Options(ai_table_merge=True), ai_cfg=cfg)
        assert len(tables(m)) == 2, "AI agrees: new table"
        assert srv.calls and all(not c["same"] for c in srv.calls), srv.calls
        assert m.info.get("ai_table_merge"), "AI decisions are logged"
        assert any("AI: new table" in (t.note or "") for t in tables(m))
        # 4. vision variant sends images
        srv.calls.clear()
        analyze(f, Options(ai_table_merge=True, ai_table_merge_vision=True), ai_cfg=cfg)
        assert srv.calls and all(c["vision"] for c in srv.calls), srv.calls
        # 5. AI off -> no calls
        srv.calls.clear()
        analyze(f, Options(ai_table_merge=False), ai_cfg=cfg)
        assert not srv.calls
        # 6. server down -> falls back to rules, no crash (V4.31: what Dez AI was taught by the answers above would be
        #    answered without the server — forget it, so this asks)
        from core import teacher
        teacher.forget("table_merge")
        bad = AIConfig(base_url="http://127.0.0.1:9", enabled=True, timeout=2)
        m = analyze(f, Options(ai_table_merge=True), ai_cfg=bad)
        assert len(tables(m)) == 2 and any("AI table merge check stopped" in w for w in m.warnings)
    srv.shutdown()
    print("AI table merge test: OK")


if __name__ == "__main__":
    main()
