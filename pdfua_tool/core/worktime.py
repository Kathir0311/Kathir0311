"""How long people really work on a file (V4.30): the time they are active — never a break.

The editor of a person who works counts the seconds in which something was done in it (a key, a click, a drag,
the mouse wheel). A pause between two such moments shorter than the idle limit (Admin → Workflow, 2 minutes by
default) is thinking and counts; a longer pause is a break and does not. The editor reports those seconds with its
heartbeat; this module keeps them, per file and per person, as

  * Working — while the file is in production (also rework), and
  * QC      — while it is with QA or Final QA,

summed in the file's meta.json (`time`, so the lists of files have them for nothing) and day by day in
`worktime.json` next to it (the reports). Written at most every 30 seconds per file and when the server stops.
"""
from __future__ import annotations

import atexit
import json
import os
import threading
import time
from datetime import datetime

QC_STAGES = ("qa", "final_qa")
DAYS_KEPT = 400
FLUSH_EVERY = 30.0            # seconds: a busy file is written to disk at most this often


def kind_of(stage: str) -> str:
    """'qc' while the file is with QA / Final QA, else 'work'."""
    return "qc" if stage in QC_STAGES else "work"


def fmt(seconds: float) -> str:
    """'1 h 05 min', '12 min', '45 s' — the way the pages show a duration."""
    s = int(round(seconds or 0))
    if s < 60:
        return f"{s} s"
    m = s // 60
    if m < 60:
        return f"{m} min"
    return f"{m // 60} h {m % 60:02d} min"


class WorkTimes:
    def __init__(self, load_meta, save_totals, day_file):
        """load_meta(jid) -> the file's stored totals dict or None · save_totals(jid, totals) writes them into the
        file's meta · day_file(jid) -> the path of the file's worktime.json (or '' when the file is gone)."""
        self._load_meta = load_meta
        self._save = save_totals
        self._day_file = day_file
        self._lock = threading.Lock()
        self._pend: dict[str, dict[tuple[str, str], float]] = {}     # jid -> {(user, kind): seconds not written yet}
        self._seen: dict[tuple[str, str], float] = {}                # (jid, user) -> when the last report came
        self._due: dict[str, float] = {}                             # jid -> when its pending seconds were first held
        self.flushes = 0
        self._thread: threading.Thread | None = None
        atexit.register(self.flush_all)

    # ----------------------------------------------------------------------------------------- counting
    def add(self, jid: str, user: str, kind: str, seconds: float, now: float | None = None,
            max_report: float = 20.0) -> float:
        """Count `seconds` of work reported by `user`'s editor on `jid`. A report cannot hold more seconds than have
        passed since the last one from the same person (plus a little), so a clock that runs wild counts nothing
        extra. Returns the seconds counted."""
        now = time.time() if now is None else now
        seconds = max(0.0, min(float(seconds or 0.0), max_report))
        if seconds <= 0 or not user:
            return 0.0
        k = (jid, user.lower())
        with self._lock:
            last = self._seen.get(k)
            self._seen[k] = now
            room = (now - last) * 1.25 + 1.0 if last is not None else 12.0
            seconds = min(seconds, room)
            if seconds <= 0:
                return 0.0
            d = self._pend.setdefault(jid, {})
            d[(user, kind)] = d.get((user, kind), 0.0) + seconds
            self._due.setdefault(jid, now)
            self._start()
        return seconds

    def pending(self, jid: str, user: str | None = None) -> dict:
        """The seconds counted and not written yet: {'work': s, 'qc': s} (for one person, or everybody)."""
        out = {"work": 0.0, "qc": 0.0}
        with self._lock:
            for (u, kind), s in (self._pend.get(jid) or {}).items():
                if user is None or u.lower() == user.lower():
                    out[kind] += s
        return out

    def totals(self, jid: str, user: str | None = None) -> dict:
        """What is written plus what is held: {'work': s, 'qc': s} (for one person, or the whole file)."""
        stored = self._load_meta(jid) or {}
        out = {"work": 0.0, "qc": 0.0}
        if user is None:
            out["work"], out["qc"] = float(stored.get("work") or 0), float(stored.get("qc") or 0)
        else:
            for u, v in (stored.get("by") or {}).items():
                if u.lower() == user.lower():
                    out["work"], out["qc"] = float(v.get("work") or 0), float(v.get("qc") or 0)
        p = self.pending(jid, user)
        out["work"] += p["work"]
        out["qc"] += p["qc"]
        return out

    def pending_all(self) -> dict:
        """{jid: {(user, kind): seconds}} counted and not written yet (a copy)."""
        with self._lock:
            return {j: dict(d) for j, d in self._pend.items() if d}

    def stamp(self) -> tuple:
        """Changes when time was written, and as seconds are counted (every 5 seconds' worth) — to tell a cached list is old."""
        with self._lock:
            held = sum(sum(d.values()) for d in self._pend.values())
        return (self.flushes, int(held // 5))

    # ------------------------------------------------------------------------------------------ writing
    def _start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, daemon=True, name="worktime")
            self._thread.start()

    def _loop(self) -> None:
        while True:
            time.sleep(5.0)
            now = time.time()
            with self._lock:
                due = [j for j, t in self._due.items() if now - t >= FLUSH_EVERY]
                idle = not self._pend
            for jid in due:
                self.flush(jid)
            if idle:
                return

    def flush(self, jid: str) -> None:
        with self._lock:
            d = self._pend.pop(jid, None)
            self._due.pop(jid, None)
        if not d:
            return
        now = datetime.now().isoformat(timespec="seconds")
        today = now[:10]
        try:
            t = self._load_meta(jid) or {}
            by = t.setdefault("by", {})
            for (user, kind), s in d.items():
                u = by.setdefault(user, {"work": 0.0, "qc": 0.0})
                u[kind] = round(float(u.get(kind) or 0) + s, 1)
                u["last"] = now
                t[kind] = round(float(t.get(kind) or 0) + s, 1)
            t.setdefault("first", now)
            t["last"] = now
            self._save(jid, t)
            self._write_days(jid, d, today)
            self.flushes += 1
        except Exception:
            # the file may be gone (deleted): its time goes with it
            pass

    def flush_all(self) -> None:
        with self._lock:
            jids = list(self._pend)
        for jid in jids:
            self.flush(jid)

    def _write_days(self, jid: str, d: dict, today: str) -> None:
        p = self._day_file(jid)
        if not p:
            return
        try:
            with open(p, encoding="utf-8") as fh:
                days = json.load(fh).get("days") or {}
        except Exception:
            days = {}
        day = days.setdefault(today, {})
        for (user, kind), s in d.items():
            u = day.setdefault(user, {"work": 0.0, "qc": 0.0})
            u[kind] = round(float(u.get(kind) or 0) + s, 1)
        for old in sorted(days)[:-DAYS_KEPT]:
            days.pop(old, None)
        tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"days": days}, fh)
        os.replace(tmp, p)

    def days(self, jid: str) -> dict:
        """{day: {user: {'work': s, 'qc': s}}} written so far."""
        p = self._day_file(jid)
        try:
            with open(p, encoding="utf-8") as fh:
                return json.load(fh).get("days") or {}
        except Exception:
            return {}
