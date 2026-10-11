"""Ollama teaches Dez AI (V4.31).

Two AIs work in this tool and they are not the same thing:

* **Dez AI** is the studio's own AI — the part that lives in this program and learns: the brain
  (core/brain.py), the memory (core/memory.py), the learned code (core/codegen.py) and the zone models
  (core/zonelearn.py). It needs no model server; it works offline, in milliseconds.
* **Ollama** is the teacher: a large language / vision model on a server. It can answer almost any question,
  but it is slow (seconds to minutes on a CPU) and it forgets the answer the moment it has given it.

This module makes the teacher help the student, so Dez AI becomes more and more able to work alone.

**Every question the tool puts to Ollama goes through a gate** (`core/ai.py` wraps its task functions with
`gated`). The gate

1. asks Dez AI first — has it been taught this? A question it has been asked before is answered from its
   lessons (exact memory); a *new* question of a kind it has learned well is answered by a small trained
   model of its own (the *student*) — but only when that student is *proven*: its answers agree with Ollama's
   on questions it had not seen (cross-validation over the lessons), and they keep agreeing in the audits
   below. Nothing answers alone on trust;
2. otherwise asks Ollama, as before — and **keeps the answer as a lesson** (the question in a compact form,
   the answer, who gave it, which model, which file). The next question of that kind may not need Ollama;
3. now and then (1 in `audit`, default 12) asks Ollama a question Dez AI could have answered alone, and
   compares: that is the honest measure of the student ("agrees with Ollama 97 % of the time"). A student
   whose agreement drops is taken off duty by itself until it has learned again.

A person's answer outweighs the teacher's: what people leave in a built or approved file (a table header
row, a link's description, a table summary) becomes a *human lesson* (`learn_job`), a lesson Ollama gave and
a person changed is marked *corrected* and is never used again. The tags Ollama proposes for the brain's
doubtful text blocks are kept with their layout features (`roles_taught`) and train the brain's own model
with a low weight until a person has had the say.

Each task has a **level** — `watch` (only records), `recall` (answers what it has been asked before), `solo`
(also answers new questions with its student) — chosen from the evidence, or fixed by an admin
(Admin → Dez AI → *Ollama teaches Dez AI*). With Ollama switched off or out of reach, Dez AI still
answers what it learned; what it has not learned stays unanswered, as before.

Everything is kept in the sample database (`sample_files/samples.db`): lessons, daily counts, audits,
students. Nothing leaves the server.
"""
from __future__ import annotations

import atexit
import collections
import contextlib
import functools
import hashlib
import json
import math
import os
import random
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta

from . import learn as L

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEVELS = ("off", "watch", "recall", "solo")
DEFAULTS = {
    "teach": True,        # keep Ollama's answers as lessons
    "solo": True,         # Dez AI answers by itself what it has learned (recall, and its students once proven)
    "offline": True,      # …also when Ollama is switched off or cannot be reached
    "fewshot": True,      # show Ollama how people here worded similar things (their approved answers) in its question
    "audit": 12,          # 1 in N of the questions Dez AI could answer alone is put to Ollama too (0 = never)
    "agree": 0.95,        # how often a student must agree with Ollama on questions it had not seen
    "sure": 0.90,         # how sure a student must be of one answer to give it
    "min_lessons": 30,    # lessons a student needs before it may be proven
    "levels": {},         # task -> "auto" | "off" | "watch" | "recall" | "solo"
}
LESSON_CAP = 20000        # per task; the oldest unconfirmed ones go first
RULE_MIN = 8              # lessons a rule needs behind it
FEATURES_VERSION = 1      # change when a student's features change: older students are trained again
Q_BYTES = 12000           # a stored question is cut here
GUESS_SECS = {"vision": 20.0, "text": 6.0}      # until the tool has timed Ollama itself


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _day() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ================================================================ settings
_SET = {"at": 0.0, "mtime": None, "val": None, "path": None}


def config_path() -> str:
    return os.environ.get("PDFUA_CONFIG") or os.path.join(ROOT, "config.json")


def settings(fresh: bool = False) -> dict:
    """The admin's choices (config.json → "teacher") over the defaults; read at most every 5 seconds per process."""
    p = config_path()
    now = time.time()
    if not fresh and _SET["val"] is not None and _SET["path"] == p and now - _SET["at"] < 5.0:
        return _SET["val"]
    raw: dict = {}
    try:
        with open(p, encoding="utf-8") as fh:
            raw = (json.load(fh) or {}).get("teacher") or {}
    except Exception:
        raw = {}
    out = {**DEFAULTS, **{k: raw[k] for k in DEFAULTS if k in raw}}
    out["levels"] = {t: v for t, v in (out.get("levels") or {}).items() if v in LEVELS + ("auto",)}
    for k, lo, hi in (("audit", 0, 1000), ("min_lessons", 5, 100000)):
        try:
            out[k] = int(max(lo, min(hi, int(out[k]))))
        except (TypeError, ValueError):
            out[k] = DEFAULTS[k]
    for k, lo, hi in (("agree", 0.5, 1.0), ("sure", 0.5, 0.999)):
        try:
            out[k] = float(max(lo, min(hi, float(out[k]))))
        except (TypeError, ValueError):
            out[k] = DEFAULTS[k]
    for k in ("teach", "solo", "offline", "fewshot"):
        out[k] = bool(out[k])
    _SET.update(at=now, val=out, path=p)
    return out


def clean_settings(change: dict, current: dict | None = None) -> dict:
    """`current` with the admin's `change` applied and checked (unknown keys are ignored)."""
    cur = dict(current or {})
    for k in ("teach", "solo", "offline", "fewshot"):
        if k in change:
            cur[k] = bool(change[k])
    if "audit" in change:
        cur["audit"] = int(max(0, min(1000, int(change["audit"]))))
    if "min_lessons" in change:
        cur["min_lessons"] = int(max(5, min(100000, int(change["min_lessons"]))))
    for k, lo, hi in (("agree", 0.5, 1.0), ("sure", 0.5, 0.999)):
        if k in change:
            v = float(change[k])
            cur[k] = round(max(lo, min(hi, v / 100.0 if v > 1.0 else v)), 4)
    if isinstance(change.get("levels"), dict):
        lv = dict(cur.get("levels") or {})
        for t, v in change["levels"].items():
            if t not in SPECS:
                continue
            if v in (None, "", "auto"):
                lv.pop(t, None)
            elif v in LEVELS:
                lv[t] = v
        cur["levels"] = lv
    return cur


# ================================================================ the database
SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT NOT NULL, qkey TEXT NOT NULL, ahash TEXT NOT NULL,
  q TEXT, a TEXT, label TEXT DEFAULT '', why TEXT DEFAULT '', source TEXT DEFAULT 'ollama', model TEXT DEFAULT '',
  client TEXT DEFAULT '', job TEXT DEFAULT '', weight REAL DEFAULT 1.0, verdict INTEGER DEFAULT 0,
  seen INTEGER DEFAULT 1, created TEXT, updated TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS lessons_uni ON lessons(task, qkey, source, ahash);
CREATE INDEX IF NOT EXISTS lessons_job ON lessons(job);
CREATE INDEX IF NOT EXISTS lessons_task ON lessons(task, id);
CREATE TABLE IF NOT EXISTS teach_days (
  day TEXT, task TEXT, dez INTEGER DEFAULT 0, ollama INTEGER DEFAULT 0, idle INTEGER DEFAULT 0,
  error INTEGER DEFAULT 0, secs REAL DEFAULT 0, saved REAL DEFAULT 0, PRIMARY KEY (day, task));
CREATE TABLE IF NOT EXISTS teach_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT, day TEXT, agree INTEGER, dez TEXT, ollama TEXT, conf REAL,
  created TEXT);
CREATE INDEX IF NOT EXISTS teach_audit_task ON teach_audit(task, id);
CREATE TABLE IF NOT EXISTS students (
  task TEXT PRIMARY KEY, trained TEXT, n INTEGER, acc REAL, cov REAL, level TEXT, info TEXT, blob BLOB);
CREATE TABLE IF NOT EXISTS teacher_roles (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, node TEXT, client TEXT, doc_type TEXT, proposed TEXT, role TEXT,
  conf REAL, accepted INTEGER, text TEXT, norm TEXT, font TEXT, why TEXT,
  size_rel REAL, bold INTEGER, italic INTEGER, lines INTEGER, words INTEGER, x REAL, y REAL, w REAL,
  centered INTEGER, starts_num INTEGER, caps REAL, ends_stop INTEGER, first_page INTEGER, cap_word INTEGER,
  verdict INTEGER DEFAULT 0, created TEXT);
