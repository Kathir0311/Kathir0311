"""A tag drawn inside a tag (V4.21.4): a zone over a few words of a paragraph
(or a heading) made a Span / Quote … becomes a tag INSIDE it, in reading order.

* "P > Span" over "brown fox": the Span is the P's child; in the built file the
  P reads "The quick " · Span "brown fox" · " jumps over …" in that order
* a second Span after it and a third before it land in reading order
* on a paragraph of two lines, a Span on line 2 comes after one on line 1
* a Quote inside a heading; a zone over the whole paragraph still replaces it
* the "inside" switch off: the old way (a tag of its own beside the paragraph)
* the built file is sound
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pikepdf  # noqa: E402
import pymupdf  # noqa: E402

from core import compat, edit  # noqa: E402
from core.build import build  # noqa: E402
from core.options import profile_options  # noqa: E402
from core.pipeline import load_model, process_file  # noqa: E402
from tests.verify import mc_balance, structure_integrity  # noqa: E402

LINE1 = "The quick brown fox jumps over the lazy dog."
LINE2 = "A second line closes the same paragraph here."


def make(path: str) -> None:
    d = pymupdf.open()
    p = d.new_page()
    p.insert_text((72, 80), "Animals at play", fontsize=20)
    p.insert_text((72, 130), LINE1, fontsize=12)
    p.insert_text((72, 145), LINE2, fontsize=12)
    p.insert_text((72, 200), "Another paragraph follows the first one.", fontsize=12)
    d.save(path)
    d.close()


def zone(page, words: str, pad: float = 1.0) -> list:
    r = page.search_for(words)[0]
    return [r.x0 - pad, r.y0 - pad, r.x1 + pad, r.y1 + pad]


def mcid_texts(pdf_path: str) -> dict:
    """MCID → the text shown inside it, on page 1."""
    pdf = pikepdf.open(pdf_path)
    page = pdf.pages[0]
    out: dict = {}
    stack: list = []
    for operands, op in pikepdf.parse_content_stream(page):
        o = str(op)
        if o in ("BDC", "BMC"):
            mc = None
            if o == "BDC" and len(operands) > 1:
                props = operands[1]
                if isinstance(props, pikepdf.Name):
                    props = page.Resources.Properties[props]
                if isinstance(props, pikepdf.Dictionary) and "/MCID" in props:
                    mc = int(props.MCID)
            stack.append(mc)
        elif o == "EMC":
            if stack:
                stack.pop()
        elif o in ("Tj", "TJ", "'", '"'):
            cur = next((m for m in reversed(stack) if m is not None), None)
            if cur is None:
                continue
            arr = operands[-1]
            parts = arr if isinstance(arr, pikepdf.Array) else [arr]
            for x in parts:
                if isinstance(x, pikepdf.String):
                    out[cur] = out.get(cur, "") + bytes(x).decode("latin-1")
    return out


def k_sequence(pdf_path: str, words: str) -> list:
    """The reading sequence of the P that holds `words`: text and child tags."""
    texts = mcid_texts(pdf_path)
    pdf = pikepdf.open(pdf_path)

    def seq(e) -> list:
        k = e.get("/K")
        items = k if isinstance(k, pikepdf.Array) else [k]
        out = []
        for x in items:
            if isinstance(x, int):
                out.append(texts.get(int(x), ""))
            elif isinstance(x, pikepdf.Dictionary) and x.get("/Type") == "/MCR":
                out.append(texts.get(int(x.MCID), ""))
            elif isinstance(x, pikepdf.Dictionary) and "/S" in x:
                out.append((str(x.S)[1:], "".join(t if isinstance(t, str) else t[1] for t in seq(x))))
        return out

    found = []

    def walk(e):
        if not isinstance(e, pikepdf.Dictionary):
            return
        if e.get("/S") in ("/P", "/H1", "/H2") :
            s_ = seq(e)
            flat = "".join(t if isinstance(t, str) else t[1] for t in s_)
            if words in flat.replace("  ", " "):
                found.append(s_)
                return
        k = e.get("/K")
        for x in (k if isinstance(k, pikepdf.Array) else [k]):
            walk(x)
    walk(pdf.Root.StructTreeRoot)
    return found[0] if found else []


def main() -> int:
    bad = []
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "p.pdf")
    make(src)
    o = profile_options("Default")
    o.ai_alt_text_auto = False
    o.run_verapdf = False
    o.use_samples = False
    job = os.path.join(tmp, "job")
    process_file(src, job, o)
    compat.use(o)
    m = load_model(job)
    work = os.path.join(job, "working.pdf")
    doc = pymupdf.open(work)
    page = doc[0]
    para = next(n for n in m.nodes.values() if n.role == "P" and "quick brown" in (n.text or ""))
    s1 = edit.create_from_rect(m, work, 0, zone(page, "brown fox"), "Span", opts=o)
    if m.nodes[s1].parent != para.id:
        bad.append(f"the Span is not inside the P: parent {m.nodes[s1].parent}")
    s2 = edit.create_from_rect(m, work, 0, zone(page, "lazy dog"), "Span", opts=o)
    s0 = edit.create_from_rect(m, work, 0, zone(page, "quick"), "Span", opts=o)
    s3 = edit.create_from_rect(m, work, 0, zone(page, "second line"), "Quote", opts=o)
    if para.children != [s0, s1, s2, s3]:
        bad.append(f"order inside the P: {[m.nodes[c].text for c in para.children]}")
    head = next(n for n in m.nodes.values() if n.role.startswith("H") and "Animals" in (n.text or ""))
    q = edit.create_from_rect(m, work, 0, zone(page, "at play"), "Quote", opts=o)
    if m.nodes[q].parent != head.id:
        bad.append("the Quote is not inside the heading")
    # the old way, on request
    other = next(n for n in m.nodes.values() if n.role == "P" and "Another" in (n.text or ""))
    s4 = edit.create_from_rect(m, work, 0, zone(page, "follows"), "Span", opts=o, inside=False)
    if m.nodes[s4].parent == other.id:
        bad.append("inside=False still put the Span inside")
    m.check_integrity()
    out = os.path.join(job, "out.pdf")
    build(m, work, out, o)
    if structure_integrity(out) or mc_balance(out):
        bad.append(f"structure {structure_integrity(out)[:2]} {mc_balance(out)[:2]}")
    sq = k_sequence(out, "jumps over")
    kinds = ["text" if isinstance(x, str) else x[0] for x in sq if not (isinstance(x, str) and not x.strip())]
    tags = [x[1].strip() for x in sq if not isinstance(x, str)]
    if tags != ["quick", "brown fox", "lazy dog", "second line"]:
        bad.append(f"tags in the built P: {tags}")
    if kinds[:4] != ["text", "Span", "Span", "text"]:
        bad.append(f"reading sequence of the built P: {kinds}")
    flat = "".join(x if isinstance(x, str) else x[1] for x in sq)
    if "The quick brown fox jumps over the lazy dog." not in flat.replace("  ", " "):
        bad.append(f"the P does not read as printed: {flat!r}")
    # a zone over the whole paragraph replaces it (as before)
    m2 = load_model(job)
    whole = page.search_for("Another paragraph follows the first one.")[0]
    nid = edit.create_from_rect(m2, work, 0, [whole.x0 - 2, whole.y0 - 2, whole.x1 + 2, whole.y1 + 2], "Span", opts=o)
    if any(n.role == "P" and "Another" in (n.text or "") for n in m2.nodes.values()) or m2.nodes[nid].role != "Span":
        bad.append("a zone over the whole paragraph did not replace it")
    doc.close()
    if bad:
        print("inline zone test: FAILED")
        for x in bad:
            print("   ", x)
        return 1
    print("inline zone test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
