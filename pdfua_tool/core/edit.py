"""Editing operations on the model (used by the editor API)."""
from __future__ import annotations

import collections
import re
import statistics

import pymupdf

from .analyzer import (LABEL_RE, RAW_FLAGS, UNDERSCORE_RUN_RE, Analyzer, Line, _ubox,
                       collect_lines, printed_text, split_prompts, to_pdf, xycut)
from .lint import table_grid
from .model import (CONTAINER_ROLES, EDITOR_ROLES, DocModel, StructNode, area,
                    clamp_heading_levels, clean_text, convert_empty_headers, intersection,
                    FLOW_GROUPS, flow_position, misplaced_by_page, open_pages_with_heading, position_index,
                    union, wrap_table_bodies, HEADINGS)
from .options import Options

EDITABLE = {"alt": 2000, "actual_text": 2000, "expansion": 500, "text": 4000, "lang": 35,
            "scope": 10, "id_attr": 120, "list_numbering": 20, "artifact_subtype": 20,
            "pagination_subtype": 20, "note": 500}


class EditError(Exception):
    pass


def set_role(model: DocModel, nid: str, role: str) -> None:
    if role not in EDITOR_ROLES:
        raise EditError(f"unknown tag type {role}")
    n = model.nodes[nid]
    if role == "Artifact":
        n.artifact = True
        if n.artifact_subtype not in ("Pagination", "Layout", "Page", "Background"):
            n.artifact_subtype = "Layout"
        if not n.role or n.role == "Artifact":
            n.role = "P"
    else:
        was_artifact = n.artifact
        n.artifact = False
        n.role = role
        if was_artifact:
            _keep_in_page_sequence(model, nid)
    n.source = "user"
    n.confidence = 1.0


def _keep_in_page_sequence(model: DocModel, nid: str) -> None:
    """An artifact that becomes a real tag keeps its place — unless that place
    is outside its page (artifacts imported from the original file used to be
    listed at the end); then it goes to its position on its own page."""
    n = model.nodes[nid]
    if n.page is None or n.page < 0:
        return
    sib = model.siblings(nid)
    i = sib.index(nid)
    blocks = [model.nodes[x] for x in sib if x in model.nodes and not model.nodes[x].artifact
              and (model.nodes[x].page or 0) >= 0]
    before = [b.page for b in blocks[:[b.id for b in blocks].index(nid)]]
    after = [b.page for b in blocks[[b.id for b in blocks].index(nid) + 1:]]
    if (before and before[-1] > n.page) or (after and after[0] < n.page):
        sib.pop(i)
        sib.insert(position_index(model, sib, n), nid)


def fix_page_order(model: DocModel, opts: Options | None = None) -> int:
    """Move blocks that break the page sequence back to their page (by position),
    and let each page open with its own heading (`page_title_first`)."""
    moved = 0
    mode = getattr(opts or Options(), "page_title_first", "margin")
    for _ in range(3):
        changed = 0
        lists = [model.root_children] + [n.children for n in model.nodes.values()
                                         if n.role in FLOW_ROLES and n.children]
        for sib in lists:
            bad = misplaced_by_page(model, list(sib))
            bad.sort(key=lambda i: (model.nodes[i].page, -(model.nodes[i].bbox or (0, 0, 0, 0))[3]))
            for i in bad:
                n = model.nodes[i]
                # to its page — inside the section that holds that page, when the
                # page's blocks live in one (a whole book in one Sect)
                pid, idx = flow_position(model, n.page, n.bbox, skip={i})
                model.detach(i)
                model.add(n, pid, idx)
                changed += 1
        moved += changed
        if not changed:
            break
    if mode != "off" and not model.info.get("teachers_guide") and open_pages_with_heading(model, mode):
        clamp_heading_levels(model)          # the order decides the levels
        moved += 1
    return moved


def fix_empty_headers(model: DocModel) -> int:
    """Turn every empty header cell (TH) into a data cell (TD)."""
    return convert_empty_headers(model)


def order_problems(model: DocModel) -> list[str]:
    out = []
    lists = [model.root_children] + [n.children for n in model.nodes.values()
                                     if n.role in FLOW_ROLES and n.children]
    for sib in lists:
        out.extend(misplaced_by_page(model, list(sib)))
    return out


def set_fields(model: DocModel, nid: str, fields: dict) -> None:
    n = model.nodes[nid]
    if "role" in fields:
        set_role(model, nid, str(fields["role"]))
    for k, limit in EDITABLE.items():
        if k in fields:
            setattr(n, k, clean_text(str(fields[k] or ""))[:limit])
    for k in ("rowspan", "colspan"):
        if k in fields:
            try:
                setattr(n, k, max(1, min(200, int(fields[k]))))
            except Exception:
                pass
    if "blank_line" in fields:
        st = dict(n.style or {})
        if fields["blank_line"]:
            st["blank_line"] = True
        else:
            st.pop("blank_line", None)
        n.style = st
    if "headers" in fields and isinstance(fields["headers"], list):
        n.headers = [h for h in fields["headers"] if h in model.nodes]
    if any(k in fields for k in ("alt", "actual_text", "text", "role")):
        n.source = "user"
        n.confidence = 1.0


def tidy_tables(model: DocModel, groups: bool = True) -> int:
    """After a table edit: no empty header cells, and every table keeps its rows
    in THead / TBody (a table with no header needs no THead, but it does need
    the TBody)."""
    n = convert_empty_headers(model)
    if groups:
        n += wrap_table_bodies(model)
    return n


def _subtree_ids(model: DocModel, nid: str) -> set[str]:
    out = {nid}
    for d in model.descendants(nid):
        out.add(d.id)
    return out


def move(model: DocModel, nid: str, action: str, parent: str | None = None,
         index: int | None = None) -> None:
    if nid not in model.nodes:
        raise EditError("no such tag")
    node = model.nodes[nid]
    sib = model.siblings(nid)
    i = sib.index(nid)
    if action in ("up", "down"):
        j = i - 1 if action == "up" else i + 1
        if 0 <= j < len(sib):
            sib[i], sib[j] = sib[j], sib[i]
    elif action == "outdent":
        if node.parent is None:
            return
        par = model.nodes[node.parent]
        grand = model.siblings(par.id)
        sib.pop(i)
        node.parent = par.parent
        grand.insert(grand.index(par.id) + 1, nid)
    elif action == "indent":
        if i == 0:
            return
        newp = model.nodes[sib[i - 1]]
        sib.pop(i)
        newp.children.append(nid)
        node.parent = newp.id
    elif action == "delete":
        model.remove(nid, promote_children=True)
    elif action == "delete_subtree":
        model.remove(nid, promote_children=False)
    elif action == "merge_next":
        if i + 1 >= len(sib):
            return
        merge(model, [nid, sib[i + 1]])
    elif action == "move_to":
        if parent is not None and parent not in model.nodes:
            raise EditError("no such parent")
        if parent is not None and parent in _subtree_ids(model, nid):
            raise EditError("cannot move a tag inside itself")
        sib.pop(i)
        dest = model.root_children if parent is None else model.nodes[parent].children
        j = len(dest) if index is None else max(0, min(int(index), len(dest)))
        dest.insert(j, nid)
        node.parent = parent
    else:
        raise EditError(f"unknown action {action}")


def levels(model: DocModel, ids: list[str], action: str) -> dict:
    """Several tags a level in or out, up or down, or their heading levels one
    up or down — in one change (V4.21.4).

    * indent: each run of selected tags that stand side by side goes into the
      tag just before the run, together (they stay siblings, in order)
    * outdent: each run leaves its parent, right after it, in order
    * up / down: each run moves past the tag before / after it
    * heading_up / heading_down: the headings selected (and the headings inside
      the selected tags) one level up (H3 → H2) or down (H2 → H3), H1 … H6
    A tag inside another selected tag moves with it."""
    ids = [i for i in dict.fromkeys(ids) if i in model.nodes]
    if not ids:
        raise EditError("select some tags")
    chosen = set(ids)
    if action in ("heading_up", "heading_down"):
        d = -1 if action == "heading_up" else 1
        heads = []
        for i in ids:
            for n in [model.nodes[i]] + model.descendants(i):
                if n.role in HEADINGS and n.id not in heads:
                    heads.append(n.id)
        if not heads:
            raise EditError("no heading (H1–H6) among the selected tags")
        changed = 0
        for h in heads:
            n = model.nodes[h]
            lv = int(n.role[1])
            nv = max(1, min(6, lv + d))
            if nv != lv:
                set_role(model, h, f"H{nv}")
                n.source = "user"
                changed += 1
        return {"changed": changed, "headings": len(heads)}
    if action not in ("indent", "outdent", "up", "down"):
        raise EditError(f"unknown level change {action}")
    tops = [i for i in ids if not any(a.id in chosen for a in model.ancestors(i))]
    order = {n.id: k for k, n in enumerate(model.ordered_nodes())}
    tops.sort(key=lambda i: order.get(i, 0))
    # runs: selected tags side by side under one parent
    runs: list[list[str]] = []
    for i in tops:
        if runs:
            last = runs[-1][-1]
            sib = model.siblings(i)
            if model.nodes[last].parent == model.nodes[i].parent and sib.index(i) == sib.index(last) + 1:
                runs[-1].append(i)
                continue
        runs.append([i])
    moved = skipped = 0
    seq = list(reversed(runs)) if action == "down" else runs
    for run in seq:
        sib = model.siblings(run[0])
        a, b = sib.index(run[0]), sib.index(run[-1])
        par = model.nodes[run[0]].parent
        if action == "up":
            if a == 0:
                skipped += len(run)
                continue
            prev = sib.pop(a - 1)
            sib.insert(b, prev)
        elif action == "down":
            if b + 1 >= len(sib):
                skipped += len(run)
                continue
            nxt = sib.pop(b + 1)
            sib.insert(a, nxt)
        elif action == "indent":
            if a == 0:
                skipped += len(run)
                continue
            host = model.nodes[sib[a - 1]]
            for i in run:
                model.detach(i)
                host.children.append(i)
                model.nodes[i].parent = host.id
        else:  # outdent
            if par is None or par not in model.nodes:
                skipped += len(run)
                continue
            gp = model.nodes[par].parent
            grand = model.siblings(par)
            at = grand.index(par) + 1
            for k, i in enumerate(run):
                model.detach(i)
                grand.insert(at + k, i)
                model.nodes[i].parent = gp
        moved += len(run)
    if not moved:
        raise EditError({"up": "already at the top of their parent", "down": "already at the bottom of their parent",
                         "indent": "nothing before them to go into", "outdent": "already at the top level"}[action])
    return {"moved": moved, "skipped": skipped}


def drop(model: DocModel, ids: list[str], target: str, where: str) -> None:
    """Move several tags before/after/into a target, keeping their order."""
    if target not in model.nodes:
        raise EditError("no such target")
    ids = [i for i in ids if i in model.nodes and i != target]
    for i in ids:
        if target in _subtree_ids(model, i):
            raise EditError("cannot move a tag inside itself")
    order = {n.id: k for k, n in enumerate(model.ordered_nodes())}
    ids.sort(key=lambda i: order.get(i, 0))
    last = target
    for i in ids:
        model.detach(i)
        t = model.nodes[target]
        if where == "into":
            t.children.append(i)
            model.nodes[i].parent = t.id
        else:
            dest = model.siblings(target)
            if where == "before":
                pos = dest.index(target)
            else:
                pos = dest.index(last) + 1
            dest.insert(pos, i)
            model.nodes[i].parent = t.parent
            last = i


def merge(model: DocModel, ids: list[str]) -> str:
    ids = [i for i in ids if i in model.nodes]
    if len(ids) < 2:
        raise EditError("select at least two tags to merge")
    first = model.nodes[ids[0]]
    for oid in ids[1:]:
        o = model.nodes[oid]
        if o.text:
            first.text = (first.text + " " + o.text).strip()
        if o.bbox is not None:
            if first.bbox is None:
                first.bbox, first.page = o.bbox, o.page
            elif o.page == first.page:
                first.bbox = union(first.bbox, o.bbox)
            else:
                first.parts.append([o.page, *o.bbox])
        first.parts += o.parts
        first.src_keys += o.src_keys
        first.annots += o.annots
        for cid in list(o.children):
            model.nodes[cid].parent = first.id
            first.children.append(cid)
        o.children = []
        model.remove(oid)
    first.source = "user"
    first.confidence = 1.0
    return first.id


# ------------------------------------------------- joining parts across pages
def _rows_of(model: DocModel, tid: str) -> tuple[list[str], list[str], str]:
    """(header row ids, body row ids, the node the body rows hang from)."""
    t = model.nodes[tid]
    k = int((t.style or {}).get("header_rows", 0) or 0)
    kids = [model.nodes[c] for c in t.children if c in model.nodes]
    head: list[str] = []
    body: list[str] = []
    parent = tid
    if any(c.role in ("THead", "TBody") for c in kids):
        for c in kids:
            rows = [x for x in c.children if x in model.nodes and model.nodes[x].role == "TR"]
            if c.role == "THead":
                head += rows
            elif c.role == "TBody":
                body += rows
                parent = c.id
    else:
        rows = [c.id for c in kids if c.role == "TR"]
        head, body = rows[:k], rows[k:]
    return head, body, parent


def _row_text(model: DocModel, rid: str) -> str:
    return " | ".join((model.nodes[c].text or "") for c in model.nodes[rid].children
                      if c in model.nodes)


def _merge_tables(model: DocModel, first: StructNode, other: StructNode,
                  repeat_header: bool | None) -> dict:
    """Second table's rows join the first; a repeated column header becomes a
    running table heading (artifact), as PDF/UA expects for a continued table."""
    f_head, _, body_parent = _rows_of(model, first.id)
    o_head, o_body, _ = _rows_of(model, other.id)
    f_sig = " ".join(_row_text(model, r) for r in f_head)
    o_sig = " ".join(_row_text(model, r) for r in o_head)
    if repeat_header is None:
        repeat_header = bool(f_sig and o_sig and clean_text(f_sig).lower() == clean_text(o_sig).lower())
    moved = 0
    for rid in o_head:
        row = model.nodes[rid]
        if repeat_header:
            row.role = "Artifact"
            row.artifact = True
            row.artifact_subtype = "Pagination"
            row.pagination_subtype = "Header"
            row.note = f"table header repeated on page {row.page + 1} (running table heading)"
            for cid in list(row.children):
                c = model.nodes.get(cid)
                if c is not None:
                    c.artifact = True
                    c.artifact_subtype = "Pagination"
        else:
            for cid in row.children:            # not a header here: data cells
                c = model.nodes.get(cid)
                if c is not None and c.role == "TH":
                    c.role, c.scope, c.headers = "TD", "", []
        model.detach(rid)
        model.add(row, body_parent)
        moved += 1
    for rid in o_body:
        row = model.nodes[rid]
        model.detach(rid)
        model.add(row, body_parent)
        moved += 1
    if other.bbox is not None:
        first.parts.append([other.page, *other.bbox])
    first.parts += other.parts
    model.detach(other.id)
    model._drop_subtree(other.id)
    return {"rows": moved, "repeat_header": bool(repeat_header)}


def _merge_children(model: DocModel, first: StructNode, other: StructNode) -> dict:
    """Lists, contents lists and sections: the second one's items join the first."""
    moved = 0
    for cid in list(other.children):
        child = model.nodes.get(cid)
        if child is None:
            continue
        model.detach(cid)
        model.add(child, first.id)
        moved += 1
    if other.bbox is not None:
        first.parts.append([other.page, *other.bbox])
    first.parts += other.parts
    model.detach(other.id)
    model._drop_subtree(other.id)
    return {"items": moved}


JOIN_CHILDREN = {"L", "TOC", "Index", "Sect", "Part", "Art", "Div", "THead", "TBody"}


