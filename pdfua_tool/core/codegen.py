"""The learning engine writes its own Python code (V4.26).

People correct the tool: a paragraph becomes an H3, a running foot becomes
an artifact. The brain already weighs such fixes. The learning engine goes
one step further: it finds the *pattern* behind a set of fixes — "set in
Rockwell, 1.3–1.5× the body, bold, at most 9 words" — and writes it down as
a small Python module, a **learned rule**:

    \"\"\"Learned rule R0003 — written by the learning engine …\"\"\"
    RULE_ID = "R0003"
    TARGET = "H3"
    FROM = ("P",)
    CLIENT = "Journal X"

    def match(f):
        return (f["font"] == 'rockwell' and 1.3 <= f["size_rel"] <= 1.5
                and f["bold"] == 1 and f["words"] <= 9)

How a rule is made, checked and used:

1. **Mined** from the fixes (approved files and built files) and the tags
   of every finished sample: for each kind of fix the engine looks for the
   few conditions (font, size, weight, capitals, length, place on the page,
   the first word's shape, the wording, the client, the document type)
   that pick out the fixed tags and as few others as possible
   (sequential covering — a small, readable rule induction). When that
   fails and the local AI is on, the AI is asked to propose the conditions
   (as data, never as code) from examples and counter-examples.
2. **Written** as Python by the engine's own code generator — every value
   goes in through repr(), so the source is always well formed.
3. **Checked** before it may run: the source is parsed and every node of it
   must be on a short whitelist (comparisons, and/or/not, f["…"] with a
   known feature name, constants, three string helpers) — no imports, no
   attributes, no loops, no calls to anything else. It is compiled with no
   builtins at all.
4. **Tested** on the whole database: its precision (of all finished tags it
   matches, how many really are the target) and how many fixes it explains.
   Only a rule that is right ≥ 93 % of the time on enough *verified* tags
   becomes **active**; others stay **candidates**.
5. **Used** by the brain as one more voice ("code", weighed by its
   precision and its trust) — an artifact rule turns running heads into
   artifacts directly. Every use is recorded.
6. **Kept honest**: every built and approved file tells whether each rule
   was right; a rule that is wrong too often is **retired** by itself. The
   admin sees every rule, its code, its numbers, and can switch it off.

The code lives in the sample database (and, for reading, as .py files in
sample_files/learned_code/), so it survives updates and is in the backup.

V4.27:

* one kind of fix in several looks (the chapter titles and the section heads of a
  book are both H2, in two fonts) gets **one tight rule for each look** instead of
  one broad rule that stretches over everything between them; a broad rule written
  by an older version is retired when it is replaced
* a rule is switched on only when it has been **seen to work beyond one place**:
  fixes from two files (or ten in one), and in no file where it fires three times
  or more right less than 75 % of the time. What waits says why (`note`); a rule
  that is already on and unchanged keeps working.
"""
from __future__ import annotations

import ast
import collections
import hashlib
import json
import math
import os
import re
import sqlite3
import time
from datetime import datetime

from . import learn as L

SCHEMA = """
CREATE TABLE IF NOT EXISTS code_rules (
  id TEXT PRIMARY KEY, slot TEXT UNIQUE, version INTEGER DEFAULT 1, created TEXT, updated TEXT,
  status TEXT, target TEXT, from_roles TEXT, client TEXT, conditions TEXT, source TEXT, sha TEXT,
  origin TEXT, positives INTEGER, verified_pos INTEGER, matched INTEGER, verified_matched INTEGER,
  precision REAL, description TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS rule_hits (job TEXT, rule TEXT, node TEXT, outcome TEXT, verified INTEGER, created TEXT,
  PRIMARY KEY (job, rule, node));
CREATE INDEX IF NOT EXISTS rule_hits_rule ON rule_hits(rule);
"""

NUMERIC = ("size_rel", "bold", "italic", "lines", "words", "x", "y", "w", "centered", "starts_num", "caps",
           "ends_stop", "first_page", "cap_word")
STRINGS = ("font", "text", "norm", "shape", "client", "doc_type", "role")
FEATURES = set(NUMERIC) | set(STRINGS)
BINARY = ("bold", "italic", "centered", "starts_num", "ends_stop", "first_page", "cap_word")
OPS = ("==", "!=", "<=", ">=", "between", "in", "starts", "ends", "has")
TARGETS = L.SWAP | {"Artifact"}
HEADINGS = L.HEADINGS
ACTIVE_PRECISION = 0.93
MIN_POSITIVES = 3
# V4.27: a rule is switched on only when it has been seen to work beyond one place
MIN_FILES = 2             # fixes from at least this many files …
SOLO_POSITIVES = 10       # … or this many fixes in one file (a convention, not a slip)
MIN_FILE_PRECISION = 0.75 # and in no file where it fires at least 3 times may it be right less often than this
MAX_LOOKS = 4             # rules written for the different looks of one kind of fix


