"""Sample files: the tool learns how a client tags (V4.20).

An admin uploads sample files for a client (or for every client — "common"):
PDFs tagged the way the client wants them, or files approved in the tool.
Each sample is read in a worker process:

* every text tag (P, H1–H6, Caption, Note, BlockQuote, …) is measured the
  way the analysis measures a paragraph — size against the body text, bold,
  italic, lines, words, where it sits on the page, how it starts and ends —
  and stored with its role in a SQLite database (sample_files/samples.db)
* the wording of short tags ("Abstract", "Key messages", "Table #") and the
  role each one got
* the running heads and feet (the text outside the structure tree, at the top
  or the foot of a page): artifacts
* the shape of the tree: which tag holds which (Table > Caption, LI > Lbl …),
  the client's own tag names (RoleMap)

When a file is analysed (core/pipeline.prepare), `apply_samples` uses what
was learned for the file's client and the common samples:

* a tag the analysis was not sure of (a heading by size, a short bold line,
  a caption, a note — confidence under 0.85) takes the role most like it
  in the samples, when the samples agree (≥ 70 %)
* a short paragraph whose wording the client's samples tag the same way
  every time ("Key messages" → H2) takes that role; a short paragraph that
  looks exactly like the client's headings becomes one (≥ 90 %)
* a paragraph at the top or foot of a page printing a running head the
  samples print (digits ignored) becomes an artifact (Header / Footer)
* heading levels never jump down by more than one; a caption is made only
  next to a Figure or Table; hand edits are never touched

With no sample for a client (and no common sample) nothing changes: the
analysis is exactly as before. Every change carries a note ("samples: …")
and the editor's popover shows what the samples say about any tag.
"""
from __future__ import annotations

import collections
import json
import math
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEXT_ROLES = {"P", "H", "H1", "H2", "H3", "H4", "H5", "H6", "Title", "Caption", "Note", "BlockQuote",
              "Quote", "Code", "BibEntry", "TOCI", "Formula"}
HEADINGS = {"H1", "H2", "H3", "H4", "H5", "H6"}
# the roles a learned vote may move a tag between
SWAP = HEADINGS | {"P", "Caption", "BlockQuote"}
CAPTION_WORDS = re.compile(r"^\s*(fig(?:ure)?|table|tab\.|plate|scheme|chart|exhibit|box|map|photo|diagram)\b", re.I)


def samples_dir() -> str:
    return os.environ.get("PDFUA_SAMPLES") or os.path.join(ROOT, "sample_files")


def db_path() -> str:
    return os.path.join(samples_dir(), "samples.db")


SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  id TEXT PRIMARY KEY, client TEXT NOT NULL DEFAULT '', filename TEXT, kind TEXT, job TEXT,
  uploaded_by TEXT, created TEXT, learned TEXT, status TEXT, error TEXT, pages INTEGER DEFAULT 0,
  tags INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1, stats TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS blocks (
  sample_id TEXT, client TEXT, role TEXT, orig TEXT, parent TEXT, text TEXT, norm TEXT,
  size_rel REAL, bold INTEGER, italic INTEGER, lines INTEGER, words INTEGER,
  x REAL, y REAL, w REAL, centered INTEGER, starts_num INTEGER, caps REAL, ends_stop INTEGER,
  first_page INTEGER, cap_word INTEGER);
CREATE INDEX IF NOT EXISTS blocks_client ON blocks(client);
CREATE INDEX IF NOT EXISTS blocks_sample ON blocks(sample_id);
CREATE TABLE IF NOT EXISTS artifacts (sample_id TEXT, client TEXT, pattern TEXT, band TEXT, n INTEGER);
CREATE TABLE IF NOT EXISTS structure (sample_id TEXT, client TEXT, parent TEXT, child TEXT, n INTEGER);
CREATE TABLE IF NOT EXISTS models (sample_id TEXT PRIMARY KEY, model BLOB, auto BLOB, size INTEGER);
CREATE TABLE IF NOT EXISTS corrections (
  sample_id TEXT, client TEXT, from_role TEXT, role TEXT, text TEXT, norm TEXT,
  size_rel REAL, bold INTEGER, italic INTEGER, lines INTEGER, words INTEGER,
  x REAL, y REAL, w REAL, centered INTEGER, starts_num INTEGER, caps REAL, ends_stop INTEGER,
  first_page INTEGER, cap_word INTEGER);
