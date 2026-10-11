"""One person at a time in a file (V4.29).

While somebody works in a file, nobody else may change it: the others can open it and look — the
tags, the pages, the issues — but every change is refused. The lock is *taken* when a person opens
the editor (or presses *Edit this file* in a file they were only looking at) and *kept* while the
editor is open: it asks every few seconds ("I am still here"). It ends

* when the person leaves (the editor lets go), or the editor stops asking for `ttl` seconds — a closed
  laptop, a lost connection — so a crashed tab never keeps a file for ever;
* when the person has done nothing in the file for `idle` seconds — a tab left open over lunch;
* when the file moves on in the workflow (sent to QA, approved …) or an admin takes it over.

The locks live in memory (one server process, like the queue); after a restart the open editors take
their files again with their next "I am still here".
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

TTL = 40.0           # seconds without a sign of life from the editor
IDLE = 600.0         # seconds without anything done in the file


def _same(a: str, b: str) -> bool:
    return (a or "").strip().lower() == (b or "").strip().lower()


class EditLocks:
    def __init__(self, ttl: float = TTL, idle: float = IDLE) -> None:
        self.ttl = ttl
        self.idle = idle
        self._d: dict[str, dict] = {}
        self._mu = threading.Lock()
        self.stats = {"taken": 0, "refused": 0, "released": 0, "expired": 0, "taken_over": 0}

    # ---------------------------------------------------------------- inside (the mutex is held)
    def _live(self, jid: str, now: float) -> dict | None:
        h = self._d.get(jid)
        if h is None:
            return None
        if now - h["beat"] > self.ttl or now - h["active"] > self.idle:
            del self._d[jid]
            self.stats["expired"] += 1
            return None
        return h

    @staticmethod
    def _public(h: dict | None, now: float) -> dict | None:
        if h is None:
            return None
        return {"user": h["user"], "since": h["since"], "idle_s": int(now - h["active"]),
                "seen_s": int(now - h["beat"])}

    # ---------------------------------------------------------------- the lock
    def holder(self, jid: str) -> dict | None:
        """Who has the file now: {user, since, idle_s, seen_s} or None."""
        now = time.time()
        with self._mu:
            return self._public(self._live(jid, now), now)

    def acquire(self, jid: str, user: str, force: bool = False) -> tuple[bool, dict | None]:
        """Take the file — or renew it, when it is already this person's. -> (it is theirs, who has it)."""
        now = time.time()
        with self._mu:
            h = self._live(jid, now)
            if h is not None and not _same(h["user"], user):
                if not force:
                    self.stats["refused"] += 1
                    return False, self._public(h, now)
                self.stats["taken_over"] += 1
                h = None
            if h is None:
                h = self._d[jid] = {"user": user, "since": datetime.now().isoformat(timespec="seconds"),
                                    "beat": now, "active": now}
                self.stats["taken"] += 1
            else:
                h["beat"] = h["active"] = now
            return True, self._public(h, now)

    def renew(self, jid: str, user: str, active: bool = False) -> tuple[bool, dict | None]:
        """The editor's "I am still here". `active`: the person did something in the last minute.
        Renews the person's own lock; takes a free file only for a person who is working (after a restart,
        or when the lock ran out while they were thinking) — never for a tab that is only open; leaves
        somebody else's alone."""
        now = time.time()
        with self._mu:
            h = self._live(jid, now)
            if h is None:
                if not active:
                    return False, None
                h = self._d[jid] = {"user": user, "since": datetime.now().isoformat(timespec="seconds"),
                                    "beat": now, "active": now}
                self.stats["taken"] += 1
            elif _same(h["user"], user):
                h["beat"] = now
                if active:
                    h["active"] = now
            else:
                return False, self._public(h, now)
            return True, self._public(h, now)

    def touch(self, jid: str, user: str) -> None:
        """The holder changed something."""
        now = time.time()
        with self._mu:
            h = self._live(jid, now)
            if h is not None and _same(h["user"], user):
                h["beat"] = h["active"] = now

    def release(self, jid: str, user: str | None = None, force: bool = False) -> bool:
        """Let go of the file (only the holder's own lock unless `force`)."""
        with self._mu:
            h = self._d.get(jid)
            if h is None or not (force or user is None or _same(h["user"], user)):
                return False
            del self._d[jid]
            self.stats["released"] += 1
            return True

    # ---------------------------------------------------------------- looking in
    def snapshot(self) -> dict[str, dict]:
        now = time.time()
        with self._mu:
            return {jid: self._public(h, now) for jid, h in list(self._d.items()) if self._live(jid, now) is not None}

    def summary(self) -> dict:
        snap = self.snapshot()
        return {"locked": len(snap), **self.stats}


def reason(h: dict | None) -> str:
    """What a person who cannot change the file is told."""
    if not h:
        return ""
    return f"{h['user']} is working on this file now — you can look at it, not change it."
