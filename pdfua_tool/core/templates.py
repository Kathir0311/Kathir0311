"""Templates (V4.19): a table or a list made right by hand is marked with a
number; another table or list (or a zone drawn on the page) is then given the
same structure in one click.

Table template: how many columns and where their lines sit (as shares of the
table's width), how rows are found (ruled lines, one per printed line, or a
new row where the first column starts), header rows and columns, merged
header cells, row groups. Applied to a table, the table's zone on each page
is read again that way: column lines are moved onto the white space nearest
to the template's place (or onto the drawn rules), rows are cut, cells read
their words, headers are set.

List template (core/listedit.py): the label shape and numbering of each
level, nesting, items of one line or several.

Templates are kept for the whole installation (config.json "templates"), so
a team marks a structure once and uses it in every file."""
from __future__ import annotations

import re

import pymupdf

from .model import DocModel, StructNode, flow_position
from .tableedit import Grid, TableEditError, _clusters, _disp, _lines, _pdf
from .zonetools import bands, gaps, h_rules, lines_in, remove_tags_inside, ubox, v_rules, zone_of


# ------------------------------------------------------------------- tables
def table_spec(model: DocModel, working_pdf: str, tid: str) -> dict:
    g = Grid(model, tid)
    if not g.cells:
        raise TableEditError("the table has no cells")
    doc = pymupdf.open(working_pdf)
    try:
        lines = _lines(g, doc)
        parts = [(int(p), part) for p, v in lines.items() for part in v["parts"]]
        if not parts:
            raise TableEditError("the table has no zones on the page")
        pno, main = max(parts, key=lambda t: len(t[1]["cells"]))
        b = main["box"]
        w = max(1.0, b[2] - b[0])
        cols = {int(k): float(v) for k, v in main["cols"].items()}
        fr = []
        for k in range(1, g.C):
            if k in cols:
                fr.append(round((cols[k] - b[0]) / w, 4))
            else:
                fr.append(round(k / g.C, 4))
        # header rows / columns: the table's own setting, else the TH rows on top
        st = g.t.style or {}
        hr = st.get("header_rows")
        hc = st.get("header_cols")
        if hr is None:
            hr = 0
            for r in range(g.R):
                cs = [c for c, v in g.cells.items() if v[0] == r]
                if cs and all(g.node(c).role == "TH" for c in cs):
                    hr += 1
                else:
                    break
        if hc is None:
            hc = 0
        # how rows are made: count printed lines per row in the main part
        page = doc[pno]
        rows = {int(k): float(v) for k, v in main["rows"].items()}
        ks = sorted(rows)
        per_row = []
        header_bands = []
        lines_all = lines_in(page, b)
        for a, c in zip(ks, ks[1:]):
            n = len(bands([l for l in lines_all if rows[a] - 1 <= l.cy <= rows[c] + 1]))
            if a < hr:
                header_bands.append(max(1, n))
            elif n:
                per_row.append(n)
        rules = h_rules(page, b)
        inner = [rows[k] for k in ks[1:-1]]
        ruled = bool(inner) and sum(1 for y in inner if any(abs(y - r) < 3.5 for r in rules)) >= 0.7 * len(inner)
        avg = sum(per_row) / max(1, len(per_row))
        mode = "ruled" if ruled else ("lines" if avg <= 1.15 else "first_col")
        spans = [[v[0], v[1], v[3]] for c, v in g.cells.items() if v[0] < hr and v[3] > 1]
        grouped = any(model.nodes[c].role in ("THead", "TBody", "TFoot") for c in g.t.children if c in model.nodes)
        ids = any(g.node(c).headers for c in g.cells)
        return {"kind": "table", "cols": g.C, "col_fracs": fr, "row_mode": mode,
                "header_rows": int(hr or 0), "header_cols": int(hc or 0), "header_bands": header_bands,
                "header_spans": spans, "grouped": grouped, "headers_mode": "ids" if ids else "auto",
                "lines_per_row": round(avg, 2), "rows_seen": g.R}
    finally:
        doc.close()


def _cover(lines, x0: float, x1: float) -> list[int]:
    """How many characters cover each point from x0 to x1."""
    n = max(1, int(x1 - x0) + 1)
    cov = [0] * n
    for l in lines:
        for ch, b in l.chars:
            if not ch.strip():
                continue
            a = max(0, int(b[0] - x0))
            z = min(n - 1, int(b[2] - x0))
            for i in range(a, z + 1):
                cov[i] += 1
    return cov