CREATE INDEX IF NOT EXISTS corr_client ON corrections(client);
"""
FEATURES = ("size_rel", "bold", "italic", "lines", "words", "x", "y", "w", "centered", "starts_num",
            "caps", "ends_stop", "first_page", "cap_word")


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.join(samples_dir(), "files"), exist_ok=True)
    c = sqlite3.connect(db_path(), timeout=30)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    c.executescript(SCHEMA)
    try:                                   # V4.21: the details of an approved file
        c.execute("ALTER TABLE samples ADD COLUMN details TEXT DEFAULT '{}'")
    except sqlite3.OperationalError:
        pass
    for t in ("blocks", "corrections"):    # V4.25: the font a tag is set in (the brain's style map)
        try:
            c.execute(f"ALTER TABLE {t} ADD COLUMN font TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
    try:                                   # V4.25: the self-learning brain's tables
        from . import brain
        brain.ensure(c)
    except Exception:
        pass
    return c


def has_data() -> bool:
    return os.path.exists(db_path())


# ------------------------------------------------------------ text features
def norm(text: str) -> str:
    t = re.sub(r"\d+", "#", (text or "").lower())
    t = re.sub(r"[^\w#]+", " ", t)
    return " ".join(t.split())[:120]


def font_family(name: str) -> str:
    """'ABCDEF+Helvetica-Bold' -> 'helvetica' (the analysis' own reading)."""
    name = re.sub(r"^[A-Z]{6}\+", "", name or "")
    return re.split(r"[-,]", name)[0].lower()


def text_features(text: str) -> dict:
    t = (text or "").strip()
    words = t.split()
    letters = [c for c in t if c.isalpha()]
    return {"words": len(words),
            "starts_num": 1 if re.match(r"^\(?\d+(\.\d+)*[.)]?\s", t) or re.match(r"^[IVXLC]+\.\s", t) else 0,
            "caps": round(sum(c.isupper() for c in letters) / len(letters), 2) if letters else 0.0,
            "ends_stop": 1 if t.endswith((".", "?", "!", ":", ";")) else 0,
            "cap_word": 1 if CAPTION_WORDS.match(t) else 0}


def vector(f: dict) -> tuple:
    """The numbers compared (weighted): size first, then bold and shape."""
    return (min(f["size_rel"], 3.0) * 4.0, f["bold"] * 1.6, f["italic"] * 0.6,
            min(f["lines"], 6) / 6 * 1.2, math.log1p(min(f["words"], 200)) / math.log1p(200) * 1.6,
            f["x"] * 0.8, f["y"] * 0.4, f["centered"] * 0.8, f["starts_num"] * 1.0,
            f["caps"] * 1.0, f["ends_stop"] * 0.9, f["first_page"] * 0.4, f["cap_word"] * 1.8)


# ------------------------------------------------------------ measuring a file
class _Pages:
    """Lines per page (display space) and the body size, read once."""

    def __init__(self, path: str, max_pages: int = 400, body: float | None = None):
        import pymupdf

        from .analyzer import collect_lines
        self.doc = pymupdf.open(path)
        self.n = min(self.doc.page_count, max_pages)
        self.lines = {}
        self._collect = collect_lines
        if body:
            self.body = float(body)
            return
        sizes = collections.Counter()
        for p in range(min(self.n, 40)):
            for l in self.page_lines(p):
                sizes[round(l.size * 2) / 2] += len(l.text)
        self.body = sizes.most_common(1)[0][0] if sizes else 10.0

    def page_lines(self, p: int):
        if p not in self.lines:
            try:
                self.lines[p] = self._collect(self.doc[p]) if p < self.doc.page_count else []
            except Exception:
                self.lines[p] = []
        return self.lines[p]

    def disp(self, p: int, box):
        import pymupdf
        r = pymupdf.Rect(box) * self.doc[p].transformation_matrix
        r.normalize()
        return r

    def size(self, p: int):
        r = self.doc[p].rect
        return r.width or 1.0, r.height or 1.0

    def close(self):
        self.doc.close()


def _inside(l, r, pad=1.5) -> bool:
    cx, cy = (l.bbox[0] + l.bbox[2]) / 2, (l.bbox[1] + l.bbox[3]) / 2
    return r.x0 - pad <= cx <= r.x1 + pad and r.y0 - pad <= cy <= r.y1 + pad


def measure(pages: _Pages, page: int, box, text: str, style: dict | None = None,
            body: float | None = None) -> dict | None:
    """The features of a tag printed in `box` (PDF space) on `page`."""
    if page is None or page < 0 or page >= pages.n or not box:
        return None
    r = pages.disp(page, box)
    pw, ph = pages.size(page)
    body = body or pages.body or 10.0
    st = style or {}
    font = ""
    if st.get("size"):
        size, bold, italic, nl = float(st["size"]), bool(st.get("bold")), bool(st.get("italic")), int(st.get("lines") or 1)
        font = str(st.get("font") or "")
    else:
        ls = [l for l in pages.page_lines(page) if _inside(l, r)]
        if not ls:
            return None
        size = max(l.size for l in ls)
        bold = sum(l.bold for l in ls) > len(ls) / 2
        italic = sum(l.italic for l in ls) > len(ls) / 2
        nl = len(ls)
        fonts = collections.Counter()
        for l in ls:
            fonts[font_family(l.font)] += len(l.text)
        font = fonts.most_common(1)[0][0] if fonts else ""
    f = {"size_rel": round(size / body, 3) if body else 1.0, "bold": int(bold), "italic": int(italic),
         "lines": nl, "x": round(max(0.0, min(1.0, r.x0 / pw)), 3), "y": round(max(0.0, min(1.0, r.y0 / ph)), 3),
         "w": round(max(0.0, min(1.0, r.width / pw)), 3),
         "centered": int(abs((r.x0 + r.x1) / 2 - pw / 2) < 0.04 * pw and r.width < 0.8 * pw),
         "first_page": int(page == 0), "font": font[:40]}
    f.update(text_features(text))
    return f


def _subtree(model, n):
    """Text and box of a tag with everything inside it (one page)."""
    texts, box = [], None
    from .model import union
    stack = [n]
    while stack:
        x = stack.pop(0)
        if x.artifact:
            continue
        if x.text and (x is n or not x.children):
            texts.append(x.text)
        if x.bbox and x.page == n.page:
            box = union(box, x.bbox)
        stack[0:0] = [model.nodes[c] for c in x.children if c in model.nodes]
    t = n.text if n.text and not n.children else " ".join(texts)
    return " ".join((t or "").split()), box


def _text_box(model, n):
    """Text and box of a tag — an artifact's own (V4.26: a tag made an
    artifact by a person is a fix too, and _subtree leaves artifacts out)."""
    if n.artifact:
        return " ".join((n.text or "").split()), n.bbox
    return _subtree(model, n)


def _band(f_y: float, f_h: float) -> str:
    if f_y + f_h <= 0.12:
        return "top"
    if f_y >= 0.88:
        return "foot"
    return ""


def caption_place(model, n) -> tuple[str, str] | None:
    """(Table | Figure, inside_first | inside_last | before | after) of a Caption."""
    par = model.nodes.get(n.parent) if n.parent else None
    if par is not None and par.role in ("Table", "Figure"):
        kids = [c for c in par.children if c in model.nodes]
        return par.role, "inside_first" if kids and kids[0] == n.id else "inside_last"
    sib = par.children if par else model.root_children
    if n.id not in sib:
        return None
    i = sib.index(n.id)
    nxt = model.nodes.get(sib[i + 1]) if i + 1 < len(sib) else None
    prv = model.nodes.get(sib[i - 1]) if i > 0 else None
    if nxt is not None and nxt.role in ("Table", "Figure"):
        return nxt.role, "before"
    if prv is not None and prv.role in ("Table", "Figure"):
        return prv.role, "after"
    return None


def _role_of(n) -> str:
    if n.artifact:
        return "Artifact"
    return "H1" if n.role == "H?" else n.role


def read_sample(model, pdf_path: str, auto=None) -> dict:
    """Everything a sample teaches: blocks, artifacts, structure, stats — and,
    for a file finished in the tool, what people changed (`auto` = what the
    tool proposed)."""
    pages = _Pages(pdf_path)
    captions = collections.Counter()
    corrections = []
    blocks, roles, pairs, origs = [], collections.Counter(), collections.Counter(), collections.Counter()
    covered = collections.defaultdict(list)
    seen_arts: set = set()
    try:
        for n in model.ordered_nodes():
            par = model.nodes.get(n.parent) if n.parent else None
            if n.artifact:
                if n.text and n.bbox and 0 <= n.page < pages.n:
                    r = pages.disp(n.page, n.bbox)
                    pw, ph = pages.size(n.page)
                    b = _band(r.y0 / ph, r.height / ph)
                    if b and norm(n.text):
                        seen_arts.add((n.page, norm(n.text), b))
                continue
            roles[n.role] += 1
            if n.role == "Caption":
                cp = caption_place(model, n)
                if cp:
                    captions[cp] += 1
            if n.orig_role and n.orig_role != n.role:
                origs[f"{n.orig_role} → {n.role}"] += 1
            pairs[((par.role if par else "Document"), n.role)] += 1
            if n.bbox and 0 <= n.page < pages.n:
                covered[n.page].append(pages.disp(n.page, n.bbox))
                for p in n.parts or []:
                    if 0 <= int(p[0]) < pages.n:
                        covered[int(p[0])].append(pages.disp(int(p[0]), p[1:5]))
            if n.role not in TEXT_ROLES:
                continue
            if any(model.nodes[c].role in TEXT_ROLES for c in n.children if c in model.nodes):
                continue
            text, box = _subtree(model, n)
            if not text.strip() or not box:
                continue
            f = measure(pages, n.page, box, text, None)
            if f is None:
                continue
            f.update(role=n.role, orig=n.orig_role or "", parent=par.role if par else "", text=text[:300],
                     norm=norm(text) if len(text.split()) <= 14 else "")
            blocks.append(f)
        # a tagged file: the lines no tag holds, at the top or foot of a page
        if getattr(model, "mode", "") == "import":
            for p in range(pages.n):
                pw, ph = pages.size(p)
                boxes = covered.get(p, [])
                for l in pages.page_lines(p):
                    b = _band(l.bbox[1] / ph, (l.bbox[3] - l.bbox[1]) / ph)
                    if not b or any(_inside(l, r, 2.0) for r in boxes):
                        continue
                    k = norm(l.text)
                    if k:
                        seen_arts.add((p, k, b))
        arts = collections.Counter((k, b) for _, k, b in seen_arts)
        # what people changed in the tool's proposal (a file approved in the tool)
        fixes = collections.Counter()
        if auto is not None:
            for n in model.nodes.values():
                a = auto.nodes.get(n.id)
                if a is None:
                    continue
                was, now = _role_of(a), _role_of(n)
                if was == now or not ({was, now} & (TEXT_ROLES | {"Artifact"})):
                    continue
                if any(model.nodes[c].role in TEXT_ROLES for c in n.children if c in model.nodes):
                    continue
                text, box = _text_box(model, n)
                if not text.strip() or not box:
                    continue
                f = measure(pages, n.page, box, text, None)
                if f is None:
                    continue
                f.update(from_role=was, role=now, text=text[:300], norm=norm(text) if len(text.split()) <= 14 else "")
                corrections.append(f)
                fixes[(was, now)] += 1
        heads = collections.defaultdict(list)
        for b in blocks:
            if b["role"] in HEADINGS or b["role"] == "Title":
                heads[b["role"]].append(b["size_rel"])
        stats = {"roles": dict(roles.most_common()), "rolemap": dict(origs.most_common(30)),
                 "pairs": [[a, c, k] for (a, c), k in pairs.most_common(40)],
                 "headings": {r: round(sum(v) / len(v), 2) for r, v in sorted(heads.items())},
                 "body": pages.body, "pages": pages.n,
                 "captions": {f"{k}:{p}": v for (k, p), v in captions.items()},
                 "corrections": len(corrections),
                 "fixes": [[a, b, k] for (a, b), k in fixes.most_common(20)]}
        pairs_out = [[a, c, k] for (a, c), k in pairs.items()]
        pairs_out += [[f"@caption:{k}", p, v] for (k, p), v in captions.items()]
        doc = None
        try:                               # V4.25: what kind of document, its fingerprint
            from .brain import digest_pages
            doc = digest_pages(pages)
            stats["doc_type"] = doc.get("type")
        except Exception:
            doc = None
        return {"blocks": blocks, "artifacts": [[k, b, n] for (k, b), n in arts.items()],
                "pairs": pairs_out, "stats": stats, "pages": pages.n, "corrections": corrections,
                "doc": doc}
    finally:
        pages.close()


# ------------------------------------------------------------ the database
def add_sample(client: str, filename: str, src: str, by: str = "", kind: str = "tagged",
               job: str = "", model_src: str = "", auto_src: str = "", details: dict | None = None) -> str:
    sid = datetime.now().strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:6]
    _copy_files(sid, src, model_src, auto_src)
    with connect() as c:
        c.execute("INSERT INTO samples (id, client, filename, kind, job, uploaded_by, created, status, details) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  (sid, (client or "").strip(), filename, kind, job, by,
                   datetime.now().isoformat(timespec="seconds"), "queued", json.dumps(details or {})))
    return sid


