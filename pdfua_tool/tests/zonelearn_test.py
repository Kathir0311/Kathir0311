"""V4.28 — the tool learns where people cut and join zones, and uses it for the next files.

A publisher sets its paragraphs apart by a small gap, so the analysis (a gap over 0.62 em, or a
short last line ending with a full stop) takes a whole page for one zone. People draw the
paragraphs; the tool learns what they changed:

* engine: four files of one client corrected → that client's zone model earns its place on a
  held-out check (fixed ≫ broken) and the next file has every paragraph in a zone of its own;
  another client is not touched by it
* a second client with the same habit → the model for everybody wakes up (it waits for two
  clients), and a client that has never been seen gets the paragraphs right as well
* the page-parallel analysis (24+ pages) decides exactly as the single-process one

Through the app (the offer of the same cut, the learner process, approval): zonelearn_app_test.py.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp()
os.environ["PDFUA_JOBS"] = os.path.join(TMP, "jobs")
os.environ["PDFUA_CONFIG"] = os.path.join(TMP, "config.json")
os.environ["PDFUA_USERS"] = os.path.join(TMP, "users.json")
os.environ["PDFUA_SAMPLES"] = os.path.join(TMP, "samples")


def main() -> int:
    from tests.zone_gen import make_pub
    from core import edit, zonelearn
    from core.options import Options, profile_options
    from core.pipeline import load_auto_model, load_model, prepare, save_model
    bad: list = []

    def ok(cond, msg):
        if not cond:
            bad.append(msg)

    def body(m, pages=None):
        return len([n for n in m.nodes.values() if n.role == "P" and n.text and len(n.text) > 80
                    and (pages is None or n.page in pages)])

    def job_for(name, seed, client, procs=1, **kw):
        os.environ["PDFUA_PAGE_PROCS"] = str(procs)
        job = os.path.join(TMP, name)
        os.makedirs(job)
        truth = make_pub(os.path.join(job, "original.pdf"), seed=seed, **kw)
        o = profile_options("Default")
        o.use_samples = False
        o.ai_alt_text_auto = False
        o.existing_tags = "retag"
        o.client = client
        o.learn_zones = True
        prepare(job, o)
        return job, truth, o

    def correct_like_a_user(job, truth):
        """Delete each merged zone and draw one zone round every true paragraph."""
        m = load_model(job)
        wp = os.path.join(job, "working.pdf")
        o = Options()
        o.learn_zones = False
        for pno, paras in enumerate(truth):
            for nid in [n.id for n in m.nodes.values() if n.page == pno and n.role == "P" and n.text
                        and len(n.text) > 80 and n.bbox[1] > 60]:
                m.remove(nid, promote_children=False)
            for (x0, y0, x1, y1, _t) in paras:
                edit.create_from_rect(m, wp, pno, (x0 - 1, y0 - 1, x1 + 1, y1 + 1), "P", split=False, opts=o)
        save_model(job, m)
        return m

    def teach(prefix, client, seeds, **kw):
        for k, seed in enumerate(seeds):
            job, truth, o = job_for(f"{prefix}{k}", seed, client, **kw)
            m0 = load_model(job)
            ok(body(m0) < sum(len(t) for t in truth) / 2, f"{client}: the rule alone joins the paragraphs ({body(m0)})")
            m1 = correct_like_a_user(job, truth)
            ok(body(m1) == sum(len(t) for t in truth), f"{client}: the person's zones {body(m1)}")
            r = zonelearn.record_job(job, client, m1, load_auto_model(job, o), approved=True)
            ok(r.get("corrections", 0) >= 8, f"{client}: corrections found {r}")

    def models():
        return {m["scope"]: m for m in zonelearn.status()["models"]}

    # ------------------------------------------------------------------ 1. one client, four files
    n0 = zonelearn.status()
    ok(not [m for m in n0["models"] if m["status"] == "active"], "no model before anybody teaches")
    teach("pub", "Pub", [1, 2, 3, 4])
    r = zonelearn.train_all(force=True)
    mp = models().get("Pub") or {}
    ok(mp.get("status") == "active", f"Pub's model earned its place: {mp.get('status')} {mp.get('note')}")
    ok(mp.get("fixed", 0) >= 30 and mp.get("broken", 99) == 0, f"fixed {mp.get('fixed')} broken {mp.get('broken')}")
    ok((models().get("") or {}).get("status") == "waiting", "everybody's model waits for a second client")
    conv = (models().get("Pub") or {}).get("convention") or ""
    ok("new paragraph starts from" in conv and "0.62" in conv, f"the model says what it learned in a sentence: {conv!r}")

    job, truth, o = job_for("pub_next", 9, "Pub")
    m = load_model(job)
    ok(body(m) == sum(len(t) for t in truth), f"the next file of Pub: {body(m)} zones, {sum(len(t) for t in truth)} paragraphs")
    ok(not [w for w in m.warnings if "zone" in w.lower()], f"no zone warnings {m.warnings[:2]}")

    job, truth, o = job_for("other_next", 9, "Somebody Else")
    m = load_model(job)
    ok(body(m) < sum(len(t) for t in truth) / 2, f"another client is not touched by Pub's habit ({body(m)})")

    # a client whose rule already works is not changed
    job, truth, o = job_for("pub_wide", 5, "Pub", gap_pt=10.0)
    m = load_model(job)
    ok(body(m) == sum(len(t) for t in truth), f"a file with wide gaps stays right: {body(m)} of {sum(len(t) for t in truth)}")

    # ------------------------------------------------------------------ 2. a second client, then a new one
    teach("sec", "Second", [11, 12], gap_pt=4.5, size=10.5, pitch=13.0)
    zonelearn.train_all(force=True)
    mm = models()
    ok((mm.get("") or {}).get("status") == "active", f"everybody's model woke up: {(mm.get('') or {}).get('status')} {(mm.get('') or {}).get('note')}")
    ok(mm["Second"]["status"] in ("active", "waiting"), f"Second {mm['Second']['status']} {mm['Second']['note']}")
    job, truth, o = job_for("third", 21, "Third Agency", gap_pt=4.2, size=10.2, pitch=12.8)
    m = load_model(job)
    ok(body(m) == sum(len(t) for t in truth), f"a client never seen before, the same habit: {body(m)} of {sum(len(t) for t in truth)}")

    # the switch in Options: off → the rule alone
    os.environ["PDFUA_PAGE_PROCS"] = "1"
    job = os.path.join(TMP, "pub_off")
    os.makedirs(job)
    truth = make_pub(os.path.join(job, "original.pdf"), seed=9)
    o = profile_options("Default")
    o.use_samples = False
    o.ai_alt_text_auto = False
    o.existing_tags = "retag"
    o.client = "Pub"
    o.learn_zones = False
    prepare(job, o)
    ok(body(load_model(job)) < sum(len(t) for t in truth) / 2, "learn_zones off → the rule alone")

    # ------------------------------------------------------------------ 2b. the other way round: a double-spaced manuscript
    # every line is "far" from the next one, so the rule takes each line for a paragraph of its own; people join them
    ms = dict(pitch=24.0, size=12.0, gap_pt=14.0)

    def everything(m_):
        return len([n for n in m_.nodes.values() if n.role == "P" and n.text and len(n.text) > 20])    # not the page number

    for k, seed in enumerate([41, 42, 43]):
        job, truth, o = job_for(f"ms{k}", seed, "Manuscripts", **ms)
        m0 = load_model(job)
        ok(everything(m0) > 2 * sum(len(t) for t in truth), f"manuscript: the rule alone cuts every line ({everything(m0)} zones)")
        m1 = load_model(job)
        wp = os.path.join(job, "working.pdf")
        oo = Options()
        oo.learn_zones = False
        for pno, paras in enumerate(truth):
            for nid in [n.id for n in m1.nodes.values() if n.page == pno and n.role == "P" and n.text and len(n.text) > 20]:
                m1.remove(nid, promote_children=False)
            for (x0, y0, x1, y1, _t) in paras:
                edit.create_from_rect(m1, wp, pno, (x0 - 1, y0 - 1, x1 + 1, y1 + 1), "P", split=False, opts=oo)
        save_model(job, m1)
        r = zonelearn.record_job(job, "Manuscripts", m1, load_auto_model(job, o), approved=True)
        ok(r.get("corrections", 0) >= 8, f"manuscript: corrections found {r}")
    zonelearn.train_all(force=True)
    mm = models().get("Manuscripts") or {}
    ok(mm.get("status") == "active" and mm.get("broken", 99) == 0, f"manuscripts: {mm.get('status')} {mm.get('note')} fixed {mm.get('fixed')} broken {mm.get('broken')}")
    ok("belong to one paragraph" in (mm.get("convention") or ""), f"manuscripts: {mm.get('convention')!r}")
    job, truth, o = job_for("ms_next", 49, "Manuscripts", **ms)
    ok(everything(load_model(job)) == sum(len(t) for t in truth),
       f"the next manuscript: {everything(load_model(job))} zones for {sum(len(t) for t in truth)} paragraphs")
    # the habit of the manuscripts does not leak into the publisher that sets paragraphs apart by a small gap
    job, truth, o = job_for("pub_after_ms", 9, "Pub")
    ok(body(load_model(job)) == sum(len(t) for t in truth), f"Pub is still right after the manuscripts: {body(load_model(job))}")

    # ------------------------------------------------------------------ 3. the page-parallel analysis decides the same
    a, ta, oa = job_for("serial", 3, "Pub", procs=1, pages=26, per_page=4)
    b, tb, ob = job_for("parallel", 3, "Pub", procs=2, pages=26, per_page=4)
    ma, mb = load_model(a), load_model(b)
    ok(mb.info.get("parallel_page_procs") == 2, f"the second file was analysed on 2 processes {mb.info.get('parallel_page_procs')}")
    ok(body(ma) == body(mb) == sum(len(t) for t in ta), f"serial {body(ma)} = parallel {body(mb)} = truth {sum(len(t) for t in ta)}")
    pa, pb = zonelearn.load_pairs(a), zonelearn.load_pairs(b)
    ok(pa is not None and pb is not None and len(pa["h"]) == len(pb["h"]) and (pa["vec"] == pb["vec"]).all(),
       "the same pairs of lines are logged")
    os.environ["PDFUA_PAGE_PROCS"] = "1"

    if bad:
        print("zone learning test: FAILED")
        for b_ in bad:
            print("   ", b_)
        return 1
    print("zone learning test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
