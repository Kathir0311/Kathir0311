"""Zones learned from people (V4.28).

The analysis cuts the text of a page into zones — one tag, one box for each
paragraph — by looking at two lines at a time: the gap between them, the
indent, how short the line above is, how it ends. Where a publisher sets its
paragraphs with a smaller gap than the analysis expects, two paragraphs end up
in one zone and a person has to cut it. The fix is the same on every page of the
book, and on every page of the next book of that publisher.

This module learns that *cut* from people, live:

* **every boundary is a row.** While it analyses, the tool writes down, for each
  pair of consecutive lines it had to decide on, how the pair looks (the gap,
  the indent, the short line …, in units of the type size so it travels from
  book to book), what the PDF/UA analysis' own rule said (`h`) and what the
  tool did (`d`) — `zone_pairs.npz` in the job's folder. Nothing in the PDF has
  to be read again to learn: a person's edit is compared with these rows.
* **what people did is read from the tags.** After an edit — and at the build,
  and at the approval — every pair is looked up in the tags as they are now: both
  lines in one tag (*joined*) or in two (*cut*). Where that differs from what the
  tool did, a person corrected it: an explicit example, the strongest there is.
  Pairs on the pages a person corrected count too (they looked at them), at a
  lower weight; an approved file, or a finished file an admin uploaded as a sample,
  counts in full.
* **a small model learns the correction, not the rule.** The analysis' own rule
  is the starting point (an offset in the logit), and a ridge-regularised logistic
  regression over the pair's features learns what to add or take away. With
  nothing learned the answer is the analysis' answer; with corrections that agree
  — a gap of 0.4 em is a new paragraph in this publisher's books — the answer
  moves, only where the evidence is, and only as far as the margin allows (the
  analysis is overruled at 65 % / 35 %, not at 51 %).
* **a model has to earn its place.** Each model (one for every client, one for
  everybody) is tried on files — or, with fewer than three files, pages — it was
  not trained on, against the analysis' own rule: it is used only when it fixes
  more pairs than it spoils (at least three, and twice as many). Until then it
  waits, and the admin page says why.
* **the same fix, in this file, at once.** `similar()` finds the other places of
  the file that look like the one a person just fixed — same gap, same indent,
  the same kind of line above — and `apply()` cuts (or joins) them in one step
  (Undo puts them back); what is accepted is a correction like any other.

The structural decisions of the analysis (another column, another type size or
weight, a list label, a table of contents line, side by side lines) are not
learned: they are not a matter of taste. A house style's engine (core/compat.py)
and a Teacher's Guide are left alone.
"""
from __future__ import annotations

import collections
import io
import json
import math
import os
import re
import sqlite3
import time
import zlib
from datetime import datetime

from . import learn as L
from .locks import replace_file

VERSION = 1
NAMES = (
    "gap", "pitch", "lead_ratio", "has_lead", "indent", "short", "b_short",
    "a_stop", "a_comma", "a_hyph", "a_other", "b_upper", "b_lower", "b_digit", "b_sym",
    "dsize", "dbold", "ditalic", "same_font", "a_words", "b_words",
    "full_a", "indent_full", "gap_stop", "short_stop", "gap_full", "indent_stop",
)
NF = len(NAMES)
PRIOR = 2.5               # the analysis' own rule counts as 92 % sure (logit ±2.5)
MARGIN = 0.15             # a model overrules the rule only beyond 65 % / 35 %
LAMBDA = 1.0              # ridge: how much evidence a weight needs
MIN_ROWS = 40             # a model needs this many examples …
MIN_EXPLICIT = 4          # … of which this many are corrections
SOLO_EXPLICIT = 6         # corrections from one file only count when they are this many
EXPLICIT_WEIGHT = 4.0
TOUCHED_WEIGHT = 0.5      # a pair on a page a person corrected, left as it was (unapproved file)
FILE_WEIGHT = 1.0         # a pair of an approved / finished file
OTHER_CLIENT = 0.25       # the other clients' examples, for a client's own model
REVIEWER = 1.5            # what QA, final QA and the admin correct counts more
MAX_ROWS = 60000
SIMILAR = 0.7             # a file this like a client's samples is cut the way that client's files are
ELIGIBLE = {"P", "H1", "H2", "H3", "H4", "H5", "H6", "Caption", "Note", "BlockQuote", "Quote", "Title",
            "Code", "BibEntry", "Formula"}
