"""V4.27 — the self-learning gets more honest and more consistent.

* **honest probabilities**: the temperature that makes the trained model's probabilities match how often it
  is right (fitted on files it did not train on) is found, saved inside the model and used; a model saved
  before V4.27 still loads; the accuracy is measured by cross-validation over files (every file held out
  once), the same data always giving the same folds
* **recent examples count a little more**; **trust forgets**: a source is judged by about its last 600
  decisions
* **one file, one convention**: the verdicts the brain is sure of are pooled over the tags of the same look
  in the file, leaving each tag's own out — a tag whose own evidence is moderate follows the sure ones, an
  unsure verdict is not shared, different looks do not mix; the same file with and without that voice
* a heading the brain made takes the level the file gives the others of its look; the doubtful tags to check
  first are ordered by what their answer teaches
* **learned code**: fixes of one kind in several looks get a tight rule each (and the broad rule of an older
  version is retired); a rule learned from one place waits for a second file; a rule wrong in one of its
  files waits; a rule already on keeps working
* **memory**: the newest confirmed description wins
* the database of an older version gets its new columns; the command line and the admin page report them
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp()
os.environ["PDFUA_JOBS"] = os.path.join(TMP, "jobs")
os.environ["PDFUA_CONFIG"] = os.path.join(TMP, "config.json")
os.environ["PDFUA_USERS"] = os.path.join(TMP, "users.json")
os.environ["PDFUA_SAMPLES"] = os.path.join(TMP, "samples")
os.environ["PDFUA_NETGUARD"] = "off"

bad: list = []


def ok(cond, msg):
    if not cond:
        bad.append(msg)


# ---------------------------------------------------------------- synthetic examples for the model
def synthetic_rows(n_files=8, per=90, noise=0.8, seed=3):
    import random

    from core import brain
    rnd = random.Random(seed)
    words = {"P": "the study found that samples were stable over time and across sites".split(),
             "H2": "methods results discussion background overview summary aims data".split(),
             "H3": "participants procedure measures analysis design setting".split()}
    rows = []
    for fi in range(n_files):
        font = rnd.choice(["helvetica", "times", "rockwell", "arial"])
        for _ in range(per):
            role = rnd.choices(["P", "H2", "H3"], [0.8, 0.12, 0.08])[0]
            base = {"P": 1.0, "H2": 1.25, "H3": 1.1}[role]
            f = {"size_rel": base + rnd.gauss(0, noise * 0.4), "bold": int(role != "P" and rnd.random() > noise / 2),
                 "italic": 0, "lines": 1 if role != "P" else rnd.randint(1, 6),
                 "words": rnd.randint(1, 4) if role != "P" else rnd.randint(8, 60), "x": 0.1, "y": rnd.random(),
                 "w": 0.5, "centered": 0, "starts_num": 0, "caps": 1.0 if (role == "H2" and rnd.random() > noise / 2) else 0.05,
                 "ends_stop": int(role == "P"), "first_page": 0, "cap_word": 0, "font": font}
            text = " ".join(rnd.choice(words[role] if rnd.random() > noise / 2 else words["P"])
                            for _ in range(f["words"]))
            rows.append((brain.dense(f), brain.tokens(f, text, "c", "report"), role, 1.0, f"s{fi}"))
    return rows


def part_1_probabilities():
    import numpy as np

    from core import brain
    # an over-confident model: the true scores, three times too sure
    rnd = np.random.default_rng(4)
    n, C = 600, 3
    true = rnd.normal(size=(n, C))
    p = brain.softmax(true, 1.0)
    y = np.array([rnd.choice(C, p=row) for row in p])
    t = brain.fit_temperature(true * 3.0, y)
    ok(2.4 <= t <= 3.6, f"an over-confident model is told so (temperature {t}, expected about 3)")
    ece_raw = brain.calibration_error(brain.softmax(true * 3.0, 1.0), y)
    ece_cal = brain.calibration_error(brain.softmax(true * 3.0, t), y)
    ok(ece_cal < ece_raw * 0.5, f"calibration error falls ({ece_raw:.3f} → {ece_cal:.3f})")
    t1 = brain.fit_temperature(true, y)
    ok(0.8 <= t1 <= 1.25, f"an honest model keeps its temperature near 1 ({t1})")
    ok(brain.fit_temperature(true[:5], y[:5]) == 1.0, "too few examples → no change")

    # the temperature is part of the model, and an older model still loads
    rows = synthetic_rows()
    classes = ["H2", "H3", "P"]
    m = brain.RoleModel.fit([r[0] for r in rows[:300]], [r[1] for r in rows[:300]], [r[2] for r in rows[:300]],
                            [r[3] for r in rows[:300]], classes, epochs=40)
    P1 = m.predict([r[0] for r in rows[300:310]], [r[1] for r in rows[300:310]])
    m.T = 2.0
    P2 = m.predict([r[0] for r in rows[300:310]], [r[1] for r in rows[300:310]])
    ok((P2.max(1) <= P1.max(1) + 1e-9).all() and (P2.argmax(1) == P1.argmax(1)).all(),
       "a higher temperature softens the probabilities and never changes the answer")
    back = brain.RoleModel.from_bytes(m.to_bytes())
    ok(abs(back.T - 2.0) < 1e-6, f"the temperature is saved with the model ({back.T})")
    old = io.BytesIO()
    z = np.load(io.BytesIO(m.to_bytes()), allow_pickle=False)
    np.savez_compressed(old, **{k: z[k] for k in z.files if k != "T"})          # a V4.26 model has no T
    ok(brain.RoleModel.from_bytes(old.getvalue()).T == 1.0, "a model saved before V4.27 still loads (temperature 1)")

    # cross-validation over files
    folds = brain._grouped_folds(rows, 4)
    held = [sorted({rows[i][4] for i in te}) for _, te in folds]
    flat = [g for h in held for g in h]
    ok(len(folds) == 4 and len(flat) == len(set(flat)) == 8, f"every file is held out exactly once: {held}")
    ok(all(not ({rows[i][4] for i in tr} & {rows[i][4] for i in te}) for tr, te in folds),
       "a file is never in the training and the test part of one round")
    ok([sorted(te) for _, te in brain._grouped_folds(rows, 4)] == [sorted(te) for _, te in folds],
       "the same data gives the same folds")
    ok(brain._grouped_folds(rows[:60], 4) == [], "a single file cannot be cross-validated")
    ev, T, k = brain.evaluate(rows, classes)
    ok(k == 4 and ev["n"] == len(rows), f"the accuracy is measured on all {len(rows)} examples in {k} rounds")
    ok(ev["accuracy"] is not None and ev["accuracy"] > ev["baseline"], f"better than guessing {ev['accuracy']} vs {ev['baseline']}")
    ok(ev["ece"] <= ev["ece_raw"] + 0.003 and T > 0, f"calibration {ev['ece_raw']} → {ev['ece']} (T {T})")
    ev0, T0, k0 = brain.evaluate(rows[:10], classes)
    ok(k0 == 0 and T0 == 1.0 and ev0["accuracy"] is None, "too few examples → nothing measured")

    # recent examples count a little more
    now = datetime(2026, 10, 10)
    ok(abs(brain.recency("2026-10-10T08:00:00", now) - 1.0) < 0.01, "today's example counts in full")
    old_w = brain.recency((now - timedelta(days=730)).isoformat(), now)
    ok(0.65 < old_w < 0.75, f"two years old {old_w}")
    ok(brain.recency((now - timedelta(days=4000)).isoformat(), now) < 0.62, "very old → the floor")
    ok(brain.recency(None, now) == 1.0 and brain.recency("garbage", now) == 1.0, "no date → full weight")

    # trust forgets
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(brain.SCHEMA)
    c.execute("INSERT INTO trust VALUES ('model','*',900,1000)")
    c.execute("INSERT INTO trust VALUES ('model','H2',50,60)")
    ok(brain.forget_trust(c) == 1, "one count is past the window")
    r = {(x["source"], x["role"]): (x["ok"], x["n"]) for x in c.execute("SELECT * FROM trust")}
    ok(r[("model", "*")] == (540, 600) and r[("model", "H2")] == (50, 60), f"scaled back together, share kept: {r}")
    c.execute("UPDATE trust SET ok = ok + 12, n = n + 12 WHERE role='*'")
    ok(c.execute("SELECT ok*1.0/n r FROM trust WHERE role='*'").fetchone()["r"] > 0.9,
       "the next results weigh more than before")


# ---------------------------------------------------------------- one file, one convention
class FakeNode:
    def __init__(self, nid, role, page=0):
        self.id, self.role, self.page, self.style, self.note, self.confidence = nid, role, page, {}, "", 0.7
        self.text = nid


class FakeModel:
    def __init__(self, nodes):
        self.nodes = {n.id: n for n in nodes}


def look(font="helvetica", size=1.0, bold=0, caps=1.0):
    return {"font": font, "size_rel": size, "bold": bold, "italic": 0, "caps": caps}


def part_2_layout():
    from core import brain
    ctx = brain.Context.__new__(brain.Context)
    ctx.trust = {}
    sure = {"rules": {"P": 0.55}, "model": {"H2": 0.9}, "samples": {"H2": 0.9}}
    mid = {"rules": {"P": 0.55}, "model": {"H2": 0.7, "P": 0.3}}
    alone = {"rules": {"P": 0.55}, "model": {"H2": 0.9}}                  # one learned source only: not sure enough
    cands = [(FakeNode(f"a{i}", "P"), "P", f"HEAD {i}", look()) for i in range(7)]
    ops = [dict(sure), dict(sure), dict(sure), dict(sure), dict(sure), dict(mid), dict(alone)]
    lay = brain.layout_voices(ctx, cands, ops)
    ok(lay[5] is not None and max(lay[5]["dist"], key=lay[5]["dist"].get) == "H2" and lay[5]["n"] == 5 and lay[5]["firm"],
       f"a tag of moderate evidence hears the five sure ones: {lay[5]}")
    ok(abs(lay[5]["dist"]["H2"] - 5 / 8) < 0.01, f"five voices speak at 5/8 strength: {lay[5]['dist']}")
    ok(lay[0]["n"] == 4 and lay[0]["firm"], f"a sure tag hears the other four, not itself: {lay[0]}")
    ok(lay[6]["n"] == 5, "a tag with one learned source is not sure enough to be heard, but hears the others")
    # not sure → not shared: with only moderate/lone verdicts nobody speaks
    lay2 = brain.layout_voices(ctx, cands, [dict(mid)] * 4 + [dict(alone)] * 3)
    ok(all(v is None for v in lay2), "unsure verdicts are not shared")
    # too few sure ones
    lay3 = brain.layout_voices(ctx, cands, [dict(sure), dict(sure), dict(mid)] + [dict(mid)] * 4)
    ok(all(v is None for v in lay3), "two sure tags are not a convention (3 needed)")
    # three sure others: they speak, but not firmly (4 needed to count as an agreeing source)
    lay4 = brain.layout_voices(ctx, cands, [dict(sure)] * 3 + [dict(mid)] + [dict(rules_only := {"rules": {"P": 0.9}})] * 3)
    ok(lay4[3] is not None and lay4[3]["n"] == 3 and not lay4[3]["firm"], f"three voices nudge only: {lay4[3]}")
    ok(lay4[4] is not None and lay4[4]["n"] == 3 and not lay4[4]["firm"],
       "a tag nothing is learned about hears the sure ones too (it only has the analysis' word)")
    lay4b = brain.layout_voices(ctx, cands[:4], [dict(sure)] * 3 + [dict(mid)])
    ok(all(v is None for v in lay4b[:3]), "three tags: each hears only two others — not a convention")
    # other looks do not mix
    cands2 = [(FakeNode(f"b{i}", "P"), "P", f"X{i}", look(size=1.0 if i < 5 else 1.5)) for i in range(7)]
    lay5 = brain.layout_voices(ctx, cands2, [dict(sure)] * 5 + [dict(mid)] * 2)
    ok(lay5[5] is None and lay5[6] is None, "tags of another look hear nothing from the first look")
    # a split cluster is not firm
    lay6 = brain.layout_voices(ctx, cands, [dict(sure)] * 3 + [{"rules": {"P": 0.9}, "model": {"P": 0.9},
                                                                "samples": {"P": 0.9}}] * 3 + [dict(mid)])
    ok(lay6[6] is None, f"a look that is a heading in half the file and a paragraph in the other half is silent: {lay6[6]}")
    # five sure ones, one of them the other way: a convention with an exception is still a convention, only quieter
    sure_p = {"rules": {"P": 0.55}, "model": {"P": 0.9}, "samples": {"P": 0.9}}
    lay7 = brain.layout_voices(ctx, cands, [dict(sure)] * 4 + [dict(sure_p)] + [dict(mid)] * 2)
    ok(lay7[5] is not None and lay7[5]["n"] == 5 and lay7[5]["firm"] and lay7[5]["dist"]["H2"] < 5 / 8,
       f"four to one: heard, firmly, but quieter than five to nothing {lay7[5]}")
    lay8 = brain.layout_voices(ctx, cands, [dict(sure)] * 3 + [dict(sure_p)] * 2 + [dict(mid)] * 2)
    ok(lay8[5] is None or (not lay8[5]["firm"] and lay8[5]["dist"].get("H2", 0) < 0.1), f"three to two: no convention {lay8[5]}")

    # headings take the level the file gives the others of their look
    nodes = [FakeNode("h1", "H2"), FakeNode("h2", "H2"), FakeNode("h3", "H2"), FakeNode("h4", "H3"),
             FakeNode("h5", "H3"), FakeNode("h6", "H3")]
    model = FakeModel(nodes)
    cs = [(n, n.role, n.id, look()) for n in nodes[:4]] + [(nodes[4], "H3", "h5", look(size=1.4)), (nodes[5], "H3", "h6", look(size=1.4))]
    out = {"changes": []}
    moved = brain._unify_levels(model, cs, {"h4": 0.6, "h5": 0.6}, out)
    ok(moved == 1 and nodes[3].role == "H2" and nodes[4].role == "H3", f"the odd level is aligned: {[n.role for n in nodes]}")
    ok("same look" in out["changes"][0]["why"] and "H2" in nodes[3].note and nodes[3].style.get("level") == 2, "noted")
    nodes[3].role = "H3"
    moved = brain._unify_levels(model, cs, {"h4": 0.9}, {"changes": []})
    ok(moved == 0 and nodes[3].role == "H3", "a level the brain was sure of is kept")
    moved = brain._unify_levels(FakeModel(nodes[:2] + [nodes[3]]), [cs[0], cs[1], cs[3]], {"h4": 0.5}, {"changes": []})
    ok(moved == 0, "two other headings are not enough to decide a level")

    # which doubtful tags to look at first
    cs2 = [(FakeNode("u1", "P"), "P", "ONE", look()), (FakeNode("u2", "P"), "P", "TWO", look()),
           (FakeNode("u3", "P"), "P", "THREE", look()), (FakeNode("u4", "P"), "P", "FOUR", look(size=2.0)),
           (FakeNode("u5", "P"), "P", "FIVE", look(size=1.5))]
    confs = [[0.55, 1, 0], [0.5, 1, 1], [0.9, 1, 2], [0.3, 1, 3], [0.2, 1, 4]]
    rev = brain._review_first(cs2, confs, {"u2"})
    ids = [x["id"] for x in rev]
    ok(ids == ["u2", "u1", "u5", "u4"], f"ordered by what the answer teaches (doubt × look-alikes): {ids}")
    ok("u3" not in ids and all(x["confidence"] < 0.6 for x in rev), "the tags it is sure of are not listed")
    ok(next(x for x in rev if x["id"] == "u1")["same_look"] == 2, "…with how many look the same")
    ok(next(x for x in rev if x["id"] == "u2")["changed"] is True, "…and whether the brain changed it")
    s_by = {x["id"]: x["same_look"] for x in rev}
    ok(s_by["u4"] == 0 and s_by["u5"] == 0, "a lone look has no look-alikes")


# ---------------------------------------------------------------- the same file with and without the voice
def part_3_file():
    from core import brain, learn
    from core.options import SMART_PROFILE, profile_options
    from core.pipeline import load_model, prepare
    from tests.samples_gen import make, make_tagged
    for k, heads in enumerate([[["KEY POINTS", "BACKGROUND", "METHODS"], ["RESULTS", "DISCUSSION", "LIMITS"]],
                               [["OVERVIEW", "THE PLAN", "SCOPE"], ["WHAT WE DID", "OUTCOMES", "LIMITS"]],
                               [["INTRODUCTION", "AIMS", "DATA"], ["ANALYSIS", "SUMMARY", "NEXT STEPS"]]]):
        d = os.path.join(TMP, f"mk{k}")
        os.makedirs(d)
        p = os.path.join(TMP, f"sample_{k}.pdf")
        make_tagged(p, d, heads=heads)
        sid = learn.add_sample("Journal X", os.path.basename(p), p, kind="tagged")
        r = learn.learn_sample(sid)
        ok(r.get("ok"), f"sample {k} learned {r}")
    t = brain.train(trigger="test", force=True)
    ok(t.get("ok") and t.get("folds", 0) >= 2 and t.get("temperature", 0) > 0 and t.get("ece") is not None,
       f"the brain trained with cross-validation and calibration {t}")
    model, meta = brain.latest_model(refresh=True)
    ok(model is not None and abs(model.T - meta["temperature"]) < 1e-3 and meta["folds"] == t["folds"],
       f"the saved model carries its temperature {meta and meta.get('temperature')}")
    st = brain.status()
    ok(st["model"]["temperature"] == meta["temperature"] and st["model"]["ece"] is not None
       and st["trainings"][-1]["folds"] == t["folds"], "the admin status reports them")
    import cli
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli.main(["--brain-status"])
    ok("calibrated" in buf.getvalue() and "temperature" in buf.getvalue(), f"the command line says so: {buf.getvalue()[:300]}")

    a1 = brain.cached_context("Journal X", "client", None, "journal_article")
    ok(brain.cached_context("Journal X", "client", None, "journal_article") is a1,
       "the popover's context is kept while nothing new was learned")
    ok(brain.cached_context("Journal X", "all", {"x": 1.0}, "journal_article") is not a1, "…and is per client / scope / samples")
    job = os.path.join(TMP, "layout")
    os.makedirs(job)
    make(os.path.join(job, "original.pdf"),
         [["ALPHA ONE", "BETA TWO", "GAMMA THREE", "DELTA FOUR"], ["EPSILON FIVE", "ZETA SIX", "ETA SEVEN"]], volume=5)
    od = profile_options("Default")
    od.use_samples = False
    od.ai_alt_text_auto = False
    od.existing_tags = "retag"
    prepare(job, od)
    SURE = {"ALPHA ONE", "BETA TWO", "GAMMA THREE", "DELTA FOUR", "EPSILON FIVE"}
    MID = {"ZETA SIX", "ETA SEVEN"}

    def fake(ctx, role, conf, text, f, probs=None, fired=None):
        if text in SURE:
            return {"rules": {"P": 0.55}, "model": {"H2": 0.9}, "samples": {"H2": 0.9}}
        if text in MID:
            return {"rules": {"P": 0.55}, "model": {"H2": 0.7, "P": 0.3}}
        return {"rules": {role: 0.9}}
    real_ops, real_lay = brain.opinions, brain.layout_voices

    def run(with_layout):
        brain.opinions = fake
        if not with_layout:
            brain.layout_voices = lambda ctx, cands, ops, *a, **k: [None] * len(cands)
        try:
            mdl = load_model(job)
            out = brain.think(mdl, os.path.join(job, "working.pdf"), profile_options(SMART_PROFILE), client="Journal X",
                              job="lay-on" if with_layout else "lay-off")
            return mdl, out
        finally:
            brain.opinions, brain.layout_voices = real_ops, real_lay
    mdl, out = run(False)
    got = {n.text: n.role for n in mdl.nodes.values() if n.text in SURE | MID}
    ok({k for k, v in got.items() if v == "H2"} == SURE and {k for k, v in got.items() if v == "P"} == MID,
       f"without the voice, the moderate evidence is not enough: {got}")
    ok(out["layout_voices"] == 0, "no layout voices when it is switched off")
    rf = {x["text"] for x in out["review_first"]}
    ok(MID <= rf, f"the two doubtful heads are the ones to check first: {out['review_first']}")
    ok(all(x["same_look"] >= 6 for x in out["review_first"] if x["text"] in MID), "…with the seven of that look counted")
    mdl, out = run(True)
    got = {n.text: n.role for n in mdl.nodes.values() if n.text in SURE | MID}
    ok(set(got.values()) == {"H2"}, f"with the voice, the whole look follows the sure ones: {got}")
    ok(out["layout_voices"] == 7 and out["changed"] == 7, f"seven voices, seven heads {out['layout_voices']} {out['changed']}")
    notes = {n.text: n.note for n in mdl.nodes.values() if n.text in MID}
    ok(all("layout" in v for v in notes.values()), f"the note names the voice: {notes}")
    ok(not (MID & {x["text"] for x in out["review_first"]}), "nothing left to check")
    with learn.connect() as db:
        ops = [json.loads(r["opinions"]) for r in db.execute("SELECT opinions FROM decisions WHERE job='lay-on'")]
    ok(sum(1 for o in ops if "layout" in o) == 7, "the layout voice is recorded, so its trust can be learned")
    ok(any(s["source"] == "layout" for s in brain.status()["trust"]), "…and shown with the other sources")
    sid = learn.add_sample("Journal X", "more.pdf", os.path.join(TMP, "sample_0.pdf"), kind="tagged")
    learn.learn_sample(sid)
    ok(brain.cached_context("Journal X", "client", None, "journal_article") is not a1, "a new sample renews it")


# ---------------------------------------------------------------- learned code
def part_4_code():
    import random

    from core import codegen
    from core import learn as L
    rnd = random.Random(5)
    os.environ["PDFUA_SAMPLES"] = os.path.join(TMP, "samples_code")
    codegen._ACTIVE["t"] = 0.0
    c = L.connect()

    def add_sample(sid, client="Pub"):
        c.execute("INSERT INTO samples (id, client, filename, kind, created, learned, status, enabled) "
                  "VALUES (?,?,?,?,?,?,?,1)", (sid, client, sid + ".pdf", "approved", "2026-10-01T10:00:00",
                                               "2026-10-01T10:00:00", "ready"))

    def feat(size, bold, caps, words, font, y=0.3):
        return dict(size_rel=size, bold=bold, italic=0, lines=1, words=words, x=0.1, y=y, w=0.5, centered=0,
                    starts_num=0, caps=caps, ends_stop=0, first_page=0, cap_word=0, font=font)

    def add_block(sid, role, f, text, fix_from=None, client="Pub"):
        cols = ("role", "orig", "parent", "text", "norm", "size_rel", "bold", "italic", "lines", "words", "x", "y", "w",
                "centered", "starts_num", "caps", "ends_stop", "first_page", "cap_word", "font")
        d = dict(f, role=role, orig="", parent="Sect", text=text, norm=L.norm(text))
        c.execute(f"INSERT INTO blocks (sample_id, client, {', '.join(cols)}) VALUES (?,?,{','.join('?' * len(cols))})",
                  (sid, client, *[d.get(k) for k in cols]))
        if fix_from:
            cc = ("from_role", "role", "text", "norm") + L.FEATURES + ("font",)
            d2 = dict(f, from_role=fix_from, role=role, text=text, norm=L.norm(text))
            c.execute(f"INSERT INTO corrections (sample_id, client, {', '.join(cc)}) VALUES (?,?,{','.join('?' * len(cc))})",
                      (sid, client, *[d2.get(k) for k in cc]))

    def body(sid, n=40):
        for i in range(n):
            add_block(sid, "P", feat(1.0, 0, 0.03, 30, "times", rnd.random()), f"Body text {i} of the file lorem ipsum")
    # the chapter titles (bold slab serif, big) and the section heads (plain capitals) both become H2, in 4 files
    for k in range(4):
        sid = f"S{k}"
        add_sample(sid)
        for i in range(3):
            add_block(sid, "H2", feat(1.8 + rnd.uniform(-0.03, 0.03), 1, 0.1, 3, "rockwell", 0.1), f"Chapter {i} title", "P")
        for i in range(3):
            add_block(sid, "H2", feat(1.0, 0, 1.0, 2, "helvetica"), f"SECTION {i}", "P")
        body(sid)
    # a heading style in a single file, five fixes: learned from one place only
    add_sample("S9")
    for i in range(5):
        add_block("S9", "H3", feat(1.3, 1, 0.1, 3, "georgia"), f"Note heading {i}", "P")
    body("S9")
    c.commit()
    # an older version had written one broad rule for all the H2 fixes
    codegen.ensure(c)
    c.execute("INSERT INTO code_rules (id, slot, version, created, updated, status, target, from_roles, client, conditions, "
              "source, sha, origin, positives, verified_pos, matched, verified_matched, precision, description, note) "
              "VALUES ('R0001','H2|P|','1','x','x','active','H2','[\"P\"]','','[]','','','induction',1,1,1,1,1,'old','')")
    c.commit()
    c.close()
    r = codegen.synthesize()
    rules = {x["slot"]: x for x in codegen.listing()}
    h2 = {k: v for k, v in rules.items() if v["target"] == "H2" and k != "H2|P|"}
    ok(r["looks"] >= 2 and len(h2) == 2, f"one tight rule for each look of the H2 fixes: {r} {list(rules)}")
    ok(any("font == rockwell" in v["description"] and "bold" in v["description"] and "size × body 1.7" in v["description"]
           for v in h2.values()), f"the chapter-title rule is as tight as its fixes: {[v['description'] for v in h2.values()]}")
    ok(any("font == helvetica" in v["description"] and "in capitals" in v["description"] for v in h2.values()),
       "…and so is the section-head rule")
    ok(all(v["status"] == "active" and v["files"] == 4 and v["min_file_precision"] == 1.0 for v in h2.values()),
       f"both are on, seen to work in four files {[(v['status'], v['files'], v['min_file_precision']) for v in h2.values()]}")
    ok(rules["H2|P|"]["status"] == "retired" and "narrower rules" in rules["H2|P|"]["note"],
       f"the broad rule of the older version is replaced: {rules['H2|P|']['status']} {rules['H2|P|']['note']}")
    for v in h2.values():
        codegen.validate(v["source"])
    solo = [v for v in rules.values() if v["target"] == "H3"]
    ok(solo and solo[0]["status"] == "candidate" and "one file" in solo[0]["note"] and solo[0]["files"] == 1,
       f"a rule learned in one place waits: {[(v['status'], v['note']) for v in solo]}")

    # a second file shows the same fixes → it is switched on
    c = L.connect()
    add_sample("S10")
    for i in range(5):
        add_block("S10", "H3", feat(1.3, 1, 0.1, 3, "georgia"), f"Other heading {i}", "P")
    body("S10")
    c.commit()
    c.close()
    codegen.synthesize()
    solo = [v for v in codegen.listing() if v["target"] == "H3"]
    ok(solo and solo[0]["status"] == "active" and solo[0]["files"] == 2 and solo[0]["note"] == "",
       f"…and a second file switches it on {[(v['status'], v['files'], v['note']) for v in solo]}")
    # …it keeps working when the second file goes away (it is the same rule, already on)
    c = L.connect()
    c.execute("UPDATE samples SET enabled=0 WHERE id='S10'")
    c.commit()
    c.close()
    codegen.synthesize()
    solo = [v for v in codegen.listing() if v["target"] == "H3"]
    ok(solo and solo[0]["status"] == "active", f"a rule already on is not switched off by the new checks {[(v['status'], v['files']) for v in solo]}")

    # wrong in one of its files: right in the book, wrong in the leaflet (the same look, the same words)
    c = L.connect()
    for sid in ("S20", "S21"):
        add_sample(sid)
    for i in range(40):
        add_block("S20", "H4", feat(1.15, 1, 0.1, 3, "verdana"), f"Deep heading {i % 3}", "P")
    for i in range(3):                                       # the leaflet prints that look as plain paragraphs
        add_block("S21", "P", feat(1.15, 1, 0.1, 3, "verdana"), f"Deep heading {i % 3}")
    body("S20")
    body("S21")
    c.commit()
    c.close()
    codegen.synthesize()
    h4 = [v for v in codegen.listing() if v["target"] == "H4"]
    ok(h4 and h4[0]["status"] == "candidate" and "wrong too often" in h4[0]["note"] and h4[0]["min_file_precision"] == 0.0
       and h4[0]["precision"] >= 0.93,
       f"right overall, wrong in one file → waits: {[(v['status'], v['note'], v['precision'], v['min_file_precision']) for v in h4]}")
    ok(not any(v["status"] == "active" for v in h4), "…and is not used")


# ---------------------------------------------------------------- memory
def part_5_memory():
    from core import memory
    rows = [("A", "old alt", 1, "j1", "2026-01-01T10:00:00"), ("A", "old alt", 1, "j2", "2026-02-01T10:00:00"),
            ("A", "better alt", 1, "j3", "2026-09-01T10:00:00")]
    ok(memory._best(rows, "A", False)[0] == "better alt", "the description people confirmed last wins, not the one written most")
    rows = [("A", "old alt", 1, "j1", "2026-01-01T10:00:00"), ("A", "new unconfirmed", 0, "j2", "2026-09-01T10:00:00"),
            ("A", "new unconfirmed", 0, "j3", "2026-09-02T10:00:00")]
    ok(memory._best(rows, "A", False)[0] == "old alt", "a confirmed description beats a newer unconfirmed one")
    ok(memory._best([("B", "other client", 1, "j1", "2026-09-01T10:00:00"), ("A", "mine", 1, "j2", "2026-01-01T10:00:00")],
                    "A", True)[0] == "mine", "the client's own memory still comes first")
    ok(memory._best([("A", "x", 1, "j1")], "A", False)[0] == "x", "rows without a date still work")
    ok(memory._best([("B", "x", 0, "j1", "2026-01-01")], "A", True) is None, "unconfirmed, one file, another client: unused")


# ---------------------------------------------------------------- an older database
def part_6_upgrade():
    from core import brain, codegen
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
CREATE TABLE brain_models (id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, n_train INTEGER, n_samples INTEGER,
  classes TEXT, accuracy REAL, macro_f1 REAL, baseline REAL, per_role TEXT, weights BLOB, signature TEXT, trigger TEXT, seconds REAL);
CREATE TABLE feedback (job TEXT, client TEXT, from_role TEXT, role TEXT, text TEXT, norm TEXT, font TEXT);
CREATE TABLE code_rules (id TEXT PRIMARY KEY, slot TEXT UNIQUE, version INTEGER DEFAULT 1, created TEXT, updated TEXT,
  status TEXT, target TEXT, from_roles TEXT, client TEXT, conditions TEXT, source TEXT, sha TEXT, origin TEXT,
  positives INTEGER, verified_pos INTEGER, matched INTEGER, verified_matched INTEGER, precision REAL, description TEXT, note TEXT);
""")
    brain.ensure(c)
    cols = {r[1] for r in c.execute("PRAGMA table_info(brain_models)")}
    ok({"logloss", "ece", "temperature", "folds"} <= cols, f"brain_models got its new columns: {cols}")
    codegen.ensure(c)
    cols = {r[1] for r in c.execute("PRAGMA table_info(code_rules)")}
    ok({"files", "min_file_precision"} <= cols, f"code_rules got its new columns: {cols}")
    brain.ensure(c)
    codegen.ensure(c)                                                   # twice is fine
    c.execute("INSERT INTO brain_models (created, n_train, classes) VALUES ('x', 1, '[]')")
    ok(brain._model_cols(c).startswith("id, created"), "the status query only asks for columns that exist")


