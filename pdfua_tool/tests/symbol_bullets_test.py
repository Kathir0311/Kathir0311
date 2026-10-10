"""Bullets set in a symbol font (V4.19.4): Word's "❑" and "➢" in Wingdings.

The test file (tests/symfont.py) has three lists whose bullets the PDF names
wrong: "" (Private Use), "�" (a CID font with U+FFFD), "¾" (a Latin
letter for the code). Each bullet is printed apart from its text. In the
Default (V4) engine and the Kriyadocs house style:

* the working copy's text layer reads ❑ and ➢ (ToUnicode repaired)
* three lists of 3, 2, 2 items; every Lbl is the bullet (❑ ❑ ❑ / ❑ ❑ / ➢ ➢)
* the item over two lines is one LI ("… peripheral nervous system (PNS).")
* no node reads "�", "", "¾" or a lone bullet
* the built file reads ❑ and ➢, has no checker error, a sound structure
* the tables: every Wingdings / Wingdings 2 / 3 / Symbol / Zapf code used for
  bullets maps to the right character; a ToUnicode with ranges is read
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymupdf  # noqa: E402

from core import compat  # noqa: E402
from core.build import build  # noqa: E402
from core.checker import check  # noqa: E402
from core.options import profile_options  # noqa: E402
from core.pipeline import load_model, process_file  # noqa: E402
from core.symbolfonts import char_for, decode, family, parse_cmap  # noqa: E402
from tests.symfont import make  # noqa: E402
from tests.verify import mc_balance, structure_integrity  # noqa: E402

BAD = ("�", "", "", "¾", "\x01")


def tables(bad: list) -> None:
    want = {("wingdings", 0x71): "❑", ("wingdings", 0xD8): "➢", ("wingdings", 0x6C): "●",
            ("wingdings", 0xA7): "▪", ("wingdings", 0x76): "❖", ("wingdings", 0xFC): "✓",
            ("wingdings", 0xE8): "➔", ("wingdings", 0x9F): "•", ("wingdings", 0xFE): "☑",
            ("wingdings2", 0x50): "✓", ("wingdings2", 0x4F): "✗", ("wingdings2", 0x98): "●",
            ("wingdings3", 0x75): "▶", ("wingdings3", 0x7D): "►",
            ("symbol", 0xB7): "•", ("symbol", 0x61): "α", ("symbol", 0x31): "1", ("symbol", 0xAE): "→",
            ("zapf", 0x6C): "●", ("zapf", 0x71): "❑", ("zapf", 0x34): "✔", ("zapf", 0x75): "◆"}
    for (fam, code), ch in want.items():
        if char_for(fam, code) != ch:
            bad.append(f"{fam} {code:#x} → {char_for(fam, code)!r}, want {ch!r}")
    names = {"ABCDEF+Wingdings-Regular": "wingdings", "Wingdings 3": "wingdings3", "SymbolMT": "symbol",
             "Symbola": "", "ZapfDingbats": "zapf", "Wingdings2": "wingdings2", "Helvetica": "", "Arial": ""}
    for n, f in names.items():
        if family(n) != f:
            bad.append(f"family({n!r}) = {family(n)!r}")
    if decode("wingdings", "") != "❑" or decode("wingdings", "q") != "❑" or decode("symbol", "") != "•":
        bad.append("decode")
    cm = (b"/CIDInit /ProcSet findresource begin begincmap 1 begincodespacerange <00> <FF> endcodespacerange "
          b"2 beginbfrange <41> <43> <0061> <71> <72> [<2751> <2752>] endbfrange "
          b"1 beginbfchar <D8> <00BE> endbfchar endcmap")
    m, two = parse_cmap(cm)
    if two or m.get(0x42) != "b" or m.get(0x72) != "❒" or m.get(0xD8) != "¾":
        bad.append(f"parse_cmap {m}")


def run(profile: str, bad: list) -> None:
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "s.pdf")
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
    t = d[0].get_text()
    d.close()
    if t.count("❑") != 5 or t.count("➢") != 2 or any(b in t for b in BAD):
        bad.append(f"{profile}: working copy reads {[c for c in t if not c.isascii()]}")
    tops = [n for n in m.ordered_nodes() if n.role == "L" and not any(a.role == "L" for a in m.ancestors(n.id))]
    shape = []
    for L in tops:
        lis = [m.nodes[c] for c in L.children if m.nodes[c].role == "LI"]
        lbls = []
        for li in lis:
            lb = [m.nodes[c] for c in li.children if m.nodes[c].role == "Lbl"]
            lbls.append(lb[0].text if lb else "")
        shape.append("".join(lbls))
    if shape != ["❑❑❑", "❑❑", "➢➢"]:
        bad.append(f"{profile}: lists {shape}")
    bodies = [n.text for n in m.nodes.values() if n.role == "LBody"]
    if not any(b.endswith("peripheral nervous system (PNS).") for b in bodies):
        bad.append(f"{profile}: the two-line item {bodies}")
    for n in m.nodes.values():
        tx = (n.text or "").strip()
        if any(b in tx for b in BAD) or (n.role in ("P", "LBody", "LI") and tx in ("❑", "➢", "•")):
            bad.append(f"{profile}: {n.role} reads {tx!r}")
    if any(n.role == "P" and "system (PNS)" in (n.text or "") for n in m.nodes.values()):
        bad.append(f"{profile}: '(PNS)' is a paragraph of its own")
    out = os.path.join(job, "out.pdf")
    build(m, work, out, o)
    d = pymupdf.open(out)
    t = d[0].get_text()
    d.close()
    if t.count("❑") != 5 or t.count("➢") != 2 or any(b in t for b in BAD):
        bad.append(f"{profile}: the built file reads {[c for c in t if not c.isascii()]}")
    errs = [f"{f.code} {f.message}" for f in check(out) if f.severity == "error" and f.code not in ("UA-23",)]
    if errs:
        bad.append(f"{profile}: checker errors {errs}")
    if structure_integrity(out) or mc_balance(out):
        bad.append(f"{profile}: structure {structure_integrity(out)[:2]} {mc_balance(out)[:2]}")


def main() -> int:
    bad: list = []
    tables(bad)
    for prof in ("Default", "Kriyadocs"):
        run(prof, bad)
    if bad:
        print("symbol bullets test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("symbol bullets test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