CREATE INDEX IF NOT EXISTS teacher_roles_job ON teacher_roles(job);
"""
_READY: set = set()


def ensure(c: sqlite3.Connection) -> None:
    c.executescript(SCHEMA)


def _conn(timeout: float = 5.0) -> sqlite3.Connection:
    """A connection of its own (closed by the caller). Short waits: a lesson is a luxury, never worth a stall."""
    os.makedirs(L.samples_dir(), exist_ok=True)
    p = L.db_path()
    c = sqlite3.connect(p, timeout=timeout)
    c.row_factory = sqlite3.Row
    if p not in _READY:
        try:
            c.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        ensure(c)
        _READY.add(p)
    return c


@contextlib.contextmanager
def _db(timeout: float = 5.0):
    c = _conn(timeout)
    try:
        yield c
        c.commit()
    except BaseException:
        try:
            c.rollback()
        except Exception:
            pass
        raise
    finally:
        c.close()


# ================================================================ small helpers
def nt(s) -> str:
    """Text for a key: lower case, one space between words."""
    return " ".join(str(s or "").lower().split())


def key_of(*parts) -> str:
    return hashlib.sha1("\x1f".join(nt(p) for p in parts).encode("utf-8", "replace")).hexdigest()[:20]


def _jd(o) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"), default=str)


def _ahash(a_json: str) -> str:
    return hashlib.sha1(a_json.encode("utf-8", "replace")).hexdigest()[:16]


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z#]+", re.sub(r"\d", "#", nt(s)))


def _shape(tok: str) -> str:
    s = re.sub(r"[A-Z]", "A", tok or "")
    s = re.sub(r"[a-z]", "a", s)
    s = re.sub(r"\d", "9", s)
    s = re.sub(r"(.)\1+", r"\1", s)
    return s[:6]


def _jaccard(a: list[str], b: list[str]) -> float:
    A, B = set(a), set(b)
    return len(A & B) / len(A | B) if (A | B) else 0.0


NUMERIC = re.compile(r"^[\s$€£¥%()+\-–−.,:/\d]*\d[\s$€£¥%()+\-–−.,:/\d]*$")


def phash(png: bytes) -> tuple[str, str, float] | None:
    """A look-alike fingerprint of a picture: 128 bits of brightness steps (horizontal and vertical) of a small grey
    copy, a 4×4 grid of how bright each part is, and width / height. The same picture at another size gets the same
    fingerprint (give or take a bit); a flat picture (a block of one colour) has nothing to tell it from the next and
    gets none."""
    try:
        import numpy as np
        import pymupdf
        pix = pymupdf.Pixmap(png)
        if pix.alpha:
            pix = pymupdf.Pixmap(pix, 0)
        if pix.colorspace is not None and pix.n != 1:
            pix = pymupdf.Pixmap(pymupdf.csGRAY, pix)
        w, h = pix.width, pix.height
        if w < 8 or h < 8:
            return None
        a = np.frombuffer(pix.samples, dtype=np.uint8)[: w * h].reshape(h, w).astype(np.float32)
        if float(a.std()) < 6.0:
            return None

        def small(th, tw):
            ys = np.linspace(0, h, th + 1).astype(int)
            xs = np.linspace(0, w, tw + 1).astype(int)
            ys[1:] = np.maximum(ys[1:], ys[:-1] + 1)
            xs[1:] = np.maximum(xs[1:], xs[:-1] + 1)
            ys, xs = np.minimum(ys, h), np.minimum(xs, w)
            r = np.add.reduceat(a, ys[:-1], axis=0) / np.maximum(np.diff(ys), 1)[:, None]
            return np.add.reduceat(r, xs[:-1], axis=1) / np.maximum(np.diff(xs), 1)[None, :]
        hb = (lambda s_: (s_[:, 1:] > s_[:, :-1]).flatten())(small(8, 9))
        vb = (lambda s_: (s_[1:, :] > s_[:-1, :]).flatten())(small(9, 8))
        v = 0
        for b in np.concatenate([hb, vb]):
            v = (v << 1) | int(b)
        g = small(4, 4).flatten()
        grid = "".join(str(min(3, int(x // 64))) for x in g)
        return f"{v:032x}", grid, round(w / max(h, 1), 3)
    except Exception:
        return None


def _hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


# ================================================================ teaching context (who asked, for whom)
_CTX = threading.local()
_GLOBAL_CTX: dict = {}
_RESOLVER: list = [None]


def set_resolver(fn) -> None:
    """fn(job id) -> the client the file belongs to (looked up only when a lesson is kept)."""
    _RESOLVER[0] = fn


def bind(job: str = "", client: str | None = None) -> None:
    """The lessons kept by this thread from now on belong to this file (a web request binds its file)."""
    _CTX.v = {"job": job or "", "client": client}


def unbind() -> None:
    _CTX.v = None


@contextlib.contextmanager
def context(job: str = "", client: str = ""):
    old = getattr(_CTX, "v", None)
    _CTX.v = {"job": job or "", "client": client or ""}
    try:
        yield
    finally:
        _CTX.v = old


def set_context(job: str = "", client: str = "") -> None:
    """For a process that works on one file only (the job runner); the processes it starts (page-parallel analysis) inherit it."""
    _GLOBAL_CTX.update(job=job or "", client=client or "")
    os.environ["PDFUA_TEACH_JOB"], os.environ["PDFUA_TEACH_CLIENT"] = job or "", client or ""


def _who() -> tuple[str, str]:
    v = getattr(_CTX, "v", None)
    if v is None:
        v = _GLOBAL_CTX
        if not v and os.environ.get("PDFUA_TEACH_JOB"):             # a process started by one that works on a file
            v.update(job=os.environ["PDFUA_TEACH_JOB"], client=os.environ.get("PDFUA_TEACH_CLIENT", ""))
    job, client = v.get("job", ""), v.get("client")
    if client is None:
        client = ""
        if job and _RESOLVER[0] is not None:
            try:
                client = _RESOLVER[0](job) or ""
            except Exception:
                client = ""
        v["client"] = client
    return job, client


_TALLY = threading.local()


@contextlib.contextmanager
def tally():
    """`with tally() as t:` — who answered the questions this thread asked meanwhile: {'dez': n, 'ollama': n, 'idle': n}."""
    t = {"dez": 0, "ollama": 0, "idle": 0}
    old = getattr(_TALLY, "t", None)
    _TALLY.t = t
    try:
        yield t
    finally:
        _TALLY.t = old


# ================================================================ counts per day (buffered)
_COUNTS: dict = {}
_COUNT_LOCK = threading.Lock()
_COUNT_AT = [time.time()]
_SECS_CACHE: dict = {"at": 0.0, "val": {}}


def _count(task: str, src: str, secs: float = 0.0, saved: float = 0.0, n: int = 1) -> None:
    """src: dez (answered by Dez AI) · ollama · idle (nobody to answer) · error."""
    k = (_day(), task)
    t = getattr(_TALLY, "t", None)
    if t is not None and src in t:
        t[src] += n
    with _COUNT_LOCK:
        e = _COUNTS.setdefault(k, {"dez": 0, "ollama": 0, "idle": 0, "error": 0, "secs": 0.0, "saved": 0.0})
        e[src] = e.get(src, 0) + n
        e["secs"] += secs
        e["saved"] += saved
        due = time.time() - _COUNT_AT[0] > 3.0 or len(_COUNTS) > 40
    if due:
        flush_counts()


def flush_counts() -> None:
    with _COUNT_LOCK:
        todo, _COUNTS_copy = dict(_COUNTS), None
        _COUNTS.clear()
        _COUNT_AT[0] = time.time()
    if not todo:
        return
    try:
        with _db(timeout=3.0) as c:
            for (day, task), e in todo.items():
                c.execute("INSERT INTO teach_days (day, task, dez, ollama, idle, error, secs, saved) VALUES (?,?,?,?,?,?,?,?) "
                          "ON CONFLICT(day, task) DO UPDATE SET dez=dez+excluded.dez, ollama=ollama+excluded.ollama, "
                          "idle=idle+excluded.idle, error=error+excluded.error, secs=secs+excluded.secs, "
                          "saved=saved+excluded.saved",
                          (day, task, e["dez"], e["ollama"], e["idle"], e["error"], round(e["secs"], 2), round(e["saved"], 2)))
    except Exception:
        with _COUNT_LOCK:                      # try again with the next batch
            for k, e in todo.items():
                n = _COUNTS.setdefault(k, {"dez": 0, "ollama": 0, "idle": 0, "error": 0, "secs": 0.0, "saved": 0.0})
                for f in n:
                    n[f] += e.get(f, 0)


atexit.register(flush_counts)


def avg_secs(task: str) -> float:
    """What one question to Ollama of this kind costs (measured here; a guess until then)."""
    now = time.time()
    if now - _SECS_CACHE["at"] > 60.0:
        val: dict = {}
        try:
            with _db(timeout=2.0) as c:
                for r in c.execute("SELECT task, SUM(secs) s, SUM(ollama) n FROM teach_days GROUP BY task"):
                    if r["n"] and r["s"]:
                        val[r["task"]] = float(r["s"]) / float(r["n"])
        except Exception:
            val = _SECS_CACHE["val"]
        _SECS_CACHE.update(at=now, val=val)
    got = _SECS_CACHE["val"].get(task)
    if got:
        return got
    return GUESS_SECS.get(SPECS[task].kind_of_model if task in SPECS else "text", 6.0)


# ================================================================ lessons
def record(task: str, qkey: str, q: dict, answer, label: str = "", why: str = "", source: str = "ollama",
           model: str = "", weight: float = 1.0, verdict: int = 0, job: str | None = None,
           client: str | None = None) -> None:
    """Keep one lesson: this question was answered like this by this teacher (or person)."""
    j, c_ = _who()
    job = j if job is None else job
    client = c_ if client is None else client
    a = _jd(answer)
    qj = _jd(q)
    if len(qj) > Q_BYTES:
        qj = _jd({"cut": 1})                    # too big to keep (it is still found by its key)
    now = _now()
    try:
        with _db(timeout=3.0) as c:
            c.execute("INSERT INTO lessons (task, qkey, ahash, q, a, label, why, source, model, client, job, weight, "
                      "verdict, seen, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?) "
                      "ON CONFLICT(task, qkey, source, ahash) DO UPDATE SET seen=seen+1, updated=excluded.updated, "
                      "model=CASE WHEN excluded.model!='' THEN excluded.model ELSE model END, "
                      "job=CASE WHEN excluded.job!='' THEN excluded.job ELSE job END",
                      (task, qkey, _ahash(a), qj, a, label, (why or "")[:200], source, model, client or "", job or "",
                       weight, verdict, now, now))
    except Exception:
        pass


def _score_rows(rows) -> tuple[str, float, int, str] | None:
    """The answer the lessons favour: (answer json, share of the weight it has, lessons behind it, who) or None.
    A person's answer outweighs the teacher's; one the teacher gave and a person confirmed outweighs the rest."""
    tot: dict = collections.defaultdict(float)
    who: dict = {}
    n: dict = collections.Counter()
    for r in rows:
        if int(r["verdict"] or 0) < 0:
            continue
        w = float(r["weight"] or 1.0) * (4.0 if r["source"] == "human" else 1.0) * (2.0 if int(r["verdict"] or 0) > 0 else 1.0)
        w *= 1.0 + math.log1p(max(0, int(r["seen"] or 1) - 1)) * 0.25
        tot[r["a"]] += w
        n[r["a"]] += int(r["seen"] or 1)
        who[r["a"]] = "human" if (r["source"] == "human" or who.get(r["a"]) == "human") else (who.get(r["a"]) or r["source"])
    if not tot:
        return None
    best = max(tot, key=lambda k: tot[k])
    return best, tot[best] / sum(tot.values()), n[best], who[best]


def recall_exact(task: str, qkey: str):
    """The favoured lesson for exactly this question: (answer json, share, count, who) or None."""
    try:
        with _db(timeout=2.0) as c:
            rows = c.execute("SELECT a, source, verdict, weight, seen FROM lessons WHERE task=? AND qkey=?",
                             (task, qkey)).fetchall()
    except Exception:
        return None
    return _score_rows(rows)


# ================================================================ the tasks (what each question looks like)
class Own:
    """What Dez AI itself says to a question."""
    __slots__ = ("answer", "conf", "how", "n", "who")

    def __init__(self, answer, conf: float, how: str, n: int = 1, who: str = ""):
        self.answer, self.conf, self.how, self.n, self.who = answer, conf, how, n, who


class Spec:
    """One kind of question. `kind`: recall (exact memory) · picture (memory by the look of a picture) · class (a
    trained yes/no or choice student) · lines (a student for lists of lines) · rule (a rule found in the lessons) ·
    watch (only recorded)."""
    name = ""
    label = ""
    kind = "recall"
    kind_of_model = "text"         # which kind of model answers (for the time-saved guess)
    help = ""

    # ---- the question
    def ask(self, *a, **kw):                       # -> {"key": str, "q": dict, ...} or None
        raise NotImplementedError

    # ---- the answer
    def to_json(self, ans) -> tuple[object, str] | None:
        """(JSON of the answer, class label) — None when the answer is not worth a lesson."""
        if ans is None or ans == "" or ans == []:
            return None
        return ans, ""

    def from_json(self, js):
        return js

    def empty(self):
        return None

    def same(self, a, b) -> bool:
        return a == b

    # ---- the student
    def classes(self) -> list[str]:
        return []

    def features(self, q: dict):
        raise NotImplementedError

    def build(self, label: str, conf: float, n: int):
        raise NotImplementedError

    fewshot = False                                  # may Ollama be shown how people here worded similar things?

    def sim_text(self, q: dict) -> str:
        """The words of a question that make it like another (for choosing examples)."""
        return ""

    def shot(self, q: dict, answer) -> str:
        """One example for Ollama's question: what was asked, and the answer people approved."""
        return ""

    def max_level(self) -> str:
        return {"watch": "watch", "recall": "recall", "picture": "recall"}.get(self.kind, "solo")

    def start_level(self) -> str:
        return "watch" if self.kind == "watch" else "recall"