def _header_verdict(model: DocModel, first: StructNode, other: StructNode) -> bool | None:
    """Does part two repeat part one's column header? None = cannot tell."""
    f_head, _, _ = _rows_of(model, first.id)
    o_head, o_body, _ = _rows_of(model, other.id)
    f_sig = clean_text(" ".join(_row_text(model, r) for r in f_head)).lower()
    first_row = (o_head or o_body)[:1]
    o_sig = clean_text(" ".join(_row_text(model, r) for r in first_row)).lower()
    if not f_sig or not o_sig:
        return None if o_head else False
    if f_sig == o_sig:
        return True
    a, b = set(f_sig.split()), set(o_sig.split())
    same = len(a & b) / max(1, len(a | b))
    if same > 0.8:
        return True
    if same < 0.3:
        return False
    return None                      # half the words match: worth asking


def merge_parts(model: DocModel, ids: list[str], ai_cfg=None, ask_ai: str = "auto") -> dict:
    """Join tags that belong together but were split — usually by a page break.

    The parts keep their own pages (a tag can live on several pages through
    `parts`); what changes is that one tag now holds the whole thing. A table
    takes the other's rows and turns a repeated column header into a running
    heading; a list takes the other's items; a paragraph takes the other's text.
    With an AI server set up, the model is asked whether the two really are one
    thing and whether the second part repeats the header — the answer is
    recorded as a note, it never cancels what you asked for."""
    ids = [i for i in ids if i in model.nodes]
    if len(ids) < 2:
        raise EditError("select at least two tags to join")
    order = {n.id: k for k, n in enumerate(model.ordered_nodes())}
    ids = sorted(ids, key=lambda i: order.get(i, 0))
    first = model.nodes[ids[0]]
    roles = {model.nodes[i].role for i in ids}
    if len(roles) > 1:
        raise EditError("the selected tags must all have the same type "
                        f"(you picked {', '.join(sorted(roles))})")
    if any(i in [a.id for a in model.ancestors(j)] for i in ids for j in ids if i != j):
        raise EditError("one of the selected tags is inside another")
    out: dict = {"id": first.id, "role": first.role, "parts": len(ids), "ai": None, "merged": []}
    for oid in ids[1:]:
        other = model.nodes[oid]
        verdict = None
        # The rules settle almost every join on their own, and a model running on
        # a CPU takes the better part of a minute — so it is asked only when it
        # can change the outcome: a table whose repeated header is half a match.
        rule_header = _header_verdict(model, first, other) if first.role == "Table" else None
        need_ai = ask_ai == "always" or (ask_ai == "auto" and first.role == "Table"
                                         and rule_header is None)
        if ai_cfg is not None and need_ai:
            try:
                from .ai import merge_check
                if first.role == "Table":
                    f_head, f_body, _ = _rows_of(model, first.id)
                    o_head, o_body, _ = _rows_of(model, other.id)
                    a_tail = [_row_text(model, r) for r in f_body[-2:]] or                              [_row_text(model, r) for r in f_head]
                    b_head = [_row_text(model, r) for r in (o_head + o_body)[:3]]
                else:
                    a_tail = [(model.nodes[c].text or "") for c in first.children[-2:]] or [first.text]
                    b_head = [(model.nodes[c].text or "") for c in other.children[:3]] or [other.text]
                verdict = merge_check(ai_cfg, first.role, a_tail, b_head, first.page, other.page)
            except Exception as exc:
                out["ai_error"] = str(exc)
        if verdict is not None:
            out["ai"] = verdict
        if first.role == "Table":
            info = _merge_tables(model, first, other,
                                 verdict["repeat_header"] if verdict else rule_header)
        elif first.role in JOIN_CHILDREN:
            info = _merge_children(model, first, other)
        else:
            text = (other.text or "").strip()
            if text:
                first.text = (first.text + " " + text).strip()
            info = _merge_children(model, first, other)
        out["merged"].append(info)
    if first.role == "Table":
        tidy_tables(model)
    first.source = "user"
    first.confidence = 1.0
    pages = sorted({first.page} | {int(p[0]) for p in first.parts})
    if len(pages) > 1:
        first.note = (first.note + "; " if first.note else "") +             "joined across pages " + ", ".join(str(p + 1) for p in pages)
    if out.get("ai") and not out["ai"]["same"]:
        first.note += f" (AI: these look like two different parts — {out['ai']['why']})"
        first.confidence = 0.6
    return out


def _same_parent(model: DocModel, ids: list[str]) -> str | None:
    parents = {model.nodes[i].parent for i in ids}
    if len(parents) != 1:
        raise EditError("the selected tags must share the same parent")
    return parents.pop()


def wrap(model: DocModel, ids: list[str], role: str) -> str:
    ids = [i for i in ids if i in model.nodes]
    if not ids:
        raise EditError("nothing selected")
    parent = _same_parent(model, ids)
    sib = model.siblings(ids[0])
    ids.sort(key=sib.index)
    pos = sib.index(ids[0])
    first = model.nodes[ids[0]]
    box = StructNode(id=model.new_id("w"), role=role, page=first.page, source="user")
    for i in ids:
        n = model.nodes[i]
        if n.page == box.page and n.bbox:
            box.bbox = union(box.bbox, n.bbox)
    for i in ids:
        sib.remove(i)
    model.add(box, parent, pos)
    for i in ids:
        model.nodes[i].parent = box.id
        box.children.append(i)
    return box.id


LABEL_ONLY = re.compile(r"\s*(\(?\d{1,3}[.)]|\(?[A-Za-z][.)]|[•●▪■◦○–—-])\s*")
PUNCT_ONLY = re.compile(r"\s*[.,;:!?…]+\s*")


def _label_split(model: DocModel, n: StructNode):
    """'4. I can' -> ('4.', label box, rest text, rest box) from the page's characters."""
    mt = LABEL_RE.match(n.text or "")
    if not mt or n.bbox is None or not model.path:
        return None
    lbl = mt.group("lbl")
    try:
        doc = pymupdf.open(model.path)
    except Exception:
        return None
    try:
        page = doc[n.page]
        tm = page.transformation_matrix
        r = pymupdf.Rect(n.bbox) * tm
        r.normalize()
        chars = []
        for b in page.get_text("rawdict", flags=RAW_FLAGS).get("blocks", []):
            for l in b.get("lines", []):
                for sp in l.get("spans", []):
                    for ch in sp.get("chars", []):
                        cb = pymupdf.Rect(ch["bbox"])
                        cx, cy = (cb.x0 + cb.x1) / 2, (cb.y0 + cb.y1) / 2
                        if ch["c"].strip() and r.x0 - 1 <= cx <= r.x1 + 1 and r.y0 - 1 <= cy <= r.y1 + 1:
                            chars.append(cb)
        chars.sort(key=lambda c: (round(c.y0 / 3), c.x0))
        need = len(lbl.strip())
        if len(chars) <= need:
            return None
        inv = ~tm

        def pdf_box(cs):
            u = pymupdf.Rect(cs[0])
            for c in cs[1:]:
                u |= c
            u = u * inv
            u.normalize()
            return (u.x0, u.y0, u.x1, u.y1)
        return lbl, pdf_box(chars[:need]), (n.text or "")[mt.end():].strip(), pdf_box(chars[need:])
    finally:
        doc.close()


def create_empty(model: DocModel, role: str, target: str, where: str = "after",
                 text: str = "") -> str:
    """A tag with nothing in it, next to or inside the tag you picked.

    A row that is one cell short, a label a list item never had, a line left for
    an answer — sometimes what is needed is a tag for something the page does
    not print. It is given a small zone at the edge of the tag it is added to
    (drag it where you want it), and, when you say what it should be read as,
    that wording is written into the file as invisible text, the way an empty
    cell is — so the tag really is there for a screen reader."""
    if role not in EDITOR_ROLES and role not in ("TD", "TH", "TR", "Lbl", "LBody", "LI"):
        raise EditError(f"unknown tag type {role}")
    t = model.nodes.get(target)
    if t is None:
        raise EditError("select the tag to add it to")
    box = None
    if t.bbox:
        x0, y0, x1, y1 = t.bbox
        w = min(60.0, max(8.0, x1 - x0))
        h = min(14.0, max(6.0, y1 - y0))
        if where == "inside":
            box = (x0, y0, x0 + w, y0 + h)              # bottom-left of the tag
        elif where == "before":
            box = (x0, y1, x0 + w, y1 + h)              # just above it
        else:
            box = (x0, y0 - h, x0 + w, y0)              # just below it
    node = StructNode(id=model.new_id("new"), role=role, page=t.page, bbox=box,
                      source="user", priority=1, note="empty tag added by hand")
    if text.strip():
        node.actual_text = text.strip()
        node.style = {"blank_line": True}
    elif role in ("TD", "TH"):
        pass                      # an empty cell is announced on its own
    else:
        node.style = {"blank_line": True}
    if where == "inside":
        model.add(node, t.id, 0)
    else:
        sib = model.siblings(target)
        model.add(node, t.parent, sib.index(target) + (0 if where == "before" else 1))
    return node.id


def make_list(model: DocModel, ids: list[str]) -> str:
    """Selected tags -> L > LI > Lbl + LBody.

    A tag that is only a number/bullet ("1.") becomes the Lbl of a new item and
    the tags after it (up to the next number) become its LBody. An empty tag
    there (a drawn answer line) becomes a write-in line, read as "Blank Line".
    A tag that starts with a number ("4. I can") is split into Lbl + LBody."""
    ids = [i for i in ids if i in model.nodes]
    if not ids:
        raise EditError("select the tags for the list")
    parent = _same_parent(model, ids)
    sib = model.siblings(ids[0])
    ids.sort(key=sib.index)
    pos = sib.index(ids[0])
    nodes = [model.nodes[i] for i in ids]

    items: list[list] = []            # [label node | None, [body nodes], labelled?]
    for n in nodes:
        t = (n.text or "").strip()
        if not n.children and t and LABEL_ONLY.fullmatch(t):
            items.append([n, [], True])
        elif LABEL_RE.match(t):
            items.append([None, [n], True])          # "4. I can" — split below
        elif items and items[-1][2]:
            items[-1][1].append(n)
        else:
            items.append([None, [n], False])

    first_lbl = next((it[0].text for it in items if it[0] is not None), None)
    if first_lbl is None:
        m0 = LABEL_RE.match(nodes[0].text or "")
        first_lbl = m0.group("lbl") if m0 else None
    numbering = Analyzer._numbering(first_lbl.strip()) if first_lbl else "None"
    L = StructNode(id=model.new_id("list"), role="L", page=nodes[0].page,
                   list_numbering=numbering, source="user")
    for i in ids:
        sib.remove(i)
    model.add(L, parent, pos)

    def is_blank(n):
        t = (n.text or "").strip()
        return not n.children and (not t or bool(re.fullmatch(r"_+", t)))

    def mark_blank(n):
        n.style = dict(n.style or {}, blank_line=True)

    for lab, body, _ in items:
        page = (lab or body[0]).page
        li = StructNode(id=model.new_id("li"), role="LI", page=page, source="user")
        model.add(li, L.id)
        for x in ([lab] if lab else []) + body:
            li.bbox = union(li.bbox, x.bbox)
            if x.page == L.page:
                L.bbox = union(L.bbox, x.bbox)
            x.artifact, x.source = False, "user"
        head = None
        rest = list(body)
        if lab is not None:
            lab.role = "Lbl"
            model.add(lab, li.id)
        else:
            sp = _label_split(model, body[0])
            if sp:                                     # "4. I can" -> Lbl + LBody
                lbl, lbox, rtext, rbox = sp
                model.add(StructNode(id=model.new_id("lbl"), role="Lbl", page=page, bbox=lbox,
                                     text=lbl, source="user"), li.id)
                head, rest = body[0], body[1:]
                head.text, head.bbox, head.role = rtext, rbox, "LBody"
                model.add(head, li.id)
                li.bbox = union(li.bbox, lbox)
            elif len(body) == 1:                       # plain item
                body[0].role = "LBody"
                if is_blank(body[0]):
                    mark_blank(body[0])
                model.add(body[0], li.id)
                continue
        # all answer lines of the item = ONE blank (read "Blank Line" once)
        blanks = [b for b in rest if is_blank(b)]
        blank = blanks[0] if blanks else None
        for extra in blanks[1:]:
            model.absorb(blank.id, extra.id)
        others = [b for b in rest if b not in blanks]
        if blank is not None:
            mark_blank(blank)
        if head is None:
            if blank is not None and not others:       # "1." + line(s)
                blank.role = "LBody"
                model.add(blank, li.id)
                continue
            head = StructNode(id=model.new_id("lb"), role="LBody", page=page, source="user")
            model.add(head, li.id)
        placed_blank = False
        for b in rest:
            if b in blanks:
                if not placed_blank and blank is not None:
                    blank.role = "Span"
                    model.add(blank, head.id)
                    placed_blank = True
            elif PUNCT_ONLY.fullmatch(b.text or ""):
                model.add(b, head.id)                  # "." is LBody text, not a tag
                model.absorb(head.id, b.id)
                head.text = ((head.text or "") + " " + (b.text or "").strip()).strip()
            else:
                model.add(b, head.id)
    # a line read off the page can carry characters that are not really printed
    # (a symbol font's private-use bullet, a soft hyphen): they would be read out
    from .repair import TEXT_JUNK, _clean, join_split_lists, nest_sub_items
    for d in [model.nodes[L.id]] + model.descendants(L.id):
        if TEXT_JUNK.search(d.text or ""):
            d.text = _clean(d.text)
    # the same rules as the automatic pass: points set further in with another
    # mark become a list inside their item, and a list this one carries on (or
    # that carries on from it) past a note is joined to it
    try:
        nest_sub_items(model, {L.id})
        join_split_lists(model, {L.id})
    except Exception:
        pass
    return L.id if L.id in model.nodes else next(
        (n.id for n in model.nodes.values() if n.role == "L"), L.id)


def table_add_rows(model: DocModel, ids: list[str]) -> str:
    """Selected Table + tags under it -> each tag becomes a table row with one
    cell spanning every column ("I can sit." / "We can sit." under a grid)."""
    ids = [i for i in ids if i in model.nodes]
    tables = [i for i in ids if model.nodes[i].role == "Table"]
    if len(tables) != 1:
        raise EditError("select exactly one Table and the tags to add as its rows")
    tid = tables[0]
    table = model.nodes[tid]
    others = [i for i in ids if i != tid and tid not in {a.id for a in model.ancestors(i)}]
    if not others:
        raise EditError("select the tags (e.g. the P tags under the table) to add as rows")
    order = {n.id: k for k, n in enumerate(model.ordered_nodes())}
    others.sort(key=lambda i: order.get(i, 0))
    from .lint import table_grid
    occ, widths = table_grid(model, tid)
    ncols = max(widths or [1])
    rows = [d for d in model.descendants(tid) if d.role == "TR"]
    row_parent = rows[-1].parent if rows else tid
    uses_ids = any(d.role == "TD" and d.headers for d in model.descendants(tid))
    head_ids = []
    if uses_ids and rows:
        hr = max(1, int((table.style or {}).get("header_rows", 1) or 1))
        for r in rows[:hr]:
            head_ids += [c for c in r.children if model.nodes[c].role == "TH"]
    tx0 = table.bbox[0] if table.bbox else None
    tx1 = table.bbox[2] if table.bbox else None
    for i in others:
        n = model.nodes[i]
        model.detach(i)
        tr = StructNode(id=model.new_id("tr"), role="TR", page=n.page, source="user")
        model.add(tr, row_parent)
        n.role, n.artifact, n.source = "TD", False, "user"
        n.colspan, n.rowspan, n.scope = ncols, 1, ""
        n.headers = list(head_ids)
        if n.bbox is not None and tx0 is not None and n.page == table.page:
            n.bbox = (min(tx0, n.bbox[0]), n.bbox[1], max(tx1, n.bbox[2]), n.bbox[3])
        model.add(n, tr.id)
        tr.bbox = n.bbox
        if n.page == table.page:
            table.bbox = union(table.bbox, n.bbox)
    return tid


