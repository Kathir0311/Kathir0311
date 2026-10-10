"""The self-learning AI brain (V4.25).

The sample files (core/learn.py) taught the tool by example: a tag the
analysis doubted took the role of the most similar sample tag. The brain
turns that database into something that learns and decides on its own:

* **A stronger database.** Every sample (a client's finished file, an
  approved file) is also stored as
  - a *document fingerprint* (its words and type styles) and a *document
    type* (journal article, book, workbook, Teacher's Guide …) — by rule, and
    by the local AI when it is on;
  - a *style map*: which font, size and weight the client sets each tag in
    (“Rockwell bold 1.4× → H3”);
  - a *memory* of its short texts as vectors — hashed n-grams always, the
    Ollama embedding model's vectors too when one is installed — so the
    nearest finished examples can be looked up for any new text (RAG);
  - *insights*: the local AI studies the sample and writes the client's
    tagging rules in plain words, which go into every later question.
* **Its own trained model.** A role classifier (softmax regression over the
  layout features, the words, the font, the client and the document type)
  is trained on every sample tag and every fix people made — again after
  each new sample, with its accuracy measured on samples it did not see.
* **Its own decisions.** For every text tag of a new file the brain hears
  several opinions — the analysis' rules, the nearest sample tags, the
  trained model, the style map, the wording memory — weighs each by how
  often that source was right before, and decides. What stays doubtful is
  put to the local AI (Ollama) with the PDF/UA rules, the client's insights
  and the most similar finished examples. Guard rails keep it to what
  PDF/UA allows (a heading is never a paragraph of sentences, a caption is
  always next to its figure or table, hand edits are never touched).
* **It learns from the outcome.** Every built file records what people
  changed (feedback); every approved file is compared with what the brain
  decided, so each source's trust goes up or down, and the share of tags
  the tool got right first time — the *first-pass quality* — is kept per
  file, the learning curve the admin sees.

Every change carries a note “brain: …” in the editor and can be undone.
With no samples, no trained model and the AI switched off, nothing changes.

V4.27 — the same brain, more honest and more consistent:

* **Honest probabilities.** The model's accuracy is measured by cross-validation
  over *files* (every file held out once; one held-out fifth when the data is
  big), and the held-out predictions fix a temperature that makes "78 % sure" mean
  right 78 % of the time (saved inside the model; an older model loads with 1.0).
* **One file, one convention.** The verdicts the learned sources are sure of are
  pooled over the tags of the same look in the file (`layout_voices`), so a tag
  with moderate evidence follows the sure ones — as loud as the look is consistent
  and silent when it is not. A heading takes the level the file gives its look
  (`_unify_levels`); the doubtful tags are listed by what their answer teaches
  (`_review_first`).
* **Recent and forgetful.** Newer examples count a little more (`recency`); a
  source is judged by about its last 600 decisions (`forget_trust`).
"""
from __future__ import annotations

import collections
import io
import json
import math
import os
import re
import sqlite3
import sys
import time
import zlib
from datetime import datetime

from . import learn as L

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
  sample_id TEXT PRIMARY KEY, client TEXT, doc_type TEXT, rule_type TEXT, ai_type TEXT,
  pages INTEGER, body REAL, fingerprint BLOB, styles TEXT, first_text TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS memory (
  sample_id TEXT, client TEXT, role TEXT, text TEXT, size_rel REAL, bold INTEGER, font TEXT,
  vec BLOB, emb BLOB, emb_model TEXT);