def part_7_kick():
    """A training asked for too early is made when the pause is over, not forgotten."""
    import time

    import app as A
    calls, timers = [], []

    class FakeTimer:
        def __init__(self, delay, fn):
            self.delay, self.fn, self.daemon = delay, fn, False
            timers.append(self)

        def start(self):
            pass
    real = (A._brain_bg, A.threading.Timer)
    A._brain_bg = lambda args, what: calls.append(args) or True
    A.threading.Timer = FakeTimer
    try:
        A._KICK["timer"] = None
        A._BRAIN_JOB.update(last=time.time(), thread=None)
        A.kick_brain_training()
        A.kick_brain_training()
        ok(not calls and len(timers) == 1, f"too early: nothing now, one training later ({calls}, {len(timers)} timer)")
        ok(300 < timers[0].delay <= 610, f"…after the pause ({timers[0].delay:.0f} s)")
        A._BRAIN_JOB["last"] = 0.0
        timers[0].fn()
        ok(calls == [["train"]] and A._KICK["timer"] is None, f"…and then it trains {calls}")
        A._BRAIN_JOB["last"] = 0.0
        A.kick_brain_training()
        ok(calls == [["train"], ["train"]], "when the pause is over a request trains at once")
    finally:
        A._brain_bg, A.threading.Timer = real
        A._KICK["timer"] = None


def main() -> int:
    part_1_probabilities()
    part_2_layout()
    part_3_file()
    part_4_code()
    part_5_memory()
    part_6_upgrade()
    part_7_kick()
    if bad:
        print("self-improve test: FAILED")
        for b_ in bad:
            print("   ", b_)
        return 1
    print("self-improve test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