def _columns(page, zone, lines, spec) -> list[float]:
    """Column lines over the zone: the template's places, moved onto a drawn
    rule or onto the white space nearest to them."""
    x0, x1 = zone[0], zone[2]
    w = x1 - x0
    want = [x0 + f * w for f in spec["col_fracs"]]
    rules = [x for x in v_rules(page, zone) if x0 + 4 < x < x1 - 4]
    body = lines
    hb = sum(spec.get("header_bands") or [])
    if hb:
        bs = bands(lines)
        skip = {id(l) for b in bs[:hb] for l in b}
        body = [l for l in lines if id(l) not in skip] or lines
    cov = _cover(body, x0, x1)
    out = []
    for t in want:
        near = [x for x in rules if abs(x - t) < 0.12 * w]
        if near:
            out.append(min(near, key=lambda x: abs(x - t)))
            continue
        lo = max(0, int(t - x0 - 0.15 * w))
        hi = min(len(cov) - 1, int(t - x0 + 0.15 * w))
        if hi <= lo:
            out.append(t)
            continue
        best = min(range(lo, hi + 1), key=lambda i: (cov[i], abs(i - (t - x0))))
        # the middle of that white stripe
        a = best
        while a > lo and cov[a - 1] == cov[best]:
            a -= 1
        z = best
        while z < hi and cov[z + 1] == cov[best]:
            z += 1
        out.append(x0 + (a + z) / 2)
    xs = [x0]
    for x in sorted(out):
        xs.append(max(x, xs[-1] + 6))
    xs.append(max(x1, xs[-1] + 6))
    return xs


def _rows(page, zone, lines, spec, colx, first_part: bool) -> list[tuple[float, float, int]]:
    """Row bands (y0, y1, printed lines) over the zone, the template's way."""
    y0, y1 = zone[1], zone[3]
    bs = bands(lines)
    if not bs:
        return [(y0, y1, 0)]
    mode = spec.get("row_mode", "lines")
    if mode == "ruled":
        rules = [y for y in h_rules(page, zone) if y0 + 3 < y < y1 - 3]
        if rules:
            ys = [y0] + rules + [y1]
            return [(a, b, 1) for a, b in zip(ys, ys[1:]) if b - a > 3]
        mode = "lines"
    groups: list[list] = []
    hb = list(spec.get("header_bands") or []) if first_part else []
    i = 0
    for n in hb:                     # header rows: as many printed lines as the template's
        if i >= len(bs):
            break
        groups.append(bs[i:i + n])
        i += n
    for b in bs[i:]:
        if mode == "first_col" and groups and len(groups) > len(hb) - 0 and \
                not any(l.box[0] < colx[1] - 2 for l in b):
            groups[-1].append(b)
        else:
            groups.append([b])
    tops = [min(l.box[1] for b in gr for l in b) for gr in groups]
    bots = [max(l.box[3] for b in gr for l in b) for gr in groups]
    out = []
    for k in range(len(groups)):
        a = y0 if k == 0 else (bots[k - 1] + tops[k]) / 2
        z = y1 if k == len(groups) - 1 else (bots[k] + tops[k + 1]) / 2
        out.append((a, z, len(groups[k])))
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def apply_table_spec(model: DocModel, working_pdf: str, spec: dict, tid: str | None = None,
                     page: int | None = None, rect=None, opts=None) -> dict:
    """Give a table (or a zone drawn on the page) the template's structure."""
    from .edit import table_headers
    from .repair import reread_cells
    if spec.get("kind") != "table":
        raise TableEditError("that template is not a table template")
    doc = pymupdf.open(working_pdf)
    try:
        zones: list[tuple[int, list]] = []
        if tid:
            t = model.nodes.get(tid)
            if t is None or t.role != "Table":
                raise TableEditError("not a table")
            g = Grid(model, tid)
            lines = _lines(g, doc)
            for p in sorted(lines, key=int):
                for part in lines[p]["parts"]:
                    zones.append((int(p), list(part["box"])))
            if not zones:
                for p, r in ([(t.page, t.bbox)] if t.bbox else []) + [(int(q[0]), q[1:5]) for q in t.parts]:
                    zones.append((p, _disp(doc[p], r)))
            if not zones:
                raise TableEditError("the table has no zone on the page — draw one")
            # the old rows go (a caption or anything else in the table stays)
            for c in list(t.children):
                n = model.nodes.get(c)
                if n is not None and n.role in ("TR", "THead", "TBody", "TFoot"):
                    t.children.remove(c)
                    model._drop_subtree(c)
        else:
            if page is None or rect is None:
                raise TableEditError("pick a table or draw a zone")
            zones = [(int(page), [float(v) for v in rect])]
            zp = _pdf(doc[page], rect)
            remove_tags_inside(model, page, zp, set())
            parent, at = flow_position(model, page, zp)
            t = StructNode(id=model.new_id("table"), role="Table", page=page, bbox=zp, source="user",
                           note=f"table made with template {spec.get('number', '')}".strip())
            model.add(t, parent, at)
        C = int(spec["cols"])
        hr = int(spec.get("header_rows") or 0)
        rows_out: list[list] = []          # [page, y0, y1, colx]
        first_band_text = None
        repeated = []
        for i, (p, z) in enumerate(zones):
            pg = doc[p]
            ls = lines_in(pg, z)
            colx = _columns(pg, z, ls, spec)
            rs = _rows(pg, z, ls, spec, colx, i == 0)
            for j, (y0, y1, n) in enumerate(rs):
                txt = _norm(" ".join(l.text for l in ls if y0 <= l.cy <= y1))
                if i == 0 and j == 0:
                    first_band_text = txt
                elif i > 0 and j < max(1, hr) and hr and txt and txt == first_band_text:
                    repeated.append((p, y0, y1, colx))
                    continue
                rows_out.append([p, y0, y1, colx])
        body_host = t
        if spec.get("grouped"):
            head = StructNode(id=model.new_id("thead"), role="THead", page=zones[0][0], source="user") if hr else None
            body = StructNode(id=model.new_id("tbody"), role="TBody", page=zones[0][0], source="user")
            if head is not None:
                model.add(head, t.id)
            model.add(body, t.id)
            body_host = body
        new_cells = []
        spans = {(r, c): cs for r, c, cs in (spec.get("header_spans") or []) if int(spec["cols"]) == C}
        for r, (p, y0, y1, colx) in enumerate(rows_out):
            host = body_host
            if spec.get("grouped") and r < hr:
                host = next(model.nodes[c] for c in t.children if model.nodes[c].role == "THead")
            tr = StructNode(id=model.new_id("tr"), role="TR", page=p, source="user")
            model.add(tr, host.id)
            c = 0
            while c < C:
                cs = spans.get((r, c), 1) if r < hr else 1
                cs = max(1, min(cs, C - c))
                cell = StructNode(id=model.new_id("td"), role="TH" if r < hr else "TD", page=p,
                                  bbox=_pdf(doc[p], [colx[c], y0, colx[c + cs], y1]), source="user", priority=1,
                                  colspan=cs)
                model.add(cell, tr.id)
                new_cells.append(cell.id)
                c += cs
        if repeated:
            from .zonetools import place_after
            art = StructNode(id=model.new_id("art"), role="Div", page=repeated[0][0], artifact=True,
                             artifact_subtype="Pagination", source="user",
                             note="header repeated where the table continues")
            for p, y0, y1, colx in repeated:
                art_p = StructNode(id=model.new_id("p"), role="P", page=p, artifact=True,
                                   bbox=_pdf(doc[p], [colx[0], y0, colx[-1], y1]), source="user", priority=1)
                model.nodes[art_p.id] = art_p
                art.children.append(art_p.id)
                art_p.parent = art.id
            place_after(model, t, [art])
            model.nodes[art.id] = art
    finally:
        doc.close()
    reread_cells(model, working_pdf, opts, [t.id], only=set(new_cells))
    t.style = dict(t.style or {}, header_rows=hr, header_cols=int(spec.get("header_cols") or 0))
    if hr or spec.get("header_cols"):
        table_headers(model, t.id, hr, int(spec.get("header_cols") or 0), spec.get("headers_mode") or "auto")
    zone_of(model, t)
    t.source = "user"
    t.confidence = 1.0
    if spec.get("number"):
        t.note = (t.note + "; " if t.note else "") + f"structure from template {spec['number']}"
    return {"ok": True, "id": t.id, "rows": len(rows_out), "cols": C, "repeated_headers": len(repeated)}


