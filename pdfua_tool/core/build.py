"""Build step: tag content, write structure, remediate, save once."""
from __future__ import annotations

import os
import re

import pikepdf
from pikepdf import Dictionary, Name

import pymupdf
from pikepdf import Operator, String
from pikepdf import ContentStreamInstruction as CSI

from .contrast import apply_fixes as apply_contrast_fixes
from .model import DocModel, convert_empty_headers, wrap_sections
from .options import Options
from .remediate import is_empty_markup, remediate
from .structure import write_structure
from .tagger import PageTagger, strip_form_marked_content


def effective_model(model: DocModel) -> DocModel:
    """Copy of the model with artifact status inherited by descendants."""
    eff = DocModel.from_json(model.to_json())
    eff.check_integrity()
    for n in eff.ordered_nodes():
        if n.parent and eff.nodes[n.parent].artifact:
            n.artifact = True
    _empty_headers_to_td(eff)
    return eff


def _sections(eff: DocModel, opts: Options) -> int:
    if eff.info.get("teachers_guide"):
        return wrap_sections(eff, "one")       # V4.24: the whole Teacher's Guide in one Sect
    return wrap_sections(eff, getattr(opts, "section_wrap", "h1"))


def _empty_headers_to_td(eff: DocModel) -> int:
    """Empty header cells (TH) are written as TD."""
    return convert_empty_headers(eff)


def _node_text(model: DocModel, nid: str) -> str:
    n = model.nodes[nid]
    parts = [n.text] if n.text else []
    for d in model.descendants(nid):
        if d.text and not d.artifact:
            parts.append(d.text)
    return " ".join(parts).strip()


def _empty_cells(eff: DocModel) -> list:
    out = []
    for n in eff.nodes.values():
        if n.role not in ("TD", "TH") or n.artifact or not n.bbox or (n.text or "").strip():
            continue
        if n.src_keys or n.annots:
            continue            # has (possibly whitespace) content of its own
        if any(not d.artifact for d in eff.descendants(n.id)):
            continue
        out.append(n)
    return out


def _blank_lines(eff: DocModel) -> list:
    return [n for n in eff.nodes.values()
            if (n.style or {}).get("blank_line") and not n.artifact and n.bbox
            and not (n.text or "").strip() and not n.children and not n.src_keys]


class BgSampler:
    """Average background luminance of a PDF-space box, from a low-res render."""

    def __init__(self, path: str, dpi: int = 36) -> None:
        self.doc = None
        try:
            self.doc = pymupdf.open(path)
        except Exception:
            pass
        self.zoom = dpi / 72.0
        self.cache: dict[int, tuple] = {}

    def lum(self, pno: int, bbox) -> float:
        if self.doc is None or pno >= self.doc.page_count:
            return 1.0
        try:
            import numpy as np
            if pno not in self.cache:
                page = self.doc[pno]
                pix = page.get_pixmap(matrix=pymupdf.Matrix(self.zoom, self.zoom), alpha=False,
                                      colorspace=pymupdf.csRGB)
                arr = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
                self.cache[pno] = (arr, page.transformation_matrix)
            arr, tm = self.cache[pno]
            r = (pymupdf.Rect(bbox) * tm) * self.zoom
            x0, y0 = max(0, int(r.x0)), max(0, int(r.y0))
            x1, y1 = min(arr.shape[1], int(r.x1) + 1), min(arr.shape[0], int(r.y1) + 1)
            if x1 <= x0 or y1 <= y0:
                return 1.0
            c = arr[y0:y1, x0:x1].reshape(-1, 3).astype(float) / 255.0
            lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
            return float((lin @ np.array([0.2126, 0.7152, 0.0722])).mean())
        except Exception:
            return 1.0

    def close(self) -> None:
        if self.doc is not None:
            self.doc.close()


