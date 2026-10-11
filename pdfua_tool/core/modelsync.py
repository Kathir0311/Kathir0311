"""What an edit changed (V4.30) — so the save, the reply and the editor's screen deal with that and nothing else.

Before, every edit wrote the whole file again (every tag of a 300-page book: ~80 ms of one Python thread), and then
the editor asked the server for the whole file, the issues, the colours and the cross-references, and drew all
its thousands of rows again. That is what made a single changed tag take seconds on a big file.

A `Shadow` is a copy of every tag as it was when the file was last saved, kept next to the file's tags in memory.
After an edit the live tags are compared with it (dictionary against dictionary, in C — a few milliseconds for
five thousand tags), which says exactly which tags changed, appeared or went. Those are
  * written again — the JSON text of every other tag is kept and joined, byte for byte what it was,
  * told to the editor ("patch"), which changes those rows and nothing else.

Nothing here decides *what* is a change: a tag counts as changed when its fields differ in any way from the
copy, so an edit function that changes a list or a dictionary in place is seen as well.
"""
from __future__ import annotations

import json
from typing import Any

_enc = json.JSONEncoder(ensure_ascii=False, separators=(",", ":")).encode

# the file's own fields, besides its tags (see DocModel.to_json)
TOP_FIELDS = ("path", "page_count", "page_sizes", "root_children", "title", "lang", "body_size",
              "warnings", "mode", "counter", "info")


def snap(v: Any) -> Any:
    """A copy of a value that no later change of the original can reach."""
    t = type(v)
    if t is list:
        return [snap(x) for x in v]
    if t is dict:
        return {k: snap(x) for k, x in v.items()}
    if t is tuple:
        return tuple(snap(x) for x in v)
    if t is set:
        return set(v)
    return v


def node_json(n) -> str:
    """The JSON text of one tag (what the file holds for it)."""
    try:
        return _enc(n.__dict__)
    except (TypeError, ValueError):                 # an attribute that is not data: only the fields count
        from .model import node_dict
        return _enc(node_dict(n))


class Shadow:
    """The tags of one file as they were when last saved, and their JSON text."""

    __slots__ = ("owner", "nodes", "piece", "top", "nums", "order", "saves")

    def __init__(self, owner, model) -> None:
        self.owner = owner                           # the DocModel this belongs to (identity)
        self.nodes: dict[str, dict] = {nid: snap(n.__dict__) for nid, n in model.nodes.items()}
        self.piece: dict[str, str] = {nid: _enc(nid) + ":" + node_json(n) for nid, n in model.nodes.items()}
        self.top: dict[str, Any] = {k: snap(getattr(model, k)) for k in TOP_FIELDS}
        self.nums: dict | None = None                # {id: (reading number, block)} the editor was last told
        self.order: dict | None = None               # {id: position in reading order} the same
        self.saves = 0

    # ---------------------------------------------------------------------------------- what changed
    def diff(self, model) -> tuple[list[str], list[str], list[str]]:
        """(tags changed or new, tags gone, the file's own fields that changed)."""
        sn, cur = self.nodes, model.nodes
        changed = [nid for nid, n in cur.items() if sn.get(nid) != n.__dict__]
        gone = [nid for nid in sn if nid not in cur]
        tops = [k for k in TOP_FIELDS if self.top.get(k) != getattr(model, k)]
        return changed, gone, tops

    def commit(self, model, changed: list[str], gone: list[str], tops: list[str]) -> None:
        """The copy follows the live tags (after the file was written, or when nobody needs the difference)."""
        cur = model.nodes
        for nid in changed:
            n = cur[nid]
            self.nodes[nid] = snap(n.__dict__)
            self.piece[nid] = _enc(nid) + ":" + node_json(n)
        for nid in gone:
            self.nodes.pop(nid, None)
            self.piece.pop(nid, None)
        for k in tops:
            self.top[k] = snap(getattr(model, k))

    # ---------------------------------------------------------------------------------------- the file
    def dump(self, model) -> str:
        """The file's JSON — the same text DocModel.to_json() writes, from the kept pieces."""
        piece = self.piece
        try:
            parts = [piece[nid] for nid in model.nodes]
        except KeyError:                             # a tag the copy has not seen: written from the tag itself
            parts = []
            for nid, n in model.nodes.items():
                f = piece.get(nid)
                if f is None:
                    f = piece[nid] = _enc(nid) + ":" + node_json(n)
                parts.append(f)
        head = _enc({k: getattr(model, k) for k in TOP_FIELDS})
        return head[:-1] + ',"nodes":{' + ",".join(parts) + "}}"