CREATE INDEX IF NOT EXISTS memory_sample ON memory(sample_id);
CREATE TABLE IF NOT EXISTS insights (
  sample_id TEXT PRIMARY KEY, client TEXT, doc_type TEXT, summary TEXT, rules TEXT, model TEXT,
  created TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS brain_models (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, n_train INTEGER, n_samples INTEGER,
  classes TEXT, accuracy REAL, macro_f1 REAL, baseline REAL, per_role TEXT, weights BLOB,
  signature TEXT, trigger TEXT, seconds REAL);
CREATE TABLE IF NOT EXISTS feedback (
  job TEXT, client TEXT, from_role TEXT, role TEXT, text TEXT, norm TEXT, font TEXT,
  size_rel REAL, bold INTEGER, italic INTEGER, lines INTEGER, words INTEGER,
  x REAL, y REAL, w REAL, centered INTEGER, starts_num INTEGER, caps REAL, ends_stop INTEGER,
  first_page INTEGER, cap_word INTEGER, created TEXT);
CREATE INDEX IF NOT EXISTS feedback_job ON feedback(job);
CREATE TABLE IF NOT EXISTS decisions (
  job TEXT, node TEXT, client TEXT, text TEXT, proposed TEXT, final TEXT, opinions TEXT,
  chosen TEXT, share REAL, learned INTEGER DEFAULT 0, created TEXT);
CREATE INDEX IF NOT EXISTS decisions_job ON decisions(job);
CREATE TABLE IF NOT EXISTS trust (source TEXT, role TEXT, ok INTEGER DEFAULT 0, n INTEGER DEFAULT 0,
  PRIMARY KEY (source, role));
CREATE TABLE IF NOT EXISTS history (
  job TEXT, client TEXT, kind TEXT, created TEXT, tags INTEGER, same INTEGER, quality REAL,
  brain_quality REAL, model_id INTEGER, doc_type TEXT);
"""

DOC_TYPES = ["journal_article", "book", "teachers_guide", "workbook", "assessment", "report",
             "manual", "presentation", "newsletter", "form", "document"]
HEADINGS = L.HEADINGS
SWAP = L.SWAP                                  # H1–H6, P, Caption, BlockQuote
LEARNABLE = (L.TEXT_ROLES - {"H"})             # what the trained model may say
SOURCES = ("rules", "samples", "model", "style", "wording", "code", "layout", "ai")
BASE_WEIGHT = {"rules": 1.0, "samples": 1.0, "model": 1.2, "style": 1.1, "wording": 1.4, "code": 1.5, "layout": 1.0,
               "ai": 1.3}
STRENGTH = {   # share the winner needs, independent sources that must agree
    "careful": (0.72, 2), "balanced": (0.60, 2), "bold": (0.52, 1)}
VEC_DIM = 256
FP_DIM = 512
# V4.27
CV_MAX_ROWS = 12000       # up to this many examples the accuracy is measured by cross-validation over files
CV_FOLDS = 4
TRUST_WINDOW = 600        # a source is judged by about its last 600 decisions, not by its whole history
LAYOUT_MIN = 3            # other tags of the same look in the file before they speak
LAYOUT_PURE = (0.65, 0.95)  # how much of one role the sure others show: silent below the first, in full above the second
LAYOUT_FIRM = 0.8         # at least this much of one role (and 4 others) and the voice counts as an agreeing source
RECENT_FLOOR = 0.6        # the weight of a very old example (1.0 = today)
RECENT_HALF_LIFE = 365.0  # days
STOP = set("the and for that with this from are was were have has had not but you your our their they "
           "its into than then them will can all any each per also may more most such which who what "
           "when where how other some one two three use used using".split())


def ensure(c: sqlite3.Connection) -> None:
    c.executescript(SCHEMA)
    for mod in ("codegen", "memory", "kb", "zonelearn"):   # V4.26: learned code, memories, the rule book · V4.28: zones
        try:
            __import__(f"{__package__}.{mod}", fromlist=["ensure"]).ensure(c)
        except Exception:
            pass
    fb_cols = {r[1] for r in c.execute("PRAGMA table_info(feedback)")}
    for col in ("node TEXT DEFAULT ''", "who TEXT DEFAULT ''"):        # V4.26: node · V4.28: who made the fix
        if col.split()[0] not in fb_cols:
            try:
                c.execute(f"ALTER TABLE feedback ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
    have = {r[1] for r in c.execute("PRAGMA table_info(brain_models)")}
    for col in ("logloss REAL", "ece REAL", "temperature REAL", "folds INTEGER"):     # V4.27: honest probabilities
        if col.split()[0] not in have:
            try:
                c.execute(f"ALTER TABLE brain_models ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass


def _np():
    try:
        import numpy as np
        return np
    except Exception:
        return None


def _h(tok: str, dim: int) -> int:
    return zlib.crc32(tok.encode("utf-8", "replace")) % dim


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ================================================================ vectors
def text_vec(text: str, dim: int = VEC_DIM) -> list[float]:
    """Hashed character trigrams and words: texts that read alike are near."""
    t = re.sub(r"\d", "#", " ".join((text or "").lower().split()))[:200]
    v = [0.0] * dim
    if not t:
        return v
    s = f" {t} "
    for i in range(len(s) - 2):
        v[_h("c" + s[i:i + 3], dim)] += 1.0
    for w in re.findall(r"[a-z#]+", t)[:40]:
        v[_h("w" + w, dim)] += 2.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _pack(vec) -> bytes:
    np = _np()
    if np is not None:
        return np.asarray(vec, dtype=np.float16).tobytes()
    import array
    return array.array("f", vec).tobytes()


def _unpack(blob: bytes, dim: int | None = None):
    np = _np()
    if not blob:
        return None
    if np is not None:
        a = np.frombuffer(blob, dtype=np.float16).astype(np.float32)
        return a
    import array
    a = array.array("f")
    a.frombytes(blob)
    return list(a)


# ================================================================ documents
def guess_doc_type(text: str, pages: int, landscape: float = 0.0, blanks: int = 0, fields: int = 0) -> str:
    t = (text or "").lower()
    if re.search(r"teacher[’']?s guide", t) and re.search(r"\blesson\s+\d", t):
        return "teachers_guide"
    if "abstract" in t and (re.search(r"\bdoi\b|doi\.org|keywords", t) or "references" in t):
        return "journal_article"
    if fields >= 2:
        return "form"
    if landscape >= 0.6 and pages >= 3:
        return "presentation"
    if blanks >= 6 or re.search(r"\bname\s*:?\s*_{3,}", t):
        return "workbook"
    if re.search(r"\b(assessment|scoring|rubric)\b", t) and re.search(r"\bstudents?\b", t):
        return "assessment"
    if re.search(r"executive summary|annual report|\breport\b", t) and pages >= 4:
        return "report"
    if re.search(r"\b(contents|chapter)\b", t) and pages >= 16:
        return "book"
    if re.search(r"\b(user guide|manual|installation|instructions)\b", t):
        return "manual"
    if re.search(r"\bnewsletter\b", t):
        return "newsletter"
    return "document"


def fingerprint(words: collections.Counter, styles: collections.Counter, dim: int = FP_DIM) -> list[float]:
    v = [0.0] * dim
    for w, n in words.items():
        v[_h("w" + w, dim)] += 1.0 + math.log1p(n)
    tot = sum(styles.values()) or 1
    for s_, n in styles.most_common(30):
        v[_h("s" + s_, dim)] += 6.0 * n / tot
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def digest_pages(pages, max_pages: int = 6) -> dict:
    """What a document is: its type, fingerprint and main type styles."""
    words: collections.Counter = collections.Counter()
    styles: collections.Counter = collections.Counter()
    texts: list[str] = []
    blanks = land = 0
    n = min(pages.n, max_pages)
    body = pages.body or 10.0
    fields = 0
    for p in range(n):
        w, h = pages.size(p)
        land += 1 if w > h * 1.05 else 0
        try:
            fields += sum(1 for _ in pages.doc[p].widgets())
        except Exception:
            pass
        for l in pages.page_lines(p):
            texts.append(l.text)
            if "___" in l.text:
                blanks += 1
            fam = L.font_family(l.font)
            styles[f"{fam}|{round(l.size / body * 10) / 10}|{int(l.bold)}"] += len(l.text)
            for wd in re.findall(r"[a-z]{3,}", l.text.lower()):
                if wd not in STOP:
                    words[wd] += 1
    full = " ".join(texts)
    first = " ".join(l.text for l in pages.page_lines(0))[:1500] if pages.n else ""
    return {"vec": fingerprint(words, styles),
            "type": guess_doc_type(full[:30000], pages.n, land / max(1, n), blanks, fields),
            "styles": [[k, v] for k, v in styles.most_common(12)], "first": first, "pages": pages.n, "body": body}


def _cos(a, b) -> float:
    np = _np()
    if a is None or b is None:
        return 0.0
    if np is not None:
        a, b = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
        if a.shape != b.shape:
            return 0.0
        d = float(np.linalg.norm(a) * np.linalg.norm(b)) or 1.0
        return float(a @ b) / d
    if len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def doc_weights(pdf_path: str, pages=None) -> tuple[dict, dict]:
    """How much each sample counts for this file: 0.4 (unlike) … 2.0 (alike).

    Returns (weights by sample id, {doc_type, similar: [...]})."""
    own_pages = pages is None
    if pages is None:
        pages = L._Pages(pdf_path, max_pages=8)
    try:
        dg = digest_pages(pages)
    finally:
        if own_pages:
            pages.close()
    info = {"doc_type": dg["type"], "similar": [], "styles": dg["styles"][:6]}
    if not L.has_data():
        return {}, info
    weights: dict[str, float] = {}
    sims = []
    with L.connect() as c:
        for r in c.execute("SELECT d.sample_id, d.doc_type, d.fingerprint, s.filename, s.client FROM docs d "
                           "JOIN samples s ON s.id=d.sample_id WHERE s.enabled=1 AND s.status='ready'"):
            sim = _cos(dg["vec"], _unpack(r["fingerprint"]))
            sims.append((sim, r["sample_id"], r["filename"], r["client"], r["doc_type"]))
    sims.sort(reverse=True)
    # a file the rules cannot place takes the type of the samples it is most like
    if dg["type"] == "document" and sims and sims[0][0] >= 0.8 and sims[0][4] and sims[0][4] != "document":
        near = [s_ for s_ in sims[:5] if s_[0] >= 0.8]
        votes = collections.Counter(s_[4] for s_ in near if s_[4])
        if votes:
            dg["type"] = votes.most_common(1)[0][0]
            info["doc_type"] = dg["type"]
            info["doc_type_from"] = "similar samples"
    for sim, sid, _, _, dt in sims:
        w = 0.4 + 1.6 * max(0.0, min(1.0, (sim - 0.1) / 0.6))
        if dt and dt == dg["type"] and dg["type"] != "document":
            w *= 1.25
        weights[sid] = round(min(2.0, w), 3)
    info["similar"] = [{"id": sid, "file": fn, "client": cl or "common", "type": dt, "sim": round(sim, 3)}
                       for sim, sid, fn, cl, dt in sims[:5]]
    return weights, info


# ================================================================ features
def dense(f: dict) -> list[float]:
    sr = float(f.get("size_rel") or 1.0)
    return [min(sr, 4.0), float(f.get("bold") or 0), float(f.get("italic") or 0),
            min(int(f.get("lines") or 1), 8) / 8.0,
            math.log1p(min(int(f.get("words") or 0), 300)) / math.log1p(300),
            float(f.get("x") or 0), float(f.get("y") or 0), float(f.get("w") or 0),
            float(f.get("centered") or 0), float(f.get("starts_num") or 0), float(f.get("caps") or 0),
            float(f.get("ends_stop") or 0), float(f.get("first_page") or 0), float(f.get("cap_word") or 0),
            1.0 if sr >= 1.15 else 0.0, 1.0 if sr <= 0.9 else 0.0]


def _bucket(n: int) -> str:
    for lim, name in ((1, "1"), (2, "2"), (5, "3-5"), (10, "6-10"), (20, "11-20"), (40, "21-40")):
        if n <= lim:
            return name
    return "41+"


def _shape(tok: str) -> str:
    s = re.sub(r"[A-Z]", "A", tok)
    s = re.sub(r"[a-z]", "a", s)
    s = re.sub(r"\d", "9", s)
    s = re.sub(r"(.)\1+", r"\1", s)
    return s[:6]


def tokens(f: dict, text: str, client: str = "", doc_type: str = "") -> list[str]:
    """The sparse features: words, shape, font, client, document type."""
    t = " ".join((text or "").split())
    ws = [re.sub(r"\d+", "#", re.sub(r"[^\w#]", "", w.lower())) for w in t.split()[:8]]
    ws = [w for w in ws if w]
    font = (f.get("font") or "?")[:24]
    sz = round(float(f.get("size_rel") or 1.0) * 10)
    bold = int(f.get("bold") or 0)
    caps = 1 if float(f.get("caps") or 0) >= 0.8 and sum(ch.isalpha() for ch in t) >= 3 else 0
    out = ["b", f"font={font}", f"fs={font}|{sz}|{bold}", f"sz={sz}|{bold}", f"len={_bucket(int(f.get('words') or 0))}",
           f"caps={caps}", f"lines={min(int(f.get('lines') or 1), 5)}"]
    if ws:
        out += [f"w0={ws[0]}", "w01=" + " ".join(ws[:2])]
        out += [f"bw={w}" for w in ws[:6]]
    first = t.split()[0] if t.split() else ""
    out.append(f"sh={_shape(first)}")
    last = t[-1:] if t else ""
    out.append("end=" + (last if last in ".:;?!," else ("a" if last.isalnum() else "o")))
    if client:
        out += [f"client={client}", f"cfs={client}|{font}|{sz}|{bold}|{caps}"]
    if doc_type:
        out += [f"doc={doc_type}", f"dfs={doc_type}|{sz}|{bold}|{caps}"]
    return out


def style_key(f: dict) -> str:
    caps = 1 if float(f.get("caps") or 0) >= 0.8 else 0
    return f"{(f.get('font') or '?')[:24]}|{round(float(f.get('size_rel') or 1.0) * 10) / 10:.1f}|" \
           f"{int(f.get('bold') or 0)}|{int(f.get('italic') or 0)}|{caps}"


# ================================================================ the trained model
class RoleModel:
    """Softmax regression over dense layout features + hashed sparse tokens.

    Small, fast (numpy only), retrained from scratch whenever the database
    changes; its accuracy is measured on samples it was not trained on."""

    DIM = 4096

    def __init__(self, classes, mean, std, Wd, Ws, b, T: float = 1.0):
        self.classes = list(classes)
        self.mean, self.std, self.Wd, self.Ws, self.b = mean, std, Wd, Ws, b
        # V4.27: the temperature that makes its probabilities honest (fitted on
        # files it was not trained on; 1.0 = as the softmax says)
        self.T = float(T) if T and float(T) > 0 else 1.0

    @staticmethod
    def _sparse(np, token_rows, dim):
        ci, starts = [], []
        for toks in token_rows:
            starts.append(len(ci))
            ci.extend(sorted({_h(t, dim) for t in toks}) or [0])
        return np.asarray(ci, dtype=np.int64), np.asarray(starts, dtype=np.int64)

    def _logits(self, np, Xs, ci, starts):
        Z = Xs @ self.Wd + self.b
        Z = Z + np.add.reduceat(self.Ws[ci], starts, axis=0)
        return Z

    @classmethod
    def fit(cls, dense_rows, token_rows, labels, weights, classes=None, epochs: int = 160,
            lr: float = 0.06, l2: float = 2e-4):
        np = _np()
        classes = list(classes or sorted(set(labels)))
        idx = {c: i for i, c in enumerate(classes)}
        keep = [i for i, y in enumerate(labels) if y in idx]
        X = np.asarray([dense_rows[i] for i in keep], dtype=np.float32)
        y = np.asarray([idx[labels[i]] for i in keep], dtype=np.int64)
        w = np.asarray([weights[i] for i in keep], dtype=np.float32)
        toks = [token_rows[i] for i in keep]
        n, d = X.shape
        C = len(classes)
        mean = X.mean(0)
        std = X.std(0) + 1e-3
        Xs = (X - mean) / std
        ci, starts = cls._sparse(np, toks, cls.DIM)
        ri = np.repeat(np.arange(n), np.diff(np.append(starts, len(ci))))
        # rarer roles count more (square root of the imbalance, at most 8×)
        cnt = np.bincount(y, minlength=C).astype(np.float32)
        cw = np.minimum(8.0, np.sqrt(cnt.max() / np.maximum(cnt, 1)))
        w = w * cw[y]
        w = w / w.sum()
        Y = np.zeros((n, C), dtype=np.float32)
        Y[np.arange(n), y] = 1.0
        m = cls(classes, mean, std, np.zeros((d, C), np.float32), np.zeros((cls.DIM, C), np.float32),
                np.zeros(C, np.float32))
        params = [m.Wd, m.Ws, m.b]
        mom = [np.zeros_like(p) for p in params]
        vel = [np.zeros_like(p) for p in params]
        b1, b2, eps = 0.9, 0.999, 1e-8
        for t in range(1, epochs + 1):
            Z = m._logits(np, Xs, ci, starts)
            Z -= Z.max(1, keepdims=True)
            P = np.exp(Z)
            P /= P.sum(1, keepdims=True)
            G = (P - Y) * w[:, None]
            gWd = Xs.T @ G + l2 * m.Wd
            gb = G.sum(0)
            Gr = G[ri]
            gWs = np.stack([np.bincount(ci, weights=Gr[:, k], minlength=cls.DIM) for k in range(C)], 1)
            gWs = gWs.astype(np.float32) + l2 * m.Ws
            for i, g in enumerate((gWd, gWs, gb)):
                mom[i] = b1 * mom[i] + (1 - b1) * g
                vel[i] = b2 * vel[i] + (1 - b2) * g * g
                mh = mom[i] / (1 - b1 ** t)
                vh = vel[i] / (1 - b2 ** t)
                params[i] -= lr * mh / (np.sqrt(vh) + eps)
        return m

    def logits(self, dense_rows, token_rows):
        """The raw scores (before the softmax and the temperature)."""
        np = _np()
        if not dense_rows:
            return np.zeros((0, len(self.classes)), dtype=np.float32)
        X = (np.asarray(dense_rows, dtype=np.float32) - self.mean) / self.std
        ci, starts = self._sparse(np, token_rows, self.DIM)
        return self._logits(np, X, ci, starts)

    def predict(self, dense_rows, token_rows):
        np = _np()
        if not dense_rows:
            return np.zeros((0, len(self.classes)))
        return softmax(self.logits(dense_rows, token_rows), self.T)

    def to_bytes(self) -> bytes:
        np = _np()
        bio = io.BytesIO()
        np.savez_compressed(bio, mean=self.mean, std=self.std, Wd=self.Wd, Ws=self.Ws.astype(np.float16),
                            b=self.b, classes=np.asarray(json.dumps(self.classes)),
                            T=np.asarray(self.T, dtype=np.float32))
        return bio.getvalue()

    @classmethod
    def from_bytes(cls, raw: bytes) -> "RoleModel":
        np = _np()
        z = np.load(io.BytesIO(raw), allow_pickle=False)
        T = float(z["T"]) if "T" in z.files else 1.0           # a model trained before V4.27 has none
        return cls(json.loads(str(z["classes"])), z["mean"], z["std"], z["Wd"], z["Ws"].astype(np.float32), z["b"], T)


# ================================================================ honest probabilities (V4.27)
def softmax(Z, T: float = 1.0):
    """Row-wise softmax of Z / T."""
    np = _np()
    Z = np.asarray(Z, dtype=np.float64) / (T or 1.0)
    Z = Z - Z.max(1, keepdims=True)
    P = np.exp(Z)
    return P / P.sum(1, keepdims=True)


def _nll(Z, y, T: float) -> float:
    np = _np()
    P = softmax(Z, T)
    return float(-np.log(np.maximum(P[np.arange(len(y)), y], 1e-12)).mean())


def fit_temperature(Z, y, lo: float = 0.4, hi: float = 8.0) -> float:
    """The one number T that makes softmax(Z / T) as honest as it can be on
    files the model did not train on (it minimises the log-loss).

    A model trained for many rounds on few examples is too sure of itself
    (T > 1 spreads its probabilities out); one trained on too much weight on
    rare tags is too timid (T < 1). The brain weighs the model's voice against
    the other sources, so a voice that says 99 % when it is right 70 % of the
    time would outvote them for no reason."""
    np = _np()
    Z = np.asarray(Z, dtype=np.float64)
    y = np.asarray(y, dtype=np.int64)
    if len(y) < 10 or Z.ndim != 2 or Z.shape[1] < 2:
        return 1.0
    grid = np.exp(np.linspace(np.log(lo), np.log(hi), 40))
    vals = [_nll(Z, y, float(t)) for t in grid]
    i = int(np.argmin(vals))
    a, b = float(grid[max(0, i - 1)]), float(grid[min(len(grid) - 1, i + 1)])
    g = (math.sqrt(5.0) - 1.0) / 2.0                     # golden section inside the best cell
    c_, d_ = b - g * (b - a), a + g * (b - a)
    fc, fd = _nll(Z, y, c_), _nll(Z, y, d_)
    for _ in range(28):
        if fc < fd:
            b, d_, fd = d_, c_, fc
            c_ = b - g * (b - a)
            fc = _nll(Z, y, c_)
        else:
            a, c_, fc = c_, d_, fd
            d_ = a + g * (b - a)
            fd = _nll(Z, y, d_)
    return round(float((a + b) / 2.0), 4)


def calibration_error(P, y, bins: int = 10) -> float:
    """Expected calibration error: how far 'I am 90 % sure' is from 'right 90 % of the time'."""
    np = _np()
    P = np.asarray(P, dtype=np.float64)
    y = np.asarray(y, dtype=np.int64)
    if len(y) == 0:
        return 0.0
    conf = P.max(1)
    hit = (P.argmax(1) == y).astype(np.float64)
    edges = np.linspace(0.0, 1.0, bins + 1)
    err = 0.0
    for k in range(bins):
        m = (conf > edges[k]) & (conf <= edges[k + 1]) if k else (conf >= edges[k]) & (conf <= edges[k + 1])
        if m.any():
            err += m.mean() * abs(float(hit[m].mean()) - float(conf[m].mean()))
    return float(err)


_MODEL = {"id": None, "model": None, "meta": None, "checked": 0.0}
_MODEL_COLS = ("id", "created", "n_train", "n_samples", "classes", "accuracy", "macro_f1", "baseline", "per_role",
               "trigger", "seconds", "logloss", "ece", "temperature", "folds")


def _model_cols(c: sqlite3.Connection) -> str:
    """The columns of brain_models that this database has (a column added by an update may be missing
    for a moment in a database another process is still holding)."""
    have = {r[1] for r in c.execute("PRAGMA table_info(brain_models)")}
    return ", ".join(k for k in _MODEL_COLS if k in have)


def latest_model(refresh: bool = False):
    """(RoleModel, its row) of the newest training — or (None, None)."""
    if _np() is None or not L.has_data():
        return None, None
    if not refresh and _MODEL["model"] is not None and time.time() - _MODEL["checked"] < 30:
        return _MODEL["model"], _MODEL["meta"]
    with L.connect() as c:
        r = c.execute(f"SELECT {_model_cols(c)} FROM brain_models ORDER BY id DESC LIMIT 1").fetchone()
        if r is None:
            _MODEL.update(id=None, model=None, meta=None, checked=time.time())
            return None, None
        if r["id"] != _MODEL["id"]:
            w = c.execute("SELECT weights FROM brain_models WHERE id=?", (r["id"],)).fetchone()
            try:
                _MODEL["model"] = RoleModel.from_bytes(w["weights"])
            except Exception:
                _MODEL["model"] = None
            _MODEL["id"] = r["id"]
        meta = dict(r)
        for k in ("classes", "per_role"):
            try:
                meta[k] = json.loads(meta.get(k) or "null")
            except ValueError:
                meta[k] = None
        _MODEL["meta"] = meta
        _MODEL["checked"] = time.time()
    return _MODEL["model"], _MODEL["meta"]


def data_signature(c: sqlite3.Connection) -> str:
    a = c.execute("SELECT COUNT(*) n, MAX(learned) m FROM samples WHERE enabled=1 AND status='ready'").fetchone()
    parts = [str(a["n"]), str(a["m"] or "")]
    for t in ("blocks", "corrections", "feedback", "docs"):
        parts.append(str(c.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]))
    return ":".join(parts)


def _sample_weight(role: str, words: int, lines: int) -> float:
    """A heading of 40 words in a sample is more likely a slip than a rule."""
    if role in HEADINGS and (words > 30 or lines > 4):
        return 0.2
    return 1.0


def recency(stamp, now: datetime | None = None) -> float:
    """1.0 for what was learned today, falling to RECENT_FLOOR for what is years old.

    A client's style moves on (a new edition, a new designer); what was learned
    last year still counts, but this year's files count a little more."""
    try:
        t = datetime.fromisoformat(str(stamp)[:19])
    except (TypeError, ValueError):
        return 1.0
    age = max(0.0, ((now or datetime.now()) - t).total_seconds() / 86400.0)
    return RECENT_FLOOR + (1.0 - RECENT_FLOOR) * 0.5 ** (age / RECENT_HALF_LIFE)


def training_rows(c: sqlite3.Connection, cap: int = 40000) -> list[tuple]:
    """(dense, tokens, role, weight, group) for every example the database holds."""
    rows = []
    now = datetime.now()
    q = ("SELECT b.*, s.client AS sclient, s.learned AS slearned, d.doc_type AS dtype FROM blocks b "
         "JOIN samples s ON s.id=b.sample_id LEFT JOIN docs d ON d.sample_id=b.sample_id "
         "WHERE s.enabled=1 AND s.status='ready'")
    for r in c.execute(q):
        f = dict(r)
        if f["role"] not in LEARNABLE:
            continue
        rows.append((dense(f), tokens(f, f["text"], f["sclient"] or "", f["dtype"] or ""), f["role"],
                     _sample_weight(f["role"], int(f["words"] or 0), int(f["lines"] or 1))
                     * recency(f["slearned"], now), f["sample_id"]))
    q = ("SELECT b.*, s.client AS sclient, s.learned AS slearned, d.doc_type AS dtype FROM corrections b "
         "JOIN samples s ON s.id=b.sample_id LEFT JOIN docs d ON d.sample_id=b.sample_id "
         "WHERE s.enabled=1 AND s.status='ready'")
    for r in c.execute(q):
        f = dict(r)
        if f["role"] not in LEARNABLE:
            continue
        rows.append((dense(f), tokens(f, f["text"], f["sclient"] or "", f["dtype"] or ""), f["role"],
                     3.0 * recency(f["slearned"], now), f["sample_id"]))
    for r in c.execute("SELECT * FROM feedback"):
        f = dict(r)
        if f["role"] not in LEARNABLE:
            continue
        rows.append((dense(f), tokens(f, f["text"], f["client"] or "", ""), f["role"],
                     1.5 * recency(f.get("created"), now), "fb:" + str(f["job"])))
    if len(rows) > cap:
        # keep every rarer tag, thin out the paragraphs
        import random
        rnd = random.Random(11)
        other = [r for r in rows if r[2] != "P"]
        ps = [r for r in rows if r[2] == "P"]
        room = max(cap - len(other), cap // 3)
        rows = other[:cap] + rnd.sample(ps, min(len(ps), room))
    return rows


def _split_idx(rows: list[tuple]) -> tuple[list[int], list[int]]:
    """Held-out *files* (about a fifth of the examples): the accuracy says how
    the brain does on a file it has never seen, not on texts it memorised."""
    groups = collections.Counter(r[4] for r in rows)
    if len(groups) >= 3:
        hold: set = set()
        k = 0
        for g in sorted(groups, key=lambda g: zlib.crc32(g.encode())):
            if k >= 0.2 * len(rows) or len(hold) >= len(groups) - 1:
                break
            hold.add(g)
            k += groups[g]
        tr = [i for i, r in enumerate(rows) if r[4] not in hold]
        te = [i for i, r in enumerate(rows) if r[4] in hold]
        if tr and te:
            return tr, te
    tr, te = [], []
    for i, _ in enumerate(rows):
        (te if i % 5 == 0 else tr).append(i)
    return tr, te


def _split(rows: list[tuple]) -> tuple[list, list]:
    tr, te = _split_idx(rows)
    return [rows[i] for i in tr], [rows[i] for i in te]


def _grouped_folds(rows: list[tuple], k: int) -> list[tuple[list[int], list[int]]]:
    """[(train indexes, test indexes)] — cross-validation over *files* (V4.27).

    Every file is held out exactly once, so the accuracy is measured on all of
    the examples instead of a fifth of them (with a handful of sample files a
    single hold-out can be three files, and the number then says more about
    which three than about the brain). The files go to the folds biggest first,
    each into the lightest fold, in an order fixed by their names: the same
    data always gives the same folds."""
    groups = collections.Counter(r[4] for r in rows)
    k = max(2, min(k, len(groups)))
    load = [0] * k
    fold_of: dict = {}
    for g in sorted(groups, key=lambda g: (-groups[g], zlib.crc32(g.encode()), g)):
        j = min(range(k), key=lambda j: (load[j], j))
        fold_of[g] = j
        load[j] += groups[g]
    out = []
    for j in range(k):
        te = [i for i, r in enumerate(rows) if fold_of[r[4]] == j]
        tr = [i for i, r in enumerate(rows) if fold_of[r[4]] != j]
        if te and tr:
            out.append((tr, te))
    return out


def _metrics(model: RoleModel, rows: list[tuple]) -> dict:
    P = model.predict([r[0] for r in rows], [r[1] for r in rows])
    return _metrics_probs(model.classes, P, [r[2] for r in rows])


def _metrics_probs(classes: list, P, gold: list) -> dict:
    """Accuracy, macro-F1, precision / recall per tag … of predictions P (rows × classes)."""
    np = _np()
    pred = [classes[int(i)] for i in P.argmax(1)]
    acc = sum(1 for a, b in zip(pred, gold) if a == b) / max(1, len(gold))
    base_role = collections.Counter(gold).most_common(1)[0][0] if gold else "P"
    baseline = sum(1 for g in gold if g == base_role) / max(1, len(gold))
    per = {}
    f1s = []
    for role in sorted(set(gold) | set(pred)):
        tp = sum(1 for a, b in zip(pred, gold) if a == role and b == role)
        fp = sum(1 for a, b in zip(pred, gold) if a == role and b != role)
        fn = sum(1 for a, b in zip(pred, gold) if a != role and b == role)
        p = tp / (tp + fp) if tp + fp else 0.0
        r_ = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * p * r_ / (p + r_) if p + r_ else 0.0
        if role in gold:
            f1s.append(f1)
        per[role] = {"precision": round(p, 3), "recall": round(r_, 3), "f1": round(f1, 3), "n": gold.count(role)}
    del np
    return {"accuracy": round(acc, 4), "macro_f1": round(sum(f1s) / max(1, len(f1s)), 4),
            "baseline": round(baseline, 4), "per_role": per, "n": len(gold)}


def evaluate(rows: list[tuple], classes: list, progress=None) -> tuple[dict, float, int]:
    """-> (metrics, temperature, folds): how the brain does on files it did not train on.

    V4.27: cross-validation over files (every file held out once) while the data is small
    enough to train a few times; one held-out fifth when it is big. The held-out predictions
    also tell the model how sure it is allowed to sound (the temperature)."""
    np = _np()
    ev = {"accuracy": None, "macro_f1": None, "baseline": None, "per_role": {}, "n": 0,
          "logloss": None, "ece": None, "ece_raw": None}
    if len(rows) < 24 or len({r[4] for r in rows}) < 2:
        return ev, 1.0, 0
    folds = _grouped_folds(rows, CV_FOLDS) if len(rows) <= CV_MAX_ROWS else [_split_idx(rows)]
    Zs, golds = [], []
    for k, (tr_i, te_i) in enumerate(folds, start=1):
        if progress:
            progress(0.05 + 0.5 * k / max(1, len(folds)),
                     f"measuring {k}/{len(folds)}: training on {len(tr_i)}, testing on {len(te_i)}")
        m_eval = RoleModel.fit([rows[i][0] for i in tr_i], [rows[i][1] for i in tr_i],
                               [rows[i][2] for i in tr_i], [rows[i][3] for i in tr_i], classes)
        Zs.append(m_eval.logits([rows[i][0] for i in te_i], [rows[i][1] for i in te_i]))
        golds += [rows[i][2] for i in te_i]
    if not Zs:
        return ev, 1.0, 0
    Z = np.concatenate(Zs, 0)
    y = np.asarray([classes.index(g) for g in golds], dtype=np.int64)
    temperature = fit_temperature(Z, y)
    P = softmax(Z, temperature)
    ev = _metrics_probs(classes, P, golds)
    ev.update(logloss=round(_nll(Z, y, temperature), 4), ece=round(calibration_error(P, y), 4),
              ece_raw=round(calibration_error(softmax(Z, 1.0), y), 4))
    return ev, temperature, len(folds)


def train(trigger: str = "manual", force: bool = False, progress=None) -> dict:
    """Train the role model again on everything the database holds."""
    np = _np()
    if np is None:
        return {"ok": False, "error": "numpy is not installed (pip install numpy)"}
    if not L.has_data():
        return {"ok": False, "error": "no sample files yet"}
    t0 = time.time()
    c = L.connect()
    try:
        sig = data_signature(c)
        last = c.execute("SELECT id, signature FROM brain_models ORDER BY id DESC LIMIT 1").fetchone()
        if last is not None and last["signature"] == sig and not force:
            return {"ok": True, "skipped": True, "id": last["id"], "why": "nothing new to learn"}
        rows = training_rows(c)
        roles = collections.Counter(r[2] for r in rows)
        classes = sorted(r for r, n in roles.items() if n >= 3)
        rows = [r for r in rows if r[2] in classes]
        if len(rows) < 20 or len(classes) < 2:
            return {"ok": False, "error": f"not enough tagged examples yet ({len(rows)} usable, "
                                          f"{len(classes)} kind(s) of tag)"}
        n_samples = len({r[4] for r in rows})
        ev, temperature, n_folds = evaluate(rows, classes, progress)
        if progress:
            progress(0.6, f"training on all {len(rows)} examples")
        model = RoleModel.fit([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows],
                              [r[3] for r in rows], classes)
        model.T = temperature
        secs = round(time.time() - t0, 1)
        cur = c.execute("INSERT INTO brain_models (created, n_train, n_samples, classes, accuracy, macro_f1, baseline, "
                        "per_role, weights, signature, trigger, seconds, logloss, ece, temperature, folds) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (_now(), len(rows), n_samples, json.dumps(classes), ev["accuracy"], ev["macro_f1"],
                         ev["baseline"], json.dumps(ev["per_role"]), model.to_bytes(), sig, trigger[:120], secs,
                         ev["logloss"], ev["ece"], temperature, n_folds))
        mid = cur.lastrowid
        # keep the last 40 trainings (the learning curve); the weights of old ones go
        c.execute("UPDATE brain_models SET weights=NULL WHERE id NOT IN "
                  "(SELECT id FROM brain_models ORDER BY id DESC LIMIT 3)")
        c.execute("DELETE FROM brain_models WHERE id NOT IN (SELECT id FROM brain_models ORDER BY id DESC LIMIT 40)")
        c.commit()
        _MODEL["checked"] = 0.0
        return {"ok": True, "id": mid, "examples": len(rows), "samples": n_samples, "classes": classes,
                "accuracy": ev["accuracy"], "macro_f1": ev["macro_f1"], "baseline": ev["baseline"],
                "tested_on": ev["n"], "seconds": secs, "folds": n_folds, "temperature": temperature,
                "ece": ev["ece"], "ece_raw": ev["ece_raw"], "logloss": ev["logloss"]}
    finally:
        c.close()


def train_all(trigger: str = "after learning", force: bool = False) -> dict:
    """The brain's model again, then the learned code (V4.26) and the zone models (V4.28)."""
    out = {"train": train(trigger=trigger, force=force)}
    try:
        from . import codegen
        cfg, lc = ai_from_config()
        out["code"] = codegen.synthesize(ai_cfg=cfg if lc.get("ai_study", True) else None)
    except Exception as exc:
        out["code"] = {"ok": False, "error": str(exc)[:300]}
    try:                                         # V4.28: where zones are cut
        from . import zonelearn
        out["zones"] = zonelearn.train_all(force=force)
    except Exception as exc:
        out["zones"] = {"ok": False, "error": str(exc)[:300]}
    return out


# ================================================================ storing a sample
def store_sample(c: sqlite3.Connection, sid: str, client: str, row: dict, r: dict, model=None, auto=None) -> dict:
    """After learn_sample read a sample: its document, its memory and — for
    an approved file — what the brain decided against what people approved."""
    out: dict = {}
    doc = r.get("doc") or {}
    if doc:
        old = c.execute("SELECT ai_type FROM docs WHERE sample_id=?", (sid,)).fetchone()
        ai_type = old["ai_type"] if old is not None else None
        c.execute("INSERT OR REPLACE INTO docs (sample_id, client, doc_type, rule_type, ai_type, pages, body, "
                  "fingerprint, styles, first_text, created) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (sid, client, ai_type or doc.get("type"), doc.get("type"), ai_type, doc.get("pages"), doc.get("body"),
                   _pack(doc["vec"]), json.dumps(doc.get("styles") or []), (doc.get("first") or "")[:1500], _now()))
        out["doc_type"] = ai_type or doc.get("type")
    # the memory: short texts with their tag, as vectors (RAG)
    c.execute("DELETE FROM memory WHERE sample_id=?", (sid,))
    mem = []
    seen: set = set()
    blocks = sorted(r.get("blocks") or [], key=lambda b: b["role"] == "P")
    for b in blocks:
        t = " ".join((b.get("text") or "").split())
        if not t or int(b.get("words") or 0) > 40 or (b["role"], t.lower()) in seen:
            continue
        if b["role"] == "P" and sum(1 for m in mem if m[2] == "P") >= 1500:
            continue
        seen.add((b["role"], t.lower()))
        mem.append((sid, client, b["role"], t[:220], b.get("size_rel"), b.get("bold"), b.get("font") or "",
                    _pack(text_vec(t)), None, None))
        if len(mem) >= 3000:
            break
    for b in r.get("corrections") or []:         # the fixes are the most useful memories
        t = " ".join((b.get("text") or "").split())
        if t and int(b.get("words") or 0) <= 40 and b["role"] in LEARNABLE:
            mem.append((sid, client, b["role"], t[:220], b.get("size_rel"), b.get("bold"), b.get("font") or "",
                        _pack(text_vec(t)), None, None))
    c.executemany("INSERT INTO memory VALUES (?,?,?,?,?,?,?,?,?,?)", mem)
    out["memory"] = len(mem)
    if row.get("kind") == "approved" and row.get("job"):
        try:
            det = json.loads(row.get("details") or "{}") if isinstance(row.get("details"), str) else (row.get("details") or {})
        except ValueError:
            det = {}
        try:
            out["outcome"] = learn_outcome(c, row["job"], client, model, auto,
                                           when=det.get("approved_at") or row.get("created") or "")
        except Exception as exc:
            out["outcome_error"] = str(exc)[:200]
        c.execute("DELETE FROM feedback WHERE job=?", (row["job"],))     # the approved sample supersedes it
    return out


