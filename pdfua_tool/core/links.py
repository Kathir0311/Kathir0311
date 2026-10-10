"""Links stage (V4.18): every link of a document in one place.

A link is one of three kinds, by where it came from:

* file   – a link annotation that was already in the PDF (``n.annots``)
* found  – made by the cross-reference search (``style["xref"]``), written as a
           new annotation at build
* manual – set by hand in the Links stage (``style["manual"]`` = pair id,
           ``style["xref"]`` = where it goes), kept when the text is searched
           for links again

A Link tag with neither an annotation nor a target is *dead* (PAC: "Link
element without a link annotation"): the build would write it as a Span.

What this module does: list the links with where each one goes (read back
from the file for its own annotations), check that they go to the right place
(the words of the link against what is printed where it lands), set links by
hand (a box over the words → a tag, a spot, a page, a web address or an e-mail
address; one-way or two-way), point a link elsewhere, make it two-way, take it
out, and fix what the check found.

Where a link of the file is pointed elsewhere, its annotation stays and gets a
new action at build (``model.info["annot_targets"]``); a link of the file that
is taken out leaves the built file (``model.info["drop_annots"]``)."""
from __future__ import annotations

import re

import pymupdf

from .analyzer import RAW_FLAGS, collect_lines, to_pdf
from .model import CONTAINER_ROLES, DocModel, StructNode, area, flow_position, intersection
from .options import Options


class LinkError(Exception):
    pass


WEB_RE = re.compile(r"^(?:https?://|ftp://|www\.)[^\s<>\"']+$", re.I)
MAIL_RE = re.compile(r"^(?:mailto:)?[\w.+'-]+@[\w-]+(?:\.[\w-]+)+(?:\?[^\s]*)?$", re.I)
PRINTED_WEB_RE = re.compile(r"((?:https?://|ftp://|www\.)[^\s<>\"]+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+)", re.I)
NAMED_RE = re.compile(
    r"\b(fig(?:ure)?s?\.?|tables?|boxe?s?|sections?|sect\.|chapters?|chap\.|appendix|appendices|"
    r"equations?|eq\.)\s*([0-9]+(?:\.[0-9]+)*[a-z]?|[ivxlc]+)\b", re.I)
CITE_RE = re.compile(r"^\W*(\d{1,4})(?:\s*[–—‒-]\s*\d{1,4})?\W*$")
LINK_KINDS = {1: "page", 2: "uri", 3: "launch", 4: "named", 5: "file"}


# ------------------------------------------------------------------ helpers
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _disp_matrix(page: pymupdf.Page) -> pymupdf.Matrix:
    return page.transformation_matrix * page.rotation_matrix


def _to_disp(page: pymupdf.Page, box) -> list[float]:
    r = pymupdf.Rect(box) * _disp_matrix(page)
    r.normalize()
    return [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)]


def _from_disp(page: pymupdf.Page, box) -> tuple:
    try:
        box = [float(v) for v in list(box)[:4]]
    except Exception:
        raise LinkError("invalid rectangle")
    if len(box) != 4:
        raise LinkError("invalid rectangle")
    r = pymupdf.Rect(box) * ~_disp_matrix(page)
    r.normalize()
    return (round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2))


def _top_to_disp_y(page: pymupdf.Page, top) -> float | None:
    if top is None:
        return None
    p = pymupdf.Point(0, float(top)) * _disp_matrix(page)
    return round(p.y, 2)


def _disp_y_to_top(page: pymupdf.Page, y: float) -> float:
    p = pymupdf.Point(0, float(y)) * ~_disp_matrix(page)
    return round(p.y, 2)


def _text_of(model: DocModel, n: StructNode) -> str:
    """A tag's words: its own text, or (a container) its descendants'."""
    if (n.text or "").strip():
        return _norm(n.text)
    parts: list[str] = []

    def walk(x: StructNode, depth: int = 0) -> None:
        for c in x.children:
            k = model.nodes.get(c)
            if k is None or k.artifact:
                continue
            if (k.text or "").strip():
                parts.append(k.text)
            elif depth < 30:
                walk(k, depth + 1)
    walk(n)
    return _norm(" ".join(parts))


def _label_of(model: DocModel, n: StructNode) -> str:
    t = _text_of(model, n)
    if not t and n.role == "Figure":
        t = n.alt
    return f"{n.role} “{t[:70]}”" if t else n.role


def _rects(n: StructNode) -> list[list]:
    out = []
    if n.bbox is not None and n.page is not None and n.page >= 0:
        out.append([n.page, *n.bbox])
    out += [[int(p[0]), *p[1:5]] for p in n.parts]
    return out


def _is_artifact(model: DocModel, n: StructNode) -> bool:
    return n.artifact or any(a.artifact for a in model.ancestors(n.id))


def _wants_reference(opts: Options | None) -> bool:
    from .compat import house
    return house() or bool(getattr(opts, "cross_reference_tags", False))


def _origin(n: StructNode) -> str:
    st = n.style or {}
    if n.annots:
        return "file"
    if st.get("manual"):
        return "manual"
    if isinstance(st.get("xref"), dict):
        return "found"
    return "dead"


def all_links(model: DocModel) -> list[StructNode]:
    return [n for n in model.ordered_nodes() if n.role == "Link"]


