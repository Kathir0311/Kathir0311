"""Client profile "Kriyadocs": a BMJ journal article tagged the client's way (V4.10).

A two-page article laid out like a BMJ Open PDF — logo, title, authors with an
ORCID icon, the sidebar (To cite, dates, Check for updates, licence,
affiliations, correspondence), the abstract, the box with its bullets, a table
ruled with horizontal lines only, its notes, the reference list numbered with
bare numbers, a download stamp up the right margin — is analysed with the
built-in "Kriyadocs" profile. The tree must read the way Kriyadocs tags it
(sample bmjgh-2024-018847):

  Document > one Sect > P "Original research", Figure "BMJ Open", H1, P authors
  (Figure "Orcid icon", Reference > Link to each affiliation; the ORCID link a
  plain Link), P To cite, P Received, P Accepted, P > Link (Check for updates),
  P ©, Note per affiliation, P Correspondence to (P > Link e-mail), H2 ABSTRACT,
  P, H3 box, L Disc,
  H2 INTRODUCTION … Figure "BMJ Group Logo" + P > Link at the end of page 1,
  Caption + Table (TR straight under it, TH header), Note "*T-test.",
  H2 REFERENCES + L > LI > Lbl + LBody, every cross-reference as Reference >
  Link and every web / e-mail address a plain Link (V4.17).

V4.17: an empty cell in a header row stays TH; citation numbers linked as one
("2 3") become one zone and one link each, a range ("1–3") stays one.

Built, the file passes the checker and the structure checks, and the stamp is
left out of the reading.
"""
import os
import sys
import tempfile

import pikepdf
import pymupdf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.checker import check                                   # noqa: E402
from core.options import Options, BUILTIN_PROFILES, profile_options       # noqa: E402
from core.pipeline import load_model, process_file               # noqa: E402
from tests.verify import mc_balance, structure_integrity          # noqa: E402

W, H = 595.0, 794.0
NAVY = (0.26, 0.21, 0.55)
GREEN = (0.65, 0.81, 0.22)
BLUE = (0.0, 0.39, 0.68)


# running text: enough of it that the body size is the 10 pt of the columns
_WORDS = ("the survey measured height weight and blood pressure of every adult who took part and "
          "each scan was read twice by two doctors who did not know the answers given in the "
          "questionnaire so that the size and number of nodules could be compared across groups").split()
BODY: list = []
_line = ""
for _k in range(900):
    _w = _WORDS[_k % len(_WORDS)]
    if pymupdf.get_text_length(f"{_line} {_w}".strip(), fontname="tiro", fontsize=10) > 172:
        BODY.append(_line)
        _line = _w
    else:
        _line = f"{_line} {_w}".strip()


def text(p, x, y, t, size=10.0, font="tiro", color=(0, 0, 0)):
    p.insert_text((x, y), t, fontsize=size, fontname=font, color=color)
    return x + pymupdf.get_text_length(t, fontname=font, fontsize=size)


def sup(p, x, y, t, size=6.5, font="tiro"):
    """A raised note mark after a word."""
    return text(p, x, y - 3.5, t, size=size, font=font)


def para(p, x, y, lines, size=10.0, lead=12.0, font="tiro"):
    for t in lines:
        text(p, x, y, t, size, font)
        y += lead
    return y


def stamp(p):
    p.insert_text((585, 120), "BMJ Open: first published as 10.1136/bmjopen-2023-000001 on 1 June 2023. "
                  "Downloaded from http://bmjopen.bmj.com/ on 1 January 2026 by guest. Protected by copyright.",
                  fontsize=7.5, fontname="hebo", rotate=-90)
    # the stamp's web address is a link of its own
    hit = p.search_for("bmjopen.bmj.com/ on 1")
    if hit:
        p.insert_link({"kind": pymupdf.LINK_URI, "uri": "http://bmjopen.bmj.com/", "from": hit[0]})


def header(p, right=""):
    p.draw_rect(pymupdf.Rect(36, 14, W - 36, 30), color=None, fill=NAVY)
    text(p, 44, 26, "Open access", 9, "hebo", (1, 1, 1))
    if right:
        text(p, W - 44 - pymupdf.get_text_length(right, fontname="hebo", fontsize=9), 26, right, 9, "hebo", (1, 1, 1))


def footer(p, n):
    text(p, 200, 772, "Lee A, et al. BMJ Open 2023;13:e000001. doi:10.1136/bmjopen-2023-000001", 7.5, "helv")
    text(p, 550, 772, str(n), 9, "helv")