def embed_memory(sid: str, cfg) -> dict:
    """The memory of one sample in the AI's own vectors (an Ollama embedding
    model, when one is installed): semantic look-ups, not only lexical."""
    from .ai import OllamaError, embed, embedding_model
    try:
        name = embedding_model(cfg)
    except OllamaError as exc:
        return {"ok": False, "error": str(exc)}
    if not name:
        return {"ok": False, "error": "no embedding model installed (e.g. ollama pull nomic-embed-text)"}
    c = L.connect()
    try:
        rows = list(c.execute("SELECT rowid, text FROM memory WHERE sample_id=? AND (emb IS NULL OR emb_model!=?)",
                              (sid, name)))
        done = 0
        for i in range(0, len(rows), 48):
            part = rows[i:i + 48]
            vecs = embed(cfg, [r["text"] for r in part], name)
            if len(vecs) != len(part):
                break
            c.executemany("UPDATE memory SET emb=?, emb_model=? WHERE rowid=?",
                          [(_pack(v), name, r["rowid"]) for r, v in zip(part, vecs)])
            c.commit()
            done += len(part)
        return {"ok": True, "embedded": done, "model": name}
    except OllamaError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        c.close()


# ================================================================ the AI studies a sample
def sample_digest(c: sqlite3.Connection, sid: str) -> str:
    s = c.execute("SELECT * FROM samples WHERE id=?", (sid,)).fetchone()
    d = c.execute("SELECT * FROM docs WHERE sample_id=?", (sid,)).fetchone()
    lines = []
    if d is not None:
        lines.append(f"Document type (by rule): {d['rule_type']} · {d['pages']} pages · body text {d['body']} pt")
        lines.append("First page text: " + " ".join((d["first_text"] or "").split())[:600])
    roles = collections.Counter()
    looks = collections.defaultdict(collections.Counter)
    examples = collections.defaultdict(list)
    for b in c.execute("SELECT role, text, size_rel, bold, font, words FROM blocks WHERE sample_id=?", (sid,)):
        roles[b["role"]] += 1
        looks[b["role"]][f"{b['font'] or '?'} {round(float(b['size_rel'] or 1), 1)}×{' bold' if b['bold'] else ''}"] += 1
        t = " ".join((b["text"] or "").split())
        if len(examples[b["role"]]) < 3 and t and len(t) <= 120:
            examples[b["role"]].append(t)
    lines.append("Tags used: " + ", ".join(f"{r} ×{n}" for r, n in roles.most_common(14)))
    for r, _ in roles.most_common(10):
        lines.append(f"- {r}: set in " + "; ".join(f"{k} ({n})" for k, n in looks[r].most_common(2))
                     + " · e.g. " + " | ".join(f'"{e[:70]}"' for e in examples[r]))
    pairs = [f"{r['parent']} > {r['child']} ×{r['n']}" for r in c.execute(
        "SELECT parent, child, n FROM structure WHERE sample_id=? AND parent NOT LIKE '@%' ORDER BY n DESC LIMIT 14",
        (sid,))]
    if pairs:
        lines.append("Which tag holds which: " + ", ".join(pairs))
    caps = [f"{r['parent'].split(':', 1)[1]} caption {r['child']} ×{r['n']}" for r in c.execute(
        "SELECT parent, child, n FROM structure WHERE sample_id=? AND parent LIKE '@caption:%'", (sid,))]
    if caps:
        lines.append("Captions: " + ", ".join(caps))
    arts = [f'"{r["pattern"][:50]}" ({r["band"]})' for r in c.execute(
        "SELECT pattern, band, n FROM artifacts WHERE sample_id=? ORDER BY n DESC LIMIT 6", (sid,))]
    if arts:
        lines.append("Running heads / feet left out as artifacts: " + ", ".join(arts))
    fixes = [f'"{(r["text"] or "")[:60]}": tool said {r["from_role"]}, correct is {r["role"]}' for r in c.execute(
        "SELECT from_role, role, text FROM corrections WHERE sample_id=? LIMIT 8", (sid,))]
    if fixes:
        lines.append("People corrected the tool's proposal: " + "; ".join(fixes))
    if s is not None:
        lines.insert(0, f"File: {s['filename']} · client: {s['client'] or 'common'}")
    return "\n".join(lines)[:6000]