class _Pages:
    """The working file, opened once for a listing or a check."""

    def __init__(self, path: str):
        self.path = path
        self.doc = pymupdf.open(path)
        self.n = self.doc.page_count
        self._links: dict[int, dict] | None = None

    def close(self):
        self.doc.close()

    def page(self, pno: int) -> pymupdf.Page:
        return self.doc[pno]

    def annot_links(self) -> dict[int, dict]:
        """Link annotation object number -> where it goes. The action's kind
        and a web address are read from the file as written (MuPDF calls an
        address with no scheme, "info@example.org", a file to open); a page
        and the spot on it (named destinations too) come from MuPDF."""
        if self._links is None:
            self._links = {}
            for p in self.doc:
                try:
                    for lk in p.get_links():
                        x = lk.get("xref")
                        if x:
                            lk = dict(lk)
                            lk["_from_page"] = p.number
                            self._links[int(x)] = lk
                except Exception:
                    continue
            try:
                import pikepdf
                pdf = pikepdf.open(self.path)
                try:
                    for pg in pdf.pages:
                        for a in pg.obj.get("/Annots", []) or []:
                            try:
                                if not a.is_indirect or a.get("/Subtype") != pikepdf.Name.Link:
                                    continue
                                act = a.get("/A")
                                num = a.objgen[0]
                                lk = self._links.setdefault(num, {"kind": 0, "xref": num})
                                if act is None:
                                    if a.get("/Dest") is None:
                                        lk["kind"] = 0
                                    continue
                                s = str(act.get("/S", ""))
                                if s == "/URI":
                                    lk["kind"] = 2
                                    lk["uri"] = str(act.get("/URI", ""))
                                elif s == "/Launch":
                                    lk["kind"] = 3
                                elif s == "/GoToR":
                                    lk["kind"] = 5
                                    f = act.get("/F")
                                    lk["file"] = str(f.get("/F", "")) if isinstance(f, pikepdf.Dictionary) else str(f or "")
                                elif s == "/Named":
                                    lk["kind"] = 4
                                    lk["name"] = str(act.get("/N", ""))
                            except Exception:
                                continue
                finally:
                    pdf.close()
            except Exception:
                pass
        return self._links


# --------------------------------------------------------- where links go
def _target_of(model: DocModel, n: StructNode, pages: _Pages) -> dict:
    """Where a link goes: {"type": page|uri|email|named|file|launch|none, ...}
    with "page", "top" (PDF y) and "y" (display y) for a place in this file."""
    st = n.style or {}
    over = model.info.get("annot_targets") or {}
    for a in n.annots or []:
        if str(a) in over:
            return _target_from_spec(over[str(a)], pages, set_by_hand=True)
    if n.annots:
        lk = None
        for a in n.annots:
            lk = pages.annot_links().get(int(a))
            if lk is not None:
                break
        if lk is None:
            return {"type": "none", "why": "the annotation has no action the reader can follow"}
        kind = LINK_KINDS.get(lk.get("kind"), "none")
        if kind == "named" and lk.get("name") and int(lk.get("page", -1) or -1) < 0:
            return {"type": "named", "name": str(lk["name"])}
        if kind == "uri":
            u = str(lk.get("uri") or "")
            return {"type": "email" if u.lower().startswith("mailto:") else "uri", "uri": u}
        if kind in ("page", "named") and lk.get("page", -1) is not None and int(lk.get("page", -1)) >= 0:
            tp = int(lk["page"])
            if tp >= pages.n:
                return {"type": "none", "why": f"goes to page {tp + 1}, the file has {pages.n}"}
            to = lk.get("to")
            page = pages.page(tp)
            top = None
            out = {"type": "page", "page": tp}
            if to is not None and (to.y or to.x):
                # MuPDF gives an explicit destination's point in its own (top
                # down) space, a named destination's as written (PDF space)
                pt = pymupdf.Point(to) if kind == "named" else pymupdf.Point(to) * ~page.transformation_matrix
                top = round(pt.y, 2)
                if pt.x > 0:
                    dp = pt * _disp_matrix(page)
                    out["x"] = round(dp.x, 2)
            out.update({"top": top, "y": _top_to_disp_y(page, top) if top is not None else 0.0})
            return out
        if kind == "named":
            return {"type": "named", "name": str(lk.get("name") or lk.get("nameddest") or "")}
        if kind == "file":
            return {"type": "file", "file": str(lk.get("file") or "")}
        if kind == "launch":
            return {"type": "launch", "file": str(lk.get("file") or "")}
        return {"type": "none", "why": "the annotation has no action the reader can follow"}
    x = st.get("xref")
    if isinstance(x, dict):
        if x.get("uri"):
            u = str(x["uri"])
            return {"type": "email" if u.lower().startswith("mailto:") else "uri", "uri": u}
        return _target_from_spec(x, pages)
    return {"type": "none", "why": "a Link tag with no link behind it"}


def _target_from_spec(x: dict, pages: _Pages, set_by_hand: bool = False) -> dict:
    if x.get("uri"):
        u = str(x["uri"])
        return {"type": "email" if u.lower().startswith("mailto:") else "uri", "uri": u, "retargeted": set_by_hand}
    try:
        tp = int(x.get("page", -1))
    except Exception:
        tp = -1
    if not 0 <= tp < pages.n:
        return {"type": "none", "why": "goes to a page the file does not have"}
    page = pages.page(tp)
    top = x.get("top")
    out = {"type": "page", "page": tp, "top": top,
           "y": _top_to_disp_y(page, top) if top is not None else 0.0, "retargeted": set_by_hand}
    if x.get("node"):
        out["node"] = x["node"]
    return out


class _Landing:
    """What is printed where a link lands: the tag at the point, or the first
    one below it."""

    def __init__(self, model: DocModel, pages: _Pages):
        self.m = model
        self.pages = pages
        self.by_page: dict[int, list] = {}
        self.raw: dict[int, list] | None = None

    def _page(self, pno: int) -> list:
        if self.raw is None:
            # one pass over the tags: their zones page by page (PDF space)
            self.raw = {}
            for n in self.m.nodes.values():
                if n.role in CONTAINER_ROLES or n.artifact or n.role in ("Link", "Reference", "Lbl", "Span"):
                    continue
                if n.bbox is not None and n.page is not None and n.page >= 0:
                    self.raw.setdefault(n.page, []).append((n.bbox, n))
                for p in n.parts:
                    self.raw.setdefault(int(p[0]), []).append((tuple(p[1:5]), n))
        if pno not in self.by_page:
            page = self.pages.page(pno)
            self.by_page[pno] = [(_to_disp(page, r), n) for r, n in self.raw.get(pno, [])]
        return self.by_page[pno]

    def at(self, tgt: dict) -> StructNode | None:
        if tgt.get("type") != "page":
            return None
        if tgt.get("node") and tgt["node"] in self.m.nodes:
            return self.m.nodes[tgt["node"]]
        y = tgt.get("y") or 0.0
        cands = self._page(tgt["page"])
        x = tgt.get("x")
        if x is not None:
            # the destination names its column too (two-column pages)
            col = [(d, n) for d, n in cands if d[2] >= x and d[0] <= x + 30]
            if col:
                cands = col
        inside = [(d, n) for d, n in cands if d[1] - 4 <= y <= d[3] + 2]
        if inside:
            return min(inside, key=lambda t: (t[0][2] - t[0][0]) * (t[0][3] - t[0][1]))[1]
        below = [(d[1] - y, n) for d, n in cands if d[1] >= y and d[1] - y < 90]
        if below:
            return min(below, key=lambda t: t[0])[1]
        if not tgt.get("top") and cands:
            # the top of the page: its first tag
            return min(cands, key=lambda t: (t[0][1], t[0][0]))[1]
        return None


