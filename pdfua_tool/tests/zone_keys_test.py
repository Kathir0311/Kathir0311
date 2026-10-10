"""A zone drawn by hand, then a key (V4.7).

In the editor a zone is drawn and a key says what it is: T table, 1–6 a
heading, F figure, L list, I (or Enter) "let the tool decide". This checks the
server side of that:

  * "Auto" without the AI: the rules pick heading / paragraph / table / list /
    figure / page number from what is printed in the zone
  * "Auto" with the AI on: the vision model's answer is used
  * with the AI on, a drawn Table is read deeply and the model's header rows
    are applied, a drawn Figure gets its alt text, a drawn Formula its words
  * a model that cannot be reached never breaks the zone: the tag is made and
    the note says the AI was unavailable

The model is replaced by canned answers, so no Ollama server is needed.
"""
import os
import sys
import tempfile

import pymupdf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core.ai as ai                                 # noqa: E402
from core import edit                                # noqa: E402
from core.ai import AIConfig, OllamaError            # noqa: E402
from core.options import Options                     # noqa: E402
from core.pipeline import load_model, process_file   # noqa: E402

TABLE = (60, 300, 420, 372)        # display space: x0, y0, x1, y1
ROWS = [["Skill set", "Score", "Date"], ["Short vowels", "18", "May 2"],
        ["Blends", "15", "May 9"], ["Digraphs", "20", "May 16"]]


def page_pdf(path: str) -> None:
    d = pymupdf.open()
    p = d.new_page(width=612, height=792)
    p.insert_text((60, 80), "Assessing the Student", fontsize=22, fontname="hebo")
    y = 120
    for t in ("Tamir is a fifth-grade student. The assessment was given to the",
              "whole class at the start of the school year to find the students",
              "who should be assessed one to one."):
        p.insert_text((60, y), t, fontsize=10.5, fontname="helv")
        y += 13
    y = 190
    for t in ("1. Give the student the task card.", "2. Read the directions aloud.",
              "3. Record each answer on the sheet."):
        p.insert_text((60, y), t, fontsize=10.5, fontname="helv")
        y += 16
    x0, y0, x1, y1 = TABLE
    h = (y1 - y0) / len(ROWS)
    cols = [x0, 200, 310, x1]
    for k in range(len(ROWS) + 1):
        p.draw_line((x0, y0 + k * h), (x1, y0 + k * h), width=0.8)
    for c in cols:
        p.draw_line((c, y0), (c, y1), width=0.8)
    for r, row in enumerate(ROWS):
        for c, t in enumerate(row):
            p.insert_text((cols[c] + 4, y0 + r * h + 12), t, fontsize=10,
                          fontname="hebo" if r == 0 else "helv")
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 40), False)
    pix.set_rect(pix.irect, (40, 120, 200))
    p.insert_image(pymupdf.Rect(60, 420, 300, 580), pixmap=pix)
    p.insert_text((60, 640), "a² + b² = c²", fontsize=14, fontname="helv")
    y = 690
    for t in ("Read the passage aloud to the student", "and time the reading.",
              "Mark each word read wrongly", "with a slash."):
        p.insert_text((60, y), t, fontsize=10.5, fontname="helv")
        y += 13
    p.insert_text((300, 770), "12", fontsize=9, fontname="helv")
    d.save(path)
    d.close()


ZONES = {
    "heading": (55, 58, 340, 88),
    "paragraph": (55, 108, 420, 152),
    "list": (55, 178, 300, 230),
    "table": (TABLE[0] - 4, TABLE[1] - 4, TABLE[2] + 4, TABLE[3] + 4),
    "figure": (55, 415, 305, 585),
    "formula": (55, 625, 200, 648),
    "pagenumber": (290, 758, 320, 776),
    "plainlist": (55, 678, 400, 735),
}


def fresh(job):
    return load_model(job), os.path.join(job, "working.pdf")


