"""Locks for many people working at the same time (V4.28).

MuPDF is not thread-safe, so the editor's requests that open a PDF run one after another
(app.py: MUPDF_LOCK). Python's own RLock lets whoever it likes in next: with twenty people
at the editor an unlucky request can wait for ever while the others pass it. `FairRLock` is
re-entrant like an RLock but serves the waiting threads in the order they came, and it
counts how long people wait, so the admin page can say whether the lock is a bottleneck.
"""
from __future__ import annotations

import collections
import os
import threading
import time


class FairRLock:
    """A re-entrant lock that serves its waiting threads first come, first served."""

    def __init__(self) -> None:
        self._cv = threading.Condition(threading.Lock())
        self._owner: int | None = None
        self._count = 0
        self._queue: collections.deque = collections.deque()
        self.stats = {"acquired": 0, "waited": 0, "wait_total": 0.0, "wait_max": 0.0, "held_total": 0.0,
                      "held_max": 0.0, "timeouts": 0}
        self._since = 0.0

    # ---------------------------------------------------------------- the lock
    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        me = threading.get_ident()
        with self._cv:
            if self._owner == me:
                self._count += 1
                return True
            if not blocking and (self._owner is not None or self._queue):
                return False
            t0 = time.monotonic()
            ticket = object()
            self._queue.append(ticket)
            end = None if timeout is None or timeout < 0 else t0 + timeout
            while self._owner is not None or self._queue[0] is not ticket:
                rest = None if end is None else end - time.monotonic()
                if rest is not None and rest <= 0:
                    self._queue.remove(ticket)
                    self.stats["timeouts"] += 1
                    self._cv.notify_all()
                    return False
                self._cv.wait(rest)
            self._queue.popleft()
            self._owner, self._count = me, 1
            waited = time.monotonic() - t0
            s = self.stats
            s["acquired"] += 1
            if waited > 0.002:
                s["waited"] += 1
                s["wait_total"] += waited
                s["wait_max"] = max(s["wait_max"], waited)
            self._since = time.monotonic()
            return True

    def release(self) -> None:
        with self._cv:
            if self._owner != threading.get_ident():
                raise RuntimeError("cannot release an un-acquired lock")
            self._count -= 1
            if self._count == 0:
                held = time.monotonic() - self._since
                self.stats["held_total"] += held
                self.stats["held_max"] = max(self.stats["held_max"], held)
                self._owner = None
                self._cv.notify_all()

    def _is_owned(self) -> bool:
        return self._owner == threading.get_ident()

    def waiting(self) -> int:
        return len(self._queue)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def snapshot(self) -> dict:
        s = dict(self.stats)
        n = max(1, s["acquired"])
        return {"requests": s["acquired"], "waited": s["waited"], "waiting_now": self.waiting(),
                "avg_wait_ms": round(1000 * s["wait_total"] / n, 1), "max_wait_ms": round(1000 * s["wait_max"]),
                "avg_hold_ms": round(1000 * s["held_total"] / n, 1), "max_hold_ms": round(1000 * s["held_max"]),
                "timeouts": s["timeouts"]}


def replace_file(src: str, dst: str, tries: int = 12) -> None:
    """`os.replace`, patient on Windows: a file another process has open for reading (the learner process
    reads the tags a moment after an edit) cannot be replaced there until it is closed — a few
    milliseconds. Everywhere else this is os.replace."""
    for k in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != "nt" or k == tries - 1:
                raise
            time.sleep(0.03 * (k + 1))