def _targets(model: DocModel, path: str):
    """What the cross-reference search can point at ("figure 3", "ref 12")."""
    try:
        from .xref import collect_targets
        doc = pymupdf.open(path)
        try:
            t, _ = collect_targets(model, None, doc)
        finally:
            doc.close()
        return t
    except Exception:
        return {}


def _expected_key(text: str, origin: str, x: dict) -> str:
    """The cross-reference a link's words name ("Figure 3" -> "figure 3",
    a citation "12" -> "ref 12")."""
    from .xref import _key
    if x.get("key") and x.get("kind") not in ("manual", "manual_back", "web", "email"):
        return str(x["key"])
    t = _norm(text)
    m = NAMED_RE.search(t)
    if m and len(t) < 40:
        return _key(m.group(1), m.group(2))
    c = CITE_RE.match(t)
    if c and len(t) <= 14:
        return f"ref {int(c.group(1))}"
    return ""


def _same_place(model: DocModel, landing: StructNode | None, tgt: dict, exp) -> bool:
    """The link lands where its words say (the expected target, a part of it,
    or a tag holding it)."""
    if exp is None:
        return True
    if landing is not None and exp.node:
        if landing.id == exp.node:
            return True
        if exp.node in model.nodes and (any(a.id == exp.node for a in model.ancestors(landing.id))
                                        or any(d.id == landing.id for d in model.descendants(exp.node))):
            return True
    if tgt.get("page") == exp.page and tgt.get("top") is not None \
            and abs(float(tgt["top"]) - float(exp.top)) < 40:
        return True
    return False


# ------------------------------------------------------------- the listing
def list_links(model: DocModel, working_pdf: str, check: bool = True) -> dict:
    """Every Link tag: where it is, what it says, where it goes and, with
    `check`, whether it goes to the right place."""
    pages = _Pages(working_pdf)
    try:
        land = _Landing(model, pages)
        exp_targets = _targets(model, working_pdf) if check else {}
        out = []
        pairs: dict[str, list] = {}
        boxes: dict[int, list] = {}
        for n in all_links(model):
            st = n.style or {}
            if st.get("manual"):
                pairs.setdefault(st["manual"], []).append(n)
        for n in all_links(model):
            st = n.style or {}
            x = st.get("xref") if isinstance(st.get("xref"), dict) else {}
            origin = _origin(n)
            tgt = _target_of(model, n, pages)
            rects = []
            for r in _rects(n):
                if 0 <= r[0] < pages.n:
                    rects.append([r[0], *_to_disp(pages.page(r[0]), r[1:5])])
            text = _text_of(model, n)
            landing = land.at(tgt) if tgt.get("type") == "page" else None
            if landing is not None:
                tgt["label"] = _label_of(model, landing)
                tgt["node"] = tgt.get("node") or landing.id
                tgt["lands_on"] = landing.id
            elif tgt.get("type") == "page":
                tgt["label"] = f"page {tgt['page'] + 1}" + (" (nothing printed there)" if tgt.get("top") else "")
            elif tgt.get("type") in ("uri", "email"):
                tgt["label"] = tgt.get("uri", "")
            kind = x.get("kind") or ("web" if tgt.get("type") == "uri" else
                                     "email" if tgt.get("type") == "email" else
                                     "file link" if origin == "file" else "")
            pair = st.get("manual") or ""
            item = {"id": n.id, "page": n.page, "rects": rects, "text": text[:160],
                    "origin": origin, "kind": kind, "target": tgt,
                    "contents": str(st.get("contents") or n.alt or ""),
                    "pair": pair, "back": x.get("kind") == "manual_back",
                    "two_way": bool(pair) and len(pairs.get(pair, [])) > 1,
                    "parent_role": model.nodes[n.parent].role if n.parent in model.nodes else "",
                    "problems": []}
            if check:
                item["problems"] = _problems(model, n, item, tgt, landing, exp_targets, pairs, x)
                for r in rects:
                    boxes.setdefault(r[0], []).append((r[1:5], n.id))
            lv = [p["level"] for p in item["problems"]]
            item["status"] = "error" if "error" in lv else "warning" if "warning" in lv else "ok"
            out.append(item)
        if check:
            _duplicates(out, boxes)
        counts = {"total": len(out), "error": 0, "warning": 0, "ok": 0,
                  "file": 0, "found": 0, "manual": 0, "dead": 0, "web": 0, "internal": 0, "fixable": 0}
        for it in out:
            counts[it["status"]] += 1
            counts[it["origin"]] += 1
            if it["target"].get("type") in ("uri", "email"):
                counts["web"] += 1
            elif it["target"].get("type") == "page":
                counts["internal"] += 1
            if any(p.get("fix") for p in it["problems"]):
                counts["fixable"] += 1
        return {"links": out, "counts": counts, "pages": pages.n}
    finally:
        pages.close()


