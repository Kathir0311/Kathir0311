"""Tag levels for several tags at once (V4.21.4, POST /levels).

* a run of tags side by side goes one level in (into the tag before it),
  together and in order; one level out again, right after its parent
* up / down move the run past its neighbour; two runs apart move each
* a tag inside another selected tag moves with it
* heading levels: the selected headings and the headings inside a selected
  Sect one level up / down, kept between H1 and H6
* nothing to do → a clear error, the tree unchanged; the tree stays sound
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import edit  # noqa: E402
from core.model import DocModel, StructNode  # noqa: E402


def tree(m: DocModel) -> list:
    def walk(i):
        n = m.nodes[i]
        return (n.id, [walk(c) for c in n.children]) if n.children else n.id
    return [walk(i) for i in m.root_children]


def make() -> DocModel:
    m = DocModel(path="x.pdf", page_count=1)
    for i, r in (("h", "H2"), ("a", "P"), ("b", "P"), ("c", "P"), ("d", "P")):
        m.add(StructNode(id=i, role=r, page=0, text=i))
    m.add(StructNode(id="s", role="Sect", page=0))
    m.add(StructNode(id="h3", role="H3", page=0, text="sub"), "s")
    m.add(StructNode(id="e", role="P", page=0, text="e"), "s")
    return m


def main() -> int:
    bad = []
    m = make()
    edit.levels(m, ["b", "c"], "indent")
    if tree(m) != ["h", ("a", ["b", "c"]), "d", ("s", ["h3", "e"])]:
        bad.append(f"indent two: {tree(m)}")
    edit.levels(m, ["c", "b"], "outdent")
    if tree(m) != ["h", "a", "b", "c", "d", ("s", ["h3", "e"])]:
        bad.append(f"outdent two: {tree(m)}")
    edit.levels(m, ["b", "c"], "up")
    if tree(m) != ["h", "b", "c", "a", "d", ("s", ["h3", "e"])]:
        bad.append(f"up: {tree(m)}")
    edit.levels(m, ["b", "c"], "down")
    if tree(m) != ["h", "a", "b", "c", "d", ("s", ["h3", "e"])]:
        bad.append(f"down: {tree(m)}")
    # two runs apart, each moves
    edit.levels(m, ["a", "c"], "down")
    if tree(m) != ["h", "b", "a", "d", "c", ("s", ["h3", "e"])]:
        bad.append(f"two runs down: {tree(m)}")
    # a selected child moves with its selected parent
    edit.levels(m, ["s", "e"], "up")
    if tree(m) != ["h", "b", "a", "d", ("s", ["h3", "e"]), "c"]:
        bad.append(f"parent and child: {tree(m)}")
    try:
        edit.levels(m, ["h"], "up")
        bad.append("moved the first tag up")
    except edit.EditError:
        pass
    try:
        edit.levels(m, ["h"], "outdent")
        bad.append("outdented a top-level tag")
    except edit.EditError:
        pass
    # heading levels: a heading and the headings inside a Sect
    r = edit.levels(m, ["h", "s"], "heading_down")
    if (m.nodes["h"].role, m.nodes["h3"].role, r["changed"]) != ("H3", "H4", 2):
        bad.append(f"heading down: {m.nodes['h'].role} {m.nodes['h3'].role} {r}")
    for _ in range(5):
        edit.levels(m, ["h"], "heading_up")
    if m.nodes["h"].role != "H1":
        bad.append(f"heading up stops at H1: {m.nodes['h'].role}")
    try:
        edit.levels(m, ["a"], "heading_up")
        bad.append("a paragraph got a heading level")
    except edit.EditError:
        pass
    m.check_integrity()
    if bad:
        print("tag levels test: FAILED")
        for x in bad:
            print("   ", x)
        return 1
    print("tag levels test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