def ensure(c: sqlite3.Connection) -> None:
    c.executescript(SCHEMA)
    have = {r[1] for r in c.execute("PRAGMA table_info(code_rules)")}
    for col in ("files INTEGER DEFAULT 0", "min_file_precision REAL"):      # V4.27: seen to work beyond one file
        if col.split()[0] not in have:
            try:
                c.execute(f"ALTER TABLE code_rules ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def code_dir() -> str:
    return os.path.join(L.samples_dir(), "learned_code")


# ================================================================ helpers the rules may call
def _starts(s, p) -> bool:
    return isinstance(s, str) and isinstance(p, str) and s.lower().startswith(p.lower())


def _ends(s, p) -> bool:
    return isinstance(s, str) and isinstance(p, str) and s.lower().endswith(p.lower())


def _has(s, p) -> bool:
    return isinstance(s, str) and isinstance(p, str) and p.lower() in s.lower()


HELPERS = {"starts": _starts, "ends": _ends, "has": _has}
TOP_NAMES = {"RULE_ID", "TARGET", "FROM", "CLIENT", "PRECISION", "VERSION"}


# ================================================================ the feature dict a rule sees
def shape(text: str) -> str:
    first = (text or "").split()[0] if (text or "").split() else ""
    s = re.sub(r"[A-Z]", "A", first)
    s = re.sub(r"[a-z]", "a", s)
    s = re.sub(r"\d", "9", s)
    return re.sub(r"(.)\1+", r"\1", s)[:6]


def features(f: dict, text: str, client: str = "", doc_type: str = "", role: str = "") -> dict:
    out = {k: (float(f.get(k) or 0) if k in ("size_rel", "x", "y", "w", "caps") else int(f.get(k) or 0))
           for k in NUMERIC}
    t = " ".join((text or "").split())
    out.update(font=(f.get("font") or "")[:40], text=t[:300], norm=L.norm(t) if len(t.split()) <= 14 else "",
               shape=shape(t), client=client or "", doc_type=doc_type or "", role=role or "")
    return out


# ================================================================ writing the code
def _lit(c: dict) -> str:
    k, op, v = c["f"], c["op"], c["v"]
    if k not in FEATURES or op not in OPS:
        raise ValueError(f"bad condition {c}")
    fk = f"f[{k!r}]"
    if op == "between":
        lo, hi = (round(float(v[0]), 3), round(float(v[1]), 3))
        return f"{lo!r} <= {fk} <= {hi!r}"
    if op == "in":
        vals = tuple(str(x)[:120] for x in v)[:40]
        return f"{fk} in {vals!r}"
    if op in ("starts", "ends", "has"):
        return f"{op}({fk}, {str(v)[:120]!r})"
    if k in NUMERIC:
        v = round(float(v), 3)
        if k not in ("size_rel", "x", "y", "w", "caps"):
            v = int(v)
    else:
        v = str(v)[:120]
    return f"{fk} {op} {v!r}"


def describe(c: dict) -> str:
    k, op, v = c["f"], c["op"], c["v"]
    names = {"size_rel": "size × body", "words": "words", "lines": "lines", "caps": "capitals", "y": "place down the page",
             "x": "place across", "w": "width", "font": "font", "norm": "wording", "text": "text", "shape": "first word",
             "client": "client", "doc_type": "document", "bold": "bold", "italic": "italic", "centered": "centred",
             "starts_num": "starts with a number", "ends_stop": "ends with a stop", "first_page": "on page 1",
             "cap_word": "starts with Figure/Table…", "role": "proposed tag"}
    n = names.get(k, k)
    if k in BINARY and op == "==":
        return n if int(v) else f"not {n}"
    if op == "between":
        return f"{n} {v[0]:g}–{v[1]:g}"
    if op == "in":
        vals = list(v)
        return f"{n} one of " + ", ".join(f"“{x}”" for x in vals[:4]) + (f" (+{len(vals) - 4})" if len(vals) > 4 else "")
    if op in ("starts", "ends", "has"):
        return f"{n} {op} “{v}”"
    if k == "caps" and op == ">=":
        return "in capitals"
    if k == "shape" and op == "==":
        return f"first word shaped “{v}” (A capital, a small letter, 9 digit)"
    return f"{n} {op} {v}"


def write_source(rid: str, target: str, from_roles: list[str], client: str, conds: list[dict], meta: dict) -> str:
    """The Python module of one learned rule — written here, never by a model."""
    lits = [_lit(c) for c in conds] or ["True"]
    body = "\n            and ".join(lits)
    what = ", ".join(describe(c) for c in conds) or "always"
    doc = (f"Learned rule {rid} (version {meta.get('version', 1)}) — written by the learning engine, {_now()[:16].replace('T', ' ')}.\n\n"
           f"What it does: a tag the tool proposed as {' or '.join(from_roles)}"
           f"{' in a file of ' + client if client else ''} becomes {target} when: {what}.\n"
           f"Learned from: {meta.get('positives', 0)} fix(es) people made ({meta.get('verified_pos', 0)} in approved files)"
           f"{', proposed by the local AI' if meta.get('origin') == 'ai' else ''}.\n"
           f"Checked on: {meta.get('matched', 0)} tag(s) it matches — {round(100 * float(meta.get('precision') or 0), 1)} % "
           f"of them are {target}.")
    doc = doc.replace('"""', "'''").replace("\\", "/")
    return (f'"""{doc}\n"""\n'
            f"RULE_ID = {rid!r}\n"
            f"VERSION = {int(meta.get('version', 1))!r}\n"
            f"TARGET = {target!r}\n"
            f"FROM = {tuple(from_roles)!r}\n"
            f"CLIENT = {client!r}\n"
            f"PRECISION = {round(float(meta.get('precision') or 0), 4)!r}\n\n\n"
            f"def match(f):\n"
            f"    return (\n            {body})\n")


# ================================================================ checking the code
class UnsafeCode(ValueError):
    pass


_ALLOWED = (ast.Module, ast.Expr, ast.Assign, ast.Name, ast.Load, ast.Store, ast.Constant, ast.Tuple, ast.List,
            ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
            ast.USub, ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
            ast.Subscript, ast.Call, ast.IfExp)


def validate(source: str) -> ast.Module:
    """Every node of the rule must be on the whitelist (see the module doc)."""
    if len(source) > 20000:
        raise UnsafeCode("the rule is too long")
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise UnsafeCode(f"not Python: {exc}")
    funcs = 0
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise UnsafeCode(f"{type(node).__name__} is not allowed in a learned rule")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, str, bool, type(None))):
            raise UnsafeCode("only numbers, text and True/False/None may be constants")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) > 2000:
            raise UnsafeCode("a text constant is too long")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise UnsafeCode("no dunder names")
        if isinstance(node, ast.FunctionDef):
            funcs += 1
            a = node.args
            if (node.name != "match" or node.decorator_list or len(a.args) != 1 or a.args[0].arg != "f"
                    or a.vararg or a.kwarg or a.kwonlyargs or a.defaults or a.kw_defaults or a.posonlyargs
                    or node.returns is not None):
                raise UnsafeCode("only one function: def match(f)")
        if isinstance(node, ast.Subscript):
            if not (isinstance(node.value, ast.Name) and node.value.id == "f" and isinstance(node.slice, ast.Constant)
                    and node.slice.value in FEATURES):
                raise UnsafeCode("only f[\"<feature>\"] may be read")
        if isinstance(node, ast.Call):
            if not (isinstance(node.func, ast.Name) and node.func.id in HELPERS) or node.keywords or len(node.args) != 2:
                raise UnsafeCode("only starts(), ends() and has() may be called")
    if funcs != 1:
        raise UnsafeCode("the rule must define match(f) once")
    for st in tree.body:
        if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str):
            continue
        if isinstance(st, ast.Assign):
            if len(st.targets) != 1 or not isinstance(st.targets[0], ast.Name) or st.targets[0].id not in TOP_NAMES:
                raise UnsafeCode("only RULE_ID, VERSION, TARGET, FROM, CLIENT, PRECISION may be set")
            continue
        if isinstance(st, ast.FunctionDef):
            for b in st.body:
                if not (isinstance(b, ast.Return) or (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant))):
                    raise UnsafeCode("match(f) may only return an expression")
            continue
        raise UnsafeCode(f"{type(st).__name__} is not allowed at the top of a rule")
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    if names - (TOP_NAMES | {"f", "match"} | set(HELPERS)):
        raise UnsafeCode("unknown names: " + ", ".join(sorted(names - (TOP_NAMES | {"f", "match"} | set(HELPERS)))))
    return tree


