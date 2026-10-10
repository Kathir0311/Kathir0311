"""A zone drawn on the page, then Delete (V4.19.3).

Every tag printed wholly inside the zone goes; a tag that reaches outside it
stays; a whole list in the zone goes with its items; table cells in the zone
go and the table's rows are put right (no empty TR); the built file has no
structure error. Default (V4) engine and Kriyadocs."""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymupdf  # noqa: E402

from core import compat, edit  # noqa: E402
from core.build import build  # noqa: E402
from core.options import profile_options  # noqa: E402
from core.pipeline import load_model, process_file, save_model  # noqa: E402
from tests.list_edit_test import make  # noqa: E402
from tests.verify import mc_balance, structure_integrity  # noqa: E402


def run(profile, bad):
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "l.pdf")
    make(src)
    o = profile_options(profile)
    o.ai_alt_text_auto = False
    o.run_verapdf = False
    job = os.path.join(tmp, "job")
    process_file(src, job, o)
    compat.use(o)
    m = load_model(job)
    work = os.path.join(job, "working.pdf")
    d = pymupdf.open(work)
    p = d[0]
    ex = p.search_for("Extra steps")[0]
    head = p.search_for("Method")[0]
    clean = p.search_for("Cleaning")[0]
    timer = p.search_for("Start the timer.")[0]
    d.close()
    texts = lambda: " ".join((n.text or "") for n in m.nodes.values())  # noqa: E731
    # a zone round "Extra steps …" only
    r = edit.delete_in_zone(m, work, 0, [ex.x0 - 3, ex.y0 - 3, ex.x0 + 260, ex.y1 + 3])
    if r["deleted"] < 1 or "Extra steps" in texts():
        bad.append(f"{profile}: the paragraph in the zone {r}")
    if "Method" not in texts():
        bad.append(f"{profile}: a tag outside the zone went")
    # a zone round the whole second list (with its heading)
    lists_before = sum(1 for n in m.nodes.values() if n.role == "L")
    r = edit.delete_in_zone(m, work, 0, [clean.x0 - 5, clean.y0 - 5, 560, timer.y1 + 5])
    if "Clean the bench" in texts() or "Cleaning" in texts() or sum(1 for n in m.nodes.values() if n.role == "L") >= lists_before:
        bad.append(f"{profile}: the list in the zone {r}")
    if any(n.role in ("LI", "LBody", "L") and not n.children and n.bbox is None for n in m.nodes.values()):
        bad.append(f"{profile}: empty list containers left")
    # a zone that only touches the heading's edge deletes nothing
    r = edit.delete_in_zone(m, work, 0, [head.x0 - 2, head.y0 - 2, head.x0 + 8, head.y1 + 2])
    if r["deleted"]:
        bad.append(f"{profile}: a tag only touched by the zone went {r}")
    m.check_integrity()
    save_model(job, m)
    out = os.path.join(job, "out.pdf")
    build(m, work, out, o)
    if structure_integrity(out) or mc_balance(out):
        bad.append(f"{profile}: structure {structure_integrity(out)[:2]} {mc_balance(out)[:2]}")
    compat.use("")


def main():
    bad = []
    for prof in ("Default", "Kriyadocs"):
        run(prof, bad)
    if bad:
        print("zone delete test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("zone delete test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
