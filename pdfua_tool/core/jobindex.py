"""Every file's meta.json, read once and kept (V4.30).

Before, every list of files — the Users page, the queue badge every editor asks for each 45 seconds, the Admin
pages, the dashboard — opened and parsed the meta.json of EVERY file on the server. With a few thousand files that
is hundreds of milliseconds of one Python thread per call, and each call holds up everybody else's edits.

Now a file's meta.json is parsed again only when it changed. A listing costs one `stat` per file at most every few
seconds — and nothing at all in between: the writer (`write_meta`) says which file it changed (`touch`), so the
next listing looks at that one file only.

The dicts handed out are shared: read them, never change them (copy first).
"""
from __future__ import annotations

import json
import os
import threading
import time


class JobIndex:
    def __init__(self, root: str, recheck: float = 4.0):
        self.root = root
        self.recheck = recheck                      # a full look at the folder at most this often (seconds)
        self._lock = threading.Lock()
        self._items: dict[str, tuple[tuple[int, int], dict]] = {}
        self._order: list[str] = []                 # newest first (the names begin with the date)
        self._dirty: set[str] = set()
        self._all_dirty = True
        self._checked = 0.0
        self.gen = 0                                # grows whenever any file's meta changed, appeared or went
        self.stats = {"scans": 0, "parsed": 0, "listings": 0}

    # --------------------------------------------------------------------------- writers say what they did
    def touch(self, name: str | None = None) -> None:
        """meta.json of `name` was written or removed (None: look at everything again)."""
        with self._lock:
            if name is None:
                self._all_dirty = True
            else:
                self._dirty.add(name)

    # ------------------------------------------------------------------------------------------ reading
    def _key(self, name: str):
        try:
            st = os.stat(os.path.join(self.root, name, "meta.json"))
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _parse(self, name: str):
        try:
            with open(os.path.join(self.root, name, "meta.json"), encoding="utf-8") as fh:
                m = json.load(fh)
        except Exception:
            return None
        return m if isinstance(m, dict) else None

    def _refresh(self) -> None:
        now = time.monotonic()
        with self._lock:
            full = self._all_dirty or now - self._checked >= self.recheck
            if not full and not self._dirty:
                return
            changed = False
            if full:
                try:
                    with os.scandir(self.root) as it:
                        names = [e.name for e in it if e.is_dir()]
                except OSError:
                    names = []
                have = set(names)
                for gone in [n for n in self._items if n not in have]:
                    del self._items[gone]
                    changed = True
                check = names
            else:
                check = list(self._dirty)
            for name in check:
                key = self._key(name)
                old = self._items.get(name)
                if key is None:
                    if old is not None:
                        del self._items[name]
                        changed = True
                    continue
                if old is not None and old[0] == key:
                    continue
                m = self._parse(name)
                if m is None:                        # half written, or not a job: keep what was known
                    continue
                self._items[name] = (key, m)
                self.stats["parsed"] += 1
                changed = True
            self._dirty.clear()
            if full:
                self._all_dirty = False
                self._checked = now
                self.stats["scans"] += 1
            if changed or len(self._order) != len(self._items):
                self._order = sorted(self._items, reverse=True)
                self.gen += 1

    def stamp(self) -> int:
        """A number that changes whenever any file's meta changed — to tell a cached answer is still good."""
        self._refresh()
        return self.gen

    def metas(self) -> list[dict]:
        """All files' meta, newest first. Shared objects — do not change them."""
        self._refresh()
        self.stats["listings"] += 1
        with self._lock:
            items = self._items
            return [items[n][1] for n in self._order if n in items]

    def get(self, name: str) -> dict | None:
        self._refresh()
        with self._lock:
            hit = self._items.get(name)
            return hit[1] if hit else None

    def count(self) -> int:
        self._refresh()
        return len(self._items)