SPECS: dict[str, Spec] = {}


def spec(cls):
    s = cls()
    SPECS[s.name] = s
    return cls


def _cells(row, n=30, w=60):
    return [str(x)[:w] for x in list(row or [])[:n]]


# ---------------------------------------------------------------- is row 1 a header?
def _row_stats(cells: list[str]) -> list[float]:
    n = len(cells) or 1
    num = sum(1 for c in cells if NUMERIC.match(c or ""))
    emp = sum(1 for c in cells if not (c or "").strip())
    cap = sum(1 for c in cells if (c or "")[:1].isupper())
    allcap = sum(1 for c in cells if len(c or "") > 1 and (c or "").isupper())
    dig = sum(1 for c in cells if any(ch.isdigit() for ch in (c or "")))
    colon = sum(1 for c in cells if (c or "").rstrip().endswith(":"))
    ln = sum(len(c or "") for c in cells) / n
    return [num / n, emp / n, cap / n, allcap / n, dig / n, colon / n, math.log1p(ln) / 5.0, math.log1p(len(cells)) / 4.0]


@spec
class TableHeader(Spec):
    name = "table_headers"
    label = "Is row 1 of a table a header?"
    kind = "class"
    help = "Yes / no from the first two rows. Dez AI learns what header rows look like in your files."

    def ask(self, row1, row2):
        r1, r2 = _cells(row1), _cells(row2)
        if not any(r1) and not any(r2):
            return None
        return {"key": key_of(" | ".join(r1)[:400], " | ".join(r2)[:400]), "q": {"r1": r1, "r2": r2}}

    def to_json(self, ans):
        return None if ans is None else (bool(ans), "true" if ans else "false")

    def classes(self):
        return ["false", "true"]

    def features(self, q):
        r1, r2 = q.get("r1") or [], q.get("r2") or []
        s1, s2 = _row_stats(r1), _row_stats(r2)
        dense = s1 + s2 + [s2[0] - s1[0], s2[4] - s1[4], s1[6] - s2[6], 1.0 if len(r1) == len(r2) else 0.0,
                           1.0 if [nt(x) for x in r1] == [nt(x) for x in r2] else 0.0]
        tok = ["b", f"n1={min(len(r1), 8)}"]
        tok += [f"h1={w}" for w in _words(" ".join(r1))[:12]]
        tok += [f"s1={_shape(c.split()[0])}" for c in r1 if c.split()][:10]
        tok += [f"s2={_shape(c.split()[0])}" for c in r2 if c.split()][:10]
        return dense, tok

    def build(self, label, conf, n):
        return label == "true"


# ---------------------------------------------------------------- does the table go on?
@spec
class TableMerge(Spec):
    name = "table_merge"
    label = "Does the table go on over the page break?"
    kind = "class"
    help = "Same table or a new one, from the titles, the header rows and the rows around the break."

    def ask(self, ctx, images=None):
        ctx = ctx or {}

        def t(k, n=200):
            return str(ctx.get(k) or "")[:n]

        def rows(k):
            return [str(x)[:120] for x in list(ctx.get(k) or [])[:3]]
        q = {"a_title": t("a_title"), "a_head": t("a_head", 300), "a_rows": rows("a_rows"), "a_trail": t("a_trail"),
             "b_title": t("b_title"), "b_head": t("b_head", 300), "b_rows": rows("b_rows"), "hints": t("hints", 300),
             "v": 1 if images else 0}
        # the picture variant is asked because somebody wants the pages looked at: its answers are its own
        key = key_of(q["a_title"], q["a_head"], "|".join(q["a_rows"][-2:]), q["a_trail"], q["b_title"], q["b_head"],
                     "|".join(q["b_rows"][:2]), "pages" if images else "text")
        return {"key": key, "q": q}

    def to_json(self, ans):
        if not isinstance(ans, dict) or not isinstance(ans.get("same"), bool):
            return None
        return {"same": ans["same"], "why": str(ans.get("why") or "")[:100]}, ("same" if ans["same"] else "new")

    def empty(self):
        return None

    def same(self, a, b):
        return bool(a and b and a.get("same") == b.get("same"))

    def classes(self):
        return ["new", "same"]

    def features(self, q):
        ah, bh = nt(q.get("a_head")), nt(q.get("b_head"))
        bt, at = q.get("b_title") or "", q.get("a_title") or ""
        hints = q.get("hints") or ""
        m = re.search(r"score\s+(-?[\d.]+)", hints)
        score = max(-5.0, min(5.0, float(m.group(1)))) / 5.0 if m else 0.0
        last_a = (q.get("a_rows") or [""])[-1]
        brows = q.get("b_rows") or []
        bnum = sum(1 for r in brows for c in r.split(" | ") if NUMERIC.match(c)) / max(1, sum(len(r.split(" | ")) for r in brows))
        dense = [1.0 if ah and ah == bh else 0.0, _jaccard(_words(ah), _words(bh)), 0.0 if nt(bt) else 1.0,
                 0.0 if nt(at) else 1.0, 0.0 if nt(q.get("a_trail")) else 1.0,
                 1.0 if re.match(r"\s*(table|tab\.|exhibit|chart|figure)\s*[\dA-Za-z.]+", bt, re.I) else 0.0,
                 1.0 if re.search(r"cont(inued|\.|d\b)", bt + " " + at, re.I) else 0.0,
                 score, 1.0 if "titles: same" in hints else 0.0, math.log1p(ah.count("|") + 1) / 3.0, bnum,
                 0.0 if bh else 1.0, 1.0 if re.search(r"\b(total|sum|overall)\b", last_a, re.I) else 0.0]
        first = bt.split()[0] if bt.split() else ""
        tok = ["b", f"bt={_shape(first)}"] + [f"bt_{w}" for w in _words(bt)[:4]] + [f"hint_{w}" for w in _words(hints)[:10]]
        return dense, tok

    def build(self, label, conf, n):
        return {"same": label == "same", "why": f"Dez AI: learned from {n} lesson(s), {round(conf * 100)} % sure"}


# ---------------------------------------------------------------- two tags joined by hand
@spec
class MergeCheck(Spec):
    name = "merge"
    label = "Is this really one list / table / paragraph (two parts joined by hand)?"
    kind = "class"
    help = "Same or not, and whether part two repeats the header."

    def ask(self, kind, a_tail, b_head, page_a=0, page_b=0):
        a, b = [str(x)[:120] for x in list(a_tail or [])[:4]], [str(x)[:120] for x in list(b_head or [])[:4]]
        if not a and not b:
            return None
        return {"key": key_of(kind, "|".join(a), "|".join(b)),
                "q": {"kind": str(kind or ""), "a": a, "b": b, "gap": int(page_b or 0) - int(page_a or 0)}}

    def to_json(self, ans):
        if not isinstance(ans, dict) or not isinstance(ans.get("same"), bool):
            return None
        rep = bool(ans.get("repeat_header"))
        return ({"same": ans["same"], "repeat_header": rep, "why": str(ans.get("why") or "")[:100]},
                ("T" if ans["same"] else "F") + ":" + ("T" if rep else "F"))

    def same(self, a, b):
        return bool(a and b and a.get("same") == b.get("same") and bool(a.get("repeat_header")) == bool(b.get("repeat_header")))

    def classes(self):
        return ["F:F", "F:T", "T:F", "T:T"]

    def features(self, q):
        a, b = q.get("a") or [""], q.get("b") or [""]
        la, fb = a[-1], b[0]
        dense = [_jaccard(_words(la), _words(fb)), 1.0 if fb[:1].islower() else 0.0, 1.0 if la.rstrip().endswith((".", "!", "?", ":")) else 0.0,
                 1.0 if nt(la) == nt(fb) else 0.0, min(abs(int(q.get("gap") or 0)), 4) / 4.0, math.log1p(len(a)) / 2.0,
                 math.log1p(len(b)) / 2.0, 1.0 if NUMERIC.match(fb.split(" | ")[0] if fb else "") else 0.0,
                 1.0 if any(nt(x) == nt(b[0]) for x in a) else 0.0]
        tok = ["b", f"kind={nt(q.get('kind'))}", f"sh={_shape(fb.split()[0] if fb.split() else '')}"]
        return dense, tok

    def build(self, label, conf, n):
        same, rep = label.split(":")
        return {"same": same == "T", "repeat_header": rep == "T", "why": f"Dez AI: learned from {n} lesson(s)"}


# ---------------------------------------------------------------- which printed lines start an entry / an item
LINE_BULLET = re.compile(r"^\s*[•●▪◦‣⁃■·*\-–—]\s")
LINE_NUMBER = re.compile(r"^\s*(\(?\d{1,3}[.)\]]|\[\d{1,3}\])\s")
LINE_LETTER = re.compile(r"^\s*\(?[A-Za-z][.)]\s")
LINE_AUTHOR = re.compile(r"^[A-Z][A-Za-z'’\-]+,\s+(?:[A-Z]\.\s?)+")
LINE_YEAR_PAREN = re.compile(r"\((?:1[89]|20)\d\d[a-z]?\)")
LINE_YEAR = re.compile(r"\b(?:1[89]|20)\d\d\b")


def line_features(lines: list[str], i: int):
    t = lines[i] or ""
    prev = lines[i - 1] if i else ""
    nxt = lines[i + 1] if i + 1 < len(lines) else ""
    words = t.split()
    dense = [1.0 if i == 0 else 0.0, math.log1p(len(t)) / 6.0, 1.0 if t[:1].isupper() else 0.0,
             1.0 if t[:1].islower() else 0.0, 1.0 if t[:1].isdigit() else 0.0, 1.0 if LINE_BULLET.match(t) else 0.0,
             1.0 if LINE_NUMBER.match(t) else 0.0, 1.0 if LINE_LETTER.match(t) else 0.0,
             1.0 if LINE_AUTHOR.match(t) else 0.0, 1.0 if LINE_YEAR_PAREN.search(t[:70]) else 0.0,
             1.0 if LINE_YEAR.search(t) else 0.0,
             1.0 if (not prev or prev.rstrip().endswith((".", "?", "!", ":", ")", "]"))) else 0.0,
             1.0 if prev.rstrip().endswith((",", "-", "–", ";")) else 0.0, 1.0 if nxt[:1].islower() else 0.0,
             math.log1p(len(prev)) / 6.0, 1.0 if t.rstrip().endswith(".") else 0.0]
    tok = ["b", f"sh={_shape(words[0] if words else '')}", f"sh2={_shape(words[1] if len(words) > 1 else '')}",
           "pe=" + (prev.rstrip()[-1:] if prev.rstrip()[-1:] in ".,;:?!)]-–" else ("a" if prev.rstrip()[-1:].isalnum() else "o")),
           "ne=" + (t.rstrip()[-1:] if t.rstrip()[-1:] in ".,;:?!)]-–" else ("a" if t.rstrip()[-1:].isalnum() else "o"))]
    tok += [f"w0={w}" for w in _words(" ".join(words[:1]))[:1]]
    return dense, tok


class _Lines(Spec):
    kind = "lines"
    limit = 160

    def ask(self, lines):
        ls = [str(x)[:160] for x in list(lines or [])[: self.limit]]
        if len(ls) < 2:
            return None
        return {"key": key_of("\n".join(ls)), "q": {"lines": [x[:80] for x in ls[:110]]}}

    def to_json(self, ans):
        if not isinstance(ans, list) or not ans:
            return None
        return [int(x) for x in ans], ""

    def classes(self):
        return ["0", "1"]

    def features(self, q):
        ls = q.get("lines") or []
        rows = [line_features(ls, i) for i in range(len(ls))]
        return [r[0] for r in rows], [r[1] for r in rows]

    def build(self, label, conf, n):
        return None


