"""Command-line batch processing.

Examples
  python cli.py book.pdf -o out/
  python cli.py incoming/ -o out/ --profile Book --set lang=en-GB --set bookmark_levels=2
  python cli.py tagged.pdf -o out/ --set existing_tags=import
  python cli.py --check some.pdf              # just run the checker
  python cli.py --list-options
  python cli.py --brain-status                # V4.25: what the self-learning brain knows
  python cli.py --brain-train                 # train the brain again on every sample and fix
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys
import tempfile
import time

from core import checker
from core.ai import AIConfig
from core.options import BUILTIN_PROFILES, META, SMART_PROFILE, Options
from core.pipeline import process_file
from core.report import write_batch_summary

BASE = os.path.dirname(os.path.abspath(__file__))


def collect(inputs: list[str]) -> list[str]:
    out = []
    for i in inputs:
        if os.path.isdir(i):
            out += sorted(glob.glob(os.path.join(i, "**", "*.pdf"), recursive=True))
        elif i.lower().endswith(".pdf") and os.path.exists(i):
            out.append(i)
        else:
            print(f"skip: {i} (not a PDF or folder)", file=sys.stderr)
    return out


def load_profiles() -> dict:
    prof = {k: dict(v) for k, v in BUILTIN_PROFILES.items()}
    cfg = os.environ.get("PDFUA_CONFIG", os.path.join(BASE, "config.json"))
    if os.path.exists(cfg):
        try:
            with open(cfg, encoding="utf-8") as fh:
                prof.update(json.load(fh).get("profiles") or {})
        except Exception:
            pass
    return prof


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Dezquare PDF Accessibility Studio — batch PDF/UA tagging")
    ap.add_argument("inputs", nargs="*", help="PDF files or folders")
    ap.add_argument("-o", "--out", help="output folder")
    ap.add_argument("--profile", default=SMART_PROFILE,
                    help=f"option profile name (default: {SMART_PROFILE} — every client's samples and the AI brain)")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override an option")
    ap.add_argument("--verapdf", default="", help="path to the veraPDF launcher")
    ap.add_argument("--ai", action="store_true", help="use Ollama (settings from config.json) for alt text")
    ap.add_argument("--ai-tables", action="store_true",
                    help="with --ai: let the model decide doubtful table joins across pages (add --set ai_table_merge_vision=true to show it the pages)")
    ap.add_argument("--keep-jobs", action="store_true", help="keep working folders (model.json etc.)")
    ap.add_argument("--check", action="store_true", help="only run the checker and print findings")
    ap.add_argument("--list-options", action="store_true")
    ap.add_argument("--brain-status", action="store_true", help="V4.25: what the self-learning brain has learned")
    ap.add_argument("--brain-train", action="store_true", help="V4.25: train the brain again now")
    a = ap.parse_args(argv)
    try:
        from core import netguard                  # V4.26: nothing leaves the server
        netguard.install(process="command line")
    except Exception:
        pass

    if a.brain_status or a.brain_train:
        from core import brain
        if a.brain_train:
            print(json.dumps(brain.train_all(trigger="command line", force=True), indent=1, default=str))
        s = brain.status()
        m = s.get("model") or {}
        print(f"model: {('#' + str(m.get('id'))) if m else 'not trained'}"
              + (f" · {m.get('n_train')} examples from {m.get('n_samples')} file(s) · accuracy "
                 f"{round((m.get('accuracy') or 0) * 100)} % (chance {round((m.get('baseline') or 0) * 100)} %)" if m else ""))
        if m and m.get("temperature"):                 # V4.27: how honest its probabilities are
            print(f"  measured by {m.get('folds') or 1} held-out round(s) over files · probabilities calibrated "
                  f"(temperature {m['temperature']}, calibration error {round((m.get('ece') or 0) * 100, 1)} %, "
                  f"log-loss {m.get('logloss')})")
        c = s.get("curve") or {}
        if c:
            print(f"right first time: {c.get('first')} % → {c.get('last')} % over {c.get('files')} approved file(s)")
        try:                                       # V4.28: where zones are cut
            from core import zonelearn
            z = zonelearn.status()
            print(f"zones: {z['corrections']} correction(s) in {z['refs']} file(s), {z['rows']} pairs")
            for m in z["models"]:
                print(f"  zone model {m['scope'] or 'everybody':14} {m['status']:8} {m['n']} pairs, {m['explicit']} corrections"
                      + (f", fixes {m['fixed']} / spoils {m['broken']} on pages it was not trained on" if m["acc_model"] is not None else "")
                      + (f" — {m['note']}" if m["note"] else ""))
        except Exception:
            pass
        print(f"memory {s.get('memory', 0)} texts ({s.get('embedded', 0)} with AI vectors) · {s.get('styles', 0)} style "
              f"patterns · {s.get('feedback', 0)} fixes from built files · doc types {s.get('doc_types') or {}}")
        for t in s.get("trust") or []:
            print(f"  trust {t['source']:8} {('%d %%' % round(t['right'] * 100)) if t['right'] is not None else '—':>6}"
                  f"  ×{t['weight']}  ({t['n']} decisions)")
        return 0

    if a.list_options:
        d = Options().to_dict()
        for k, (label, help_, group, choices) in META.items():
            ch = f" [{'|'.join(choices)}]" if choices else ""
            print(f"{group:9} {k:28} = {d[k]!s:10}{ch}  {label}")
        print("\nprofiles:", ", ".join(load_profiles()))
        return 0

    files = collect(a.inputs)
    if not files:
        ap.print_help()
        return 2

    if a.check:
        bad = 0
        for f in files:
            fs = checker.check(f)
            sc = checker.score(fs)
            print(f"\n{f}: {sc['errors']} errors, {sc['warnings']} warnings, pass rate {sc['percent']}%")
            for x in fs:
                if x.severity in ("error", "warning"):
                    pages = f" (p. {', '.join(str(p + 1) for p in x.pages[:10])})" if x.pages else ""
                    print(f"  {x.severity.upper():7} {x.code:6} {x.title} — {x.detail[:90]}{pages}")
            bad += sc["errors"] > 0
        return 1 if bad else 0

    if not a.out:
        ap.error("--out is required")
    os.makedirs(a.out, exist_ok=True)
    profiles = load_profiles()
    if a.profile not in profiles:
        ap.error(f"unknown profile {a.profile}; choose from {', '.join(profiles)}")
    base = dict(profiles[a.profile])
    for kv in a.set:
        if "=" not in kv:
            ap.error(f"--set expects KEY=VALUE, got {kv}")
        k, v = kv.split("=", 1)
        if k not in Options().to_dict():
            ap.error(f"unknown option {k} (see --list-options)")
        base[k] = v
    opts = Options.from_dict(base)
    ai_cfg = None
    if a.ai:
        cfgp = os.environ.get("PDFUA_CONFIG", os.path.join(BASE, "config.json"))
        d = {}
        if os.path.exists(cfgp):
            with open(cfgp, encoding="utf-8") as fh:
                d = json.load(fh).get("ai") or {}
        ai_cfg = AIConfig(**{k: v for k, v in d.items() if k in AIConfig().__dict__})
        ai_cfg.enabled = True
        opts.ai_alt_text_auto = True
    if not opts.client:
        opts.client = a.profile                    # whose sample files (V4.20)
        if a.ai_tables:
            opts.ai_table_merge = True
    vera = checker.find_verapdf(a.verapdf)

    work_root = os.path.join(a.out, "_jobs") if a.keep_jobs else tempfile.mkdtemp(prefix="pdfua_")
    rows = []
    failed = 0
    for i, f in enumerate(files, 1):
        stem = os.path.splitext(os.path.basename(f))[0]
        job = os.path.join(work_root, f"{i:04d}_{stem}")
        t = time.time()
        print(f"[{i}/{len(files)}] {f} … ", end="", flush=True)
        r = process_file(f, job, opts, ai_cfg=ai_cfg, verapdf_exe=vera)
        dt = time.time() - t
        if r.get("status") != "done":
            failed += 1
            print(f"FAILED ({r.get('error')})")
            rows.append({"file": os.path.basename(f), "status": "error", "output": r.get("error", "")})
            continue
        out_pdf = os.path.join(a.out, f"{stem}_accessible.pdf")
        out_rep = os.path.join(a.out, f"{stem}_accessibility_report.xlsx")
        shutil.copyfile(os.path.join(job, "accessible.pdf"), out_pdf)
        shutil.copyfile(os.path.join(job, "report.xlsx"), out_rep)
        sb, sa = r["score_before"], r["score_after"]
        st = r.get("stats", {})
        cs = st.get("contrast_summary") or {}
        print(f"{sb['errors']} → {sa['errors']} errors, {st.get('missing_alt', 0)} figure(s) need alt text"
              + (f", contrast {cs.get('open', 0)} open/{cs.get('fixed', 0)} fixed" if cs else "") + " "
              f"[{r.get('mode')}] {dt:.1f}s" + (f" · {r['verapdf']}" if r.get("verapdf") else ""))
        br = r.get("brain") or {}
        if br.get("quality") is not None:
            print(f"     brain: {br.get('changed', 0)} tag(s) decided, {br.get('ai_asked', 0)} asked to the AI, "
                  f"confidence {br['quality']} %")
        rows.append({"file": os.path.basename(f), "pages": r.get("pages"), "mode": r.get("mode"),
                     "errors_before": sb["errors"], "errors_after": sa["errors"],
                     "warnings_after": sa["warnings"], "pass_rate_after": f"{sa['percent']}%",
                     "figures_without_alt": st.get("missing_alt", 0), "verapdf": r.get("verapdf", ""),
                     "contrast_issues": cs.get("open", ""),
                     "status": "done", "output": out_pdf})
    write_batch_summary(os.path.join(a.out, "batch_summary.xlsx"), rows)
    if not a.keep_jobs:
        shutil.rmtree(work_root, ignore_errors=True)
    print(f"\n{len(files) - failed} done, {failed} failed · summary: {os.path.join(a.out, 'batch_summary.xlsx')}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