PAGE_NO_RE = re.compile(r"^\s*(?:page\s*)?(?P<n>\d{1,5}|[ivxlcdmIVXLCDM]{1,7})\s*$", re.I)


def convert_table_columns(model: DocModel, tid: str, mode: str = "cells") -> dict:
    """A heading and the words under it, one group after another.

    A sound-spelling card sets its sample words in columns under "Initial
    Position", "Medial Position", "Final Position". Read across, that is
    "ace acorn make break away play" — six unrelated words. What the card
    teaches is each heading and then its own words, read down each column:

        TR  > TH  Initial Position
        TR  > ace, age, ape, ate, acorn, aid, aim, aimless
        TR  > TH  Medial Position
        TR  > make, face, paid, wait, break, great, steak
        ...

    Every heading keeps the tags printed under it, in column order: down the
    first column of the group, then down the next. `mode="cells"` makes each
    word a `TD` (the rows stay a regular table, so PAC is happy); `mode="paragraphs"`
    leaves the tags as they are and moves them into the row."""
    t = model.nodes.get(tid)
    if t is None or t.role != "Table":
        raise EditError("select a table first")
    kids = [n for n in model.descendants(tid)]
    heads = [n for n in kids if n.role == "TH" and not n.artifact and n.bbox is not None
             and (n.text or "").strip()]
    if not heads:
        raise EditError("this table has no header cells — mark the headings TH first")
    content = [n for n in kids if n.role in ("TD", "P", "Span", "Caption")
               and n.bbox is not None and (n.text or "").strip()
               and n.id not in {h.id for h in heads}]
    if not content:
        raise EditError("nothing under the headings to group")
    heads.sort(key=lambda n: n.bbox[0])

    # where one heading's group ends and the next begins
    bounds: list[tuple[float, float]] = []
    for i, h in enumerate(heads):
        left = -1e9 if i == 0 else (heads[i - 1].bbox[2] + h.bbox[0]) / 2
        right = 1e9 if i == len(heads) - 1 else (h.bbox[2] + heads[i + 1].bbox[0]) / 2
        bounds.append((left, right))

    groups: list[list[StructNode]] = [[] for _ in heads]
    left_over: list[StructNode] = []
    for n in content:
        cx = (n.bbox[0] + n.bbox[2]) / 2
        hit = next((i for i, (a, b) in enumerate(bounds) if a <= cx < b), None)
        if hit is None:
            left_over.append(n)
            continue
        groups[hit].append(n)

    def in_columns(nodes: list[StructNode]) -> list[StructNode]:
        """Down the first column of the group, then down the next."""
        if not nodes:
            return []
        wide = statistics.median([n.bbox[2] - n.bbox[0] for n in nodes]) or 10.0
        cols: list[list[StructNode]] = []
        for n in sorted(nodes, key=lambda x: x.bbox[0]):
            if cols and n.bbox[0] - cols[-1][0].bbox[0] <= max(6.0, wide * 0.5):
                cols[-1].append(n)
            else:
                cols.append([n])
        out: list[StructNode] = []
        for c in cols:
            out += sorted(c, key=lambda x: -x.bbox[3])      # top of the page first
        return out

    rows = [(h, in_columns(g)) for h, g in zip(heads, groups)]
    width = max((len(g) for _h, g in rows), default=0)
    if not width:
        raise EditError("nothing under the headings to group")

    # rebuild the table: heading row, words row, heading row, words row …
    old_children = list(t.children)
    for cid in old_children:
        model.detach(cid)
    t.style = dict(t.style or {}, header_rows=0, header_cols=0)
    body = StructNode(id=model.new_id("tbody"), role="TBody", page=t.page, source="user")
    model.add(body, t.id)
    made = 0
    for h, words in rows:
        hr = StructNode(id=model.new_id("tr"), role="TR", page=h.page, bbox=h.bbox, source="user")
        model.add(hr, body.id)
        model.detach(h.id)
        model.add(h, hr.id)
        if mode == "cells":
            h.colspan = max(1, width)
        h.scope = h.scope or "Column"
        wr = StructNode(id=model.new_id("tr"), role="TR", page=h.page, source="user")
        model.add(wr, body.id)
        for n in words:
            model.detach(n.id)
            if mode == "cells" and n.role != "TD":
                n.role = "TD"
                n.scope, n.headers = "", []
            model.add(n, wr.id)
            wr.bbox = union(wr.bbox, n.bbox)
            made += 1
        if mode == "cells" and words:
            # the shorter groups are filled up so every row has the same number
            # of columns (an irregular table is a PAC failure); each filler is a
            # real box at the end of the row, read once as "Blank cell"
            last = words[-1].bbox
            w = max(8.0, last[2] - last[0])
            right = (t.bbox or last)[2]
            for k in range(width - len(words)):
                x = min(last[2] + k * w, max(right - w, last[0]))
                model.add(StructNode(id=model.new_id("td"), role="TD", page=h.page,
                                     bbox=(x, last[1], min(x + w, right), last[3]),
                                     source="user", note="filler so the rows match"), wr.id)
    for n in left_over:                                  # printed beside the headings
        model.detach(n.id)
        model.add(n, body.id)
    for cid in old_children:          # the old rows and row groups, now empty
        n = model.nodes.get(cid)
        if n is None:
            continue
        keeps = [d for d in model.descendants(cid)
                 if (d.text or "").strip() or d.role in ("Figure", "Formula")]
        if not keeps and not (n.text or "").strip():
            model._drop_subtree(cid)
            model.nodes.pop(cid, None)
        else:
            model.add(n, t.id)
    t.note = (t.note + "; " if t.note else "") + \
        f"grouped: {len(rows)} heading(s), each with the words printed under it"
    return {"id": t.id, "groups": len(rows), "cells": made, "width": width,
            "left_over": len(left_over)}


def set_actual_text(model: DocModel, ids: list[str], text: str = "",
                    mode: str = "same") -> dict:
    """Write /ActualText on the selected tags — one of them or a hundred.

    mode "same"  the same sentence on every selected tag
    mode "each"  a sentence of its own per tag (ids and texts in step)
    mode "clear" the tag is read as it is printed again
    """
    changed, skipped = 0, []
    texts = text if isinstance(text, list) else [text] * len(ids)
    for nid, value in zip(ids, texts):
        n = model.nodes.get(nid)
        if n is None:
            skipped.append(nid)
            continue
        if n.role in CONTAINER_ROLES and n.children:
            skipped.append(nid)          # a container is read through its children
            continue
        v = "" if mode == "clear" else clean_text(str(value or ""))[:2000]
        if (n.actual_text or "") == v:
            continue
        n.actual_text = v
        n.source = "user"
        if v:
            n.note = (n.note or "").split("; read as ")[0] + "; read as its /ActualText"
        changed += 1
    return {"changed": changed, "skipped": skipped}


# ------------------------------------------- /ActualText on a Span inside the tag
def _span_needed(model: DocModel, n: StructNode) -> bool:
    """Is this a tag whose /ActualText should sit on a Span inside it?

    Figure and Formula keep it (it is their description there), a Span already
    is one, and a tag with nothing in it (an empty cell announced as "Blank")
    has no content to wrap."""
    if n.artifact or not (n.actual_text or "").strip():
        return False
    from .structure import NO_SPAN
    if n.role in NO_SPAN:
        return False
    kids = [model.nodes[c] for c in n.children if c in model.nodes]
    if any(k.role in ("Lbl", "LBody", "TR", "TD", "TH", "Caption") for k in kids):
        return False                      # those must stay direct children
    has_content = bool((n.text or "").strip() or n.bbox is not None or n.parts or n.annots or kids)
    return has_content


def actualtext_on_tag(model: DocModel) -> list[str]:
    """The tags that carry /ActualText themselves instead of on a Span."""
    return [n.id for n in model.ordered_nodes() if _span_needed(model, n)]


def actualtext_to_span(model: DocModel, ids: list[str] | None = None) -> int:
    """Put every /ActualText on a Span inside its tag: tag > Span, the Span
    holding the tag's words (and the tags and links printed inside it) and the
    /ActualText, the tag itself carrying none — what the clients' own files
    look like. -> how many tags were changed."""
    want = [i for i in (ids or actualtext_on_tag(model)) if i in model.nodes]
    made = 0
    for nid in want:
        n = model.nodes[nid]
        if not _span_needed(model, n):
            continue
        span = StructNode(id=model.new_id("atspan"), role="Span", page=n.page,
                          bbox=tuple(n.bbox) if n.bbox is not None else None,
                          text=n.text, actual_text=n.actual_text,
                          source="user", priority=max(1, n.priority), confidence=1.0,
                          note="the tag's /ActualText is read from this Span")
        span.parts = [list(p) for p in n.parts]
        span.lang = n.lang
        span.expansion = n.expansion
        span.annots = list(n.annots)
        span.style = dict(n.style or {})
        kids = list(n.children)
        model.nodes[span.id] = span
        span.parent = n.id
        for c in kids:
            model.nodes[c].parent = span.id
        span.children = kids
        n.children = [span.id]
        n.actual_text = ""
        n.expansion = ""
        n.annots = []
        n.note = (n.note + "; " if n.note else "") + "read through the Span inside it"
        made += 1
    return made


def suggest_actual_text(model: DocModel, working_pdf: str, ids: list[str]) -> list[dict]:
    """What each selected tag should be read as, from the glyphs printed in it.

    The letters and the colour each one is printed in are on the page, so the
    sentence is built from the page: "c, a and t, a highlighted in red"."""
    from .actualtext import page_text_colour, suggest
    doc = pymupdf.open(working_pdf)
    out: list[dict] = []
    try:
        normal: dict[int, tuple] = {}
        for nid in ids:
            n = model.nodes.get(nid)
            if n is None or n.page < 0 or n.page >= doc.page_count:
                continue
            page = doc[n.page]
            if n.page not in normal:
                normal[n.page] = page_text_colour(page)
            box = None
            if n.bbox is not None:
                r = pymupdf.Rect(n.bbox) * page.transformation_matrix
                r.normalize()
                box = (r.x0, r.y0, r.x1, r.y1)
            s = suggest(page, box, n.text or "", normal[n.page])
            out.append({"id": nid, "role": n.role, "page": n.page,
                        "text": (n.text or "").strip(), "current": n.actual_text or "",
                        "suggestion": s["text"], "colour": s["colour"],
                        "highlighted": len(s["highlighted"])})
        return out
    finally:
        doc.close()


def mark_page_numbers(model: DocModel, ids: list[str], text: str = "Page") -> dict:
    """"These are all page numbers": each one is read as "Page 5".

    A folio is normally an artifact, but some publishers want it announced. The
    tag becomes a `P` with /ActualText "Page 5", so a screen reader says the
    page number instead of reading a bare digit out of context."""
    done, skipped = 0, []
    for nid in ids:
        n = model.nodes.get(nid)
        if n is None:
            continue
        raw = clean_text(n.text or "")
        m = PAGE_NO_RE.match(raw)
        if not m:
            skipped.append(raw[:30] or n.id)
            continue
        n.artifact = False
        n.pagination_subtype = ""
        n.role = "P"
        n.actual_text = f"{text} {m.group('n')}".strip()
        n.note = "page number, read as its /ActualText"
        n.style = dict(n.style or {}, page_number=True)
        n.source = "user"
        n.confidence = 1.0
        done += 1
    return {"changed": done, "skipped": skipped}


def bulk(model: DocModel, ids: list[str], action: str, role: str | None = None,
         ai_cfg=None) -> str | None:
    ids = [i for i in ids if i in model.nodes]
    if action == "role":
        for i in ids:
            set_role(model, i, role or "P")
    elif action == "artifact":
        for i in ids:
            set_role(model, i, "Artifact")
    elif action == "delete":
        for i in ids:
            if i in model.nodes:
                model.remove(i, promote_children=True)
    elif action == "wrap":
        return wrap(model, ids, role or "Sect")
    elif action == "make_list":
        return make_list(model, ids)
    elif action == "table_rows":
        out = table_add_rows(model, ids)
        tidy_tables(model)
        return out
    elif action == "merge":
        sib_sorted = sorted(ids, key=lambda i: model.ordered_nodes().index(model.nodes[i]))
        return merge(model, sib_sorted)
    elif action == "merge_parts":
        return merge_parts(model, ids, ai_cfg, "auto" if ai_cfg is not None else "never")["id"]
    elif action == "page_numbers":
        mark_page_numbers(model, ids, role or "Page")
        return None
    else:
        raise EditError(f"unknown bulk action {action}")
    return None


# ------------------------------------------------------------- geometry
def display_matrix(page: pymupdf.Page) -> pymupdf.Matrix:
    """PDF user space -> rendered (rotated) page image space, in points."""
    return page.transformation_matrix * page.rotation_matrix


def pdf_to_display(page: pymupdf.Page, box) -> list[float]:
    r = pymupdf.Rect(box) * display_matrix(page)
    r.normalize()
    return [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)]


def display_to_pdf(page: pymupdf.Page, box) -> tuple[float, float, float, float]:
    try:
        box = [float(v) for v in list(box)[:4]]
    except Exception:
        raise EditError("invalid rectangle")
    if len(box) != 4:
        raise EditError("invalid rectangle")
    r = pymupdf.Rect(box) * ~display_matrix(page)
    r.normalize()
    return (round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2))


def _insert_position(model: DocModel, page: int, top: float) -> int:
    roots = model.root_children
    last = None
    for k, rid in enumerate(roots):
        r = model.nodes[rid]
        if r.artifact:
            continue
        if r.page == page:
            last = k
            if r.bbox and r.bbox[3] < top - 1:
                return k
        elif r.page > page:
            return k
    return (last + 1) if last is not None else len(roots)


# containers a hand-drawn tag belongs INSIDE, rather than on top of
HOLDERS = ("TD", "TH", "LBody", "LI", "Sect", "Div", "Art", "BlockQuote", "Note", "TOCI")


def _holder_for(model: DocModel, page_no: int, box) -> StructNode | None:
    """The smallest tag the drawn area sits inside — a table cell, a list item's
    body, a section. A box drawn over the text in a cell is meant to tag what is
    printed there, not to replace the cell: the cell keeps its place in the table
    and the new tag is read inside it, where it is printed."""
    a = max(area(box), 1e-6)
    best = None
    for n in model.nodes.values():
        if n.role not in HOLDERS or n.artifact or n.page != page_no or not n.bbox:
            continue
        if intersection(box, n.bbox) / a < 0.9:
            continue
        if best is None or area(n.bbox) < area(best.bbox):
            best = n
    return best


# tags read inside a line of text: a zone drawn inside a paragraph, a heading,
# a cell … for one of these becomes a tag inside it (V4.21.4)
INLINE_ROLES = ("Span", "Link", "Quote", "Code", "Reference", "BibEntry", "Lbl", "Note",
                "Formula", "Figure", "Annot", "Form")
TEXT_HOLDERS = tuple(HEADINGS) + ("H", "P", "Caption", "LBody", "Lbl", "TD", "TH", "Note", "Span", "Link",
                                  "Quote", "Code", "Reference", "BibEntry", "TOCI", "BlockQuote")