@spec
class BibStarts(_Lines):
    name = "bibliography"
    label = "Reference list: which line starts an entry?"
    limit = 160
    help = "Per line: does it start a new entry? Learned from every list Ollama split."


@spec
class ListStarts(_Lines):
    name = "zone_list"
    label = "Drawn list: which line starts an item?"
    limit = 120
    help = "Per line: does it start a new item?"


# ---------------------------------------------------------------- the label of a form field
def _field_clean(s: str) -> str:
    s = " ".join(str(s or "").replace("_", " ").split())
    s = re.sub(r"[\s:.\-–—…]+$", "", s)
    s = re.sub(r"^[\s:.\-–—…]+", "", s)
    return s[:100]


def _humanize(name: str) -> str:
    n = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(name or ""))
    n = re.sub(r"[_.\[\]\-]+", " ", n)
    n = " ".join(n.split())
    return "" if re.fullmatch(r"(text|check|radio|button|field|combo|list)?\s*\d*", n.lower()) else n.title()[:100]


@spec
class FieldLabel(Spec):
    name = "fields"
    label = "Form field: its label"
    kind = "rule"
    help = "The label is usually the text printed beside the field; Dez AI finds out which side, per kind of field."
    fewshot = True

    def sim_text(self, q):
        return f"{q.get('left', '')} {q.get('above', '')} {q.get('right', '')} {q.get('name', '')}"

    def shot(self, q, a):
        bits = [f"{k} “{q[k][:50]}”" for k in ("left", "right", "above") if q.get(k)]
        return f"{q.get('ft', 'field')}: {', '.join(bits) or 'name ' + q.get('name', '')[:40]} → {str(a)[:80]}"

    def ask(self, ft, name, left="", right="", above=""):
        q = {"ft": str(ft or "")[:30], "name": str(name or "")[:60], "left": str(left or "")[:120],
             "right": str(right or "")[:120], "above": str(above or "")[:120]}
        if q["left"] or q["right"] or q["above"]:
            key = key_of(q["ft"], q["left"], q["right"], q["above"])
        else:
            key = key_of(q["ft"], q["name"])
        return {"key": key, "q": q}

    def to_json(self, ans):
        a = str(ans or "").strip()
        return (a, "") if a else None

    def empty(self):
        return ""

    def same(self, a, b):
        return nt(_field_clean(a)) == nt(_field_clean(b))

    def candidates(self, q) -> dict:
        return {k: v for k, v in {"left": _field_clean(q.get("left")), "above": _field_clean(q.get("above")),
                                  "right": _field_clean(q.get("right")), "name": _humanize(q.get("name"))}.items() if v}

    def pattern(self, q) -> str:
        ft = nt(q.get("ft")).split()[-1] if nt(q.get("ft")) else "?"
        return ft + ":" + "+".join(sorted(self.candidates(q)))


@spec
class LinkText(Spec):
    name = "links"
    label = "Link: what to say instead of “click here”"
    help = "The description Ollama wrote for a link is remembered for the same link text and address."
    fewshot = True

    def sim_text(self, q):
        return f"{q.get('text', '')} {q.get('dest', '')}"

    def shot(self, q, a):
        return f"Link text “{q.get('text', '')[:60]}”, goes to {q.get('dest', '')[:100]} → {str(a)[:100]}"

    def ask(self, text, dest, context=""):
        q = {"text": str(text or "")[:120], "dest": str(dest or "")[:200], "ctx": str(context or "")[:160]}
        return {"key": key_of(q["text"], q["dest"]), "q": q}

    def empty(self):
        return ""


@spec
class TableSummary(Spec):
    name = "table_summary"
    label = "Table: one-sentence summary"
    help = "Remembered for the same caption, header and first rows."
    fewshot = True

    def sim_text(self, q):
        return f"{q.get('cap', '')} {q.get('head', '')}"

    def shot(self, q, a):
        return f"Table “{q.get('cap', '')[:60]}” with columns {q.get('head', '')[:100]} → {str(a)[:140]}"

    def ask(self, caption, head, rows):
        q = {"cap": str(caption or "")[:160], "head": " | ".join(str(x) for x in (head or []))[:300],
             "rows": "\n".join(str(r) for r in list(rows or [])[:3])[:600]}
        if not (q["cap"] or q["head"]):
            return None
        return {"key": key_of(q["cap"], q["head"], q["rows"]), "q": q}

    def empty(self):
        return ""


@spec
class XRef(Spec):
    name = "xref"
    label = "Doubtful cross-reference"
    help = "Remembered for the same wording, sentence and candidates."

    def ask(self, mention, context, candidates):
        q = {"m": str(mention or "")[:80], "ctx": str(context or "")[:160], "c": [str(x)[:100] for x in list(candidates or [])[:8]]}
        return {"key": key_of(q["m"], q["ctx"], "|".join(q["c"])), "q": q}

    def to_json(self, ans):
        if not isinstance(ans, dict) or not isinstance(ans.get("is_reference"), bool):
            return None
        return ans, ""


# ---------------------------------------------------------------- pictures
class _Picture(Spec):
    kind = "picture"
    kind_of_model = "vision"
    near = 3                    # how many of the 128 bits of the fingerprint may differ
    near_grid = 4               # how far the 4×4 brightness grids may differ (summed over the 16 parts)

    def extra(self, *a, **kw) -> str:           # the rest of the question besides the picture
        return ""

    def picture_of(self, a, kw):                # -> png bytes
        return a[0] if a else kw.get("png_bytes")

    def ask(self, *a, **kw):
        png = self.picture_of(a, kw)
        ph = phash(png) if png else None
        if ph is None:
            return None
        ex = self.extra(*a, **kw)
        return {"key": ph[0] + ":" + ph[1] + ":" + key_of(ex), "q": {"h": ph[0], "g": ph[1], "ar": ph[2], "x": ex[:200]},
                "hash": ph[0], "grid": ph[1], "ar": ph[2], "extra": key_of(ex)}


@spec
class AltText(_Picture):
    name = "alt_text"
    label = "Figure: alternative text"
    help = "The same picture (a logo, an icon) gets the same alt text without asking."

    def ask(self, png_bytes, context=""):
        return _Picture.ask(self, png_bytes)

    def empty(self):
        return ""


@spec
class MathRead(_Picture):
    name = "math"
    label = "Formula: read aloud / decorative?"
    help = "The same formula picture gets the same reading."

    def ask(self, png_bytes, context=""):
        return _Picture.ask(self, png_bytes)

    def to_json(self, ans):
        return (ans, "") if isinstance(ans, dict) else None


@spec
class TableRowInside(_Picture):
    name = "table_rows"
    label = "Table edge: is this line inside the table?"
    help = "The same picture and text get the same answer."

    def extra(self, png_bytes, text=""):
        return nt(text)[:200]

    def ask(self, png_bytes, text=""):
        return _Picture.ask(self, png_bytes, text)

    def to_json(self, ans):
        return None if ans is None else (bool(ans), "true" if ans else "false")


@spec
class ZoneStructure(_Picture):
    name = "zone"
    label = "Drawn zone: rows, columns, headers"

    def extra(self, png_bytes, role="", text=""):
        return nt(role) + "|" + nt(text)[:300]

    def ask(self, png_bytes, role="", text=""):
        return _Picture.ask(self, png_bytes, role, text)

    def to_json(self, ans):
        return (ans, "") if isinstance(ans, dict) else None


@spec
class ZoneRole(_Picture):
    name = "zone_role"
    label = "Drawn zone: what is it?"

    def extra(self, png_bytes, text="", hints=""):
        return nt(text)[:300] + "|" + nt(hints)[:200]

    def ask(self, png_bytes, text="", hints=""):
        return _Picture.ask(self, png_bytes, text, hints)

    def to_json(self, ans):
        return (ans, str(ans.get("role") or "")) if isinstance(ans, dict) and ans.get("role") else None


@spec
class ReadingOrder(_Picture):
    name = "reading_order"
    label = "Page reading order"

    def extra(self, png_bytes, blocks=None):
        return "\n".join(nt(b)[:90] for b in list(blocks or [])[:80])

    def ask(self, png_bytes, blocks=None):
        return _Picture.ask(self, png_bytes, blocks)

    def to_json(self, ans):
        return (ans, "") if isinstance(ans, dict) else None


# ---------------------------------------------------------------- recorded only (the brain learns from them elsewhere)
class _Watch(Spec):
    kind = "watch"

    def ask(self, *a, **kw):
        try:
            payload = _jd([a, kw])[:2000]
        except Exception:
            return None
        return {"key": key_of(payload), "q": {"args": payload}}

    def to_json(self, ans):
        if not ans:
            return None
        js = _jd(ans)
        if len(js) > 4000:
            return None
        return ans, ""

    def empty(self):
        return []


@spec
class ReviewRoles(_Watch):
    name = "roles"
    label = "Tag roles (editor review)"
    help = "Recorded. The brain's own model learns tag roles from the brain's questions (below)."


@spec
class SampleRoles(_Watch):
    name = "sample_roles"
    label = "Doubtful tags, with the client's samples"


@spec
class HeadingLevels(_Watch):
    name = "headings"
    label = "Heading levels"


@spec
class StudyWatch(_Watch):
    name = "study"
    label = "Study of a sample file"

    def empty(self):
        return None


@spec
class BrainWatch(_Watch):
    name = "brain"
    label = "Brain: doubtful text tags"
    help = "Every tag Ollama decides is kept with its layout and trains Dez AI's own model (low weight, until a person has had the say)."


# ================================================================ students
_STUDENTS: dict = {}          # task -> {"stamp": str, "obj": Student, "checked": float}
_ST_LOCK = threading.Lock()


def _np():
    try:
        import numpy as np
        return np
    except Exception:
        return None


class Student:
    """A trained model (or rule table) for one task."""

    def __init__(self, task: str, info: dict, model=None):
        self.task, self.info, self.model = task, info, model

    @property
    def tau(self) -> float:
        return float(self.info.get("tau") or 0.9)