def study_sample(sid: str, cfg) -> dict:
    """The local AI reads the digest of a sample and writes the client's
    tagging rules (and the document type) into the database."""
    from .ai import OllamaError, study_sample as ask
    c = L.connect()
    try:
        s = c.execute("SELECT * FROM samples WHERE id=?", (sid,)).fetchone()
        if s is None or s["status"] != "ready":
            return {"ok": False, "error": "sample not ready"}
        digest = sample_digest(c, sid)
        try:
            ans = ask(cfg, s["client"] or "every client", digest, DOC_TYPES)
        except OllamaError as exc:
            c.execute("INSERT OR REPLACE INTO insights (sample_id, client, created, error) VALUES (?,?,?,?)",
                      (sid, s["client"], _now(), str(exc)[:300]))
            c.commit()
            return {"ok": False, "error": str(exc)}
        if not ans:
            return {"ok": False, "error": "the model gave no usable answer"}
        from .ai import resolve_model
        try:
            mname = resolve_model(cfg, "text", "study")[0]
        except Exception:
            mname = ""
        c.execute("INSERT OR REPLACE INTO insights (sample_id, client, doc_type, summary, rules, model, created, error) "
                  "VALUES (?,?,?,?,?,?,?,?)", (sid, s["client"], ans.get("doc_type"), ans.get("summary"),
                                               json.dumps(ans.get("rules") or []), mname, _now(), ""))
        if ans.get("doc_type") in DOC_TYPES:
            c.execute("UPDATE docs SET ai_type=?, doc_type=? WHERE sample_id=?", (ans["doc_type"], ans["doc_type"], sid))
        c.commit()
        return {"ok": True, "rules": len(ans.get("rules") or []), "doc_type": ans.get("doc_type")}
    finally:
        c.close()