def _copy_files(sid: str, src: str, model_src: str = "", auto_src: str = "") -> None:
    import shutil
    files = os.path.join(samples_dir(), "files")
    os.makedirs(files, exist_ok=True)
    shutil.copyfile(src, os.path.join(files, sid + ".pdf"))
    for s_, ext in ((model_src, ".model.json"), (auto_src, ".auto.json")):
        dst = os.path.join(files, sid + ext)
        if s_ and os.path.exists(s_):
            shutil.copyfile(s_, dst)
        elif os.path.exists(dst) and not s_:
            os.remove(dst)


def job_sample(job: str) -> dict | None:
    """The sample an approved file became (if it did)."""
    if not job or not has_data():
        return None
    with connect() as c:
        r = c.execute("SELECT * FROM samples WHERE job=? AND kind='approved' ORDER BY created DESC", (job,)).fetchone()
        return dict(r) if r else None


def upsert_job_sample(job: str, client: str, filename: str, pdf: str, model_src: str, auto_src: str = "",
                      by: str = "", details: dict | None = None) -> str:
    """An approved file teaches the tool: its sample is made — or, approved
    again after more work, brought up to date (V4.21)."""
    old = job_sample(job)
    if old is None:
        return add_sample(client, filename, pdf, by, kind="approved", job=job, model_src=model_src,
                          auto_src=auto_src, details=details)
    sid = old["id"]
    _copy_files(sid, pdf, model_src, auto_src)
    with connect() as c:
        c.execute("UPDATE samples SET client=?, filename=?, uploaded_by=?, status='queued', enabled=1, error='', "
                  "details=? WHERE id=?", ((client or "").strip(), filename, by, json.dumps(details or {}), sid))
        for t in SAMPLE_TABLES + BRAIN_SAMPLE_TABLES:
            c.execute(f"UPDATE {t} SET client=? WHERE sample_id=?", ((client or "").strip(), sid))
    return sid


def set_job_sample_enabled(job: str, enabled: bool) -> bool:
    s_ = job_sample(job)
    if s_ is None:
        return False
    return set_sample(s_["id"], enabled=1 if enabled else 0)