def article(path: str) -> None:
    d = pymupdf.open()
    p = d.new_page(width=W, height=H)
    header(p, "Original research")
    # the masthead logo: letters drawn as filled shapes, no text
    for k, x in enumerate(range(42, 130, 12)):
        p.draw_rect(pymupdf.Rect(x, 50, x + 9, 66), color=None, fill=NAVY)
    # title
    text(p, 160, 72, "Risk factors for thyroid nodules in", 20, "tibo", NAVY)
    text(p, 160, 96, "adults: a cross-sectional study", 20, "tibo", NAVY)
    # authors, an ORCID icon after the last name
    x = text(p, 160, 130, "Ana Lee,", 11, "helv")
    x = sup(p, x, 130, "1", 7, "helv")
    x = text(p, x + 3, 130, "Bo Chen,", 11, "helv")
    x = sup(p, x, 130, "2", 7, "helv")
    x = text(p, x + 3, 130, "Cara Diaz", 11, "helv")
    p.draw_circle((x + 7, 126), 4.3, color=None, fill=GREEN)
    p.insert_link({"kind": pymupdf.LINK_URI, "uri": "https://orcid.org/0000-0000-0000-0001",
                   "from": pymupdf.Rect(x + 2, 121, x + 12, 131)})
    sup(p, x + 14, 130, "1", 7, "helv")
    # sidebar
    y = para(p, 42, 170, ["To cite: Lee A, Chen B, Diaz C,", "et al. Risk factors for thyroid",
                          "nodules in adults. BMJ Open", "2023;13:e000001.", ], 7.5, 9.0, "helv")
    para(p, 42, y + 14, ["Received 20 October 2022", "Accepted 18 October 2023"], 7.5, 9.0, "helv")
    badge = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 90, 18), 0)
    badge.set_rect(badge.irect, (200, 200, 200))
    p.insert_image(pymupdf.Rect(42, 260, 132, 278), pixmap=badge)
    p.insert_link({"kind": pymupdf.LINK_URI, "uri": "http://crossmark.crossref.org/dialog/?doi=10.1136/x",
                   "from": pymupdf.Rect(40, 258, 134, 280)})
    y = para(p, 42, 298, ["© Author(s) (or their", "employer(s)) 2023. Re-use", "permitted under CC BY-NC."],
             7.5, 9.0, "helv")
    y += 10
    for n, lines in (("1", ["Department of Surgery, City", "Hospital, Shenyang, China"]),
                     ("2", ["University of Somewhere,", "Beijing, China"])):
        x = sup(p, 42, y, n, 5.5, "helv")
        text(p, x, y, lines[0], 7.5, "helv")
        y = para(p, 42, y + 9, lines[1:], 7.5, 9.0, "helv")
    y += 10
    text(p, 42, y, "Correspondence to", 7.5, "hebo")
    text(p, 42, y + 9, "Dr Ana Lee;", 7.5, "helv")
    text(p, 42, y + 18, "ana.lee@example.org", 7.5, "helv")
    # abstract
    text(p, 160, 170, "ABSTRACT", 9, "hebo", NAVY)
    para(p, 160, 182, ["Objectives This study examined the association",
                       "between body measurements and thyroid nodules",
                       "among adults in Northeast China.",
                       "Design A cross-sectional survey of 1092 adults",
                       "with a questionnaire and an ultrasound scan.",
                       "Results Thyroid nodules were more common in",
                       "women and in older adults.",
                       "Conclusions Age and sex matter most."], 9, 11, "helv")
    # the box, right column
    text(p, 365, 170, "STRENGTHS AND LIMITATIONS OF THIS STUDY", 8.5, "hebo")
    yb = 184
    for item in (["Few studies have looked at thyroid nodules in", "this region of the country."],
                 ["The sample of this study is not large enough."]):
        text(p, 365, yb, "•  " + item[0], 8.5, "helv")
        yb = para(p, 375, yb + 10.5, item[1:], 8.5, 10.5, "helv")
    # introduction: a first paragraph, then one that runs on from the foot of
    # the left column to the top of the right one, under the box
    text(p, 160, 300, "INTRODUCTION", 9, "hebo", NAVY)
    yy = para(p, 160, 314, BODY[:29] + [BODY[29] + "."], 10, 12)
    para(p, 170, yy, ["Thyroid nodules are discrete lesions within"], 10, 12)
    para(p, 160, yy + 12, ["the thyroid gland that are seen clearly on",
                           "an ultrasound scan of the neck and in",
                           "many more people of all ages and in"], 10, 12)
    yy = para(p, 365, 314, ["both sexes in every region that has been",
                            "studied so far in the published literature."], 10, 12)
    yy = para(p, 375, yy, ["Iodine intake is a factor that has been"], 10, 12)
    para(p, 365, yy, BODY[30:62], 10, 12)
    # page-1 footer logo and its link
    for x in (42, 50, 58):
        p.draw_rect(pymupdf.Rect(x, 762, x + 6, 774), color=None, fill=BLUE)
    p.insert_link({"kind": pymupdf.LINK_URI, "uri": "http://bmjopen.bmj.com/",
                   "from": pymupdf.Rect(40, 760, 66, 776)})
    footer(p, 1)
    stamp(p)

    q = d.new_page(width=W, height=H)
    header(q)
    # Table 1: ruled with horizontal lines only
    text(q, 44, 60, "Table 1", 8.5, "hebo", NAVY)
    text(q, 80, 60, "Characteristics of the participants", 8.5, "helv")
    rules = [66, 82, 98, 114, 130]
    for yy in rules:
        q.draw_line((40, yy), (300, yy), color=(0.5, 0.5, 0.5), width=0.5)
    for k, row in enumerate([("", "Without nodules", "With nodules"),     # an empty header cell
                             ("Age", "54.0±11.5", "59.7±10.7*"),
                             ("Male", "192 (39.3%)", "150 (24.9%)"),
                             ("Female", "297 (60.7%)", " ")]):      # an empty cell: a lone space
        f = "hebo" if k == 0 else "helv"
        yy = rules[k] + 11
        for x, t in zip((44, 130, 220), row):
            text(q, x, yy, t, 8, f)
    text(q, 44, 142, "Significant associations are highlighted in bold.", 7, "helv")
    text(q, 44, 151, "*T-test.", 7, "helv")
    # body text with citations
    text(q, 44, 180, "RESULTS", 9, "hebo", NAVY)
    x = text(q, 44, 194, "The prevalence of nodules rose with age.", 10)
    x = text(q, x, 194, " ", 10)        # a space set in a text object of its own
    sup(q, x, 194, "1")
    yy = para(q, 44, 206, ["Women had nodules more often than men did in",
                           "every age group of the survey."], 10, 12)
    # V4.17: two citations linked as one by the file ("2 3"), and a range
    x = 44 + pymupdf.get_text_length("every age group of the survey.", fontname="tiro", fontsize=10)
    x2 = sup(q, x, 218, "2 3")
    q.insert_link({"kind": pymupdf.LINK_GOTO, "page": 1, "to": pymupdf.Point(320, 74),
                   "from": pymupdf.Rect(x - 0.5, 209.5, x2 + 0.5, 216)})
    x3 = text(q, x2, 218, " Older too.", 10)
    x4 = sup(q, x3, 218, "1-3")         # a range (the base-14 font has no en dash)
    q.insert_link({"kind": pymupdf.LINK_GOTO, "page": 1, "to": pymupdf.Point(320, 74),
                   "from": pymupdf.Rect(x3 - 0.5, 209.5, x4 + 0.5, 216)})
    para(q, 54, yy, BODY[:1], 10, 12)
    para(q, 44, yy + 12, BODY[1:40], 10, 12)
    para(q, 320, 150, BODY[40:88], 10, 12)
    # references, numbered with bare numbers
    text(q, 320, 60, "REFERENCES", 9, "hebo", NAVY)
    yr = 74
    for n, lines in (("1", ["Haugen BR, Alexander EK. American Thyroid", "Association guidelines. Thyroid 2016;26:1–133."]),
                     ("2", ["Fisher SB, Perrier ND. The incidental thyroid", "nodule. CA Cancer J Clin 2018;68:97–105."]),
                     ("3", ["Durante C, Grani G. The diagnosis of thyroid", "nodules: a review. JAMA 2018;319:914–24."])):
        text(q, 326, yr, n, 7.5, "helv")
        yr = para(q, 336, yr, lines, 7.5, 9.0, "helv")
    footer(q, 2)
    stamp(q)
    d.save(path)
    d.close()