NOT_TEXT_CONTEXT = {"L", "LI", "LBody", "Lbl", "Table", "TR", "TD", "TH", "TOC", "TOCI", "Figure", "Form", "Index"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _cl(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _np():
    try:
        import numpy as np
        return np
    except Exception:
        return None


# ================================================================ what a pair of lines looks like
def _fam(font: str) -> str:
    return re.split(r"[-,]", re.sub(r"^[A-Z]{6}\+", "", font or ""))[0].lower()


def features(a, b, prev, col_right: float) -> list[float]:
    """The pair (line a above line b) as numbers — in units of the type size, so the
    same publisher's next book looks the same. `prev` is the line above `a` (or None);
    nothing here depends on how the tool grouped the lines before: only on the page."""
    size = max(a.size, b.size, 1.0)
    gap = (b.y0 - a.y1) / size
    pitch = (b.y0 - a.y0) / size
    has_lead, lead_ratio = 0.0, 1.0
    if prev is not None and getattr(prev, "flow", None) == a.flow:
        lead = (a.y0 - prev.y0) / size
        if 0.4 < lead < 3.0 and pitch > 0:
            has_lead, lead_ratio = 1.0, _cl(pitch / lead, 0.3, 4.0)
    indent = _cl((b.x0 - a.x0) / size, -4.0, 8.0)
    short = _cl((col_right - a.x1) / size, -2.0, 14.0)
    b_short = _cl((col_right - b.x1) / size, -2.0, 14.0)
    ta, tb = a.text.rstrip(), b.text.lstrip()
    last, first = ta[-1:], tb[:1]
    a_stop = 1.0 if last and last in ".?!:”\"’)" else 0.0
    a_comma = 1.0 if last and last in ",;" else 0.0
    a_hyph = 1.0 if last and last in "-–—" else 0.0
    a_other = 1.0 - max(a_stop, a_comma, a_hyph)
    b_upper = 1.0 if first.isupper() else 0.0
    b_lower = 1.0 if first.islower() else 0.0
    b_digit = 1.0 if first.isdigit() else 0.0
    b_sym = 1.0 if first and not first.isalnum() else 0.0
    full_a = 1.0 if short < 0.6 else 0.0
    gapc = _cl(gap, -1.0, 3.0)
    return [round(float(v), 4) for v in (
        gapc, _cl(pitch, 0.0, 5.0), lead_ratio, has_lead, indent, short / 14.0, b_short / 14.0,
        a_stop, a_comma, a_hyph, a_other, b_upper, b_lower, b_digit, b_sym,
        _cl((b.size - a.size) / size, -1.0, 1.0), float(int(b.bold) - int(a.bold)),
        float(int(b.italic) - int(a.italic)), 1.0 if _fam(a.font) == _fam(b.font) else 0.0,
        min(len(ta.split()), 40) / 40.0, min(len(tb.split()), 40) / 40.0,
        full_a, indent * full_a / 8.0, gapc * a_stop, short / 14.0 * a_stop, gapc * full_a, indent * a_stop / 8.0)]


# ================================================================ the model
def _sigmoid(x):
    np = _np()
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30.0, 30.0)))


class ZoneModel:
    """logit = ±PRIOR (what the analysis' rule said) + b + w·z; z = the pair's features, standardised."""

    def __init__(self, mean, std, w, b: float, scope: str = "", meta: dict | None = None,
                 prior: float = PRIOR, margin: float = MARGIN):
        self.mean, self.std, self.w, self.b = list(mean), list(std), list(w), float(b)
        self.scope, self.meta = scope, dict(meta or {})
        self.prior, self.margin = float(prior), float(margin)
        np = _np()
        self._m = np.asarray(self.mean, dtype=np.float64)
        self._s = np.asarray(self.std, dtype=np.float64)
        self._w = np.asarray(self.w, dtype=np.float64)

    def prob(self, X, h):
        """P(a new paragraph starts here) for rows X (n × NF) given the analysis' answers h (n)."""
        np = _np()
        X = np.asarray(X, dtype=np.float64).reshape(-1, NF)
        Z = np.clip((X - self._m) / self._s, -6.0, 6.0)
        off = np.where(np.asarray(h).reshape(-1) >= 1, self.prior, -self.prior)
        return _sigmoid(off + self.b + Z @ self._w)

    def decide(self, vec, h) -> bool:
        """The answer for one pair: the analysis' own unless the evidence is clear."""
        p = float(self.prob([vec], [1 if h else 0])[0])
        if not h and p >= 0.5 + self.margin:
            return True
        if h and p <= 0.5 - self.margin:
            return False
        return bool(h)

    def decide_many(self, X, h):
        np = _np()
        p = self.prob(X, h)
        h = np.asarray(h).reshape(-1) >= 1
        return np.where(h, p > 0.5 - self.margin, p >= 0.5 + self.margin), p

    def to_dict(self) -> dict:
        return {"v": VERSION, "names": list(NAMES), "mean": [round(x, 6) for x in self.mean],
                "std": [round(x, 6) for x in self.std], "w": [round(x, 6) for x in self.w],
                "b": round(self.b, 6), "scope": self.scope, "prior": self.prior, "margin": self.margin,
                "meta": self.meta}

    @classmethod
    def from_dict(cls, d: dict) -> "ZoneModel | None":
        if not d or d.get("names") != list(NAMES):
            return None                                   # made for other features: not used
        return cls(d["mean"], d["std"], d["w"], d["b"], d.get("scope", ""), d.get("meta"),
                   d.get("prior", PRIOR), d.get("margin", MARGIN))

    def explain(self, top: int = 6) -> list[dict]:
        """The features that move the answer most (standardised weight) — for the admin."""
        rows = [{"feature": n, "weight": round(float(w), 2)} for n, w in zip(NAMES, self.w) if abs(w) >= 0.05]
        rows.sort(key=lambda r: -abs(r["weight"]))
        return rows[:top]


def fit(X, h, y, w, lam: float = LAMBDA, prior: float = PRIOR, iters: int = 30) -> ZoneModel:
    """Weighted ridge logistic regression of the correction on top of the analysis' rule (IRLS)."""
    np = _np()
    X = np.asarray(X, dtype=np.float64).reshape(-1, NF)
    h = np.asarray(h, dtype=np.float64).reshape(-1)
    y = np.asarray(y, dtype=np.float64).reshape(-1)
    w = np.asarray(w, dtype=np.float64).reshape(-1)
    sw = float(w.sum()) or 1.0
    mean = (X * w[:, None]).sum(0) / sw
    var = (((X - mean) ** 2) * w[:, None]).sum(0) / sw
    std = np.sqrt(var) + 1e-3
    std = np.maximum(std, 0.02)                           # a feature that hardly varies has no say
    Z = np.clip((X - mean) / std, -6.0, 6.0)
    A = np.hstack([Z, np.ones((len(Z), 1))])
    off = np.where(h >= 1, prior, -prior)
    reg = np.full(NF + 1, lam)
    reg[-1] = 0.3 * lam                                   # the bias is lightly held, too: no drift
    beta = np.zeros(NF + 1)
    for _ in range(iters):
        eta = off + A @ beta
        p = _sigmoid(eta)
        g = A.T @ (w * (p - y)) + reg * beta
        Wd = w * p * (1.0 - p) + 1e-9
        H = A.T @ (A * Wd[:, None]) + np.diag(reg)
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            break
        beta -= step
        if float(np.abs(step).max()) < 1e-6:
            break
    return ZoneModel(mean, std, beta[:-1], float(beta[-1]), prior=prior)