def _inline_holder(model: DocModel, page_no: int, box, role: str) -> StructNode | None:
    """The smallest text tag (P, H1 … , a cell, a list body, a Span …) the drawn
    zone lies inside, when the zone is for a tag that is read inside a line:
    "P > Span", "P > Quote", "LBody > Reference" … None otherwise. The tag must
    be bigger than the zone — a zone over the whole paragraph replaces it."""
    if role not in INLINE_ROLES:
        return None
    a = max(area(box), 1e-6)
    best, best_a = None, 0.0
    for n in model.nodes.values():
        if n.role not in TEXT_HOLDERS or n.artifact:
            continue
        rs = n.rects_on(page_no)
        if not rs:
            continue
        cover = sum(intersection(box, r) for r in rs) / a
        na = sum(area(r) for r in rs)
        if cover < 0.85 or na < a * 1.15:
            continue
        if best is None or na < best_a:
            best, best_a = n, na
    return best


def _inline_position(model: DocModel, parent: StructNode, box, page_no: int) -> int:
    """Where a tag goes among the tags inside a line of text: in reading order
    — line by line (PDF space: higher up is read first), then left to right."""
    h = max(1.0, box[3] - box[1])
    cy = (box[1] + box[3]) / 2

    def before(r) -> bool:
        rcy = (r[1] + r[3]) / 2
        same_line = abs(rcy - cy) < 0.5 * max(h, r[3] - r[1])
        if same_line:
            return box[0] < r[0]
        return cy > rcy

    for k, cid in enumerate(parent.children):
        c = model.nodes.get(cid)
        if c is None:
            continue
        rs = c.rects_on(page_no)
        if not rs:
            if c.page is not None and c.page > page_no:
                return k
            continue
        if before(rs[0]):
            return k
    return len(parent.children)


def _child_position(model: DocModel, parent: StructNode, box, page_no: int | None = None) -> int:
    """Where the new tag goes among this tag's children — in reading order,
    page first (a section runs over pages), then top to bottom."""
    for k, cid in enumerate(parent.children):
        c = model.nodes.get(cid)
        if c is None or c.artifact or not c.bbox:
            continue
        if page_no is not None and c.page is not None and c.page >= 0:
            if c.page > page_no:
                return k
            if c.page < page_no:
                continue
        if c.bbox[3] < box[3] - 1:
            return k
    return len(parent.children)


def create_from_rect(model: DocModel, working_pdf: str, page_no: int, rect_display,
                     role: str, absorb: bool = True, opts: Options | None = None,
                     deep: bool = False, ai_cfg=None, split: bool | str = True,
                     ai_answers: dict | None = None, inside: bool = True) -> str:
    """`inside`: a Span, Quote, Lbl, Reference … drawn inside a paragraph (or a
    heading, a cell …) becomes a tag inside it, in reading order (V4.21.4).
    `ai_answers` is what the vision model already said about this zone
    (zone_ai_answers): asked before the job was locked, so the editor answers
    everything else while the model reads the picture (V4.16)."""
    chosen = ""
    if role == "Auto":
        # key I in the editor: the tool (and the vision model, when it is on)
        # decides what the zone is
        d0 = pymupdf.open(working_pdf)
        try:
            b0 = display_to_pdf(d0[page_no], rect_display)
        finally:
            d0.close()
        role, chosen = guess_zone_role(model, working_pdf, page_no, b0, opts or Options(), ai_cfg,
                                       ai_answer=(ai_answers or {}).get("role"))
    if role not in EDITOR_ROLES:
        raise EditError(f"unknown tag type {role}")
    if ai_cfg is not None and role in ("Table", "L"):
        deep = True               # with the AI on, a drawn table or list is always read deeply
    doc = pymupdf.open(working_pdf)
    page = doc[page_no]
    box = display_to_pdf(page, rect_display)
    if area(box) < 4:
        raise EditError("draw a larger box")
    # text inside (unrotated MuPDF space)
    un = pymupdf.Rect(box) * page.transformation_matrix
    un.normalize()
    text = re.sub(r"\s+", " ", printed_text(page, un)).strip()
    from .repair import _clean
    text = _clean(text)
    doc.close()

    # a box drawn inside a cell, a list item or a section belongs in it
    holder = _holder_for(model, page_no, box)
    inline = _inline_holder(model, page_no, box, role) if inside else None
    if inline is not None:
        holder = inline
    hold: set = set()
    if holder is not None:
        hold = {holder.id} | {a.id for a in model.ancestors(holder.id)}

    # absorb tags that sit inside the new box
    absorbed_tops: list[str] = []
    rescue: list[str] = []
    if absorb:
        cands = []
        for n in model.nodes.values():
            rs = n.rects_on(page_no)
            if not rs:
                continue
            a = sum(area(r) for r in rs)
            inside = sum(intersection(r, box) for r in rs)
            if a > 0 and inside / a >= 0.7:
                cands.append(n.id)
        if role == "L":
            # a list drawn again over its labels (and first texts) takes the
            # old list over, even where the old one also covered a second
            # column outside the zone (V4.21.4)
            have = set(cands)
            for n in list(model.nodes.values()):
                if n.role != "L" or n.id in have or n.id in hold:
                    continue
                lbls = [d for d in model.descendants(n.id) if d.role == "Lbl" and d.page == page_no and d.bbox]
                if len(lbls) < 2:
                    continue
                inside_ = sum(1 for d in lbls if box[0] <= (d.bbox[0] + d.bbox[2]) / 2 <= box[2]
                              and box[1] <= (d.bbox[1] + d.bbox[3]) / 2 <= box[3])
                if inside_ / len(lbls) >= 0.7 and not any(model.nodes[x].page != page_no for x in [n.id]) \
                        and not n.parts:
                    cands.append(n.id)
                    cands += [d.id for d in model.descendants(n.id) if d.id not in have]
        cands = [c for c in dict.fromkeys(cands) if c not in hold]
        cset = set(cands)
        for cid in cands:
            n = model.nodes[cid]
            if n.parent not in cset:
                absorbed_tops.append(cid)
        for tid in absorbed_tops:
            for d in [model.nodes[tid]] + model.descendants(tid):
                if d.role in ("Link", "Annot", "Form") and d.annots:
                    rescue.append(d.id)

    pos_parent = None
    pos_index = None
    if absorbed_tops:
        first = min(absorbed_tops, key=lambda i: model.ordered_nodes().index(model.nodes[i]))
        pos_parent = model.nodes[first].parent
        pos_index = model.siblings(first).index(first)

    rescued_nodes = []
    rescue = list(dict.fromkeys(rescue))
    for rid in rescue:
        n = model.nodes[rid]
        par = model.nodes.get(n.parent) if n.parent else None
        sib = model.root_children if n.parent is None else (par.children if par is not None else [])
        if rid in sib:
            sib.remove(rid)
        n.children = [c for c in n.children if c in model.nodes]
        rescued_nodes.append(n)
    rescued_ids = set(rescue)
    # text of a taken-over tag printed outside the zone (a list item's second
    # column when only the first was drawn) stays tagged, as a paragraph after
    # the new tag — not dropped with the old tag (V4.21.4)
    outside: list[StructNode] = []
    if role in ("Table", "L"):
        for tid in absorbed_tops:
            if tid not in model.nodes or tid in rescued_ids:
                continue
            for d in model.descendants(tid):
                if d.children or d.artifact or d.id in rescued_ids or not (d.text or "").strip() \
                        or d.role in ("Lbl", "Link", "Annot", "Form", "Figure", "Formula"):
                    continue
                rs = d.rects_on(page_no)
                a_ = sum(area(r) for r in rs)
                if a_ > 0 and sum(intersection(r, box) for r in rs) / a_ < 0.3:
                    outside.append(d)
    for tid in absorbed_tops:
        if tid in model.nodes and tid not in rescued_ids:   # a link kept is not removed
            model.remove(tid, promote_children=False)
    for n in rescued_nodes:
        model.nodes[n.id] = n
    if outside and pos_index is not None:
        sib0 = model.root_children if pos_parent is None else model.nodes[pos_parent].children
        at0 = min(pos_index, len(sib0))
        for k, d in enumerate(outside):
            d.children = []
            d.parent = None
            if d.role not in ("P", "Span", "Note", "Caption") and d.role not in HEADINGS:
                d.role = "P"
            model.add(d, pos_parent, at0 + k)

    made: list[StructNode] = []
    if role in ("Table", "L"):
        new_id = _structured_from_rect(model, working_pdf, page_no, box, role, opts or Options(),
                                       deep=deep, ai_cfg=ai_cfg,
                                       ai_answers=ai_answers)
        node = model.nodes[new_id]
        model.detach(new_id)
        made = [node]
        extras = (model.info.get("_zone_extras") or {}).pop(new_id, None)
        if not model.info.get("_zone_extras"):
            model.info.pop("_zone_extras", None)
        if extras:
            # the caption over the table and the notes under it, drawn with it
            before = [model.nodes[i] for i in extras.get("before", []) if i in model.nodes]
            after = [model.nodes[i] for i in extras.get("after", []) if i in model.nodes]
            for x in before + after:
                model.detach(x.id)
            made = before + [node] + after
    elif role == "P" and split:
        # the marked area is where the text is, not what it is: read the lines
        # inside it and give each paragraph (or each line) its own tag and zone
        made = _paragraphs_from_rect(model, working_pdf, page_no, box, opts or Options(),
                                     "lines" if str(split) == "lines" else "paragraphs")
        node = made[0] if made else None
    if not made:
        node = StructNode(id=model.new_id("u"), role=role if role != "Artifact" else "P",
                          page=page_no, bbox=box, text=text, source="user", priority=1,
                          artifact=(role == "Artifact"),
                          note="drawn by hand")
        model.nodes[node.id] = node
        made = [node]
    if pos_parent is not None and pos_parent in model.nodes and \
            model.nodes[pos_parent].role not in HOLDERS and model.nodes[pos_parent].role not in FLOW_ROLES:
        # the replaced tags sat inside a paragraph or a span: the new block is
        # read where that paragraph is, not inside it
        up = model.nodes[pos_parent]
        while up.parent is not None and model.nodes[up.parent].role not in HOLDERS \
                and model.nodes[up.parent].role not in FLOW_ROLES:
            up = model.nodes[up.parent]
        pos_parent = up.parent
        sibs_ = model.root_children if pos_parent is None else model.nodes[pos_parent].children
        pos_index = sibs_.index(up.id) + 1 if up.id in sibs_ else None
    if holder is not None and holder.id in model.nodes and holder.role not in FLOW_GROUPS and not (
            pos_parent is not None and (pos_parent == holder.id or holder.id in
                                        {a.id for a in model.ancestors(pos_parent)})):
        pos_parent = holder.id
        pos_index = (_inline_position(model, holder, box, page_no) if inline is not None
                     else _child_position(model, holder, box, page_no))
    elif pos_index is None:
        # nothing was replaced: the block goes to its place on its page, inside
        # the section that holds that page (never to the end of the document)
        pos_parent, pos_index = flow_position(model, page_no, box)
    siblings = model.root_children if pos_parent is None else model.nodes[pos_parent].children
    at = min(pos_index, len(siblings))
    for k, nd in enumerate(made):
        siblings.insert(at + k, nd.id)
        nd.parent = pos_parent
    last = made[-1]
    hosts = [d for x in made for d in [x] + model.descendants(x.id)
             if not d.artifact and d.role in ("TD", "TH", "LBody", "P", "Caption", "Note", "H1", "H2", "H3",
                                              "H4", "H5", "H6", "Formula")]
    for n in rescued_nodes:
        # a link printed inside a cell or an item of what was drawn goes in it
        home = None
        from .compat import house
        if house() and (n.bbox is not None and node.role in ("Table", "L") or (n.bbox is not None and len(made) > 1)):
            cx, cy = (n.bbox[0] + n.bbox[2]) / 2, (n.bbox[1] + n.bbox[3]) / 2
            inside = [d for d in hosts if any(r[0] - 0.5 <= cx <= r[2] + 0.5 and r[1] - 0.5 <= cy <= r[3] + 0.5
                                               for r in d.rects_on(n.page))]
            if inside:
                home = min(inside, key=lambda d: sum(area(r) for r in d.rects_on(n.page)))
        if home is not None:
            model.add(n, home.id)
        elif node.role in ("Figure", "Table", "L") or node.artifact or len(made) > 1:
            model.add(n, pos_parent, siblings.index(last.id) + 1)
        else:
            model.add(n, node.id)
    if node.role == "Formula" and not node.alt and (node.text or "").strip():
        from . import mathspeak
        node.alt = mathspeak.speak(node.text)
    if node.role in ("Figure", "Formula") and not node.artifact and \
            (ai_cfg is not None or (ai_answers or {}).get("describe") is not None):
        _ai_describe(model, working_pdf, page_no, node, ai_cfg,
                     ans_ready=(ai_answers or {}).get("describe"))
    if chosen:
        node.note = (node.note + "; " if node.note else "") + chosen
    return node.id


def _zone_context(model: DocModel, page_no: int, box, limit: int = 600) -> str:
    """The text printed around a zone: its caption, the paragraph that refers to it."""
    near = []
    for n in model.ordered_nodes():
        if n.page != page_no or n.artifact or not (n.text or "").strip() or n.bbox is None:
            continue
        dy = max(n.bbox[1] - box[3], box[1] - n.bbox[3], 0)
        if dy <= 80:
            near.append((0 if n.role == "Caption" else 1, dy, n.text))
    near.sort()
    return " ".join(t for _, _, t in near)[:limit]


def _ai_describe(model: DocModel, working_pdf: str, page_no: int, node: StructNode, ai_cfg,
                 ans_ready: dict | None = None) -> None:
    """A drawn Figure gets its alt text, a drawn Formula its words, from the
    vision model. `ans_ready` is an answer asked for earlier (zone_ai_answers),
    so nothing waits for Ollama while the job is locked."""
    from .ai import alt_text, math_from_image
    if ans_ready is None:
        doc = pymupdf.open(working_pdf)
        try:
            page = doc[page_no]
            png = _zone_png(page, node.bbox, 2.0, max_px=_ai_px(ai_cfg))
            ctx = _zone_context(model, page_no, node.bbox)
        finally:
            doc.close()
    try:
        if node.role == "Figure":
            text = (ans_ready.get("alt", "") if ans_ready is not None
                    else alt_text(ai_cfg, png, ctx) or "").strip()
            if text.upper().startswith("DECORATIVE"):
                node.note = (node.note + "; " if node.note else "") + \
                    "AI: this looks decorative — tag it Artifact if it carries nothing"
            elif text:
                node.alt = text
                node.source = "ai"
                node.note = (node.note + "; " if node.note else "") + "alt text drafted by the AI — check it"
        else:
            ans = ans_ready.get("math") if ans_ready is not None else math_from_image(ai_cfg, png, ctx)
            if ans and ans.get("is_math") and ans.get("alt"):
                node.alt = ans["alt"]
                if ans.get("latex"):
                    node.style = dict(node.style or {}, latex=ans["latex"])
                node.note = (node.note + "; " if node.note else "") + "formula read aloud by the AI — check it"
    except Exception as exc:
        node.note = (node.note + "; " if node.note else "") + f"AI unavailable ({exc})"


HEADING_SIZE_STEPS = ((1.8, "H1"), (1.4, "H2"), (1.15, "H3"))


def _heading_level_like(model: DocModel, size: float, bold: bool, font: str) -> str | None:
    """The level the document already gives headings set like this one."""
    votes: collections.Counter = collections.Counter()
    for n in model.nodes.values():
        if not n.is_heading or n.artifact:
            continue
        st = n.style or {}
        try:
            s = float(st.get("size") or 0)
        except (TypeError, ValueError):
            continue
        if s and abs(s - size) <= 0.6 and bool(st.get("bold")) == bool(bold) and \
                (not font or not st.get("font") or str(st.get("font")).lower() in font.lower()):
            votes[n.role] += 1
    return votes.most_common(1)[0][0] if votes else None


