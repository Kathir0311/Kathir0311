"""The AI helpers added in V4, against the fake Ollama server.

Checks that each endpoint asks the right question, understands the answer and
offers it as a suggestion the editor can apply — no model is needed."""
import os
import sys
import threading

import tempfile as _tf  # noqa: E402
os.environ.setdefault("PDFUA_SAMPLES", _tf.mkdtemp(prefix="pdfua_samples_"))   # V4.31: Ollama's answers are kept as lessons — not in the project folder
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_ollama                               # noqa: E402
import core.ai as ai_mod                          # noqa: E402
from core.ai import (AIConfig, bib_entry_starts, field_label, heading_levels,  # noqa: E402
                     link_description, list_item_starts, math_from_image, table_summary,
                     xref_match, zone_role)

PORT = 11498


def serve():
    srv = fake_ollama.Server(("127.0.0.1", PORT), fake_ollama.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> int:
    srv = serve()
    cfg = AIConfig(base_url=f"http://127.0.0.1:{PORT}", enabled=True,
                   text_model="llama3.2:latest", vision_model="qwen2.5vl:7b", timeout=20)
    bad = []

    heads = [{"i": 1, "level": 1, "text": "Chapter 1", "page": 0},
             {"i": 2, "level": 1, "text": "Scoring", "page": 1}]
    got = heading_levels(cfg, heads)
    if got != [{"i": 2, "level": 2, "why": "sub-point of the heading above"}]:
        bad.append(f"heading_levels: {got}")

    d = link_description(cfg, "click here", "https://x.test/guide.pdf", "Read the guide.")
    if d != "Download the scoring guide (PDF)":
        bad.append(f"link_description: {d!r}")

    lbl = field_label(cfg, "text field", "T1", left="Student", right="", above="")
    if lbl != "Student name":
        bad.append(f"field_label: {lbl!r}")

    sm = table_summary(cfg, "Summary Score Sheet", ["Skill Set", "Task", "Score"], ["1 | A | /26"])
    if "Skill sets 1 to 13" not in sm:
        bad.append(f"table_summary: {sm!r}")

    m = math_from_image(cfg, b"\x89PNG fake", "right triangle")
    if not m or not m["is_math"] or "a squared" not in m["alt"] or "a^{2}" not in m["latex"]:
        bad.append(f"math_from_image: {m}")

    x = xref_match(cfg, "Section 2.1", "as shown in Section 2.1 above", ["Section 2.1 Scoring"])
    if not x or not x["is_reference"] or x["target"] != 1:
        bad.append(f"xref_match: {x}")

    # V4.7: what a drawn zone is, where reference entries and list items start
    z = zone_role(cfg, b"\x89PNG fake", "Assessing the Student", "Font size 18 pt; body 10.5 pt")
    if not z or z["role"] != "H2":
        bad.append(f"zone_role: {z}")
    lines = ["Adams, M. J. (1990). Beginning to read: Thinking and", "learning about print.",
             "Kame'enui, E. J., Carnine, D. W., & Coyne, M. D.", "(2002). Effective teaching strategies.",
             "Moats, L. C. (2005). How spelling supports reading."]
    st = bib_entry_starts(cfg, lines)
    if st != [0, 2, 4]:
        bad.append(f"bib_entry_starts: {st}")
    st = list_item_starts(cfg, ["Read the passage aloud", "and time it.", "Mark each error", "with a slash."])
    if st != [0, 2]:
        bad.append(f"list_item_starts: {st}")

    # the wait hook is entered around every request (the web app lets go of its
    # MuPDF lock there while the model thinks)
    entered = []

    class Hook:
        def __enter__(self):
            entered.append(1)

        def __exit__(self, *a):
            return False
    ai_mod.set_wait_hook(lambda: Hook())
    try:
        zone_role(cfg, b"\x89PNG fake", "x")
    finally:
        ai_mod.set_wait_hook(None)
    if not entered:
        bad.append("the wait hook was not entered around the request")

    srv.shutdown()
    if bad:
        print("AI helpers test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("AI helpers test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