def _folds(groups: list, k: int) -> list[set]:
    """Group names → k sets, in an order fixed by the names (the same data, the same folds)."""
    uniq = sorted(set(groups), key=lambda g: (zlib.crc32(str(g).encode()), str(g)))
    k = max(2, min(k, len(uniq)))
    out: list[set] = [set() for _ in range(k)]
    for i, g in enumerate(uniq):
        out[i % k].add(g)
    return out


def validate(X, h, y, w, groups: list, own=None, folds: int = 5) -> dict:
    """How the model does on pairs it was not trained on, against the analysis' own rule.
    `own`: a mask — only these held-out pairs are counted (a client's model is judged on its
    client's pairs)."""
    np = _np()
    n = len(y)
    if n < 10 or len(set(groups)) < 2:
        return {"folds": 0, "n": 0, "fixed": 0, "broken": 0, "acc_rule": None, "acc_model": None}
    fixed = broken = wrong_rule = right_model = counted = 0
    gl = np.asarray([str(g) for g in groups])
    for held in _folds(list(gl), folds):
        te = np.isin(gl, list(held))
        tr = ~te
        if tr.sum() < 10 or te.sum() < 1 or float(w[tr].sum()) <= 0:
            continue
        m = fit(X[tr], h[tr], y[tr], w[tr])
        d, _ = m.decide_many(X[te], h[te])
        d = d.astype(int)
        yt, ht = y[te].astype(int), h[te].astype(int)
        mask = np.ones(te.sum(), dtype=bool) if own is None else np.asarray(own)[te]
        counted += int(mask.sum())
        rule_ok, model_ok = (ht == yt), (d == yt)
        fixed += int((mask & ~rule_ok & model_ok).sum())
        broken += int((mask & rule_ok & ~model_ok).sum())
        wrong_rule += int((mask & ~rule_ok).sum())
        right_model += int((mask & model_ok).sum())
    if not counted:
        return {"folds": 0, "n": 0, "fixed": 0, "broken": 0, "acc_rule": None, "acc_model": None}
    rule_right = counted - wrong_rule
    return {"folds": folds, "n": counted, "fixed": fixed, "broken": broken,
            "acc_rule": round(rule_right / counted, 4), "acc_model": round(right_model / counted, 4)}


def earns_place(v: dict, explicit: int, refs: int) -> tuple[bool, str]:
    """Is the model good enough to be used? (it must fix more than it spoils)"""
    if explicit < MIN_EXPLICIT:
        return False, f"{explicit} correction(s) so far — needs {MIN_EXPLICIT}"
    if refs < 2 and explicit < SOLO_EXPLICIT:
        return False, f"corrections from one file only ({explicit}) — waits for a second file or {SOLO_EXPLICIT}"
    if not v.get("folds"):
        return False, "not enough files or pages to try it on"
    if v["fixed"] < 3:
        return False, f"it fixes {v['fixed']} pair(s) it was not trained on — needs 3"
    if v["fixed"] < 2 * v["broken"] + 1:
        return False, f"it fixes {v['fixed']} but spoils {v['broken']} — not good enough"
    return True, ""


# ================================================================ the pairs of one analysis
def new_log() -> list:
    return []


def log_pair(log: list, pno: int, abox, bbox, vec, h: bool, d: bool, text: str = "") -> None:
    """One pair the analysis decided on: its page, where both lines are (PDF space), how
    it looks, what the rule said and what the tool did — and the start of the lower line, so that the
    editor can show a person which places it means."""
    log.append([int(pno), *[float(x) for x in abox], *[float(x) for x in bbox], list(vec), 1 if h else 0,
                1 if d else 0, " ".join(str(text or "").split())[:40]])


def _pairs_path(where: str) -> str:
    return where if where.endswith(".npz") else os.path.join(where, "zone_pairs.npz")


def save_pairs(where: str, log: list, only_pages: set | None = None) -> int:
    """Write a job's pairs to its folder (or to a .npz file). `only_pages`: a re-analysis of some
    pages — the other pages keep theirs."""
    np = _np()
    if np is None:
        return 0
    old = load_pairs(where) if only_pages is not None else None
    rows = list(log)
    if old is not None and len(old["page"]):
        keep = ~np.isin(old["page"], list(only_pages or ()))
        for i in np.nonzero(keep)[0]:
            rows.append([int(old["page"][i]), *[float(x) for x in old["box"][i]], [float(x) for x in old["vec"][i]],
                         int(old["h"][i]), int(old["d"][i]), str(old["txt"][i])])
    if not rows:
        return 0
    page = np.asarray([r[0] for r in rows], dtype=np.int32)
    box = np.asarray([r[1:9] for r in rows], dtype=np.float32)
    vec = np.asarray([r[9] for r in rows], dtype=np.float16)
    h = np.asarray([r[10] for r in rows], dtype=np.uint8)
    d = np.asarray([r[11] for r in rows], dtype=np.uint8)
    txt = np.asarray([r[12] if len(r) > 12 else "" for r in rows], dtype="U40")
    dst = _pairs_path(where)
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    tmp = dst[:-4] + ".tmp.npz"
    np.savez_compressed(tmp, page=page, box=box, vec=vec, h=h, d=d, txt=txt, names=np.asarray(json.dumps(list(NAMES))))
    replace_file(tmp, dst)
    return len(rows)


