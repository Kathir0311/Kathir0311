"""The offline rule book (V4.26).

When the tool is in doubt it looks things up here — not on the internet.
Nothing about a file (its text, its name, its client) ever leaves the
server: a search on the web would have to send the doubtful text out.

The book holds short summaries of the PDF/UA-1 (ISO 14289-1) requirements,
the WCAG 2.x success criteria they serve, and the tagging practice the
tool follows — written for the people who tag, with the clause to look up
in the standard itself. They are summaries, not the standards' text.
On top of that come the rules the local AI wrote while studying each
client's sample files (core/brain.study_sample), and any house rule the
admin adds.

Search is a small BM25 over titles, text and keywords, all in memory.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime

from . import learn as L

SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_custom (id TEXT PRIMARY KEY, title TEXT, text TEXT, roles TEXT, refs TEXT, client TEXT,
  created TEXT, author TEXT);
"""

# id, title, roles it concerns, references, text
BOOK: list[tuple[str, str, str, str, str]] = [
    ("ua-real-content", "Every piece of real content is tagged", "P H Figure Table L",
     "ISO 14289-1 7.1 · WCAG 1.3.1",
     "Everything that carries meaning — text, pictures, tables, form fields, links — must be inside a structure "
     "element of the tag tree, in the order it is meant to be read. Content that is not tagged is skipped or read "
     "out of order by a screen reader."),
    ("ua-artifacts", "Decoration and pagination are artifacts", "Artifact",
     "ISO 14289-1 7.1, 7.8 · WCAG 1.3.1",
     "Running heads and feet, page numbers, rules, background shapes, watermarks and purely decorative pictures are "
     "marked as artifacts so assistive technology does not read them. Artifacts carry a type (Pagination, Layout, "
     "Page, Background) and pagination artifacts a subtype (Header, Footer, Watermark). A page number some "
     "publishers want read becomes a P instead."),
    ("ua-standard-roles", "Tags are standard or role-mapped", "",
     "ISO 14289-1 7.1",
     "Every structure type is a standard type of ISO 32000-1 or is mapped to one in the RoleMap. A custom name "
     "without a mapping cannot be understood by assistive technology."),
    ("ua-lang", "The language is set", "P H1 Span",
     "ISO 14289-1 7.2 · WCAG 3.1.1, 3.1.2",
     "The document has a default natural language (/Lang in the catalog), and a passage in another language "
     "(a Tamil word in an English text, a Greek formula name) carries its own /Lang on its tag."),
    ("ua-unicode", "Every character maps to Unicode", "",
     "ISO 14289-1 7.21.7 · WCAG 1.3.1",
     "A screen reader can only say characters it can map to Unicode. Fonts need a ToUnicode map (or a standard "
     "encoding); private-use and U+FFFD characters are read as nothing or as noise. Symbol-font bullets and ticks "
     "need a real Unicode value."),
    ("ua-fonts", "Fonts are embedded", "",
     "ISO 14289-1 7.21.4",
     "The font programs of all fonts used to render text are embedded, so the text renders and is read the same "
     "everywhere. Fonts used only for invisible text (rendering mode 3, such as an OCR layer) are the exception."),
    ("ua-figure-alt", "Figures have alternative text", "Figure",
     "ISO 14289-1 7.3 · WCAG 1.1.1",
     "A Figure that carries information needs /Alt that says what it conveys, not what it looks like: short, "
     "factual, no “image of”. A decorative picture is an artifact, not a Figure without alt. A caption may help "
     "but does not replace the alt text."),
    ("ua-formula-alt", "Formulas have alternative text", "Formula",
     "ISO 14289-1 7.7 · WCAG 1.1.1",
     "Every Formula carries /Alt (or /ActualText) with the expression in words a screen reader can say: “a "
     "squared plus b squared equals c squared”."),
    ("ua-headings-nest", "Headings follow the outline and do not skip", "H1 H2 H3 H4 H5 H6",
     "ISO 14289-1 7.4 · Matterhorn 14-002, 14-003 · WCAG 1.3.1, 2.4.6",
     "Numbered headings H1–H6 reflect the logical outline. A level is never skipped going down (H2 is followed by "
     "H2 or H3, not H4), and the first numbered heading is H1. Do not mix H with H1–H6 in one file."),
    ("ua-heading-is-short", "A heading is a title, not a paragraph", "H1 H2 H3 H4 H5 H6 P",
     "WCAG 2.4.6, 1.3.1 · tool practice",
     "A heading names the text that follows it: short, usually one line, no run of full sentences. Text that is "
     "merely set large or bold — a pull quote, a grid of words, a reading passage in large print — is P, not a "
     "heading. Text set like the body but in capitals can be a heading when the publisher uses it as one."),
    ("ua-paragraph", "Running text is P, joined across breaks", "P",
     "WCAG 1.3.1, 1.3.2 · tool practice",
     "Each paragraph is one P, also when a column or page break cuts it. Labels and short lines that are not "
     "headings are P too."),
    ("ua-caption", "A caption belongs to its figure or table", "Caption Figure Table",
     "ISO 32000-1 14.8.4.2 · WCAG 1.3.1",
     "Caption is used only for the title or label printed with a figure or table, and sits next to it (or inside "
     "it, first or last, as the client's files do). “Table 2 shows …” inside running text is P."),
    ("ua-tables-headers", "Tables have header cells and regular rows", "Table TH TD TR",
     "ISO 14289-1 7.5 · WCAG 1.3.1",
     "A data table has TH cells for its headers with a Scope (Row, Column, Both) or Headers/ID associations for "
     "complex tables. Every row covers the same number of columns (RowSpan / ColSpan counted). A layout grid of "
     "words with no header has no TH. An empty TH is a TD."),
    ("ua-tables-groups", "THead, TBody and TFoot", "THead TBody TFoot",
     "ISO 32000-1 14.8.4.3",
     "Header rows may be grouped in THead, data rows in TBody, footer rows in TFoot. A table continued over pages "
     "is one Table; the repeated header on later pages is an artifact."),
    ("ua-lists", "Lists are L > LI > Lbl + LBody", "L LI Lbl LBody",
     "ISO 14289-1 7.6 · WCAG 1.3.1",
     "A list is L holding LI items; each item has its bullet or number in Lbl and its content in LBody. A list "
     "inside an item is an L inside the LBody. ListNumbering says how the items are numbered (Disc, Decimal, "
     "UpperAlpha …)."),
    ("ua-notes", "Notes and references", "Note Reference",
     "ISO 14289-1 7.9 · WCAG 1.3.1",
     "Footnotes and endnotes are tagged Note, each with a unique ID. A note mark in the text may be a Reference "
     "or a Link to its note."),
    ("ua-links", "Links are tagged with their annotation", "Link",
     "ISO 14289-1 7.18.5 · WCAG 2.4.4",
     "Every link annotation is inside a Link structure element (as an OBJR) together with its visible text, and "
     "carries a /Contents description that makes sense on its own. “Click here” alone does not."),
    ("ua-annotations", "Annotations are tagged or hidden", "Annot",
     "ISO 14289-1 7.18.1",
     "Annotations that are content are tagged (Annot, Link, Form); review marks that carry nothing are removed or "
     "made hidden. Pages with annotations use the structure order for tabbing (/Tabs /S)."),
    ("ua-tab-order", "Tab order follows the structure", "Form Link",
     "ISO 14289-1 7.18.3 · WCAG 2.4.3",
     "Every page that has annotations has /Tabs /S, so keyboard focus follows the tag tree."),
    ("ua-forms", "Form fields have a name a person understands", "Form",
     "ISO 14289-1 7.18.4 · WCAG 4.1.2, 3.3.2",
     "Each widget is inside a Form tag and the field has a /TU tooltip — the label a screen reader announces "
     "(“Student name”), taken from the text printed beside it."),
    ("ua-title", "The document has a title that is shown", "",
     "ISO 14289-1 7.1 · WCAG 2.4.2",
     "The XMP metadata holds dc:title, and /ViewerPreferences /DisplayDocTitle true makes viewers show it instead "
     "of the file name."),
    ("ua-identifier", "The file says it is PDF/UA", "",
     "ISO 14289-1 5",
     "The XMP metadata carries pdfuaid:part = 1, and /MarkInfo /Marked true says the file is tagged."),
    ("ua-bookmarks", "Long documents have bookmarks", "H1 H2 H3",
     "ISO 14289-1 7.17 · WCAG 2.4.5",
     "Documents with more than a few pages should have bookmarks (outlines) that follow the headings."),
    ("ua-reading-order", "The tag order is the reading order", "",
     "ISO 14289-1 7.1 · WCAG 1.3.2",
     "A screen reader follows the tag tree, not the page. Columns are read top to bottom one after the other, a "
     "caption with its figure, a sidebar after the text it sits beside unless it is meant to be read first, "
     "footnotes after the text."),
    ("ua-colour", "Meaning is not carried by colour alone", "",
     "ISO 14289-1 7.1 · WCAG 1.4.1",
     "Information shown only by colour (a letter printed red to mark a sound) must also be in the text or the "
     "/ActualText (“a highlighted in red”)."),
    ("ua-contrast", "Text has enough contrast", "",
     "WCAG 1.4.3 (AA 4.5:1, 3:1 large) · 1.4.6 (AAA 7:1)",
     "Text against its background needs a contrast ratio of at least 4.5:1 (3:1 for large text) at level AA."),
    ("ua-actualtext", "ActualText says what is printed differently", "Span",
     "ISO 32000-1 14.9.4 · WCAG 1.3.1",
     "/ActualText replaces the printed characters for reading: a write-in line read as “Blank line”, a "
     "letter-spaced logo read as its words, “Rr” read as “uppercase R, lowercase r”. Put it on a Span inside the "
     "tag that holds the content."),
    ("ua-abbreviation", "Abbreviations can be expanded", "Span",
     "ISO 32000-1 14.9.5 · WCAG 3.1.4",
     "/E on a Span gives the expansion of an abbreviation, read instead of the letters."),
    ("ua-optional-content", "Optional content has names", "",
     "ISO 14289-1 7.10",
     "Each optional content configuration has a /Name, and content in hidden layers is not relied upon."),
    ("ua-embedded-files", "Embedded files have a description", "",
     "ISO 14289-1 7.11",
     "Every embedded file specification has /F and /UF and, best, a /Desc."),
    ("ua-xfa", "No dynamic XFA", "Form",
     "ISO 14289-1 7.15",
     "A PDF/UA file does not use dynamic XFA forms."),
    ("ua-security", "Encryption does not block assistive technology", "",
     "ISO 14289-1 7.16",
     "If a file is encrypted, the permission for accessibility (content extraction for assistive technology) "
     "is granted."),
    ("ua-math-words", "Mathematics set as text is read in words", "Formula",
     "tool practice",
     "A line that is an equation becomes a Formula whose /Alt reads it aloud. Prose that mentions maths stays P."),
    ("ua-blank-lines", "Write-in lines are read", "P Span LBody",
     "WCAG 1.3.1 · tool practice",
     "An answer line (“1. ______”) is read as “Blank Line”; several lines for one answer as “Multiple Blank "
     "Lines”; numbered lines form a list."),
    ("ua-toc", "A contents list is TOC > TOCI with links", "TOC TOCI",
     "ISO 32000-1 14.8.4.2 · WCAG 2.4.5",
     "Each contents entry is a TOCI inside a TOC, and a Link to the page it names makes it usable."),
    ("ua-index", "An index is a list of entries with page links", "Index L LI",
     "ISO 32000-1 14.8.4.2 · tool practice",
     "Index > L > LI per entry, sub-entries as a list inside their entry; each page number a link."),
    ("ua-bibliography", "Each reference is one entry", "BibEntry L LI",
     "ISO 32000-1 14.8.4.4 · tool practice",
     "A reference list has one BibEntry (or one list item) per reference, found from the hanging indent or the "
     "number — not from where the paragraph breaks fell."),
    ("ua-sect", "Sections group a heading and its content", "Sect",
     "ISO 32000-1 14.8.4.2 · tool practice",
     "Sect may group a heading with everything under it; the client's practice decides (one Sect per H1, one for "
     "the whole article, or none)."),
    ("ua-empty-cells", "Empty table cells are kept", "TD TH",
     "ISO 14289-1 7.5 · tool practice",
     "An empty cell stays a TD so the table stays regular, and can be announced (“Blank cell”) with invisible "
     "text so it is associated with content."),
    ("ua-scanned", "Scanned pages need a text layer", "",
     "ISO 14289-1 7.1 · WCAG 1.1.1",
     "An image of text cannot be read. OCR adds the text layer that is then tagged."),
]