def tree(m):
    out = []
    for n in m.ordered_nodes():
        if not n.artifact:
            out.append(n)
    return out


def formula_speech(bad: list) -> None:
    """A displayed equation read from its layout: the stacked fraction is
    "one-fifth", the lowered i is "sub i" (the client's Formula /ActualText)."""
    from core.mathspeak import speak_region
    d = pymupdf.open()
    p = d.new_page(width=300, height=200)
    x = text(p, 40, 100, "CCI = ", 10)
    text(p, x + 1, 95, "1", 8)
    p.draw_line((x, 97.6), (x + 6, 97.6), color=(0, 0, 0), width=0.4)
    text(p, x + 1, 105, "5", 8)
    x = text(p, x + 9, 100, "(FP + V", 10)
    x = text(p, x, 102, "i", 7)
    x = text(p, x + 2, 100, "+ ", 10)
    text(p, x, 100, "ANC4", 8)
    p.draw_line((x, 102), (x + 24, 102), color=(0, 0, 0), width=0.4)
    text(p, x + 9, 110, "2", 8)
    text(p, x + 27, 100, ")", 10)
    said, linear = speak_region(p, pymupdf.Rect(30, 80, 260, 118))
    want = "CCI equals one-fifth times the sum of FP, V sub i, and ANC4 over 2."
    if said != want:
        bad.append(f"formula speech: {said!r} ({linear!r})")
    d.close()
    # read as a sentence: averages, the sum of a bracket, the natural log, a
    # subscript outside the bracket of one name
    from core.mathspeak import say_tree
    for parts, want in (
            ([("t", "CCI = "), ("frac", [("t", "1")], [("t", "5")]), ("t", " (FP + "),
              ("frac", [("t", "ANC4+SBA")], [("t", "2")]), ("t", " + "),
              ("frac", [("t", "BCG+2×DPT3+MSL")], [("t", "4")]), ("t", " + LLIN + TB)")],
             "CCI equals one-fifth times the sum of FP, the average of ANC4 and SBA, the average of "
             "BCG plus two DPT3 plus MSL, LLIN, and TB."),
            ([("t", "ln (output)"), ("sub", "i"), ("t", " = ln(HF"), ("sub", "i"), ("t", ") + (V"), ("sub", "i"),
              ("t", " − µ"), ("sub", "i"), ("t", ")")],
             "The natural log of output sub i equals the natural log of HF sub i plus open bracket "
             "V sub i minus mu sub i close bracket."),
            ([("t", "E = mc"), ("sup", "2")], "E equals mc squared.")):
        got = say_tree(parts)
        if got != want:
            bad.append(f"formula sentence: {got!r}")