def learn_sample(sid: str) -> dict:
    """Read one sample into the database (run in a worker process)."""
    c = connect()
    row = c.execute("SELECT * FROM samples WHERE id=?", (sid,)).fetchone()
    if row is None:
        return {"ok": False, "error": "no such sample"}
    c.execute("UPDATE samples SET status='learning', error='' WHERE id=?", (sid,))
    c.commit()
    pdf = os.path.join(samples_dir(), "files", sid + ".pdf")
    mjson = os.path.join(samples_dir(), "files", sid + ".model.json")
    ajson = os.path.join(samples_dir(), "files", sid + ".auto.json")
    auto = None
    try:
        import zlib
        if os.path.exists(mjson):
            from .model import DocModel
            with open(mjson, encoding="utf-8") as fh:
                raw = fh.read()
            model = DocModel.from_json(raw)
            araw = b""
            if os.path.exists(ajson):
                with open(ajson, encoding="utf-8") as fh:
                    a_txt = fh.read()
                try:
                    auto = DocModel.from_json(a_txt)
                    araw = zlib.compress(a_txt.encode("utf-8"), 6)
                except Exception:
                    auto = None
            # the file model itself goes into the database too
            blob = zlib.compress(raw.encode("utf-8"), 6)
            c.execute("INSERT OR REPLACE INTO models VALUES (?,?,?,?)", (sid, blob, araw, len(raw)))
        else:
            from .importer import assess_tags, import_tags
            a = assess_tags(pdf)
            if not a.get("tagged"):
                raise ValueError("this PDF has no tags — a sample must be a tagged (finished) file")
            model = import_tags(pdf)
            import zlib
            raw = model.to_json()
            c.execute("INSERT OR REPLACE INTO models VALUES (?,?,?,?)",
                      (sid, zlib.compress(raw.encode("utf-8"), 6), b"", len(raw)))
        r = read_sample(model, pdf, auto)
        client = row["client"]
        c.execute("DELETE FROM blocks WHERE sample_id=?", (sid,))
        c.execute("DELETE FROM artifacts WHERE sample_id=?", (sid,))
        c.execute("DELETE FROM structure WHERE sample_id=?", (sid,))
        c.execute("DELETE FROM corrections WHERE sample_id=?", (sid,))
        ccols = ("from_role", "role", "text", "norm") + FEATURES + ("font",)
        c.executemany(f"INSERT INTO corrections (sample_id, client, {', '.join(ccols)}) VALUES (?,?,{','.join('?' * len(ccols))})",
                      [(sid, client, *[b.get(k) for k in ccols]) for b in r["corrections"]])
        cols = ("role", "orig", "parent", "text", "norm", "size_rel", "bold", "italic", "lines", "words",
                "x", "y", "w", "centered", "starts_num", "caps", "ends_stop", "first_page", "cap_word", "font")
        c.executemany(f"INSERT INTO blocks (sample_id, client, {', '.join(cols)}) VALUES (?,?,{','.join('?' * len(cols))})",
                      [(sid, client, *[b.get(k) for k in cols]) for b in r["blocks"]])
        c.executemany("INSERT INTO artifacts VALUES (?,?,?,?,?)", [(sid, client, *a) for a in r["artifacts"]])
        c.executemany("INSERT INTO structure VALUES (?,?,?,?,?)", [(sid, client, *p) for p in r["pairs"]])
        c.execute("UPDATE samples SET status='ready', learned=?, pages=?, tags=?, stats=? WHERE id=?",
                  (datetime.now().isoformat(timespec="seconds"), r["pages"], len(r["blocks"]),
                   json.dumps(r["stats"]), sid))
        brain_out = {}
        try:                               # V4.25: the brain's memory, the document, the outcome
            from . import brain
            brain_out = brain.store_sample(c, sid, client, dict(row), r, model, auto)
        except Exception as exc:
            brain_out = {"error": str(exc)[:200]}
        c.commit()
        return {"ok": True, "blocks": len(r["blocks"]), "brain": brain_out}
    except Exception as exc:
        c.execute("UPDATE samples SET status='error', error=? WHERE id=?", (str(exc)[:500], sid))
        c.commit()
        return {"ok": False, "error": str(exc)}
    finally:
        c.close()


def samples() -> list[dict]:
    if not has_data():
        return []
    with connect() as c:
        out = []
        for r in c.execute("SELECT * FROM samples ORDER BY created DESC"):
            d = dict(r)
            for k in ("stats", "details"):
                try:
                    d[k] = json.loads(d.get(k) or "{}")
                except (ValueError, TypeError):
                    d[k] = {}
            out.append(d)
        return out


SAMPLE_TABLES = ("blocks", "artifacts", "structure", "corrections")
BRAIN_SAMPLE_TABLES = ("docs", "memory", "insights")


def set_sample(sid: str, **kw) -> bool:
    allowed = {k: v for k, v in kw.items() if k in ("client", "enabled", "status")}
    if not allowed:
        return False
    with connect() as c:
        cur = c.execute(f"UPDATE samples SET {', '.join(k + '=?' for k in allowed)} WHERE id=?",
                        (*allowed.values(), sid))
        if "client" in allowed:
            for t in SAMPLE_TABLES + BRAIN_SAMPLE_TABLES:
                c.execute(f"UPDATE {t} SET client=? WHERE sample_id=?", (allowed["client"], sid))
        return cur.rowcount > 0


def delete_sample(sid: str) -> bool:
    with connect() as c:
        for t in SAMPLE_TABLES + ("models",) + BRAIN_SAMPLE_TABLES:
            c.execute(f"DELETE FROM {t} WHERE sample_id=?", (sid,))
        cur = c.execute("DELETE FROM samples WHERE id=?", (sid,))
    for ext in (".pdf", ".model.json", ".auto.json"):
        p = os.path.join(samples_dir(), "files", sid + ext)
        if os.path.exists(p):
            os.remove(p)
    return cur.rowcount > 0


def queued() -> list[str]:
    if not has_data():
        return []
    with connect() as c:
        return [r["id"] for r in c.execute("SELECT id FROM samples WHERE status='queued' ORDER BY created")]


def clients() -> list[dict]:
    """Every client with samples: files, pages, tags learned."""
    if not has_data():
        return []
    with connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT client, COUNT(*) AS files, SUM(pages) AS pages, SUM(tags) AS tags, "
            "SUM(status='ready' AND enabled=1) AS ready, MAX(learned) AS learned "
            "FROM samples GROUP BY client ORDER BY client")]