def load_pairs(where: str) -> dict | None:
    np = _np()
    p = _pairs_path(where)
    if np is None or not os.path.exists(p):
        return None
    try:
        z = np.load(p, allow_pickle=False)
        if json.loads(str(z["names"])) != list(NAMES):
            return None
        n = len(z["page"])
        return {"page": z["page"], "box": z["box"].astype(np.float64), "vec": z["vec"].astype(np.float64),
                "h": z["h"].astype(np.int8), "d": z["d"].astype(np.int8),
                "txt": z["txt"] if "txt" in z.files else np.asarray([""] * n, dtype="U40")}
    except Exception:
        return None


# ================================================================ what people did, read from the tags
def _text_nodes(model) -> dict[int, list]:
    """Per page: [(rect, node, area, priority)] of the tags that hold running text."""
    out: dict[int, list] = collections.defaultdict(list)
    for n in model.nodes.values():
        if n.role not in ELIGIBLE or n.artifact:
            continue
        if any(model.nodes[c].role in ELIGIBLE for c in n.children if c in model.nodes):
            continue                                       # a holder, not the text itself
        ok = True
        for a in model.ancestors(n.id):
            if a.role in NOT_TEXT_CONTEXT:
                ok = False
                break
        for pg in {n.page} | {int(p[0]) for p in n.parts}:
            for r in n.rects_on(pg):
                out[pg].append((r, n, max(1.0, (r[2] - r[0]) * (r[3] - r[1])), int(n.priority or 0), ok))
    return out


def _owner(nodes: list, x: float, y: float, pad: float = 1.5):
    best = None
    for r, n, area, pri, ok in nodes:
        if r[0] - pad <= x <= r[2] + pad and r[1] - pad <= y <= r[3] + pad:
            key = (-pri, area)
            if best is None or key < best[0]:
                best = (key, n, ok)
    return None if best is None else (best[1], best[2])


def joined_now(model, pairs: dict) -> "list[int]":
    """For every pair: 1 = a new tag starts between the lines, 0 = one tag holds both,
    -1 = cannot tell (a line is in no text tag, or in a list, a table …).

    V4.30: all the pairs of a page against all its tags in one step (numpy) — a 300-page book took ~300 ms of one
    Python thread for this, which the editor asked for after every zone cut and the learner for every edit.
    The answer is the one `_joined_now_py` gives."""
    np = _np()
    if np is None:
        return _joined_now_py(model, pairs)
    by_page = _text_nodes(model)
    page = np.asarray(pairs["page"]).astype(int)
    box = np.asarray(pairs["box"], dtype=np.float64)
    out = np.full(len(page), -1, dtype=np.int64)
    pad = 1.5
    for pg in np.unique(page):
        nodes = by_page.get(int(pg))
        if not nodes:
            continue
        idx = np.nonzero(page == pg)[0]
        R = np.asarray([e[0] for e in nodes], dtype=np.float64)                       # k x 4
        area = np.asarray([e[2] for e in nodes], dtype=np.float64)
        pri = np.asarray([e[3] for e in nodes], dtype=np.float64)
        ok = np.asarray([e[4] for e in nodes], dtype=bool)
        owner = {id(e[1]): j for j, e in enumerate({id(e[1]): e for e in nodes}.values())}
        who = np.asarray([owner[id(e[1])] for e in nodes], dtype=np.int64)
        order = np.lexsort((np.arange(len(nodes)), area, -pri))                       # best first, ties: as listed
        R, ok, who = R[order], ok[order], who[order]
        b = box[idx]
        ends = []
        for (xa, ya) in ((0, 1), (2, 3)):
            x = (b[:, 0 if xa == 0 else 4] + b[:, 2 if xa == 0 else 6]) / 2
            y = (b[:, 1 if ya == 1 else 5] + b[:, 3 if ya == 1 else 7]) / 2
            inside = ((x[:, None] >= R[None, :, 0] - pad) & (x[:, None] <= R[None, :, 2] + pad)
                      & (y[:, None] >= R[None, :, 1] - pad) & (y[:, None] <= R[None, :, 3] + pad))
            has = inside.any(axis=1)
            first = inside.argmax(axis=1)
            ends.append((has, ok[first], who[first]))
        (ha, oka, wa), (hb, okb, wb) = ends
        good = ha & hb & oka & okb
        out[idx] = np.where(good, np.where(wa == wb, 0, 1), -1)
    return [int(v) for v in out]


def _joined_now_py(model, pairs: dict) -> "list[int]":
    """The plain version (kept: it is what the fast one is checked against)."""
    by_page = _text_nodes(model)
    out = []
    box = pairs["box"]
    for i, pg in enumerate(pairs["page"]):
        nodes = by_page.get(int(pg))
        if not nodes:
            out.append(-1)
            continue
        ax, ay = (box[i][0] + box[i][2]) / 2, (box[i][1] + box[i][3]) / 2
        bx, by = (box[i][4] + box[i][6]) / 2, (box[i][5] + box[i][7]) / 2
        oa, ob = _owner(nodes, ax, ay), _owner(nodes, bx, by)
        if oa is None or ob is None or not oa[1] or not ob[1]:
            out.append(-1)
        else:
            out.append(0 if oa[0] is ob[0] else 1)
    return out


# ================================================================ the same fix, elsewhere in this file
# what makes two places "the same": the space between the lines, the indent, a word split at the end
# of the line, a lower-case start — not how long the line above happens to be (every paragraph ends
# at another place)
_KEY = ("gap", "indent", "dsize", "a_hyph", "b_lower")
_TOL = {"gap": 0.15, "indent": 0.5, "dsize": 0.2}                        # the others must be the same