def guess_zone_role(model: DocModel, working_pdf: str, page_no: int, box, opts: Options,
                    ai_cfg=None, ai_answer=None) -> tuple[str, str]:
    """What a drawn zone most likely is -> (tag name, why).

    `ai_answer` is an answer the vision model already gave (zone_ai_answers,
    asked before the job was locked for the edit); without one the model is
    asked here."""
    from .analyzer import CAPTION_RE, PAGENO_RE, _is_formula
    doc = pymupdf.open(working_pdf)
    try:
        page = doc[page_no]
        un = pymupdf.Rect(box) * page.transformation_matrix
        un.normalize()
        lines = [l for l in collect_lines(page)
                 if un.contains(pymupdf.Point((l.x0 + l.x1) / 2, (l.y0 + l.y1) / 2))]
        text = " ".join(l.text for l in lines).strip()
        body = model.body_size or 10.0
        size = max((l.size for l in lines), default=0.0)
        bold = bool(lines) and all(l.bold for l in lines)
        font = lines[0].font if lines else ""
        note = ""
        if ai_cfg is not None or ai_answer is not None:
            try:
                from .ai import zone_role
                prev = None
                for n in model.ordered_nodes():
                    if n.is_heading and not n.artifact and (n.page < page_no or (
                            n.page == page_no and n.bbox is not None and n.bbox[1] >= box[3] - 1)):
                        prev = n
                hints = (f"Font size of the text in the region: {size:.1f} pt"
                         f"{' bold' if bold else ''}; body text of the document: {body:.1f} pt; "
                         f"{len(lines)} printed line(s).")
                if prev is not None:
                    hints += f" The heading before this region is {prev.role}: “{(prev.text or '')[:80]}”."
                ans = ai_answer if ai_answer is not None else \
                    zone_role(ai_cfg, _zone_png(page, box, max_px=_ai_px(ai_cfg)), text, hints)
                if ans and ans.get("role"):
                    role = ans["role"]
                    if role.startswith("H") and role[1:].isdigit():
                        like = _heading_level_like(model, size, bold, font)
                        if like and not ans.get("level"):
                            role = like
                    return role, f"chosen for the zone by the AI ({role}{': ' + ans['why'] if ans.get('why') else ''})"
            except Exception as exc:
                note = f"AI unavailable ({exc}) — "
        # ---- the rules, when there is no model to ask
        zone_area = max(1.0, un.get_area())
        pics = 0.0
        try:
            for info in page.get_image_info():
                pics += (pymupdf.Rect(info["bbox"]) & un).get_area()
        except Exception:
            pass
        if not text:
            drawn = 0
            try:
                drawn = sum(1 for d in page.get_drawings() if pymupdf.Rect(d["rect"]).intersects(un))
            except Exception:
                pass
            if pics > 0.05 * zone_area or drawn >= 3:
                return "Figure", note + "chosen for the zone: a picture with no text"
            return "Artifact", note + "chosen for the zone: nothing to read"
        if pics > 0.5 * zone_area and len(text) < 200:
            return "Figure", note + "chosen for the zone: mostly a picture"
        head_band = un.y1 <= page.rect.height * 0.09 or un.y0 >= page.rect.height * 0.91
        if head_band and (PAGENO_RE.match(text) or len(text) < 60 and len(lines) == 1):
            return "Artifact", note + "chosen for the zone: a page number / running line"
        if len(lines) >= 2:
            try:
                padded = pymupdf.Rect(un.x0 - 3, un.y0 - 3, un.x1 + 3, un.y1 + 3) & page.rect
                for t in page.find_tables(clip=padded, strategy="lines").tables:
                    if t.row_count >= 2 and t.col_count >= 2 and \
                            (pymupdf.Rect(t.bbox) & un).get_area() >= 0.5 * zone_area:
                        return "Table", note + f"chosen for the zone: a ruled grid of {t.row_count}×{t.col_count}"
            except Exception:
                pass
            labelled = sum(1 for l in lines if LABEL_RE.match(l.text))
            if labelled >= 2 and labelled >= 0.3 * len(lines):
                return "L", note + "chosen for the zone: numbered / bulleted lines"
        if CAPTION_RE.match(text) and len(lines) <= 4:
            return "Caption", note + "chosen for the zone: a figure / table label"
        if len(lines) <= 3 and _is_formula(text):
            return "Formula", note + "chosen for the zone: an equation"
        if len(lines) <= 3 and len(text) <= 160 and (size >= body * 1.12 or (bold and size >= body - 0.3)):
            like = _heading_level_like(model, size, bold, font)
            if like:
                return like, note + f"chosen for the zone: set like the other {like} headings"
            for ratio, h in HEADING_SIZE_STEPS:
                if size >= body * ratio:
                    return h, note + f"chosen for the zone: {size:.0f} pt text over {body:.0f} pt body"
            return "H4", note + "chosen for the zone: a bold line"
        return "P", note + "chosen for the zone: running text"
    finally:
        doc.close()


def _rows_with_margin(groups: list[list], zone) -> list[list]:
    """The number printed in the margin belongs to its line, not to a tag of its own.

    A fluency passage prints a word count ("__73") out at the right edge of every
    line. The layout order reads that column on its own, so splitting a zone line
    by line left the text in one tag and the number in another — two tags for one
    printed row. On each printed row the leftmost piece is the line, and a narrow
    piece beside it (a number, a score box, a tick) joins it. A second column of
    real text is not narrow, so a zone drawn over two columns still splits into
    its own lines."""
    zone_w = max(1.0, zone.x1 - zone.x0)

    def narrow(g) -> bool:
        return all(len(p.text.strip()) <= 15 or (p.x1 - p.x0) <= 0.25 * zone_w for p in g)

    def band(parts) -> tuple[float, float]:
        return min(p.y0 for p in parts), max(p.y1 for p in parts)

    rows: list[list[int]] = []
    for i, g in enumerate(groups):
        top, bottom = band(g)
        for r in rows:
            rtop, rbottom = band([p for j in r for p in groups[j]])
            if rbottom > top + 0.5 and rtop < bottom - 0.5:      # the same printed row
                r.append(i)
                break
        else:
            rows.append([i])
    taken: set = set()
    for r in rows:
        if len(r) < 2:
            continue
        host = min(r, key=lambda j: min(p.x0 for p in groups[j]))
        for j in r:
            if j == host or not narrow(groups[j]):
                continue
            groups[host] = groups[host] + groups[j]
            taken.add(j)
    return [sorted(g, key=lambda x: x.x0) for i, g in enumerate(groups) if i not in taken]


def _paragraphs_from_rect(model: DocModel, working_pdf: str, page_no: int, box,
                          opts: Options, mode: str = "paragraphs") -> list[StructNode]:
    """One `P` per paragraph inside the marked area, each with its own zone.

    A zone drawn over half a page usually covers several paragraphs; making one
    tag of the lot would have a screen reader read them as a single block and
    lose every pause. The lines inside are grouped exactly as the automatic
    analysis groups them (column order, line spacing, indents, a full stop at a
    short last line), and each group becomes its own tag."""
    a = Analyzer(working_pdf, opts)
    a.model = model
    out: list[StructNode] = []
    try:
        page = a.doc[page_no]
        a.page_lines = [[] for _ in range(a.doc.page_count)]
        a.page_lines[page_no] = collect_lines(page)
        a.body = a.page_body_size(page_no, model.body_size or 10.0)
        un = pymupdf.Rect(box) * page.transformation_matrix
        un.normalize()
        lines = [l for l in collect_lines(page)
                 if un.contains(pymupdf.Point((l.x0 + l.x1) / 2, (l.y0 + l.y1) / 2))]
        if not lines:
            return []
        ordered: list = []
        xycut(list(lines), max(8.0, 0.8 * a.body), 0, ordered, [0])
        right: dict[int, float] = collections.defaultdict(float)
        for l in ordered:
            right[l.flow] = max(right[l.flow], l.x1)
        groups: list[list] = []
        if mode == "lines":
            # one tag per printed line: a numbered reading passage is scored line
            # by line, so each line has to be its own block in the reading order
            groups = [[l] for l in ordered]
            groups = _rows_with_margin(groups, un)
        else:
            for l in ordered:
                if groups and not a._breaks(groups[-1][-1], l, right[l.flow], groups[-1]):
                    groups[-1].append(l)
                else:
                    groups.append([l])
        if opts.detect_blank_lines:
            # "Name: ____  Date: ____" on one row: one P per prompt
            split: list[list] = []
            for g in groups:
                segs = split_prompts(g[0]) if len(g) == 1 else None
                split.extend([[s] for s in segs] if segs else [g])
            groups = split
        for g in groups:
            gbox = _ubox(x.bbox for x in g)
            text = ""
            for l in g:
                if text.endswith("-") and l.text[:1].islower():
                    text = text[:-1] + l.text
                else:
                    text = (text + " " + l.text).strip()
            nd = StructNode(id=model.new_id("u"), role="P", page=page_no,
                            bbox=to_pdf(page, gbox), text=text, source="user", priority=1,
                            note=("drawn by hand" if len(groups) == 1 else
                                  f"drawn by hand — {'line' if mode == 'lines' else 'paragraph'} "
                                  f"{len(out) + 1} of {len(groups)} in the zone"))
            model.nodes[nd.id] = nd
            out.append(nd)
            if opts.detect_blank_lines and UNDERSCORE_RUN_RE.search(text):
                a._underscore_spans(page, page_no, nd, g)
        return out
    finally:
        a.doc.close()
        a.pdf.close()


def items_missing_labels(model: DocModel) -> int:
    """List items whose number is still inside the LBody."""
    n = 0
    for li in model.nodes.values():
        if li.role != "LI":
            continue
        kids = [model.nodes[c] for c in li.children if c in model.nodes]
        if any(k.role == "Lbl" for k in kids):
            continue
        body = next((k for k in kids if k.role == "LBody"), None)
        if body is not None and LABEL_RE.match(body.text or ""):
            n += 1
    return n


def fix_item_labels(model: DocModel, working_pdf: str, opts: Options | None = None) -> int:
    """Editor: every list item whose number is still inside its LBody gets a Lbl."""
    a = Analyzer(working_pdf, opts or Options())
    a.model = model
    try:
        a.body = model.body_size or 10.0
        a.page_lines = [collect_lines(pg) for pg in a.doc]
        return a._fix_item_labels()
    finally:
        a.doc.close()
        a.pdf.close()


def _zone_png(page, box, zoom: float = 2.2, max_px: int = 1100) -> bytes:
    """The marked area as a picture for the vision model — at most `max_px` on
    its longest side. A whole-page zone at 2.2x is 1300x1900 px, and a vision
    model spends minutes on tiles that say nothing more than the smaller
    picture does (V4.16)."""
    r = pymupdf.Rect(box) * page.transformation_matrix
    r.normalize()
    r += (-6, -6, 6, 6)
    r &= page.rect
    if max_px > 0:
        side = max(r.width, r.height) or 1.0
        zoom = max(0.5, min(zoom, max_px / side))
    return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=r).tobytes("png")


def _ai_px(ai_cfg) -> int:
    """How large a picture this server wants to be sent (Settings)."""
    try:
        return int(getattr(ai_cfg, "max_image_px", 0) or 0) or 1100
    except Exception:
        return 1100


def zone_ai_answers(model: DocModel, working_pdf: str, page_no: int, rect_display,
                    role: str, opts: Options | None = None, ai_cfg=None) -> dict:
    """Ask the vision model about a zone the person has just drawn, without
    touching the model tree — so the web app can ask BEFORE it locks the job
    for the edit and the rest of the editor keeps answering (V4.16).

    -> {"role": <what it is>, "structure": <rows/columns of a table or list>},
    either key missing when that question does not arise. The answers go back
    into create_from_rect(ai_answers=…), which then asks nothing itself."""
    if ai_cfg is None:
        return {}
    out: dict = {}
    doc = pymupdf.open(working_pdf)
    try:
        if not 0 <= page_no < doc.page_count:
            return {}
        page = doc[page_no]
        box = display_to_pdf(page, rect_display)
        if area(box) < 4:
            return {}
        un = pymupdf.Rect(box) * page.transformation_matrix
        un.normalize()
        lines = [l for l in collect_lines(page)
                 if un.contains(pymupdf.Point((l.x0 + l.x1) / 2, (l.y0 + l.y1) / 2))]
        text = re.sub(r"\s+", " ", " ".join(l.text for l in lines)).strip()
        png = _zone_png(page, box, max_px=_ai_px(ai_cfg))
        if role == "Auto":
            from .ai import zone_role
            size = max((l.size for l in lines), default=0.0)
            bold = bool(lines) and all(l.bold for l in lines)
            body = model.body_size or 10.0
            hints = (f"Font size of the text in the region: {size:.1f} pt"
                     f"{' bold' if bold else ''}; body text of the document: {body:.1f} pt; "
                     f"{len(lines)} printed line(s).")
            try:
                ans = zone_role(ai_cfg, png, text, hints)
            except Exception:
                ans = None
            if ans:
                out["role"] = ans
                role = ans.get("role") or role
        if role in ("Table", "L"):
            from .ai import zone_structure
            try:
                out["structure"] = zone_structure(
                    ai_cfg, png, "table" if role == "Table" else "list",
                    " / ".join(l.text for l in lines)[:500]) or {}
            except Exception:
                pass
        if role in ("Figure", "Formula"):
            from .ai import alt_text, math_from_image
            ctx = _zone_context(model, page_no, box)
            got: dict = {}
            try:
                if role == "Figure":
                    got["alt"] = (alt_text(ai_cfg, png, ctx) or "").strip()
                else:
                    got["math"] = math_from_image(ai_cfg, png, ctx)
            except Exception:
                got = {}
            if got:
                out["describe"] = got
        return out
    finally:
        doc.close()


def _table_score(tb, want, lines=None) -> tuple:
    """How well a found grid matches the area the person marked.

    More cells is not better: a looser strategy will happily run past the box and
    swallow the paragraph above it, or cut the gaps between the printed rows into
    rows of their own. What counts is staying inside the marked area, having
    something printed in each row, filling the area, and rows of equal width."""
    try:
        cells = [c for c in tb.cells if c]
        if not cells:
            return (0, 0, 0, 0)
        b = pymupdf.Rect(tb.bbox)
        w = pymupdf.Rect(want)
        inter = (b & w).get_area()
        inside = inter / max(1e-6, b.get_area())        # none of it outside the box
        covers = inter / max(1e-6, w.get_area())        # and it fills the box
        rows: dict = collections.defaultdict(int)
        for c in cells:
            rows[round(c[1], 1)] += 1
        widths = list(rows.values())
        regular = len(set(widths)) <= 1
        # a grid built from the text positions often puts a row in every gap
        # between the printed rows; a ruled grid has something in every row
        filled = 1.0
        if lines is not None:
            bands = sorted({round(c[1], 1) for c in cells} | {round(c[3], 1) for c in cells})
            hit = sum(1 for i in range(len(bands) - 1)
                      if any(bands[i] - 1 <= (l.y0 + l.y1) / 2 <= bands[i + 1] + 1
                             and b.x0 - 1 <= (l.x0 + l.x1) / 2 <= b.x1 + 1 for l in lines))
            filled = hit / max(1, len(bands) - 1)
        return (round(inside, 2), round(filled, 1), round(covers, 1), regular, len(cells))
    except Exception:
        return (0, 0, 0, 0)


