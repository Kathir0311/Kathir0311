"""Live issues for the editor, computed from the model (no PDF needed)."""
from __future__ import annotations

import re

from .compat import house as _house
from .model import CONTAINER_ROLES, HEADINGS, DocModel, is_empty_header

LANG_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*$")


def table_grid(model: DocModel, tid: str) -> tuple[dict, list[int]]:
    """(row, col) -> cell id, plus the width of each row (with spans)."""
    rows = []
    for c in model.nodes[tid].children:
        n = model.nodes.get(c)
        if n is None:
            continue
        if n.role == "TR":
            rows.append(n)
        elif n.role in ("THead", "TBody", "TFoot"):
            rows += [model.nodes[g] for g in n.children if model.nodes[g].role == "TR"]
    occ: dict[tuple[int, int], str] = {}
    widths = []
    for ri, row in enumerate(rows):
        col = 0
        for cid in row.children:
            cell = model.nodes[cid]
            if cell.role not in ("TH", "TD"):
                continue
            while (ri, col) in occ:
                col += 1
            for dr in range(max(1, cell.rowspan)):
                for dc in range(max(1, cell.colspan)):
                    occ[(ri + dr, col + dc)] = cid
            col += max(1, cell.colspan)
        widths.append(max([c for (r, c) in occ if r == ri], default=-1) + 1)
    return occ, widths


_LIST_LABEL = re.compile(r"^\s*(?:[\u2022\u25cf\u25e6\u25cb\u25aa\u25a0\u25a1\u2013\u2014\u00b7*-]|"
                         r"\(?\d{1,3}(?:\s*(?:and|&|to|or|[\u2013\u2014-])\s*\d{1,3})?[.):]|"
                         r"\(?[A-Za-z][.)])(?=\s)")
_WEAK_LINK = re.compile(r"^\s*(click here|here|read more|more|link|this|www\.|https?://|\S{0,3})\s*$", re.I)


_TOC_PAGE = re.compile(r"(\d{1,4}|\b[ivxlc]{1,6})\s*$", re.I)