def similar(job_dir: str, model, auto=None, limit: int = 400) -> dict:
    """The other places of this file that look like one a person has just corrected.

    A person cut a zone between two lines (the gap, the indent and the line above looked like this) —
    where else in the file is there a pair of lines that look the same and are still one zone? And the
    other way round for a zone a person joined. -> {"cut": [pair numbers], "join": [pair numbers],
    "corrections": n}. Nothing is changed: `apply` does that, when the person says so."""
    np = _np()
    pairs = load_pairs(job_dir)
    out = {"cut": [], "join": [], "corrections": 0, "pairs": 0}
    if pairs is None or not len(pairs["page"]) or np is None:
        return out
    ys = np.asarray(joined_now(model, pairs))
    d, changed = _corrections(pairs, ys.tolist(), auto, False)
    d = np.asarray(d)
    out["pairs"], out["corrections"] = int(len(ys)), len(changed)
    if not changed:
        return out
    ix = {n: i for i, n in enumerate(NAMES)}
    cols = [ix[k] for k in _KEY]
    tol = np.asarray([_TOL.get(k, 0.5) for k in _KEY])
    V = pairs["vec"][:, cols]
    h = pairs["h"].astype(int)
    for kind, y_from, y_to in (("cut", 0, 1), ("join", 1, 0)):
        refs = [i for i in changed if int(d[i]) == y_from and int(ys[i]) == y_to][-60:]
        if not refs:
            continue
        ok = (ys == y_from) & (d == y_from)              # still as the tool decided, never touched
        cand = np.zeros(len(ys), dtype=bool)
        for r in refs:
            near = (np.abs(V - V[r]) / tol).max(axis=1) <= 1.0
            cand |= near & (h == h[r])
        cand &= ok
        out[kind] = [int(i) for i in np.nonzero(cand)[0][:limit]]
    return out


def apply(model, job_dir: str, auto, working_pdf: str, action: str, indexes: list[int]) -> dict:
    """Cut (or join) the zones at these pairs of lines — the answer to "do the same here?". Only
    what `similar` finds is done; a zone that cannot be cut (links inside it) is left as it is."""
    from . import edit
    pairs = load_pairs(job_dir)
    if pairs is None:
        return {"changed": 0, "skipped": len(indexes), "why": "no pairs for this file"}
    now = similar(job_dir, model, auto)
    allowed = set(now.get(action) or [])
    want = [int(i) for i in indexes if int(i) in allowed]
    box = pairs["box"]
    by_page = _text_nodes(model)
    changed = skipped = 0
    if action == "cut":
        per: dict[str, list[float]] = collections.defaultdict(list)
        for i in want:
            nodes = by_page.get(int(pairs["page"][i])) or []
            o = _owner(nodes, (box[i][0] + box[i][2]) / 2, (box[i][1] + box[i][3]) / 2)
            if o is None or not o[1]:
                skipped += 1
                continue
            per[o[0].id].append((box[i][1] + box[i][7]) / 2)       # between the lower edge of a and the top of b
        for nid, cuts in per.items():
            try:
                ids = edit.split_at(model, working_pdf, nid, cuts)
                changed += len(ids) - 1
            except edit.EditError:
                skipped += len(cuts)
    elif action == "join":
        merged_into: dict[str, str] = {}
        for i in want:
            nodes = by_page.get(int(pairs["page"][i])) or []
            oa = _owner(nodes, (box[i][0] + box[i][2]) / 2, (box[i][1] + box[i][3]) / 2)
            ob = _owner(nodes, (box[i][4] + box[i][6]) / 2, (box[i][5] + box[i][7]) / 2)
            if oa is None or ob is None or not oa[1] or not ob[1]:
                skipped += 1
                continue
            a_id, b_id = oa[0].id, ob[0].id
            while a_id in merged_into:
                a_id = merged_into[a_id]
            while b_id in merged_into:
                b_id = merged_into[b_id]
            na, nb = model.nodes.get(a_id), model.nodes.get(b_id)
            if na is None or nb is None or a_id == b_id:
                continue
            sib = model.siblings(a_id)
            if (na.role != nb.role or na.page != nb.page or na.parent != nb.parent or na.children or nb.children
                    or na.parts or nb.parts or a_id not in sib or sib.index(a_id) + 1 >= len(sib)
                    or sib[sib.index(a_id) + 1] != b_id):
                skipped += 1
                continue
            edit.merge(model, [a_id, b_id])
            na.note = (na.note + "; " if na.note else "") + "joined, as the zones before it were joined (learned)"
            merged_into[b_id] = a_id
            changed += 1
    return {"changed": changed, "skipped": skipped, "asked": len(indexes), "action": action}


# ================================================================ the database
SCHEMA = """
CREATE TABLE IF NOT EXISTS zone_rows (
  ref TEXT NOT NULL, client TEXT, kind TEXT, page INTEGER, y INTEGER, h INTEGER, d INTEGER, w REAL,
  vec BLOB, who TEXT, created TEXT);
CREATE INDEX IF NOT EXISTS zone_rows_ref ON zone_rows(ref);
CREATE INDEX IF NOT EXISTS zone_rows_client ON zone_rows(client);
CREATE TABLE IF NOT EXISTS zone_models (
  scope TEXT PRIMARY KEY, created TEXT, n INTEGER, explicit INTEGER, refs INTEGER, status TEXT, note TEXT,
  acc_rule REAL, acc_model REAL, fixed INTEGER, broken INTEGER, model TEXT, signature TEXT, seconds REAL);
"""


def ensure(c: sqlite3.Connection) -> None:
    c.executescript(SCHEMA)


def _pack(vec) -> bytes:
    np = _np()
    return np.asarray(vec, dtype=np.float16).tobytes()