def ensure(c) -> None:
    c.executescript(SCHEMA)


def _tok(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 1]


def entries(client: str = "") -> list[dict]:
    out = [{"id": i, "title": t, "roles": r.split(), "refs": ref, "text": txt, "source": "rule book"}
           for i, t, r, ref, txt in BOOK]
    if L.has_data():
        try:
            with L.connect() as c:
                ensure(c)
                for r in c.execute("SELECT * FROM kb_custom"):
                    if r["client"] and client and r["client"] != client:
                        continue
                    out.append({"id": r["id"], "title": r["title"], "roles": (r["roles"] or "").split(),
                                "refs": r["refs"] or "house rule", "text": r["text"], "source": "house rule",
                                "client": r["client"] or ""})
                for r in c.execute("SELECT i.sample_id, i.client, i.rules, s.filename FROM insights i JOIN samples s "
                                   "ON s.id=i.sample_id WHERE s.enabled=1 AND i.rules IS NOT NULL AND i.rules != ''"):
                    if client and r["client"] and r["client"] != client:
                        continue
                    try:
                        rules = json.loads(r["rules"] or "[]")
                    except ValueError:
                        continue
                    for k, x in enumerate(rules[:10]):
                        t = (x.get("rule") if isinstance(x, dict) else str(x)) or ""
                        if t:
                            out.append({"id": f"ai-{r['sample_id']}-{k}", "title": t[:80], "roles": [x.get("tag", "")]
                                        if isinstance(x, dict) else [], "refs": f"learned from {r['filename']}",
                                        "text": t, "source": f"learned ({r['client'] or 'common'})"})
        except Exception:
            pass
    return out