def shared_text_object(bad: list) -> None:
    """The last line of a title and the authors in one text object (as
    InDesign writes them): the title is still one content item, and the page
    renders to the pixel."""
    from core.model import DocModel, StructNode
    from core.tagger import PageTagger
    from tests.verify import render_diff
    tmp = tempfile.mkdtemp()
    src, out = os.path.join(tmp, "t.pdf"), os.path.join(tmp, "t_out.pdf")
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(400, 400))
    pg = pdf.pages[0]
    pg.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica,
        Encoding=pikepdf.Name.WinAnsiEncoding)))
    pg.Contents = pdf.make_stream(
        b"BT /F1 16 Tf 14 TL 1 0 0 1 40 340 Tm (A title set over) Tj T* (three lines of) Tj ET\n"
        b"BT /F1 16 Tf 1 0 0 1 40 308 Tm (type) Tj 0 0 1 rg /F1 9 Tf 1 0 0 1 40 280 Tm "
        b"(Ana Lee, Bo Chen) Tj 0 g ( ) Tj ET\n")
    pdf.save(src)
    m = DocModel(path=src, page_count=1)
    h1 = StructNode(id="h1", role="H1", page=0, bbox=(38, 302, 300, 358), text="A title set over three lines of type")
    au = StructNode(id="au", role="P", page=0, bbox=(38, 276, 300, 292), text="Ana Lee, Bo Chen")
    m.add(h1)
    m.add(au)
    pdf = pikepdf.open(src)
    r = PageTagger(pdf, 0, m, import_mode=False, join=True).run()
    pdf.save(out)
    if r.mcid_owner.count("h1") != 1 or r.mcid_owner.count("au") != 1:
        bad.append(f"shared text object: content items {r.mcid_owner} (the title should be one)")
    if any(render_diff(src, out, zoom=2)):
        bad.append("shared text object: the page does not render the same after the split")


