"""What people wrote once, the tool remembers (V4.26).

* **Pictures.** Every picture has a digest of its pixels (core/inventory).
  When a person describes a figure — the publisher's logo, an icon, a
  badge used in every book — the description is kept with the digest. The
  next file that draws the same picture gets the same alt text at once
  (before the AI is asked, so it is also quicker), with a note saying where
  it was learned.
* **Readings.** When a person sets what a tag is *read as* (/ActualText) —
  "Rr" → "uppercase R, lowercase r", "_____" → "Blank line" — the reading is
  kept with the exact printed text, and the next tag printing that text
  gets it.

A built file teaches (unverified); an approved file confirms (verified). A
memory is used when it was confirmed, or when two files agree. The
client's own memories come first; with "Samples to learn from = all"
another client's confirmed memory is used too.
"""
from __future__ import annotations

import os
from datetime import datetime

from . import learn as L

SCHEMA = """
CREATE TABLE IF NOT EXISTS alt_memory (job TEXT, client TEXT, digest TEXT, alt TEXT, verified INTEGER, updated TEXT,
  PRIMARY KEY (job, digest));
CREATE INDEX IF NOT EXISTS alt_memory_digest ON alt_memory(digest);
CREATE TABLE IF NOT EXISTS say_memory (job TEXT, client TEXT, text TEXT, said TEXT, verified INTEGER, updated TEXT,
  PRIMARY KEY (job, text));
CREATE INDEX IF NOT EXISTS say_memory_text ON say_memory(text);
"""
TEXT_MAX = 80


def ensure(c) -> None:
    c.executescript(SCHEMA)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _disp(doc, page: int, box):
    import pymupdf
    r = pymupdf.Rect(box) * doc[page].transformation_matrix
    r.normalize()
    return [r.x0, r.y0, r.x1, r.y1]


def learn(job_path: str, client: str, model, auto=None, verified: bool = False) -> dict:
    """Keep the alt texts and readings people gave in this file."""
    from . import inventory
    import pymupdf
    job = os.path.basename(os.path.normpath(job_path))
    work = os.path.join(job_path, "working.pdf")
    if not os.path.exists(work):
        return {}
    inv = inventory.load(job_path)
    alts, says = [], []
    doc = pymupdf.open(work)
    try:
        if inv is None:
            inv = inventory.build(work, deep=False)
        for n in model.nodes.values():
            a = auto.nodes.get(n.id) if auto is not None else None
            if n.role == "Figure" and not n.artifact and (n.alt or "").strip() and n.bbox and 0 <= n.page < doc.page_count:
                by_person = a is None or (a.alt or "") != n.alt or n.source == "user"
                if not (verified or by_person):
                    continue
                if n.source == "memory" and not verified:
                    continue                     # what the tool itself filled in teaches nothing new
                dg = inventory.digests_in(inv, n.page, _disp(doc, n.page, n.bbox))
                if len(set(dg)) == 1:
                    alts.append((job, client, dg[0], n.alt.strip()[:500], 1 if verified else 0, _now()))
            said = (n.actual_text or "").strip()
            text = " ".join((n.text or "").split())
            if said and text and len(text) <= TEXT_MAX and not n.artifact and said != text:
                by_person = a is None or (a.actual_text or "") != n.actual_text
                if verified or by_person:
                    says.append((job, client, text, said[:300], 1 if verified else 0, _now()))
    finally:
        doc.close()
    with L.connect() as c:
        ensure(c)
        c.execute("DELETE FROM alt_memory WHERE job=?", (job,))
        c.execute("DELETE FROM say_memory WHERE job=?", (job,))
        c.executemany("INSERT OR REPLACE INTO alt_memory VALUES (?,?,?,?,?,?)", alts)
        c.executemany("INSERT OR REPLACE INTO say_memory VALUES (?,?,?,?,?,?)", says)
    return {"alts": len(alts), "readings": len(says)}


