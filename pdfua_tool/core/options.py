"""Pipeline options and saved profiles.

Every option carries a label and a help line so the web UI and the CLI
can render them without duplicating text.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any


# announcement texts renamed in this release; saved profiles / old jobs still hold the old ones
LEGACY_TEXTS = {"Blank Box": "Blank cell", "Multiple Blank Line": "Multiple Blank Lines"}


def migrate_legacy_texts(d):
    """Rename the old default announcement texts in a saved options dict (exact match
    only, so custom texts are never touched)."""
    if not d:
        return d
    for k in ("empty_cell_text", "multi_blank_line_text"):
        if d.get(k) in LEGACY_TEXTS:
            d[k] = LEGACY_TEXTS[d[k]]
    return d


@dataclass
class Options:
    # ---- preflight ----------------------------------------------------------
    existing_tags: str = "ask"             # ask | auto | retag | import
    ocr: str = "auto"                      # auto | off | force
    ocr_lang: str = "eng"
    ocr_dpi: int = 300
    embed_fonts: bool = True
    repair_tounicode: bool = True
    flatten_forms: bool = True

    # ---- analysis -----------------------------------------------------------
    lang: str = "en-US"
    title_source: str = "metadata"         # metadata | heading | filename
    reading_order: str = "xycut"           # xycut | top_to_bottom
    headings_from_bookmarks: bool = True
    headings_from_numbering: bool = True
    max_heading_levels: int = 6
    detect_tables: bool = True
    table_strategy: str = "lines"          # lines | lines_then_text | journal
    table_headers_mode: str = "auto"       # auto | scope | ids
    table_thead_tbody: bool = True
    merge_continued_tables: bool = True    # one Table across pages when the header repeats
    running_title_artifact: bool = True    # repeated page title (same text, next page) -> artifact
    split_empty_merged_cells: bool = True  # empty cell over 2 columns -> 2 blank cells
    lists_in_cells: bool = True            # a numbered list printed inside a cell -> L > LI
    detect_lists: bool = True
    detect_vector_figures: bool = True
    decorative_image_min_pt: float = 14.0
    repeated_images_artifact: bool = True
    header_zone: float = 0.08
    footer_zone: float = 0.08
    detect_footnotes: bool = True
    detect_toc: bool = True
    detect_captions: bool = True
    detect_formulas: bool = True           # equations become Formula with a spoken /Alt
    join_spaced_letters: bool = True       # "B e n c h m a r k" -> read as the words
    join_split_lists: bool = True          # a note inside a list, items set further in
    detect_bibliography: bool = True       # reference list -> one BibEntry per entry
    bibliography_as: str = "list"          # list (L > LI > LBody > BibEntry) | entries | plain (L > LI > Lbl + LBody)
    detect_blank_lines: bool = True
    blank_lines_as_list: bool = True       # "1. ____" -> L > LI > Lbl + LBody
    join_split_paragraphs: bool = True
    script_language_tagging: bool = True
    check_contrast: bool = True
    contrast_level: str = "AA"             # AA | AAA
    contrast_large_text: str = "pac"       # pac (18pt) | wcag (18pt or 14pt bold)
    tag_links: bool = True
    tag_form_fields: bool = True
    tag_other_annotations: bool = True

    # ---- cross-references and links (core/xref.py) ---------------------------
    detect_cross_references: bool = True   # find "see Figure 3", URLs, contents entries …
    cross_reference_tags: bool = False     # wrap each mention in a Reference tag
    link_web: bool = True                  # bare URLs / e-mail addresses become links
    link_toc: bool = True                  # contents entries jump to their page
    link_sections: bool = False            # "Chapter 3", "Section 2.1", "Part II"
    link_figures_tables: bool = True       # "see Figure 3", "Table 2"
    link_references: bool = True           # "[12]", "Smith (2019)" -> bibliography
    link_index: bool = True                # index entries and "page 12" references
    link_footnotes: bool = True            # a footnote mark in the text -> its note
    link_back: bool = True                 # the note / the caption -> back to where the text refers to it
    link_direction: str = "one"            # one | two — a link set by hand in the Links stage
    detect_index: bool = True              # an Index section -> Index > L > LI, sub-entries nested
    empty_markup_annots: str = "remove"    # remove | keep — empty review marks (tiny Square/Circle/… with no comment)

    # ---- build --------------------------------------------------------------
    bookmarks: str = "headings"            # headings | keep | off
    bookmark_levels: int = 3
    link_contents: str = "text"            # text | uri
    form_tooltips_from_labels: bool = True
    unmatched_content: str = "nearest"     # nearest | artifact
    figure_bbox_attribute: bool = True
    section_wrap: str = "h1"               # off | h1 | all | one — Sect around each heading
    page_title_first: str = "margin"       # off | margin | first — a page opens with its heading
    page_numbers: str = "artifact"         # artifact | P — a printed page number
    page_number_text: str = "Page"         # a page number read as P says "Page 5"
    fix_contrast: str = "all"              # all | selected | off
    contrast_fix_method: str = "auto"      # auto | background | text | stroke
    contrast_stroke_width: float = 0.5     # outline width in pt (method stroke)
    contrast_stroke_color: str = "black"   # black | white | auto
    normalize_backgrounds: bool = True     # trim fills hidden under coloured boxes
    empty_cell_text: str = "Blank cell"     # announced for empty table cells ("" = silent)
    empty_cell_method: str = "text"        # text | actualtext
    table_spans: str = "keep"              # keep | pad — merged cells, or one cell per grid position
    join_marked_content: bool = False      # one marked-content sequence per tag across text objects
    pdf_min_version: str = "auto"          # auto | 1.6 | 1.7 — the version the built file declares
    blank_line_text: str = "Blank Line"    # announced for write-in lines ("" = silent)
    multi_blank_line_text: str = "Multiple Blank Lines"   # a blank made of several lines
    underscore_tick_text: str = "Underscore"   # the short "__73" tick in a margin
    run_verapdf: bool = True

    # ---- AI -----------------------------------------------------------------
    ai_alt_text_auto: bool = False
    ai_table_headers: bool = False
    ai_table_merge: bool = False           # text model decides doubtful page-to-page table joins
    ai_table_merge_vision: bool = False    # ... looking at the page images too (vision model)
    ai_table_check: bool = False           # vision model decides doubtful table rows
    ai_bibliography: bool = False          # text model splits a reference list with no clear layout

    # ---- client -------------------------------------------------------------
    house_style: str = "none"              # none | kriyadocs — a publisher's own tag structure (core/house.py)
    teachers_guide: str = "off"            # off | auto | on — the Academain Teacher's Guide structure (core/tguide.py, V4.24)
    actualtext_span: bool = True           # /ActualText on a Span made inside the tag, not on the tag (every client)
    use_samples: bool = True               # V4.20: tags the analysis doubts take the role the client's samples give them
    client: str = ""                       # V4.20: whose samples (empty = the profile's name)
    ai_samples: bool = True                # V4.21: what the samples cannot settle goes to the AI, with the client's examples
    sample_rules: bool = True              # V4.21: the client's structure from the samples (where captions go)
    samples_scope: str = "client"          # V4.25: client (its own + common samples) | all (every client's, the most similar files count most)
    ai_brain: bool = False                 # V4.25: the self-learning brain decides every text tag (core/brain.py)
    ai_brain_max: int = 45                 # V4.25: at most this many doubtful tags per file go to the AI
    brain_strength: str = "balanced"       # V4.25: careful | balanced | bold — how sure the brain must be to change a tag
    learn_feedback: bool = False           # V4.25 (set by the app): a built file's fixes teach the brain

    # ---- mode ---------------------------------------------------------------
    auto_build: bool = False               # skip manual review (batch)
    opts_version: int = 7                  # internal: migrates old saved defaults

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Options":
        o = cls()
        if not d:
            return o
        d = migrate_legacy_texts(dict(d))
        for f in fields(cls):
            if f.name in d and d[f.name] is not None:
                v = d[f.name]
                try:
                    if f.type in ("bool", bool):
                        v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on", "yes")
                    elif f.type in ("int", int):
                        v = int(v)
                    elif f.type in ("float", float):
                        v = float(v)
                    else:
                        v = str(v)
                except Exception:
                    continue
                setattr(o, f.name, v)
        # v1 saved "actualtext" as the default; it fails PAC "Associated with content"
        ver = int(d.get("opts_version") or 1)
        if ver < 2 and o.empty_cell_method == "actualtext":
            o.empty_cell_method = "text"
        # v2 defaulted to fixing only ticked contrast issues; PAC-clean output needs all
        if ver < 3 and o.fix_contrast == "selected":
            o.fix_contrast = "all"
        # v3 left table rows directly under the Table; every table needs a TBody
        if ver < 4 and not o.table_thead_tbody:
            o.table_thead_tbody = True
        # v4 left citations unlinked by default; "(Hasbrouck, 2010)" now jumps to its entry
        if ver < 5 and not o.link_references:
            o.link_references = True
        # v5 left figure/table mentions and index page numbers unlinked
        if ver < 6:
            o.link_figures_tables = True
            o.link_index = True
        # v6 wrote /ActualText on a Span for the Academain profile only; every
        # client's files are built that way now (V4.16)
        if ver < 7 and not o.actualtext_span:
            o.actualtext_span = True
        o.opts_version = 7
        return o


# label, help, group, choices (None = free / bool)
META: dict[str, tuple[str, str, str, list[str] | None]] = {
    "existing_tags": ("Existing tags", "ask: when the file already has tags, the tool asks whether to load them or analyse the file · auto: keep good tags, re-tag weak ones · retag: start from scratch · import: keep & edit the tags that are already there", "Preflight", ["ask", "auto", "retag", "import"]),
    "ocr": ("OCR scanned pages", "Adds an invisible text layer to image-only pages (needs Tesseract)", "Preflight", ["auto", "off", "force"]),
    "ocr_lang": ("OCR language", "Tesseract language codes, e.g. eng, eng+tam", "Preflight", None),
    "ocr_dpi": ("OCR resolution (dpi)", "300 is a good default", "Preflight", None),
    "embed_fonts": ("Embed standard fonts", "Embeds metric-compatible fonts for non-embedded Helvetica/Times/Courier/Arial/Symbol", "Preflight", None),
    "repair_tounicode": ("Repair Unicode mapping", "Adds ToUnicode maps where they can be derived with certainty", "Preflight", None),
    "flatten_forms": ("Unpack form XObjects", "Lets text drawn inside form XObjects be tagged word by word", "Preflight", None),
    "lang": ("Document language", "BCP 47 tag, e.g. en-US, en-GB, ta-IN", "Analysis", None),
    "title_source": ("Title source", "Where the document title comes from when none is set", "Analysis", ["metadata", "heading", "filename"]),
    "reading_order": ("Reading order", "xycut handles columns, sidebars and full-width figures", "Analysis", ["xycut", "top_to_bottom"]),
    "headings_from_bookmarks": ("Use bookmarks for heading levels", "Existing outline entries decide heading levels", "Analysis", None),
    "headings_from_numbering": ("Use numbering for heading levels", "1.2.3 style numbering votes for level 3", "Analysis", None),
    "max_heading_levels": ("Max heading levels", "Deeper styles collapse into the last level", "Analysis", None),
    "detect_tables": ("Detect tables", "", "Analysis", None),
    "table_strategy": ("Table detection", "lines: ruled tables only · lines_then_text: also try borderless tables · journal: tables ruled with horizontal lines or shaded rows only (InDesign journals) — rows from the rules, columns from the gaps between the words, a group label spanning its rows, the caption above and the notes below kept out of the table", "Analysis", ["lines", "lines_then_text", "journal"]),
    "table_headers_mode": ("Header association", "auto: Scope for simple tables, IDs/Headers for tables with merged cells", "Analysis", ["auto", "scope", "ids"]),
    "table_thead_tbody": ("Group rows in THead/TBody", "The header rows of a table go in a THead and the data rows in a TBody. A table with no header gets no THead — but it still gets the TBody", "Analysis", None),
    "merge_continued_tables": ("Merge tables continued across pages", "A table that continues on the next page with the same column header becomes ONE Table tag. The header row is tagged (TH) on the first page only; the repeated header rows on the following pages become artifacts (running table heading)", "Analysis", None),
    "running_title_artifact": ("Repeated page title is a running head", "The same title line on the next pages of a continued table is an artifact; only the first one stays a heading", "Analysis", None),
    "lists_in_cells": ("Lists inside table cells", "A numbered or bulleted list printed inside one cell (a rubric score key, a set of instructions) is tagged L > LI > Lbl + LBody inside that cell instead of one block of text", "Analysis", None),
    "split_empty_merged_cells": ("Split empty merged cells", "An empty cell spanning several columns/rows becomes one blank cell per column/row (each read as 'Blank cell')", "Analysis", None),
    "detect_lists": ("Detect lists", "Bulleted and numbered lists, nested by indent, with Lbl/LBody", "Analysis", None),
    "detect_vector_figures": ("Detect vector figures", "Charts and diagrams drawn with lines rather than images", "Analysis", None),
    "decorative_image_min_pt": ("Decorative image size (pt)", "Images smaller than this are marked as artifacts", "Analysis", None),
    "repeated_images_artifact": ("Repeated images are artifacts", "Logos and ornaments that repeat across pages", "Analysis", None),
    "header_zone": ("Header zone (fraction of page)", "", "Analysis", None),
    "footer_zone": ("Footer zone (fraction of page)", "", "Analysis", None),
    "detect_footnotes": ("Detect footnotes", "Tagged as Note with a unique ID", "Analysis", None),
    "detect_toc": ("Detect table of contents", "Dot-leader lines become TOC / TOCI", "Analysis", None),
    "detect_captions": ("Detect captions", "Figure N / Table N lines become Caption", "Analysis", None),
    "detect_formulas": ("Detect formulas", "A line that is an equation (a² + b² = c², 3 × 4 ÷ 2) becomes a Formula tag, and its /Alt is written out in words (“a squared plus b squared equals c squared”) so a screen reader can read it — PDF/UA needs an /Alt on every Formula. Check the wording in the editor", "Analysis", None),
    "join_spaced_letters": ("Read letter-spaced lines as words", "A line tracked out letter by letter (\"B e n c h m a r k   E d u c a t i o n\") is read as the words it spells, through its /ActualText; the page is not changed", "Analysis", None),
    "join_split_lists": ("Keep a list together", "A list printed with a note in the middle of it stays one list (the note goes inside the item it belongs to), and points set further in than the item above them become a list inside it", "Analysis", None),
    "detect_bibliography": ("Tag the reference list", "The entries under a References / Bibliography / Works Cited heading become one BibEntry each — found from the hanging indent, the label (“1.”, “[1]”), the space between entries and the author pattern, not from where the paragraph breaks fell — so an entry whose first line ends with “D.” is not cut in two. Citations such as “(Hasbrouck, 2010)” then link to their entry when “Link bibliography references” is on", "Analysis", None),
    "bibliography_as": ("Reference list structure", "list: L > LI > LBody > BibEntry, one item per entry (a numbered list keeps its number as the Lbl) — what NVDA reads as “list with 12 items” · entries: one BibEntry tag per entry, straight under the section · plain: L > LI > Lbl + LBody with the entry's text in the LBody, no BibEntry (the structure some publishers ask for)", "Analysis", ["list", "entries", "plain"]),
    "detect_blank_lines": ("Detect write-in lines", "Answer lines like '1. ______' are tagged, the line read as the blank-line text", "Analysis", None),
    "blank_lines_as_list": ("Numbered write-in lines as a list", "on: L > LI > Lbl '1.' + LBody (the line, ActualText 'Blank Line') · off: the number and the line are separate P tags", "Analysis", None),
    "join_split_paragraphs": ("Join paragraphs split across columns/pages", "", "Analysis", None),
    "script_language_tagging": ("Tag other-script passages with /Lang", "Tamil, Hindi, Arabic, CJK etc. inside an English document", "Analysis", None),
    "check_contrast": ("Check colour contrast", "WCAG 1.4.3: measures every text run against its real background", "Analysis", None),
    "contrast_level": ("Contrast level", "AA: 4.5:1 (3:1 large text) · AAA: 7:1 (4.5:1 large text)", "Analysis", ["AA", "AAA"]),
    "contrast_large_text": ("Large-text rule", "pac: only 18pt+ counts as large (matches PAC 2024) · wcag: 18pt, or 14pt bold", "Analysis", ["pac", "wcag"]),
    "tag_links": ("Tag links", "Link elements with OBJR and link text", "Analysis", None),
    "detect_cross_references": ("Find cross-references", "Looks for web/e-mail addresses, contents entries, “Chapter 3”, “see Figure 2”, “[12]” and index page numbers, and matches each one to its target in this file. Found items are listed in the editor’s Links tab; which of them actually become clickable links is set below", "Analysis", None),
    "cross_reference_tags": ("Tag cross-references as Reference", "Each mention (“see Figure 3”, “Chapter 2”) is wrapped in a Reference tag — correct PDF/UA semantics, but it adds an inline tag inside the paragraph", "Analysis", None),
    "link_web": ("Link web and e-mail addresses", "A printed URL or e-mail address with no link annotation gets one (https:// or mailto:)", "Analysis", None),
    "link_toc": ("Link contents entries", "Each contents line jumps to the page it names (printed page numbers are matched to the real pages)", "Analysis", None),
    "link_sections": ("Link chapter / section / part references", "“Chapter 3”, “Section 2.1”, “§2.1”, “Part II”, “Appendix A” jump to that heading", "Analysis", None),
    "link_figures_tables": ("Link figure and table references", "“see Figure 3”, “Table 2.1” jump to that figure or table", "Analysis", None),
    "link_references": ("Link bibliography references", "“[12]” and “Smith (2019)” jump to the entry in the reference list", "Analysis", None),
    "link_index": ("Link index entries and page references", "Index page numbers and “see page 12” jump to that printed page", "Analysis", None),
    "link_footnotes": ("Link footnote marks", "A footnote mark in the text (a small raised “3”) jumps to its note — on the same page, or the next note 3 for endnotes", "Analysis", None),
    "link_back": ("Links back (two-way)", "The note’s own mark jumps back to where the text refers to it, and a caption’s “Figure 3” jumps back to where the text first mentions Figure 3", "Analysis", None),
    "link_direction": ("Links set by hand", "The Links stage of the editor: one: the words go to the target · two: the target links back as well (a note's Lbl, or the first words of the target, or words you mark there). Each link can be set either way when it is made", "Editor", ["one", "two"]),
    "detect_index": ("Tag the index", "The entries under an Index heading become Index > L > LI > LBody, sub-entries as a list inside their entry, a wrapped line kept with its entry; letter dividers stay as they are. With “Link index entries” each page number is a link", "Analysis", None),
    "empty_markup_annots": ("Empty review marks", "remove: delete leftover markup annotations that are tiny and carry no comment text (a stray 3 pt review box would fail PDF/UA 7.18.1) · keep: leave them and report", "Analysis", ["remove", "keep"]),
    "tag_form_fields": ("Tag form fields", "Form elements with OBJR", "Analysis", None),
    "tag_other_annotations": ("Tag other annotations", "Comments, highlights, stamps as Annot", "Analysis", None),
    "bookmarks": ("Bookmarks", "headings: rebuild from headings · keep: leave existing outline", "Build", ["headings", "keep", "off"]),
    "bookmark_levels": ("Bookmark depth", "", "Build", None),
    "link_contents": ("Link descriptions", "text: use the visible link text · uri: use the address", "Build", ["text", "uri"]),
    "form_tooltips_from_labels": ("Field tooltips from labels", "Fills missing /TU from the nearest label (review!)", "Build", None),
    "unmatched_content": ("Content outside every tag", "nearest: attach to the closest tag · artifact: hide from assistive tech", "Build", ["nearest", "artifact"]),
    "page_numbers": ("Page numbers", "artifact: the printed page number is pagination, left out of the reading (the PDF/UA default) · P: some publishers want it read — it becomes a P tag where it is printed, first on the page when it sits at the top, last when it sits at the bottom, read as \u201cPage 5\u201d", "Analysis", ["artifact", "P"]),
    "page_number_text": ("Word before a page number", "A page number read as a P says this word and then the number (/ActualText \u201cPage 5\u201d). Leave empty to read only the number", "Analysis", None),
    "page_title_first": ("Page opens with its own heading", "A page whose title is printed as a tab in the margin (a unit title turned on its side) reads that title last, after the table. margin: a heading set outside the text moves to the front of its page · first: the page's top heading always moves to the front · off: the order is left alone. A heading with an equal or better heading above it on the page never moves", "Analysis", ["margin", "first", "off"]),
    "section_wrap": ("Wrap headings in sections", "h1: every top-level heading opens a Sect that holds that heading and everything under it, until the next one (Document > Sect > H1, H2, P, Table … > next Sect) · all: sections nest, an H2 opens a section inside its H1's section · one: the whole article in one Sect (Document > Sect > everything — how InDesign exports a journal article) · off: the headings stay at document level. A file that already has its own sections is left alone", "Build", ["h1", "all", "one", "off"]),
    "figure_bbox_attribute": ("Figure BBox attribute", "Adds Layout/BBox to figures", "Build", None),
    "fix_contrast": ("Fix low-contrast colours", "all: every fixable issue (light text on a coloured box → the box is darkened; otherwise the text colour changes) · selected: only issues ticked in the Contrast tab · off: report only. Changes the design slightly — agree it with the client", "Build", ["all", "selected", "off"]),
    "contrast_fix_method": ("Contrast fix method", "auto: light text on a box → box darker; otherwise text colour (or box lighter when that is the smaller change); an outline when no colour works · background: move the box colour behind the text up or down · text: move the text colour · stroke: outline the letters (like PitStop Fill & Stroke, 0.5 pt black)", "Build", ["auto", "background", "text", "stroke"]),
    "contrast_stroke_width": ("Outline width (pt)", "For contrast fix method 'stroke' — PitStop default used by the team: 0.5", "Build", None),
    "contrast_stroke_color": ("Outline colour", "black (PitStop default) · white · auto: whichever gives more contrast with the background", "Build", ["black", "white", "auto"]),
    "normalize_backgrounds": ("Clean up hidden backgrounds", "Removes the parts of background panels that are completely covered by coloured boxes with text on them (page looks identical). Stops PAC reporting white-on-white for white text on coloured table headers", "Build", None),
    "empty_cell_text": ("Text for empty table cells", "What NVDA/JAWS announce for a cell with no content, e.g. Blank. Leave empty to keep cells silent", "Build", None),
    "table_spans": ("Merged table cells", "keep: a cell printed over several rows or columns is one cell with RowSpan/ColSpan (the regular way, easiest to navigate) · pad: every grid position is a cell of its own — the merged cell keeps its text in its first position and the positions it covered are empty cells (how some publishers' InDesign exports look)", "Build", ["keep", "pad"]),
    "join_marked_content": ("One content item per tag", "A tag whose text runs over several text objects (a title set line by line, a soft hyphen or a space in a text object of its own) gets one marked-content sequence instead of one per text object, and white space set on its own goes with the text beside it (or is an artifact) — Acrobat's Tags panel then shows no empty-looking content items", "Build", None),
    "pdf_min_version": ("PDF version of the built file", "auto: what the file needs (1.5 with compressed object streams) · 1.6 / 1.7: declare at least this version (a client may ask for the version their own files have)", "Build", ["auto", "1.6", "1.7"]),
    "empty_cell_method": ("Empty-cell method", "text (recommended): invisible real text + /ActualText in the cell, so PAC 'Associated with content' passes and NVDA reads it · actualtext: /ActualText only (PAC flags the cell as not associated with content)", "Build", ["text", "actualtext"]),
    "blank_line_text": ("Text for write-in lines", "What screen readers announce for an answer line, e.g. Blank Line. Written as invisible text + /ActualText (same method as empty cells)", "Build", None),
    "underscore_tick_text": ("Text for a short underscore tick", "What a screen reader announces for the two-underscore tick a fluency passage prints before its line count (\u201c__73\u201d). Leave empty to say nothing", "Build", None),
    "multi_blank_line_text": ("Text for multi-line blanks", "Announced when one answer has several lines (e.g. question 5 with 3 lines). Leave empty to use the write-in line text", "Build", None),
    "run_verapdf": ("Validate with veraPDF", "Runs only when a veraPDF path is set in Settings", "Build", None),
    "ai_alt_text_auto": ("AI alt text during analysis", "Drafts alt text for every figure with the vision model", "AI", None),
    "ai_table_headers": ("AI table header check", "Asks the text model whether row 1 is a header", "AI", None),
    "ai_table_merge": ("AI table merge check", "When it is unclear whether a table on the next page continues the previous one (title changed without an H1, header missing, note after the table …) the text model decides: same table (merged) or new table", "AI", None),
    "ai_table_merge_vision": ("AI table merge: look at the pages", "Also sends the bottom of page N and the top of page N+1 to the vision model (needs AI table merge check)", "AI", None),
    "ai_table_check": ("AI table structure check", "When a full-width row at a table's edge looks like a background panel, the vision model looks at the table and decides: real row (kept) or text outside the table", "AI", None),
    "ai_bibliography": ("AI reference list check", "When the reference list has no hanging indent, no numbers and no space between entries, the text model decides where each entry starts", "AI", None),
    "teachers_guide": ("Teacher's Guide structure", "Academain's Teacher's Guides (Benchmark Phonics Intervention: cover, contents, lessons \"Lesson 1 (20–30 MIN.)\", stop-and-assess page, components page) tagged the way the client's finished book is: H2 for each lesson, H3 for its parts (Warm-Up, Multimodal Mini-Lessons, Vocabulary Booster …), H4 for the sub-heads, H5 for each column head of a ruled box — the boxes are H5 + P, not tables —, each word of a blending grid a P, \"_____\" read \"Blank line\", \"Rr\" read \"uppercase R, lowercase r\", the page number a P read \"Page 2\", the whole book in one Sect. auto: when the book is one (\"Teacher's Guide\" on its first pages and lessons headed \"Lesson N (20–30 MIN.)\") · on: always · off: never", "Client", ["off", "auto", "on"]),
    "house_style": ("Client house style", "The tag structure a client asks for, on top of the analysis. kriyadocs: BMJ journal articles as Kriyadocs tags them — the whole article in one Sect; page 1 read as article type, journal logo (Figure), title, authors (P with the ORCID icons), the sidebar (To cite, dates, Check for updates, licence, one Note per affiliation, correspondence), abstract, the box after the abstract; every link as Reference > Link; a table's caption before it and its notes as Note > Lbl; the reference list as L > LI > Lbl + LBody. none: the tool's own V4 structure (Academain and every other client) — exactly as before the house styles came in", "Client", ["none", "kriyadocs"]),
    "use_samples": ("Learn from sample files", "V4.20: the admin's sample files (Admin → Samples) teach the tool how this client tags. A tag the analysis is not sure of (a heading by size, a short bold line, a caption, a note) takes the role most like it in the samples when they agree; wording the client's samples always tag one way (“Key messages” → H2) and running heads / feet they print are tagged the same. With no samples nothing changes. Every change carries a note “samples: …”", "Client", None),
    "ai_samples": ("AI with the samples", "V4.21: the doubtful tags the samples cannot settle are put to the local AI (Ollama, when it is on) together with the client's own examples from the sample files and the fixes people made — so the model answers the way this client tags. Off: no question is asked", "Client", None),
    "sample_rules": ("Client structure from the samples", "V4.21: the structure the client's samples always use is followed — a table's or figure's caption is put where 80 % of the sample captions are (inside first / inside last / before / after)", "Client", None),
    "samples_scope": ("Samples to learn from", "V4.25 · client: the samples of this profile's client and the common ones · all: every client's samples — the files most like this one (same kind of document, same words and type styles) count most, so any PDF is tagged from everything the tool has learned", "Client", ["client", "all"]),
    "ai_brain": ("AI brain (self-learning)", "V4.25: every text tag of the file is decided by the brain — the analysis' rules, the nearest sample tags, the tool's own trained model, the client's style map and wording, each weighed by how often it was right before; what stays doubtful goes to the local AI (Ollama) with the PDF/UA rules, the client's rules and the most similar finished examples. Every change has a note “brain: …”; tags it is still unsure of are outlined for review. It learns from every built and approved file", "AI", None),
    "ai_brain_max": ("AI brain: tags asked per file", "At most this many doubtful tags of one file go to the local AI (15 per question). 0: the brain decides without the AI", "AI", None),
    "brain_strength": ("AI brain: how bold", "careful: changes a tag only when the evidence is overwhelming · balanced (recommended) · bold: follows the learned sources sooner", "AI", ["careful", "balanced", "bold"]),
    "client": ("Samples client", "Whose sample files this profile learns from (Admin → Samples). Empty: the profile's own name. Common samples (for every client) are always used too", "Client", None),
    "actualtext_span": ("ActualText on a Span", "Every client: wherever a tag has /ActualText (a blank line, a spelled card, a page number, a letter-spaced line, a blank cell …), a Span is made inside the tag, holds the tag's content and carries the /ActualText — the tag itself (P, TD, H1 …) carries none. Figure and Formula keep it on the tag, since it is their description, and a tag with no content has nothing to wrap. On by default; a file whose /ActualText still sits on the tag is reported in the Checks tab with a “Put /ActualText on a Span” button, and by the output checker as UA-58", "Build", None),
    "auto_build": ("Build without review", "Batch mode: analyse and build immediately", "Mode", None),
}


BEST_PROFILE = "Best quality (AI, slower)"
# V4.25: the upload default — any PDF, any client: the PDF/UA rules, the local
# AI and everything the tool learned from every client's sample files
SMART_PROFILE = "Smart AI (all samples)"

_AI_ALL = {
    "ai_alt_text_auto": True, "ai_table_headers": True, "ai_table_merge": True,
    "ai_table_merge_vision": True, "ai_table_check": True, "ai_bibliography": True,
    "detect_bibliography": True,
    "link_references": True, "link_toc": True, "link_web": True,
    "link_figures_tables": True, "link_sections": True,
    "join_split_lists": True, "lists_in_cells": True,
}

BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    # V4.25: the default — no client's house rules, every sample of every
    # client (the most similar files count most), the self-learning brain and
    # the local AI on every doubtful tag; a Teacher's Guide is still found
    SMART_PROFILE: {
        **_AI_ALL,
        "samples_scope": "all", "use_samples": True, "ai_samples": True, "sample_rules": True,
        "ai_brain": True, "ai_brain_max": 45, "brain_strength": "balanced",
        "teachers_guide": "auto",
    },
    # every check the tool has, the local AI models included: slower, and the
    # one to use when the tagging has to be right first time
    BEST_PROFILE: {**_AI_ALL, "ai_brain": True},
    "Default": {},
    "Journal article (2-column)": {
        "reading_order": "xycut", "detect_footnotes": True, "detect_toc": False,
        "join_split_paragraphs": True, "table_strategy": "lines",
    },
    "Book": {
        "detect_toc": True, "header_zone": 0.09, "footer_zone": 0.09,
        "bookmarks": "headings", "bookmark_levels": 3, "detect_footnotes": True,
    },
    "Word / report export": {
        "existing_tags": "auto", "detect_toc": True, "table_strategy": "lines_then_text",
    },
    "Scanned document (OCR)": {
        "ocr": "force", "detect_vector_figures": False, "table_strategy": "lines_then_text",
    },
    "Batch auto (no review)": {"auto_build": True},
    # client: Kriyadocs (BMJ journals) — tagged the way the client's own
    # InDesign export is (sample bmjgh-2024-018847)
    # client: Academain — the tool's own V4 structure (no house style). Every
    # /ActualText goes on a Span inside the tag; since V4.16 that is how every
    # profile builds, so this profile only names the client
    "Academain": {
        "actualtext_span": True,
        "teachers_guide": "auto",          # V4.24: a Teacher's Guide gets the client's own structure
    },
    "Kriyadocs": {
        "opts_version": 7,
        "house_style": "kriyadocs",
        "existing_tags": "retag",
        "reading_order": "xycut", "detect_toc": False, "join_split_paragraphs": True,
        "table_strategy": "journal", "table_thead_tbody": False, "merge_continued_tables": False,
        "table_headers_mode": "scope",
        "detect_blank_lines": False, "blank_lines_as_list": False,
        "section_wrap": "one", "page_title_first": "off",
        "detect_bibliography": True, "bibliography_as": "plain",
        "cross_reference_tags": True, "link_references": True, "link_figures_tables": True,
        "link_web": True, "link_footnotes": True, "link_back": False, "link_sections": False,
        "detect_index": False, "detect_footnotes": True, "detect_captions": True,
        "lists_in_cells": True, "join_split_lists": True,
        "bookmarks": "headings", "bookmark_levels": 3,
        "title_source": "heading",
        "ai_alt_text_auto": True,
        # the client's files: empty cells stay empty, one content item per
        # tag, PDF 1.6
        "empty_cell_text": "", "join_marked_content": True, "pdf_min_version": "1.6",
        "table_spans": "keep",
    },
}


def profile_options(name: str, saved: dict[str, dict] | None = None) -> Options:
    base = BUILTIN_PROFILES.get(name)
    if base is None and saved:
        base = saved.get(name)
    return Options.from_dict(base or {})
