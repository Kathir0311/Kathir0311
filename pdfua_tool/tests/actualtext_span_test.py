"""/ActualText sits on a Span made inside the tag, not on the tag (V4.15–4.16).

No client's file may carry /ActualText on a P, TD, H … itself. Wherever a tag is
read through /ActualText (a write-in line, a blank cell, a spelled card, a
letter-spaced line), the tag gets a Span child that holds its content and
carries the /ActualText. V4.16: that is how **every** profile builds (it was the
Academain profile only), a file whose /ActualText still sits on the tag is
reported as UA-58, and the editor's one-click fix (edit.actualtext_to_span) puts
it on a Span in the tag tree itself.
"""
import os
import sys
import tempfile

import pikepdf
import pymupdf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import edit                                  # noqa: E402
from core.analyzer import analyze                      # noqa: E402
from core.checker import check                         # noqa: E402
from core.options import Options, profile_options      # noqa: E402
from core.pipeline import finish, load_model, process_file, save_model  # noqa: E402
from tests.verify import mc_balance, qpdf_check, structure_integrity    # noqa: E402

RED = (0.8, 0.1, 0.1)


def make(path: str) -> None:
    d = pymupdf.open()
    p = d.new_page(width=400, height=400)
    p.insert_text((20, 50), "c", fontsize=30)
    p.insert_text((45, 50), "a", fontsize=30, color=RED)
    p.insert_text((70, 50), "t", fontsize=30)
    p.insert_text((20, 110), "u", fontsize=30, color=RED)
    p.insert_text((45, 110), "_", fontsize=30)
    p.insert_text((70, 110), "e", fontsize=30, color=RED)
    p.insert_text((20, 170), "1. Write the word here:", fontsize=11)
    p.draw_line((190, 172), (330, 172))
    y = 230
    for _ in range(8):
        p.insert_text((20, y), "ordinary running text printed on the card for the page", fontsize=8)
        y += 10
    d.save(path)
    d.close()


def walk(pdf):
    """Every struct element with its parent element (None for the top)."""
    out, stack = [], [(pdf.Root.StructTreeRoot.get("/K"), None)]
    while stack:
        e, parent = stack.pop()
        if isinstance(e, pikepdf.Array):
            stack.extend((x, parent) for x in e)
            continue
        if isinstance(e, pikepdf.Dictionary) and "/S" in e:
            out.append((e, parent))
            k = e.get("/K")
            if k is not None:
                stack.append((k, e))
    return out