def rules_for(client: str, weights: dict | None = None, limit: int = 10) -> list[str]:
    """The AI-written rules of this client (and of the most similar samples)."""
    if not L.has_data():
        return []
    out: list[str] = []
    with L.connect() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT i.sample_id, i.client, i.rules FROM insights i JOIN samples s ON s.id=i.sample_id "
            "WHERE s.enabled=1 AND i.rules IS NOT NULL AND i.rules != ''")]
    w = weights or {}
    rows.sort(key=lambda r: (r["client"] != client or not client, -float(w.get(r["sample_id"], 0))))
    for r in rows:
        if r["client"] != client and float(w.get(r["sample_id"], 0)) < 1.2 and out:
            continue
        try:
            for x in json.loads(r["rules"] or "[]"):
                t = (x.get("rule") if isinstance(x, dict) else str(x)) or ""
                t = " ".join(t.split())[:160]
                if t and t not in out:
                    out.append(t)
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out[:limit]


# ================================================================ deciding
class Context:
    """Everything the brain knows, read once for a file."""

    def __init__(self, client: str, scope: str = "client", weights: dict | None = None, doc_type: str = "",
                 know=None, samples: bool = True):
        np = _np()
        self.client = client or ""
        self.scope = scope
        self.weights = weights or {}
        self.doc_type = doc_type or ""
        samples = samples and L.has_data()
        self.know = know if know is not None else (L.Knowledge(client, scope=scope, weights=weights)
                                                   if samples else None)
        self.model, self.model_meta = latest_model() if samples else (None, None)
        self.rules = []
        self.rule_clients = {self.client, ""}
        if samples:
            try:                                   # V4.26: the code the learning engine wrote
                from . import codegen
                self.rules = codegen.active_rules()
            except Exception:
                self.rules = []
        self.style: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        self.trust: dict[tuple, tuple] = {}
        self.mem_rows: list[dict] = []
        self.mem = None
        self.mem_emb = None
        self.emb_model = ""
        if not samples:
            return
        with L.connect() as c:
            for r in c.execute("SELECT source, role, ok, n FROM trust"):
                self.trust[(r["source"], r["role"])] = (r["ok"], r["n"])
            if scope == "all":
                ids = [r["id"] for r in c.execute("SELECT id FROM samples WHERE enabled=1 AND status='ready'")]
            else:
                ids = [r["id"] for r in c.execute("SELECT id FROM samples WHERE enabled=1 AND status='ready' "
                                                  "AND (client=? OR client='')", (self.client,))]
            mine = {r["id"] for r in c.execute("SELECT id FROM samples WHERE client=? AND ?!=''",
                                               (self.client, self.client))}

            def sw(sid):
                if scope == "all":
                    return max(2.0, self.weights.get(sid, 1.0)) if sid in mine else float(self.weights.get(sid, 1.0))
                return 2.0 if sid in mine else 1.0
            if ids:
                q = ",".join("?" * len(ids))
                for b in c.execute(f"SELECT sample_id, role, font, size_rel, bold, italic, caps FROM blocks "
                                   f"WHERE sample_id IN ({q}) AND font IS NOT NULL AND font != '' LIMIT 80000", ids):
                    if b["role"] in LEARNABLE:
                        self.style[style_key(dict(b))][b["role"]] += sw(b["sample_id"])
                for b in c.execute(f"SELECT sample_id, role, font, size_rel, bold, italic, caps FROM corrections "
                                   f"WHERE sample_id IN ({q}) AND font IS NOT NULL AND font != ''", ids):
                    if b["role"] in LEARNABLE:
                        self.style[style_key(dict(b))][b["role"]] += 2.0 * sw(b["sample_id"])
                self.mem_rows = [dict(r) for r in c.execute(
                    f"SELECT sample_id, client, role, text, size_rel, bold, font, vec, emb, emb_model FROM memory "
                    f"WHERE sample_id IN ({q}) LIMIT 60000", ids)]
            fb = "SELECT role, font, size_rel, bold, italic, caps FROM feedback WHERE font IS NOT NULL AND font != ''"
            for b in (c.execute(fb) if scope == "all" else c.execute(fb + " AND client=?", (self.client,))):
                if b["role"] in LEARNABLE:
                    self.style[style_key(dict(b))][b["role"]] += 1.0
        if self.mem_rows and np is not None:
            self.mem = np.stack([_unpack(r["vec"]) for r in self.mem_rows])
            self.mem_w = np.asarray([sw(r["sample_id"]) for r in self.mem_rows], dtype=np.float32)
            self.mem_size = np.asarray([float(r["size_rel"] or 1.0) for r in self.mem_rows], dtype=np.float32)
            self.mem_bold = np.asarray([int(r["bold"] or 0) for r in self.mem_rows], dtype=np.float32)
            embs = collections.Counter(r["emb_model"] for r in self.mem_rows if r["emb"])
            if embs:
                self.emb_model = embs.most_common(1)[0][0]
                dim = len(_unpack(next(r["emb"] for r in self.mem_rows if r["emb_model"] == self.emb_model)))
                self.mem_emb = np.stack([_unpack(r["emb"]) if r["emb_model"] == self.emb_model and r["emb"]
                                         else np.zeros(dim, np.float32) for r in self.mem_rows])

    @property
    def learned(self) -> bool:
        return bool((self.know is not None and not self.know.empty) or self.model is not None or self.style
                    or getattr(self, "rules", None))

    def trust_factor(self, source: str, role: str) -> float:
        ok, n = self.trust.get((source, role), (0, 0))
        ok2, n2 = self.trust.get((source, "*"), (0, 0))
        ok, n = ok + 0.3 * ok2, n + 0.3 * n2
        r = (ok + 2.25) / (n + 3.0)               # prior 75 % right
        return max(0.3, min(1.4, r / 0.75))

    def weight(self, source: str, role: str) -> float:
        return BASE_WEIGHT[source] * self.trust_factor(source, role)

    def similar(self, text: str, f: dict, k: int = 3, qemb=None) -> list[dict]:
        """The nearest finished examples (text and tag) in the memory."""
        np = _np()
        if self.mem is None or np is None:
            return []
        if qemb is not None and self.mem_emb is not None:
            q = np.asarray(qemb, dtype=np.float32)
            q = q / (np.linalg.norm(q) or 1.0)
            M = self.mem_emb
            norms = np.linalg.norm(M, axis=1)
            norms[norms == 0] = 1.0
            sim = (M @ q) / norms
        else:
            q = np.asarray(text_vec(text), dtype=np.float32)
            sim = self.mem @ q
        size = float(f.get("size_rel") or 1.0)
        look = np.exp(-np.abs(self.mem_size - size) * 2.0) * \
            (1.0 - 0.3 * np.abs(self.mem_bold - float(f.get("bold") or 0)))
        score = (0.72 * sim + 0.28 * look) * (0.75 + 0.25 * np.minimum(self.mem_w, 2.0) / 2.0)
        out, seen = [], set()
        for i in np.argsort(-score)[: k * 6]:
            r = self.mem_rows[int(i)]
            key = (r["role"], r["text"].lower())
            if key in seen:
                continue
            seen.add(key)
            out.append({"text": r["text"], "role": r["role"], "sim": round(float(sim[int(i)]), 3),
                        "client": r["client"] or "common"})
            if len(out) >= k:
                break
        return out


def opinions(ctx: Context, role: str, conf: float, text: str, f: dict, probs=None, fired: list | None = None) -> dict:
    """{source: {role: weight}} — what each source says about one tag."""
    out: dict[str, dict] = {"rules": {role: max(0.05, min(1.0, float(conf if conf is not None else 1.0)))}}
    if ctx.rules:
        try:
            from . import codegen
            fd = codegen.features(f, text, ctx.client, ctx.doc_type, role)
            hits = [r for r in codegen.opinions_for(ctx.rules, fd, ctx.rule_clients) if r["target"] != "Artifact"]
            if hits:
                best = max(hits, key=lambda r: r["precision"])
                out["code"] = {best["target"]: best["precision"]}
                if fired is not None:
                    fired.append(best)
        except Exception:
            pass
    # how new this tag is: nothing like it in the samples → the learned voices fade
    novelty = 1.0
    if ctx.know is not None and not ctx.know.empty:
        votes = ctx.know.vote(f)
        if votes:
            novelty = max(0.25, math.exp(-max(0.0, votes[0][2] - 0.3) / 1.2))
        if votes and votes[0][2] <= 1.5:
            near = math.exp(-votes[0][2] / 2.0)
            out["samples"] = {r: sh * near for r, sh, _ in votes[:4] if r in LEARNABLE}
        wd = ctx.know.wording(text)
        if wd and wd[2] >= 2:
            out["wording"] = {wd[0]: wd[1] * min(1.0, wd[2] / 3.0)}
    if probs is not None and ctx.model is not None:
        dist = {c: float(p) * novelty for c, p in zip(ctx.model.classes, probs) if p >= 0.04}
        if dist:
            out["model"] = dist
    st = ctx.style.get(style_key(f))
    if st:
        tot = sum(st.values())
        if tot >= 3:
            sup = min(1.0, tot / 8.0)
            out["style"] = {r: n / tot * sup for r, n in st.most_common(3)}
    return {k: v for k, v in out.items() if v}


def combine(ctx: Context, ops: dict) -> tuple[dict, str, float, float]:
    """-> (score per role, winner, share, margin)."""
    score: collections.Counter = collections.Counter()
    for src, dist in ops.items():
        for r, p in dist.items():
            score[r] += ctx.weight(src, r) * p
    if not score:
        return {}, "", 0.0, 0.0
    tot = sum(score.values()) or 1.0
    top = score.most_common(2)
    win, s1 = top[0]
    s2 = top[1][1] if len(top) > 1 else 0.0
    return dict(score), win, s1 / tot, (s1 - s2) / tot


def allowed(model, n, target: str, text: str, f: dict, ops: dict | None = None) -> str:
    """'' when PDF/UA allows the change, else why not."""
    if target not in SWAP:
        return f"{target} is not a tag the brain sets"
    words = len(text.split())
    if target in HEADINGS:
        if words > 22 or int(f.get("lines") or 1) > 3:
            return "too long for a heading"
        st = (ops or {}).get("style") or {}
        styled = bool(st) and max(st, key=st.get) == target and max(st.values()) >= 0.85
        if float(f.get("size_rel") or 1.0) < 0.95 and not styled:
            return "smaller than the body text — not a heading unless the client's style says so"
        if float(f.get("y") or 0) >= 0.9 and not styled:
            return "in the foot of the page — a running foot, not a heading"
        if words > 8 and re.search(r"[.!?]\s+[A-Z]", text):
            return "several sentences are a paragraph, not a heading"
        if text.rstrip().endswith(","):
            return "a heading does not end with a comma"
    if target == "Caption" and not any(x.role in ("Figure", "Table") for x in L._neighbours(model, n)):
        return "a caption must sit next to its figure or table"
    if target == "BlockQuote" and words < 6:
        return "too short for a quotation"
    return ""


def _retag(model, n, target: str, why: str, out: dict, changed: set) -> bool:
    if target == "Caption" and not any(x.role in ("Figure", "Table") for x in L._neighbours(model, n)):
        return False
    old = n.role
    n.role = target
    n.confidence = max(float(n.confidence or 0), 0.8)
    n.note = (n.note + "; " if n.note else "") + why
    if target in HEADINGS:
        n.style = dict(n.style or {}, level=int(target[1]))
    changed.add(n.id)
    out["changes"].append({"id": n.id, "from": old, "to": target, "why": why})
    return True


def _fix_levels(model, changed: set) -> None:
    """A heading the brain made never jumps more than one level down."""
    prev = 0
    for n in model.ordered_nodes():
        if n.role in HEADINGS:
            lv = int(n.role[1])
            if n.id in changed and prev and lv > prev + 1:
                n.role = f"H{prev + 1}"
                n.style = dict(n.style or {}, level=prev + 1)
                lv = prev + 1
            prev = lv


def _prev_heading(model, n) -> str:
    last = ""
    for x in model.ordered_nodes():
        if x.id == n.id:
            break
        if x.role in HEADINGS and x.text:
            last = f'{x.role} "{" ".join(x.text.split())[:40]}"'
    return last