def edited(bad: list, job: str, o) -> None:
    """In a Kriyadocs job, a table drawn by hand comes out the client's way too
    (Caption before it, TR straight under it, its notes as Note > Lbl, the
    marks in its cells linked again), and so does a drawn Note."""
    from core import edit, house
    from core.model import unwrap_table_bodies
    from core.pipeline import load_model
    m = load_model(job)
    work = os.path.join(job, "working.pdf")
    d = pymupdf.open(work)
    try:
        cap = d[1].search_for("Table 1")[0]
        note = d[1].search_for("*T-test.")[0]
    finally:
        d.close()
    before = {k: n.role for k, n in m.nodes.items()}
    tid = edit.create_from_rect(m, work, 1, [36, cap.y0 - 2, 304, note.y1 + 2], "Table", True, o)
    unwrap_table_bodies(m)
    changed = {k for k, n in m.nodes.items() if before.get(k) != n.role}
    gone = {k for k, r in before.items() if r == "Link" and k not in m.nodes}
    house.after_edit(m, work, o, changed, gone=gone)
    t = m.nodes[tid]
    sib = m.siblings(tid)
    at = sib.index(tid)
    kids = {m.nodes[c].role for c in t.children}
    prev = m.nodes[sib[at - 1]] if at else None
    after = [m.nodes[x] for x in sib[at + 1:at + 4]]
    if kids != {"TR"}:
        bad.append(f"drawn table: {kids} under the Table")
    if prev is None or prev.role != "Caption" or not prev.text.startswith("Table 1"):
        bad.append(f"drawn table: no Caption before it ({prev.role if prev else None})")
    tn = [x for x in after if x.role == "Note" and x.text.startswith("T-test")]
    if not tn or [m.nodes[c].text for c in tn[0].children if m.nodes[c].role == "Lbl"] != ["*"]:
        bad.append(f"drawn table: its note is not Note > Lbl ({[(x.role, x.text[:12]) for x in after]})")
    star = [ln for ln in m.nodes.values() if ln.role == "Link" and ln.text.strip() == "*"
            and tid in {a.id for a in m.ancestors(ln.id)}]
    if not star or m.nodes[star[0].parent].role != "Reference":
        bad.append("drawn table: the * in its cell is not linked again")