def _add_missed_rows(model: DocModel, page, page_no: int, table: StructNode, lines: list) -> int:
    """Text inside a drawn Table zone that no cell covers becomes its own row.

    A worksheet often rules its word grid and the sentences under it as two
    separate boxes. The grid finder returns one of them, so marking the whole
    thing as a Table left the sentences outside the table — printed inside it,
    read after it. When the person has drawn the area and said "this is one
    table", everything inside it belongs in a cell: each line of text no cell
    covers becomes a full-width row, in its place in the reading order."""
    cells = [n for n in model.descendants(table.id)
             if n.role in ("TD", "TH") and n.bbox is not None]
    if not cells:
        return 0
    rows = [n for n in model.descendants(table.id) if n.role == "TR" and n.bbox is not None]
    ncols = max((sum(max(1, model.nodes[c].colspan) for c in r.children if c in model.nodes)
                 for r in rows), default=1)

    def covered(bb) -> bool:
        cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
        return any(c.bbox[0] - 1 <= cx <= c.bbox[2] + 1 and c.bbox[1] - 1 <= cy <= c.bbox[3] + 1
                   for c in cells)

    missed = []
    for l in lines:
        bb = to_pdf(page, l.bbox)
        if not covered(bb) and (l.text or "").strip():
            missed.append((bb, l))
    if not missed:
        return 0
    groups: list[list] = []
    for bb, l in sorted(missed, key=lambda t: (-t[0][3], t[0][0])):
        if groups and min(g[0][1] for g in groups[-1]) < bb[3] - 0.5 and \
                max(g[0][3] for g in groups[-1]) > bb[1] + 0.5:
            groups[-1].append((bb, l))
        else:
            groups.append([(bb, l)])

    host = next((model.nodes[c] for c in table.children
                 if model.nodes[c].role == "TBody"), table)
    x0, x1 = table.bbox[0], table.bbox[2]
    added = 0
    for g in groups:
        top = max(bb[3] for bb, _ in g)
        bottom = min(bb[1] for bb, _ in g)
        text = " ".join(l.text for _, l in sorted(g, key=lambda t: t[0][0])).strip()
        tr = StructNode(id=model.new_id("tr"), role="TR", page=page_no,
                        bbox=(x0, bottom, x1, top), source="user", priority=1,
                        note="row added from text the grid did not cover")
        sibs = [model.nodes[c] for c in host.children if model.nodes[c].role == "TR"]
        pos = len(host.children)
        for r in sibs:
            if r.bbox is not None and r.bbox[3] < top:          # this row sits higher
                pos = host.children.index(r.id)
                break
        model.add(tr, host.id, pos)
        td = StructNode(id=model.new_id("td"), role="TD", page=page_no, text=text,
                        bbox=(x0, bottom, x1, top), source="user", priority=1)
        td.colspan = ncols
        model.add(td, tr.id)
        table.bbox = union(table.bbox, tr.bbox)
        added += 1
    if added:
        table.note = (table.note + "; " if table.note else "") + \
            f"{added} row(s) added from text the grid did not cover"
    return added


def _structured_from_rect(model: DocModel, working_pdf: str, page_no: int, box,
                          role: str, opts: Options, deep: bool = False, ai_cfg=None,
                          ai_answers: dict | None = None) -> str:
    a = Analyzer(working_pdf, opts)
    a.model = model
    page = a.doc[page_no]
    un = pymupdf.Rect(box) * page.transformation_matrix
    un.normalize()
    try:
        a.page_lines = [[] for _ in range(a.doc.page_count)]
        a.page_lines[page_no] = collect_lines(page)
        a.body = a.page_body_size(page_no, model.body_size or 10.0)
        lines = [l for l in collect_lines(page)
                 if un.contains(pymupdf.Point((l.x0 + l.x1) / 2, (l.y0 + l.y1) / 2))]
        if role == "Table":
            tabs = []
            ai_ans = None
            ai_note = ""
            padded = pymupdf.Rect(un.x0 - 4, un.y0 - 4, un.x1 + 4, un.y1 + 4) & page.rect
            if getattr(opts, "table_strategy", "") == "journal":
                # a journal's table (ruled with horizontal lines or bands only)
                # read the way the analysis reads it — and its caption and notes
                # kept out of it, as their own tags
                jid_ = _journal_table(a, model, page, page_no, un, padded, lines)
                if jid_ is not None:
                    return jid_
            if deep:
                # look harder: try every strategy and keep the grid that reads
                # best (most cells, rows of equal width), not just the first one
                if (ai_answers or {}).get("structure") is not None:
                    ai_ans = (ai_answers or {}).get("structure") or None
                elif ai_cfg is not None:
                    # the vision model counts the rows and columns it sees; of the
                    # grids that fit the zone, the one with that shape wins
                    try:
                        from .ai import zone_structure
                        ai_ans = zone_structure(ai_cfg, _zone_png(page, box, max_px=_ai_px(ai_cfg)),
                                                "table",
                                                " / ".join(l.text for l in lines)[:500])
                    except Exception as exc:
                        ai_note = f"AI check failed ({exc})"
                cands = []
                for strat in ("lines", "lines_strict", "text"):
                    try:
                        found = list(page.find_tables(clip=padded, strategy=strat).tables)
                    except Exception:
                        found = []
                    for t in found:
                        cands.append((_table_score(t, padded, lines), t))
                cands.sort(key=lambda c: c[0], reverse=True)
                pick = cands[0] if cands else None
                if pick is not None and ai_ans and ai_ans.get("rows") and ai_ans.get("columns") \
                        and ai_ans.get("kind") in ("table", ""):
                    def dist(t):
                        try:
                            return abs(t.row_count - ai_ans["rows"]) + abs(t.col_count - ai_ans["columns"])
                        except Exception:
                            return 99
                    good = [c for c in cands if len(c[0]) > 2 and c[0][0] >= 0.85 and c[0][1] >= 0.7]
                    if good:
                        chosen = min(good, key=lambda c: dist(c[1]))
                        if chosen is not pick and dist(chosen[1]) < dist(pick[1]):
                            ai_note = (f"AI: {ai_ans['rows']}×{ai_ans['columns']} — the grid with that "
                                       f"shape was used")
                            pick = chosen
                tabs = [pick[1]] if pick else []
                if not tabs and ai_ans and ai_ans.get("kind") in ("list", "paragraphs"):
                    raise EditError(f"no table grid found in that area — the AI reads it as "
                                    f"{ai_ans['kind']}; try L or P")
            else:
                for strat in ("lines", "text"):
                    try:
                        tabs = list(page.find_tables(clip=padded, strategy=strat).tables)
                    except Exception:
                        tabs = []
                    if tabs:
                        break
            if not tabs:
                raise EditError("no table grid found in that area — try drawing tighter around the table")
            tb = max(tabs, key=lambda t: pymupdf.Rect(t.bbox).get_area())
            try:
                from .analyzer import _clean_table, _h_rules
                tb = _clean_table(tb, _h_rules(page)) or tb
            except Exception:
                pass
            node = a._build_table(page, page_no, tb, lines, refine=True)
            _add_missed_rows(model, page, page_no, node, lines)
            node.priority = 1
            node.source = "user"
            for d in model.descendants(node.id):
                d.priority = 1
            model.root_children.append(node.id)
            node.parent = None
            if ai_note:
                node.note = (node.note + "; " if node.note else "") + ai_note
            if deep and ai_ans is not None:
                _ai_check_zone(model, page, node, "table", ai_cfg, ans=ai_ans)
            tidy_tables(model)
            return node.id
        # list
        for l in lines:
            l.flow = 0
        lines.sort(key=lambda l: (round(l.y0, 1), l.x0))
        j, top = a._list(page, page_no, lines, 0) if lines else (0, None)
        if top is None:
            # no bullets or numbers: one item per line — unless the text model,
            # asked when the AI is on, says which lines start an item
            L = StructNode(id=model.new_id("list"), role="L", page=page_no, bbox=box,
                           list_numbering="None", source="user")
            model.add(L)
            starts = list(range(len(lines)))
            if ai_cfg is not None and len(lines) > 1:
                try:
                    from .ai import list_item_starts
                    got = list_item_starts(ai_cfg, [l.text for l in lines])
                    if got:
                        starts = got
                        L.note = f"AI: {len(got)} item(s) in {len(lines)} line(s)"
                except Exception as exc:
                    L.note = f"AI unavailable ({exc}) — one item per line"
            bounds = list(zip(starts, starts[1:] + [len(lines)]))
            for a_, b_ in bounds:
                grp = lines[a_:b_]
                gb = to_pdf(page, _ubox(x.bbox for x in grp))
                text = " ".join(x.text for x in grp).strip()
                li = StructNode(id=model.new_id("li"), role="LI", page=page_no, bbox=gb)
                model.add(li, L.id)
                model.add(StructNode(id=model.new_id("lb"), role="LBody", page=page_no,
                                     bbox=gb, text=text, source="user", priority=1), li.id)
            return L.id
        top.source = "user"
        if deep and (ai_cfg is not None or (ai_answers or {}).get("structure") is not None):
            _ai_check_zone(model, page, top, "list", ai_cfg,
                           ans=(ai_answers or {}).get("structure"))
        # items set in two columns: "1. duck     The duck is swimming." — two
        # texts in the item, also when the second column was drawn apart (V4.21.4)
        try:
            from .analyzer import list_columns
            list_columns(model, working_pdf, [top.id])
        except Exception:
            pass
        for d in model.descendants(top.id):
            d.priority = 1
        return top.id
    finally:
        a.doc.close()
        a.pdf.close()


def _journal_table(a, model: DocModel, page, page_no: int, un, padded, lines: list) -> str | None:
    from .analyzer import _CleanTable, _h_rules, _ubox
    from .tablegrid import journal_cells, rule_regions
    cells: list = []
    region = pymupdf.Rect(un)
    try:
        # the rules drawn across the area give the bands (rows, caption, notes);
        # a rule drawn cell by cell is one rule
        raw = sorted((r for r in _h_rules(page) if un.y0 - 2 <= r[0] <= un.y1 + 2
                      and min(r[2], un.x1) - max(r[1], un.x0) > 0.5 * (r[2] - r[1])),
                     key=lambda r: (round(r[0], 1), r[1]))
        rules: list = []
        for y, x0, x1 in raw:
            if rules and abs(rules[-1][0] - y) < 0.6 and x0 <= rules[-1][2] + 2.5:
                rules[-1] = (rules[-1][0], rules[-1][1], max(rules[-1][2], x1))
            else:
                rules.append((y, x0, x1))
        for _, rc in rule_regions(rules, page.rect.width, []):
            cells += list(rc)
        if cells:
            # the area may run on past the table: under its last rule only the
            # notes set close below it (smaller than the text that follows)
            last = max(c[3] for c in cells)
            inside_rules = [l for l in lines if min(c[1] for c in cells) <= (l.y0 + l.y1) / 2 <= last]
            body = statistics.median([l.size for l in inside_rules]) if inside_rules else 9.0
            prev = last
            for l in sorted((l for l in lines if (l.y0 + l.y1) / 2 > last), key=lambda l: l.y0):
                if l.y0 - prev > max(4.0, 0.9 * (l.y1 - l.y0)) or l.size > body + 0.8:
                    break
                prev = l.y1
            region.y1 = min(region.y1, prev + 1.0)
            # and over its first rule only its caption ("Table 2 Descriptive …")
            first = min(c[1] for c in cells)
            above = sorted((l for l in lines if (l.y0 + l.y1) / 2 < first), key=lambda l: l.y0)
            caps = [l for l in above if re.match(r"^\s*(?:Table|Tab\.)\s*[\dIVXivx]+", l.text)]
            region.y0 = max(region.y0, (caps[-1].y0 - 1.0) if caps else first - 1.0)
    except Exception:
        cells = []
    if not cells:
        try:
            for t in page.find_tables(clip=padded, strategy="lines").tables:
                cells += [c for c in t.cells if c]
        except Exception:
            pass
    try:
        res = journal_cells(page, tuple(region), cells)
    except Exception:
        res = None
    if res is None or res.get("cols", 0) < 2 or res.get("rows", 0) < 1:
        return None
    jt = _CleanTable(res["cells"])
    jt.journal = res
    bb = pymupdf.Rect(res["bbox"])
    inside = lambda l, r: r.contains(pymupdf.Point((l.x0 + l.x1) / 2, (l.y0 + l.y1) / 2))
    node = a._build_table(page, page_no, jt, [l for l in lines if inside(l, bb + (-1, -1, 1, 1))])
    node.priority = 1
    node.source = "user"
    for d in model.descendants(node.id):
        d.priority = 1
    model.root_children.append(node.id)
    node.parent = None
    before, after = [], []
    if res.get("caption"):
        cl = [l for l in lines if inside(l, pymupdf.Rect(res["caption"]))]
        if cl:
            cap = StructNode(id=model.new_id("cap"), role="Caption", page=page_no,
                             bbox=to_pdf(page, _ubox(l.bbox for l in cl)),
                             text=" ".join(l.text for l in cl).strip(), source="user", priority=1)
            model.add(cap)
            before.append(cap.id)
    if res.get("notes"):
        nl = sorted([l for l in lines if inside(l, pymupdf.Rect(res["notes"]))], key=lambda l: (l.y0, l.x0))
        groups: list[list] = []
        mark_re = re.compile(r"^\s*[*\u2020\u2021\u00a7\u00b6#]{1,3}")
        for l in nl:
            # a note opens with its mark; the abbreviations run on over lines
            if not groups or mark_re.match(l.text) or (
                    mark_re.match(groups[-1][0].text) and groups[-1][-1].text.rstrip().endswith(".")):
                groups.append([l])
            else:
                groups[-1].append(l)
        for g in groups:
            t = " ".join(l.text for l in g).strip()
            nt = StructNode(id=model.new_id("tn"), role=("Note" if re.match(r"^\s*[*\u2020\u2021\u00a7\u00b6#]", t)
                                                       else "P"),
                            page=page_no, bbox=to_pdf(page, _ubox(l.bbox for l in g)), text=t,
                            source="user", priority=1, note="table note (drawn with the table)")
            model.add(nt)
            after.append(nt.id)
    if before or after:
        model.info.setdefault("_zone_extras", {})[node.id] = {"before": before, "after": after}
    tidy_tables(model)
    return node.id


def _node_text(model: DocModel, nid: str) -> str:
    """The words of a tag and everything inside it, in reading order."""
    n = model.nodes[nid]
    parts = [n.text] if (n.text or "").strip() else []
    parts += [d.text for d in model.descendants(nid) if (d.text or "").strip() and not d.artifact]
    return re.sub(r"\s+", " ", " / ".join(parts)).strip()


def _ai_check_zone(model: DocModel, page, node: StructNode, role: str, ai_cfg, ans=None) -> None:
    """Show the marked area to the vision model and take what it is sure about:
    how many header rows a table has, and whether a list is numbered."""
    from .ai import zone_structure
    if ans is None:
        try:
            png = _zone_png(page, node.bbox)
            text = _node_text(model, node.id)[:500] if hasattr(model, "nodes") else ""
            ans = zone_structure(ai_cfg, png, role, text)
        except Exception as exc:
            node.note = (node.note + "; " if node.note else "") + f"AI check failed ({exc})"
            return
    if not ans:
        return
    note = []
    if role == "table":
        if ans["kind"] and ans["kind"] not in ("table", ""):
            note.append(f"AI reads this area as {ans['kind']}, not a table — check it")
        hr = ans.get("header_rows")
        cur = int((node.style or {}).get("header_rows", 0) or 0)
        if hr is not None and hr != cur and hr <= 2:
            try:
                table_headers(model, node.id, hr, 1 if ans.get("header_column") else 0, "auto")
                note.append(f"AI: {hr} header row(s)" + (" + header column" if ans.get("header_column") else ""))
            except EditError:
                pass
        if ans.get("rows") and ans.get("columns"):
            occ, widths = table_grid(model, node.id)
            if widths and (abs(len(widths) - ans["rows"]) > 1 or abs(max(widths) - ans["columns"]) > 1):
                note.append(f"AI sees {ans['rows']}×{ans['columns']}, the tool built "
                            f"{len(widths)}×{max(widths)} — check the grid")
    else:
        if ans["kind"] == "table":
            note.append("AI reads this area as a table, not a list — check it")
        want = "Decimal" if ans.get("ordered") else "Disc"
        if node.list_numbering in ("", "None") and ans.get("kind") == "list":
            node.list_numbering = want
            note.append(f"AI: {'numbered' if ans.get('ordered') else 'bulleted'} list")
    if ans.get("why"):
        note.append(f"AI: {ans['why']}")
    if note:
        node.note = (node.note + "; " if node.note else "") + "; ".join(note)