def _problems(model, n, item, tgt, landing, exp_targets, pairs, x) -> list[dict]:
    out = []

    def add(level, code, msg, fix=None, label=""):
        p = {"level": level, "code": code, "msg": msg}
        if fix:
            p["fix"] = fix
            p["fix_label"] = label
        out.append(p)

    text = item["text"]
    t = tgt.get("type")
    if item["origin"] == "dead":
        add("error", "dead", "A Link tag with no link behind it — the build writes it as a Span "
            "(PAC: Link element without a link annotation). Set where it goes, or take the link out.",
            "remove", "Make it plain text")
    elif t == "none":
        add("error", "no_target", "Goes nowhere: " + (tgt.get("why") or "no destination") + ".",
            "remove", "Take the link out")
    elif t == "file":
        add("warning", "other_file", f"Opens another file ({tgt.get('file') or 'unknown'}) — "
            "it is not part of this PDF and may not exist for the reader.")
    elif t == "launch":
        add("error", "launch", "Starts a program or opens a file — not allowed in an accessible PDF.",
            "remove", "Take the link out")
    elif t == "named":
        add("warning", "named", f"A named action ({tgt.get('name') or '?'}) — check it in Acrobat.")
    if t in ("uri", "email"):
        u = (tgt.get("uri") or "").strip()
        bare = u[7:] if u.lower().startswith("mailto:") else u
        if not u:
            add("error", "empty_uri", "The web address is empty.", "remove", "Take the link out")
        elif " " in u.strip():
            add("error", "bad_uri", f"The address has spaces in it ({u[:60]}).",
                "trim_uri", "Remove the spaces")
        elif MAIL_RE.match(u) and not u.lower().startswith("mailto:"):
            add("warning", "mailto", "An e-mail address without “mailto:” — the reader opens it as a web page.",
                "mailto", "Add mailto:")
        elif not (WEB_RE.match(u) or MAIL_RE.match(bare) or re.match(r"^(?:https?|ftp|mailto|doi|urn):", u, re.I)):
            add("warning", "odd_uri", f"“{u[:60]}” does not look like a web or e-mail address.")
        printed = PRINTED_WEB_RE.search(text or "")
        if printed and u:
            pw = printed.group(1).rstrip(".,;)").lower()
            pw_core = re.sub(r"^(https?://|ftp://|mailto:)?(www\.)?", "", pw).rstrip("/")
            u_core = re.sub(r"^(https?://|ftp://|mailto:)?(www\.)?", "", u.lower()).rstrip("/")
            if pw_core and pw_core.split("/")[0] != u_core.split("/")[0]:
                add("warning", "printed_differs",
                    f"The words say “{printed.group(1)[:60]}” but the link opens “{u[:60]}”.",
                    "use_printed", "Open what is printed")
    if not (text or "").strip() and not item.get("contents"):
        add("warning", "no_words", "There are no words in the link zone — a screen reader has "
            "nothing to say for it. Draw the zone over the words, or give it a description.")
    if _is_artifact(model, n):
        add("warning", "artifact", "The link sits in an artifact — screen readers never reach it.")
    if t == "page":
        if landing is not None:
            # lands on the link or on the tag it is printed in
            same_spot = landing.id == n.id or any(a.id == landing.id for a in model.ancestors(n.id))
        else:
            same_spot = (tgt["page"] == n.page and n.bbox is not None and tgt.get("top") is not None
                         and n.bbox[1] - 4 <= float(tgt["top"]) <= n.bbox[3] + 8 and tgt.get("x") is None)
        if same_spot:
            add("warning", "self", "Lands on the link itself.")
        if landing is None and tgt.get("top") is not None:
            add("warning", "lands_empty", f"Lands on an empty part of page {tgt['page'] + 1}.",
                "snap", "Point it at the nearest tag")
        key = _expected_key(text, item["origin"], x)
        exp = exp_targets.get(key) if key else None
        if exp is not None and not _same_place(model, landing, tgt, exp):
            add("warning", "mismatch",
                f"The words say “{text[:40]}” but it lands on {tgt.get('label') or 'page ' + str(tgt['page'] + 1)} "
                f"(p{tgt['page'] + 1}); “{key}” is on page {exp.page + 1}.",
                {"action": "key", "key": key}, f"Point it at {key}")
        elif key and exp is None and key.startswith(("figure", "table")) and landing is not None \
                and not re.search(re.escape(key.split()[1]), _text_of(model, landing) or "", re.I):
            add("info", "unchecked", f"No “{key}” found in the document to check this link against.")
    pair = item.get("pair")
    if pair:
        halves = pairs.get(pair, [])
        if (n.style or {}).get("two_way") and len(halves) < 2:
            add("warning", "back_missing", "Set as a two-way link but the way back was taken out.",
                "twoway", "Make the way back again")
    return out


def _duplicates(items: list[dict], boxes: dict[int, list]) -> None:
    by_id = {it["id"]: it for it in items}
    seen = set()
    for pno, lst in boxes.items():
        for i in range(len(lst)):
            for j in range(i + 1, len(lst)):
                (a, ia), (b, ib) = lst[i], lst[j]
                if ia == ib or (ia, ib) in seen:
                    continue
                if intersection(a, b) > 0.6 * min(area(a), area(b)) > 0:
                    seen.add((ia, ib))
                    later = by_id[ib]
                    later["problems"].append({"level": "warning", "code": "duplicate",
                                              "msg": f"Two links on the same words (also {ia}).",
                                              "fix": "remove", "fix_label": "Take this one out"})
                    if later["status"] == "ok":
                        later["status"] = "warning"


# ----------------------------------------------------------- set by hand
def _child_box(model: DocModel, n: StructNode):
    if n.bbox is not None:
        return n.page, n.bbox
    for d in model.descendants(n.id):
        if d.bbox is not None:
            return d.page, d.bbox
    return None, None


def _insert_inline(model: DocModel, owner: StructNode, node: StructNode) -> None:
    """Put `node` among the owner's children where it is printed (line by
    line, left to right)."""
    page, (x0, y0, x1, y1) = node.page, node.bbox
    at = len(owner.children)
    for k, c in enumerate(owner.children):
        cn = model.nodes.get(c)
        if cn is None:
            continue
        p, b = _child_box(model, cn)
        if b is None:
            continue
        same_line = p == page and min(b[3], y1) - max(b[1], y0) > 0.3 * min(b[3] - b[1], y1 - y0)
        later = (p > page) or (p == page and not same_line and b[3] < (y0 + y1) / 2) \
            or (same_line and b[0] > x0)
        if later:
            at = k
            break
    model.add(node, owner.id, at)


def _words_in(page, un) -> str:
    """The characters printed inside the box (their centres in it)."""
    try:
        raw = page.get_text("rawdict", clip=pymupdf.Rect(un) + (-2, -2, 2, 2), flags=RAW_FLAGS)
    except Exception:
        return ""
    out: list[str] = []
    for b in raw.get("blocks", []):
        for l in b.get("lines", []):
            row = []
            for sp in l.get("spans", []):
                for ch in sp.get("chars", []):
                    x0, y0, x1, y1 = ch["bbox"]
                    if un.contains(pymupdf.Point((x0 + x1) / 2, (y0 + y1) / 2)):
                        row.append(ch["c"])
            if "".join(row).strip():
                out.append("".join(row).strip())
    return re.sub(r"\s+", " ", " ".join(out)).strip()