# ================================================================ one file, one convention (V4.27)
def layout_voices(ctx: Context, cands: list, ops_list: list, need: float = 0.6, agree_min: int = 2) -> list:
    """What the *other* tags of the same look in this file say — one more voice per tag.

    A document keeps its conventions: the bold 14 pt lines are all section
    headings or none of them is. Every tag was decided on its own text, its own
    place and what the samples say. A tag that the learned sources are *sure*
    of (as sure as the brain needs to change a tag: `need`, `agree_min` sources
    agreeing) has a verdict worth sharing; this pools those verdicts over the
    tags that share its exact look (font, size, weight, capitals) in the same
    file, leaving the tag's own out. A heading whose wording is unusual is then
    not left behind by its neighbours — and a lone odd one does not drag the
    others along, because only sure verdicts are pooled.

    The voice is as loud as the look is *consistent*: when the sure others agree on one role
    (95 % and more) it speaks in full, when they split (65 % and less) it is silent — a look that
    is a heading here and a label there has no convention to follow. (In a simulation of files
    whose looks are 70 to 97 % consistent this is the difference between pooling that never loses
    anything and pooling that costs a few per cent where looks are mixed.)

    -> per candidate: None, or {"dist": {role: weight}, "firm": bool, "n": others}
    `firm` (≥ 4 sure others, ≥ 80 % of them one role) lets the voice count as an
    agreeing source; a weaker one only nudges the weighted share."""
    n = len(cands)
    dists: list = [None] * n
    groups: dict = collections.defaultdict(list)
    for i, ((_, _, _, f), ops) in enumerate(zip(cands, ops_list)):
        groups[style_key(f)].append(i)
        learned = {k: v for k, v in ops.items() if k not in ("rules", "layout") and v}
        if not learned:
            continue                       # nothing learned about it: it has no verdict to share
        score, win, share, _ = combine(ctx, learned)
        agree = sum(1 for v in learned.values() if max(v, key=v.get) == win)
        if not win or share < need or agree < agree_min:
            continue                       # it is not sure of itself: nothing to share
        tot = sum(score.values()) or 1.0
        dists[i] = {r: s / tot for r, s in score.items()}
    out: list = [None] * n
    for idxs in groups.values():
        voiced = [i for i in idxs if dists[i] is not None]
        if len(voiced) < LAYOUT_MIN:
            continue
        total: collections.Counter = collections.Counter()
        for j in voiced:
            total.update(dists[j])
        for i in idxs:
            k = len(voiced) - (1 if dists[i] is not None else 0)
            if k < LAYOUT_MIN:
                continue
            mean = collections.Counter(total)
            if dists[i] is not None:
                mean.subtract(dists[i])
            mean = {r: v / k for r, v in mean.items() if v / k >= 0.08}
            if not mean:
                continue
            top = max(mean.values())
            lo, hi = LAYOUT_PURE
            strength = min(1.0, k / 8.0) * max(0.0, min(1.0, (top - lo) / (hi - lo)))
            if strength * top < 0.03:
                continue                   # the sure others disagree: no convention to follow
            out[i] = {"dist": {r: round(v * strength, 4) for r, v in mean.items()}, "n": k,
                      "firm": k >= 4 and top >= LAYOUT_FIRM}
    return out


def _unify_levels(model, cands: list, shares: dict, out: dict) -> int:
    """The same look gets the same heading level in one file.

    A tag the brain made a heading takes the level the file already gives the
    others of its look when those agree (≥ 3 headings, ≥ 75 % one level) and
    the brain was not sure of its own level (< 85 %): H2 here and H3 there for
    the very same type style is a mistake somebody has to find in the editor."""
    by_look: dict = collections.defaultdict(collections.Counter)
    for n, _, _, f in cands:
        if n.id in model.nodes and n.role in HEADINGS:
            by_look[style_key(f)][n.role] += 1
    moved = 0
    for n, _, _, f in cands:
        share = shares.get(n.id)
        if share is None or n.id not in model.nodes or n.role not in HEADINGS or share >= 0.85:
            continue
        cnt = by_look.get(style_key(f))
        if not cnt or sum(cnt.values()) < 3:
            continue
        lvl, k = cnt.most_common(1)[0]
        if lvl == n.role or k / sum(cnt.values()) < 0.75:
            continue
        old = n.role
        n.role = lvl
        n.style = dict(n.style or {}, level=int(lvl[1]))
        n.note = (n.note + "; " if n.note else "") + f"brain: {lvl}, like the other {lvl}s of this look in the file"
        out["changes"].append({"id": n.id, "from": old, "to": lvl, "why": "same look, same level"})
        moved += 1
    return moved


def _review_first(cands: list, confs: list, changed: set, k: int = 8) -> list:
    """The doubtful tags to look at first: those whose answer teaches the most.

    A tag the brain is unsure of counts for more when many others in the file look
    the same — one decision there settles them all (Think again, "make all N …")."""
    size: collections.Counter = collections.Counter(style_key(f) for _, _, _, f in cands)
    rows = []
    for conf, _, i in confs:
        n, role, text, f = cands[i]
        if conf >= 0.6:
            continue
        same = size[style_key(f)] - 1
        rows.append(((1.0 - conf) * (1.0 + 0.5 * math.log2(1 + same)), n, role, text, conf, same))
    rows.sort(key=lambda t: -t[0])
    return [{"id": n.id, "text": text[:70], "page": n.page, "role": n.role, "confidence": round(conf, 2),
             "same_look": same, "changed": n.id in changed} for _, n, role, text, conf, same in rows[:k]]


def think(model, pdf_path: str, opts=None, ai_cfg=None, client: str = "", job: str = "",
          know=None, docinfo: dict | None = None, weights: dict | None = None, progress=None,
          use_samples: bool = True) -> dict:
    """Decide the text tags of a freshly analysed file (see the module doc).
    Returns what was decided; `model.info["brain"]` gets the same."""
    t0 = time.time()
    progress = progress or (lambda fr, m: None)
    strength = getattr(opts, "brain_strength", "balanced") if opts is not None else "balanced"
    need, agree_min = STRENGTH.get(strength, STRENGTH["balanced"])
    ai_max = int(getattr(opts, "ai_brain_max", 45) if opts is not None else 45)
    scope = getattr(opts, "samples_scope", "client") if opts is not None else "client"
    protect = bool(model.info.get("teachers_guide")) or \
        (opts is not None and (getattr(opts, "house_style", "none") or "none").lower() not in ("", "none", "off"))
    pages = L._Pages(pdf_path, body=model.body_size or None)
    out = {"version": VERSION, "client": client, "scope": scope, "strength": strength, "candidates": 0,
           "changed": 0, "ai_asked": 0, "ai_changed": 0, "unsure": 0, "quality": None, "protected": protect,
           "sources": {}, "changes": [], "learned": False}
    try:
        if docinfo is None:
            if scope == "all":
                weights, docinfo = doc_weights(pdf_path, pages)
            else:
                docinfo = {"doc_type": digest_pages(pages)["type"], "similar": []}
        out["doc_type"] = docinfo.get("doc_type")
        out["similar"] = docinfo.get("similar", [])[:3]
        ctx = Context(client, scope=scope, weights=weights, doc_type=out["doc_type"], know=know,
                      samples=use_samples)
        if scope == "all":
            # a client's own learned code is used for the files most like its samples
            ctx.rule_clients |= {s_["client"] for s_ in docinfo.get("similar", []) if float(s_.get("sim") or 0) >= 0.75}
        out["learned"] = ctx.learned
        if ctx.model_meta:
            out["model"] = {"id": ctx.model_meta["id"], "accuracy": ctx.model_meta.get("accuracy"),
                            "examples": ctx.model_meta.get("n_train")}
        cands = []
        decisions = []
        changed: set = set()
        art_rules = [r for r in ctx.rules if r["target"] == "Artifact" and r["precision"] >= 0.95] if not protect else []
        out["code_rules"] = len(ctx.rules)
        out["code_fired"] = 0
        for n, role, text in L._candidates(model):
            f = L.measure(pages, n.page, n.bbox, text, n.style if (n.style or {}).get("size") else None, pages.body)
            if f is None:
                continue
            if art_rules and role == "P":
                # learned code: a running head / foot people keep making an artifact
                from . import codegen
                fd = codegen.features(f, text, ctx.client, out["doc_type"], role)
                hit = codegen.opinions_for(art_rules, fd, ctx.rule_clients)
                if hit:
                    r0 = hit[0]
                    for c_ in list(n.children):
                        if c_ in model.nodes:
                            model._drop_subtree(c_)
                    n.children = []
                    n.role, n.artifact = "Artifact", True
                    yb = float(f.get("y") or 0)
                    n.artifact_subtype = "Pagination" if (yb <= 0.15 or yb >= 0.85) else "Layout"
                    n.pagination_subtype = "Header" if yb <= 0.15 else ("Footer" if yb >= 0.85 else "")
                    n.note = (n.note + "; " if n.note else "") + f"learned code {r0['id']}: {r0['description'][:80]}"
                    changed.add(n.id)
                    out["changes"].append({"id": n.id, "from": role, "to": "Artifact", "why": f"learned code {r0['id']}"})
                    out["code_fired"] += 1
                    decisions.append({"i": -1, "node": n.id, "text": text[:160], "proposed": role, "final": "Artifact",
                                      "opinions": {"rules": [role, round(float(n.confidence or 1), 3)],
                                                   "code": ["Artifact", round(r0["precision"], 3), r0["id"]]},
                                      "chosen": "code", "share": 1.0, "score": {}})
                    continue
            cands.append((n, role, text, f))
        out["candidates"] = len(cands)
        progress(0.15, f"brain: {len(cands)} text tags")
        probs = None
        if ctx.model is not None and cands:
            try:
                probs = ctx.model.predict([dense(f) for _, _, _, f in cands],
                                          [tokens(f, t, client, out["doc_type"]) for _, _, t, f in cands])
            except Exception as exc:
                out["model_error"] = str(exc)[:200]
                probs = None
        unsure = []
        confs = []
        # V4.27: first what each source says about every tag, then what the tags of the same look in
        # this file say to each other — one file, one convention
        pre = []
        for i, (n, role, text, f) in enumerate(cands):
            fired: list = []
            pre.append((opinions(ctx, role, n.confidence, text, f, probs[i] if probs is not None else None, fired),
                        fired))
        lay = layout_voices(ctx, cands, [p[0] for p in pre], need, agree_min) \
            if (ctx.learned and not protect) else [None] * len(cands)
        out["layout_voices"] = sum(1 for v in lay if v)
        shares: dict = {}
        for i, (n, role, text, f) in enumerate(cands):
            ops, fired = pre[i]
            lv = lay[i]
            if lv:
                ops["layout"] = lv["dist"]
            score, win, share, margin = combine(ctx, ops)
            learned_ops = {k: v for k, v in ops.items() if k != "rules"}
            agree = sum(1 for k, v in learned_ops.items()
                        if v and max(v, key=v.get) == win and (k != "layout" or lv["firm"]))
            final, chosen = role, "rules"
            if learned_ops and win and win != role and not protect:
                why_not = allowed(model, n, win, text, f, ops)
                strong_single = any(k in ("wording", "style", "code") and max(v.values()) >= 0.9
                                    and max(v, key=v.get) == win for k, v in learned_ops.items())
                if not why_not and share >= need and (agree >= agree_min or (agree >= 1 and strong_single)):
                    srcs = sorted(k for k, v in learned_ops.items() if max(v, key=v.get) == win)
                    if _retag(model, n, win, f"brain: {round(share * 100)} % — " + ", ".join(srcs), out, changed):
                        final, chosen = win, "+".join(srcs)
                        shares[n.id] = share
                elif not why_not and share >= 0.3:
                    unsure.append(i)
            elif learned_ops and win == role and share < 0.55:
                unsure.append(i)
            elif not learned_ops and n.confidence < 0.6:
                unsure.append(i)
            conf = score.get(final, 0.0) / (sum(score.values()) or 1.0) if learned_ops else float(n.confidence or 1.0)
            confs.append([conf, 1.0 + math.log1p(len(text.split())), i])
            if learned_ops:
                o_ = {k: [max(v, key=v.get), round(max(v.values()), 3)] for k, v in ops.items()}
                if fired and "code" in o_:
                    o_["code"].append(fired[0]["id"])
                    if final == fired[0]["target"] and final != role:
                        out["code_fired"] += 1
                decisions.append({"i": i, "node": n.id, "text": text[:160], "proposed": role, "final": final,
                                  "opinions": o_, "chosen": chosen, "share": round(share, 3), "score": score})
        # what stays doubtful: the local AI, with the rules and the nearest examples
        ai_on = ai_cfg is not None and getattr(ai_cfg, "enabled", False) and getattr(opts, "ai_brain", True)
        if ai_on and unsure and not protect:
            unsure = [i for i in unsure if cands[i][0].id not in changed][:max(0, ai_max)]
            progress(0.5, f"brain: asking the AI about {len(unsure)} tag(s)")
            try:
                out.update(_ask_ai(model, ctx, cands, unsure, probs, ai_cfg, client, weights, decisions, changed,
                                   out, confs))
            except Exception as exc:
                out["ai_error"] = str(exc)[:300]
        # tags the brain is still unsure of are outlined for review
        for conf, _, i in confs:
            n = cands[i][0]
            if ctx.learned and conf < 0.45 and n.id not in changed and n.id in model.nodes:
                n.confidence = round(min(n.confidence, conf), 2)
                out["unsure"] += 1
        out["levels_unified"] = _unify_levels(model, cands, shares, out) if shares else 0
        _fix_levels(model, changed)
        out["review_first"] = _review_first(cands, confs, changed)
        tw = sum(w for _, w, _ in confs) or 1.0
        out["quality"] = round(100.0 * sum(c_ * w for c_, w, _ in confs) / tw, 1) if confs else None
        out["changed"] = len(changed)
        out["sources"] = dict(collections.Counter(d["chosen"] for d in decisions if d["final"] != d["proposed"]))
        out["seconds"] = round(time.time() - t0, 1)
        out["changes"] = out["changes"][:300]
        if job:
            try:
                _record(job, client, decisions)
            except Exception as exc:
                out["record_error"] = str(exc)[:200]
    finally:
        pages.close()
    model.info["brain"] = {k: v for k, v in out.items()}
    progress(1.0, "brain: done")
    return out