def main() -> int:
    bad: list = []
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "zones.pdf")
    page_pdf(src)
    job = os.path.join(tmp, "job")
    r = process_file(src, job, Options(existing_tags="retag", run_verapdf=False))
    if r.get("status") != "done":
        print("zone keys test: FAILED — the first build failed", r.get("error"))
        return 1

    # 1. Auto without the AI: the rules decide
    want = {"heading": ("H1", "H2", "H3"), "paragraph": ("P",), "list": ("L",),
            "table": ("Table",), "figure": ("Figure",), "formula": ("Formula",),
            "pagenumber": ("Artifact",), "plainlist": ("P",)}
    for name, rect in ZONES.items():
        m, work = fresh(job)
        try:
            nid = edit.create_from_rect(m, work, 0, rect, "Auto", True, Options())
        except edit.EditError as exc:
            bad.append(f"auto {name}: {exc}")
            continue
        n = m.nodes[nid]
        got = "Artifact" if n.artifact else n.role
        if got not in want[name]:
            bad.append(f"auto {name}: tagged {got}, expected {want[name]} ({n.note})")
        if "chosen for the zone" not in (n.note or ""):
            bad.append(f"auto {name}: the note does not say why ({n.note!r})")
        if name == "formula" and not (n.alt or "").strip():
            bad.append("auto formula: no words for the formula")

    # 2. the AI on: canned answers stand in for the model
    calls = []
    real = (ai.zone_role, ai.zone_structure, ai.alt_text, ai.math_from_image, ai.list_item_starts)

    def fake_role(cfg, png, text="", hints=""):
        calls.append("zone_role")
        if not png.startswith(b"\x89PNG"):
            raise AssertionError("not a picture")
        return {"role": "H2", "level": 2, "why": "a section title"}

    def fake_structure(cfg, png, role, text=""):
        calls.append("zone")
        return {"kind": "table", "rows": 4, "columns": 3, "header_rows": 1,
                "header_column": False, "ordered": False, "why": "ruled grid"}

    def fake_alt(cfg, png, context=""):
        calls.append("alt")
        return "Blue rectangle standing for the class score band"

    def fake_math(cfg, png, context=""):
        calls.append("math")
        return {"is_math": True, "decorative": False,
                "alt": "a squared plus b squared equals c squared", "latex": "a^2+b^2=c^2"}

    def fake_items(cfg, lines):
        calls.append("items")
        return [0, 2] if len(lines) == 4 else None

    ai.zone_role, ai.zone_structure, ai.alt_text, ai.math_from_image, ai.list_item_starts = \
        fake_role, fake_structure, fake_alt, fake_math, fake_items
    try:
        cfg = AIConfig()
        m, work = fresh(job)
        nid = edit.create_from_rect(m, work, 0, ZONES["paragraph"], "Auto", True, Options(), ai_cfg=cfg)
        if m.nodes[nid].role != "H2" or "AI" not in m.nodes[nid].note:
            bad.append(f"AI auto: {m.nodes[nid].role} / {m.nodes[nid].note!r}")

        m, work = fresh(job)
        nid = edit.create_from_rect(m, work, 0, ZONES["table"], "Table", True, Options(), ai_cfg=cfg)
        t = m.nodes[nid]
        ths = [d for d in m.descendants(nid) if d.role == "TH"]
        if t.role != "Table" or len(ths) != 3:
            bad.append(f"AI table: {t.role}, {len(ths)} header cell(s), expected 3 ({t.note})")
        if "zone" not in calls:
            bad.append("AI table: the vision model was not asked (deep read is on with the AI)")

        m, work = fresh(job)
        nid = edit.create_from_rect(m, work, 0, ZONES["figure"], "Figure", True, Options(), ai_cfg=cfg)
        if not m.nodes[nid].alt.startswith("Blue rectangle"):
            bad.append(f"AI figure: alt {m.nodes[nid].alt!r}")

        m, work = fresh(job)
        nid = edit.create_from_rect(m, work, 0, ZONES["formula"], "Formula", True, Options(), ai_cfg=cfg)
        if m.nodes[nid].alt != "a squared plus b squared equals c squared":
            bad.append(f"AI formula: alt {m.nodes[nid].alt!r}")

        m, work = fresh(job)
        nid = edit.create_from_rect(m, work, 0, ZONES["plainlist"], "L", True, Options(), ai_cfg=cfg)
        items = [d for d in m.descendants(nid) if d.role == "LBody"]
        if [i.text for i in items] != ["Read the passage aloud to the student and time the reading.",
                                       "Mark each word read wrongly with a slash."]:
            bad.append(f"AI list items: {[i.text for i in items]}")

        # 3. the model cannot be reached: the zone is still tagged
        def down(*a, **k):
            raise OllamaError("cannot reach Ollama at http://localhost:11434: refused")
        ai.zone_role = ai.zone_structure = ai.alt_text = ai.math_from_image = down
        for name, role, expect in (("figure", "Figure", "Figure"), ("table", "Table", "Table"),
                                   ("heading", "Auto", None)):
            m, work = fresh(job)
            try:
                nid = edit.create_from_rect(m, work, 0, ZONES[name], role, True, Options(), ai_cfg=cfg)
            except edit.EditError as exc:
                bad.append(f"AI down, {name}: {exc}")
                continue
            n = m.nodes[nid]
            if expect and n.role != expect:
                bad.append(f"AI down, {name}: {n.role}")
            if role == "Auto" and not n.role.startswith("H"):
                bad.append(f"AI down, auto heading: fell back to {n.role}")
            if name != "table" and "unavailable" not in (n.note or "") and "failed" not in (n.note or ""):
                bad.append(f"AI down, {name}: the note does not say so ({n.note!r})")
    finally:
        ai.zone_role, ai.zone_structure, ai.alt_text, ai.math_from_image, ai.list_item_starts = real

    if bad:
        print("zone keys test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("zone keys test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