def _first_line_box(doc, page_no: int, box) -> tuple | None:
    """The first words printed inside a tag's box (PDF space): where the way
    back starts when the person did not mark it."""
    page = doc[page_no]
    un = pymupdf.Rect(box) * page.transformation_matrix
    un.normalize()
    lines = [l for l in collect_lines(page)
             if un.contains(pymupdf.Point((l.x0 + l.x1) / 2, (l.y0 + l.y1) / 2)) and (l.text or "").strip()]
    if not lines:
        return None
    l = min(lines, key=lambda l: (round(l.y0, 1), l.x0))
    end = l.x1
    upto = len(l.chars)
    words = 0
    for k, c in enumerate(l.chars):
        if k and not c[0].strip() and l.chars[k - 1][0].strip():
            words += 1
            if words == 3:
                end = l.chars[k - 1][1][2]
                upto = k
                break
    said = re.sub(r"\s+", " ", "".join(c[0] for c in l.chars[:upto])).strip()
    return to_pdf(page, (l.x0, l.y0, end, l.y1)), (said or l.text)[:60]


def _owner_for(model: DocModel, page_no: int, box) -> StructNode | None:
    from .xref import _owner_index, _owner_of
    cands = [c for c in _owner_index(model).get(page_no, [])
             if (c[0] if isinstance(c, tuple) else c).role not in ("Reference", "Link")]
    return _owner_of(cands, box)


def _put_link(model: DocModel, page_no: int, boxes: list, text: str, xref: dict, pair: str,
              opts: Options | None, owner: StructNode | None = None, note: str = "",
              reference: bool | None = None, contents: str = "", two_way: bool = False) -> StructNode:
    """A Link tag (and its Reference wrapper for a cross-reference, when the
    profile wants one) placed inline where its words are printed. `boxes`:
    [(page, box_pdf)], the first one is where it starts."""
    first_page, first_box = boxes[0]
    if owner is None:
        owner = _owner_for(model, first_page, first_box)
    link = StructNode(id=model.new_id("mlink"), role="Link", page=first_page, bbox=tuple(first_box),
                      text=text, source="user", priority=1, confidence=1.0, note=note or "link set by hand")
    link.parts = [[int(p), *[float(v) for v in b]] for p, b in boxes[1:]]
    link.style = {"xref": dict(xref), "manual": pair}
    if two_way:
        link.style["two_way"] = True
    if contents:
        link.style["contents"] = contents
    web = bool(xref.get("uri"))
    if reference is None:
        reference = _wants_reference(opts) and not web
    holder = link
    if reference:
        ref = StructNode(id=model.new_id("mref"), role="Reference", page=first_page, source="user",
                         note="Reference > Link (set by hand)")
        ref.style = {"manual": pair}
        model.nodes[ref.id] = ref
        holder = ref
        model.nodes[link.id] = link
        link.parent = ref.id
        ref.children = [link.id]
    if owner is None:
        parent, idx = flow_position(model, first_page, first_box)
        model.add(holder, parent, idx)
    elif holder is link:
        _insert_inline(model, owner, link)
    else:
        ref.bbox = tuple(first_box)
        _insert_inline(model, owner, ref)
        ref.bbox = None
    return link


def _forget_removed(model: DocModel, page_no: int, box) -> None:
    """A link set again where one was taken out by hand is wanted after all."""
    gone = model.info.get("links_removed") or []
    keep = [g for g in gone if not (int(g[0]) == page_no and intersection(tuple(g[1:5]), box)
                                    > 0.4 * min(area(tuple(g[1:5])), area(box)))]
    if len(keep) != len(gone):
        model.info["links_removed"] = keep


def _clear_links_at(model: DocModel, page_no: int, box, replace_file: bool = False) -> list[int]:
    """A link the search made over the same words gives way to the one set by
    hand. A link of the file itself is an error — unless `replace_file`, then
    its annotation leaves the built file. Returns the annotations dropped."""
    dropped: list[int] = []
    for n in list(model.nodes.values()):
        if n.role != "Link" or n.id not in model.nodes:
            continue
        for r in n.rects_on(page_no):
            if intersection(box, r) > 0.4 * min(area(box), area(r)):
                if n.annots and not replace_file:
                    raise LinkError("these words are a link of the file already — point that link "
                                    "elsewhere (select it in the list), or tick “Replace a link that is there”")
                if n.annots:
                    dropped += list(n.annots)
                    _drop_annots(model, n.annots)
                _take_out(model, n)
                break
    return dropped


def _drop_annots(model: DocModel, annots) -> None:
    drop = model.info.setdefault("drop_annots", [])
    for a in annots:
        if int(a) not in drop:
            drop.append(int(a))
        for k in ("annot_targets", "annot_rects"):
            d = model.info.get(k)
            if isinstance(d, dict):
                d.pop(str(a), None)


def _take_out(model: DocModel, n: StructNode) -> None:
    """The Link tag goes; its words stay where they are (a Span when the tag
    holds content of its own)."""
    host = model.nodes.get(n.parent) if n.parent else None
    if n.src_keys:
        n.role = "Span"
        n.annots = []
        st = dict(n.style or {})
        for k in ("xref", "manual", "two_way", "contents"):
            st.pop(k, None)
        n.style = st
        n.note = (n.note + "; " if n.note else "") + "link taken out by hand"
    else:
        model.remove(n.id, promote_children=True)
    if host is not None and host.id in model.nodes and host.role == "Reference" \
            and not any(model.nodes[c].role == "Link" for c in host.children if c in model.nodes):
        model.remove(host.id, promote_children=True)