# ------------------------------------------------------------------- storage
def store(cfg: dict) -> dict:
    t = cfg.setdefault("templates", {})
    t.setdefault("table", {})
    t.setdefault("list", {})
    return t


def listing(cfg: dict) -> dict:
    t = store(cfg)
    out = {}
    for kind in ("table", "list"):
        out[kind] = [{"number": k, "name": v.get("name", ""), "by": v.get("by", ""), "made": v.get("made", ""),
                      "summary": summary(v)} for k, v in sorted(t[kind].items(), key=lambda kv: int(kv[0]))]
    return out


def summary(spec: dict) -> str:
    if spec.get("kind") == "table":
        mode = {"ruled": "rows at the ruled lines", "lines": "a row per printed line",
                "first_col": "a new row where the first column starts"}.get(spec.get("row_mode"), "")
        return (f"{spec.get('cols')} columns · {spec.get('header_rows', 0)} header row(s)"
                f"{' · ' + str(spec.get('header_cols')) + ' header column(s)' if spec.get('header_cols') else ''}"
                f" · {mode}{' · THead/TBody' if spec.get('grouped') else ''}")
    if spec.get("kind") == "list":
        lv = spec.get("levels") or []
        shapes = ", ".join((l.get("shape") or "no label").replace("\\", "") for l in lv[:3])
        return f"{len(lv)} level(s) · labels {shapes} · {spec.get('lines_per_item', 1)} line(s) per item"
    return ""