def _list_zone_below(working_pdf: str, page_no: int, box) -> tuple:
    """The list's zone, stretched down over the lines that still belong to it.

    A list drawn (or read) a little short leaves out its last points — "– If the
    student has not been assessed on Skill Set 1 …" sits just under the zone,
    set in past the item marks. Lines straight below the zone, in the same
    column and set in past the list's own margin, are part of the list; the
    first line back at the margin, a wider gap or another column ends it."""
    from .analyzer import LABEL_RE, collect_lines
    doc = pymupdf.open(working_pdf)
    try:
        page = doc[page_no]
        mat = page.transformation_matrix
        r = pymupdf.Rect(box) * mat
        r.normalize()
        lines = [l for l in collect_lines(page) if (l.text or "").strip()]
        inside = [l for l in lines if r.x0 - 2 <= (l.x0 + l.x1) / 2 <= r.x1 + 2
                  and r.y0 - 2 <= (l.y0 + l.y1) / 2 <= r.y1 + 2]
        if not inside:
            return box
        labelled = [l for l in inside if LABEL_RE.match(l.text or "")]
        margin = min(l.x0 for l in (labelled or inside))
        height = statistics.median([l.y1 - l.y0 for l in inside]) if inside else 12.0
        bottom, took = r.y1, []
        for l in sorted((l for l in lines if l.y0 >= r.y1 - 1), key=lambda l: l.y0):
            w = max(1.0, l.x1 - l.x0)
            if max(0.0, min(l.x1, r.x1) - max(l.x0, r.x0)) / w < 0.5:
                continue                              # another column
            if l.y0 - bottom > 1.8 * height or l.x0 < margin + 4:
                break
            took.append(l)
            bottom = max(bottom, l.y1)
        if not took:
            return box
        grown = pymupdf.Rect(r.x0, r.y0, max(r.x1, max(l.x1 for l in took)), bottom + 0.5)
        out = grown * ~mat
        out.normalize()
        return (out.x0, out.y0, out.x1, out.y1)
    finally:
        doc.close()


def _zone_sequence(model: DocModel, working_pdf: str, page_no: int, box,
                   opts: Options) -> list[StructNode]:
    """What is printed in one zone of a list, in reading order: the lists in it
    and the paragraphs between them (the text of an item that ran on from the
    page before, a note under an item). Each comes back detached, ready to be
    put in place."""
    a = Analyzer(working_pdf, opts)
    a.model = model
    out: list[StructNode] = []
    try:
        page = a.doc[page_no]
        un = pymupdf.Rect(box) * page.transformation_matrix
        un.normalize()
        a.page_lines = [[] for _ in range(a.doc.page_count)]
        a.page_lines[page_no] = collect_lines(page)
        a.body = a.page_body_size(page_no, model.body_size or 10.0)
        lines = [l for l in collect_lines(page) if (l.text or "").strip()
                 and un.contains(pymupdf.Point((l.x0 + l.x1) / 2, (l.y0 + l.y1) / 2))]
        for l in lines:
            l.flow = 0
        lines.sort(key=lambda l: (round(l.y0, 1), l.x0))
        i = 0
        while i < len(lines):
            if a._label(lines[i], limit=None) is not None:
                before = list(model.root_children)
                j, top = a._list(page, page_no, lines, i)
                if top is not None and j > i:
                    new = [r for r in model.root_children if r not in before]
                    for r in new:
                        model.detach(r)
                        out.append(model.nodes[r])
                    i = j
                    continue
            j = i + 1
            while j < len(lines) and a._label(lines[j], limit=None) is None \
                    and lines[j].y0 - lines[j - 1].y1 < 0.8 * lines[j].size \
                    and abs(lines[j].x0 - lines[i].x0) < 30:
                j += 1
            grp = lines[i:j]
            p = StructNode(id=model.new_id("p"), role="P", page=page_no, source="user",
                           bbox=to_pdf(page, _ubox(l.bbox for l in grp)),
                           text=" ".join(l.text for l in grp))
            model.nodes[p.id] = p
            out.append(p)
            i = j
        return out
    finally:
        a.doc.close()
        a.pdf.close()


def _rebuild_list_across_pages(model: DocModel, working_pdf: str, nid: str,
                               opts: Options) -> str | None:
    """A list that runs over a page break, read again from every one of its zones.

    Item 4 starts at the foot of page 6 and runs on at the top of page 7, with
    paragraphs and a bulleted list of its own, then comes item 5. Each zone is
    read on its own and the pieces are put back together by the same rules the
    automatic pass uses: what runs on goes inside item 4, item 5 joins the list."""
    from .repair import join_split_lists, nest_sub_items
    n = model.nodes[nid]
    zones = [(n.page, n.bbox)] + [(int(p[0]), tuple(p[1:5])) for p in n.parts]
    zones = [z for z in zones if z[1] is not None]
    if len({z[0] for z in zones}) < 2:
        return None
    zones.sort(key=lambda z: z[0])
    parent = n.parent
    sibs = model.siblings(nid)
    pos = sibs.index(nid) if nid in sibs else len(sibs)
    seq: list[StructNode] = []
    for pg, box in zones:
        seq += _zone_sequence(model, working_pdf, pg, box, opts)
    lists = [x for x in seq if x.role == "L"]
    if not lists:
        for x in seq:
            model._drop_subtree(x.id)
        return None
    model.detach(nid)
    model._drop_subtree(nid)
    for k, x in enumerate(seq):
        model.add(x, parent, pos + k)
        x.source = "user"
        for d in model.descendants(x.id):
            d.priority = 1
    focus = {x.id for x in lists}
    join_split_lists(model, focus)
    nest_sub_items(model, focus)
    first = lists[0]
    first.priority = 1
    first.note = (first.note + "; " if first.note else "") + \
        "read again from its zones on every page and joined"
    return first.id


def rebuild_structured(model: DocModel, working_pdf: str, nid: str, opts: Options,
                       ai_cfg=None) -> str:
    """Build a Table or L again from its own zone, looking harder this time.

    Used after a zone is drawn or stretched: the area is what the person means,
    so the grid (or the list) is read again inside exactly that area."""
    if nid not in model.nodes:
        raise EditError("no such tag")
    n = model.nodes[nid]
    if n.role not in ("Table", "L"):
        raise EditError("only a Table or a list can be built again from its zone")
    if n.bbox is None or n.page < 0:
        raise EditError("this tag has no zone on a page")
    parent, sibs = n.parent, model.siblings(nid)
    pos = sibs.index(nid) if nid in sibs else None
    box, page_no, role, note = n.bbox, n.page, n.role, n.note
    if role == "L" and any(int(p[0]) != page_no for p in n.parts):
        done = _rebuild_list_across_pages(model, working_pdf, nid, opts)
        if done is not None:
            return done
    if role == "L":
        grown = _list_zone_below(working_pdf, page_no, box)
        if tuple(grown) != tuple(box):
            # the tags printed in the strip it grew over are read in the list now
            strip = (grown[0], grown[1], grown[2], box[1])
            for o in list(model.nodes.values()):
                if o.id == nid or o.parent != parent or o.page != page_no \
                        or not o.bbox or o.artifact:
                    continue
                if area(o.bbox) > 0 and intersection(o.bbox, strip) / area(o.bbox) >= 0.7:
                    model.detach(o.id)
                    model._drop_subtree(o.id)
            sibs = model.siblings(nid)
            pos = sibs.index(nid) if nid in sibs else pos
            box = grown
    new_id = _structured_from_rect(model, working_pdf, page_no, box, role, opts,
                                   deep=True, ai_cfg=ai_cfg)
    extras = (model.info.get("_zone_extras") or {}).pop(new_id, None)
    if not model.info.get("_zone_extras"):
        model.info.pop("_zone_extras", None)
    for x in (extras or {}).get("before", []) + (extras or {}).get("after", []):
        # the table's own zone read again: its caption and notes are tags already
        if x in model.nodes:
            model.remove(x, promote_children=False)
    new = model.nodes[new_id]
    model.detach(new_id)
    model.detach(nid)
    model._drop_subtree(nid)
    model.add(new, parent, pos)
    new.priority = 1
    new.source = "user"
    if note and "built again" not in note:
        new.note = (new.note + "; " if new.note else "") + "built again from the zone"
    return new.id


def reorder_page(model: DocModel, page: int, clicked: list[str]) -> None:
    """Put the clicked tags' top-level ancestors in click order, keeping their slots."""
    tops: list[str] = []
    for cid in clicked:
        if cid not in model.nodes:
            continue
        cur = model.nodes[cid]
        while cur.parent is not None:
            cur = model.nodes[cur.parent]
        if cur.id not in tops:
            tops.append(cur.id)
    if len(tops) < 2:
        return
    roots = model.root_children
    slots = sorted(roots.index(t) for t in tops)
    for slot, t in zip(slots, tops):
        roots[slot] = t


def table_headers(model: DocModel, tid: str, header_rows: int, header_cols: int,
                  mode: str = "auto") -> None:
    if model.nodes[tid].role != "Table":
        raise EditError("not a table")
    occ, _ = table_grid(model, tid)
    if not occ:
        raise EditError("table has no cells")
    start: dict[str, tuple[int, int]] = {}
    for (r, c), cid in sorted(occ.items()):
        start.setdefault(cid, (r, c))
    spans = any(model.nodes[c].rowspan > 1 or model.nodes[c].colspan > 1 for c in start)
    use_ids = mode == "ids" or (mode == "auto" and spans)
    for cid, (r, c) in start.items():
        cell = model.nodes[cid]
        head_row = r < header_rows
        head_col = c < header_cols
        if head_row or head_col:
            cell.role = "TH"
            cell.scope = "Column" if head_row and not head_col else ("Row" if head_col and not head_row else "Both")
        else:
            cell.role = "TD"
            cell.scope = ""
        cell.headers = []
        cell.source = "user"
        cell.confidence = 1.0
    if use_ids:
        for cid, (r, c) in start.items():
            cell = model.nodes[cid]
            if cell.role != "TD":
                continue
            hdrs = []
            for cc in range(c, c + max(1, cell.colspan)):
                for rr in range(header_rows):
                    h = occ.get((rr, cc))
                    if h and model.nodes[h].role == "TH" and h not in hdrs:
                        hdrs.append(h)
            for rr in range(r, r + max(1, cell.rowspan)):
                for cc in range(header_cols):
                    h = occ.get((rr, cc))
                    if h and model.nodes[h].role == "TH" and h not in hdrs:
                        hdrs.append(h)
            cell.headers = hdrs
    t = model.nodes[tid]
    t.style = dict(t.style or {}, header_rows=header_rows, header_cols=header_cols)
    t.source = "user"
    t.confidence = 1.0
    # a header cell with nothing in it is a data cell: an empty TH announces a
    # column that is not there, and PAC reports it
    convert_empty_headers(model)


# ------------------------------------------------------------- tag zones
RETEXT_ROLES = {"P", "H", "H1", "H2", "H3", "H4", "H5", "H6", "Caption", "Note", "Lbl", "LBody",
                "TD", "TH", "TOCI", "Link", "Span", "Quote", "BlockQuote", "Code", "Reference",
                "BibEntry", "Annot"}


def _text_in(page: pymupdf.Page, box) -> str:
    un = pymupdf.Rect(box) * page.transformation_matrix
    un.normalize()
    return re.sub(r"\s+", " ", printed_text(page, un)).strip()


def set_geometry(model: DocModel, working_pdf: str, nid: str, part: int | None,
                 page_no: int, rect_display) -> None:
    """Move/resize one of a tag's boxes (its zone). The tag then owns exactly
    the content inside the new box (user geometry wins over automatic tags)."""
    if nid not in model.nodes:
        raise EditError("no such tag")
    n = model.nodes[nid]
    doc = pymupdf.open(working_pdf)
    try:
        page = doc[page_no]
        box = display_to_pdf(page, rect_display)
        if area(box) < 1:
            raise EditError("the box is too small")
        if part is None or part < 0:
            n.bbox, n.page = box, page_no
        else:
            if part >= len(n.parts):
                raise EditError("no such part")
            n.parts[part] = [page_no, *box]
        n.priority = 1
        n.source = "user"
        n.confidence = 1.0
        if n.role in RETEXT_ROLES and not n.artifact:
            texts = []
            for p in sorted(n.pages()):
                for r in n.rects_on(p):
                    texts.append(_text_in(doc[p], r))
            n.text = " ".join(t for t in texts if t)
        if n.note and "drawn by hand" not in n.note:
            n.note = (n.note + "; " if n.note else "") + "zone adjusted by hand"
    finally:
        doc.close()


# ---------------------------------------------------------- reading order
FLOW_ROLES = {"Document", "Part", "Art", "Sect", "Div", "BlockQuote", "NonStruct", "Private", "Index"}
ITEM_ROLES = {"LI", "TR", "TD", "TH", "TOCI", "Lbl", "LBody"}


def is_block(model: DocModel, n: StructNode) -> bool:
    """A reading block: a top-level unit (paragraph, heading, whole table, whole list…)."""
    if n.artifact:
        return False
    return n.parent is None or model.nodes[n.parent].role in FLOW_ROLES


def reading_numbers(model: DocModel) -> dict[str, tuple[str, int | None]]:
    """id -> (display number like '5' or '5.2', block number or None)."""
    out: dict[str, tuple[str, int | None]] = {}
    block = 0
    for n in model.ordered_nodes():
        if n.artifact or (n.parent and out.get(n.parent, ("", None))[0] == ""
                          and n.parent in out and not is_block(model, n)):
            out[n.id] = ("", None)
            continue
        if is_block(model, n):
            block += 1
            out[n.id] = (str(block), block)
        else:
            sibs = [c for c in model.nodes[n.parent].children if not model.nodes[c].artifact]
            base = out.get(n.parent, ("", None))[0]
            out[n.id] = ((base + "." if base else "") + str(sibs.index(n.id) + 1), None)
    return out


def set_order(model: DocModel, nid: str, number: int) -> None:
    """Reading blocks take block position N; nested tags take sibling position N."""
    if nid not in model.nodes:
        raise EditError("no such tag")
    x = model.nodes[nid]
    number = int(number)
    if is_block(model, x):
        pool = [n for n in model.ordered_nodes() if is_block(model, n)]
    else:
        pool = [model.nodes[c] for c in model.nodes[x.parent].children if not model.nodes[c].artifact]
    ids = [n.id for n in pool]
    cur = ids.index(nid) + 1
    number = max(1, min(number, len(ids)))
    if number == cur:
        return
    target = pool[number - 1]
    if target.id in _subtree_ids(model, nid):
        raise EditError("that position is inside this tag itself")
    drop(model, [nid], target.id, "before" if number < cur else "after")