def _ask_ai(model, ctx, cands, idxs, probs, ai_cfg, client, weights, decisions, changed, out, confs) -> dict:
    from .ai import OllamaError, brain_roles, embed, embedding_model
    res = {"ai_asked": 0, "ai_changed": 0}
    qembs = {}
    if ctx.emb_model:
        try:
            if embedding_model(ai_cfg) == ctx.emb_model:
                vecs = embed(ai_cfg, [cands[i][2][:200] for i in idxs], ctx.emb_model)
                qembs = {i: v for i, v in zip(idxs, vecs)}
        except OllamaError:
            qembs = {}
    rules = rules_for(client, weights)
    try:                                      # V4.26: the offline rule book, never the internet
        from .kb import for_roles
        rules = rules + for_roles(sorted({cands[i][1] for i in idxs} | {"H2", "Caption"}), k=3)
    except Exception:
        pass
    dec_by_i = {d["i"]: d for d in decisions if d["i"] >= 0}
    for k in range(0, len(idxs), 15):
        part = idxs[k:k + 15]
        blocks = []
        for j, i in enumerate(part, start=1):
            n, role, text, f = cands[i]
            d = dec_by_i.get(i)
            votes = []
            if d:
                tot = sum(d["score"].values()) or 1.0
                votes = [(r, s / tot) for r, s in sorted(d["score"].items(), key=lambda t: -t[1])[:3]]
            blocks.append({"i": j, "role": role, "text": text[:220], "size": f["size_rel"], "bold": f["bold"],
                           "font": f.get("font") or "", "page": n.page, "after": _prev_heading(model, n),
                           "votes": votes, "similar": ctx.similar(text, f, 3, qembs.get(i))})
        try:
            answers = brain_roles(ai_cfg, client or "every client", rules, blocks)
        except OllamaError as exc:
            res["ai_error"] = str(exc)[:300]
            break
        res["ai_asked"] += len(part)
        for a in answers:
            j = a["i"] - 1
            if not (0 <= j < len(part)):
                continue
            i = part[j]
            n, role, text, f = cands[i]
            conf = float(a.get("confidence") or 0.7)
            d = dec_by_i.get(i)
            if d is None:
                d = {"i": i, "node": n.id, "text": text[:160], "proposed": role, "final": role, "opinions":
                     {"rules": [role, round(float(n.confidence or 1.0), 3)]}, "chosen": "rules", "share": 0.0,
                     "score": {}}
                decisions.append(d)
                dec_by_i[i] = d
            d["opinions"]["ai"] = [a["role"], round(conf, 3)]
            if a["role"] != role and conf >= 0.55 and n.id not in changed and n.id in model.nodes \
                    and not allowed(model, n, a["role"], text, f):
                if _retag(model, n, a["role"], "brain + AI: " + (a.get("why") or "like the finished examples"),
                          out, changed):
                    d["final"], d["chosen"] = a["role"], "ai"
                    res["ai_changed"] += 1
            for row in confs:
                if row[2] == i:
                    row[0] = max(row[0], conf if a["role"] == d["final"] else row[0])
    return res


def _ago(days: int) -> str:
    from datetime import timedelta
    return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")


def _record(job: str, client: str, decisions: list[dict]) -> None:
    if not decisions:
        return
    with L.connect() as c:
        c.execute("DELETE FROM decisions WHERE job=?", (job,))
        # what is long learned, or never approved within a year, is not kept
        c.execute("DELETE FROM decisions WHERE (learned=1 AND created < ?) OR created < ?", (_ago(120), _ago(365)))
        c.executemany("INSERT INTO decisions (job, node, client, text, proposed, final, opinions, chosen, share, "
                      "learned, created) VALUES (?,?,?,?,?,?,?,?,?,0,?)",
                      [(job, d["node"], client, d["text"], d["proposed"], d["final"], json.dumps(d["opinions"]),
                        d["chosen"], d["share"], _now()) for d in decisions[:3000]])


# ================================================================ learning from the outcome
def _final_roles(model) -> dict:
    out = {}
    for n in model.nodes.values():
        out[n.id] = "Artifact" if n.artifact else ("H1" if n.role == "H?" else n.role)
    return out


def forget_trust(c: sqlite3.Connection, window: int = TRUST_WINDOW) -> int:
    """A source is judged by about its last `window` decisions, not by its whole life (V4.27).

    The trained model gets better with every file; the style map goes out of date
    when a client changes designer. If the counts only ever grew, a source that
    was wrong a thousand decisions ago would still be pulling its trust down long
    after it had learned better. When a count passes the window, old and new are
    scaled back together, so the share that was right stays and the next results
    weigh more. -> how many counts were scaled."""
    cur = c.execute("UPDATE trust SET ok = ok * (? * 1.0 / n), n = ? WHERE n > ?", (window, window, window))
    return cur.rowcount or 0


def learn_outcome(c: sqlite3.Connection, job: str, client: str, model, auto=None, when: str = "") -> dict:
    """An approved file: was each source right? And how much did the tool
    get right before people changed anything (the first-pass quality)?"""
    res = {"decisions": 0, "first_pass": None}
    if model is None:
        return res
    final = _final_roles(model)
    rows = list(c.execute("SELECT rowid, node, opinions FROM decisions WHERE job=? AND learned=0", (job,)))
    upd = collections.Counter()
    tot = collections.Counter()
    for r in rows:
        f = final.get(r["node"])
        if f is None:
            continue
        try:
            ops = json.loads(r["opinions"] or "{}")
        except ValueError:
            continue
        for src, v in ops.items():
            role = v[0]
            tot[(src, role)] += 1
            tot[(src, "*")] += 1
            if role == f:
                upd[(src, role)] += 1
                upd[(src, "*")] += 1
        res["decisions"] += 1
    for (src, role), n in tot.items():
        c.execute("INSERT INTO trust (source, role, ok, n) VALUES (?,?,?,?) ON CONFLICT(source, role) DO UPDATE SET "
                  "ok = ok + excluded.ok, n = n + excluded.n", (src, role, upd[(src, role)], n))
    forget_trust(c)
    c.execute("UPDATE decisions SET learned=1 WHERE job=?", (job,))
    try:                                      # V4.26: was each learned rule right in this approved file?
        from . import codegen
        ds = [{"node": r["node"], "opinions": json.loads(r["opinions"] or "{}")} for r in
              c.execute("SELECT node, opinions FROM decisions WHERE job=?", (job,))]
        codegen.ensure(c)
        rows = []
        for d in ds:
            v = d["opinions"].get("code")
            if v and len(v) >= 3 and d["node"] in final:
                rows.append((job, v[2], d["node"], "right" if final[d["node"]] == v[0] else "wrong", 1, _now()))
        c.execute("DELETE FROM rule_hits WHERE job=?", (job,))
        c.executemany("INSERT OR REPLACE INTO rule_hits VALUES (?,?,?,?,?,?)", rows)
        codegen.retire(c)
        res["rule_hits"] = len(rows)
    except Exception as exc:
        res["rule_hits_error"] = str(exc)[:200]
    if auto is not None:
        a = _final_roles(auto)
        same = total = 0
        for nid, r in final.items():
            if r not in L.TEXT_ROLES and r != "Artifact":
                continue
            if nid in a:
                total += 1
                same += 1 if a[nid] == r else 0
        bq = ((auto.info or {}).get("brain") or {}).get("quality")
        mid = c.execute("SELECT MAX(id) m FROM brain_models").fetchone()["m"]
        dt = ((auto.info or {}).get("brain") or {}).get("doc_type")
        c.execute("DELETE FROM history WHERE job=? AND kind='approved'", (job,))
        q = round(100.0 * same / total, 1) if total else None
        c.execute("INSERT INTO history VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (job, client, "approved", when or _now(), total, same, q, bq, mid, dt))
        res["first_pass"] = q
    return res


def record_feedback(job_path: str, client: str, model, auto, by: str = "") -> dict:
    """A built file: the tags people changed from the tool's proposal are
    remembered (they weigh less than an approved file's, until it is one)."""
    if auto is None or model is None:
        return {"feedback": 0}
    job = os.path.basename(os.path.normpath(job_path))
    work = os.path.join(job_path, "working.pdf")
    changed = []
    for n in model.nodes.values():
        a = auto.nodes.get(n.id)
        if a is None:
            continue
        was, now = L._role_of(a), L._role_of(n)
        if was == now or not ({was, now} & (L.TEXT_ROLES | {"Artifact"})):
            continue
        if any(model.nodes[c_].role in L.TEXT_ROLES for c_ in n.children if c_ in model.nodes):
            continue
        changed.append((n, was, now))
    rows = []
    if changed and os.path.exists(work):
        pages = L._Pages(work, body=model.body_size or None)
        try:
            for n, was, now in changed[:2000]:
                text, box = L._text_box(model, n)
                if not text.strip() or not box:
                    continue
                f = L.measure(pages, n.page, box, text, None)
                if f is None:
                    continue
                rows.append((job, client, was, now, text[:300], L.norm(text) if len(text.split()) <= 14 else "",
                             f.get("font") or "", *[f.get(k) for k in L.FEATURES], _now(), n.id, by))
        finally:
            pages.close()
    with L.connect() as c:
        c.execute("DELETE FROM feedback WHERE job=? OR created < ?", (job, _ago(540)))
        c.executemany("INSERT INTO feedback (job, client, from_role, role, text, norm, font, " + ", ".join(L.FEATURES)
                      + ", created, node, who) VALUES (" + ",".join("?" * (10 + len(L.FEATURES))) + ")", rows)
        auto_r, fin = _final_roles(auto), _final_roles(model)
        total = sum(1 for nid, r in fin.items() if (r in L.TEXT_ROLES or r == "Artifact") and nid in auto_r)
        same = sum(1 for nid, r in fin.items() if (r in L.TEXT_ROLES or r == "Artifact") and auto_r.get(nid) == r)
        bq = ((auto.info or {}).get("brain") or {}).get("quality")
        c.execute("DELETE FROM history WHERE job=? AND kind='built'", (job,))
        c.execute("INSERT INTO history VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (job, client, "built", _now(), total, same, round(100.0 * same / total, 1) if total else None, bq,
                   None, ((auto.info or {}).get("brain") or {}).get("doc_type")))
    hits = 0
    try:                                      # V4.26: did people undo what a learned rule did?
        from . import codegen
        with L.connect() as c:
            ds = [{"node": r["node"], "opinions": json.loads(r["opinions"] or "{}")} for r in
                  c.execute("SELECT node, opinions FROM decisions WHERE job=?", (job,))]
        hits = codegen.record_hits(job, ds, _final_roles(model), verified=False)
    except Exception:
        hits = 0
    return {"feedback": len(rows), "rule_hits": hits}


