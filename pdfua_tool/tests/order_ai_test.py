"""The AI looks at a page's reading order (V4.8).

The page is shown to the vision model with every reading block boxed and
numbered; its answer is checked (every block, once) and offered as a new order
that moves blocks only among their own siblings. A fake Ollama server answers,
so no model is needed: it says blocks 1 and 2 are the wrong way round.
"""
import os
import sys
import tempfile
import threading

import pymupdf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_ollama                                   # noqa: E402
from core import edit                                # noqa: E402
from core.ai import AIConfig                         # noqa: E402
from core.options import Options                     # noqa: E402
from core.pipeline import load_model, process_file   # noqa: E402

PORT = 11497


def page(path):
    d = pymupdf.open()
    p = d.new_page(width=612, height=792)
    p.insert_text((60, 70), "Reading Together", fontsize=20, fontname="hebo")
    p.insert_text((60, 110), "Families can help children read at home every day.", fontsize=11)
    p.insert_text((60, 130), "Short practice works better than long sessions.", fontsize=11)
    p.insert_text((60, 170), "Talk about the pictures before reading the words.", fontsize=11)
    d.save(path)
    d.close()


def main() -> int:
    bad = []
    srv = fake_ollama.Server(("127.0.0.1", PORT), fake_ollama.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = AIConfig(base_url=f"http://127.0.0.1:{PORT}", enabled=True, vision_model="qwen2.5vl:7b",
                   timeout=20)
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "p.pdf")
    page(src)
    job = os.path.join(tmp, "job")
    process_file(src, job, Options(existing_tags="retag", run_verapdf=False))
    m = load_model(job)
    work = os.path.join(job, "working.pdf")
    blocks = edit.page_blocks(m, 0)
    if len(blocks) < 2:
        bad.append(f"{len(blocks)} block(s) on the page")
    png = edit.numbered_page_png(work, 0, blocks)
    if not png.startswith(b"\x89PNG") or len(png) < 2000:
        bad.append("no numbered page picture")
    r = edit.ai_reading_order(m, work, 0, cfg)
    if r["same"] or r["numbers"][:2] != [2, 1] or len(r["order"]) != len(blocks):
        bad.append(f"the AI's answer was not taken: {r}")
    before = [b.id for b in blocks]
    moved = edit.reorder_blocks(m, 0, r["order"])
    after = [b.id for b in edit.page_blocks(m, 0)]
    if moved != 2 or after[:2] != [before[1], before[0]] or after[2:] != before[2:]:
        bad.append(f"applying the order: moved {moved}, {before} -> {after}")
    srv.shutdown()
    if bad:
        print("AI order test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("AI order test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
