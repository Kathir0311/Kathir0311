"""Regression suite: process every corpus PDF headlessly and verify the output."""
from __future__ import annotations

import glob
import os
import shutil
import sys
import tempfile
import time

ROOT = __file__.rsplit("/tests/", 1)[0]
sys.path.insert(0, ROOT)

import pikepdf  # noqa: E402
import pymupdf  # noqa: E402

from core.options import Options  # noqa: E402
from core.pipeline import load_findings, load_model, process_file  # noqa: E402
from tests.verify import mc_balance, qpdf_check, render_diff, structure_integrity  # noqa: E402


def roles(path):
    pdf = pikepdf.open(path)
    out = []
    st = pdf.Root.get("/StructTreeRoot")
    stack = [st.K]
    while stack:
        e = stack.pop()
        if isinstance(e, pikepdf.Array):
            stack.extend(reversed(list(e)))
            continue
        if isinstance(e, pikepdf.Dictionary) and "/S" in e:
            out.append(str(e.S)[1:])
            k = e.get("/K")
            if k is not None:
                stack.append(k)
    return out


def run_one(src, work_root, opts=None, label=None):
    label = label or os.path.basename(src)
    job = os.path.join(work_root, label.replace(".pdf", ""))
    shutil.rmtree(job, ignore_errors=True)
    t = time.time()
    # colour fixes change pixels on purpose; the pixel-identity check runs without them
    r = process_file(src, job, opts or Options(fix_contrast="off"))
    dt = time.time() - t
    if r.get("status") != "done":
        print(f"✗ {label}: {r.get('error')}\n{r.get('trace', '')[-1500:]}")
        return None
    out = os.path.join(job, "accessible.pdf")
    work = os.path.join(job, "working.pdf")
    diff = render_diff(work, out, 1.5)
    integ = structure_integrity(out)
    bal = mc_balance(out)
    q = qpdf_check(out)
    after = load_findings(job, "after")
    errs = [f"{f.code}" for f in after if f.severity == "error"]
    warns = [f"{f.code}" for f in after if f.severity == "warning"]
    s = r["stats"]
    ok = max(diff) == 0 and not integ and not bal and q == "ok"
    print(f"{'✓' if ok else '✗'} {label:26} {dt:5.1f}s mode={r['mode']:6} "
          f"errors {r['score_before']['errors']}→{r['score_after']['errors']} "
          f"elems={s.get('elements')} mcids={s.get('mcids')} split={s.get('split_ops')} "
          f"unmatched={s.get('unmatched')} annots={s.get('annots_tagged')}+{s.get('annots_fallback')} "
          f"pruned={s.get('pruned')}")
    if max(diff):
        print(f"    render diff per page: {diff}")
    if integ:
        print(f"    integrity: {integ[:6]}")
    if bal:
        print(f"    balance: {bal[:4]}")
    if q != "ok":
        print(f"    qpdf: {q}")
    if errs or warns:
        detail = {f.code: f.detail[:70] for f in after if f.severity in ("error", "warning")}
        print(f"    remaining: {detail}")
    return job


def main():
    work_root = os.path.join(tempfile.gettempdir(), "pdfua_suite")
    os.makedirs(work_root, exist_ok=True)
    corpus = sorted(glob.glob(os.path.join(ROOT, "tests", "corpus", "*.pdf")))
    corpus.append(os.path.join(ROOT, "samples", "untagged_sample.pdf"))
    jobs = {}
    for src in corpus:
        jobs[os.path.basename(src)] = run_one(src, work_root)

    # rotated pages
    rot = os.path.join(work_root, "rotated.pdf")
    d = pymupdf.open(os.path.join(ROOT, "tests", "corpus", "article.pdf"))
    for i, p in enumerate(d):
        p.set_rotation([90, 180, 270][i % 3])
    d.save(rot)
    run_one(rot, work_root, label="rotated.pdf")

    # import round trip: re-open our own tagged output and keep its tags
    art = jobs.get("article.pdf")
    if art:
        tagged = os.path.join(work_root, "article_tagged.pdf")
        shutil.copyfile(os.path.join(art, "accessible.pdf"), tagged)
        o = Options(existing_tags="auto")
        job2 = run_one(tagged, work_root, o, label="import_roundtrip.pdf")
        if job2:
            a, b = roles(tagged), roles(os.path.join(job2, "accessible.pdf"))
            same = a == b
            print(f"    import round trip: structure {'identical' if same else 'DIFFERS'} "
                  f"({len(a)} vs {len(b)} elements)")
            if not same:
                import difflib
                for line in list(difflib.unified_diff(a, b, lineterm=""))[:20]:
                    print("      " + line)
            m = load_model(job2)
            print(f"    import info: {m.info.get('import')}")


if __name__ == "__main__":
    main()