def search(q: str, k: int = 6, client: str = "", roles: list[str] | None = None) -> list[dict]:
    docs = entries(client)
    qt = _tok(q) + [r.lower() for r in (roles or [])]
    if not qt:
        return docs[:k]
    toks = [_tok(d["title"] + " " + d["text"] + " " + " ".join(d["roles"]) + " " + d["refs"]) for d in docs]
    N = len(docs)
    avg = sum(len(t) for t in toks) / max(1, N)
    df: dict[str, int] = {}
    for t in toks:
        for w in set(t):
            df[w] = df.get(w, 0) + 1
    scored = []
    for d, t in zip(docs, toks):
        tf: dict[str, int] = {}
        for w in t:
            tf[w] = tf.get(w, 0) + 1
        s = 0.0
        for w in qt:
            if w not in tf:
                continue
            idf = math.log(1 + (N - df[w] + 0.5) / (df[w] + 0.5))
            s += idf * tf[w] * 2.2 / (tf[w] + 1.2 * (0.25 + 0.75 * len(t) / avg))
        if roles and any(r in d["roles"] for r in roles):
            s += 1.5
        if s > 0:
            scored.append((s, d))
    scored.sort(key=lambda x: -x[0])
    return [dict(d, score=round(s, 2)) for s, d in scored[:k]]


def for_roles(roles: list[str], k: int = 4) -> list[str]:
    """Short lines of the rule book for the AI's question about these tags."""
    hits = search(" ".join(roles) + " heading caption paragraph", k=k, roles=roles)
    return [f"{h['title']} ({h['refs']}): {h['text'][:220]}" for h in hits if h["source"] == "rule book"]


def add(title: str, text: str, roles: str = "", refs: str = "", client: str = "", author: str = "") -> str:
    import uuid
    rid = "house-" + uuid.uuid4().hex[:8]
    with L.connect() as c:
        ensure(c)
        c.execute("INSERT INTO kb_custom VALUES (?,?,?,?,?,?,?,?)",
                  (rid, title[:200], text[:2000], roles[:200], refs[:200], client[:80],
                   datetime.now().isoformat(timespec="seconds"), author[:80]))
    return rid


def remove(rid: str) -> bool:
    with L.connect() as c:
        ensure(c)
        return c.execute("DELETE FROM kb_custom WHERE id=?", (rid,)).rowcount > 0