_COMPILED: dict[str, dict] = {}


def compile_rule(source: str) -> dict:
    """Checked, then compiled with no builtins at all. -> its namespace."""
    sha = hashlib.sha256(source.encode()).hexdigest()
    hit = _COMPILED.get(sha)
    if hit is not None:
        return hit
    tree = validate(source)
    ns: dict = {"__builtins__": {}, **HELPERS}
    exec(compile(tree, "<learned rule>", "exec"), ns)          # noqa: S102 — whitelisted, no builtins
    if not callable(ns.get("match")):
        raise UnsafeCode("no match(f)")
    out = {"match": ns["match"], "id": ns.get("RULE_ID"), "target": ns.get("TARGET"),
           "from": tuple(ns.get("FROM") or ()), "client": ns.get("CLIENT") or "",
           "precision": float(ns.get("PRECISION") or 0), "sha": sha}
    _COMPILED[sha] = out
    return out


def run(rule: dict, fd: dict) -> bool:
    try:
        return bool(rule["match"](fd))
    except Exception:
        return False


# ================================================================ mining
def _examples(c: sqlite3.Connection) -> tuple[list[dict], list[dict]]:
    """(finished tags, fixes) as rule feature dicts."""
    finished, fixes = [], []
    docs = {r["sample_id"]: r["doc_type"] or "" for r in c.execute("SELECT sample_id, doc_type FROM docs")}
    q = ("SELECT b.*, s.client AS sclient, s.kind AS skind FROM blocks b JOIN samples s ON s.id=b.sample_id "
         "WHERE s.enabled=1 AND s.status='ready' LIMIT 120000")
    for r in c.execute(q):
        d = dict(r)
        if d["role"] not in L.TEXT_ROLES or d["role"] == "H":
            continue
        fd = features(d, d["text"], d["sclient"] or "", docs.get(d["sample_id"], ""), d["role"])
        fd["_truth"], fd["_src"] = d["role"], d["sample_id"]
        finished.append(fd)
    q = ("SELECT c.*, s.client AS sclient FROM corrections c JOIN samples s ON s.id=c.sample_id "
         "WHERE s.enabled=1 AND s.status='ready'")
    for r in c.execute(q):
        d = dict(r)
        fd = features(d, d["text"], d["sclient"] or "", docs.get(d["sample_id"], ""), d["from_role"])
        fd["_truth"], fd["_src"], fd["_verified"] = d["role"], d["sample_id"], 1
        fixes.append(fd)
    for r in c.execute("SELECT * FROM feedback"):
        d = dict(r)
        fd = features(d, d["text"], d["client"] or "", "", d["from_role"])
        fd["_truth"], fd["_src"], fd["_verified"] = d["role"], "fb:" + str(d["job"]), 0
        fixes.append(fd)
    return finished, fixes