def _parse_target(model: DocModel, doc, target: dict) -> dict:
    """{"node"} | {"page", "y"} | {"page"} (its top) | {"uri"} | {"email"}
    -> an xref dict (page/top/node/label or uri)."""
    if not isinstance(target, dict):
        raise LinkError("pick where the link goes")
    if target.get("email"):
        e = str(target["email"]).strip()
        e = e[7:] if e.lower().startswith("mailto:") else e
        if not MAIL_RE.match(e):
            raise LinkError(f"“{e}” is not an e-mail address")
        return {"kind": "email", "uri": "mailto:" + e, "label": e, "key": e}
    if target.get("uri"):
        u = str(target["uri"]).strip()
        if " " in u:
            raise LinkError("a web address has no spaces")
        if MAIL_RE.match(u) and not u.lower().startswith("mailto:"):
            return {"kind": "email", "uri": "mailto:" + u, "label": u, "key": u}
        if not re.match(r"^[a-z][a-z0-9+.-]*:", u, re.I):
            u = "https://" + u
        return {"kind": "web", "uri": u, "label": u, "key": u}
    tnode = model.nodes.get(str(target.get("node") or "")) if target.get("node") else None
    if tnode is not None:
        p_b = next(((p, r) for p in sorted(tnode.pages()) for r in tnode.rects_on(p)), None)
        if p_b is None:
            p_b = _child_box(model, tnode)
            if p_b[1] is None:
                raise LinkError("that tag has no place on a page to link to")
        tpage, tbox = p_b
        label = _text_of(model, tnode)[:80] or tnode.role
        return {"kind": "manual", "page": tpage, "top": max(tbox[1], tbox[3]), "node": tnode.id,
                "label": label, "key": label}
    if target.get("page") is not None:
        try:
            tpage = int(target["page"])
        except Exception:
            raise LinkError("no such page")
        if not 0 <= tpage < doc.page_count:
            raise LinkError(f"no page {tpage + 1} — the file has {doc.page_count}")
        if target.get("y") is None:
            pg = doc[tpage]
            return {"kind": "manual", "page": tpage, "top": _disp_y_to_top(pg, 0), "node": "",
                    "label": f"page {tpage + 1}", "key": f"page {tpage + 1}"}
        pg = doc[tpage]
        return {"kind": "manual", "page": tpage, "top": _disp_y_to_top(pg, float(target["y"])), "node": "",
                "label": f"page {tpage + 1}", "key": f"page {tpage + 1}"}
    raise LinkError("pick where the link goes")


def _boxes(doc, page_no: int, rect_display, more=None) -> list:
    if not 0 <= page_no < doc.page_count:
        raise LinkError("no such page")
    out = [(page_no, _from_disp(doc[page_no], rect_display))]
    for m in more or []:
        if not m or len(m) < 5:
            continue
        p = int(m[0])
        if not 0 <= p < doc.page_count:
            continue
        out.append((p, _from_disp(doc[p], m[1:5])))
    for p, b in out:
        if area(b) < 2:
            raise LinkError("drag a box over the words that link")
    return out


def _words(doc, boxes) -> str:
    from .repair import _clean
    said = []
    for p, b in boxes:
        page = doc[p]
        un = pymupdf.Rect(b) * page.transformation_matrix
        un.normalize()
        said.append(_words_in(page, un))
    return _clean(" ".join(s for s in said if s))


def set_link(model: DocModel, working_pdf: str, page_no: int, rect_display, target: dict,
             two_way: bool = False, back_rect_display=None, opts: Options | None = None,
             more_rects=None, contents: str = "", reference: bool | None = None,
             replace: bool = False) -> dict:
    """A link set by hand: the words in the box (and `more_rects`, a link over
    two lines: [[page, x0, y0, x1, y1], …] in display space) go to `target`
    (see _parse_target). Two-way: the target links back, from the words
    marked there (`back_rect_display`) or, when none were marked, from its
    first words (a note: its Lbl). Kept when the text is searched for links
    again."""
    doc = pymupdf.open(working_pdf)
    try:
        boxes = _boxes(doc, page_no, rect_display, more_rects)
        text = _words(doc, boxes) or "link"
        x = _parse_target(model, doc, target)
        web = bool(x.get("uri"))
        if two_way and web:
            raise LinkError("a web or e-mail link cannot link back — make it one-way")
        tnode = model.nodes.get(x.get("node") or "") if x.get("node") else None
        back = None
        if two_way:
            back = _back_box(model, doc, x, tnode, back_rect_display)
    finally:
        doc.close()

    for p, b in boxes:
        _clear_links_at(model, p, b, replace_file=replace)
        _forget_removed(model, p, b)

    pair = model.new_id("pair")
    owner = _owner_for(model, boxes[0][0], boxes[0][1])
    fwd = _put_link(model, page_no, boxes, text, x, pair, opts, owner,
                    note=f"link set by hand ({'two-way' if two_way else 'one-way'})",
                    reference=reference, contents=contents, two_way=two_way)
    out = {"ok": True, "pair": pair, "id": fwd.id, "two_way": bool(two_way), "text": text}
    if two_way:
        out["back_id"] = _make_back(model, fwd, owner, x, tnode, back, pair, opts, reference).id
    return out


def _back_box(model, doc, x: dict, tnode, back_rect_display):
    """Where the way back starts: (page, box_pdf, words, host)."""
    tpage = int(x["page"])
    if back_rect_display:
        bpage = doc[tpage]
        bbox = _from_disp(bpage, back_rect_display)
        u2 = pymupdf.Rect(bbox) * bpage.transformation_matrix
        u2.normalize()
        return tpage, bbox, _words_in(bpage, u2), None
    if tnode is None:
        # a spot on a page: the tag printed there
        pages = _Pages(doc.name)
        try:
            tnode = _Landing(model, pages).at({"type": "page", "page": tpage,
                                                "y": _top_to_disp_y(doc[tpage], x.get("top")) or 0.0,
                                                "top": x.get("top")})
        finally:
            pages.close()
        if tnode is None:
            raise LinkError("nothing is printed where the link goes — the way back needs words there "
                            "(mark them with a box)")
    lbl = next((model.nodes[c] for c in tnode.children if c in model.nodes
                and model.nodes[c].role == "Lbl" and model.nodes[c].bbox is not None), None)
    if lbl is not None and tnode.role in ("Note", "LI"):
        return lbl.page, lbl.bbox, lbl.text or "", lbl
    p, tbox = _child_box(model, tnode)
    if tbox is None:
        raise LinkError("the target has no words to link back from — mark them with a box")
    got = _first_line_box(doc, p, tbox)
    if got is None:
        raise LinkError("no words found at the target to link back from — mark them with a box")
    return p, got[0], got[1], None