def knowledge(client: str) -> dict:
    """What the tool learned for one client ('' = common), for the admin page."""
    if not has_data():
        return {"client": client, "files": 0}
    with connect() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM samples WHERE client=? AND enabled=1 AND status='ready'", (client,))]
        if not ids:
            return {"client": client, "files": 0}
        q = ",".join("?" * len(ids))
        roles = {r["role"]: r["n"] for r in c.execute(
            f"SELECT role, COUNT(*) n FROM blocks WHERE sample_id IN ({q}) GROUP BY role ORDER BY n DESC", ids)}
        heads = {r["role"]: {"size": round(r["s"], 2), "bold": round(r["b"] * 100), "n": r["n"]} for r in c.execute(
            f"SELECT role, AVG(size_rel) s, AVG(bold) b, COUNT(*) n FROM blocks WHERE sample_id IN ({q}) "
            "AND (role LIKE 'H_' OR role IN ('Title','Caption','Note','P')) GROUP BY role", ids)}
        words = []
        for r in c.execute(f"SELECT norm, role, COUNT(*) n, MIN(text) t FROM blocks WHERE sample_id IN ({q}) "
                           "AND norm != '' GROUP BY norm, role HAVING n >= 2 ORDER BY n DESC LIMIT 60", ids):
            words.append({"text": r["t"][:80], "role": r["role"], "n": r["n"]})
        arts = [{"pattern": r["pattern"], "band": r["band"], "n": r["n"]} for r in c.execute(
            f"SELECT pattern, band, SUM(n) n FROM artifacts WHERE sample_id IN ({q}) GROUP BY pattern, band "
            "HAVING n >= 2 ORDER BY n DESC LIMIT 30", ids)]
        pairs = [{"parent": r["parent"], "child": r["child"], "n": r["n"]} for r in c.execute(
            f"SELECT parent, child, SUM(n) n FROM structure WHERE sample_id IN ({q}) AND parent NOT LIKE '@%' "
            "GROUP BY parent, child ORDER BY n DESC LIMIT 40", ids)]
        captions = [{"kind": r["parent"].split(":", 1)[1], "place": r["child"], "n": r["n"]} for r in c.execute(
            f"SELECT parent, child, SUM(n) n FROM structure WHERE sample_id IN ({q}) AND parent LIKE '@caption:%' "
            "GROUP BY parent, child ORDER BY n DESC", ids)]
        fixes = [{"from": r["from_role"], "to": r["role"], "n": r["n"], "example": (r["t"] or "")[:70]} for r in c.execute(
            f"SELECT from_role, role, COUNT(*) n, MIN(text) t FROM corrections WHERE sample_id IN ({q}) "
            "GROUP BY from_role, role ORDER BY n DESC LIMIT 20", ids)]
        kinds = collections.Counter(r["kind"] or "tagged" for r in c.execute(
            f"SELECT kind FROM samples WHERE id IN ({q})", ids))
        rolemap = collections.Counter()
        for r in c.execute(f"SELECT stats FROM samples WHERE id IN ({q})", ids):
            try:
                for k, v in (json.loads(r["stats"] or "{}").get("rolemap") or {}).items():
                    rolemap[k] += v
            except ValueError:
                pass
        return {"client": client, "files": len(ids), "roles": roles, "styles": heads, "words": words,
                "artifacts": arts, "pairs": pairs, "rolemap": dict(rolemap.most_common(25)),
                "captions": captions, "fixes": fixes, "kinds": dict(kinds)}