class Table:
    """Examples as columns (numpy), so a condition is tested on 100 000 tags at once."""

    def __init__(self, rows: list[dict]):
        import numpy as np
        self.np = np
        self.n = len(rows)
        self.rows = rows
        self.col = {}
        for k in NUMERIC:
            self.col[k] = np.asarray([float(r[k]) for r in rows], dtype=float)
        for k in STRINGS:
            self.col[k] = np.asarray([r[k] for r in rows], dtype=object)
        self.truth = np.asarray([r["_truth"] for r in rows], dtype=object)
        self.src = np.asarray([r.get("_src", "") for r in rows], dtype=object)
        self.verified = np.asarray([int(r.get("_verified", 1)) for r in rows], dtype=bool)
        self._cache: dict[str, object] = {}

    def mask(self, c: dict):
        key = json.dumps(c, sort_keys=True)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        np = self.np
        k, op, v = c["f"], c["op"], c["v"]
        col = self.col[k]
        if self.n == 0:
            m = np.zeros(0, dtype=bool)
        elif op == "between":
            m = (col >= float(v[0])) & (col <= float(v[1]))
        elif op == "in":
            m = np.isin(col, list(v))
        elif op in ("starts", "ends", "has"):
            fn = HELPERS[op]
            m = np.asarray([fn(x, v) for x in col], dtype=bool)
        elif op == "==":
            m = col == (float(v) if k in NUMERIC else v)
        elif op == "!=":
            m = col != (float(v) if k in NUMERIC else v)
        elif op == "<=":
            m = col <= float(v)
        else:
            m = col >= float(v)
        m = np.asarray(m, dtype=bool)
        self._cache[key] = m
        return m

    def all(self, conds: list[dict]):
        m = self.np.ones(self.n, dtype=bool)
        for c in conds:
            m &= self.mask(c)
        return m


def _test(conds: list[dict], target: str, from_roles: set, fin: "Table", fix: "Table",
          only=None) -> dict:
    """How a set of conditions does on everything the database holds.

    `only` (V4.27): a mask over the fixes — the positives of one look. The fixes of the same kind
    that belong to another look are then neither right nor wrong for this rule: they are left out."""
    np = fin.np
    mf = fin.all(conds) if fin.n else np.zeros(0, dtype=bool)
    mx = fix.all(conds) if fix.n else np.zeros(0, dtype=bool)
    from_m = np.isin(fix.col["role"], list(from_roles)) if fix.n else np.zeros(0, dtype=bool)
    is_t = fix.truth == target if fix.n else np.zeros(0, dtype=bool)
    pos = from_m & is_t
    if only is not None and fix.n:
        pos = pos & only
    hit_pos = mx & pos
    hit_bad_fix = mx & from_m & ~is_t
    hit_fin = int(mf.sum())
    good_fin = 0 if target == "Artifact" or not fin.n else int((mf & (fin.truth == target)).sum())
    good = int(hit_pos.sum()) + good_fin
    matched = int(hit_pos.sum()) + hit_fin + int(hit_bad_fix.sum())
    ver_pos = int((hit_pos & fix.verified).sum()) if fix.n else 0
    return {"precision": good / matched if matched else 0.0, "matched": matched, "covered": int(hit_pos.sum()),
            "positives": int(pos.sum()), "verified_pos": ver_pos, "verified_matched": hit_fin + ver_pos,
            "bad": hit_fin - good_fin + int(hit_bad_fix.sum())}


def _candidates(pos: list[dict]) -> list[dict]:
    """Conditions every (or nearly every) fixed tag of one kind shares."""
    out: list[dict] = []
    n = len(pos)

    def share(fn):
        return sum(1 for x in pos if fn(x)) / n
    for k in BINARY:
        for v in (0, 1):
            if share(lambda x: x[k] == v) >= 0.95:
                out.append({"f": k, "op": "==", "v": v})
    sizes = sorted(x["size_rel"] for x in pos)
    lo, hi = sizes[int(0.05 * (n - 1))], sizes[int(math.ceil(0.95 * (n - 1)))]
    out.append({"f": "size_rel", "op": "between", "v": [round(lo - 0.06, 2), round(hi + 0.06, 2)]})
    if share(lambda x: x["caps"] >= 0.8) >= 0.95:
        out.append({"f": "caps", "op": ">=", "v": 0.8})
    if share(lambda x: x["caps"] <= 0.3) >= 0.95:
        out.append({"f": "caps", "op": "<=", "v": 0.3})
    for k, pad in (("words", 2), ("lines", 1)):
        out.append({"f": k, "op": "<=", "v": max(x[k] for x in pos) + pad})
    ys = [x["y"] for x in pos]
    if min(ys) >= 0.8:
        out.append({"f": "y", "op": ">=", "v": round(min(ys) - 0.03, 2)})
    if max(ys) <= 0.2:
        out.append({"f": "y", "op": "<=", "v": round(max(ys) + 0.03, 2)})
    for k in ("font", "shape", "doc_type"):
        cnt = collections.Counter(x[k] for x in pos if x[k])
        if cnt:
            v, m = cnt.most_common(1)[0]
            if m / n >= 0.9 and not (k == "doc_type" and v == "document"):
                out.append({"f": k, "op": "==", "v": v})
    norms = collections.Counter(x["norm"] for x in pos if x["norm"])
    if norms and sum(norms.values()) / n >= 0.8 and len(norms) <= 12:
        out.append({"f": "norm", "op": "in", "v": sorted(norms)})
    firsts = collections.Counter((x["text"].split() or [""])[0].lower().strip(".:") for x in pos)
    if firsts:
        w, m = firsts.most_common(1)[0]
        if w and m / n >= 0.9 and len(w) >= 3 and not w.isdigit():
            out.append({"f": "text", "op": "starts", "v": w})
    return out