def _make_back(model, fwd: StructNode, owner, x: dict, tnode, back, pair: str, opts, reference) -> StructNode:
    bpage, bbox, btext, host = back
    _clear_links_at(model, bpage, bbox)
    _forget_removed(model, bpage, bbox)
    text = fwd.text or "link"
    fx = (fwd.style or {}).get("xref") or {}
    return _put_link(model, bpage, [(bpage, bbox)], btext or "back",
                     {"kind": "manual_back", "page": fwd.page, "top": max(fwd.bbox[1], fwd.bbox[3]),
                      "node": (owner.id if owner is not None else fwd.id), "label": text[:80],
                      "key": text[:80]},
                     pair, opts, host if host is not None else (_owner_for(model, bpage, bbox) or tnode),
                     note="link set by hand (two-way: the way back)", reference=reference, two_way=True)


# ------------------------------------------------------- change a link
def _link(model: DocModel, lid: str) -> StructNode:
    n = model.nodes.get(lid)
    if n is None or n.role != "Link":
        raise LinkError("no such link")
    return n


def retarget(model: DocModel, working_pdf: str, lid: str, target: dict, opts: Options | None = None) -> dict:
    """Point a link somewhere else. A link of the file keeps its annotation (a
    new action at build); a link the search made becomes one set by hand (the
    next search keeps it)."""
    n = _link(model, lid)
    doc = pymupdf.open(working_pdf)
    try:
        x = _parse_target(model, doc, target)
    finally:
        doc.close()
    st = dict(n.style or {})
    if n.annots:
        over = model.info.setdefault("annot_targets", {})
        spec = {"uri": x["uri"]} if x.get("uri") else {"page": x["page"], "top": x.get("top"),
                                                       "node": x.get("node", "")}
        for a in n.annots:
            over[str(a)] = dict(spec)
        st["retargeted"] = True
    else:
        old = st.get("xref") if isinstance(st.get("xref"), dict) else {}
        if old.get("kind") == "manual_back" and not x.get("uri"):
            x["kind"] = "manual_back"
        st["xref"] = x
        st["manual"] = st.get("manual") or model.new_id("pair")
    n.style = st
    _reference_for(model, n, opts)
    return {"ok": True, "id": n.id}


def _reference_for(model: DocModel, n: StructNode, opts) -> None:
    """A web or e-mail link is a plain Link (Kriyadocs, V4.17); a
    cross-reference sits in a Reference when the profile wants one."""
    from .compat import house
    st = n.style or {}
    x = st.get("xref") if isinstance(st.get("xref"), dict) else {}
    over = model.info.get("annot_targets") or {}
    web = bool(x.get("uri")) or any((over.get(str(a)) or {}).get("uri") for a in n.annots)
    par = model.nodes.get(n.parent) if n.parent else None
    if web and house() and par is not None and par.role == "Reference" \
            and [c for c in par.children if c in model.nodes] == [n.id]:
        model.remove(par.id, promote_children=True)
    elif not web and _wants_reference(opts) and (par is None or par.role != "Reference") \
            and not any(a.role in ("TOC", "TOCI", "Index") for a in model.ancestors(n.id)):
        sib = model.siblings(n.id)
        at = sib.index(n.id)
        ref = StructNode(id=model.new_id("mref"), role="Reference", page=n.page, source="user",
                         note="Reference > Link (set by hand)")
        ref.style = {"manual": st.get("manual") or ""}
        sib[at] = ref.id
        model.nodes[ref.id] = ref
        ref.parent = n.parent
        n.parent = ref.id
        ref.children.append(n.id)


def set_contents(model: DocModel, lid: str, contents: str) -> dict:
    """What a screen reader says for the link (the annotation's /Contents)."""
    n = _link(model, lid)
    st = dict(n.style or {})
    c = _norm(contents)[:300]
    if c:
        st["contents"] = c
    else:
        st.pop("contents", None)
    n.style = st
    return {"ok": True}


def set_zone(model: DocModel, working_pdf: str, lid: str, rect_display, page_no: int | None = None) -> dict:
    """Draw the link's zone again (its words: a box over them)."""
    n = _link(model, lid)
    doc = pymupdf.open(working_pdf)
    try:
        p = n.page if page_no is None else int(page_no)
        boxes = _boxes(doc, p, rect_display)
        text = _words(doc, boxes)
    finally:
        doc.close()
    if n.annots:
        model.info.setdefault("annot_rects", {})
        for a in n.annots[:1]:
            model.info["annot_rects"][str(a)] = list(boxes[0][1])
        if len(n.annots) > 1:
            _drop_annots(model, n.annots[1:])
            n.annots = n.annots[:1]
    n.page, n.bbox, n.parts = p, boxes[0][1], []
    if text:
        n.text = text
    n.priority = 1
    return {"ok": True, "text": text}


def make_two_way(model: DocModel, working_pdf: str, lid: str, back_rect_display=None,
                 opts: Options | None = None) -> dict:
    """The place a link goes to links back to the link."""
    n = _link(model, lid)
    pages = _Pages(working_pdf)
    try:
        tgt = _target_of(model, n, pages)
    finally:
        pages.close()
    if tgt.get("type") != "page":
        raise LinkError("only a link to a place in this document can link back")
    st = dict(n.style or {})
    pair = st.get("manual") or model.new_id("pair")
    for m in model.nodes.values():
        if m.role == "Link" and (m.style or {}).get("manual") == pair \
                and ((m.style or {}).get("xref") or {}).get("kind") == "manual_back":
            raise LinkError("this link is two-way already")
    st["manual"] = pair
    st["two_way"] = True
    if not n.annots:
        x = dict(st.get("xref") or {})
        if x.get("kind") not in ("manual", "manual_back"):
            x["kind"] = "manual"
        st["xref"] = x
    n.style = st
    x = {"page": tgt["page"], "top": tgt.get("top")}
    tnode = model.nodes.get(tgt.get("node") or "") if tgt.get("node") else None
    doc = pymupdf.open(working_pdf)
    try:
        back = _back_box(model, doc, x, tnode, back_rect_display)
    finally:
        doc.close()
    owner = model.nodes.get(n.parent) if n.parent else None
    while owner is not None and owner.role in ("Reference", "Link", "Span"):
        owner = model.nodes.get(owner.parent) if owner.parent else None
    b = _make_back(model, n, owner, x, tnode, back, pair, opts, None)
    return {"ok": True, "back_id": b.id, "pair": pair}


