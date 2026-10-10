"""End-to-end processing of one PDF: shared by the web app and the CLI.

Job folder layout:
    original.pdf      the upload, never modified
    working.pdf       after preflight (what gets analysed and tagged)
    model.json        the editable structure
    working.colours.pdf / contrast.orig.json
                      originals kept by "Fix contrast now" (for Undo)
    accessible.pdf    the result
    report.xlsx       the report
    meta.json         status, scores, logs
"""
from __future__ import annotations

import json
import os
import shutil
import traceback
from dataclasses import asdict
from datetime import datetime
from typing import Callable

import pikepdf
import pymupdf

from . import checker
from . import contrast as contrast_mod
from .analyzer import analyze
from .build import build
from .compat import use as use_engine
from .importer import assess_tags, import_tags
from .model import DocModel, Finding
from .options import Options
from .preflight import preflight
from .report import write_report


def _write(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def load_contrast(job: str) -> dict | None:
    p = os.path.join(job, "contrast.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def save_contrast(job: str, data: dict) -> None:
    _write(os.path.join(job, "contrast.json"), data)


COLOUR_BACKUP = "working.colours.pdf"
CONTRAST_BACKUP = "contrast.orig.json"


def _fixable(i: dict, method: str = "auto") -> bool:
    return bool(contrast_mod.fix_method(i, method))


METHOD_WORDS = {"background": "box colour", "text": "text colour", "stroke": "outline"}


def fix_contrast_now(job: str, ids: list[str] | None = None,
                     progress: Callable | None = None, fix: dict | None = None) -> dict:
    """Apply contrast fixes to working.pdf right away (editor "Fix" button),
    re-measure the changed pages and record the result. The first call keeps
    the original colours so undo_contrast_fixes() can restore them.
    fix = {"method": auto|background|text|stroke, "stroke_width": pt,
    "stroke_color": black|white|auto}."""
    progress = progress or (lambda f, m: None)
    fix = fix or {}
    method = fix.get("method") if fix.get("method") in contrast_mod.METHODS else "auto"
    cdata = load_contrast(job)
    if cdata is None:
        raise ValueError("contrast was not measured for this file — use Re-measure first")
    want = set(ids) if ids else None
    sel = [i for i in cdata.get("issues", [])
           if i.get("status") != "fixed" and _fixable(i, method) and (want is None or i["id"] in want)]
    if not sel:
        return {"applied": 0, "fixed": 0, "still": 0, "skipped": 0,
                "summary": contrast_mod.summarize(cdata),
                "message": "nothing left that can be fixed automatically" + (
                    f" with {METHOD_WORDS[method]} — try another fix method" if method in METHOD_WORDS else "")}
    work = os.path.join(job, "working.pdf")
    bk, bkc = os.path.join(job, COLOUR_BACKUP), os.path.join(job, CONTRAST_BACKUP)
    if not os.path.exists(bk):
        shutil.copyfile(work, bk)
        shutil.copyfile(os.path.join(job, "contrast.json"), bkc)
    progress(0.1, f"changing colours ({len(sel)} item(s))")
    pdf = pikepdf.open(work)
    st = contrast_mod.apply_fixes(pdf, sel, method=method,
                                  stroke_width=float(fix.get("stroke_width") or 0.5),
                                  stroke_color=fix.get("stroke_color") or "black")
    tmp = work + ".tmp"
    pdf.save(tmp)
    pdf.close()
    os.replace(tmp, work)
    applied = set(st.get("applied", []))
    for i in cdata["issues"]:
        if i["id"] in (st.get("skipped") or {}):
            i["fix_note"] = st["skipped"][i["id"]]
    fixed = still = 0
    pages = sorted({i["page"] for i in sel if i["id"] in applied})
    if pages:
        progress(0.35, f"re-measuring {len(pages)} page(s)")
        model = load_model(job) if os.path.exists(os.path.join(job, "model.json")) else None
        re_ = contrast_mod.analyze(work, level=cdata.get("level", "AA"), pages=pages, model=model,
                                   large_rule=cdata.get("large_rule", "pac"),
                                   progress=lambda f, m: progress(0.35 + 0.6 * f, m))
        remaining = re_.get("issues", [])
        for i in cdata["issues"]:
            if i["id"] not in applied:
                continue
            x0, y0, x1, y1 = i["bbox"]
            hit = [r for r in remaining if r["page"] == i["page"] and not (
                r["bbox"][2] < x0 or r["bbox"][0] > x1 or r["bbox"][3] < y0 or r["bbox"][1] > y1)]
            i["fixed_in_working"] = True
            i["fixed_with"] = st["method"].get(i["id"], "")
            if i["fixed_with"] == "stroke":
                i["fixed_detail"] = f'{(st.get("stroke_colour") or {}).get(i["id"], "black")} outline ' \
                                    f'{float(fix.get("stroke_width") or 0.5):g} pt'
            i["apply"] = False
            i.pop("fix_note", None)
            if hit:
                i["status"] = "still failing"
                i["ratio_after"] = min(h["ratio"] for h in hit)
                still += 1
            else:
                i["status"] = "fixed"
                fixed += 1
    save_contrast(job, cdata)
    save_findings(job, "before", contrast_findings(load_findings(job, "before"), cdata))
    progress(1.0, "done")
    return {"applied": len(applied), "fixed": fixed, "still": still,
            "skipped": len(st.get("skipped") or {}), "summary": contrast_mod.summarize(cdata),
            "score_before": checker.score(load_findings(job, "before"))}


def undo_contrast_fixes(job: str) -> dict:
    """Put the original colours back (undo every "Fix" click)."""
    bk, bkc = os.path.join(job, COLOUR_BACKUP), os.path.join(job, CONTRAST_BACKUP)
    if not os.path.exists(bk):
        raise ValueError("there are no colour fixes to undo")
    os.replace(bk, os.path.join(job, "working.pdf"))
    if os.path.exists(bkc):
        os.replace(bkc, os.path.join(job, "contrast.json"))
    cdata = load_contrast(job)
    save_findings(job, "before", contrast_findings(load_findings(job, "before"), cdata))
    return {"summary": contrast_mod.summarize(cdata) if cdata else {},
            "score_before": checker.score(load_findings(job, "before"))}


def contrast_findings(findings: list[Finding], data: dict | None) -> list[Finding]:
    """Replace the manual contrast checkpoint with measured results."""
    if not data:
        return findings
    out = [f for f in findings if f.code not in ("M-01", "UA-C1")]
    s = contrast_mod.summarize(data)
    open_ = [i for i in data.get("issues", []) if i.get("status") != "fixed"]
    pages = sorted({i["page"] for i in open_})
    lvl = data.get("level", "AA")
    clause = "WCAG 1.4.3" if lvl == "AA" else "WCAG 1.4.6"
    if not open_:
        out.append(Finding("UA-C1", f"Text meets {lvl} contrast", "pass", clause,
                           f"{s['checked']} text runs measured" + (f", {s['fixed']} fixed" if s["fixed"] else "")))
    else:
        errs = [i for i in open_ if i["severity"] == "error"]
        worst = min(i["ratio"] for i in open_)
        out.append(Finding(
            "UA-C1", f"Text below {lvl} contrast", "error" if errs else "warning", clause,
            f"{len(open_)} text run(s), lowest {worst}:1"
            + (f"; {len(open_) - len(errs)} on varying backgrounds or in artifacts" if len(errs) != len(open_) else ""),
            pages=pages, count=len(open_), fixable="review"))
    out.append(Finding("M-01", "Contrast of text inside images and graphics", "info", "WCAG 1.4.3",
                       "Text that is part of a picture is not measured — check visually", fixable="manual"))
    return out


def save_findings(job: str, which: str, findings: list[Finding]) -> None:
    _write(os.path.join(job, f"findings_{which}.json"), [asdict(f) for f in findings])


def load_findings(job: str, which: str) -> list[Finding]:
    p = os.path.join(job, f"findings_{which}.json")
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as fh:
        return [Finding(**d) for d in json.load(fh)]


def save_model(job: str, model: DocModel) -> None:
    tmp = os.path.join(job, "model.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(model.to_json())
    os.replace(tmp, os.path.join(job, "model.json"))


def save_auto_model(job: str, model: DocModel) -> None:
    """The untouched automatic proposal, used by 'Auto-tag page'."""
    tmp = os.path.join(job, "auto_model.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(model.to_json())
    os.replace(tmp, os.path.join(job, "auto_model.json"))


def load_auto_model(job: str, opts: Options, fresh: bool = False,
                    progress: Callable | None = None) -> DocModel:
    use_engine(opts)
    p = os.path.join(job, "auto_model.json")
    if os.path.exists(p) and not fresh:
        with open(p, encoding="utf-8") as fh:
            m = DocModel.from_json(fh.read())
        if not m.info.get("rows_paired"):
            # a proposal made before V4.21.4: list items set in two columns
            # get their second texts (cheap — no new analysis)
            try:
                from .analyzer import list_columns
                list_columns(m, os.path.join(job, "working.pdf"))
                save_auto_model(job, m)
            except Exception:
                pass
        return m
    o = Options.from_dict(opts.to_dict())
    o.ai_alt_text_auto = False
    m = analyze(os.path.join(job, "working.pdf"), o, progress)
    save_auto_model(job, m)
    return m


def load_model(job: str) -> DocModel:
    with open(os.path.join(job, "model.json"), encoding="utf-8") as fh:
        return DocModel.from_json(fh.read())


def ai_alt_text_all(job: str, model: DocModel, ai_cfg, progress: Callable | None = None) -> dict:
    from .ai import OllamaError, alt_text
    progress = progress or (lambda f, m: None)
    figs = [n for n in model.nodes.values()
            if n.role == "Figure" and not n.artifact and not n.alt and n.bbox]
    doc = pymupdf.open(os.path.join(job, "working.pdf"))
    done = failed = decorative = 0
    error = ""
    for k, n in enumerate(figs):
        progress(k / max(1, len(figs)), f"AI alt text {k + 1}/{len(figs)}")
        try:
            page = doc[n.page]
            clip = pymupdf.Rect(n.bbox) * page.transformation_matrix
            clip.normalize()
            png = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=clip & page.rect).tobytes("png")
            ctx = " ".join(x.text for x in model.ordered_nodes() if x.page == n.page and x.text)[:800]
            text = alt_text(ai_cfg, png, ctx)
            if text.upper().startswith("DECORATIVE"):
                n.note = (n.note + "; " if n.note else "") + "AI thinks this is decorative"
                decorative += 1
            elif text:
                n.alt = text
                n.source = "ai"
                done += 1
        except OllamaError as exc:
            failed += 1
            error = str(exc)
            break
        except Exception:
            failed += 1
    doc.close()
    progress(1.0, f"AI alt text {len(figs)}/{len(figs)}")
    return {"drafted": done, "failed": failed, "decorative": decorative,
            "figures": len(figs), "error": error}


def prepare(job: str, opts: Options, *, ai_cfg=None,
            progress: Callable | None = None, filename: str = "") -> dict:
    """Preflight + analysis (or import). Writes working.pdf and model.json."""
    use_engine(opts)            # V4 engine, or the client house style's (core/compat.py)
    progress = progress or (lambda f, m: None)
    src = os.path.join(job, "original.pdf")
    work = os.path.join(job, "working.pdf")
    result: dict = {"filename": filename}

    for stale in (COLOUR_BACKUP, CONTRAST_BACKUP):
        if os.path.exists(os.path.join(job, stale)):
            os.remove(os.path.join(job, stale))
    progress(0.02, "checking the original")
    before = checker.check(src)
    save_findings(job, "before", before)
    result["score_before"] = checker.score(before)

    mode = opts.existing_tags
    if mode == "ask":
        mode = "auto"            # unanswered (batch, CLI): decide as before
    assessment = None
    if mode in ("auto", "import"):
        assessment = assess_tags(src)
        result["assessment"] = assessment
        if mode == "auto":
            mode = assessment["verdict"]
        elif not assessment["tagged"]:
            mode = "retag"
    result["mode"] = mode

    progress(0.08, "preflight")
    pf = preflight(src, work, opts, import_mode=(mode == "import"))
    result["preflight"] = pf

    def sub(lo, hi):
        return lambda f, m: progress(lo + (hi - lo) * f, m)

    if mode == "import":
        model = import_tags(work, sub(0.15, 0.9))
        if not model.lang or model.lang == "en-US" and opts.lang not in ("", "auto"):
            model.lang = model.lang or opts.lang
        # the tags came from the file, but the cross-references in the text did
        # not: a contents list imported as TOC/TOCI has nothing to click, so the
        # same search that runs after an analysis runs here too
        if getattr(opts, "detect_bibliography", True):
            # a reference list imported as paragraphs: one BibEntry per entry
            try:
                from .biblio import fix_bibliography, problems
                if problems(model)["sections"]:
                    bib = fix_bibliography(model, work, opts,
                                           ai_cfg if getattr(opts, "ai_bibliography", False) else None)
                    if bib.get("entries"):
                        model.info["bibliography"] = bib
            except Exception as exc:
                model.warnings.append(f"reference list: {exc}")
        if getattr(opts, "detect_index", True) or getattr(opts, "detect_footnotes", True):
            try:
                from .backmatter import fix_backmatter, index_problems
                from .backmatter import tag_endnotes as _endnotes
                if getattr(opts, "detect_index", True) and index_problems(model):
                    model.info["backmatter"] = fix_backmatter(model, work, opts)
                elif getattr(opts, "detect_footnotes", True):
                    _endnotes(model)
            except Exception as exc:
                model.warnings.append(f"notes / index: {exc}")
        if opts.detect_cross_references:
            try:
                import pymupdf

                from .analyzer import collect_lines
                from .xref import dead_links, find_and_apply
                dead_links(model)
                doc = pymupdf.open(work)
                try:
                    find_and_apply(model, [collect_lines(p) for p in doc], doc, opts)
                finally:
                    doc.close()
            except Exception as exc:
                model.warnings.append(f"cross-reference search failed ({exc})")
        if opts.join_spaced_letters or opts.detect_blank_lines:
            try:
                from .repair import polish_imported
                result["imported_fixes"] = polish_imported(model, work, opts)
            except Exception as exc:
                model.warnings.append(f"imported tag clean-up failed ({exc})")
    else:
        model = analyze(work, opts, sub(0.15, 0.9),
                        ai_cfg=ai_cfg if (opts.ai_table_headers or opts.ai_table_check or opts.ai_table_merge
                                          or getattr(opts, "ai_bibliography", False)) else None)
    model.path = work
    client = getattr(opts, "client", "") or ""
    scope = getattr(opts, "samples_scope", "client") or "client"
    brain_on = mode != "import" and bool(getattr(opts, "ai_brain", False))
    know = weights = docinfo = None
    if mode != "import" and getattr(opts, "use_samples", True):
        # the client's sample files (V4.20, core/learn.py): nothing without samples
        try:
            from .learn import Knowledge, apply_samples, has_data
            if has_data():
                progress(0.9, "using the sample files" if scope == "all" else "using the client's sample files")
                if scope == "all":
                    # V4.25: every client's samples, the ones most like this file first
                    from .brain import doc_weights
                    weights, docinfo = doc_weights(work)
                know = Knowledge(client, scope=scope, weights=weights)
                sr = apply_samples(model, work, client,
                                   ai_cfg=ai_cfg if (getattr(opts, "ai_samples", True) and not brain_on) else None,
                                   rules=getattr(opts, "sample_rules", True), know=know)
                if sr.get("files"):
                    if docinfo:
                        sr["doc_type"] = docinfo.get("doc_type")
                        sr["similar"] = docinfo.get("similar", [])[:3]
                    result["samples"] = {k: v for k, v in sr.items() if k != "changes"}
                    model.info["samples"] = sr
        except Exception as exc:
            model.warnings.append(f"sample files could not be used ({exc})")
    if brain_on:
        # V4.25: the self-learning brain decides every text tag (core/brain.py)
        try:
            from .brain import think
            progress(0.91, "AI brain: deciding every text tag")
            br = think(model, work, opts, ai_cfg=ai_cfg, client=client,
                       job=os.path.basename(os.path.normpath(job)), know=know, docinfo=docinfo, weights=weights,
                       use_samples=bool(getattr(opts, "use_samples", True)),
                       progress=sub(0.91, 0.94))
            result["brain"] = {k: v for k, v in br.items() if k not in ("changes",)}
        except Exception as exc:
            model.warnings.append(f"AI brain could not decide ({exc})")
    if not model.title:
        model.title = os.path.splitext(os.path.basename(result.get("filename", "") or "Untitled"))[0]

    # V4.26: everything in the PDF, counted — and every line, picture, link and
    # field matched against the tags, so nothing is missed (core/inventory.py)
    inv = None
    try:
        from . import inventory as INV
        progress(0.94, "inventory: every object and page item")
        inv = INV.build(work)
        try:
            import pymupdf as _mu          # (prepare imports pymupdf locally elsewhere — keep the names apart)
            odoc = _mu.open(src)
            try:
                inv["original"] = INV.document_facts(odoc)
                inv["original"]["fonts_not_embedded"] = sum(1 for f in INV.fonts(odoc) if not f["embedded"])
            finally:
                odoc.close()
        except Exception:
            pass
        cov = INV.coverage(inv, model, work)
        INV.save(job, inv, cov)
        result["inventory"] = INV.summary(inv, cov)
        model.info["inventory"] = result["inventory"]
    except Exception as exc:
        model.warnings.append(f"inventory: {exc}")
    # V4.26: the pictures and readings people gave before (core/memory.py) —
    # before the AI is asked, so a known logo costs no question
    if mode != "import" and getattr(opts, "use_samples", True) and inv is not None:
        try:
            from .memory import apply as remember
            mem = remember(model, job, client, any_client=(scope == "all"), inv=inv)
            if mem.get("alts") or mem.get("readings"):
                result["memory"] = mem
                model.info["memory"] = mem
        except Exception as exc:
            model.warnings.append(f"memory: {exc}")

    if opts.ai_alt_text_auto and ai_cfg is not None and getattr(ai_cfg, "enabled", False):
        progress(0.9, "AI alt text")
        result["ai_alt"] = ai_alt_text_all(job, model, ai_cfg, sub(0.9, 0.98))

    if opts.check_contrast:
        progress(0.95, "measuring colour contrast")
        try:
            cdata = contrast_mod.analyze(work, level=opts.contrast_level, model=model,
                                         large_rule=opts.contrast_large_text,
                                         progress=sub(0.95, 0.99))
            save_contrast(job, cdata)
            result["contrast"] = contrast_mod.summarize(cdata)
            save_findings(job, "before", contrast_findings(load_findings(job, "before"), cdata))
            result["score_before"] = checker.score(load_findings(job, "before"))
        except Exception as exc:
            result["contrast_error"] = f"contrast check failed: {exc}"

    save_model(job, model)
    if mode != "import":
        save_auto_model(job, model)
    result["nodes"] = len(model.nodes)
    result["pages"] = model.page_count
    result["title"] = model.title
    result["lang"] = model.lang
    progress(1.0, "ready for review")
    return result


def finish(job: str, opts: Options, *, filename: str, verapdf_exe: str = "",
           progress: Callable | None = None, preflight_info: dict | None = None,
           score_before: dict | None = None) -> dict:
    """Build accessible.pdf from model.json, check it, write the report."""
    use_engine(opts)
    progress = progress or (lambda f, m: None)
    model = load_model(job)
    work = os.path.join(job, "working.pdf")
    out = os.path.join(job, "accessible.pdf")
    cdata = load_contrast(job)
    fixes = []
    if cdata and opts.fix_contrast != "off":
        for i in cdata.get("issues", []):
            if i.get("fixed_in_working"):
                continue                      # already corrected with the editor's Fix button
            i.pop("fix_note", None)
            i.pop("fixed_with", None)
            i.pop("fixed_detail", None)
            if i.get("status") == "fixed":
                i["status"] = "open"
            if _fixable(i, opts.contrast_fix_method) and (
                    opts.fix_contrast == "all" or i.get("apply")):
                fixes.append(i)
    progress(0.05, "tagging content")
    stats = build(model, work, out, opts, contrast_fixes=fixes)
    progress(0.7, "checking the result")
    after = checker.check(out)
    if cdata:
        cst = stats.get("contrast") or {}
        applied = set(cst.get("applied", []))
        for i in cdata["issues"]:
            if i["id"] in applied:
                i["fixed_with"] = (cst.get("method") or {}).get(i["id"], "")
                if i["fixed_with"] == "stroke":
                    i["fixed_detail"] = f'{(cst.get("stroke_colour") or {}).get(i["id"], "black")} outline ' \
                                        f'{opts.contrast_stroke_width:g} pt'
        for iid, why in (cst.get("skipped") or {}).items():
            for i in cdata["issues"]:
                if i["id"] == iid:
                    i["fix_note"] = why
        if applied:
            progress(0.75, "re-measuring contrast")
            pages = sorted({i["page"] for i in cdata["issues"] if i["id"] in applied})
            recheck = contrast_mod.analyze(out, level=cdata.get("level", "AA"), pages=pages, model=model,
                                           large_rule=cdata.get("large_rule", opts.contrast_large_text))
            still = recheck.get("issues", [])
            for i in cdata["issues"]:
                if i["id"] not in applied:
                    continue
                x0, y0, x1, y1 = i["bbox"]
                hit = [r for r in still if r["page"] == i["page"] and not (
                    r["bbox"][2] < x0 or r["bbox"][0] > x1 or r["bbox"][3] < y0 or r["bbox"][1] > y1)]
                i["status"] = "still failing" if hit else "fixed"
                if hit:
                    i["ratio_after"] = min(h["ratio"] for h in hit)
        save_contrast(job, cdata)
        after = contrast_findings(after, cdata)
        stats["contrast_summary"] = contrast_mod.summarize(cdata)
    vera = {}
    if opts.run_verapdf and verapdf_exe:
        progress(0.8, "running veraPDF")
        vf, vs = checker.run_verapdf(out, verapdf_exe)
        vera = {"summary": vs, "findings": vf,
                "verdict": vf[0].detail if vf else vs.get("error", "veraPDF did not run")}
        after = vf[:1] + after + vf[1:]
    save_findings(job, "after", after)
    before = load_findings(job, "before")
    bs = score_before or checker.score(before)
    as_ = checker.score(after)
    progress(0.9, "writing the report")
    report_error = ""
    try:
        write_report(os.path.join(job, "report.xlsx"), filename=filename, before=before,
                     after=after, before_score=bs, after_score=as_, model=model,
                     build_stats=stats, preflight=preflight_info, options=opts.to_dict(),
                     verapdf=vera, contrast=cdata)
    except Exception as exc:                       # the PDF is still good
        report_error = f"report could not be written: {exc}"
    save_model(job, model)
    learned = {}
    if getattr(opts, "learn_feedback", False):
        # V4.25: what people changed in the tool's proposal teaches the brain
        try:
            ap = os.path.join(job, "auto_model.json")
            if os.path.exists(ap):
                from .brain import record_feedback
                with open(ap, encoding="utf-8") as fh:
                    auto = DocModel.from_json(fh.read())
                learned = record_feedback(job, getattr(opts, "client", "") or "", model, auto)
                from .memory import learn as keep
                learned["memory"] = keep(job, getattr(opts, "client", "") or "", model, auto, verified=False)
        except Exception as exc:
            learned = {"error": str(exc)[:200]}
    progress(1.0, "done")
    slim = {k: v for k, v in stats.items() if k not in ("unmatched_pages",)}
    slim["pruned"] = len(stats.get("pruned", []))
    slim["missing_alt"] = len(stats.get("missing_alt", []))
    return {"score_before": bs, "score_after": as_, "stats": slim,
            "applied": stats.get("applied", []), "review": stats.get("review", []),
            "verapdf": vera.get("verdict", ""), "report_error": report_error,
            "built_at": datetime.now().isoformat(timespec="seconds"), "learned": learned}


def process_file(src_path: str, job: str, opts: Options, *, ai_cfg=None, verapdf_exe: str = "",
                 progress: Callable | None = None) -> dict:
    """Headless: prepare + finish (batch / CLI)."""
    import shutil
    os.makedirs(job, exist_ok=True)
    shutil.copyfile(src_path, os.path.join(job, "original.pdf"))
    try:
        r = prepare(job, opts, ai_cfg=ai_cfg, progress=progress,
                    filename=os.path.basename(src_path))
        f = finish(job, opts, filename=os.path.basename(src_path), verapdf_exe=verapdf_exe,
                   progress=progress, preflight_info=r.get("preflight"),
                   score_before=r.get("score_before"))
        r.update(f)
        r["status"] = "done"
    except Exception as exc:
        r = {"status": "error", "error": f"{exc}", "trace": traceback.format_exc()[-3000:]}
    return r