def _wilson(k: int, n: int, z: float = 1.64) -> float:
    if n <= 0:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    return (p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d


def load_student(task: str, fresh: bool = False) -> Student | None:
    now = time.time()
    with _ST_LOCK:
        e = _STUDENTS.get(task)
        if e is not None and not fresh and now - e["checked"] < 30.0:
            return e["obj"]
    try:
        with _db(timeout=2.0) as c:
            r = c.execute("SELECT trained, info, blob FROM students WHERE task=?", (task,)).fetchone()
    except Exception:
        return e["obj"] if e else None
    with _ST_LOCK:
        if r is None:
            _STUDENTS[task] = {"stamp": "", "obj": None, "checked": now}
            return None
        if e is not None and e["stamp"] == r["trained"]:
            e["checked"] = now
            return e["obj"]
        try:
            info = json.loads(r["info"] or "{}")
            if int(info.get("fv") or 0) != FEATURES_VERSION:
                _STUDENTS[task] = {"stamp": r["trained"], "obj": None, "checked": now}
                return None
            model = None
            if r["blob"]:
                from .brain import RoleModel
                model = RoleModel.from_bytes(r["blob"])
            obj = Student(task, info, model)
        except Exception:
            obj = None
        _STUDENTS[task] = {"stamp": r["trained"], "obj": obj, "checked": now}
        return obj


def _predict_class(sp: Spec, st: Student, q: dict):
    d, t = sp.features(q)
    P = st.model.predict([d], [t])[0]
    i = int(P.argmax())
    return st.model.classes[i], float(P[i])


def _predict_lines(sp: Spec, st: Student, q: dict):
    d, t = sp.features(q)
    if not d:
        return None
    P = st.model.predict(d, t)
    cls = st.model.classes
    one = cls.index("1")
    starts = [i for i in range(len(d)) if P[i][one] >= 0.5]
    conf = float(min(max(P[i][one], 1 - P[i][one]) for i in range(len(d))))
    return sorted({0, *starts}), conf


def _predict_rule(sp: FieldLabel, st: Student, q: dict):
    cand = sp.candidates(q)
    pat = sp.pattern(q)
    rules = st.info.get("rules") or {}
    r = rules.get(pat) or rules.get(pat.split(":", 1)[0] + ":*") or rules.get("*")
    if not r:
        return None
    src = r.get("src")
    if src not in cand:
        return None
    return cand[src], float(r.get("conf") or 0.0), int(r.get("n") or 0)


def student_ready(task: str, st_set: dict | None = None) -> bool:
    """Is this task's student proven enough to answer alone? (agrees with Ollama on unseen questions, enough lessons,
    and not caught out by the audits since)."""
    st_set = st_set or settings()
    sp = SPECS.get(task)
    if sp is None or sp.max_level() != "solo":
        return False
    s = load_student(task)
    if s is None:
        return False
    i = s.info
    if int(i.get("n") or 0) < st_set["min_lessons"] or float(i.get("acc") or 0) < st_set["agree"] or float(i.get("cov") or 0) < 0.05:
        return False
    aud = audit_stats(task, 40)
    if aud["n"] >= 10 and aud["agree"] < st_set["agree"] - 0.05:
        return False
    return True


def level_of(task: str, st_set: dict | None = None) -> str:
    """off · watch · recall · solo — what Dez AI may do for this task right now."""
    st_set = st_set or settings()
    sp = SPECS[task]
    forced = (st_set.get("levels") or {}).get(task)
    top = LEVELS.index(sp.max_level())
    if forced in LEVELS:
        return LEVELS[min(LEVELS.index(forced), top)]
    lvl = sp.start_level()
    if sp.max_level() == "solo" and student_ready(task, st_set):
        lvl = "solo"
    return lvl


def _mark(ans):
    """An answer that says why (“same header”) also says it came from Dez AI's lessons, not from Ollama just now."""
    if isinstance(ans, dict) and "why" in ans:
        w = str(ans.get("why") or "")
        if "Dez AI" not in w:
            return dict(ans, why=((w + " · ") if w else "") + "Dez AI: from its lessons")
    return ans


def own_answer(sp: Spec, qd: dict, level: str):
    """Dez AI's own answer to the question, or None: -> Own."""
    if not L.has_data():
        return None
    hit = recall_exact(sp.name, qd["key"])
    if hit is not None:
        a_json, share, n, who = hit
        if share >= 0.6 or who == "human":
            try:
                return Own(_mark(sp.from_json(json.loads(a_json))), share, "memory", n, who)
            except Exception:
                pass
    if sp.kind == "picture" and level in ("recall", "solo"):
        got = _picture_recall(sp, qd)
        if got is not None:
            return got
    if level != "solo":
        return None
    st = load_student(sp.name)
    if st is None:
        return None
    sure = settings()["sure"]
    if sp.kind == "class" and st.model is not None:
        label, conf = _predict_class(sp, st, qd["q"])
        if conf >= max(sure, st.tau):
            return Own(sp.build(label, conf, int(st.info.get("n") or 0)), conf, "student", int(st.info.get("n") or 0), "dez")
    elif sp.kind == "lines" and st.model is not None:
        got = _predict_lines(sp, st, qd["q"])
        if got is not None and got[1] >= max(sure, st.tau):
            return Own(got[0], got[1], "student", int(st.info.get("n") or 0), "dez")
    elif sp.kind == "rule":
        got = _predict_rule(sp, st, qd["q"])
        if got is not None and got[1] >= st.info.get("min_conf", 0.9):
            return Own(got[0], got[1], "student", got[2], "dez")
    return None


_PIC: dict = {}
_PIC_LOCK = threading.Lock()


def _picture_items(sp) -> list:
    """Every usable lesson of a picture task, parsed once and then topped up with the new ones (the picture jobs ask
    hundreds of questions in a row; parsing every lesson again for each would make them slower than Ollama)."""
    cols = "id, qkey, q, a, source, verdict, weight, seen"
    with _PIC_LOCK, _db(timeout=2.0) as c:
        ent = _PIC.get(sp.name)
        n_ok = c.execute("SELECT COUNT(*) n FROM lessons WHERE task=? AND verdict>=0", (sp.name,)).fetchone()["n"]
        new = []
        if ent is not None:
            bad = {r["id"] for r in c.execute("SELECT id FROM lessons WHERE task=? AND verdict<0 AND id<=?", (sp.name, ent["last"]))}
            ent["ids"] -= bad
            for i in bad:
                ent["items"].pop(i, None)
            new = c.execute(f"SELECT {cols} FROM lessons WHERE task=? AND verdict>=0 AND id>? ORDER BY id", (sp.name, ent["last"])).fetchall()
            if len(ent["ids"]) + len(new) != n_ok:
                ent = None                                   # something was forgotten meanwhile: read it all again
        if ent is None:
            ent = _PIC[sp.name] = {"last": 0, "ids": set(), "items": {}}
            new = c.execute(f"SELECT {cols} FROM lessons WHERE task=? AND verdict>=0 ORDER BY id", (sp.name,)).fetchall()
        for r in new:
            ent["last"] = max(ent["last"], r["id"])
            ent["ids"].add(r["id"])
            try:
                qq = json.loads(r["q"])
                ent["items"][r["id"]] = (qq["h"], qq.get("g") or "", float(qq.get("ar") or 0), r["qkey"].rsplit(":", 1)[-1], r)
            except Exception:
                continue
        return list(ent["items"].values())


def _picture_recall(sp: _Picture, qd: dict):
    """The lessons of this task whose picture looks like this one (and whose other words are the same)."""
    try:
        items = _picture_items(sp)
    except Exception:
        return None
    best: list = []
    for h, g, ar, ex, r in items:
        if ex != qd["extra"]:
            continue
        if ar and qd["ar"] and abs(ar - qd["ar"]) > 0.08 * max(ar, qd["ar"]):
            continue
        if _hamming(h, qd["hash"]) > sp.near:
            continue
        if g and qd.get("grid") and sum(abs(int(x) - int(y)) for x, y in zip(g, qd["grid"])) > sp.near_grid:
            continue
        best.append(r)
    if not best:
        return None
    got = _score_rows(best)
    if got is None or (got[1] < 0.6 and got[3] != "human"):
        return None
    try:
        return Own(_mark(sp.from_json(json.loads(got[0]))), got[1], "memory", got[2], got[3])
    except Exception:
        return None


# ---------------------------------------------------------------- training
def _lessons_for(task: str, cap: int = 20000) -> list:
    with _db(timeout=10.0) as c:
        return c.execute("SELECT id, qkey, q, a, label, source, verdict, weight, seen FROM lessons WHERE task=? AND verdict>=0 "
                         "ORDER BY id DESC LIMIT ?", (task, cap)).fetchall()


def _fit(np, rows, classes, folds: int = 5):
    """rows: (dense, tokens, label, weight, group) -> (model, acc at tau, cov at tau, acc overall, T, tau)."""
    from . import brain
    ev_rows = rows
    best_tau = 0.9
    Zs, golds = [], []
    if len({r[4] for r in ev_rows}) >= 4:
        for tr_i, te_i in brain._grouped_folds(ev_rows, folds):
            m = brain.RoleModel.fit([ev_rows[i][0] for i in tr_i], [ev_rows[i][1] for i in tr_i],
                                    [ev_rows[i][2] for i in tr_i], [ev_rows[i][3] for i in tr_i], classes, epochs=120)
            Zs.append(m.logits([ev_rows[i][0] for i in te_i], [ev_rows[i][1] for i in te_i]))
            golds += [ev_rows[i][2] for i in te_i]
    T = 1.0
    acc = cov = overall = 0.0
    if Zs:
        Z = np.concatenate(Zs, 0)
        y = np.asarray([classes.index(g) for g in golds], dtype=np.int64)
        T = brain.fit_temperature(Z, y)
        P = brain.softmax(Z, T)
        conf = P.max(1)
        hit = (P.argmax(1) == y)
        overall = float(hit.mean())
        # the lowest confidence at which the answers kept to ≥ 98 % right (or the highest we have)
        best_tau = 0.99
        for tau in (0.70, 0.75, 0.80, 0.85, 0.90, 0.93, 0.96, 0.98):
            m_ = conf >= tau
            if m_.sum() >= max(8, 0.05 * len(y)) and float(hit[m_].mean()) >= 0.98:
                best_tau = tau
                break
        m_ = conf >= best_tau
        cov = float(m_.mean())
        acc = float(hit[m_].mean()) if m_.any() else 0.0
    model = brain.RoleModel.fit([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows],
                                classes, epochs=140)
    model.T = T
    return model, acc, cov, overall, best_tau


def train_task(task: str) -> dict:
    """Learn this task from its lessons; keep the student with how well it did on questions it had not seen."""
    sp = SPECS.get(task)
    if sp is None or sp.max_level() != "solo":
        return {"task": task, "ok": False, "why": "this task is remembered, not trained"}
    np = _np()
    if np is None:
        return {"task": task, "ok": False, "why": "numpy is not installed"}
    t0 = time.time()
    rows_db = _lessons_for(task, 1500 if sp.kind == "lines" else 20000)      # a list is up to 160 examples: the newest 1 500 lists
    info: dict = {"task": task, "kind": sp.kind, "fv": FEATURES_VERSION}
    blob = b""
    if sp.kind == "rule":
        info.update(_train_rules(sp, rows_db))
        n, acc, cov = int(info["n"]), float(info["acc"]), float(info["cov"])
    else:
        rows = []
        classes = sp.classes()
        if sp.kind == "class":
            for r in rows_db:
                try:
                    if r["label"] not in classes:
                        continue
                    d, t = sp.features(json.loads(r["q"]))
                except Exception:
                    continue
                w = float(r["weight"] or 1.0) * (3.0 if r["source"] == "human" else 1.0) * (1.5 if r["verdict"] > 0 else 1.0)
                rows.append((d, t, r["label"], w, r["qkey"]))
        else:                                              # lines
            for r in rows_db:
                try:
                    q = json.loads(r["q"])
                    starts = set(json.loads(r["a"]))
                    d_, t_ = sp.features(q)
                except Exception:
                    continue
                w = float(r["weight"] or 1.0) * (3.0 if r["source"] == "human" else 1.0)
                for i in range(len(d_)):
                    rows.append((d_[i], t_[i], "1" if i in starts else "0", w, r["qkey"]))
        n = len({x[4] for x in rows})
        if len(rows) < 12 or len({x[2] for x in rows}) < 2:
            info.update(n=n, acc=0.0, cov=0.0, note=f"{n} lesson(s) — needs both answers to be seen")
            acc = cov = 0.0
        else:
            model, acc, cov, overall, tau = _fit(np, rows, classes)
            blob = model.to_bytes()
            info.update(n=n, rows=len(rows), acc=round(acc, 4), cov=round(cov, 4), overall=round(overall, 4), tau=tau,
                        classes=classes, temperature=model.T)
    info["seconds"] = round(time.time() - t0, 2)
    st_set = settings(fresh=True)
    ready = n >= st_set["min_lessons"] and acc >= st_set["agree"] and cov >= 0.05
    info["ready"] = bool(ready)
    with _db(timeout=15.0) as c:
        c.execute("INSERT INTO students (task, trained, n, acc, cov, level, info, blob) VALUES (?,?,?,?,?,?,?,?) "
                  "ON CONFLICT(task) DO UPDATE SET trained=excluded.trained, n=excluded.n, acc=excluded.acc, "
                  "cov=excluded.cov, level=excluded.level, info=excluded.info, blob=excluded.blob",
                  (task, _now(), n, acc, cov, "solo" if ready else "recall", _jd(info), blob))
    with _ST_LOCK:
        _STUDENTS.pop(task, None)
    return {"task": task, "ok": True, **{k: info.get(k) for k in ("n", "acc", "cov", "overall", "tau", "ready", "seconds", "note")}}


def _train_rules(sp: FieldLabel, rows_db) -> dict:
    """Which printed text is the label of a field? Count, per kind of field and per set of texts around it, how often the
    teacher's label was each of them; keep the rules that held on lessons they were not counted from (leave one out)."""
    by_pat: dict = collections.defaultdict(lambda: collections.Counter())
    total: dict = collections.Counter()
    items = []
    for r in rows_db:
        try:
            q = json.loads(r["q"])
            ans = json.loads(r["a"])
        except Exception:
            continue
        cand = sp.candidates(q)
        match = [k for k, v in cand.items() if nt(v) == nt(_field_clean(ans))]
        pat = sp.pattern(q)
        w = 3 if r["source"] == "human" else 1
        for key in (pat, pat.split(":", 1)[0] + ":*", "*"):
            total[key] += w
            for k in match:
                by_pat[key][k] += w
        items.append((pat, match, w))
    rules: dict = {}
    for key, cnt in by_pat.items():
        src, hits = cnt.most_common(1)[0]
        n = total[key]
        conf = (hits + 1) / (n + 2)                          # a rule seen only a few times is not as sure as one seen often
        if n >= RULE_MIN and hits / n >= 0.95 and conf >= 0.9:
            rules[key] = {"src": src, "n": n, "hits": hits, "conf": round(conf, 4)}
    # leave-one-out: would the rule have been right for this lesson if it had not been counted?
    right = covered = 0
    for pat, match, w in items:
        for key in (pat, pat.split(":", 1)[0] + ":*", "*"):
            r_ = rules.get(key)
            if not r_:
                continue
            n_ = r_["n"] - w
            h_ = r_["hits"] - (w if r_["src"] in match else 0)
            if n_ >= RULE_MIN - 2 and (h_ + 1) / (n_ + 2) >= 0.88:
                covered += 1
                right += 1 if r_["src"] in match else 0
            break
    n = len(items)
    acc = right / covered if covered else 0.0
    return {"rules": rules, "n": n, "acc": round(acc, 4), "cov": round(covered / n, 4) if n else 0.0, "min_conf": 0.9}


def train_all(force: bool = False, only: list[str] | None = None) -> dict:
    """Train the students whose lessons changed since they were last trained."""
    out = {"tasks": [], "skipped": 0}
    try:
        with _db(timeout=10.0) as c:
            seen = {r["task"]: (r["n"], r["m"]) for r in c.execute("SELECT task, COUNT(*) n, MAX(updated) m FROM lessons GROUP BY task")}
            have = {r["task"]: r["trained"] for r in c.execute("SELECT task, trained FROM students")}
    except Exception as exc:
        return {"tasks": [], "error": str(exc)[:200]}
    for task, sp in SPECS.items():
        if only and task not in only:
            continue
        if sp.max_level() != "solo" or task not in seen:
            continue
        n, m = seen[task]
        if not force and task in have and (m or "") <= have[task]:
            out["skipped"] += 1
            continue
        if n < 8 and not force:
            out["skipped"] += 1
            continue
        try:
            out["tasks"].append(train_task(task))
        except Exception as exc:
            out["tasks"].append({"task": task, "ok": False, "why": str(exc)[:200]})
    try:
        prune()
    except Exception:
        pass
    return out


def prune() -> int:
    """Keep each task to LESSON_CAP lessons: the oldest the people never confirmed go first. Daily counts older than 400 days and
    audits beyond the last 400 of a task go too."""
    gone = 0
    with _db(timeout=15.0) as c:
        for r in c.execute("SELECT task, COUNT(*) n FROM lessons GROUP BY task HAVING n > ?", (LESSON_CAP,)).fetchall():
            cur = c.execute("DELETE FROM lessons WHERE id IN (SELECT id FROM lessons WHERE task=? AND source!='human' AND verdict<=0 "
                            "ORDER BY id ASC LIMIT ?)", (r["task"], r["n"] - LESSON_CAP))
            gone += cur.rowcount or 0
        c.execute("DELETE FROM teach_days WHERE day < ?", ((datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d"),))
        for r in c.execute("SELECT task, MAX(id) m FROM teach_audit GROUP BY task").fetchall():
            c.execute("DELETE FROM teach_audit WHERE task=? AND id < ?", (r["task"], r["m"] - 400))
        c.execute("DELETE FROM teacher_roles WHERE created < ?", ((datetime.now() - timedelta(days=365)).isoformat(timespec="seconds"),))
    return gone


# ---------------------------------------------------------------- audits
_AUD: dict = {}


def audit_stats(task: str, last: int = 40, fresh: bool = False) -> dict:
    """How often Dez AI's own answer agreed with Ollama's in the last audits (cached for 20 s)."""
    now = time.time()
    hit = _AUD.get((task, last))
    if hit is not None and not fresh and now - hit[0] < 20.0:
        return hit[1]
    try:
        with _db(timeout=2.0) as c:
            rows = c.execute("SELECT agree FROM teach_audit WHERE task=? ORDER BY id DESC LIMIT ?", (task, last)).fetchall()
    except Exception:
        return {"n": 0, "agree": 1.0}
    n = len(rows)
    out = {"n": n, "agree": (sum(int(r["agree"]) for r in rows) / n) if n else 1.0}
    _AUD[(task, last)] = (now, out)
    return out


def _audit(task: str, agree: bool, dez, ollama, conf: float) -> None:
    _AUD.clear()
    try:
        with _db(timeout=3.0) as c:
            c.execute("INSERT INTO teach_audit (task, day, agree, dez, ollama, conf, created) VALUES (?,?,?,?,?,?,?)",
                      (task, _day(), 1 if agree else 0, _jd(dez)[:400], _jd(ollama)[:400], round(float(conf), 3), _now()))
    except Exception:
        pass


# ================================================================ Dez AI shows Ollama how people here worded things
_PROMPT = threading.local()


def prompt_extra() -> str:
    """What core/ai.py adds to the question it is about to put to Ollama (empty unless a gated task set it)."""
    return getattr(_PROMPT, "v", "") or ""


def examples_for(sp: Spec, qd: dict, k: int = 3) -> str:
    """The k answers people here approved (or confirmed) for the questions most like this one — a few lines for Ollama's
    question, so what it writes follows the wording of this studio. '' when there are fewer than two."""
    if not sp.fewshot or not L.has_data():
        return ""
    from .brain import text_vec
    with _db(timeout=2.0) as c:
        rows = c.execute("SELECT q, a FROM lessons WHERE task=? AND verdict>=0 AND (source='human' OR verdict>0) "
                         "ORDER BY updated DESC LIMIT 200", (sp.name,)).fetchall()
    if len(rows) < 2:
        return ""
    want = text_vec(sp.sim_text(qd["q"]))
    scored = []
    seen = set()
    for r in rows:
        try:
            q, a = json.loads(r["q"]), json.loads(r["a"])
        except Exception:
            continue
        if not isinstance(a, str) or a in seen:
            continue
        sim = sum(x * y for x, y in zip(want, text_vec(sp.sim_text(q))))
        if sim >= 0.25:
            scored.append((sim, sp.shot(q, a)))
            seen.add(a)
    scored.sort(key=lambda t: -t[0])
    if len(scored) < 2:
        return ""
    return ("Examples of what people in this studio approved for similar items (follow their wording and style):\n"
            + "\n".join("- " + t for _, t in scored[:k]))


# ================================================================ the gate
class _Missing(Exception):
    pass


def gated(task: str):
    """Decorator for a function of core/ai.py: `fn(cfg, *args)` — Dez AI answers first when it can."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(cfg, *args, **kw):
            if kw.pop("_direct", False):
                return fn(cfg, *args, **kw)
            return call(task, fn, cfg, args, kw)
        wrapper.dez_task = task
        wrapper.__wrapped__ = fn
        return wrapper
    return deco


def _model_of(cfg, task: str) -> str:
    try:
        from . import ai
        sp = SPECS[task]
        kind = "vision" if sp.kind_of_model == "vision" else "text"
        return ai.resolve_model(cfg, kind, task)[0]
    except Exception:
        return ""


def call(task: str, fn, cfg, args: tuple, kw: dict):
    sp = SPECS.get(task)
    st = settings()
    ollama_on = cfg is not None and bool(getattr(cfg, "enabled", False))
    if sp is None or not (st["teach"] or st["solo"]):
        return fn(cfg, *args, **kw) if (ollama_on or sp is None) else sp.empty()
    try:
        qd = sp.ask(*args, **kw)
    except Exception:
        qd = None
    if qd is None:                                    # not a question Dez AI can learn from (e.g. not a picture)
        if ollama_on:
            return fn(cfg, *args, **kw)
        _count(task, "idle")
        return sp.empty()
    level = level_of(task, st)
    own = None
    if st["solo"] and level in ("recall", "solo") and (ollama_on or st["offline"]):
        try:
            own = own_answer(sp, qd, level)
        except Exception:
            own = None
    if own is not None:
        audit = (ollama_on and own.how == "student" and st["audit"] > 0 and random.random() < 1.0 / st["audit"])
        if not audit:
            _count(task, "dez", saved=avg_secs(task) if ollama_on else 0.0)
            return own.answer
    if not ollama_on:
        _count(task, "idle")
        if own is not None:
            return own.answer
        return sp.empty()
    extra = ""
    if st["fewshot"] and sp.fewshot and level != "off":
        try:
            extra = examples_for(sp, qd)
        except Exception:
            extra = ""
    _PROMPT.v = extra
    t0 = time.time()
    try:
        ans = fn(cfg, *args, **kw)
    except Exception:
        _count(task, "error")
        raise
    finally:
        _PROMPT.v = ""
    dt = time.time() - t0
    _count(task, "ollama", secs=dt)
    if st["teach"] and level != "off":
        try:
            got = sp.to_json(ans)
            if got is not None:
                a_js, label = got
                why = ""
                if isinstance(a_js, dict):
                    why = str(a_js.get("why") or "")
                record(task, qd["key"], qd["q"], a_js, label=label, why=why, model=_model_of(cfg, task))
                if own is not None:                          # an audit: did Dez AI say what Ollama says?
                    _audit(task, sp.same(own.answer, ans), own.answer, ans, own.conf)
        except Exception:
            pass
    return ans


def install(namespace: dict) -> list[str]:
    """Wrap the task functions of core/ai.py (called once at the end of that module)."""
    wrapped = []
    for task, fname in GATED_FUNCTIONS.items():
        fn = namespace.get(fname)
        if fn is not None and not hasattr(fn, "dez_task"):
            namespace[fname] = gated(task)(fn)
            wrapped.append(fname)
    return wrapped


# which function of core/ai.py is which task. table_continues is one function with a text and a picture variant.
GATED_FUNCTIONS = {
    "alt_text": "alt_text", "roles": "review_roles", "sample_roles": "sample_roles", "table_headers": "table_has_header",
    "table_rows": "table_row_inside", "table_merge": "table_continues", "headings": "heading_levels", "links": "link_description",
    "fields": "field_label", "table_summary": "table_summary", "math": "math_from_image", "xref": "xref_match",
    "merge": "merge_check", "zone": "zone_structure", "zone_role": "zone_role", "bibliography": "bib_entry_starts",
    "zone_list": "list_item_starts", "reading_order": "reading_order", "study": "study_sample",
}


# ================================================================ what the brain's questions teach (tags)
def roles_taught(client: str, job: str, doc_type: str, items: list[dict]) -> int:
    """The tags Ollama chose for the brain's doubtful text blocks, with the layout of each block.
    items: [{node, text, f (the layout features), proposed, role, conf, accepted, why}]"""
    if not items or not settings()["teach"]:
        return 0
    rows = []
    for it in items:
        f = it.get("f") or {}
        rows.append((job, it.get("node") or "", client or "", doc_type or "", it.get("proposed") or "", it.get("role") or "",
                     float(it.get("conf") or 0.7), 1 if it.get("accepted") else 0, (it.get("text") or "")[:300],
                     L.norm(it.get("text") or "") if len((it.get("text") or "").split()) <= 14 else "", f.get("font") or "",
                     (it.get("why") or "")[:120], *[f.get(k) for k in L.FEATURES], _now()))
    try:
        with _db(timeout=5.0) as c:
            c.execute("DELETE FROM teacher_roles WHERE job=? AND verdict=0", (job,))
            c.executemany("INSERT INTO teacher_roles (job, node, client, doc_type, proposed, role, conf, accepted, text, norm, "
                          "font, why, " + ", ".join(L.FEATURES) + ", created) VALUES (" + ",".join("?" * (12 + len(L.FEATURES) + 1)) + ")",
                          rows)
        _count("brain", "ollama", n=len(rows))
        return len(rows)
    except Exception:
        return 0


def role_rows(c: sqlite3.Connection, now: datetime | None = None) -> list[tuple]:
    """Training rows from the teacher's tags no person has judged yet: (dense, tokens, role, weight, group) for brain.train.
    Only the tags the brain went along with count, and they weigh little: 0.35 × how sure Ollama was."""
    from . import brain
    rows = []
    try:
        cur = c.execute("SELECT * FROM teacher_roles WHERE accepted=1 AND verdict=0")
    except sqlite3.OperationalError:
        return rows
    for r in cur:
        f = dict(r)
        if f["role"] not in brain.LEARNABLE:
            continue
        rows.append((brain.dense(f), brain.tokens(f, f["text"], f["client"] or "", f["doc_type"] or ""), f["role"],
                     0.35 * max(0.3, min(1.0, float(f["conf"] or 0.7))) * brain.recency(f.get("created"), now), "tr:" + str(f["job"])))
    return rows


def roles_judged(job: str, final_roles: dict, verified: bool, c: sqlite3.Connection | None = None) -> dict:
    """A person has had the say on a file's tags (a build, an approval): the teacher's tag was right (+1) or changed (−1).
    A judged tag no longer trains by itself — the sample / feedback rows say it better. (`c`: the caller's own connection,
    when it is in the middle of a write.)"""
    n_ok = n_bad = 0

    def run(c):
        nonlocal n_ok, n_bad
        for r in c.execute("SELECT id, node, role FROM teacher_roles WHERE job=? AND verdict=0", (job,)).fetchall():
            f = final_roles.get(r["node"])
            if f is None:
                continue
            if f == r["role"]:
                if verified:                       # only an approved file confirms; a built file is silence, not consent
                    c.execute("UPDATE teacher_roles SET verdict=1 WHERE id=?", (r["id"],))
                    n_ok += 1
            else:
                c.execute("UPDATE teacher_roles SET verdict=-1 WHERE id=?", (r["id"],))
                n_bad += 1
    try:
        if c is not None:
            run(c)
        else:
            with _db(timeout=5.0) as c2:
                run(c2)
    except Exception:
        pass
    return {"confirmed": n_ok, "changed": n_bad}


# ================================================================ what people leave in a file (human lessons)
def _table_rows(model, n) -> list[list[str]]:
    rows = []
    for cid in n.children:
        k = model.nodes.get(cid)
        if k is None:
            continue
        trs = [k] if k.role == "TR" else [model.nodes[x] for x in k.children if x in model.nodes and model.nodes[x].role == "TR"]
        for tr in trs:
            rows.append([(model.nodes[c].text or "") for c in tr.children if c in model.nodes])
    return rows


def _link_dest(n) -> str:
    x = (n.style or {}).get("xref") or {}
    return x.get("uri") or (f"page {int(x['page']) + 1}" if x.get("page") is not None else "")


def harvest(model, auto=None) -> list[tuple]:
    """What a person decided in this file, as questions with answers: [(task, qd, answer, changed_by_person)]."""
    out = []
    for n in model.nodes.values():
        a = auto.nodes.get(n.id) if auto is not None else None
        try:
            if n.role == "Table" and not n.artifact:
                rows = _table_rows(model, n)
                if len(rows) >= 2:
                    qd = SPECS["table_headers"].ask(rows[0], rows[1])
                    hr = int((n.style or {}).get("header_rows") or 0)
                    if qd is not None:
                        changed = a is not None and int((a.style or {}).get("header_rows") or 0) != hr
                        out.append(("table_headers", qd, hr > 0, changed))
                if (n.alt or "").strip():
                    cap = ""
                    for c in n.children:
                        k = model.nodes.get(c)
                        if k is not None and k.role == "Caption":
                            cap = k.text
                            break
                    flat = [" | ".join(t[:40] for t in r) for r in rows[:6]]
                    if flat:
                        qd = SPECS["table_summary"].ask(cap, flat[0].split(" | "), flat[1:4])
                        if qd is not None:
                            out.append(("table_summary", qd, n.alt.strip(), a is None or (a.alt or "") != n.alt))
            elif n.role == "Link" and (n.alt or "").strip():
                txt = (n.text or "").strip()
                dest = _link_dest(n) or txt
                qd = SPECS["links"].ask(txt, dest, "")
                out.append(("links", qd, n.alt.strip(), a is None or (a.alt or "") != n.alt))
        except Exception:
            continue
    return out


def learn_job(job_dir: str, client: str, model, auto=None, verified: bool = False) -> dict:
    """The decisions people left in a file become lessons of their own, and mark the teacher's lessons they confirm or change.
    A built file's untouched answers are not consent (nobody looked): only what a person changed, or an approved file, counts."""
    job = os.path.basename(os.path.normpath(job_dir))
    res = {"human": 0, "confirmed": 0, "corrected": 0}
    if not settings()["teach"]:
        return res
    items = harvest(model, auto)
    try:
        with _db(timeout=8.0) as c:
            for task, qd, ans, by_person in items:
                if not (verified or by_person):
                    continue
                sp = SPECS[task]
                got = sp.to_json(ans)
                if got is None:
                    continue
                a_js, label = got
                a_s = _jd(a_js)
                ah = _ahash(a_s)
                for r in c.execute("SELECT id, ahash FROM lessons WHERE task=? AND qkey=? AND source!='human'", (task, qd["key"])).fetchall():
                    if r["ahash"] == ah:
                        c.execute("UPDATE lessons SET verdict=1 WHERE id=?", (r["id"],))
                        res["confirmed"] += 1
                    else:
                        c.execute("UPDATE lessons SET verdict=-1 WHERE id=?", (r["id"],))
                        res["corrected"] += 1
                now = _now()
                c.execute("INSERT INTO lessons (task, qkey, ahash, q, a, label, why, source, model, client, job, weight, verdict, "
                          "seen, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?) ON CONFLICT(task, qkey, source, ahash) "
                          "DO UPDATE SET seen=CASE WHEN job=excluded.job THEN seen ELSE seen+1 END, "
                          "updated=excluded.updated, job=excluded.job",
                          (task, qd["key"], ah, _jd(qd["q"])[:Q_BYTES], a_s, label, "", "human", "", client or "", job,
                           1.0, 1, now, now))
                res["human"] += 1
    except Exception as exc:
        res["error"] = str(exc)[:200]
    return res


# ================================================================ for the Admin page
def _task_row(task: str, sp: Spec, st_set: dict, lessons: dict, week: dict, student: dict, aud: dict) -> dict:
    lvl_forced = (st_set.get("levels") or {}).get(task, "auto")
    lvl = level_of(task, st_set)
    ls = lessons.get(task) or {}
    wk = week.get(task) or {}
    dez, oll = int(wk.get("dez") or 0), int(wk.get("ollama") or 0)
    stud = student.get(task) or {}
    return {"task": task, "label": sp.label, "kind": sp.kind, "help": sp.help, "level": lvl, "forced": lvl_forced,
            "max": sp.max_level(), "lessons": int(ls.get("n") or 0), "human": int(ls.get("human") or 0),
            "confirmed": int(ls.get("confirmed") or 0), "corrected": int(ls.get("corrected") or 0),
            "ollama_right": (round(int(ls.get("confirmed") or 0) / (int(ls.get("confirmed") or 0) + int(ls.get("corrected") or 0)), 3)
                             if int(ls.get("confirmed") or 0) + int(ls.get("corrected") or 0) >= 3 else None),
            "dez": dez, "ollama": oll, "idle": int(wk.get("idle") or 0), "errors": int(wk.get("error") or 0),
            "alone": round(dez / (dez + oll), 3) if (dez + oll) else None,
            "saved": round(float(wk.get("saved") or 0.0), 1), "secs": round(float(wk.get("secs") or 0.0), 1),
            "student": ({"n": stud.get("n"), "acc": stud.get("acc"), "cov": stud.get("cov"), "trained": stud.get("trained"),
                         "ready": bool(stud.get("ready")), "note": stud.get("note", "")} if stud else None),
            "audit": aud.get(task) or {"n": 0, "agree": None},
            "model": ls.get("model") or ""}


def status(days: int = 7) -> dict:
    """Everything the Admin page shows about the lessons."""
    flush_counts()
    st_set = settings(fresh=True)
    with _ST_LOCK:                                    # what the admin sees is what is on the disk now
        _STUDENTS.clear()
    _AUD.clear()
    out: dict = {"settings": st_set, "tasks": [], "totals": {}, "recent": [], "levels": list(LEVELS)}
    if not L.has_data():
        out["totals"] = {"lessons": 0, "human": 0, "dez": 0, "ollama": 0, "alone": None, "week_dez": 0, "week_ollama": 0,
                         "saved": 0.0, "ollama_secs": 0.0, "students": 0,
                         "trainable": sum(1 for sp in SPECS.values() if sp.max_level() == "solo")}
        out["roles"] = {"n": 0, "waiting": 0, "confirmed": 0, "changed": 0}
        out["ollama_right"] = None
        for t, sp in SPECS.items():
            out["tasks"].append(_task_row(t, sp, st_set, {}, {}, {}, {}))
        return out
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with _db(timeout=10.0) as c:
        lessons = {}
        for r in c.execute("SELECT task, COUNT(*) n, SUM(source='human') human, SUM(verdict>0) confirmed, SUM(verdict<0) corrected, "
                           "MAX(model) model FROM lessons GROUP BY task"):
            lessons[r["task"]] = dict(r)
        week = {}
        for r in c.execute("SELECT task, SUM(dez) dez, SUM(ollama) ollama, SUM(idle) idle, SUM(error) error, SUM(secs) secs, "
                           "SUM(saved) saved FROM teach_days WHERE day >= ? GROUP BY task", (since,)):
            week[r["task"]] = dict(r)
        allt = c.execute("SELECT SUM(dez) dez, SUM(ollama) ollama, SUM(secs) secs, SUM(saved) saved FROM teach_days").fetchone()
        student = {}
        for r in c.execute("SELECT task, trained, n, acc, cov, info FROM students"):
            d = dict(r)
            try:
                info = json.loads(d.pop("info") or "{}")
            except ValueError:
                info = {}
            d["ready"] = bool(info.get("ready"))
            d["note"] = info.get("note", "")
            d["rules"] = len(info.get("rules") or {})
            student[r["task"]] = d
        aud = {}
        for t in SPECS:
            rows = c.execute("SELECT agree FROM teach_audit WHERE task=? ORDER BY id DESC LIMIT 40", (t,)).fetchall()
            if rows:
                aud[t] = {"n": len(rows), "agree": round(sum(int(r["agree"]) for r in rows) / len(rows), 3)}
        for t, sp in SPECS.items():
            out["tasks"].append(_task_row(t, sp, st_set, lessons, week, student, aud))
        for r in c.execute("SELECT id, task, source, verdict, a, q, model, client, job, seen, updated FROM lessons "
                           "ORDER BY updated DESC, id DESC LIMIT 14"):
            d = dict(r)
            d["a"] = (d["a"] or "")[:160]
            d["q"] = (d["q"] or "")[:160]
            out["recent"].append(d)
        tr = c.execute("SELECT COUNT(*) n, SUM(accepted=1 AND verdict=0) waiting, SUM(verdict>0) ok, SUM(verdict<0) bad "
                       "FROM teacher_roles").fetchone()
        out["roles"] = {"n": tr["n"] or 0, "waiting": tr["waiting"] or 0, "confirmed": tr["ok"] or 0, "changed": tr["bad"] or 0}
        trust = c.execute("SELECT ok, n FROM trust WHERE source='ai' AND role='*'").fetchone() if _has_table(c, "trust") else None
        out["ollama_right"] = (round(float(trust["ok"]) / float(trust["n"]), 3) if trust is not None and trust["n"] else None)
        n_les = sum(int((lessons.get(t) or {}).get("n") or 0) for t in SPECS)
    dez_all, oll_all = int(allt["dez"] or 0), int(allt["ollama"] or 0)
    wd = sum(int((week.get(t) or {}).get("dez") or 0) for t in SPECS)
    wo = sum(int((week.get(t) or {}).get("ollama") or 0) for t in SPECS)
    out["totals"] = {"lessons": n_les, "human": sum(int((lessons.get(t) or {}).get("human") or 0) for t in SPECS),
                     "dez": dez_all, "ollama": oll_all, "alone": round(wd / (wd + wo), 3) if (wd + wo) else None,
                     "week_dez": wd, "week_ollama": wo, "saved": round(float(allt["saved"] or 0.0), 1),
                     "ollama_secs": round(float(allt["secs"] or 0.0), 1),
                     "students": sum(1 for t in SPECS if (student.get(t) or {}).get("ready")),
                     "trainable": sum(1 for t, sp in SPECS.items() if sp.max_level() == "solo")}
    return out


def brief() -> dict:
    """A line for the System tab: how much Dez AI has been taught and how much it answers alone (the last 7 days)."""
    if not L.has_data():
        return {"lessons": 0, "alone": None, "students": 0, "trainable": sum(1 for sp in SPECS.values() if sp.max_level() == "solo")}
    since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    try:
        with _db(timeout=3.0) as c:
            n = c.execute("SELECT COUNT(*) n FROM lessons WHERE verdict>=0").fetchone()["n"]
            w = c.execute("SELECT SUM(dez) d, SUM(ollama) o FROM teach_days WHERE day>=?", (since,)).fetchone()
            ready = 0
            for r in c.execute("SELECT info FROM students"):
                try:
                    ready += 1 if json.loads(r["info"] or "{}").get("ready") else 0
                except ValueError:
                    pass
    except Exception:
        return {"lessons": 0, "alone": None, "students": 0, "trainable": 0}
    d, o = int(w["d"] or 0), int(w["o"] or 0)
    return {"lessons": n, "alone": round(d / (d + o), 3) if d + o else None, "students": ready,
            "trainable": sum(1 for sp in SPECS.values() if sp.max_level() == "solo")}


def _has_table(c, name: str) -> bool:
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def forget(task: str, what: str = "all") -> int:
    """Admin: drop a task's lessons (what='all' · 'teacher' keeps the people's · 'corrected')."""
    if task not in SPECS:
        return 0
    with _db(timeout=10.0) as c:
        if what == "teacher":
            cur = c.execute("DELETE FROM lessons WHERE task=? AND source!='human'", (task,))
        elif what == "corrected":
            cur = c.execute("DELETE FROM lessons WHERE task=? AND verdict<0", (task,))
        else:
            cur = c.execute("DELETE FROM lessons WHERE task=?", (task,))
        c.execute("DELETE FROM students WHERE task=?", (task,))
        c.execute("DELETE FROM teach_audit WHERE task=?", (task,))
    with _ST_LOCK:
        _STUDENTS.pop(task, None)
    with _PIC_LOCK:
        _PIC.pop(task, None)
    return cur.rowcount or 0


def lessons_list(task: str, limit: int = 40, offset: int = 0) -> list[dict]:
    if task not in SPECS:
        return []
    with _db(timeout=5.0) as c:
        rows = c.execute("SELECT id, source, verdict, a, q, why, model, client, job, seen, updated FROM lessons WHERE task=? "
                         "ORDER BY updated DESC, id DESC LIMIT ? OFFSET ?", (task, limit, offset)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["a"] = (d["a"] or "")[:300]
        d["q"] = (d["q"] or "")[:300]
        out.append(d)
    return out


_KNOWS: dict = {"at": 0.0, "v": False}


def offline_knows() -> bool:
    """Does Dez AI have anything to answer with while Ollama is off? (the admin allows it, and it has lessons)"""
    st = settings()
    if not (st["offline"] and st["solo"]) or not L.has_data():
        return False
    now = time.time()
    if now - _KNOWS["at"] < 30.0:
        return _KNOWS["v"]
    v = False
    try:
        with _db(timeout=1.0) as c:
            v = bool(c.execute("SELECT 1 FROM lessons WHERE verdict>=0 LIMIT 1").fetchone())
    except Exception:
        v = False
    _KNOWS.update(at=now, v=v)
    return v


def needs_training() -> list[str]:
    """The tasks whose lessons have grown since their student was trained (a tenth more, at least 5)."""
    out = []
    if not L.has_data():
        return out
    try:
        with _db(timeout=2.0) as c:
            have = {}
            for r in c.execute("SELECT task, trained, info FROM students"):
                try:
                    old_fv = int(json.loads(r["info"] or "{}").get("fv") or 0) != FEATURES_VERSION
                except ValueError:
                    old_fv = True
                if not old_fv:
                    have[r["task"]] = r["trained"]
            for r in c.execute("SELECT task, COUNT(*) n FROM lessons WHERE verdict>=0 GROUP BY task"):
                sp = SPECS.get(r["task"])
                if sp is None or sp.max_level() != "solo" or r["n"] < 8:
                    continue
                if r["task"] not in have:
                    out.append(r["task"])
                    continue
                new = c.execute("SELECT COUNT(*) n FROM lessons WHERE task=? AND verdict>=0 AND updated > ?",
                                (r["task"], have[r["task"]])).fetchone()["n"]
                if new >= max(5, int(0.1 * r["n"])):
                    out.append(r["task"])
    except Exception:
        return []
    return out


def offline_ok() -> bool:
    """May Dez AI answer when Ollama is off? (the admin's choice, and at least one task has something to say)."""
    return bool(settings()["offline"] and settings()["solo"])


def harvest_jobs(jobs_dir: str | None = None, progress=None) -> dict:
    """Read the finished files of this server once: what people decided in them become lessons (an approved file's all,
    any other file's only what a person changed), and the students learn from them."""
    from .model import DocModel
    from .options import Options
    from .pipeline import load_model
    jobs_dir = jobs_dir or os.environ.get("PDFUA_JOBS") or os.path.join(ROOT, "jobs")
    res = {"files": 0, "human": 0, "confirmed": 0, "corrected": 0, "skipped": 0}
    names = []
    try:
        names = sorted(n for n in os.listdir(jobs_dir) if os.path.isfile(os.path.join(jobs_dir, n, "meta.json")))
    except OSError:
        pass
    for i, name in enumerate(names):
        if progress:
            progress(i, len(names))
        d = os.path.join(jobs_dir, name)
        try:
            with open(os.path.join(d, "meta.json"), encoding="utf-8") as fh:
                meta = json.load(fh)
            if meta.get("stage") not in ("review", "done") or not os.path.exists(os.path.join(d, "model.json")):
                res["skipped"] += 1
                continue
            model = load_model(d)
            auto = None
            ap = os.path.join(d, "auto_model.json")
            if os.path.exists(ap):
                with open(ap, encoding="utf-8") as fh:
                    auto = DocModel.from_json(fh.read())
            client = Options.from_dict(meta.get("options")).client or meta.get("profile") or ""
            r = learn_job(d, client, model, auto, verified=(meta.get("work") or "") == "approved")
            res["files"] += 1
            for k in ("human", "confirmed", "corrected"):
                res[k] += int(r.get(k) or 0)
        except Exception:
            res["skipped"] += 1
    if progress:
        progress(len(names), len(names))
    res["students"] = train_all(force=True)
    return res


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    try:
        from . import netguard
        netguard.install(process="teacher")
    except Exception:
        pass
    if cmd == "train":
        r = train_all(force="--force" in argv)
    elif cmd == "harvest":
        r = harvest_jobs(progress=lambda d, t: print(f"@@P {d} {t}", flush=True))
    elif cmd == "forget" and len(argv) > 1:
        r = {"deleted": forget(argv[1], argv[2] if len(argv) > 2 else "all")}
    elif cmd == "status":
        s = status()
        r = {"totals": s["totals"], "tasks": [{k: t[k] for k in ("task", "level", "lessons", "dez", "ollama", "student")} for t in s["tasks"]]}
    else:
        print("usage: python -m core.teacher status | train [--force] | harvest | forget <task> [all|teacher|corrected]", file=sys.stderr)
        return 2
    print(json.dumps(r, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, ROOT)
    sys.exit(main(sys.argv[1:]))