def main() -> int:
    bad = []
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "card.pdf")
    make(src)

    def build(opts: Options, name: str):
        job = os.path.join(tmp, name)
        res = process_file(src, job, opts)
        if res.get("status") != "done":
            bad.append(f"{name}: build failed: {res.get('error')}")
            return None
        m = load_model(job)
        ids = [n.id for n in m.ordered_nodes()
               if (n.text or "").strip().replace(" ", "") in ("cat", "u_e") and not n.artifact]
        if len(ids) < 2:
            bad.append(f"{name}: the two cards were not tagged separately")
            return None
        items = edit.suggest_actual_text(m, os.path.join(job, "working.pdf"), ids)
        edit.set_actual_text(m, [i["id"] for i in items], [i["suggestion"] for i in items], "each")
        save_model(job, m)
        finish(job, opts, filename="card.pdf")
        return os.path.join(job, "accessible.pdf")

    # -- Academain profile
    ac = profile_options("Academain")
    if not ac.actualtext_span or ac.house_style not in ("", "none"):
        bad.append("the Academain profile must set actualtext_span and no house style")
    out = build(ac, "ac")
    if out:
        pdf = pikepdf.open(out)
        els = walk(pdf)
        with_at = [(e, p) for e, p in els if "/ActualText" in e]
        if not with_at:
            bad.append("Academain: no /ActualText at all in the built file")
        for e, p in with_at:
            role = str(e.S)[1:]
            if role not in ("Span", "Figure", "Formula"):
                bad.append(f"Academain: /ActualText left on a {role}")
            if role == "Span" and (p is None or str(p.S)[1:] == "Span"):
                bad.append("Academain: a Span with /ActualText has no tag around it")
        spans = [(e, p) for e, p in with_at if str(e.S) == "/Span"]
        want = {"c, a and t, a highlighted in red", "u, underscore e, u, e, are highlighted in red"}
        got = {str(e.ActualText) for e, _ in spans}
        if not want <= got:
            bad.append(f"Academain: card sentences missing from the Spans: {want - got}")
        for e, p in spans:
            if "/K" not in e:
                bad.append("Academain: an ActualText Span holds no content")
            if e.get("/P") is None or e.P.objgen != p.objgen:
                bad.append("Academain: a Span's /P is not its tag")
        for e, p in els:                      # every /P points at the element it is a kid of
            for k in (list(e.K) if isinstance(e.get("/K"), pikepdf.Array) else [e.get("/K")]):
                if isinstance(k, pikepdf.Dictionary) and "/S" in k and k.P.objgen != e.objgen:
                    bad.append(f"Academain: a {k.S} has /P wrong")
        errs = structure_integrity(out)
        if errs:
            bad.append(f"Academain: structure integrity: {errs[:3]}")
        if mc_balance(out):
            bad.append(f"Academain: marked content unbalanced: {mc_balance(out)[:2]}")
        if qpdf_check(out) != "ok":
            bad.append(f"Academain: qpdf: {qpdf_check(out)}")

    # -- V4.16: every profile builds the same way
    out2 = build(Options(), "plain")
    if out2:
        pdf = pikepdf.open(out2)
        els = walk(pdf)
        on_tag = [e for e, _ in els if "/ActualText" in e
                  and str(e.S) not in ("/Span", "/Figure", "/Formula")]
        wrapped = [e for e, p in els if "/ActualText" in e and str(e.S) == "/Span"
                   and p is not None and str(p.S) not in ("/Span",)]
        if on_tag or not wrapped:
            bad.append(f"default profile: {len(on_tag)} tag(s) still carry /ActualText, "
                       f"{len(wrapped)} on a Span")
        if [f.code for f in check(out2) if f.code == "UA-58" and f.severity != "pass"]:
            bad.append("default profile: UA-58 reported on a file built the new way")

    # -- turned off on purpose: it stays on the tag, UA-58 says so, and the
    #    editor's one-click fix puts it on a Span even with the option off
    off = Options()
    off.actualtext_span = False
    job = os.path.join(tmp, "off")
    opts_res = process_file(src, job, off)
    if opts_res.get("status") != "done":
        bad.append(f"off: {opts_res.get('error')}")
    else:
        m = load_model(job)
        ids = [n.id for n in m.ordered_nodes()
               if (n.text or "").strip().replace(" ", "") in ("cat", "u_e") and not n.artifact]
        items = edit.suggest_actual_text(m, os.path.join(job, "working.pdf"), ids)
        edit.set_actual_text(m, [i["id"] for i in items], [i["suggestion"] for i in items], "each")
        save_model(job, m)
        finish(job, off, filename="card.pdf")
        out3 = os.path.join(job, "accessible.pdf")
        pdf3 = pikepdf.open(out3)
        on_tag = [e for e, _ in walk(pdf3) if "/ActualText" in e
                  and str(e.S) not in ("/Span", "/Figure", "/Formula")]
        if not on_tag:
            bad.append("off: nothing carries /ActualText on the tag with the option off")
        ua58 = [f for f in check(out3) if f.code == "UA-58" and f.severity == "warning"]
        if not ua58:
            bad.append("off: the checker does not report UA-58")
        elif ua58[0].fixable != "auto":
            bad.append("off: UA-58 is not reported as fixable")
        # the fix in the tag tree
        m = load_model(job)
        left = edit.actualtext_on_tag(m)
        if not left:
            bad.append("off: the editor finds no tag to fix")
        changed = edit.actualtext_to_span(m)
        if changed != len(left) or edit.actualtext_on_tag(m):
            bad.append(f"the fix changed {changed} of {len(left)} tag(s), "
                       f"{len(edit.actualtext_on_tag(m))} left")
        for nid in left:
            n = m.nodes[nid]
            kids = [m.nodes[c] for c in n.children]
            if (n.actual_text or "").strip() or len(kids) != 1 or kids[0].role != "Span" \
                    or not (kids[0].actual_text or "").strip():
                bad.append(f"the fix: {n.role} > {[k.role for k in kids]} "
                           f"actual_text={n.actual_text!r}")
                break
        errs = m.check_integrity()
        if errs:
            bad.append(f"the fix: structure integrity {errs[:2]}")
        save_model(job, m)
        finish(job, off, filename="card.pdf")
        out4 = os.path.join(job, "accessible.pdf")
        pdf4 = pikepdf.open(out4)
        els = walk(pdf4)
        if [e for e, _ in els if "/ActualText" in e and str(e.S) not in ("/Span", "/Figure", "/Formula")]:
            bad.append("the fix: /ActualText still on a tag in the built file")
        if [f for f in check(out4) if f.code == "UA-58" and f.severity == "warning"]:
            bad.append("the fix: UA-58 still reported after it")
        if structure_integrity(out4) or mc_balance(out4):
            bad.append(f"the fix: {structure_integrity(out4)[:2]} {mc_balance(out4)[:2]}")
        if qpdf_check(out4) != "ok":
            bad.append(f"the fix: qpdf {qpdf_check(out4)}")

    if bad:
        print("actualtext span test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("actualtext span test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