def main() -> int:
    bad: list = []
    formula_speech(bad)
    shared_text_object(bad)
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "bmjopen.pdf")
    article(src)
    if "Kriyadocs" not in BUILTIN_PROFILES:
        print("kriyadocs test: FAILED — no built-in Kriyadocs profile")
        return 1
    o = profile_options("Kriyadocs")
    if o.house_style != "kriyadocs" or o.table_thead_tbody or o.section_wrap != "one" \
            or o.bibliography_as != "plain" or o.table_strategy != "journal":
        bad.append(f"profile options: {o.house_style}, thead={o.table_thead_tbody}, "
                   f"{o.section_wrap}, {o.bibliography_as}, {o.table_strategy}")
    o.run_verapdf = False
    o.ai_alt_text_auto = False
    job = os.path.join(tmp, "job")
    res = process_file(src, job, o)
    if res.get("status") != "done":
        print("kriyadocs test: FAILED —", res.get("error"))
        return 1
    m = load_model(job)

    # ---- page 1, in reading order
    top = [n for n in m.ordered_nodes() if n.parent is None and not n.artifact]
    seq = [(n.role, (n.text or "").strip()) for n in top]
    roles = [r for r, _ in seq]
    # "Open access" and "Original research" printed on top: a P each
    want_start = ["P", "P", "Figure", "H1", "P"]
    if roles[:5] != want_start or seq[0][1] != "Open access" or seq[1][1] != "Original research":
        bad.append(f"page 1 opens with {seq[:5]}")
    top = top[1:]          # from here on as the client's sample (no "Open access" bar there)
    seq = seq[1:]
    logo = top[1] if len(top) > 1 else None
    if logo is None or logo.alt != "BMJ Open":
        bad.append(f"the journal logo: {logo.role if logo else None} alt={logo.alt if logo else None!r}")
    authors = top[3] if len(top) > 3 else None
    if authors is not None:
        kids = [m.nodes[c] for c in authors.children]
        if not any(k.role == "Figure" and k.alt == "Orcid icon" for k in kids):
            bad.append(f"no ORCID icon Figure in the authors' P: {[k.role for k in kids]}")
        refs = [k for k in kids if k.role == "Reference"]
        if not refs or not all(any(m.nodes[c].role == "Link" for c in r.children) for r in refs):
            bad.append("the authors' links are not Reference > Link")
    texts = [t for _, t in seq]
    order = ["To cite:", "Received 20 October 2022", "Accepted 18 October 2023", "© Author(s)",
             "Department of Surgery", "University of Somewhere", "Correspondence to", "ABSTRACT",
             "STRENGTHS AND LIMITATIONS", "INTRODUCTION"]
    at = []
    for w in order:
        k = next((i for i, t in enumerate(texts) if t.startswith(w)), None)
        at.append(k)
    if None in at or at != sorted(at):
        bad.append(f"page 1 order: {list(zip(order, at))}")
    else:
        if top[at[4]].role != "Note" or top[at[5]].role != "Note":
            bad.append(f"affiliations are {top[at[4]].role}/{top[at[5]].role}, not Note")
        else:
            for k, want in ((4, "1"), (5, "2")):
                lbl = [m.nodes[c] for c in top[at[k]].children if m.nodes[c].role == "Lbl"]
                if len(lbl) != 1 or lbl[0].text != want:
                    bad.append(f"affiliation {want}: Note without Lbl {want!r} ({[x.text for x in lbl]})")
        if top[at[8]].role != "H3" or top[at[8] + 1].role != "L" or top[at[8] + 1].list_numbering != "Disc":
            bad.append(f"the box: {top[at[8]].role}, {top[at[8] + 1].role} {top[at[8] + 1].list_numbering}")
        if top[at[7]].role != "H2" or top[at[9]].role != "H2":
            bad.append("ABSTRACT / INTRODUCTION are not H2")
        cor = top[at[6]]
        mails = [d for d in m.descendants(cor.id) if d.role == "Link"]
        if "ana.lee@example.org" not in cor.text or not mails \
                or any(m.nodes[d.parent].role == "Reference" for d in mails):
            bad.append(f"correspondence: {cor.text!r} — the e-mail is not a plain Link (no Reference)")
    # the link's description goes in the annotation (/Contents), not as /Alt on the tag
    cm = [n for n in top if n.role == "P" and any(
        d.role == "Link" and (d.style or {}).get("contents") == "Check for updates" and not d.alt
        and m.nodes[d.parent].role == "P" for d in m.descendants(n.id))]
    if not cm:
        bad.append("Check for updates is not P > Link (a web link, no Reference)")
    intro = [n for n in top if n.role == "P" and "Thyroid nodules are discrete" in n.text]
    if not intro or "both sexes in every region" not in intro[0].text:
        bad.append("the introduction paragraph is not one P over the two columns")
    foot = [i for i, n in enumerate(top) if n.role == "Figure" and n.alt == "BMJ Group Logo"]
    if not foot:
        bad.append("no footer logo Figure")
    else:
        i = foot[0]
        nxt = top[i + 1] if i + 1 < len(top) else None
        if nxt is None or nxt.role != "P" or not any(m.nodes[c].role == "Link" for c in nxt.children):
            bad.append("the footer logo's link is not P > Link after it")
        if any(n.page == 0 for n in top[i + 2:] if n.role not in ("Reference",)):
            bad.append("the footer logo is not read at the end of page 1")
    if any(n.page == 0 and "first published as" in (n.text or "") for n in tree(m)):
        bad.append("the download stamp is read")

    # ---- page 2: table, notes, references
    cap = [n for n in top if n.role == "Caption" and n.text.startswith("Table 1")]
    tbl = [n for n in top if n.role == "Table"]
    if not cap or not tbl or top.index(cap[0]) + 1 != top.index(tbl[0]):
        bad.append("the table's Caption does not stand right before the Table")
    if tbl:
        t = tbl[0]
        kids = [m.nodes[c].role for c in t.children]
        if set(kids) != {"TR"}:
            bad.append(f"the Table holds {set(kids)}, not TR straight under it")
        rows = [m.nodes[c] for c in t.children]
        if len(rows) != 4 or any(len(r.children) != 3 for r in rows):
            bad.append(f"the table grid: {[len(r.children) for r in rows]}")
        elif {m.nodes[c].role for c in rows[0].children} != {"TH"}:
            bad.append(f"the header row is not TH (the empty header cell too): "
                       f"{[(m.nodes[c].role, m.nodes[c].text) for c in rows[0].children]}")
    notes = [n for n in top if n.role == "Note" and n.text.startswith("T-test")]
    if not notes:
        bad.append("the table note is not a Note")
    elif [m.nodes[c].text for c in notes[0].children if m.nodes[c].role == "Lbl"] != ["*"]:
        bad.append("the table note has no Lbl '*'")
    refh = [i for i, n in enumerate(top) if n.role == "H2" and n.text == "REFERENCES"]
    if not refh or top[refh[0] + 1].role != "L":
        bad.append("REFERENCES is not H2 + L")
    else:
        L = top[refh[0] + 1]
        items = [m.nodes[c] for c in L.children]
        shape = [[m.nodes[c].role for c in li.children] for li in items]
        if len(items) != 3 or any(s != ["Lbl", "LBody"] for s in shape) \
                or [m.nodes[li.children[0]].text for li in items] != ["1", "2", "3"]:
            bad.append(f"reference list: {shape}")
        if any(d.role == "BibEntry" for d in m.descendants(L.id)):
            bad.append("the reference list carries BibEntry tags")
    # note links: each author's affiliation mark goes to its Note, the table's
    # "*" to the table's note — both Reference > Link, with a name that says so
    notes_of = {}
    for ln in [n for n in m.nodes.values() if n.role == "Link"]:
        x = (ln.style or {}).get("xref") or {}
        if x.get("kind") == "footnote" and x.get("node") in m.nodes:
            notes_of.setdefault(m.nodes[x["node"]].text, []).append(ln)
    aff = [ln for t, ls in notes_of.items() if t.startswith("Department of Surgery") for ln in ls]
    if not aff or not all((ln.style or {}).get("contents", "").startswith("Affiliation 1") for ln in aff):
        bad.append(f"affiliation mark links: {[(t[:20], len(ls)) for t, ls in notes_of.items()]}")
    tn = [ln for t, ls in notes_of.items() if t.startswith("T-test") for ln in ls]
    if not tn or not any(a.role == "TD" for a in m.ancestors(tn[0].id)) \
            or (tn[0].style or {}).get("contents") != "Table note *: T-test.":
        bad.append("the table's * is not a link to its note inside the cell")
    import types as _types
    from core import house as _house
    _k = _house.Kriyadocs(_types.SimpleNamespace(model=m, doc=pymupdf.open(os.path.join(job, "working.pdf")),
                                                 pdf=pikepdf.open(os.path.join(job, "working.pdf")),
                                                 page_lines=[], opts=o))
    for ln in [n for n in m.nodes.values() if n.role == "Link" and not n.artifact]:
        par = m.nodes.get(ln.parent) if ln.parent else None
        if (ln.style or {}).get("no_reference"):
            continue
        if _k.is_web(ln):
            if par is not None and par.role == "Reference":
                bad.append(f"a web / e-mail Link inside a Reference: {ln.text!r}")
                break
            continue
        if par is None or par.role != "Reference":
            bad.append(f"a Link outside a Reference: {ln.text!r} under {par.role if par else 'the document'}")
            break
    # citations: "2 3" → two links, each in its own Reference; "1–3" one link
    cites = {(ln.text or "").strip(): ln for ln in m.nodes.values()
             if ln.role == "Link" and ln.page == 1 and (ln.text or "").strip() in ("2", "3", "1-3")}
    if set(cites) != {"2", "3", "1-3"}:
        bad.append(f"citation links: {sorted(cites)} (want 2, 3 and 1–3 apart)")
    else:
        if any(m.nodes[c.parent].role != "Reference" for c in cites.values()):
            bad.append("a citation link is not Reference > Link")
        if cites["2"].parent == cites["3"].parent:
            bad.append("the citations 2 and 3 share one Reference")
        x3 = (cites["3"].style or {}).get("xref") or {}
        if x3.get("page") != 1 or not x3.get("node"):
            bad.append(f"citation 3 does not go to its reference entry: {x3}")

    # ---- the built file
    out = os.path.join(job, "accessible.pdf")
    errs = [f"{f.code}: {f.detail[:50]}" for f in check(out) if f.severity == "error"]
    if errs:
        bad.append(f"checker errors: {errs}")
    if structure_integrity(out):
        bad.append(f"structure integrity: {structure_integrity(out)[:3]}")
    if mc_balance(out):
        bad.append("marked content unbalanced")
    pdf = pikepdf.open(out)
    try:
        if pdf.pdf_version < "1.6":
            bad.append(f"PDF version {pdf.pdf_version}, the client's files are 1.6 at least")
        roles_of: dict = {}

        def walk(e, parent):
            k = e.get("/K")
            ks = list(k) if isinstance(k, pikepdf.Array) else ([k] if k is not None else [])
            s = str(e.get("/S"))
            if s == "/Link" and "/Alt" in e:
                bad.append(f"a Link carries /Alt {str(e.Alt)!r}")
            if s == "/Lbl":
                roles_of.setdefault(parent, 0)
                roles_of[parent] += 1
            for c in ks:
                if isinstance(c, pikepdf.Dictionary) and "/S" in c:
                    walk(c, s)
        walk(pdf.Root.StructTreeRoot.K if not isinstance(pdf.Root.StructTreeRoot.K, pikepdf.Array)
             else pdf.Root.StructTreeRoot.K[0], None)
        if not roles_of.get("/Note"):
            bad.append("no Note > Lbl in the built file")
        # every Note: Lbl (the number or mark) + P (the text), nothing else, no loose text
        def kids_of(e):
            k = e.get("/K")
            return list(k) if isinstance(k, pikepdf.Array) else ([k] if k is not None else [])

        def notes_in(e, out):
            if str(e.get("/S")) == "/Note":
                out.append(e)
            for c in kids_of(e):
                if isinstance(c, pikepdf.Dictionary) and "/S" in c:
                    notes_in(c, out)
        notes_built: list = []
        notes_in(pdf.Root.StructTreeRoot.K[0] if isinstance(pdf.Root.StructTreeRoot.K, pikepdf.Array)
                 else pdf.Root.StructTreeRoot.K, notes_built)
        for ne in notes_built:
            ks = kids_of(ne)
            sub = [str(c.S) for c in ks if isinstance(c, pikepdf.Dictionary) and "/S" in c]
            loose = [c for c in ks if isinstance(c, int) or (isinstance(c, pikepdf.Dictionary) and "/S" not in c)]
            if sub != ["/Lbl", "/P"] or loose:
                bad.append(f"{ne.get('/ID')}: a Note is {sub} with {len(loose)} loose content item(s), "
                           "not Lbl + P")
        near = [a for a in pdf.pages[1].obj.get("/Annots", [])
                if 205 < float(pdf.pages[1].mediabox[3]) - float(a.Rect[3]) < 222]
        rects = sorted([round(float(v), 1) for v in a.Rect] for a in near)
        if len(rects) != 3 or any(r1[0] < r0[2] - 0.5 for r0, r1 in zip(rects, rects[1:])):
            bad.append(f"citation annotations on the 2 3 / 1–3 line: {rects} (want 3 apart)")
        stamps = [a for pg in pdf.pages for a in pg.obj.get("/Annots", [])
                  if float(a.Rect[0]) > 570]
        if stamps:
            bad.append(f"the download stamp's link is still there ({len(stamps)})")
        # white space on its own is no content item: it goes with its text, or is an artifact
        blank = 0
        for pg in pdf.pages:
            stack, txt = [], {}
            for operands, op in pikepdf.parse_content_stream(pg):
                o = str(op)
                if o == "BDC":
                    pr = operands[1]
                    mc = int(pr.MCID) if isinstance(pr, pikepdf.Dictionary) and "/MCID" in pr else None
                    stack.append(mc)
                    if mc is not None:
                        txt[mc] = ""
                elif o == "BMC":
                    stack.append(None)
                elif o == "EMC":
                    if stack:
                        stack.pop()
                elif o in ("Tj", "TJ", "f", "f*", "S", "Do", "B", "b", "sh"):
                    cur = next((x for x in reversed(stack) if x is not None), None)
                    if cur is None:
                        continue
                    if o in ("Tj", "TJ"):
                        arr = operands[-1]
                        t = b"".join(bytes(x) for x in arr if isinstance(x, pikepdf.String)) \
                            if isinstance(arr, pikepdf.Array) else bytes(arr)
                        txt[cur] += t.decode("latin-1")
                    else:
                        txt[cur] += "#"
            blank += sum(1 for t in txt.values() if not t.replace(" ", ""))
        if blank:
            bad.append(f"{blank} content items hold nothing but a space")
        doc = pdf.Root.StructTreeRoot.K
        doc = doc[0] if isinstance(doc, pikepdf.Array) else doc
        kids = doc.K if isinstance(doc.K, pikepdf.Array) else [doc.K]
        sects = [k for k in kids if isinstance(k, pikepdf.Dictionary) and k.get("/S") == "/Sect"]
        if str(doc.get("/S")) != "/Document" or len(sects) != 1 or len(kids) != 1:
            bad.append(f"the tree is not Document > one Sect ({[str(k.get('/S')) for k in kids][:5]})")
    finally:
        pdf.close()

    edited(bad, job, profile_options("Kriyadocs"))

    # any other client: the Notes stay as V4 made them — no P wrapped round the text
    o2 = Options(fix_contrast="off")
    o2.run_verapdf = False
    o2.ai_alt_text_auto = False
    job2 = os.path.join(tmp, "job_v4")
    if process_file(src, job2, o2).get("status") == "done":
        for n in load_model(job2).nodes.values():
            if n.role == "Note" and any(load_model(job2).nodes[c].role == "P" for c in n.children):
                bad.append("V4 job: a Note got a P — Kriyadocs-only change leaked into other clients")
                break

    if bad:
        print("kriyadocs test: FAILED")
        for b in bad:
            print("   ", b)
        return 1
    print("kriyadocs test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