def remove_link(model: DocModel, lid: str, both: bool = True) -> dict:
    """Take a link out. Its words stay. A link of the file leaves the built
    file; a link the search made stays out when the text is searched again;
    `both`: the other half of a two-way link goes too."""
    n = _link(model, lid)
    gone = [n]
    pair = (n.style or {}).get("manual")
    if both and pair:
        gone += [m for m in model.nodes.values() if m.role == "Link" and m.id != n.id
                 and (m.style or {}).get("manual") == pair]
    removed = model.info.setdefault("links_removed", [])
    for m in gone:
        if m.annots:
            _drop_annots(model, m.annots)
        for r in _rects(m):
            removed.append([int(r[0]), *[round(float(v), 2) for v in r[1:5]]])
        if m.id in model.nodes:
            _take_out(model, m)
    return {"ok": True, "removed": len(gone)}


# ------------------------------------------------------------------ fixes
def fix_problem(model: DocModel, working_pdf: str, lid: str, fix, opts: Options | None = None) -> dict:
    n = _link(model, lid)
    action = fix.get("action") if isinstance(fix, dict) else str(fix or "")
    if action == "remove":
        return remove_link(model, lid, both=False)
    if action in ("mailto", "trim_uri", "use_printed"):
        pages = _Pages(working_pdf)
        try:
            tgt = _target_of(model, n, pages)
        finally:
            pages.close()
        u = (tgt.get("uri") or "").strip()
        if action == "mailto":
            u = "mailto:" + u
        elif action == "trim_uri":
            u = re.sub(r"\s+", "", u)
        else:
            m = PRINTED_WEB_RE.search(_text_of(model, n))
            if not m:
                raise LinkError("no address printed in the link")
            u = m.group(1).rstrip(".,;)")
            if "@" in u and not u.lower().startswith(("http", "mailto:")) and "/" not in u:
                u = "mailto:" + u
            elif not re.match(r"^[a-z]+:", u, re.I):
                u = "https://" + u
        return retarget(model, working_pdf, lid, {"uri": u}, opts)
    if action == "snap":
        pages = _Pages(working_pdf)
        try:
            tgt = _target_of(model, n, pages)
            land = _Landing(model, pages)
            cands = land._page(tgt["page"]) if tgt.get("type") == "page" else []
        finally:
            pages.close()
        if not cands:
            raise LinkError("nothing is printed on that page")
        y = tgt.get("y") or 0.0
        d, best = min(cands, key=lambda t: min(abs(t[0][1] - y), abs(t[0][3] - y)))
        return retarget(model, working_pdf, lid, {"node": best.id}, opts)
    if action == "key":
        key = fix.get("key") if isinstance(fix, dict) else ""
        t = _targets(model, working_pdf).get(key)
        if t is None:
            raise LinkError(f"no “{key}” in the document")
        if t.node and t.node in model.nodes:
            return retarget(model, working_pdf, lid, {"node": t.node}, opts)
        doc = pymupdf.open(working_pdf)
        try:
            y = _top_to_disp_y(doc[t.page], t.top)
        finally:
            doc.close()
        return retarget(model, working_pdf, lid, {"page": t.page, "y": y}, opts)
    if action == "twoway":
        st = dict(n.style or {})
        st.pop("two_way", None)
        n.style = st
        return make_two_way(model, working_pdf, lid, None, opts)
    raise LinkError(f"unknown fix {action}")


def fix_all(model: DocModel, working_pdf: str, opts: Options | None = None,
            codes: set | None = None) -> dict:
    """Every fix the check offers (but taking links out, unless asked)."""
    codes = codes or {"mailto", "trim_uri", "mismatch", "lands_empty", "back_missing", "printed_differs"}
    done, failed = 0, []
    data = list_links(model, working_pdf)
    for it in data["links"]:
        for p in it["problems"]:
            if p.get("fix") and p["code"] in codes and it["id"] in model.nodes:
                try:
                    fix_problem(model, working_pdf, it["id"], p["fix"], opts)
                    done += 1
                except LinkError as exc:
                    failed.append(f"{it['id']}: {exc}")
                break
    return {"ok": True, "fixed": done, "failed": failed}


def peek(working_pdf: str, page_no: int, y: float | None, width: int = 460) -> bytes:
    """A picture of where a link lands: a band of the page with the spot
    marked (display y)."""
    doc = pymupdf.open(working_pdf)
    try:
        if not 0 <= page_no < doc.page_count:
            raise LinkError("no such page")
        page = doc[page_no]
        W, H = page.rect.width, page.rect.height
        y = 0.0 if y is None else max(0.0, min(H, float(y)))
        # the width the text is printed in, so the words are big enough to read
        x0, x1 = 0.0, W
        try:
            m = page.rotation_matrix
            xs = []
            for b in page.get_text("blocks"):
                r = pymupdf.Rect(b[:4]) * m
                r.normalize()
                if r.width > 2:
                    xs += [r.x0, r.x1]
            if xs:
                x0, x1 = max(0.0, min(xs) - 12), min(W, max(xs) + 12)
                if x1 - x0 < 200:
                    c = (x0 + x1) / 2
                    x0, x1 = max(0.0, c - 100), min(W, c + 100)
        except Exception:
            pass
        top = max(0.0, y - 50)
        clip = pymupdf.Rect(x0, top, x1, min(H, top + 50 + 0.32 * (x1 - x0)))
        scale = max(0.4, min(2.5, width / max(1.0, clip.width)))
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False)
        # the spot: a red bar on the left and a thin line across
        yy = int((y - clip.y0) * scale)
        for xx in range(0, pix.width):
            for dy in (0, 1):
                if 0 <= yy + dy < pix.height and (xx < 14 or xx % 6 < 3):
                    pix.set_pixel(xx, yy + dy, (220, 38, 38))
        return pix.tobytes("png")
    finally:
        doc.close()