# ------------------------------------------------------- page auto-tagging
def parse_pages(spec: str, page_count: int) -> list[int]:
    out: set[int] = set()
    for part in re.split(r"[,\s]+", str(spec or "").strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
        if m:
            a, b = sorted((int(m.group(1)), int(m.group(2))))
            out.update(range(a, b + 1))
        elif part.isdigit():
            out.add(int(part))
        else:
            raise EditError(f"cannot read page list '{spec}' — use e.g. 5 or 5-8,10")
    pages = sorted(p - 1 for p in out if 1 <= p <= page_count)
    if not pages:
        raise EditError("no valid page numbers")
    return pages


def clear_pages(model: DocModel, pages: list[int]) -> int:
    """Take every tag off `pages` (V4.21.4: "Remove tags" — this page, pages
    from … to …, or the whole file). A tag printed on other pages too keeps
    them and gives up its zone here; a group left empty by this goes as well.
    The printed page is untouched; Undo brings the tags back."""
    pset = set(pages)
    whole = model.page_count and pset >= set(range(model.page_count))
    removed = 0
    touched: set = set()
    for n in reversed(model.ordered_nodes()):
        if n.id not in model.nodes:
            continue
        pg = n.pages()
        if not pg:
            desc = model.descendants(n.id)
            dp = set().union(*(d.pages() for d in desc)) if desc else set()
            if dp and dp <= pset:
                pg = dp
            elif n.children:
                continue
            elif n.page in pset or (whole and (n.page is None or n.page < 0)):
                pg = set(pset) if whole else {n.page}
            else:
                continue
        if pg <= pset:
            if n.parent is not None:
                touched.add(n.parent)
            model.remove(n.id, promote_children=True)
            removed += 1
        elif pg & pset:
            n.parts = [p for p in n.parts if int(p[0]) not in pset]
            if n.page in pset:
                if n.parts:
                    first = n.parts.pop(0)
                    n.page, n.bbox = int(first[0]), tuple(first[1:])
                else:
                    n.bbox = None
    # the groups this left with nothing inside (a Sect, a list, a table …)
    while touched:
        nid = touched.pop()
        n = model.nodes.get(nid)
        if n is None or n.children or (n.text or "").strip() or n.bbox is not None or n.parts:
            continue
        if n.parent is not None:
            touched.add(n.parent)
        model.remove(nid, promote_children=False)
        removed += 1
    model.check_integrity()
    return removed


def retag_pages(model: DocModel, auto: DocModel, pages: list[int]) -> dict:
    """Replace every tag on `pages` with the automatic proposal for those pages."""
    pset = set(pages)
    removed = 0

    # 1. drop geometry on these pages; remove tags that live only there
    order = model.ordered_nodes()
    for n in reversed(order):
        if n.id not in model.nodes:
            continue
        pg = n.pages()
        if not pg:
            # geometry-less containers: judge by their descendants
            desc = model.descendants(n.id)
            dp = set().union(*(d.pages() for d in desc)) if desc else set()
            if dp and dp <= pset:
                pg = dp
            elif n.children:
                continue
            elif n.page in pset:
                pg = {n.page}
            else:
                continue
        if pg <= pset:
            model.remove(n.id, promote_children=True)
            removed += 1
        elif pg & pset:
            # spans other pages too: keep it, but give up its geometry here
            n.parts = [p for p in n.parts if int(p[0]) not in pset]
            if n.page in pset:
                if n.parts:
                    first = n.parts.pop(0)
                    n.page, n.bbox = int(first[0]), tuple(first[1:])
                else:
                    n.bbox = None
    model.check_integrity()

    # 2. transplant the automatic tags of these pages (new ids)
    added = 0
    idmap: dict[str, str] = {}

    def clone(src_id: str, parent: str | None, index_: int | None, page: int) -> None:
        nonlocal added
        s = auto.nodes[src_id]
        n = StructNode(**{k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
                          for k, v in s.__dict__.items()})
        n.id = model.new_id("a")
        idmap[s.id] = n.id
        n.children = []
        n.mcids = {}
        n.src_keys = []
        # geometry restricted to this page
        rects = s.rects_on(page)
        if s.page != page and rects:
            n.page, n.bbox = page, rects[0]
            n.parts = [[page, *r] for r in rects[1:]]
        else:
            n.parts = [p for p in s.parts if int(p[0]) == page]
        model.add(n, parent, index_)
        added += 1
        for c in s.children:
            cn = auto.nodes[c]
            if page in cn.pages() or not cn.pages():
                clone(c, n.id, None, page)

    for p in pages:
        roots = [auto.nodes[r] for r in auto.root_children
                 if p in auto.nodes[r].pages() or (not auto.nodes[r].pages() and any(
                     p in d.pages() for d in auto.descendants(r)))]
        # the page's automatic reading order is inserted as one block, at the
        # point where its topmost real content belongs
        tops = [r.bbox[3] for r in roots if r.bbox and r.page == p and not r.artifact]
        pos = _insert_position(model, p, max(tops) if tops else 1e9)
        for k, r in enumerate(roots):
            clone(r.id, None, min(pos + k, len(model.root_children)), p)
    # remap header references inside the transplanted tables
    for new in idmap.values():
        n = model.nodes[new]
        if n.headers:
            n.headers = [idmap.get(h, h) for h in n.headers if idmap.get(h, h) in model.nodes]
    model.check_integrity()
    return {"removed": removed, "added": added, "pages": [p + 1 for p in pages]}


# ------------------------------------------------------ table cell zones
def fill_cell_zones(model: DocModel, working_pdf: str) -> int:
    """Give every TD/TH without geometry (typically empty cells of imported
    tables) a zone taken from the table grid: ruling lines when the page has
    them, otherwise the row/column extents of the neighbouring cells."""
    from .lint import table_grid
    doc = pymupdf.open(working_pdf)
    filled = 0
    try:
        for t in [n for n in model.nodes.values() if n.role == "Table" and not n.artifact]:
            occ, widths = table_grid(model, t.id)
            if not occ:
                continue
            start: dict[str, tuple[int, int]] = {}
            for (r, c), cid in sorted(occ.items()):
                start.setdefault(cid, (r, c))
            cells = {cid: model.nodes[cid] for cid in start}
            missing = [cid for cid, n in cells.items() if n.bbox is None]
            if not missing:
                continue
            pages = [n.page for n in cells.values() if n.bbox is not None and n.page >= 0]
            pno = t.page if t.page >= 0 else (max(set(pages), key=pages.count) if pages else -1)
            if pno < 0 or pno >= doc.page_count:
                continue
            page = doc[pno]
            nrows, ncols = len(widths), max(widths)
            grid = None
            # 1) ruling lines around the table
            boxes = [n.bbox for n in cells.values() if n.bbox is not None and n.page == pno]
            if t.bbox:
                boxes.append(t.bbox)
            if boxes:
                area_pdf = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                            max(b[2] for b in boxes), max(b[3] for b in boxes))
                clip = pymupdf.Rect(area_pdf) * page.transformation_matrix
                clip.normalize()
                try:
                    found = page.find_tables(strategy="lines").tables
                    found = sorted(found, key=lambda tb: -(pymupdf.Rect(tb.bbox) & clip).get_area())
                    for tb in found:
                        if (pymupdf.Rect(tb.bbox) & clip).is_empty:
                            continue
                        def lines(vals):
                            out = []
                            for v in sorted(vals):
                                if not out or v - out[-1] > 2:
                                    out.append(v)
                            return out
                        cs_ = [c for c in tb.cells if c]
                        xs = lines([c[0] for c in cs_] + [c[2] for c in cs_])
                        ys = lines([c[1] for c in cs_] + [c[3] for c in cs_])
                        if len(xs) - 1 == ncols and len(ys) - 1 == nrows:
                            grid = (xs, ys)
                            break
                except Exception:
                    grid = None
            for cid in missing:
                n = cells[cid]
                r, c = start[cid]
                rs, cs = max(1, n.rowspan), max(1, n.colspan)
                box = None
                if grid:
                    xs, ys = grid
                    disp = pymupdf.Rect(xs[c], ys[r], xs[min(c + cs, ncols)], ys[min(r + rs, nrows)])
                    rr = disp * ~page.transformation_matrix
                    rr.normalize()
                    box = (round(rr.x0, 2), round(rr.y0, 2), round(rr.x1, 2), round(rr.y1, 2))
                else:
                    # 2) neighbours: column x-extent from the same column, row y-extent from the same row
                    colb = [cells[o].bbox for o, (rr_, cc) in start.items()
                            if cc == c and cells[o].colspan == 1 and cells[o].bbox is not None]
                    rowb = [cells[o].bbox for o, (rr_, cc) in start.items()
                            if rr_ == r and cells[o].rowspan == 1 and cells[o].bbox is not None]
                    if colb and rowb:
                        box = (min(b[0] for b in colb), min(b[1] for b in rowb),
                               max(b[2] for b in colb), max(b[3] for b in rowb))
                if box and box[2] - box[0] > 1 and box[3] - box[1] > 1:
                    n.bbox, n.page = box, pno
                    n.note = (n.note + "; " if n.note else "") + "zone from the table grid (empty cell)"
                    filled += 1
    finally:
        doc.close()
    return filled


# ------------------------------------------------- reading order, checked by the AI
def page_blocks(model: DocModel, pno: int) -> list[StructNode]:
    """The reading blocks printed on a page, in the order they are read."""
    out = [n for n in model.ordered_nodes()
           if not n.artifact and is_block(model, n) and n.rects_on(pno)]
    # a Sect / Index whose own blocks are listed is not a block of its own here
    listed = {n.id for n in out}
    return [n for n in out if not any(c in listed for c in n.children)]


def numbered_page_png(working_pdf: str, pno: int, blocks: list[StructNode], longest: int = 1400) -> bytes:
    """The page with a red box and number on each block, as a PNG."""
    src = pymupdf.open(working_pdf)
    out = pymupdf.open()
    try:
        page = src[pno]
        w, h = page.rect.width, page.rect.height
        np_ = out.new_page(width=w, height=h)
        np_.show_pdf_page(np_.rect, src, pno)
        for k, n in enumerate(blocks, start=1):
            rects = [pymupdf.Rect(pdf_to_display(page, r)) for r in n.rects_on(pno)]
            for r in rects:
                np_.draw_rect(r, color=(0.85, 0.1, 0.1), width=1.3)
            if rects:
                r0 = rects[0]
                fs = max(8.0, min(14.0, 0.022 * max(w, h)))
                tw = fs * (0.7 + 0.6 * len(str(k)))
                x0 = r0.x0 - tw if r0.x0 - tw >= 0 else r0.x0      # beside the box, not over its words
                tag = pymupdf.Rect(x0, r0.y0, x0 + tw, r0.y0 + fs * 1.25)
                np_.draw_rect(tag, color=(0.85, 0.1, 0.1), fill=(0.85, 0.1, 0.1))
                np_.insert_text((tag.x0 + 0.25 * fs, tag.y1 - 0.3 * fs), str(k), fontsize=fs,
                                fontname="hebo", color=(1, 1, 1))
        zoom = min(2.5, longest / max(w, h))
        return np_.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).tobytes("png")
    finally:
        out.close()
        src.close()


def ai_reading_order(model: DocModel, working_pdf: str, pno: int, ai_cfg) -> dict:
    """Ask the vision model whether the page reads in the right order.
    -> {page, blocks, ok, same, order (block ids in the suggested order), why}"""
    from .ai import reading_order
    if not 0 <= pno < model.page_count:
        raise EditError("no such page")
    blocks = page_blocks(model, pno)
    info = [{"n": k, "id": n.id, "role": n.role,
             "text": (_node_text(model, n.id) or n.alt or "")[:80]}
            for k, n in enumerate(blocks, start=1)]
    if len(blocks) < 2:
        return {"page": pno, "blocks": info, "ok": True, "same": True,
                "order": [b["id"] for b in info], "why": "fewer than two blocks on the page"}
    png = numbered_page_png(working_pdf, pno, blocks)
    ans = reading_order(ai_cfg, png, [f"{b['role']} — {b['text']}" for b in info])
    if ans is None:
        raise EditError("the model did not give a usable order — try again, or use a larger vision model")
    order = [info[k - 1]["id"] for k in ans["order"]]
    same = order == [b["id"] for b in info]
    return {"page": pno, "blocks": info, "ok": bool(ans["ok"]) or same, "same": same,
            "order": order, "numbers": ans["order"], "why": ans.get("why", "")}


def reorder_blocks(model: DocModel, page: int, ids: list[str]) -> int:
    """Read the given blocks in this order. Blocks that share a parent swap
    places among themselves; nothing moves to another parent, so a Sect, a
    list or a table keeps what it holds."""
    ids = [i for i in ids if i in model.nodes]
    if len(ids) < 2:
        return 0
    by_parent: dict = collections.defaultdict(list)
    for i in ids:
        by_parent[model.nodes[i].parent].append(i)
    moved = 0
    for parent, group in by_parent.items():
        if len(group) < 2:
            continue
        sibs = model.root_children if parent is None else model.nodes[parent].children
        slots = sorted(sibs.index(i) for i in group)
        for slot, nid in zip(slots, group):
            if sibs[slot] != nid:
                moved += 1
            sibs[slot] = nid
    return moved


# ------------------------------------------------- delete what is in a zone (V4.19.3)
def delete_in_zone(model: DocModel, working_pdf: str, page_no: int, rect_display) -> dict:
    """Every tag printed wholly inside a zone drawn on the page goes (with
    what is inside it): paragraphs, figures, artifacts, a whole table or list
    when all of it is in the zone, cells and items when only they are. Tags
    that reach outside the zone, or onto another page, stay. Containers left
    empty go too; a table that lost cells has its rows put right."""
    doc = pymupdf.open(working_pdf)
    try:
        if not 0 <= page_no < doc.page_count:
            raise EditError("no such page")
        box = display_to_pdf(doc[page_no], rect_display)
    finally:
        doc.close()
    x0, y0, x1, y1 = box
    if (x1 - x0) < 2 or (y1 - y0) < 2:
        raise EditError("drag a box over the tags to delete")

    def inside(r) -> bool:
        cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
        return x0 - 1 <= cx <= x1 + 1 and y0 - 1 <= cy <= y1 + 1

    memo: dict[str, bool | None] = {}

    def all_in(n: StructNode) -> bool | None:
        """True: everything of the tag is printed in the zone; False: some of
        it is outside; None: nothing of it is printed anywhere."""
        if n.id in memo:
            return memo[n.id]
        seen = False
        ok = True
        own = ([(n.page, n.bbox)] if n.bbox is not None else []) + [(int(p[0]), tuple(p[1:5])) for p in n.parts]
        for p, r in own:
            seen = True
            if p != page_no or not inside(r):
                ok = False
        for c in n.children:
            k = model.nodes.get(c)
            if k is None:
                continue
            v = all_in(k)
            if v is None:
                continue
            seen = True
            if not v:
                ok = False
        memo[n.id] = (ok if seen else None)
        return memo[n.id]

    gone: list[str] = []
    tables: set[str] = set()
    for n in model.ordered_nodes():
        if n.id not in model.nodes:
            continue
        if all_in(n) is not True:
            continue
        if any(a.id in gone for a in model.ancestors(n.id)):
            continue
        t = next((a for a in model.ancestors(n.id) if a.role == "Table"), None)
        if t is not None:
            tables.add(t.id)
        gone.append(n.id)
    for nid in gone:
        if nid in model.nodes:
            model.remove(nid, promote_children=False)
    # containers left with nothing in them
    changed = True
    while changed:
        changed = False
        for n in list(model.nodes.values()):
            if n.id in model.nodes and n.role in CONTAINER_ROLES | {"LBody", "Reference", "Caption"} \
                    and not n.children and n.bbox is None and not n.parts and not n.src_keys:
                model.remove(n.id)
                changed = True
    for tid in tables:
        if tid in model.nodes:
            try:
                from .tableedit import Grid
                Grid(model, tid).write()
            except Exception:
                pass
    return {"ok": True, "deleted": len(gone), "ids": gone}