def similar_tags(model, pdf_path: str, nid: str, from_role: str = "") -> list[dict]:
    """The tags of this file that look like `nid` did before a person changed
    it: the same font, size, weight, capitals — and the same tag it had."""
    n = model.nodes.get(nid)
    if n is None or not n.bbox:
        return []
    pages = L._Pages(pdf_path, body=model.body_size or None)
    try:
        text = " ".join((n.text or "").split())
        f0 = L.measure(pages, n.page, n.bbox, text, n.style if (n.style or {}).get("size") else None, pages.body)
        if f0 is None:
            return []
        key = style_key(f0)
        want = from_role or ""
        out = []
        for m, role, t in L._candidates(model):
            if m.id == nid or (want and role != want) or m.role == n.role:
                continue
            f = L.measure(pages, m.page, m.bbox, t, m.style if (m.style or {}).get("size") else None, pages.body)
            if f is None or style_key(f) != key:
                continue
            if abs(int(f.get("words") or 0) - int(f0.get("words") or 0)) > max(6, int(f0.get("words") or 0)):
                continue
            if allowed(model, m, n.role, t, f) if n.role in SWAP else False:
                continue
            out.append({"id": m.id, "role": role, "text": t[:80], "page": m.page})
            if len(out) >= 300:
                break
        return out
    finally:
        pages.close()


# ================================================================ for the editor
_CTX: dict = {"key": None, "ctx": None, "at": 0.0}


def cached_context(client: str, scope: str, weights: dict | None, doc_type: str, ttl: float = 120.0) -> Context:
    """The context for the editor's popover (V4.27).

    The popover asks about one tag after another; building the context reads every sample's tags
    and memory, which takes a couple of seconds on a big database. It is kept while nothing was
    learned and the model is the same."""
    key = None
    try:
        with L.connect() as c:
            sig = data_signature(c)
        key = (client, scope, tuple(sorted((weights or {}).items())), doc_type, sig,
               (latest_model()[1] or {}).get("id"))
    except Exception:
        key = None
    if key is not None and _CTX["key"] == key and time.time() - _CTX["at"] < ttl:
        return _CTX["ctx"]
    ctx = Context(client, scope=scope, weights=weights, doc_type=doc_type)
    if key is not None:
        _CTX.update(key=key, ctx=ctx, at=time.time())
    return ctx


def explain(model, pdf_path: str, nid: str, client: str = "", scope: str = "client") -> dict:
    """What every source says about one tag (the editor's popover)."""
    n = model.nodes.get(nid)
    if n is None or not n.bbox or not L.has_data():
        return {}
    pages = L._Pages(pdf_path, body=model.body_size or None)
    try:
        text = " ".join((n.text or "").split())
        f = L.measure(pages, n.page, n.bbox, text, n.style if (n.style or {}).get("size") else None, pages.body)
        if f is None:
            return {}
        bi = model.info.get("brain") or {}
        weights = None
        if scope == "all":
            weights = {s["id"]: 1.0 for s in bi.get("similar") or []}
        ctx = cached_context(client, scope, weights, bi.get("doc_type", ""))
        probs = None
        if ctx.model is not None:
            probs = ctx.model.predict([dense(f)], [tokens(f, text, client, bi.get("doc_type", ""))])[0]
        role = "H1" if n.role == "H?" else n.role
        ops = opinions(ctx, role, n.confidence, text, f, probs)
        score, win, share, _ = combine(ctx, ops)
        tot = sum(score.values()) or 1.0
        return {"brain": [{"role": r, "share": round(s / tot, 3)} for r, s in
                          sorted(score.items(), key=lambda t: -t[1])[:4]],
                "sources": {k: [max(v, key=v.get), round(max(v.values()), 2)] for k, v in ops.items()},
                "similar": ctx.similar(text, f, 3), "winner": win}
    finally:
        pages.close()


# ================================================================ status for the admin
def status() -> dict:
    model, meta = latest_model(refresh=True)
    out = {"numpy": _np() is not None, "model": None, "trainings": [], "trust": [], "history": [], "insights": [],
           "doc_types": {}, "memory": 0, "embedded": 0, "feedback": 0, "decisions": 0, "styles": 0, "curve": {}}
    if not L.has_data():
        return out
    with L.connect() as c:
        if meta:
            out["model"] = {k: meta.get(k) for k in ("id", "created", "n_train", "n_samples", "classes", "accuracy",
                                                      "macro_f1", "baseline", "per_role", "trigger", "seconds",
                                                      "logloss", "ece", "temperature", "folds")}
        out["trainings"] = [dict(r) for r in c.execute(
            "SELECT " + ", ".join(k for k in ("id", "created", "n_train", "n_samples", "accuracy", "macro_f1", "baseline",
                                              "trigger", "seconds", "logloss", "ece", "temperature", "folds")
                                  if k in _model_cols(c).split(", ")) + " FROM brain_models ORDER BY id")]
        tr = collections.defaultdict(dict)
        for r in c.execute("SELECT source, role, ok, n FROM trust ORDER BY source, n DESC"):
            tr[r["source"]][r["role"]] = {"ok": r["ok"], "n": r["n"]}
        ctx = Context.__new__(Context)
        ctx.trust = {(s, r): (v["ok"], v["n"]) for s, d in tr.items() for r, v in d.items()}
        for src in SOURCES:
            d = tr.get(src, {})
            al = d.get("*", {"ok": 0, "n": 0})
            out["trust"].append({"source": src, "ok": round(al["ok"], 1), "n": round(al["n"]),
                                 "right": round(al["ok"] / al["n"], 3) if al["n"] else None,
                                 "weight": round(BASE_WEIGHT[src] * ctx.trust_factor(src, "*"), 2),
                                 "roles": {r: v for r, v in d.items() if r != "*"}})
        out["history"] = [dict(r) for r in c.execute(
            "SELECT job, client, kind, created, tags, same, quality, brain_quality, model_id, doc_type FROM history "
            "ORDER BY created DESC LIMIT 200")]
        appr = [h for h in reversed(out["history"]) if h["kind"] == "approved" and h["quality"] is not None]
        if appr:
            k = max(1, len(appr) // 3)
            first, last = appr[:k], appr[-k:]
            out["curve"] = {"first": round(sum(h["quality"] for h in first) / len(first), 1),
                            "last": round(sum(h["quality"] for h in last) / len(last), 1), "files": len(appr)}
        for r in c.execute("SELECT i.sample_id, i.client, i.doc_type, i.summary, i.rules, i.model, i.created, i.error, "
                           "s.filename FROM insights i LEFT JOIN samples s ON s.id=i.sample_id ORDER BY i.created DESC "
                           "LIMIT 60"):
            d = dict(r)
            try:
                d["rules"] = json.loads(d.get("rules") or "[]")
            except ValueError:
                d["rules"] = []
            out["insights"].append(d)
        out["doc_types"] = {r["doc_type"] or "?": r["n"] for r in c.execute(
            "SELECT d.doc_type, COUNT(*) n FROM docs d JOIN samples s ON s.id=d.sample_id WHERE s.enabled=1 "
            "GROUP BY d.doc_type ORDER BY n DESC")}
        out["memory"] = c.execute("SELECT COUNT(*) n FROM memory").fetchone()["n"]
        out["embedded"] = c.execute("SELECT COUNT(*) n FROM memory WHERE emb IS NOT NULL").fetchone()["n"]
        out["feedback"] = c.execute("SELECT COUNT(*) n FROM feedback").fetchone()["n"]
        out["corrections"] = c.execute("SELECT COUNT(*) n FROM corrections c JOIN samples s ON s.id=c.sample_id "
                                       "WHERE s.enabled=1").fetchone()["n"]
        out["feedback_files"] = c.execute("SELECT COUNT(DISTINCT job) n FROM feedback").fetchone()["n"]
        out["decisions"] = c.execute("SELECT COUNT(*) n FROM decisions").fetchone()["n"]
        st = collections.defaultdict(collections.Counter)
        for b in c.execute("SELECT b.role, b.font, b.size_rel, b.bold, b.italic, b.caps FROM blocks b JOIN samples s "
                           "ON s.id=b.sample_id WHERE s.enabled=1 AND b.font IS NOT NULL AND b.font != '' LIMIT 80000"):
            st[style_key(dict(b))][b["role"]] += 1
        out["styles"] = len(st)
        rows = []
        for k, cnt in st.items():
            tot = sum(cnt.values())
            role, n = cnt.most_common(1)[0]
            if role != "P" and tot >= 3:
                f, s_, b, i, cp = k.split("|")
                rows.append({"font": f, "size": float(s_), "bold": b == "1", "italic": i == "1", "caps": cp == "1",
                             "role": role, "share": round(n / tot, 2), "n": tot})
        rows.sort(key=lambda r: (-r["n"]))
        out["style_map"] = rows[:24]
        out["stale"] = False
        if meta and meta.get("id"):
            r = c.execute("SELECT signature FROM brain_models WHERE id=?", (meta["id"],)).fetchone()
            out["stale"] = bool(r is not None and r["signature"] != data_signature(c))
    return out


def needs_relearn() -> list[str]:
    """Samples learned before V4.25 (no document, no memory): read again once."""
    if not L.has_data():
        return []
    with L.connect() as c:
        return [r["id"] for r in c.execute(
            "SELECT id FROM samples WHERE status='ready' AND id NOT IN (SELECT sample_id FROM docs)")]


def ai_from_config():
    """The AI settings of this installation (for the learner process)."""
    from .ai import AIConfig
    path = os.environ.get("PDFUA_CONFIG") or os.path.join(ROOT, "config.json")
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        return None, {}
    lc = {"enabled": True, "use_ai": True, "ai_study": True, **(cfg.get("learning") or {})}
    ai = cfg.get("ai") or {}
    if not ai.get("enabled", True) or not lc.get("use_ai", True):
        return None, lc
    try:
        return AIConfig(**{k: v for k, v in ai.items() if k in AIConfig().__dict__}), lc
    except Exception:
        return None, lc


def study_pending(limit: int = 50) -> dict:
    """AI study (and embeddings) for the samples that have none yet."""
    cfg, lc = ai_from_config()
    if cfg is None or not lc.get("ai_study", True):
        return {"studied": 0, "why": "AI off"}
    done = emb = 0
    with L.connect() as c:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM samples WHERE status='ready' AND enabled=1 AND id NOT IN "
            "(SELECT sample_id FROM insights WHERE (error IS NULL OR error='') AND rules IS NOT NULL) "
            "ORDER BY created DESC LIMIT ?", (limit,))]
    for sid in ids:
        r = study_sample(sid, cfg)
        if not r.get("ok") and "reach" in str(r.get("error", "")):
            break                                  # the server is not there: try another time
        done += 1 if r.get("ok") else 0
        e = embed_memory(sid, cfg)
        emb += int(e.get("embedded") or 0)
    return {"studied": done, "embedded": emb}


# ================================================================ worker
def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    try:
        from . import netguard                     # V4.26: nothing leaves the server
        netguard.install(process="brain")
    except Exception:
        pass
    if cmd == "train":
        r = train_all(trigger="command" if "--force" in argv else "after learning", force="--force" in argv)
    elif cmd == "code":
        from . import codegen
        cfg, lc = ai_from_config()
        r = codegen.synthesize(ai_cfg=cfg if lc.get("ai_study", True) else None)
    elif cmd == "study":
        r = study_pending(limit=10000 if "--all" in argv else 50)
    elif cmd == "status":
        s = status()
        r = {k: s[k] for k in ("model", "memory", "embedded", "feedback", "decisions", "styles", "doc_types", "curve")}
    else:
        print("usage: python -m core.brain train [--force] | study [--all] | status", file=sys.stderr)
        return 2
    print(json.dumps(r, indent=1, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, ROOT)
    sys.exit(main(sys.argv[1:]))