def _unpack_rows(blobs) -> "object":
    np = _np()
    return np.stack([np.frombuffer(b, dtype=np.float16).astype(np.float64) for b in blobs]) if blobs else \
        np.zeros((0, NF))


def forget(ref: str) -> None:
    with L.connect() as c:
        c.execute("DELETE FROM zone_rows WHERE ref=?", (ref,))


def _store(ref: str, client: str, kind: str, pairs: dict, ys: list[int], weights, who: str) -> dict:
    np = _np()
    rows = []
    n_expl = 0
    for i, y in enumerate(ys):
        w = float(weights[i])
        if y < 0 or w <= 0:
            continue
        if int(pairs["d"][i]) != y:
            n_expl += 1
        rows.append((ref, client, kind, int(pairs["page"][i]), int(y), int(pairs["h"][i]), int(pairs["d"][i]), w,
                     _pack(pairs["vec"][i]), who, _now()))
    if len(rows) > MAX_ROWS:                               # keep every correction, thin out the rest
        expl = [r for r in rows if r[4] != r[6]]
        rest = [r for r in rows if r[4] == r[6]]
        step = max(1, len(rest) // max(1, MAX_ROWS - len(expl)))
        rows = expl + rest[::step]
    with L.connect() as c:
        c.execute("DELETE FROM zone_rows WHERE ref=?", (ref,))
        c.executemany("INSERT INTO zone_rows (ref, client, kind, page, y, h, d, w, vec, who, created) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    return {"rows": len(rows), "corrections": n_expl}


def _corrections(pairs: dict, ys: list[int], auto, approved_all: bool) -> tuple[list[int], list[int]]:
    """(what the tool did, per pair; which pairs a person changed). What the tool did is read from its
    own proposal (the saved auto model) — a later step of the pipeline may have joined paragraphs it cut
    — and, with no proposal, is what the analysis logged."""
    d = [int(x) for x in pairs["d"]]
    if auto is not None:
        np = _np()
        # V4.30: the proposal does not change while the file is edited: its answers are kept with it
        sig = (len(d), float(np.sum(pairs["box"])), int(np.sum(pairs["page"]))) if np is not None else None
        kept = getattr(auto, "_zone_da", None)
        if sig is not None and kept is not None and kept[0] == sig:
            da = kept[1]
        else:
            da = joined_now(auto, pairs)
            if sig is not None:
                try:
                    auto._zone_da = (sig, da)
                except Exception:
                    pass
        d = [da[i] if da[i] >= 0 else d[i] for i in range(len(d))]
    return d, [i for i, y in enumerate(ys) if y >= 0 and y != d[i]]


def record_job(job_dir: str, client: str, model, auto=None, by: str = "", reviewer: bool = False,
               approved: bool = False, ref: str | None = None, kind: str | None = None) -> dict:
    """What the people working on this job did to its zones, as examples (live, at the build, at the approval).

    The pairs the tool decided on are compared with the tags as they are now. A pair whose
    answer a person changed is a correction. A pair on a page where something was corrected
    is an example too (the person looked at the page) — all of them in an approved file."""
    pairs = load_pairs(job_dir)
    if pairs is None or not len(pairs["page"]):
        return {"rows": 0, "corrections": 0, "why": "no pairs for this job"}
    ys = joined_now(model, pairs)
    d, changed = _corrections(pairs, ys, auto, approved)
    pairs = dict(pairs, d=d)
    wrole = REVIEWER if reviewer else 1.0
    corrected = {int(pairs["page"][i]) for i in changed}
    weights = []
    for i, y in enumerate(ys):
        if y < 0:
            weights.append(0.0)
        elif y != d[i]:
            weights.append(EXPLICIT_WEIGHT * wrole)
        elif approved:
            weights.append(FILE_WEIGHT * wrole)
        elif int(pairs["page"][i]) in corrected:
            weights.append(TOUCHED_WEIGHT * wrole)
        else:
            weights.append(0.0)
    job = os.path.basename(os.path.normpath(job_dir))
    out = _store(ref or f"fb:{job}", client or "", kind or ("approved" if approved else "feedback"), pairs, ys,
                 weights, by)
    out["pages"] = len(corrected)
    return out


def record_sample(sid: str, client: str, model, pairs_file: str, auto=None) -> dict:
    """A finished file (an approved job, or a tagged PDF an admin uploaded): every pair, in full."""
    pairs = load_pairs(pairs_file)
    if pairs is None or not len(pairs["page"]):
        return {"rows": 0, "corrections": 0, "why": "no pairs for this sample"}
    ys = joined_now(model, pairs)
    d, _ = _corrections(pairs, ys, auto, True)
    pairs = dict(pairs, d=d)
    weights = [0.0 if y < 0 else (EXPLICIT_WEIGHT if y != d[i] else FILE_WEIGHT) for i, y in enumerate(ys)]
    return _store(sid, client or "", "sample", pairs, ys, weights, "")


# ================================================================ training
def _load_rows(c: sqlite3.Connection):
    np = _np()
    rs = c.execute("SELECT z.ref, z.client, z.y, z.h, z.d, z.w, z.vec, z.page FROM zone_rows z "
                   "LEFT JOIN samples s ON s.id = z.ref WHERE s.id IS NULL OR (s.enabled=1 AND s.status='ready')"
                   ).fetchall()
    if not rs:
        return None
    X = _unpack_rows([r["vec"] for r in rs])
    return {"X": X, "ref": [r["ref"] for r in rs], "client": [r["client"] or "" for r in rs],
            "y": np.asarray([r["y"] for r in rs], dtype=np.float64), "h": np.asarray([r["h"] for r in rs], dtype=np.float64),
            "d": np.asarray([r["d"] for r in rs], dtype=np.float64), "w": np.asarray([r["w"] for r in rs], dtype=np.float64),
            "page": [r["page"] for r in rs]}


def _signature(data, scope: str) -> str:
    sel = [i for i, cl in enumerate(data["client"]) if scope == "" or cl == scope]
    return f"{len(sel)}:{round(float(data['w'][sel].sum()), 2) if sel else 0}:{int((data['y'][sel] != data['d'][sel]).sum()) if sel else 0}"


def train_scope(data: dict, scope: str) -> dict:
    """One model: `scope` '' (every client's pairs) or a client (its own pairs, the others' lighter)."""
    np = _np()
    t0 = time.time()
    own = np.asarray([scope == "" or cl == scope for cl in data["client"]])
    w = data["w"].copy()
    if scope:
        w = np.where(own, w, w * OTHER_CLIENT)
    keep = w > 0
    X, h, y, w = data["X"][keep], data["h"][keep], data["y"][keep], w[keep]
    own = own[keep]
    refs = [r for r, k in zip(data["ref"], keep) if k]
    pages = [p for p, k in zip(data["page"], keep) if k]
    clients = [c_ for c_, k in zip(data["client"], keep) if k]
    corrected = (y != data["d"][keep]) & own
    explicit = int(corrected.sum())
    nrefs = len({r for r, o in zip(refs, own) if o})
    out = {"scope": scope, "n": int(own.sum()), "explicit": explicit, "refs": nrefs, "status": "off", "note": "",
           "fixed": 0, "broken": 0, "acc_rule": None, "acc_model": None, "model": None}
    if int(own.sum()) < MIN_ROWS or explicit < MIN_EXPLICIT:
        out["note"] = f"{int(own.sum())} pair(s), {explicit} correction(s) — needs {MIN_ROWS} and {MIN_EXPLICIT}"
        out["status"] = "waiting"
        return out
    # tried on what it was not trained on. Everybody's model is used for clients that have no model
    # of their own, so it must hold for a client it was not trained on: it waits for the corrections of
    # two clients, and is tried client by client. A client's model: other files; with fewer than
    # three, other pages.
    if scope == "":
        who = {cl for cl, c_ in zip(clients, corrected) if c_ and cl}
        if len(who) < 2:
            out["note"] = (f"corrections from {len(who)} client(s) so far — everybody's model waits for two, "
                           "so that what it learned is not one client's habit")
            out["status"] = "waiting"
            return out
        groups = [cl or "(no client)" for cl in clients]
    else:
        groups = refs if nrefs >= 3 else [f"{r}#{p}" for r, p in zip(refs, pages)]
    v = validate(X, h, y, w, groups, own=own if scope else None)
    ok, why = earns_place(v, explicit, nrefs)
    model = fit(X, h, y, w)
    model.scope = scope
    model.meta = {"n": out["n"], "explicit": explicit, "refs": nrefs, "fixed": v["fixed"], "broken": v["broken"],
                  "acc_rule": v["acc_rule"], "acc_model": v["acc_model"], "trained": _now(),
                  "convention": _convention(X[own], y[own])}
    out.update(status="active" if ok else "candidate", note=why, fixed=v["fixed"], broken=v["broken"],
               acc_rule=v["acc_rule"], acc_model=v["acc_model"], model=model, seconds=round(time.time() - t0, 2))
    return out


def _convention(X, y) -> dict | None:
    """What the examples say in plain figures: how far apart (in em) two lines of one paragraph are in
    these files, and from where a new paragraph starts."""
    np = _np()
    try:
        gap = X[:, NAMES.index("gap")]
        cut, same = gap[y >= 0.5], gap[y < 0.5]
        if len(cut) < 5 or len(same) < 5:
            return None
        return {"cut_from": round(float(np.quantile(cut, 0.1)), 2), "cut_typical": round(float(np.median(cut)), 2),
                "same_to": round(float(np.quantile(same, 0.9)), 2), "same_typical": round(float(np.median(same)), 2),
                "n_cut": int(len(cut)), "n_same": int(len(same))}
    except Exception:
        return None


def convention_text(meta: dict | None) -> str:
    """The convention of a model in a sentence a person can check against the file."""
    c = (meta or {}).get("convention")
    if not c:
        return ""
    if c["same_to"] < c["cut_from"]:
        usual = f" (usually {c['cut_typical']:.2f})" if abs(c["cut_typical"] - c["cut_from"]) >= 0.02 else ""
        return (f"Lines up to {c['same_to']:.2f} em apart belong to one paragraph; a new paragraph starts from "
                f"{c['cut_from']:.2f} em{usual}. The built-in rule starts one at 0.62 em.")
    return (f"The space between the lines does not tell the paragraphs apart here (one paragraph: usually "
            f"{c['same_typical']:.2f} em, a new one: usually {c['cut_typical']:.2f} em, with an overlap) — "
            "the model reads the end of the line above, the indent and the start of the next line as well.")


def train_all(force: bool = False) -> dict:
    """Train the model of everybody and of every client with enough pairs; keep what earns its place."""
    np = _np()
    if np is None or not L.has_data():
        return {"ok": False, "error": "no sample database or numpy"}
    t0 = time.time()
    c = L.connect()
    out = {"ok": True, "models": [], "skipped": 0}
    try:
        data = _load_rows(c)
        if data is None:
            return {"ok": True, "models": [], "skipped": 0, "why": "no zone examples yet"}
        counts = collections.Counter(cl for cl in data["client"] if cl)
        scopes = [""] + [cl for cl, n in counts.items() if n >= MIN_ROWS]
        old = {r["scope"]: r["signature"] for r in c.execute("SELECT scope, signature FROM zone_models")}
        for scope in scopes:
            sig = _signature(data, scope)
            if not force and old.get(scope) == sig:
                out["skipped"] += 1
                continue
            r = train_scope(data, scope)
            c.execute("INSERT OR REPLACE INTO zone_models (scope, created, n, explicit, refs, status, note, acc_rule, "
                      "acc_model, fixed, broken, model, signature, seconds) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (scope, _now(), r["n"], r["explicit"], r["refs"], r["status"], r["note"], r["acc_rule"],
                       r["acc_model"], r["fixed"], r["broken"],
                       json.dumps(r["model"].to_dict()) if r["model"] is not None else "", sig, r.get("seconds")))
            out["models"].append({k: r[k] for k in ("scope", "n", "explicit", "refs", "status", "note", "fixed",
                                                     "broken", "acc_rule", "acc_model")})
        gone = [r["scope"] for r in c.execute("SELECT scope FROM zone_models WHERE scope != ''")
                if r["scope"] not in scopes]           # a client whose pairs went away
        c.executemany("DELETE FROM zone_models WHERE scope=?", [(g,) for g in gone])
        c.commit()
        _CACHE["t"] = 0.0
    finally:
        c.close()
    out["seconds"] = round(time.time() - t0, 2)
    return out


# ================================================================ using it in an analysis
_CACHE: dict = {"t": 0.0, "models": {}, "path": ""}


def active_models(refresh: bool = False) -> dict:
    """{scope: ZoneModel} of the models that earned their place."""
    if not L.has_data():
        return {}
    p = L.db_path()
    if not refresh and _CACHE["path"] == p and time.time() - _CACHE["t"] < 20:
        return _CACHE["models"]
    out = {}
    try:
        with L.connect() as c:
            for r in c.execute("SELECT scope, model FROM zone_models WHERE status='active' AND model != ''"):
                try:
                    m = ZoneModel.from_dict(json.loads(r["model"]))
                except Exception:
                    m = None
                if m is not None:
                    out[r["scope"]] = m
    except sqlite3.DatabaseError:
        out = {}
    _CACHE.update(t=time.time(), models=out, path=p)
    return out


def wanted(opts) -> bool:
    """Zones are learned for a profile that asks for it — never for a house style's engine."""
    if not getattr(opts, "learn_zones", True):
        return False
    if (getattr(opts, "house_style", "") or "").strip().lower() not in ("", "none", "off"):
        return False
    return True


def pick(opts, pdf_path: str = "") -> "ZoneModel | None":
    """The model for this analysis: the client's own, or — with every client's samples the files
    most like this one — the model of the client whose files this one resembles, or everybody's."""
    if not wanted(opts):
        return None
    models = active_models()
    if not models:
        return None
    client = getattr(opts, "client", "") or ""
    if getattr(opts, "samples_scope", "client") == "all" and pdf_path:
        # every client's samples (Smart AI): the files most like this one — the model of the client
        # whose files this one resembles (the same fonts, sizes and words), when it resembles them closely
        try:
            from . import brain
            _, info = brain.doc_weights(pdf_path)
            for s_ in info.get("similar") or []:
                m = models.get(s_.get("client"))
                if m is not None and float(s_.get("sim") or 0) >= SIMILAR and int(m.meta.get("refs") or 0) >= 2:
                    return m
        except Exception:
            pass
        return models.get("")
    return models.get(client) or models.get("")


# ================================================================ a finished PDF → its pairs
def pairs_for_pdf(pdf_path: str, out_file: str, opts=None) -> int:
    """The pairs of a finished (tagged) PDF an admin uploaded: the analysis is run over its
    pages with the plain rule, and every pair it decides on is written down — the tags
    of the file then say what each answer should have been."""
    from .analyzer import Analyzer
    from .options import Options
    o = opts or Options()
    o.learn_zones = False                                  # the plain rule, nothing learned
    a = Analyzer(pdf_path, o)
    a.zone_log = []
    try:
        a.run()
        rows = list(a.zone_log)
    finally:
        try:
            a.doc.close()
            a.pdf.close()
        except Exception:
            pass
    return save_pairs(out_file, rows)


# ================================================================ for the admin
def status() -> dict:
    out = {"models": [], "rows": 0, "corrections": 0, "refs": 0, "by_kind": {}, "recent": [], "numpy": _np() is not None}
    if not L.has_data():
        return out
    with L.connect() as c:
        r = c.execute("SELECT COUNT(*) n, COALESCE(SUM(y != d), 0) e, COUNT(DISTINCT ref) f FROM zone_rows").fetchone()
        out.update(rows=r["n"], corrections=int(r["e"] or 0), refs=r["f"])
        out["by_kind"] = {x["kind"]: x["n"] for x in c.execute("SELECT kind, COUNT(*) n FROM zone_rows GROUP BY kind")}
        for m in c.execute("SELECT scope, created, n, explicit, refs, status, note, acc_rule, acc_model, fixed, broken, "
                           "model FROM zone_models ORDER BY scope"):
            d = dict(m)
            try:
                zm = ZoneModel.from_dict(json.loads(d.pop("model") or "{}"))
            except Exception:
                zm = None
            d["why"] = zm.explain() if zm is not None else []
            d["convention"] = convention_text(zm.meta) if zm is not None else ""
            out["models"].append(d)
        out["people"] = {x["who"] or "—": x["n"] for x in c.execute(
            "SELECT who, COUNT(*) n FROM zone_rows WHERE y != d GROUP BY who ORDER BY n DESC LIMIT 12")}
    return out


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    if cmd == "train":
        print(json.dumps(train_all(force="--force" in argv), indent=1, default=str))
    else:
        print(json.dumps(status(), indent=1, default=str))
    return 0


if __name__ == "__main__":
    import sys
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, ROOT)
    sys.exit(main(sys.argv[1:]))
