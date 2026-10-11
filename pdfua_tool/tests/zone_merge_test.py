"""Joining split tags across pages, and reading a drawn zone deeply (V4.1).

A list and a table are split by a page break; the second table repeats the
column header. The test joins both, with the fake Ollama answering the AI
checks, then draws a zone and has it read again."""
import os
import sys
import tempfile
import threading

import pymupdf

import tempfile as _tf  # noqa: E402
os.environ.setdefault("PDFUA_SAMPLES", _tf.mkdtemp(prefix="pdfua_samples_"))   # V4.31: Ollama's answers are kept as lessons — not in the project folder
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_ollama                                  # noqa: E402
from core import edit                               # noqa: E402
from core.ai import AIConfig                        # noqa: E402
from core.analyzer import analyze                   # noqa: E402
from core.options import Options                    # noqa: E402

PORT = 11572
HEAD = ["Skill", "Task", "Score"]
ROWS = [[f"Set {i}", "A", "/10"] for i in range(1, 10)]
XS = [40, 160, 240, 340]


def table(p, rows, y0, head=True):
    data = ([HEAD] if head else []) + rows
    for r, row in enumerate(data):
        for c, t in enumerate(row):
            rect = pymupdf.Rect(XS[c], y0 + r * 22, XS[c + 1], y0 + (r + 1) * 22)
            p.draw_rect(rect, color=(0, 0, 0), width=0.6)
            p.insert_textbox(rect + (3, 3, -3, -3), t, fontsize=9,
                             fontname="hebo" if (head and r == 0) else "helv")


def make(path):
    d = pymupdf.open()
    p = d.new_page(width=400, height=560)
    p.insert_text((40, 50), "Scoring Guide", fontsize=16, fontname="hebo")
    y = 80
    for t in ["1. Say the first sound.", "2. Blend the sounds.", "3. Read the whole word."]:
        p.insert_text((40, y), t, fontsize=10)
        y += 16
    table(p, ROWS[:4], 160)
    p.insert_text((40, 530), "2", fontsize=9)
    p = d.new_page(width=400, height=560)
    y = 60
    for t in ["4. Check the meaning.", "5. Record the score."]:
        p.insert_text((40, y), t, fontsize=10)
        y += 16
    table(p, ROWS[4:9], 120)
    p.insert_text((40, 530), "3", fontsize=9)
    d.save(path)
    d.close()


def main() -> int:
    srv = fake_ollama.Server(("127.0.0.1", PORT), fake_ollama.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = AIConfig(base_url=f"http://127.0.0.1:{PORT}", enabled=True,
                   text_model="llama3.2:latest", vision_model="qwen2.5vl:7b", timeout=20)
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "split.pdf")
    make(src)
    bad = []
    m = analyze(src, Options(merge_continued_tables=False))
    tops = [n for n in m.ordered_nodes() if n.parent is None and not n.artifact]
    lists = [n.id for n in tops if n.role == "L"]
    tables = [n.id for n in tops if n.role == "Table"]
    if len(lists) != 2 or len(tables) != 2:
        bad.append(f"expected 2 lists and 2 tables to join, got {len(lists)} / {len(tables)}")
    else:
        before = sum(len(m.nodes[i].children) for i in lists)
        r = edit.merge_parts(m, lists, cfg, "always")
        joined = m.nodes[r["id"]]
        if len(joined.children) != before:
            bad.append(f"list items lost: {before} -> {len(joined.children)}")
        if len(joined.parts) != 1:
            bad.append("the joined list does not keep the second page")
        if not (r.get("ai") or {}).get("same"):
            bad.append(f"AI merge check not used: {r.get('ai')}")

        # the default never calls a model for a list, and still joins
        m2 = analyze(src, Options(merge_continued_tables=False))
        l2 = [n.id for n in m2.ordered_nodes() if n.role == "L" and n.parent is None]
        r2 = edit.merge_parts(m2, l2, cfg)          # ask_ai defaults to "auto"
        if r2.get("ai") is not None:
            bad.append("a list join asked the model although the rules decide it")
        if len(m2.nodes[r2["id"]].children) != before:
            bad.append("the rules-only join lost items")

        # rows live in THead / TBody (table_thead_tbody, on by default)
        rows_before = sum(sum(1 for n in m.descendants(i) if n.role == "TR") for i in tables)
        r = edit.merge_parts(m, tables, cfg, "always")
        t = m.nodes[r["id"]]
        rows = [n.id for n in m.descendants(t.id) if n.role == "TR" and not n.artifact]
        arts = [n.id for n in m.descendants(t.id) if n.role == "Artifact"]
        if not r["merged"][0]["repeat_header"]:
            bad.append("the repeated column header was not recognised")
        if len(arts) != 1:
            bad.append(f"the repeated header should become 1 artifact, got {len(arts)}")
        if len(rows) != rows_before - 1:
            bad.append(f"rows after join: {len(rows)}, expected {rows_before - 1}")
        ths = [c for row in rows for c in m.nodes[row].children if m.nodes[c].role == "TH"]
        if any(not (m.nodes[c].text or "").strip() for c in ths):
            bad.append("an empty TH survived the join")

        # read the table again from its own zone, with the vision model
        nid = edit.rebuild_structured(m, src, t.id, Options(), cfg)
        t2 = m.nodes[nid]
        if t2.role != "Table" or not [n for n in m.descendants(t2.id) if n.role == "TR"]:
            bad.append("rebuilding the table from its zone gave nothing")
        if "AI" not in (t2.note or "") and "header" not in (t2.note or ""):
            bad.append(f"no AI note after the deep zone check: {t2.note!r}")

    # page numbers announced as "Page 5"
    arts = [n.id for n in m.nodes.values() if n.artifact and "page number" in (n.note or "")]
    out = edit.mark_page_numbers(m, arts)
    if out["changed"] != len(arts) or not arts:
        bad.append(f"page numbers: {out}")
    for a in arts:
        n = m.nodes[a]
        if n.artifact or n.role != "P" or not n.actual_text.startswith("Page "):
            bad.append(f"page number not converted: {n.role} {n.artifact} {n.actual_text!r}")
            break
    srv.shutdown()
    if bad:
        print("zone/merge test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("zone/merge test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