def induce(target: str, from_roles: set, pos: list[dict], fin: "Table", fix: "Table",
           must: list[dict] | None = None, only=None) -> tuple[list[dict], dict] | None:
    """Sequential covering, greedy: add the condition that makes the rule
    most precise while it still explains ≥ 75 % of the fixes."""
    cands = _candidates(pos)
    need_cover = max(MIN_POSITIVES, int(math.ceil(0.75 * len(pos))))

    def greedy(pool):
        chosen = list(must or [])
        best = _test(chosen, target, from_roles, fin, fix, only) if chosen else {"precision": 0.0}
        for _ in range(7):
            top = None
            for c_ in pool:
                if c_ in chosen:
                    continue
                r = _test(chosen + [c_], target, from_roles, fin, fix, only)
                if r["covered"] < need_cover:
                    continue
                # as precise as possible; then explaining the most fixes; then the
                # most finished tags of the target backing it (a general rule)
                key = (round(r["precision"], 4), r["covered"], r["matched"] - r["bad"])
                if top is None or key > top[0]:
                    top = (key, c_, r)
            if top is None or top[2]["precision"] <= best["precision"] + 0.004:
                break
            chosen.append(top[1])
            best = top[2]
            if best["precision"] >= 0.985:
                break
        return chosen, best
    # a text tag: by its look first — wording only when the look cannot tell
    wordy = {"norm", "text"}
    chosen, best = greedy([c_ for c_ in cands if c_["f"] not in wordy] if target != "Artifact" else cands)
    if target != "Artifact" and best.get("precision", 0) < ACTIVE_PRECISION:
        c2, b2 = greedy(cands)
        if b2.get("precision", 0) > best.get("precision", 0):
            chosen, best = c2, b2
    if not chosen or best.get("covered", 0) < MIN_POSITIVES:
        return None
    # specialise to the evidence: a rule only reaches as far as the fixes it
    # was learned from — every look the fixed tags share is part of it (a
    # precise rule with no counter-examples nearby could otherwise be far too
    # broad: "anything at the foot of a page is an artifact")
    keep = {"size_rel", "words", "lines", "bold", "italic", "caps", "font"}
    if target == "Artifact":
        keep |= {"y", "text", "norm", "shape"}
    for c_ in cands:
        if c_ in chosen or c_["f"] not in keep:
            continue
        r = _test(chosen + [c_], target, from_roles, fin, fix, only)
        if r["covered"] >= best["covered"] and r["precision"] >= best["precision"] - 0.001:
            chosen.append(c_)
            best = r
    # a heading is never long (PDF/UA, and the brain's own guard rails)
    if target in HEADINGS and not any(c_["f"] == "words" for c_ in chosen):
        chosen.append({"f": "words", "op": "<=", "v": 22})
        best = _test(chosen, target, from_roles, fin, fix, only)
    return chosen, best