def sample_detail(sid: str) -> dict | None:
    """One sample, everything stored about it (the admin's sample drawer)."""
    if not has_data():
        return None
    with connect() as c:
        r = c.execute("SELECT * FROM samples WHERE id=?", (sid,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        for k in ("stats", "details"):
            try:
                d[k] = json.loads(d.get(k) or "{}")
            except (ValueError, TypeError):
                d[k] = {}
        m = c.execute("SELECT size, length(model) z, length(auto) a FROM models WHERE sample_id=?", (sid,)).fetchone()
        d["model"] = {"size": m["size"], "stored": m["z"], "auto": m["a"]} if m else None
        d["blocks"] = [dict(b) for b in c.execute(
            "SELECT role, text, size_rel, bold FROM blocks WHERE sample_id=? LIMIT 400", (sid,))]
        d["corrections"] = [dict(b) for b in c.execute(
            "SELECT from_role, role, text FROM corrections WHERE sample_id=? LIMIT 200", (sid,))]
        d["artifacts"] = [dict(b) for b in c.execute(
            "SELECT pattern, band, n FROM artifacts WHERE sample_id=? ORDER BY n DESC LIMIT 40", (sid,))]
        return d


def sample_model(sid: str, which: str = "model") -> str | None:
    """The stored file model (JSON) of a sample — or what the tool proposed."""
    if not has_data():
        return None
    import zlib
    with connect() as c:
        r = c.execute(f"SELECT {'auto' if which == 'auto' else 'model'} b FROM models WHERE sample_id=?", (sid,)).fetchone()
        if r is None or not r["b"]:
            return None
        return zlib.decompress(r["b"]).decode("utf-8")


def db_stats() -> dict:
    if not has_data():
        return {"exists": False}
    with connect() as c:
        out = {"exists": True, "size": os.path.getsize(db_path())}
        for t in ("samples", "blocks", "artifacts", "structure", "corrections", "models"):
            out[t] = c.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
        files = os.path.join(samples_dir(), "files")
        out["files_size"] = sum(os.path.getsize(os.path.join(files, f)) for f in os.listdir(files)) if os.path.isdir(files) else 0
        return out


# ------------------------------------------------------------ using it
class Knowledge:
    """What the samples of a client (and the common samples) say, ready to vote.

    V4.25: `scope="all"` reads every client's samples; `weights` (sample id ->
    0.4 … 2.0, from core/brain.doc_weights) lets the samples most like this
    file count most — they then play the part of the client's own samples.
    The fixes people made in built files (the brain's feedback) count too."""

    def __init__(self, client: str, scope: str = "client", weights: dict | None = None):
        self.client = client or ""
        self.scope = scope if scope in ("client", "all") else "client"
        self.weights = dict(weights or {})
        self.protos: dict[tuple, dict] = {}
        self.words: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        self.client_words: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        self.arts: collections.Counter = collections.Counter()
        self.files = 0
        self.client_files = 0
        self._list = []
        self._ex_own = collections.defaultdict(list)
        self._ex_common = collections.defaultdict(list)
        self.fixes: list[dict] = []
        self.captions: collections.Counter = collections.Counter()
        self.presence: collections.Counter = collections.Counter()
        if not has_data():
            return
        with connect() as c:
            if self.scope == "all":
                rows = list(c.execute("SELECT id, client FROM samples WHERE enabled=1 AND status='ready'"))
            else:
                rows = list(c.execute("SELECT id, client FROM samples WHERE enabled=1 AND status='ready' "
                                      "AND (client=? OR client='')", (self.client,)))
            fb_rows = []
            try:                           # V4.25: what people fixed in built files
                if self.scope == "all":
                    fb_rows = [dict(r) for r in c.execute("SELECT * FROM feedback LIMIT 20000")]
                else:
                    fb_rows = [dict(r) for r in c.execute("SELECT * FROM feedback WHERE client=? LIMIT 20000",
                                                          (self.client,))]
            except sqlite3.OperationalError:
                fb_rows = []
            if not rows and not fb_rows:
                return
            mine = {r["id"] for r in rows if r["client"] == self.client and self.client}
            own = set(mine)
            if self.scope == "all":
                # the samples most like this file stand in for the client's own
                own |= {sid for sid, w in self.weights.items() if w >= 1.2}

            def sw(sid: str) -> float:
                if self.scope != "all":
                    return 2.0 if sid in own else 1.0
                w = float(self.weights.get(sid, 1.0))
                return max(w, 2.0) if sid in mine else w
            wcache = {r["id"]: sw(r["id"]) for r in rows}
            self.files = len(rows)
            self.client_files = len(own)
            ids = [r["id"] for r in rows] or [""]
            q = ",".join("?" * len(ids))
            for b in c.execute(f"SELECT * FROM blocks WHERE sample_id IN ({q}) LIMIT 60000", ids):
                f = dict(b)
                if f["role"] == "H":
                    continue
                v = vector(f)
                key = tuple(round(x, 1) for x in v)
                p = self.protos.get(key)
                if p is None:
                    p = self.protos[key] = {"v": v, "roles": collections.Counter()}
                w = wcache.get(f["sample_id"], 1.0)
                p["roles"][f["role"]] += w
                if f["norm"]:
                    self.words[f["norm"]][f["role"]] += w
                    if f["sample_id"] in own:
                        self.client_words[f["norm"]][f["role"]] += 1
                if len(f["text"] or "") <= 160 and f["words"] <= 18:
                    pool = self._ex_own if f["sample_id"] in own else self._ex_common
                    if len(pool[f["role"]]) < 6 and all(e["text"] != f["text"] for e in pool[f["role"]]):
                        pool[f["role"]].append({"text": f["text"], "role": f["role"], "size": f["size_rel"], "bold": f["bold"]})
            # what people corrected: the cases the analysis gets wrong weigh most
            corr = [dict(b) for b in c.execute(f"SELECT * FROM corrections WHERE sample_id IN ({q}) LIMIT 20000", ids)]
            for f in fb_rows:              # a built (not yet approved) file: weighs less
                f["sample_id"] = "fb:" + str(f.get("job") or "")
                corr.append(f)
            for f in corr:
                v = vector(f)
                key = tuple(round(x, 1) for x in v)
                p = self.protos.get(key)
                if p is None:
                    p = self.protos[key] = {"v": v, "roles": collections.Counter()}
                if f["sample_id"].startswith("fb:"):
                    w = 1.0
                else:
                    w = 1.5 * wcache.get(f["sample_id"], 1.0)
                if f["role"] != "Artifact":
                    p["roles"][f["role"]] += w
                    if f["norm"]:
                        self.words[f["norm"]][f["role"]] += w
                        if f["sample_id"] in own:
                            self.client_words[f["norm"]][f["role"]] += 2
                if len(self.fixes) < 12 and len(f["text"] or "") <= 160:
                    self.fixes.append({"text": f["text"], "from": f["from_role"], "to": f["role"]})
            for a in c.execute(f"SELECT pattern, band, SUM(n) n FROM artifacts WHERE sample_id IN ({q}) "
                               "GROUP BY pattern, band", ids):
                self.arts[(a["pattern"], a["band"])] += a["n"]
            for r in c.execute(f"SELECT parent, child, SUM(n) n FROM structure WHERE sample_id IN ({q}) "
                               "AND parent LIKE '@caption:%' GROUP BY parent, child", ids):
                self.captions[(r["parent"].split(":", 1)[1], r["child"])] += r["n"]
            for r in c.execute(f"SELECT stats FROM samples WHERE id IN ({q})", ids):
                try:
                    for role in (json.loads(r["stats"] or "{}").get("roles") or {}):
                        self.presence[role] += 1
                except ValueError:
                    pass
        self._list = list(self.protos.values())
        self._np = None
        try:
            import numpy as np
            if len(self._list) > 200:
                self._np = (np, np.array([p["v"] for p in self._list], dtype=float))
        except Exception:
            self._np = None

    @property
    def empty(self) -> bool:
        return not self.protos and not self.arts

    def examples(self, limit: int = 24) -> list[dict]:
        """A few of the client's own tagged texts per tag, for the AI."""
        out = []
        for pool in (self._ex_own, self._ex_common):
            for role in sorted(pool, key=lambda r: (r != "P", r)):
                for e in pool[role]:
                    if len(out) < limit and all(x["text"] != e["text"] for x in out):
                        out.append(e)
        return out

    def caption_rule(self, kind: str):
        """(place, share, seen) the samples put a Table's / Figure's caption."""
        c = {p: n for (k, p), n in self.captions.items() if k == kind}
        tot = sum(c.values())
        if not tot:
            return None
        place, n = max(c.items(), key=lambda t: t[1])
        return place, n / tot, tot

    def vote(self, f: dict, k: int = 9) -> list[tuple[str, float, float]]:
        """[(role, share, mean distance)] of the nearest sample tags."""
        if not self._list:
            return []
        v = vector(f)
        if getattr(self, "_np", None):
            np, arr = self._np
            d2 = ((arr - np.array(v)) ** 2).sum(axis=1)
            if len(d2) > k:                   # V4.25: only the k nearest need sorting
                idx = np.argpartition(d2, k)[:k]
                idx = idx[np.argsort(d2[idx])]
            else:
                idx = np.argsort(d2)[:k]
            best = [(float(d2[i]), self._list[int(i)]) for i in idx]
        else:
            best = sorted(((sum((a - b) ** 2 for a, b in zip(v, p["v"])), p) for p in self._list),
                          key=lambda t: t[0])[:k]
        # only the near ones: a far tag says nothing about this one
        near = math.sqrt(best[0][0]) + 0.8
        best = [t for t in best if math.sqrt(t[0]) <= near]
        tot = collections.Counter()
        dist = collections.defaultdict(list)
        for d2, p in best:
            d = math.sqrt(d2)
            w = 1.0 / (d + 0.15)
            n = sum(p["roles"].values())
            for role, c in p["roles"].items():
                tot[role] += w * c / n * min(n, 6)
                dist[role].append(d)
        s = sum(tot.values()) or 1.0
        return [(r, round(x / s, 3), round(sum(dist[r]) / len(dist[r]), 3)) for r, x in tot.most_common()]

    def wording(self, text: str, own_only: bool = False):
        k = norm(text)
        if not k or len(text.split()) > 14:
            return None
        c = (self.client_words if own_only else self.words).get(k)
        if not c:
            return None
        role, n = c.most_common(1)[0]
        tot = sum(c.values())
        return role, n / tot, tot


def _neighbours(model, n):
    par = model.nodes.get(n.parent) if n.parent else None
    sib = par.children if par else model.root_children
    i = sib.index(n.id) if n.id in sib else -1
    out = []
    for j in (i - 1, i + 1):
        if 0 <= j < len(sib) and sib[j] in model.nodes:
            out.append(model.nodes[sib[j]])
    return out


def _candidates(model):
    """The text tags a sample vote may retag (not in a table, list, figure …)."""
    for n in list(model.ordered_nodes()):
        if n.id not in model.nodes or n.source == "user" or n.artifact or n.priority:
            continue
        role = "H1" if n.role == "H?" else n.role
        if role not in SWAP and role not in ("Note",):
            continue
        if any(a.role in ("Table", "L", "TOC", "TOCI", "Figure", "Form", "Index") for a in model.ancestors(n.id)):
            continue
        if any(model.nodes[c].role in TEXT_ROLES for c in n.children if c in model.nodes):
            continue
        text = " ".join((n.text or "").split())
        if not text or not n.bbox:
            continue
        yield n, role, text


def _retag(model, n, target: str, why: str, out: dict, changed: list) -> bool:
    if target == "Caption" and not any(x.role in ("Figure", "Table") for x in _neighbours(model, n)):
        return False
    old = n.role
    n.role = target
    n.confidence = max(n.confidence, 0.8)
    n.note = (n.note + "; " if n.note else "") + why
    if target in HEADINGS:
        n.style = dict(n.style or {}, level=int(target[1]))
    changed.append(n.id)
    out["changes"].append({"id": n.id, "from": old, "to": target, "why": why})
    return True


def _make_artifact(model, n, band: str) -> None:
    for c in list(n.children):          # a link the tool found in it goes too
        if c in model.nodes:
            model._drop_subtree(c)
    n.children = []
    n.role, n.artifact, n.artifact_subtype = "Artifact", True, "Pagination"
    n.pagination_subtype = "Header" if band == "top" else "Footer"


def place_captions(model, know, out: dict | None = None) -> int:
    """Put each Table's / Figure's caption where the client's samples put it
    (inside first / inside last / before / after) — when ≥ 80 % of at least
    three sample captions agree (V4.21)."""
    moved = 0
    used: set = set()
    for kind in ("Table", "Figure"):
        rule = know.caption_rule(kind)
        if not rule or rule[1] < 0.8 or rule[2] < 3:
            continue
        place = rule[0]
        for t in [x for x in model.ordered_nodes() if x.role == kind]:
            if t.id not in model.nodes:
                continue
            cap = None
            kids = [c for c in t.children if c in model.nodes]
            for cid in (kids[:1] + kids[-1:]):
                if model.nodes[cid].role == "Caption":
                    cap = model.nodes[cid]
                    break
            if cap is None:
                par = model.nodes.get(t.parent) if t.parent else None
                sib = par.children if par else model.root_children
                i = sib.index(t.id) if t.id in sib else -1
                for j in (i - 1, i + 1):
                    if 0 <= j < len(sib) and sib[j] in model.nodes and model.nodes[sib[j]].role == "Caption":
                        cap = model.nodes[sib[j]]
                        break
            if cap is None or cap.id in used or cap.source == "user":
                continue
            used.add(cap.id)
            cur = caption_place(model, cap)
            if cur is None or cur[1] == place:
                continue
            model.detach(cap.id)
            if place in ("inside_first", "inside_last"):
                model.add(cap, t.id, 0 if place == "inside_first" else None)
            else:
                par = model.nodes.get(t.parent) if t.parent else None
                sib = par.children if par else model.root_children
                i = sib.index(t.id)
                model.add(cap, t.parent, i if place == "before" else i + 1)
            cap.note = (cap.note + "; " if cap.note else "") + \
                f"samples: the client puts a {kind.lower()}'s caption {place.replace('_', ' ')} ({round(rule[1] * 100)} %)"
            moved += 1
            if out is not None:
                out["changes"].append({"id": cap.id, "from": "Caption", "to": "Caption",
                                       "why": f"moved {place.replace('_', ' ')} the {kind}"})
    return moved


def apply_samples(model, pdf_path: str, client: str = "", progress=None, ai_cfg=None,
                  rules: bool = True, know: "Knowledge | None" = None) -> dict:
    """Use the samples on a freshly analysed model. Returns what changed.

    `ai_cfg`: the tags the samples could not settle go to the AI with the
    client's own examples (and the fixes people made) in the question.
    `know`: the samples already read (V4.25: every client's, weighted by how
    like this file they are — see core/brain.doc_weights)."""
    know = know if know is not None else Knowledge(client)
    out = {"client": client or "", "files": know.files, "client_files": know.client_files,
           "changed": 0, "artifacts": 0, "ai": 0, "captions": 0, "changes": []}
    if know.empty:
        return out
    pages = _Pages(pdf_path)
    body = model.body_size or pages.body
    changed: list[str] = []
    unsure: list = []
    try:
        for n, role, text in _candidates(model):
            words = len(text.split())
            f = measure(pages, n.page, n.bbox, text, n.style if (n.style or {}).get("size") else None, body)
            if f is None:
                continue
            # 1. a running head or foot the samples print
            r = pages.disp(n.page, n.bbox)
            pw, ph = pages.size(n.page)
            band = _band(r.y0 / ph, r.height / ph)
            if band and role == "P" and words <= 25 and know.arts.get((norm(text), band), 0) >= 2:
                _make_artifact(model, n, band)
                n.note = (n.note + "; " if n.note else "") + "samples: running " + \
                    ("head" if band == "top" else "foot") + " in the client's samples"
                out["artifacts"] += 1
                out["changes"].append({"id": n.id, "from": role, "to": "Artifact", "why": "running " + band})
                continue
            # 2. the wording the client's samples always tag one way
            target, why = None, ""
            wd = know.wording(text, own_only=bool(client) and (know.scope != "all" or bool(know.client_files)))
            if wd and wd[1] >= 0.85 and wd[2] >= 2 and wd[0] in SWAP:
                target, why = wd[0], f"the samples tag “{text[:40]}” as {wd[0]} ({wd[2]}×)"
            else:
                doubt = n.confidence < 0.85 or n.role == "H?"
                if not doubt and not (know.client_files and role == "P" and words <= 20 and f["lines"] <= 3):
                    continue
                votes = know.vote(f)
                if not votes:
                    continue
                top, share, dist = votes[0]
                need = 0.7 if doubt else 0.9
                if top in SWAP and share >= need and dist <= (1.2 if doubt else 0.6):
                    target, why = top, f"{round(share * 100)} % of the most similar sample tags are {top}"
                elif doubt or (top != role and share >= 0.5 and words <= 12):
                    unsure.append((n, role, text, f, votes))
            if not target or target == role:
                continue
            _retag(model, n, target, "samples: " + why, out, changed)
        # 3. what the samples could not settle: the AI, shown the client's own examples
        if ai_cfg is not None and unsure and getattr(ai_cfg, "enabled", False):
            try:
                from .ai import sample_roles
                batch = unsure[:25]
                blocks = [{"i": i + 1, "role": role, "text": text[:200], "size": f["size_rel"], "bold": f["bold"],
                           "page": n.page, "votes": votes[:2]}
                          for i, (n, role, text, f, votes) in enumerate(batch)]
                answers = sample_roles(ai_cfg, client or "every client", know.examples(), know.fixes, blocks)
                for a in answers:
                    k = a["i"] - 1
                    if not (0 <= k < len(batch)) or a["role"] not in SWAP:
                        continue
                    n, role, text, f, votes = batch[k]
                    if a["role"] == role or n.id in changed:
                        continue
                    if _retag(model, n, a["role"], "samples + AI: " + (a.get("why") or "like the client's examples"),
                              out, changed):
                        out["ai"] += 1
            except Exception as exc:
                out["ai_error"] = str(exc)[:200]
        # 4. the client's structure: where captions go
        if rules:
            out["captions"] = place_captions(model, know, out)
        # heading levels: never down by more than one after a change
        prev = 0
        for n in model.ordered_nodes():
            if n.role in HEADINGS:
                lv = int(n.role[1])
                if n.id in changed and prev and lv > prev + 1:
                    n.role = f"H{prev + 1}"
                    lv = prev + 1
                prev = lv
    finally:
        pages.close()
    out["changed"] = len(changed)
    out["changes"] = out["changes"][:300]
    return out


IGNORE_ROLES = {"Document", "Part", "Sect", "Div", "Span", "Link", "Reference", "NonStruct", "Private",
                "Artifact", "THead", "TBody", "TFoot", "TR", "LBody", "Lbl", "LI", "TD", "TH", "Annot"}


def compare(model, pdf_path: str, client: str = "", know: "Knowledge | None" = None) -> dict:
    """This file against the client's samples (the editor's Client tab):
    tags the client never uses, tags its files always have, captions put
    elsewhere, running heads still read, tags the samples tag otherwise."""
    know = know if know is not None else Knowledge(client)
    res = {"client": client, "files": know.files, "client_files": know.client_files, "findings": [], "nodes": [],
           "scope": know.scope}
    if know.empty:
        return res
    here = collections.Counter(("Artifact" if n.artifact else n.role) for n in model.nodes.values())
    F = res["findings"]
    if know.files >= 2:
        for role, k in sorted(here.items()):
            if role in IGNORE_ROLES or role == "H?":
                continue
            if know.presence.get(role, 0) == 0:
                F.append({"kind": "never", "role": role, "n": k,
                          "text": f"{role} ×{k} — none of the {know.files} sample files uses {role}"})
        for role, k in know.presence.most_common():
            if role in IGNORE_ROLES or role in here:
                continue
            if k >= max(2, 0.8 * know.files):
                F.append({"kind": "missing", "role": role, "n": k,
                          "text": f"No {role} here — {k} of the {know.files} sample files have one"})
    for kind in ("Table", "Figure"):
        rule = know.caption_rule(kind)
        if not rule or rule[1] < 0.8 or rule[2] < 3:
            continue
        off = [n.id for n in model.nodes.values() if n.role == "Caption"
               and (caption_place(model, n) or ("", ""))[0] == kind and caption_place(model, n)[1] != rule[0]]
        if off:
            F.append({"kind": "caption", "role": "Caption", "n": len(off), "ids": off[:30], "place": rule[0],
                      "of": kind,
                      "text": f"{len(off)} {kind.lower()} caption(s) not {rule[0].replace('_', ' ')} the {kind.lower()} "
                              f"— the samples put {round(rule[1] * 100)} % there"})
    pages = _Pages(pdf_path)
    try:
        body = model.body_size or pages.body
        for n, role, text in _candidates(model):
            if len(res["nodes"]) >= 60:
                break
            f = measure(pages, n.page, n.bbox, text, n.style if (n.style or {}).get("size") else None, body)
            if f is None:
                continue
            r = pages.disp(n.page, n.bbox)
            pw, ph = pages.size(n.page)
            band = _band(r.y0 / ph, r.height / ph)
            if band and role == "P" and know.arts.get((norm(text), band), 0) >= 2:
                res["nodes"].append({"id": n.id, "role": role, "says": "Artifact", "share": 1.0, "text": text[:80],
                                     "why": "a running " + ("head" if band == "top" else "foot") + " in the samples"})
                continue
            wd = know.wording(text)
            if wd and wd[1] >= 0.85 and wd[2] >= 2 and wd[0] != role and wd[0] in SWAP:
                res["nodes"].append({"id": n.id, "role": role, "says": wd[0], "share": round(wd[1], 2), "text": text[:80],
                                     "why": f"this wording is {wd[0]} in the samples ({wd[2]}×)"})
                continue
            votes = know.vote(f)
            if votes and votes[0][0] != role and votes[0][0] in SWAP and votes[0][1] >= 0.85 and votes[0][2] <= 0.8:
                res["nodes"].append({"id": n.id, "role": role, "says": votes[0][0], "share": votes[0][1],
                                     "text": text[:80], "why": f"{round(votes[0][1] * 100)} % of the most similar sample tags"})
    finally:
        pages.close()
    return res


def suggest(model, pdf_path: str, nid: str, client: str = "", know: "Knowledge | None" = None) -> dict:
    """What the samples say about one tag (the editor's popover)."""
    know = know if know is not None else Knowledge(client)
    n = model.nodes.get(nid)
    if know.empty or n is None or not n.bbox:
        return {"files": know.files, "votes": []}
    pages = _Pages(pdf_path)
    try:
        text = " ".join((n.text or "").split())
        f = measure(pages, n.page, n.bbox, text, n.style if (n.style or {}).get("size") else None,
                    model.body_size or pages.body)
        votes = know.vote(f) if f else []
        wd = know.wording(text)
        return {"files": know.files, "client_files": know.client_files,
                "votes": [{"role": r, "share": s} for r, s, _ in votes[:4]],
                "wording": {"role": wd[0], "share": round(wd[1], 2), "n": wd[2]} if wd else None}
    finally:
        pages.close()


# ------------------------------------------------------------ worker
def main(argv: list[str]) -> int:
    try:
        from . import netguard                     # V4.26: nothing leaves the server
        netguard.install(process="learner")
    except Exception:
        pass
    if len(argv) >= 2 and argv[0] == "learn":
        for sid in argv[1:]:
            r = learn_sample(sid)
            print(json.dumps({"id": sid, **r}), flush=True)
        return 0
    if argv and argv[0] == "queued":
        done = 0
        while True:
            q = queued()
            if not q:
                break
            learn_sample(q[0])
            done += 1
        out = {"learned": done}
        if done:
            # V4.25: the AI studies the new samples, then the brain trains again
            try:
                from . import brain
                out["study"] = brain.study_pending()
            except Exception as exc:
                out["study"] = {"error": str(exc)[:200]}
            try:
                from . import brain
                out["train"] = brain.train_all(trigger=f"{done} sample file(s) learned")
            except Exception as exc:
                out["train"] = {"error": str(exc)[:200]}
        print(json.dumps(out, default=str))
        return 0
    print("usage: python -m core.learn learn <id>… | queued", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.path.insert(0, ROOT)
    sys.exit(main(sys.argv[1:]))
