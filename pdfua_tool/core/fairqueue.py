"""A work queue that is fair to many people (V4.28).

The queue of files to analyse and build used to be first come, first served. When one person
puts forty files in, everybody who comes after waits for all forty — even for a single page.
`FairQueue` serves the waiting people in turn: one file of the first person, one of the
second, one of the third … and then the first again. Inside one person's files the more
urgent ones go first (the file's priority), and an urgent file of anybody goes before the
others. It is a drop-in for `queue.Queue` where app.py uses it: `put`, `get(timeout=…)`,
`task_done`, `qsize`, and `queue` — the waiting items in the order they will run.
"""
from __future__ import annotations

import bisect
import collections
import itertools
import queue
import threading
import time
from typing import Any, Callable


class FairQueue:
    def __init__(self, key: Callable[[Any], tuple[str, float]] | None = None) -> None:
        """`key(item)` → (who it is for, rank). A lower rank goes first; people take turns among equal ranks."""
        self._key = key or (lambda item: ("", 0.0))
        self._cv = threading.Condition(threading.Lock())
        self._lists: dict[str, list] = {}            # who -> [(rank, seq, item, put time)] best first
        self._turn: collections.deque = collections.deque()   # the people waiting, in the order of their turns
        self._seq = itertools.count()
        self._n = 0
        self.unfinished_tasks = 0
        self.stats = {"put": 0, "served": 0, "wait_total": 0.0, "wait_max": 0.0}

    # ---------------------------------------------------------------- putting in
    def put(self, item: Any) -> None:
        try:
            who, rank = self._key(item)
        except Exception:
            who, rank = "", 0.0
        who = str(who or "")
        with self._cv:
            lst = self._lists.get(who)
            if lst is None:
                lst = self._lists[who] = []
                self._turn.append(who)               # a person with nothing waiting joins the end of the turns
            bisect.insort(lst, (float(rank), next(self._seq), item, time.monotonic()))   # seq is unique: items are never compared
            self._n += 1
            self.unfinished_tasks += 1
            self.stats["put"] += 1
            self._cv.notify()

    put_nowait = put

    # ---------------------------------------------------------------- taking out
    def _best(self, lists: dict, turn) -> str | None:
        best, who = None, None
        for w in turn:                               # in the order of the turns; a tie keeps the earlier one
            head = lists[w][0]
            if best is None or head[0] < best:
                best, who = head[0], w
        return who

    def get(self, block: bool = True, timeout: float | None = None) -> Any:
        with self._cv:
            if not block and not self._n:
                raise queue.Empty
            end = None if timeout is None else time.monotonic() + timeout
            while not self._n:
                rest = None if end is None else end - time.monotonic()
                if rest is not None and rest <= 0:
                    raise queue.Empty
                self._cv.wait(rest)
            who = self._best(self._lists, self._turn)
            lst = self._lists[who]
            rank, seq, item, t0 = lst.pop(0)
            self._turn.remove(who)
            if lst:
                self._turn.append(who)               # served: the next person's turn comes before this one's
            else:
                del self._lists[who]
            self._n -= 1
            waited = time.monotonic() - t0
            s = self.stats
            s["served"] += 1
            s["wait_total"] += waited
            s["wait_max"] = max(s["wait_max"], waited)
            return item

    get_nowait = lambda self: self.get(False)        # noqa: E731

    def task_done(self) -> None:
        with self._cv:
            if self.unfinished_tasks <= 0:
                raise ValueError("task_done() called too many times")
            self.unfinished_tasks -= 1
            if not self.unfinished_tasks:
                self._cv.notify_all()

    def join(self) -> None:
        with self._cv:
            while self.unfinished_tasks:
                self._cv.wait()

    def rekey(self, match: Callable[[Any], bool]) -> int:
        """The priority of waiting items changed (an admin made a file urgent): put the items for which
        `match(item)` is true in their new place. They keep their place in line among equals. -> how many."""
        with self._cv:
            items = [e[2] for lst in self._lists.values() for e in lst if match(e[2])]
        if not items:
            return 0
        keys = {}
        for it in items:                              # the key reads the file's data: not while holding the lock
            try:
                keys[id(it)] = self._key(it)
            except Exception:
                keys[id(it)] = ("", 0.0)
        moved = []
        with self._cv:
            for who in list(self._lists):
                keep = []
                for e in self._lists[who]:
                    (moved if id(e[2]) in keys else keep).append(e)
                if keep:
                    self._lists[who] = keep
                else:
                    del self._lists[who]
                    self._turn.remove(who)
            for rank, seq, item, t0 in moved:
                who, new = keys[id(item)]
                who = str(who or "")
                lst = self._lists.get(who)
                if lst is None:
                    lst = self._lists[who] = []
                    self._turn.append(who)
                bisect.insort(lst, (float(new), seq, item, t0))
        return len(moved)

    # ---------------------------------------------------------------- looking in
    def qsize(self) -> int:
        return self._n

    def empty(self) -> bool:
        return not self._n

    @property
    def queue(self) -> list:
        """The waiting items in the order they will run (a copy: safe to loop over while others put and get)."""
        with self._cv:
            lists = {w: list(l) for w, l in self._lists.items()}
            turn = collections.deque(self._turn)
        out: list = []
        while turn:
            who = self._best(lists, turn)
            out.append(lists[who].pop(0)[2])
            turn.remove(who)
            if lists[who]:
                turn.append(who)
        return out

    def waiting_by(self) -> dict[str, int]:
        with self._cv:
            return {w or "-": len(l) for w, l in self._lists.items()}

    def snapshot(self) -> dict:
        with self._cv:
            s = self.stats
            n = max(1, s["served"])
            return {"waiting": self._n, "people": len(self._lists), "put": s["put"], "served": s["served"],
                    "avg_wait_s": round(s["wait_total"] / n, 2), "max_wait_s": round(s["wait_max"], 2),
                    "unfinished": self.unfinished_tasks}