def agrees(source: str, conds: list[dict], tables: list["Table"], sample: int = 3000) -> bool:
    """The written code does exactly what the conditions say (checked on real tags)."""
    rule = compile_rule(source)
    for t in tables:
        if not t.n:
            continue
        m = t.all(conds)
        step = max(1, t.n // sample)
        for i in range(0, t.n, step):
            if run(rule, t.rows[i]) != bool(m[i]):
                return False
    return True


def per_file(conds: list[dict], target: str, from_roles: set, fin: "Table", fix: "Table", only=None) -> dict:
    """How a set of conditions does in each file it fires in: {file: [right, fired]} (V4.27).

    The overall precision can hide a file where the rule is plain wrong (100 % in a
    book with twenty fixes, 0 % in the leaflet with three)."""
    out: dict = collections.defaultdict(lambda: [0, 0])
    np = fix.np
    if fix.n:
        hit = fix.all(conds) & np.isin(fix.col["role"], list(from_roles))
        if only is not None:                      # the fixes of another look are not this rule's business
            hit = hit & (only | (fix.truth != target))
        good = hit & (fix.truth == target)
        for src, g in zip(fix.src[hit], good[hit]):
            out[src][1] += 1
            out[src][0] += int(g)
    if fin.n:
        hit = fin.all(conds)
        good = hit & (fin.truth == target) if target != "Artifact" else np.zeros(fin.n, dtype=bool)
        for src, g in zip(fin.src[hit], good[hit]):
            out[src][1] += 1
            out[src][0] += int(g)
    return dict(out)


def file_stats(pf: dict) -> tuple[int, float | None]:
    """(files where it fired rightly at least once, its worst precision in a file it fired in 3+ times)."""
    files = sum(1 for g, n in pf.values() if g >= 1)
    ratios = [g / n for g, n in pf.values() if n >= 3]
    return files, (round(min(ratios), 4) if ratios else None)


def waits_for(res: dict, extra: dict) -> str:
    """'' when a rule has been seen to work beyond one place; else why it stays a candidate."""
    mfp = extra.get("min_file_precision")
    if mfp is not None and mfp < MIN_FILE_PRECISION:
        return f"wrong too often in one of its files ({round(mfp * 100)} % right there)"
    if int(extra.get("files") or 0) < MIN_FILES and res["covered"] < SOLO_POSITIVES:
        return f"learned from one file only — waits for a second file (or {SOLO_POSITIVES} fixes)"
    return ""


def look_clusters(pos: list[dict]) -> list[tuple[str, list[dict]]]:
    """The fixes of one kind, grouped by how the fixed tags look: [(look key, fixes)] (V4.27).

    One kind of fix can come in several looks — the chapter titles *and* the section heads of a
    book both become H2, one in a bold slab serif, the other in plain capitals. A single rule
    would have to stretch over everything between the two (a size range from 0.9× to 1.9× takes in
    every short line of the book), so each look — font, weight, capitals — gets a rule of its own,
    as tight as its own fixes. The key does not depend on the sizes, so the same fixes always give
    the same key and the rule keeps its slot when the engine writes it again."""
    buckets: dict = collections.defaultdict(list)
    for x in pos:
        buckets[(x["font"] or "", int(x["bold"]), 1 if x["caps"] >= 0.8 else 0)].append(x)
    out = []
    for (font, bold, caps), members in sorted(buckets.items(), key=lambda t: (-len(t[1]), t[0])):
        if len(members) >= MIN_POSITIVES:
            out.append((f"{font or '*'}/{'b' if bold else 'r'}{'C' if caps else ''}", members))
    return out[:MAX_LOOKS]


def supersede(c, slot: str, by: list[str]) -> bool:
    """A broad rule written before its fixes were seen to come in several looks is replaced by the
    narrower rules of each look (unless a person already switched it off)."""
    cur = c.execute("UPDATE code_rules SET status='retired', note=?, updated=? WHERE slot=? AND status IN "
                    "('active', 'candidate')", ("replaced by narrower rules for each look: " + ", ".join(by),
                                               _now(), slot))
    return bool(cur.rowcount)


# ================================================================ the store
def _connect():
    c = L.connect()
    ensure(c)
    return c


def _next_id(c) -> str:
    n = 0
    for r in c.execute("SELECT id FROM code_rules"):
        try:
            n = max(n, int(r["id"][1:]))
        except ValueError:
            pass
    return f"R{n + 1:04d}"


def _export(rule_id: str, source: str | None) -> None:
    os.makedirs(code_dir(), exist_ok=True)
    p = os.path.join(code_dir(), rule_id + ".py")
    if source is None:
        if os.path.exists(p):
            os.remove(p)
        return
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(source)


def _save(c, slot: str, target: str, from_roles: list[str], client: str, conds: list[dict], res: dict,
          origin: str, extra: dict | None = None) -> tuple[str, str]:
    extra = extra or {}
    old = c.execute("SELECT * FROM code_rules WHERE slot=?", (slot,)).fetchone()
    same_conds = old is not None and json.loads(old["conditions"] or "[]") == conds
    good = (res["precision"] >= ACTIVE_PRECISION and res["verified_matched"] >= 5 and res["covered"] >= MIN_POSITIVES)
    why = waits_for(res, extra) if good else ""
    # V4.27: a rule that is already on and unchanged keeps working (the new checks are for new rules)
    status = "active" if (good and (not why or (same_conds and old["status"] == "active"))) else "candidate"
    note = why if (good and status == "candidate") else ""
    if old is not None and old["status"] in ("disabled", "retired"):
        status = old["status"]                               # a person or the outcome said no — stays so
        note = old["note"] or ""
    meta = {"positives": res["covered"], "verified_pos": res["verified_pos"], "matched": res["matched"],
            "precision": res["precision"], "origin": origin}
    files, mfp = int(extra.get("files") or 0), extra.get("min_file_precision")
    if old is not None:
        if same_conds and old["status"] == status:
            # (a rule that is on keeps its note — "set by the admin"; a candidate says what it waits for)
            keep = note if status == "candidate" else (old["note"] or "")
            c.execute("UPDATE code_rules SET positives=?, verified_pos=?, matched=?, verified_matched=?, precision=?, "
                      "updated=?, files=?, min_file_precision=?, note=? WHERE id=?",
                      (res["covered"], res["verified_pos"], res["matched"], res["verified_matched"], res["precision"],
                       _now(), files, mfp, keep, old["id"]))
            return old["id"], "same"
        rid, ver = old["id"], int(old["version"] or 1) + 1
    else:
        rid, ver = _next_id(c), 1
    meta["version"] = ver
    src = write_source(rid, target, from_roles, client, conds, meta)
    rule = compile_rule(src)                                  # never stored unless it passes the checks
    desc = ", ".join(describe(x) for x in conds)
    row = (rid, slot, ver, old["created"] if old is not None else _now(), _now(), status, target, json.dumps(from_roles),
           client, json.dumps(conds), src, rule["sha"], origin, res["covered"], res["verified_pos"], res["matched"],
           res["verified_matched"], res["precision"], desc, note, files, mfp)
    c.execute("INSERT OR REPLACE INTO code_rules (id, slot, version, created, updated, status, target, from_roles, "
              "client, conditions, source, sha, origin, positives, verified_pos, matched, verified_matched, precision, "
              "description, note, files, min_file_precision) VALUES (" + ",".join("?" * 22) + ")", row)
    _export(rid, src)
    return rid, "new" if old is None else "updated"


def _mine(c, slot: str, target: str, from_roles: list[str], client: str, pos: list[dict], finished: list[dict],
          fin: "Table", fix: "Table", must: list[dict], ai_cfg, out: dict, allow_ai: bool = True,
          only_these: bool = False):
    """Find, write, check and store the rule for one set of fixes.
    `only_these`: `pos` is one look of a kind of fix — the other looks are left out of its tests.
    -> None, or (rule id, how, the compiled rule)."""
    only = None
    if only_these and fix.n:
        ids = {id(x) for x in pos}
        only = fix.np.asarray([id(r) in ids for r in fix.rows], dtype=bool)
    res = induce(target, set(from_roles), pos, fin, fix, must, only)
    origin = "induction"
    if allow_ai and (res is None or res[1]["precision"] < 0.9) and ai_cfg is not None \
            and getattr(ai_cfg, "enabled", False):
        ai = _ask_ai(ai_cfg, target, from_roles, client, pos, finished)
        if ai:
            r2 = _test(must + ai, target, set(from_roles), fin, fix, only)
            if r2["covered"] >= MIN_POSITIVES and (res is None or r2["precision"] > res[1]["precision"]):
                res, origin = (must + ai, r2), "ai"
                out["ai"] += 1
    if res is None or res[1]["precision"] < 0.9:
        return None
    conds, r = res
    src = write_source("R0000", target, from_roles, client, conds, {"precision": r["precision"]})
    if not agrees(src, conds, [fin, fix]):
        return None                                # never: the writer and the test disagree
    files, mfp = file_stats(per_file(conds, target, set(from_roles), fin, fix, only))
    rid, how = _save(c, slot, target, from_roles, client, conds, r, origin,
                     {"files": files, "min_file_precision": mfp})
    return rid, how, compile_rule(src)


def synthesize(ai_cfg=None, progress=None) -> dict:
    """Mine every kind of fix, write (or rewrite) the rules, test them."""
    t0 = time.time()
    if not L.has_data():
        return {"ok": False, "error": "no sample database yet"}
    try:
        import numpy  # noqa: F401
    except Exception:
        return {"ok": False, "error": "numpy is not installed"}
    c = _connect()
    out = {"ok": True, "new": 0, "updated": 0, "same": 0, "tried": 0, "failed": 0, "ai": 0, "rules": [], "looks": 0}
    try:
        finished, fixes = _examples(c)
        fin_all, fix_all = Table(finished), Table(fixes)
        by_client_fin: dict[str, Table] = {}
        by_client_fix: dict[str, Table] = {}
        groups: dict[tuple, list] = collections.defaultdict(list)
        for x in fixes:
            if x["_truth"] in TARGETS and x["role"] != x["_truth"]:
                groups[(x["_truth"], x["client"])].append(x)
                groups[(x["_truth"], "")].append(x)
        for (target, client), pos in sorted(groups.items(), key=lambda t: -len(t[1])):
            if len(pos) < MIN_POSITIVES:
                continue
            from_roles = sorted({x["role"] for x in pos if x["role"]})
            if not from_roles:
                continue
            if client and len(groups.get((target, ""), [])) == len(pos):
                continue                        # the general rule says the same
            out["tried"] += 1
            must = [{"f": "client", "op": "==", "v": client}] if client else []
            if client:
                if client not in by_client_fin:
                    by_client_fin[client] = Table([x for x in finished if x["client"] == client])
                    by_client_fix[client] = Table([x for x in fixes if x["client"] == client])
                fin, fix = by_client_fin[client], by_client_fix[client]
            else:
                fin, fix = fin_all, fix_all
            slot = f"{target}|{','.join(from_roles)}|{client}"
            # V4.27: fixes of one kind that come in several looks — one tight rule for each look
            looks = look_clusters(pos)
            made = 0
            if len(looks) >= 2:
                ids = []
                for look, members in looks:
                    sub = _mine(c, f"{slot}|{look}", target, sorted({x["role"] for x in members if x["role"]}),
                                client, members, finished, fin, fix, must, ai_cfg, out, allow_ai=False,
                                only_these=True)
                    if sub is not None:
                        out[sub[1]] = out.get(sub[1], 0) + 1
                        out["rules"].append(sub[0])
                        out["looks"] += 1
                        ids.append(sub[0])
                        made += 1
                if made:
                    supersede(c, slot, ids)
            if not made:
                got = _mine(c, slot, target, from_roles, client, pos, finished, fin, fix, must, ai_cfg, out)
                if got is not None:
                    out[got[1]] = out.get(got[1], 0) + 1
                    out["rules"].append(got[0])
                    made += 1
            if not made:
                out["failed"] += 1
        c.commit()
        retire(c)
        c.commit()
        _ACTIVE["t"] = 0.0
    finally:
        c.close()
    out["seconds"] = round(time.time() - t0, 2)
    return out


AI_RULE_PROMPT = (
    "You help a PDF accessibility tool learn from people's corrections. People changed these text tags from "
    "__FROM__ to __TARGET__ (each line: the text, then how it looks):\n__POS__\n\n"
    "These similar tags were correctly NOT __TARGET__:\n__NEG__\n\n"
    "Propose the few conditions that pick out the corrected tags and not the others. Features you may use: "
    "size_rel (size against the body text), bold (0/1), italic (0/1), caps (share of capitals 0-1), words, lines, "
    "y (0 top - 1 bottom of the page), x, centered (0/1), starts_num (0/1), ends_stop (0/1), font, shape (first word "
    "with letters as A/a and digits as 9). Operators: ==, !=, <=, >=, between (value [low, high]).\n"
    'Reply JSON only: {"conditions": [{"f": "<feature>", "op": "<operator>", "v": <value>}], "why": "<max 15 words>"}'
)


def _ask_ai(cfg, target: str, from_roles: list[str], client: str, pos: list[dict], finished: list[dict]) -> list[dict] | None:
    """The local AI proposes conditions — as data; the engine writes the code."""
    try:
        from .ai import OllamaError, _generate, _json_from
    except Exception:
        return None

    def look(x):
        return (f'"{x["text"][:60]}" — size {x["size_rel"]:.2f}, {"bold" if x["bold"] else "regular"}, '
                f'caps {x["caps"]:.1f}, {x["words"]} words, y {x["y"]:.2f}, font {x["font"] or "?"}, shape {x["shape"]}')
    negs = [x for x in finished if x["_truth"] != target and x["role"] in from_roles][:400]
    if pos:
        cx = {k: sum(p[k] for p in pos) / len(pos) for k in ("size_rel", "bold", "caps", "y")}
        negs.sort(key=lambda x: sum((x[k] - cx[k]) ** 2 for k in cx))
    prompt = (AI_RULE_PROMPT.replace("__FROM__", "/".join(from_roles)).replace("__TARGET__", target)
              .replace("__POS__", "\n".join(look(x) for x in pos[:14]))
              .replace("__NEG__", "\n".join(look(x) for x in negs[:14]) or "(none)"))
    try:
        data = _generate(cfg, "text", {"prompt": prompt, "stream": False, "format": "json",
                                       "options": {"temperature": 0.1}}, task="study")
    except OllamaError:
        return None
    ans = _json_from(re.sub(r"<think>.*?</think>", "", data.get("response") or "", flags=re.S))
    conds = []
    for x in (ans or {}).get("conditions") or [] if isinstance(ans, dict) else []:
        try:
            k, op, v = str(x["f"]), str(x["op"]), x["v"]
            if k not in FEATURES or k in ("text", "norm", "client", "role") or op not in ("==", "!=", "<=", ">=", "between"):
                continue
            if op == "between":
                v = [float(v[0]), float(v[1])]
            elif k in NUMERIC:
                v = float(v)
            else:
                v = str(v)[:60]
            _lit({"f": k, "op": op, "v": v})
            conds.append({"f": k, "op": op, "v": v})
        except Exception:
            continue
    return conds[:6] or None


# ================================================================ using the rules
_ACTIVE = {"t": 0.0, "rules": []}


def active_rules(refresh: bool = False) -> list[dict]:
    if not L.has_data():
        return []
    if not refresh and time.time() - _ACTIVE["t"] < 20:
        return _ACTIVE["rules"]
    out = []
    c = _connect()
    try:
        for r in c.execute("SELECT * FROM code_rules WHERE status='active'"):
            try:
                rule = compile_rule(r["source"])
            except UnsafeCode:
                c.execute("UPDATE code_rules SET status='disabled', note=? WHERE id=?",
                          ("failed the safety check", r["id"]))
                c.commit()
                continue
            rule = dict(rule, id=r["id"], precision=float(r["precision"] or 0), verified=int(r["verified_matched"] or 0),
                        description=r["description"] or "")
            out.append(rule)
    finally:
        c.close()
    _ACTIVE.update(t=time.time(), rules=out)
    return out


def opinions_for(rules: list[dict], fd: dict, clients: set) -> list[dict]:
    """The active rules that fire for one tag."""
    hits = []
    for r in rules:
        if fd["role"] not in r["from"]:
            continue
        if r["client"] and r["client"] not in clients:
            continue
        if run(r, fd):
            hits.append(r)
    return hits


# ================================================================ keeping them honest
def record_hits(job: str, decisions: list[dict], final: dict, verified: bool) -> int:
    """Was each rule that fired on this file right? (`final`: node -> role now)"""
    rows = []
    for d in decisions:
        ops = d.get("opinions") or {}
        v = ops.get("code")
        if not v or len(v) < 3:
            continue
        now = final.get(d["node"])
        if now is None:
            continue
        rows.append((job, v[2], d["node"], "right" if now == v[0] else "wrong", 1 if verified else 0, _now()))
    if not rows:
        return 0
    c = _connect()
    try:
        c.execute("DELETE FROM rule_hits WHERE job=? AND (verified=0 OR ?=1)", (job, 1 if verified else 0))
        c.executemany("INSERT OR REPLACE INTO rule_hits VALUES (?,?,?,?,?,?)", rows)
        c.commit()
        retire(c)
        c.commit()
    finally:
        c.close()
    return len(rows)


def retire(c) -> list[str]:
    """A rule people keep undoing is switched off by itself."""
    gone = []
    for r in c.execute("SELECT rule, SUM(outcome='right') ok, SUM(outcome='wrong') bad FROM rule_hits GROUP BY rule"):
        ok, bad = int(r["ok"] or 0), int(r["bad"] or 0)
        if bad >= 3 and ok / max(1, ok + bad) < 0.75:
            cur = c.execute("UPDATE code_rules SET status='retired', note=?, updated=? WHERE id=? AND status='active'",
                            (f"retired by itself: right {ok}, wrong {bad} in real files", _now(), r["rule"]))
            if cur.rowcount:
                gone.append(r["rule"])
    if gone:
        _ACTIVE["t"] = 0.0
    return gone


def set_status(rid: str, status: str) -> bool:
    if status not in ("active", "disabled", "candidate"):
        return False
    c = _connect()
    try:
        cur = c.execute("UPDATE code_rules SET status=?, updated=?, note=? WHERE id=?",
                        (status, _now(), "set by the admin", rid))
        c.commit()
        _ACTIVE["t"] = 0.0
        return cur.rowcount > 0
    finally:
        c.close()


def delete(rid: str) -> bool:
    c = _connect()
    try:
        cur = c.execute("DELETE FROM code_rules WHERE id=?", (rid,))
        c.execute("DELETE FROM rule_hits WHERE rule=?", (rid,))
        c.commit()
        _export(rid, None)
        _ACTIVE["t"] = 0.0
        return cur.rowcount > 0
    finally:
        c.close()


def listing() -> list[dict]:
    if not L.has_data():
        return []
    c = _connect()
    try:
        hits = {r["rule"]: (int(r["ok"] or 0), int(r["bad"] or 0)) for r in c.execute(
            "SELECT rule, SUM(outcome='right') ok, SUM(outcome='wrong') bad FROM rule_hits GROUP BY rule")}
        out = []
        for r in c.execute("SELECT * FROM code_rules ORDER BY status='active' DESC, precision DESC, id"):
            d = dict(r)
            d["from_roles"] = json.loads(d.get("from_roles") or "[]")
            d["conditions"] = json.loads(d.get("conditions") or "[]")
            d["right"], d["wrong"] = hits.get(d["id"], (0, 0))
            out.append(d)
        return out
    finally:
        c.close()