def _free_spot(box, tl: float, fs: float, avoid: list) -> tuple[float, float]:
    """Where in this cell the invisible text can sit without standing inside a
    neighbouring cell's box as well.

    A ruled form sometimes has a hairline column drawn inside another cell. Both
    are cells, and glyphs in the overlap go to the smaller of the two — so the
    text written for the larger cell is read in the smaller one and the larger is
    left with nothing at all, which PAC reports. The text is placed in the part
    of the cell no other cell covers, when there is one."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    cands = [(x0 + max(1.0, (w - tl) / 2), (y0 + y1) / 2)]
    for fx in (0.0, 1.0, 0.5):
        for fy in (0.5, 0.12, 0.88, 0.3, 0.7):
            tx = x0 + 1.0 + fx * max(0.0, w - tl - 2.0)
            cands.append((tx, y0 + fy * h))
    for tx, ty in cands:
        r = (tx - 0.5, ty - fs * 0.6, tx + tl + 0.5, ty + fs * 0.6)
        if not any(a[0] - 1.5 < r[2] and r[0] < a[2] + 1.5
                   and a[1] - 1.5 < r[3] and r[1] < a[3] + 1.5 for a in avoid):
            return tx, ty
    return cands[0]


def _add_invisible_cell_text(pdf: pikepdf.Pdf, cells: list, text: str,
                             sampler: "BgSampler | None" = None,
                             avoid: dict | None = None) -> int:
    """Put invisible (render mode 3) real text in empty cells so every screen
    reader finds content there. Fonts used only invisibly are exempt from
    embedding (ISO 14289-1 7.21.4.1)."""
    try:
        data = text.encode("cp1252")
    except UnicodeEncodeError:
        return 0
    helv = pymupdf.Font("helv")
    by_page: dict[int, list] = {}
    for n in cells:
        by_page.setdefault(n.page, []).append(n)
    added = 0
    from .preflight import _own_resources
    for pno, items in by_page.items():
        page = pdf.pages[pno]
        res = _own_resources(page)
        fonts = res.get("/Font")
        if not isinstance(fonts, Dictionary):
            fonts = Dictionary()
            res.Font = fonts
        if "/UABlank" not in fonts:
            fonts.UABlank = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                                         BaseFont=Name.Helvetica,
                                                         Encoding=Name.WinAnsiEncoding))
        ops = []
        for n in items:
            x0, y0, x1, y1 = n.bbox
            w, h = x1 - x0, y1 - y0
            fs = min(8.0, max(1.0, h * 0.5))
            tl = helv.text_length(text, fontsize=fs) or 1
            if tl > w - 2:
                fs = max(0.5, fs * (w - 2) / tl)
            # centre it: in a narrow cell, text written hard against the left
            # edge can sit within a neighbour's box too, and the neighbour then
            # claims the glyphs — leaving this cell empty for PAC to report
            tl = helv.text_length(text, fontsize=fs) or 1
            tx = x0 + max(1.0, (w - tl) / 2)
            ty = (y0 + y1) / 2
            near = [b for b in (avoid or {}).get(n.page, [])
                    if b[0] < x1 and x0 < b[2] and b[1] < y1 and y0 < b[3]
                    and not (b[0] <= x0 and b[1] <= y0 and x1 <= b[2] and y1 <= b[3])]
            if near:
                tx, ty = _free_spot(n.bbox, tl, fs, near)
            # invisible, but give it the colour with the most contrast against
            # what is behind it, in case a checker measures invisible text too
            lum = sampler.lum(n.page, n.bbox) if sampler is not None else 1.0
            ops += [CSI([], Operator("q")), CSI([0 if lum >= 0.179 else 1], Operator("g")),
                    CSI([], Operator("BT")),
                    CSI([Name.UABlank, round(fs, 2)], Operator("Tf")), CSI([3], Operator("Tr")),
                    CSI([1, 0, 0, 1, round(tx, 2), round(ty - fs * 0.3, 2)], Operator("Tm")),
                    CSI([String(data)], Operator("Tj")), CSI([], Operator("ET")), CSI([], Operator("Q"))]
            n.style = dict(n.style or {}, blank_text=True)
            added += 1
        page.contents_add(pdf.make_stream(b"q\n"), prepend=True)
        page.contents_add(pdf.make_stream(b"Q\n" + pikepdf.unparse_content_stream(ops)), prepend=False)
        page.contents_coalesce()
    return added


def build(model: DocModel, src: str, dst: str, opts: Options,
          contrast_fixes: list[dict] | None = None) -> dict:
    from .compat import use as use_engine
    use_engine(opts)
    eff = effective_model(model)
    pdf = pikepdf.open(src)
    contrast_stats = {"applied": [], "skipped": {}}
    if contrast_fixes:
        contrast_stats = apply_contrast_fixes(
            pdf, contrast_fixes, method=getattr(opts, "contrast_fix_method", "auto"),
            stroke_width=getattr(opts, "contrast_stroke_width", 0.5),
            stroke_color=getattr(opts, "contrast_stroke_color", "black"))
    stats: dict = {"pages": len(pdf.pages), "mcids": 0, "glyphs": 0, "split_ops": 0,
                   "unmatched": 0, "attached_to_nearest": 0, "kept_oc": 0,
                   "kept_spans": 0, "forms_whole": 0, "page_errors": [],
                   "unmatched_pages": {}, "blank_text_cells": 0}
    # a blank row found on top of the row that is really there can never be given
    # any content, so it goes before anything is written for it
    try:
        from .repair import drop_duplicate_rows
        dup = drop_duplicate_rows(eff)
        if dup:
            stats["duplicate_rows_dropped"] = dup
    except Exception as exc:
        stats["duplicate_row_error"] = f"{exc}"
    # hidden fills under text confuse vector-based contrast checkers (PAC)
    stats["fills_trimmed"] = 0
    if getattr(opts, "normalize_backgrounds", True):
        from .normalize import trim_hidden_fills
        for pno in range(len(pdf.pages)):
            try:
                stats["fills_trimmed"] += trim_hidden_fills(pdf, pno)
            except Exception as exc:
                stats["page_errors"].append(f"page {pno + 1}: background normalisation skipped ({exc})")
    sampler = BgSampler(src)
    if opts.empty_cell_text.strip() and opts.empty_cell_method == "text":
        stats["blank_text_cells"] = _add_invisible_cell_text(pdf, _empty_cells(eff),
                                                             opts.empty_cell_text.strip(), sampler)
    # write-in lines: P with the drawn rule, announced as e.g. "Blank Line"
    bl_text = (getattr(opts, "blank_line_text", "") or "").strip()
    lines_ = _blank_lines(eff)
    all_blanks = [n for n in eff.nodes.values()
                  if (n.style or {}).get("blank_line") and not n.artifact]
    stats["blank_lines"] = len(all_blanks)
    multi_text = (getattr(opts, "multi_blank_line_text", "") or "").strip() or bl_text
    stats["multi_blank_lines"] = 0
    for n in all_blanks:                  # drawn lines and "_______" runs alike
        if (n.actual_text or "").strip():
            continue                      # set by hand in the editor
        # one answer made of several lines -> "Multiple Blank Lines"
        multi = len(n.rects_on(n.page)) + sum(1 for p in n.parts if int(p[0]) != n.page) > 1
        if multi:
            stats["multi_blank_lines"] += 1
        if (n.style or {}).get("tick"):
            # the two-underscore tick a fluency passage prints before its line
            # count is not a line to write on — it is read as what it is
            text = (getattr(opts, "underscore_tick_text", "") or "").strip()
            stats["underscore_ticks"] = stats.get("underscore_ticks", 0) + 1
        else:
            text = multi_text if multi else bl_text
        if text:
            n.actual_text = text
    if lines_ and opts.empty_cell_method == "text":
        by_text: dict[str, list] = {}
        for n in lines_:
            t = (n.actual_text or "").strip()
            if t:
                by_text.setdefault(t, []).append(n)
        for t, ns in by_text.items():
            _add_invisible_cell_text(pdf, ns, t, sampler)
    sampler.close()

    # forms that were not unpacked keep no stale MCIDs
    seen: set = set()
    stripped = 0
    for page in pdf.pages:
        res = page.obj.get("/Resources")
        xo = res.get("/XObject") if isinstance(res, Dictionary) else None
        if isinstance(xo, Dictionary):
            for _, x in xo.items():
                if isinstance(x, pikepdf.Stream) and x.get("/Subtype") == Name.Form:
                    stripped += strip_form_marked_content(pdf, x, seen)
    stats["form_mc_stripped"] = stripped

    owners: list[list[str]] = []
    for pno in range(len(pdf.pages)):
        t = PageTagger(pdf, pno, eff, import_mode=(eff.mode == "import"),
                       unmatched=opts.unmatched_content,
                       join=bool(getattr(opts, "join_marked_content", False)))
        r = t.run()
        owners.append(r.mcid_owner)
        stats["mcids"] += len(r.mcid_owner)
        stats["glyphs"] += r.glyphs
        stats["split_ops"] += r.split_ops
        stats["unmatched"] += len(r.unmatched)
        stats["attached_to_nearest"] += r.to_nearest
        stats["kept_oc"] += r.kept_oc
        stats["kept_spans"] += r.kept_spans
        stats["forms_whole"] += r.forms_whole
        if r.unmatched:
            stats["unmatched_pages"][pno] = len(r.unmatched)
        if r.error:
            stats["page_errors"].append(f"page {pno + 1}: {r.error}")

    # A cell can still end up with nothing: two cells that overlap (a ruled form
    # with a hairline column inside another) both claim the same glyphs and the
    # loser is left empty, which PAC reports as "not associated with content".
    # Those are only known once the first tagging pass has run, so they get the
    # invisible text now and their pages are tagged again.
    if opts.empty_cell_text.strip() and opts.empty_cell_method == "text":
        owned = {nid for lst in owners for nid in lst}
        late = [n for n in eff.nodes.values()
                if n.role in ("TD", "TH") and not n.artifact and n.bbox and n.id not in owned
                and not n.children and not n.annots and not n.src_keys
                and not (n.style or {}).get("blank_text_again")]
        if late:
            # one of these may still be marked TH; a cell that is visually empty
            # is a data cell whatever the grid said
            for n in late:
                if n.role == "TH":
                    n.role, n.scope, n.headers = "TD", "", []
            # the second attempt keeps clear of the cells it overlaps
            boxes: dict[int, list] = {}
            ids = {n.id for n in late}
            for c in eff.nodes.values():
                if c.role in ("TD", "TH") and not c.artifact and c.bbox and c.id not in ids:
                    boxes.setdefault(c.page, []).append(c.bbox)
            for n in late:
                n.style = dict(n.style or {}, blank_text_again=True)
            s2 = BgSampler(src)
            stats["blank_text_cells"] += _add_invisible_cell_text(
                pdf, late, opts.empty_cell_text.strip(), s2, boxes)
            s2.close()
            stats["late_blank_cells"] = len(late)
            for pno in sorted({n.page for n in late}):
                t = PageTagger(pdf, pno, eff, import_mode=(eff.mode == "import"),
                               unmatched=opts.unmatched_content,
                               join=bool(getattr(opts, "join_marked_content", False)))
                r = t.run()
                stats["mcids"] += len(r.mcid_owner) - len(owners[pno])
                owners[pno] = r.mcid_owner
            owned2 = {nid for lst in owners for nid in lst}
            still = [n for n in late if n.id not in owned2]
            stats["blank_cells_left"] = len(still)
            if os.environ.get("UA_DEBUG_CELLS") and still:
                for n in still:
                    others = [tuple(round(v, 1) for v in c.bbox) for c in eff.nodes.values()
                              if c.role in ("TD", "TH") and c.page == n.page and c.id != n.id
                              and c.bbox and c.bbox[0] < n.bbox[2] and n.bbox[0] < c.bbox[2]
                              and c.bbox[1] < n.bbox[3] and n.bbox[1] < c.bbox[3]]
                    print(f"[cells] page {n.page + 1} {n.id} {tuple(round(v, 1) for v in n.bbox)} "
                          f"overlaps {others}", flush=True)

    # Last word on header cells: a TH that ends up with no content at all is
    # written as TD. The model's empty THs were already converted before
    # tagging, but a cell can also *lose* its text here, when two overlapping
    # cells both claim the same glyphs — and an empty TH is a header for a
    # column that does not exist.
    # Two tables found over the same ruling (a boxed answer band read as a table
    # of its own, on top of the row that is really there): the glyphs can only
    # belong to one of them, and the loser is left as a tree of elements with
    # nothing associated — exactly what PAC reports. The empty copy goes.
    owned = {nid for lst in owners for nid in lst}
    dropped_rows = 0
    live = eff.attached_ids()               # V4.33: only what is in the tree is looked at — and what goes, goes from the model too
    for tr in [n for n in eff.nodes.values() if n.role == "TR" and not n.artifact and n.id in live]:
        if tr.id not in eff.nodes:          # (it went with a row that held it)
            continue
        fam = [tr] + list(eff.descendants(tr.id))
        if any(d.id in owned or d.annots for d in fam):
            continue
        eff.remove(tr.id, promote_children=False)
        dropped_rows += 1
    if dropped_rows:
        stats["empty_rows_dropped"] = dropped_rows
        live = eff.attached_ids()
        for tb in [n for n in eff.nodes.values() if n.role == "Table" and not n.artifact and n.id in live]:
            if tb.id in eff.nodes and not any(d.role == "TR" for d in eff.descendants(tb.id)):
                eff.remove(tb.id, promote_children=True)        # a caption or a note it held stays where the table was

    from .compat import house as _house
    # Kriyadocs (V4.17): an empty header cell stays TH, as in the client's files
    blank_th = [] if _house() else [n for n in eff.nodes.values()
                if n.role == "TH" and not n.artifact and n.id not in owned
                and not n.annots and not (n.text or "").strip()
                and not any(not d.artifact for d in eff.descendants(n.id))]
    for n in blank_th:
        n.role = "TD"
        n.scope = ""
        n.headers = []
    if blank_th:
        gone = {n.id for n in blank_th}
        for n in eff.nodes.values():
            if n.headers:
                n.headers = [h for h in n.headers if h not in gone]
        stats["empty_headers_to_td"] = len(blank_th)

    # annotations the analysis set aside (a client's house style: the link in
    # a website's download stamp) leave the file
    drop = {int(x) for x in (eff.info.get("drop_annots") or [])}
    if drop:
        n_drop = 0
        for page in pdf.pages:
            arr = page.obj.get("/Annots")
            if not isinstance(arr, pikepdf.Array):
                continue
            keep = [a for a in arr if not (isinstance(a, Dictionary) and a.is_indirect and a.objgen[0] in drop)]
            if len(keep) != len(arr):
                n_drop += len(arr) - len(keep)
                page.obj.Annots = pikepdf.Array(keep)
        stats["annots_dropped"] = n_drop

    # a link of the file pointed elsewhere in the Links stage (V4.18): the
    # annotation stays, its action is new
    retarget = {int(k): v for k, v in (eff.info.get("annot_targets") or {}).items()}
    if retarget:
        from .xref import _dest
        n_ret = 0
        for page in pdf.pages:
            for a in page.obj.get("/Annots", []) or []:
                if not (isinstance(a, Dictionary) and a.is_indirect and a.objgen[0] in retarget):
                    continue
                v = retarget[a.objgen[0]] or {}
                try:
                    if v.get("uri"):
                        a.A = Dictionary(S=Name.URI, URI=String(str(v["uri"])))
                    else:
                        tp = int(v.get("page", -1))
                        if not 0 <= tp < len(pdf.pages):
                            continue
                        a.A = Dictionary(S=Name.GoTo, D=_dest(pdf, pdf.pages[tp], v.get("top")))
                    if "/Dest" in a:
                        del a["/Dest"]
                    n_ret += 1
                except Exception:
                    continue
        stats["links_pointed_elsewhere"] = n_ret

    # a link annotation cut down to the words it is for (Kriyadocs, V4.17:
    # "2 3" printed under one annotation — the annotation keeps the "2", the
    # "3" gets its own)
    cut = {int(k): v for k, v in (eff.info.get("annot_rects") or {}).items()}
    if cut:
        n_cut = 0
        for page in pdf.pages:
            for a in page.obj.get("/Annots", []) or []:
                if isinstance(a, Dictionary) and a.is_indirect and a.objgen[0] in cut:
                    r = cut[a.objgen[0]]
                    a.Rect = pikepdf.Array([round(float(v), 2) for v in r])
                    n_cut += 1
        stats["annots_cut_to_their_words"] = n_cut

    if getattr(opts, "empty_markup_annots", "remove") == "remove":
        gone = 0
        for page in pdf.pages:
            arr = page.obj.get("/Annots")
            if not isinstance(arr, pikepdf.Array):
                continue
            keep, popups = [], set()
            for a in arr:
                if isinstance(a, Dictionary) and is_empty_markup(a):
                    pop = a.get("/Popup")
                    if isinstance(pop, Dictionary) and pop.is_indirect:
                        popups.add(pop.objgen)
                    gone += 1
                else:
                    keep.append(a)
            if len(keep) != len(arr):
                page.obj.Annots = pikepdf.Array(
                    [a for a in keep if not (isinstance(a, Dictionary) and a.is_indirect and a.objgen in popups)])
        stats["empty_review_marks_removed"] = gone

    # tags that sit where PDF/UA does not allow them (a contents entry inside a
    # paragraph, a list item at section level) go back where they belong first:
    # the sections are built on the repaired tree
    try:
        from .repair import fix_structure
        fixed = fix_structure(eff)
        if fixed:
            stats["structure_fixed"] = fixed
    except Exception as exc:
        stats["structure_repair_error"] = f"{exc}"

    if getattr(opts, "table_spans", "keep") == "pad":
        from .model import pad_table_spans
        stats["span_positions_padded"] = pad_table_spans(eff)

    made = _sections(eff, opts)
    if made:
        stats["sections"] = made

    # link annotations for the cross-references found during analysis must exist
    # before the structure is written, so their OBJR entries can point at them
    xstats = {}
    if getattr(opts, "detect_cross_references", True):
        from .xref import create_annotations
        try:
            xstats = create_annotations(pdf, eff, opts)
        except Exception as exc:
            stats["page_errors"].append(f"cross-reference links: {exc}")
    stats["xref_links"] = xstats

    sstats = write_structure(pdf, eff, owners, figure_bbox=opts.figure_bbox_attribute,
                             empty_cell_text=opts.empty_cell_text.strip(),
                             actualtext_span=bool(getattr(opts, "actualtext_span", False)))
    stats["contrast"] = contrast_stats
    stats.update({k: v for k, v in sstats.items()})

    link_texts = {}
    link_overrides: dict[int, str] = {}
    house = (getattr(opts, "house_style", "") or "").lower() not in ("", "none", "off")
    field_labels = {}
    for n in eff.nodes.values():
        if not n.annots:
            continue
        if n.role == "Link":
            # an /Alt set on the Link (by hand or from an AI draft) is the
            # description the author chose and replaces whatever /Contents holds
            txt = _node_text(eff, n.id)
            if house:
                # the full stop a citation mark is set after is not its name (".1")
                txt = re.sub(r"^[\s.,;:]+(?=\w)", "", txt or "")
            # a description kept for the annotation alone (no /Alt on the tag)
            said = (n.alt or "").strip() or str((n.style or {}).get("contents") or "").strip()
            for a in n.annots:
                link_texts[a] = said or txt
                if said:
                    link_overrides[a] = said
        elif n.role == "Form":
            for a in n.annots:
                if n.text:
                    field_labels[a] = n.text
    rem = remediate(pdf, eff, opts, link_texts, field_labels, link_overrides)
    stats["applied"] = rem["applied"]
    if stats.get("sections"):
        stats["applied"] = list(stats["applied"]) + [
            f"{stats['sections']} Sect element(s) wrapped around the headings"]
    if stats.get("structure_fixed"):
        what = ", ".join(f"{v} {k}" for k, v in sorted(stats["structure_fixed"].items()))
        stats["applied"] = list(stats["applied"]) + [
            f"{what} put back where PDF/UA allows them (a contents entry inside a paragraph, "
            "a list item outside a list …)"]
    if stats.get("empty_rows_dropped") or stats.get("duplicate_rows_dropped"):
        n_ = (stats.get("empty_rows_dropped", 0) or 0) + (stats.get("duplicate_rows_dropped", 0) or 0)
        stats["applied"] = list(stats["applied"]) + [
            f"{n_} table row(s) printed on top of another row removed — nothing could be "
            "read in them"]
    if xstats.get("created"):
        kinds = ", ".join(f"{v} {k}" for k, v in sorted(xstats.get("by_kind", {}).items()))
        stats["applied"] = list(stats["applied"]) + [
            f"{xstats['created']} link(s) created from cross-references in the text ({kinds})"]
    if stats.get("empty_review_marks_removed"):
        stats["applied"] = list(stats["applied"]) + [
            f"{stats['empty_review_marks_removed']} empty review mark(s) removed "
            "(tiny markup annotation without comment text — would fail PDF/UA 7.18.1)"]
    stats["review"] = rem["review"]
    stats["bookmarks"] = rem["bookmarks"]
    stats["missing_alt"] = [
        {"id": n.id, "page": n.page} for n in eff.nodes.values()
        if n.role in ("Figure", "Formula") and not n.artifact and not (n.alt or n.actual_text)
        and n.id not in {p["id"] for p in stats.get("pruned", [])}
    ]

    ver = str(getattr(opts, "pdf_min_version", "auto") or "auto")
    pdf.save(dst, compress_streams=True,
             object_stream_mode=pikepdf.ObjectStreamMode.generate,
             **({"min_version": ver} if ver in ("1.6", "1.7") else {}))
    pdf.close()

    # write MCIDs back for reporting
    for nid, n in eff.nodes.items():
        if nid in model.nodes:
            model.nodes[nid].mcids = n.mcids
    return stats