def _best(rows, client: str, any_client: bool):
    """(value, how) of the memory to use, or None."""
    agg: dict = {}
    for r in rows:
        v = r[1]
        e = agg.setdefault(v, {"n": 0, "verified": 0, "own": 0, "jobs": set()})
        e["n"] += 1
        e["verified"] += int(r[2] or 0)
        e["own"] += 1 if r[0] == client else 0
        e["jobs"].add(r[3])
    good = []
    for v, e in agg.items():
        if not (e["verified"] or len(e["jobs"]) >= 2):
            continue
        if not e["own"] and not (any_client and e["verified"]):
            continue
        good.append(((e["own"] > 0, e["verified"], e["n"]), v, e))
    if not good:
        return None
    good.sort(key=lambda t: t[0], reverse=True)
    _, v, e = good[0]
    return v, f"{e['n']} file(s){', confirmed' if e['verified'] else ''}"


def apply(model, job_path: str, client: str, any_client: bool = False, inv: dict | None = None) -> dict:
    """Give figures and tags what people gave the same picture or text before."""
    from . import inventory
    import pymupdf
    out = {"alts": 0, "readings": 0}
    if not L.has_data():
        return out
    work = os.path.join(job_path, "working.pdf")
    inv = inv or inventory.load(job_path)
    if inv is None:
        return out
    with L.connect() as c:
        ensure(c)
        if not c.execute("SELECT 1 FROM alt_memory LIMIT 1").fetchone() and \
                not c.execute("SELECT 1 FROM say_memory LIMIT 1").fetchone():
            return out
        doc = pymupdf.open(work)
        try:
            for n in list(model.nodes.values()):
                if n.source == "user" or n.artifact:
                    continue
                if n.role == "Figure" and not (n.alt or "").strip() and n.bbox and 0 <= n.page < doc.page_count:
                    dg = inventory.digests_in(inv, n.page, _disp(doc, n.page, n.bbox))
                    if len(set(dg)) != 1:
                        continue
                    rows = [(r["client"], r["alt"], r["verified"], r["job"]) for r in c.execute(
                        "SELECT client, alt, verified, job FROM alt_memory WHERE digest=?", (dg[0],))]
                    best = _best(rows, client, any_client)
                    if best:
                        n.alt, n.source = best[0], "memory"
                        n.note = (n.note + "; " if n.note else "") + f"memory: the same picture was described so in {best[1]}"
                        out["alts"] += 1
                    continue
                text = " ".join((n.text or "").split())
                if text and len(text) <= TEXT_MAX and not (n.actual_text or "").strip() and \
                        n.role not in ("Figure", "Formula", "Table", "L", "LI", "TR", "Link", "Document", "Sect"):
                    rows = [(r["client"], r["said"], r["verified"], r["job"]) for r in c.execute(
                        "SELECT client, said, verified, job FROM say_memory WHERE text=?", (text,))]
                    best = _best(rows, client, any_client)
                    if best:
                        n.actual_text = best[0]
                        n.note = (n.note + "; " if n.note else "") + f"memory: read as people set it in {best[1]}"
                        out["readings"] += 1
        finally:
            doc.close()
    return out


def stats() -> dict:
    if not L.has_data():
        return {"pictures": 0, "readings": 0}
    with L.connect() as c:
        ensure(c)
        return {"pictures": c.execute("SELECT COUNT(DISTINCT digest) n FROM alt_memory").fetchone()["n"],
                "pictures_confirmed": c.execute("SELECT COUNT(DISTINCT digest) n FROM alt_memory WHERE verified=1")
                .fetchone()["n"],
                "readings": c.execute("SELECT COUNT(DISTINCT text) n FROM say_memory").fetchone()["n"],
                "examples": [dict(r) for r in c.execute(
                    "SELECT text, said, COUNT(*) n, MAX(verified) v FROM say_memory GROUP BY text, said "
                    "ORDER BY v DESC, n DESC LIMIT 12")],
                "alts": [dict(r) for r in c.execute(
                    "SELECT substr(digest,1,10) digest, alt, COUNT(*) n, MAX(verified) v FROM alt_memory "
                    "GROUP BY digest, alt ORDER BY v DESC, n DESC LIMIT 12")]}