def lint(model: DocModel) -> list[dict]:
    out: list[dict] = []

    def add(sev, code, msg, node=None):
        out.append({"severity": sev, "code": code, "message": msg,
                    "id": node.id if node else None,
                    "page": node.page if node else None})

    if not (model.title or "").strip():
        add("error", "title", "Document has no title")
    if not LANG_RE.match(model.lang or ""):
        add("error", "lang", f"Document language '{model.lang}' is not a valid BCP 47 tag")

    # reading order: a block read in the middle of other pages' content
    from .edit import order_problems
    for nid in order_problems(model):
        n = model.nodes[nid]
        prev_pages = [model.nodes[x].page for x in model.siblings(nid)[:model.siblings(nid).index(nid)]
                      if not model.nodes[x].artifact]
        after = max(prev_pages) + 1 if prev_pages else None
        add("warning", "order",
            f"Reading order: this page-{n.page + 1} tag is read "
            + (f"after page-{after} content" if after and after > n.page + 1 else "out of page order")
            + " — use “Fix reading order”", n)

    art_anc = set()
    for n in model.ordered_nodes():
        if n.artifact or (n.parent in art_anc):
            art_anc.add(n.id)

    prev = 0
    for n in model.ordered_nodes():
        if n.id in art_anc:
            continue
        parent = model.nodes.get(n.parent) if n.parent else None
        prole = parent.role if parent else "Document"
        r = n.role

        if r in ("Figure", "Formula") and not (n.alt or n.actual_text):
            add("error", "alt", f"{r} has no alternative text", n)
        if r in HEADINGS:
            lv = int(r[1])
            if prev == 0 and lv != 1:
                add("error", "heading", f"First heading is {r}; it should be H1", n)
            elif prev and lv > prev + 1:
                add("error", "heading", f"{r} follows H{prev} — a level is skipped", n)
            prev = lv
        if r == "Table":
            occ, widths = table_grid(model, n.id)
            cells = [model.nodes[c] for c in set(occ.values())]
            if not any(c.role == "TH" for c in cells):
                add("error", "table", "Table has no header cells (TH)", n)
            if widths and len(set(widths)) > 1:
                add("warning", "table", f"Rows have different widths {sorted(set(widths))} — check spans", n)
            if not occ:
                add("error", "table", "Table has no rows", n)
        if r == "TR" and prole not in ("Table", "THead", "TBody", "TFoot"):
            add("error", "table", "TR outside a table", n)
        if prole == "TR" and r not in ("TD", "TH"):
            add("error", "table", f"{r} inside a table row but not in a cell "
                                  "— use “Fix structure”", n)
        if r == "TH" and is_empty_header(model, n) and not _house():
            add("error", "empty_th", "Empty header cell (TH) — an empty header must be a TD, use “Fix empty headers”", n)
        if r in ("TH", "TD") and prole != "TR":
            add("error", "table", f"{r} outside a table row", n)
        if r == "L":
            bad = [model.nodes[c].role for c in n.children
                   if model.nodes[c].role not in ("LI", "Caption", "L") and not model.nodes[c].artifact]
            if bad:
                add("error", "list", f"List contains {', '.join(sorted(set(bad)))} — only LI allowed", n)
            if not n.children:
                add("warning", "list", "Empty list", n)
        if r == "LI":
            kids = [model.nodes[c].role for c in n.children]
            if "LBody" not in kids:
                add("warning", "list", "List item without LBody", n)
            if "Lbl" not in kids:
                body = next((model.nodes[c] for c in n.children
                             if model.nodes[c].role == "LBody"), None)
                if body is not None and _LIST_LABEL.match(body.text or ""):
                    add("warning", "list", "The item number is inside the text instead of a Lbl "
                        "— use “Fix list labels”", n)
            if prole != "L":
                add("error", "list", "LI outside a list", n)
        if r in ("Lbl", "LBody") and prole not in ("LI",):
            add("error", "list", f"{r} outside a list item", n)
        if r == "TOCI" and prole != "TOC":
            add("warning", "toc", "TOCI outside a TOC", n)
        if r == "TOCI" and not any(d.role == "Link" for d in model.descendants(n.id)) \
                and _TOC_PAGE.search(" ".join(d.text or "" for d in [n] + model.descendants(n.id))):
            # (a group title with no page number has nothing to point at)
            add("warning", "toc", "Contents entry with nothing to click "
                                  "— use “Link the contents entries”", n)
        if r == "Link" and not n.annots and not (n.style or {}).get("xref"):
            add("error", "link", "Link tag without a link annotation", n)
        if r == "Link" and not (n.alt or "").strip() and not (n.style or {}).get("xref") \
                and _WEAK_LINK.match((n.text or "").strip()):
            # (a link the tool made says where it goes: "Go to page 12", "Back to …")
            add("warning", "link", "Link text says nothing on its own (“click here”, a bare "
                "address) — give it a description, or let the AI draft one", n)
        if r == "Formula" and not (n.alt or "").strip() and not (n.actual_text or "").strip():
            add("error", "formula", "Formula without /Alt — a screen reader cannot read it", n)
        if r == "Form" and not n.text:
            add("warning", "form", "Form field has no label — its tooltip will be empty", n)
        if r == "Annot" and not n.text and not n.alt:
            add("warning", "annot", "Annotation has no description", n)
        if r not in CONTAINER_ROLES and not n.children and not n.src_keys and not n.annots \
                and n.bbox is None and not n.parts:
            add("warning", "empty", f"{r} has no content and will be removed", n)
        if r in CONTAINER_ROLES and not n.children:
            add("warning", "empty", f"Empty {r} will be removed", n)
        if n.confidence < 0.55 and n.source not in ("user", "import"):
            add("info", "review", f"Low confidence {r}: {n.note or 'please confirm'}", n)
        if r == "H" and any(m.role in HEADINGS for m in model.nodes.values()):
            add("warning", "heading", "Generic H mixed with H1–H6", n)
    try:
        from .biblio import problems as bib_problems
        for hid in bib_problems(model)["sections"]:
            add("warning", "bibliography", "Reference list: the entries are not tagged as BibEntry "
                "(one entry may be cut into several paragraphs) — use “Tag the reference list”",
                model.nodes[hid])
    except Exception:
        pass
    try:
        from .backmatter import index_problems
        for hid in index_problems(model):
            add("warning", "index", "Index: the entries are paragraphs, not Index > L > LI, and "
                "their page numbers are not links — use “Tag the index”", model.nodes[hid])
    except Exception:
        pass
    order = {"error": 0, "warning": 1, "info": 2}
    out.sort(key=lambda i: (order[i["severity"]], i["page"] if i["page"] is not None else -1))
    return out
