"""Layout analysis: propose a logical structure tree for a PDF.

Everything produced here is a proposal with a confidence value; the editor
shows low-confidence decisions for review before anything is written.
"""
from __future__ import annotations

import collections
import difflib
import os
import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable

import pikepdf
import pymupdf

from . import mathspeak
from .compat import house
from .compat import use as use_engine
from .model import HEADINGS, DocModel, StructNode, area, intersection, position_index, union
from .actualtext import looks_spaced, reads_as_words, spaced_words
from .options import Options
from .remediate import is_empty_markup

# the bullets Word, PowerPoint and InDesign set in Wingdings / Symbol /
# ZapfDingbats once their real characters are known (core/symbolfonts.py,
# V4.19.4): boxes, diamonds, arrowheads, ticks — never a letter or a sign
# that starts a sentence
SYMBOL_BULLETS = "❑❒❏❐⬧⧫⬥◻◼◾▫⚫⬝⮚⮘⮙⮛✗✘☐☑☒⇨⇦❖➔▶◉◎"
BULLETS_V4 = "•●◦○▪■□–—·‣⁃➢➤►✓✔∙◆◇♦" + SYMBOL_BULLETS + "*-"
# a client house style (Kriyadocs, V4.10) also reads arrows as bullets
BULLETS = BULLETS_V4[:-2] + "⇒→*-"
# an arrow or a solid bullet is often set with a tab after it that the text
# layer does not keep ("⇒Few studies have …"): a capital right after it still
# starts an item; a dash or an asterisk needs its space ("*Mann-Whitney U")
TIGHT_BULLETS = "⇒→➔►➢➤•●▪■◆♦❖"


def _label_re(bullets: str, tight: bool):
    return re.compile(
        # a dash bullet is often set as two or three dashes ("–– If the student …")
        r"^\s*(?P<lbl>–{2,3}|—{2,3}|-{2,3}|[" + re.escape(bullets) + r"]|\(?\d{1,3}[\.\)]|\(?[a-z][\.\)]|"
        r"\(?[A-Z][\.\)]|\(?(?:[ivxlc]{1,6}|[IVXLC]{1,6})[\.\)]|\(\d{1,3}\)|\([a-z]\)|"
        # a rubric or score list labels its items with a number and a colon, and the
        # label may cover a range: "1 and 2: …", "3: …", "4–5: …"
        r"\d{1,3}(?:\s*(?:and|&|to|or|[–—-])\s*\d{1,3})?:)"
        + (r"(?=\s|(?<=[" + re.escape(TIGHT_BULLETS) + r"])[A-Z(\u201c\"'])" if tight else r"(?=\s)"))


class _ByEngine:
    """A pattern with a V4 form and a house-style form (core/compat.py)."""

    def __init__(self, v4, house_form):
        self.v4, self.house_form = v4, house_form

    def __getattr__(self, name):
        from .compat import house
        return getattr(self.house_form if house() else self.v4, name)


LABEL_RE = _ByEngine(_label_re(BULLETS_V4, False), _label_re(BULLETS, True))
PAGENO_RE = re.compile(r"^\s*(?:page\s+)?(?:[ivxlcdm]{1,7}|\d{1,5})(?:\s*(?:of|/)\s*\d{1,5})?\s*$", re.I)
CAPTION_RE = re.compile(r"^\s*(fig(?:ure)?|table|tab\.|plate|scheme|chart|exhibit|box|map|photo|diagram|listing)"
                        r"\s*\.?\s*[\dIVXivx]+[A-Za-z]?[\.\):\s-]", re.I)
NOTE_RE = re.compile(r"^\s*(\d{1,3}|[*†‡§¶])\s*\S")
TOC_RE = re.compile(r"^(?P<title>.*?\S)\s*(?:\.{3,}|(?:\.\s){3,}|…+|·{3,}|_{3,})\s*(?P<pg>\d{1,4}|[ivxlc]{1,6})\s*$", re.I)
LABEL_ONLY_RE = re.compile(r"^\s*(?:[\(\[]?\d{1,3}[\.\)\]]?|[\(\[]?[A-Za-z][\.\)\]]|[.,;:!?…]+)\s*$")
STEM_LABEL_RE = re.compile(r"^\s*(?P<lbl>\(?\d{1,3}[\.\)]|\(?[A-Za-z][\.\)]|[•●▪■◦○–—-])(?=\s|$)")
# a rule to write on: three or more underscores anywhere, or the two-underscore
# tick a fluency passage prints before its line count ("__73") — but never the
# underscores inside a word (snake_case, __init__)
UNDERSCORE_RUN_RE = re.compile(r"_{3,}|(?<![^\W_])_{2}(?!_)")
PUNCT_ONLY_RE = re.compile(r"^\s*[.,;:!?…]+\s*$")
NUMBERED_HEADING = re.compile(r"^\s*(?:(?:chapter|section|part|appendix)\s+)?(\d+(?:\.\d+){0,5})\.?\s+\S", re.I)

SCRIPTS = [  # (start, end, script, lang)
    (0x0B80, 0x0BFF, "Tamil", "ta"), (0x0900, 0x097F, "Devanagari", "hi"),
    (0x0980, 0x09FF, "Bengali", "bn"), (0x0C00, 0x0C7F, "Telugu", "te"),
    (0x0C80, 0x0CFF, "Kannada", "kn"), (0x0D00, 0x0D7F, "Malayalam", "ml"),
    (0x0A80, 0x0AFF, "Gujarati", "gu"), (0x0A00, 0x0A7F, "Gurmukhi", "pa"),
    (0x0B00, 0x0B7F, "Oriya", "or"), (0x0600, 0x06FF, "Arabic", "ar"),
    (0x0590, 0x05FF, "Hebrew", "he"), (0x0370, 0x03FF, "Greek", "el"),
    (0x0400, 0x04FF, "Cyrillic", "ru"), (0x0E00, 0x0E7F, "Thai", "th"),
    (0xAC00, 0xD7AF, "Hangul", "ko"), (0x3040, 0x30FF, "Kana", "ja"),
    (0x4E00, 0x9FFF, "Han", "zh"), (0x0D80, 0x0DFF, "Sinhala", "si"),
]
LANG_SCRIPT = {"ta": "Tamil", "hi": "Devanagari", "mr": "Devanagari", "sa": "Devanagari",
               "ne": "Devanagari", "bn": "Bengali", "te": "Telugu", "kn": "Kannada",
               "ml": "Malayalam", "gu": "Gujarati", "pa": "Gurmukhi", "or": "Oriya",
               "ar": "Arabic", "ur": "Arabic", "fa": "Arabic", "he": "Hebrew", "el": "Greek",
               "ru": "Cyrillic", "uk": "Cyrillic", "bg": "Cyrillic", "sr": "Cyrillic",
               "th": "Thai", "ko": "Hangul", "ja": "Kana", "zh": "Han", "si": "Sinhala"}


# ======================================================================
@dataclass
class Line:
    text: str
    bbox: tuple[float, float, float, float]      # display space (y down)
    size: float
    bold: bool
    italic: bool
    font: str
    chars: list[tuple[str, tuple[float, float, float, float]]] = field(default_factory=list)
    kind: str = "line"
    flow: int = 0
    stem: bool = False          # followed by a write-in line on the same row
    rot: bool = False           # set on its side (a vertical margin stamp, a spine label)

    @property
    def x0(self): return self.bbox[0]

    @property
    def y0(self): return self.bbox[1]

    @property
    def x1(self): return self.bbox[2]

    @property
    def y1(self): return self.bbox[3]

    @property
    def cy(self): return (self.bbox[1] + self.bbox[3]) / 2


@dataclass
class Block:
    """Non-text item that takes part in reading order (figure, table)."""
    kind: str
    bbox: tuple[float, float, float, float]
    payload: Any = None
    flow: int = 0

    @property
    def x0(self): return self.bbox[0]

    @property
    def y0(self): return self.bbox[1]

    @property
    def x1(self): return self.bbox[2]

    @property
    def y1(self): return self.bbox[3]


def _is_bold(span: dict) -> bool:
    if span.get("flags", 0) & 16:
        return True
    n = (span.get("font") or "").lower()
    return any(k in n for k in ("bold", "black", "heavy", "semibold", "demi", "-bd", ",bd"))


def _is_italic(span: dict) -> bool:
    if span.get("flags", 0) & 2:
        return True
    n = (span.get("font") or "").lower()
    return "italic" in n or "oblique" in n or "-it" in n


def _font_family(name: str) -> str:
    name = re.sub(r"^[A-Z]{6}\+", "", name or "")
    return re.split(r"[-,]", name)[0].lower()


def to_pdf(page: pymupdf.Page, r) -> tuple[float, float, float, float]:
    rr = pymupdf.Rect(r) * ~page.transformation_matrix
    rr.normalize()
    return (round(rr.x0, 2), round(rr.y0, 2), round(rr.x1, 2), round(rr.y1, 2))


def frame(page: pymupdf.Page) -> pymupdf.Rect:
    """Page rectangle in the unrotated space that text/image coordinates use."""
    r = page.rect * page.derotation_matrix
    r.normalize()
    return pymupdf.Rect(0, 0, r.width, r.height)


def to_disp(page: pymupdf.Page, r) -> pymupdf.Rect:
    rr = pymupdf.Rect(r) * page.transformation_matrix
    rr.normalize()
    return rr


def _ubox(boxes) -> tuple[float, float, float, float]:
    boxes = list(boxes)
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _inside(inner, outer, frac: float = 0.6) -> bool:
    a = max(area(inner), 1e-6)
    return intersection(inner, outer) / a >= frac


def _center_in(r, box, pad: float = 0.0) -> bool:
    cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
    return box[0] - pad <= cx <= box[2] + pad and box[1] - pad <= cy <= box[3] + pad


_PROMPT_RUN = re.compile(r"_{3,}")


_LABEL_VERB = re.compile(r"^\s*(?:fig(?:ure)?|table|tab\.)\s*\.?\s*[\dIVXivx]+[A-Za-z]?\s+"
                         r"(?:outlines|shows|presents|lists|summari[sz]es|gives|describes|reports|displays|"
                         r"illustrates|provides|compares|contains|depicts|details|indicates|includes|is|are|was|were)\b",
                         re.I)


def _mentions_label(text: str, size: float, body: float) -> bool:
    """"Table 1 outlines the variables …": running text that starts by
    naming a table, not its caption."""
    from .compat import house
    return house() and bool(_LABEL_VERB.match(text)) and len(text) > 60


def _without_marks(l) -> str:
    """The line's text without the note marks raised after its last word
    ("… Act 525.³⁻⁵ ⁸ ¹⁰" ends with the full stop)."""
    chars = [(c, b) for c, b in (l.chars or []) if c.strip()]
    if len(chars) < 3:
        return l.text.rstrip()
    hs = sorted(b[3] - b[1] for _, b in chars)
    h = hs[len(hs) // 2]
    base = sorted(b[3] for _, b in chars)[len(chars) // 2]
    k = len(chars)
    raised = 0
    while k > 0:
        c, b = chars[k - 1]
        small = (b[3] - b[1]) < 0.8 * h and b[3] < base - 0.12 * h
        if small or (raised and c in ",–-*†‡§¶"):
            raised += small
            k -= 1
            continue
        break
    if not raised:
        return l.text.rstrip()
    return "".join(c for c, _ in chars[:k])
_EDGE_BLANK = re.compile(r"^\s*_{2,}\s*|\s*_{2,}\s*$")


def own_text(text: str, model=None, node=None) -> str:
    """The words a tag reads itself, once its write-in line is a Span of its own.

    "Name: ________" tagged as P > Span: the underscores belong to the Span (read
    "Blank Line"), so the P's own text is "Name:" — it must not show, or be read
    with, the underscores a second time. A line inside a sentence ("The ____
    ran") keeps its place in the text; only a line at the start or the end goes."""
    if model is not None and node is not None:
        kids = [model.nodes[c] for c in node.children if c in model.nodes]
        if not any(k.role == "Span" and ((k.style or {}).get("blank_line")
                                         or set((k.text or "").strip()) == {"_"}) for k in kids):
            return text
    t = text or ""
    for _ in range(2):
        t = _EDGE_BLANK.sub("", t)
    return re.sub(r"\s+", " ", t).strip()


def split_prompts(l: Line) -> list[Line] | None:
    """One printed row holding several "label + write-in line" prompts —
    "Name: ________  Date: ______", "Correct Words per Minute ____  National Norm
    of Words per Minute ____" — becomes one Line per prompt, so each is its own
    P with its own blank. A sentence with two blanks ("The ___ ran to the ___.")
    and a blank with a few words after it ("___ of 26") stay one line.

    The row is cut after a write-in line when what follows is a new label: one
    that ends in a colon, a row whose first label ends in a colon, a tab-wide gap,
    or — with no words after the line — a capitalised label of three words or
    more after a capitalised label of two words or more."""
    chars = list(l.chars)
    text = "".join(c for c, _ in chars)
    runs = [m.span() for m in _PROMPT_RUN.finditer(text)]
    if len(runs) < 2:
        return None
    size = l.size or 10.0
    cuts: list[int] = []
    first_label = text[:runs[0][0]].strip()
    if not first_label:
        return None
    for (a1, b1), (a2, _b2) in zip(runs, runs[1:]):
        between = text[b1:a2]
        words = between.split()
        if not words:
            continue
        # where each word starts, as an index into the text
        starts, pos = [], b1
        for w in words:
            k = text.index(w, pos)
            starts.append(k)
            pos = k + len(w)
        k_up = next((i for i, w in enumerate(words) if w[:1].isupper()), None)
        if k_up is None:
            continue
        label = " ".join(words[k_up:])
        gap = 0.0
        # the space right before the new label (not the words between the line and it)
        prev_box = next((b for c, b in reversed(chars[:starts[k_up]]) if c.strip() and b is not None), None)
        next_box = next((b for c, b in chars[starts[k_up]:] if c.strip() and b is not None), None)
        if prev_box is not None and next_box is not None:
            gap = next_box[0] - prev_box[2]
        ok = (label.endswith(":") or first_label.endswith(":") or gap >= 1.0 * size
              or (k_up == 0 and len(words) >= 3 and len(first_label.split()) >= 2
                  and first_label[:1].isupper()))
        if ok and len(words[k_up:]) <= 10:
            cuts.append(starts[k_up])
    if not cuts:
        return None
    out = []
    bounds = [0] + cuts + [len(chars)]
    for a, b in zip(bounds, bounds[1:]):
        seg = chars[a:b]
        vis = [bx for c, bx in seg if c.strip() and bx is not None]
        if not vis:
            return None
        out.append(Line(text="".join(c for c, _ in seg).strip(), bbox=_ubox(vis), size=l.size,
                        bold=l.bold, italic=l.italic, font=l.font, chars=list(seg), flow=l.flow))
    return out


def _line_words(l) -> list[tuple[str, tuple]]:
    """The words of a line with the box each one is printed in."""
    out: list[tuple[str, tuple]] = []
    word: list[str] = []
    boxes: list[tuple] = []
    for ch, b in list(l.chars) + [(" ", None)]:
        if ch.isspace() or b is None:
            if word and boxes:
                out.append(("".join(word),
                            (min(x[0] for x in boxes), min(x[1] for x in boxes),
                             max(x[2] for x in boxes), max(x[3] for x in boxes))))
            word, boxes = [], []
            continue
        word.append(ch)
        boxes.append(b)
    return out


def _line_pieces(l, boxes: list[tuple]) -> list[tuple[tuple, str]]:
    """A line printed across several cells, cut at the cell borders.

    A row of a letter grid is often one line of text ("M N O") even though each
    letter is printed in its own ruled cell. Sent whole into the cell its middle
    happens to fall in, it leaves the cells beside it empty — and an empty cell
    is then read "Blank cell" over the letter that is plainly printed there.

    The line is cut word by word, never inside a word, and only when the words
    really do land in different cells; anything else keeps the line whole."""
    words = _line_words(l)
    if len(words) < 2:
        return []
    # a cell that holds the whole line (a merged cell a sentence is set in) keeps
    # it whole — only a line that no single cell contains is cut
    lb = l.bbox
    for b in boxes:
        if b[0] - 2 <= lb[0] and lb[2] <= b[2] + 2 and b[1] - 2 <= lb[1] and lb[3] <= b[3] + 2:
            return []
    got: dict[tuple, list[tuple[float, str]]] = {}
    for text, wb in words:
        cx, cy = (wb[0] + wb[2]) / 2, (wb[1] + wb[3]) / 2
        hit = None
        for b in boxes:
            if b[0] <= cx <= b[2] and b[1] <= cy <= b[3]:
                if hit is None or (b[2] - b[0]) * (b[3] - b[1]) < (hit[2] - hit[0]) * (hit[3] - hit[1]):
                    hit = b
        if hit is None:
            return []                      # a word outside every cell: leave it alone
        got.setdefault(hit, []).append((wb[0], text))
    if len(got) < 2:
        return []
    return [(b, " ".join(t for _, t in sorted(ws))) for b, ws in got.items()]


def _norm(t: str) -> str:
    return re.sub(r"[^\w]+", " ", (t or "").lower()).strip()


def _all_caps(t: str) -> bool:
    letters = [c for c in (t or "") if c.isalpha()]
    return len(letters) >= 3 and all(c.isupper() for c in letters)


SENTENCE_END_RE = re.compile(r"[.!?][\"\u201d\u2019)\]]?\s+[\"\u201c(\[]?[A-Z0-9]")


def _is_prose(text: str) -> bool:
    """Running text, however large it is set.

    A big typeface alone does not make a heading: a reading passage set at 13 pt
    in a 10 pt book is still a passage. More than one sentence is the giveaway —
    a heading is one phrase, even when it ends in a question mark."""
    t = (text or "").strip()
    return bool(SENTENCE_END_RE.search(t)) and len(t.split()) > 8


def _is_formula(text: str) -> bool:
    """A displayed equation, not a sentence that happens to contain a number."""
    t = mathspeak.normalise(text)
    return mathspeak.looks_like_math(t) and len(t) <= 200


# ============================================================ text lines
from .textflags import (DICT_FLAGS, IGNORE_ACTUALTEXT, RAW_FLAGS, TEXT_FLAGS,  # noqa: E402,F401
                        WORD_FLAGS, printed_text)


def collect_lines(page: pymupdf.Page) -> list[Line]:
    from .compat import house
    v5 = house()               # the house-style line reading (V4.10 on), see core/compat.py
    out: list[Line] = []
    flags = RAW_FLAGS
    data = page.get_text("rawdict", flags=flags)
    for blk in data.get("blocks", []):
        if blk.get("type") != 0:
            continue
        blk_chars: list[list] = []
        for ln in blk.get("lines", []):
            blk_chars.append([(c["c"], tuple(c["bbox"])) for s in ln.get("spans", [])
                              for c in s.get("chars", []) if c["c"].strip()])

        def justified(i: int, a: float, b: float) -> bool:
            """Is the gap a..b in line i the stretch of a justified line? The
            other lines of the block run through it with words; a gutter
            between two columns merged into one line is empty in all of them."""
            across = [lc for j, lc in enumerate(blk_chars) if j != i and lc
                      and min(c[1][0] for c in lc) < a and max(c[1][2] for c in lc) > b]
            if len(across) < 2:
                return False
            filled = sum(1 for lc in across if any(c[1][0] < b - 1 and c[1][2] > a + 1 for c in lc))
            return filled >= 0.6 * len(across)

        for li, ln in enumerate(blk.get("lines", [])):
            spans = ln.get("spans", [])
            d = ln.get("dir") or (1, 0)
            rot = abs(d[0]) < 0.9
            chars: list[tuple[str, tuple, dict]] = []
            for s in spans:
                for c in s.get("chars", []):
                    chars.append((c["c"], tuple(c["bbox"]), s))
            if not "".join(c[0] for c in chars).strip():
                continue
            # a space the typesetter kerned away to nothing ("cross- sectional",
            # "zhenshcc@ 163. com" — InDesign's break points): the next glyph
            # starts where the space does, so no space is printed
            if v5 and not rot:
                keep = []
                for k, c in enumerate(chars):
                    if c[0] == " " and 0 < k < len(chars) - 1 and chars[k + 1][0].strip() \
                            and chars[k + 1][1][0] <= c[1][0] + 0.3 and chars[k - 1][0].strip():
                        continue
                    keep.append(c)
                chars = keep
            # split at very large horizontal gaps (safety net for merged columns)
            groups: list[list] = [[]]
            for c in chars:
                g = groups[-1]
                if g and c[0].strip() and g[-1][0]:
                    prev_x1 = max(cc[1][2] for cc in g if cc[0].strip()) if any(cc[0].strip() for cc in g) else g[-1][1][2]
                    size = c[2].get("size", 10)
                    if c[1][0] - prev_x1 > max(3.2 * size, 24) and not (v5 and justified(li, prev_x1, c[1][0])):
                        groups.append([])
                groups[-1].append(c)
            for g in groups:
                text = "".join(c[0] for c in g)
                if not text.strip():
                    continue
                vis = [c for c in g if c[0].strip()]
                box = _ubox(c[1] for c in vis)
                weights: collections.Counter = collections.Counter()
                bold_w = ital_w = 0
                fonts: collections.Counter = collections.Counter()
                for ch, _, s in vis:
                    weights[round(s.get("size", 0) * 2) / 2] += 1
                    bold_w += _is_bold(s)
                    ital_w += _is_italic(s)
                    fonts[s.get("font", "")] += 1
                n = max(1, len(vis))
                out.append(Line(
                    text=re.sub(r"\s+", " ", text).strip(), bbox=box,
                    size=weights.most_common(1)[0][0], bold=bold_w / n > 0.6,
                    italic=ital_w / n > 0.6, font=fonts.most_common(1)[0][0],
                    chars=[(c[0], c[1]) for c in g], rot=rot))
    out = _attach_bullets(out)
    return _join_row_pieces(_merge_trailing_numbers(out)) if v5 else _merge_trailing_numbers(out)


# a bullet set on its own: Word and PowerPoint write the bullet, a tab, then
# the text, and the bullet often comes out as a "line" of its own ("❑" |
# "The central nervous system …"); dashes, asterisks and dots are left alone
# (a table's "–" cell beside the next cell)
LONE_BULLETS = "•●◦○▪■□‣⁃➢➤►✓✔◆◇♦⇒→" + SYMBOL_BULLETS


def _attach_bullets(lines: list[Line]) -> list[Line]:
    """Join a bullet printed apart to the text on its right (V4.19.4).

    The bullet must be the first thing on its row, the text close to its
    right (the hanging indent), on the same baseline. A glyph of a symbol
    font the text layer could not name (Private Use, U+FFFD) is read by
    what the font prints (core/symbolfonts.py), else as "•"."""
    from .symbolfonts import decode, family, unreadable
    lone = []
    for l in lines:
        t = l.text.strip()
        if l.rot or len(t) != 1:
            continue
        fam = family(l.font)
        if t in LONE_BULLETS or unreadable(t) or (fam and fam != "symbol"):
            lone.append(l)
        elif fam == "symbol" and decode(fam, t) in LONE_BULLETS:
            lone.append(l)
    if not lone:
        return lines
    drop: set[int] = set()
    joined: dict[int, Line] = {}
    for b in lone:
        h = max(b.y1 - b.y0, 1.0)
        best = None
        for l in lines:
            if l is b or l.rot or id(l) in drop or id(l) in joined:
                continue
            ov = min(l.y1, b.y1) - max(l.y0, b.y0)
            if ov < 0.5 * min(h, l.y1 - l.y0) or not (l.y0 - 1 <= b.cy <= l.y1 + 1):
                continue
            if l.x1 <= b.x0 + 0.5 and l.x1 > b.x0 - 60:
                best = None                 # something before it on the row: not a bullet
                break
            gap = l.x0 - b.x1
            if gap < -0.5 or gap > max(2.6 * l.size, 24):
                continue
            if best is None or l.x0 < best.x0:
                best = l
        if best is None or len(best.text.strip()) < 2 or LABEL_RE.match(best.text) \
                or best.text.strip()[0] in LONE_BULLETS:
            continue
        t = b.text.strip()
        fam = family(b.font)
        if fam:
            t = decode(fam, t) or t
        if not t or unreadable(t) or t not in LONE_BULLETS:
            t = "\u2022"
        gap_box = (b.x1, best.y0, max(b.x1, best.x0), best.y1)
        joined[id(best)] = Line(
            text=f"{t} {best.text}", bbox=_ubox([b.bbox, best.bbox]), size=best.size,
            bold=best.bold, italic=best.italic, font=best.font,
            chars=[(t, c[1]) for c in b.chars if c[0].strip()][:1] + [(" ", gap_box)] + list(best.chars),
            rot=False)
        drop.add(id(b))
    if not drop:
        return lines
    return [joined.get(id(l), l) for l in lines if id(l) not in drop]


def _join_row_pieces(lines: list[Line]) -> list[Line]:
    """Put back together a line the extractor broke in pieces.

      * "as (" | "X ±S). In cases …" — cut at a glyph with an accent drawn
        over it (X̄): two pieces on one baseline with no gap between them
      * "www.who.int/…/tab_1))." | "This" — a justified line whose word gap
        came out wider than a word space, inside a column: the lines above and
        below run straight through the gap, which a gutter never does
      * the bar of that X̄, a one-glyph "line" of its own sitting on top of the
        word it belongs to: it joins that line's box (not its text)"""
    if len(lines) < 2:
        return lines
    flat = [l for l in lines if not l.rot]
    drop: set[int] = set()

    def same_row(a: Line, b: Line) -> bool:
        s = max(a.size, b.size, 1.0)
        return abs(a.cy - b.cy) < 0.3 * s and abs(a.size - b.size) < 0.6 and \
            abs((a.y1 - a.y0) - (b.y1 - b.y0)) < 0.35 * s

    def spans_gap(a: float, b: float, l: Line, above: bool, ref: Line) -> bool:
        h = max(ref.y1 - ref.y0, 1.0)
        if l is ref or l.rot or id(l) in drop:
            return False
        if above and not (ref.y0 - 2.6 * h <= l.cy < ref.y0):
            return False
        if not above and not (ref.y1 < l.cy <= ref.y1 + 2.6 * h):
            return False
        return l.x0 < a - 0.5 and l.x1 > b + 0.5 and any(
            ch.strip() and bx[0] < b - 1 and bx[2] > a + 1 for ch, bx in l.chars)

    # a raised mark set at the end of a line ("… programmes.⁵", "… output.¹⁸")
    # comes out as a line of its own: it belongs to the line it is printed on
    for t in sorted(flat, key=lambda l: l.x0):
        if id(t) in drop or len(t.text.strip()) > 14 or not re.fullmatch(r"[\d,–\-*†‡§¶ ]+", t.text.strip()):
            continue
        host = None
        for l in flat:
            if l is t or id(l) in drop or l.size < t.size / 0.85 or len(l.text) < 3:
                continue
            h = l.y1 - l.y0
            if not (l.y0 - 0.45 * h <= t.y0 and t.y1 <= l.y1 + 0.5 and t.cy < l.cy + 0.15 * h):
                continue
            if -0.5 <= t.x0 - l.x1 <= 0.5 * l.size or -0.5 <= l.x0 - t.x1 <= 0.3 * l.size:
                host = l
                break
        if host is None:
            continue
        if t.x0 >= host.x1 - 0.5:
            host.text = host.text + t.text.strip()
            host.chars = host.chars + t.chars
        else:
            host.text = t.text.strip() + host.text
            host.chars = t.chars + host.chars
        host.bbox = _ubox([host.bbox, t.bbox])
        drop.add(id(t))
    by_y = sorted((l for l in flat if id(l) not in drop), key=lambda l: l.cy)
    ys = [l.cy for l in by_y]
    import bisect
    for a in sorted(flat, key=lambda l: l.x0):
        if id(a) in drop:
            continue
        while True:
            s = max(a.size, 1.0)
            lo = bisect.bisect_left(ys, a.cy - 0.3 * s)
            hi = bisect.bisect_right(ys, a.cy + 0.3 * s)
            right = [b for b in by_y[lo:hi] if b is not a and id(b) not in drop
                     and b.x0 >= a.x1 - 0.5 and same_row(a, b) and b.x0 - a.x1 <= 4 * s]
            if not right:
                break
            b = min(right, key=lambda b: b.x0)
            gap = b.x0 - a.x1
            if gap >= 0.25 * s and not (any(spans_gap(a.x1, b.x0, l, True, a) for l in flat)
                                        and any(spans_gap(a.x1, b.x0, l, False, a) for l in flat)):
                break
            sep = "" if gap < 0.15 * s or a.text.endswith(("(", "[", "/")) else " "
            a.text = f"{a.text}{sep}{b.text}"
            if sep:
                a.chars = a.chars + [(" ", (a.x1, a.y0, b.x0, a.y1))]
            a.chars = a.chars + b.chars
            a.bbox = _ubox([a.bbox, b.bbox])
            drop.add(id(b))
    # an accent set as a glyph of its own over the next word (the bar of X̄)
    for t in flat:
        if id(t) in drop or len(t.text.strip()) > 1 or t.text.strip() not in "−-¯‾ˉ~^˜ˆ˙¨´`":
            continue
        if t.x1 - t.x0 > 1.4 * max(t.size, 1.0):
            continue
        host = None
        for l in flat:
            if l is t or id(l) in drop or len(l.text) < 2:
                continue
            ov_y = min(t.y1, l.y1) - max(t.y0, l.y0)
            ov_x = min(t.x1, l.x1) - max(t.x0, l.x0)
            if ov_y > 0.4 * (t.y1 - t.y0) and ov_x > 0.3 * (t.x1 - t.x0) and t.y0 < l.y0:
                host = l
                break
        if host is not None:
            host.bbox = _ubox([host.bbox, t.bbox])
            drop.add(id(t))
    return [l for l in lines if id(l) not in drop]


def _merge_trailing_numbers(lines: list[Line]) -> list[Line]:
    """Rejoin 'Title ........ 12' rows that the extractor split in two."""
    nums = [l for l in lines if re.fullmatch(r"\d{1,4}|[ivxlc]{1,6}", l.text, re.I)]
    if not nums:
        return lines
    drop = set()
    for n in nums:
        best = None
        for l in lines:
            if l is n or id(l) in drop or l.x1 > n.x0:
                continue
            if abs(l.cy - n.cy) < 0.4 * max(l.size, n.size) and abs(l.size - n.size) < 1.5:
                if best is None or l.x1 > best.x1:
                    best = l
        if best is not None and (TOC_RE.match(best.text + " 1") or best.text.rstrip().endswith((".", "…", "_"))):
            best.text = f"{best.text} {n.text}"
            best.bbox = _ubox([best.bbox, n.bbox])
            best.chars += n.chars
            drop.add(id(n))
    return [l for l in lines if id(l) not in drop]


# ============================================================== XY-cut
def _intervals(items, lo, hi) -> list[tuple[float, float]]:
    iv = sorted((lo(i), hi(i)) for i in items)
    merged: list[list[float]] = []
    for a, b in iv:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def _narrow_gutter(items: list, a: float, b: float, min_gutter: float) -> bool:
    """A gutter narrower than usual that still parts two columns of text: a
    journal's sidebar set 7 pt from the abstract. Both sides are columns of
    real lines (five or more, each several words wide) — never a column of
    list numbers or labels beside their text."""
    from .compat import house
    if not house() or b - a < max(4.0, 0.55 * min_gutter):
        return False
    wide = 5.0 * min_gutter / 0.8
    left = [i.x1 - i.x0 for i in items if isinstance(i, Line) and i.x1 <= a + 0.5]
    right = [i.x1 - i.x0 for i in items if isinstance(i, Line) and i.x0 >= b - 0.5]
    if len(left) < 5 or len(right) < 5:
        return False
    return statistics.median(left) >= wide and statistics.median(right) >= wide


def _word_grids(lines: list) -> list[list[list]]:
    """Grids of single words set in three or more columns (a phonics or
    spelling word list: "litch  mudge  vux  quam  cep" / "gen  knaz …"),
    as rows of lines left to right. The eye reads them row by row
    (V4.21.2)."""
    flat = [l for l in lines if isinstance(l, Line) and not l.rot and l.text.strip()]
    rows: list[list] = []
    for l in sorted(flat, key=lambda i: (i.y0 + i.y1) / 2):
        cy, h = (l.y0 + l.y1) / 2, max(l.y1 - l.y0, 1.0)
        if rows and abs(rows[-1][-1].cy - cy) < 0.35 * h:
            rows[-1].append(l)
        else:
            rows.append([l])

    def gridrow(r):
        if len(r) < 3 or not all(len(i.text.split()) <= 3 and len(i.text.strip()) <= 30 for i in r):
            return False
        r = sorted(r, key=lambda i: i.x0)
        return all(b.x0 - a.x1 >= 1.5 * max(a.size, b.size) for a, b in zip(r, r[1:]))

    def aligned(r1, r2):
        x1 = [i.x0 for i in r1]
        hits = sum(1 for i in r2 if any(abs(i.x0 - x) <= max(6.0, 0.6 * i.size) for x in x1))
        return hits >= max(3, 0.6 * min(len(r1), len(r2)))
    grids, cur = [], []
    for r in rows:
        if gridrow(r) and (not cur or (aligned(cur[-1], r)
                                       and r[0].cy - cur[-1][0].cy <= 4.5 * max(r[0].y1 - r[0].y0, 1.0))):
            cur.append(sorted(r, key=lambda i: i.x0))
            continue
        if len(cur) >= 2:
            grids.append(cur)
        cur = [sorted(r, key=lambda i: i.x0)] if gridrow(r) else []
    if len(cur) >= 2:
        grids.append(cur)
    # a grid wider than it is tall ("litch mudge vux quam cep" over two rows)
    # reads across; a tall one — words set in columns, "discount / confide /
    # portable / nonsense" then the next column — reads down (V4.13), as before
    return [g for g in grids if max(len(r) for r in g) > len(g)]


def _grids_row_by_row(ordered: list, lines: list) -> list:
    """The words of a word grid read row by row, where the grid's first word
    stood in the reading order; each word a flow of its own (one P each)."""
    from .compat import house
    if house():
        return ordered
    grids = _word_grids(lines)
    if not grids:
        return ordered
    top_flow = max([getattr(i, "flow", 0) or 0 for i in ordered] + [0])
    for g in grids:
        members = [l for r in g for l in r]
        ids = {id(l) for l in members}
        pos = [k for k, it in enumerate(ordered) if id(it) in ids]
        if len(pos) != len(members):
            continue
        at = pos[0]
        rest = [it for it in ordered if id(it) not in ids]
        for l in members:
            top_flow += 1
            l.flow = top_flow
        ordered = rest[:at] + members + rest[at:]
    return ordered


def xycut(items: list, min_gutter: float, flow: int, out: list, counter: list[int],
          depth: int = 0) -> None:
    if len(items) <= 1 or depth > 60:
        for it in sorted(items, key=lambda i: (round(i.y0, 1), i.x0)):
            it.flow = flow
            out.append(it)
        return
    rx0 = min(i.x0 for i in items)
    rx1 = max(i.x1 for i in items)
    # vertical gutters
    xiv = _intervals(items, lambda i: i.x0, lambda i: i.x1)
    cuts = [(xiv[k][1] + xiv[k + 1][0]) / 2 for k in range(len(xiv) - 1)
            if xiv[k + 1][0] - xiv[k][1] >= min_gutter or _narrow_gutter(items, xiv[k][1], xiv[k + 1][0],
                                                                        min_gutter)]
    if cuts:
        parts: list[list] = [[] for _ in range(len(cuts) + 1)]
        for it in items:
            cx = (it.x0 + it.x1) / 2
            k = sum(1 for c in cuts if cx > c)
            parts[k].append(it)
        parts = [p for p in parts if p]
        if len(parts) > 1:
            for p in parts:
                counter[0] += 1
                xycut(p, min_gutter, counter[0], out, counter, depth + 1)
            return
    # horizontal bands
    yiv = _intervals(items, lambda i: i.y0, lambda i: i.y1)
    if len(yiv) > 1:
        width = max(rx1 - rx0, 1)
        wide = [i for i in items if (i.x1 - i.x0) >= 0.6 * width]
        gaps = [(yiv[k][1], yiv[k + 1][0]) for k in range(len(yiv) - 1)]
        chosen = []
        for a, b in gaps:
            if any(abs(w.y0 - b) < 0.5 or abs(w.y1 - a) < 0.5 for w in wide):
                chosen.append((a + b) / 2)
        if not chosen:
            a, b = max(gaps, key=lambda g: g[1] - g[0])
            chosen = [(a + b) / 2]
        parts = [[] for _ in range(len(chosen) + 1)]
        for it in items:
            cy = (it.y0 + it.y1) / 2
            k = sum(1 for c in chosen if cy > c)
            parts[k].append(it)
        parts = [p for p in parts if p]
        if len(parts) > 1:
            for p in parts:
                xycut(p, min_gutter, flow, out, counter, depth + 1)
            return
    for it in sorted(items, key=lambda i: (round(i.y0, 1), i.x0)):
        it.flow = flow
        out.append(it)


class _CleanTable:
    """A table from find_tables with background panels removed."""

    def __init__(self, cells, header=None):
        self.cells = cells
        self.bbox = _ubox(cells)
        xs = sorted({round(c[0]) for c in cells} | {round(c[2]) for c in cells})
        ys = sorted({round(c[1]) for c in cells} | {round(c[3]) for c in cells})
        self.col_count = max(1, len(xs) - 1)
        self.row_count = max(1, len(ys) - 1)
        self.header = header


def _h_rules(page) -> list[tuple[float, float, float]]:
    """Horizontal rules on the page: stroked lines, stroked box edges and
    thin filled bars — (y, x0, x1). Big filled panels are not rules."""
    out = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return out
    for d in drawings:
        stroked = d.get("type") in ("s", "fs") and (d.get("width") or 0) >= 0
        for it in d.get("items") or []:
            if it[0] == "l":
                p1, p2 = it[1], it[2]
                if abs(p1.y - p2.y) < 0.6 and stroked:
                    out.append(((p1.y + p2.y) / 2, min(p1.x, p2.x), max(p1.x, p2.x)))
            elif it[0] in ("re", "qu"):
                # InDesign often draws boxes under a transformation: MuPDF
                # reports those rectangles as quads
                r = it[1] if it[0] == "re" else it[1].rect
                if r.height <= 2.5:
                    out.append(((r.y0 + r.y1) / 2, r.x0, r.x1))
                elif stroked:
                    out.append((r.y0, r.x0, r.x1))
                    out.append((r.y1, r.x0, r.x1))
    return out


def _v_rules(page) -> list[tuple[float, float, float]]:
    """Vertical rules: (x, y0, y1) from stroked lines, box sides and thin bars."""
    out = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return out
    for d in drawings:
        stroked = d.get("type") in ("s", "fs")
        for it in d.get("items") or []:
            if it[0] == "l":
                p1, p2 = it[1], it[2]
                if abs(p1.x - p2.x) < 0.6 and stroked:
                    out.append(((p1.x + p2.x) / 2, min(p1.y, p2.y), max(p1.y, p2.y)))
            elif it[0] in ("re", "qu"):
                r = it[1] if it[0] == "re" else it[1].rect
                if r.width <= 2.5:
                    out.append(((r.x0 + r.x1) / 2, r.y0, r.y1))
                elif stroked:
                    out.append((r.x0, r.y0, r.y1))
                    out.append((r.x1, r.y0, r.y1))
    return out


def _row_boxes(page) -> list[tuple]:
    """Filled or stroked boxes (x0, y0, x1, y1) of any size — edges of row boxes."""
    out = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return out
    for d in drawings:
        for it in d.get("items") or []:
            if it[0] in ("re", "qu"):
                r = it[1] if it[0] == "re" else it[1].rect
                if r.width > 2.5 and r.height > 2.5:
                    out.append((r.x0, r.y0, r.x1, r.y1))
    return out


def _cover(intervals, a, b) -> float:
    """Length of [a, b] covered by the union of intervals."""
    segs = sorted((max(a, s), min(b, e)) for s, e in intervals if e > a and s < b)
    tot, cur_s, cur_e = 0.0, None, None
    for s, e in segs:
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                tot += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        tot += cur_e - cur_s
    return tot


def _text_grid(lines: list, box) -> list | None:
    """A grid built from where the words actually sit, not from the ruling.

    A sound-spelling card rules only the outside of its word block and the line
    under the headings; inside, the words simply sit in columns. The grid finder
    then returns one big cell per block, and twelve words end up in a single
    cell. Where the text itself is laid out in rows and columns, that layout is
    the grid: the rows come from lines that share a band, the column edges from
    where the lines start, measured on the rows that have more than one item (so
    a heading centred over three columns does not invent a column of its own).
    A heading like that spans the columns it covers."""
    items = [l for l in lines if (l.text or "").strip()]
    if len(items) < 6:
        return None
    rows: list[list] = []
    for l in sorted(items, key=lambda x: x.y0):
        if rows and min(p.y1 for p in rows[-1]) > l.y0 + 1:
            rows[-1].append(l)
        else:
            rows.append([l])
    # the column edges come from the fullest rows: a heading centred over three
    # columns, or a row with one item in it, would otherwise invent a column
    best = max(len(r) for r in rows)
    wide = [r for r in rows if len(r) >= max(2, 0.8 * best)]
    if len(rows) < 2 or len(wide) < 2 or best < 2:
        return None
    widths = [l.x1 - l.x0 for r in wide for l in r]
    gap = max(8.0, statistics.median(widths) * 0.5)
    xs = sorted(l.x0 for r in wide for l in r)
    edges = [xs[0]]
    for x in xs[1:]:
        if x - edges[-1] > gap:
            edges.append(x)
    if len(edges) < 2:
        return None
    left, right = box[0], box[2]
    bounds = [(max(left, edges[i] - 3) if i else left,
               (edges[i + 1] - 3) if i + 1 < len(edges) else right)
              for i in range(len(edges))]
    bands: list[list[float]] = []
    for r in rows:
        bands.append([min(l.y0 for l in r), max(l.y1 for l in r)])
    for i in range(len(bands) - 1):                 # the rows meet half way
        mid = (bands[i][1] + bands[i + 1][0]) / 2
        bands[i][1] = bands[i + 1][0] = mid
    bands[0][0] = min(bands[0][0], box[1] + 1)
    bands[-1][1] = max(bands[-1][1], box[3] - 1)
    cells: list[tuple] = []
    for (top, bottom), r in zip(bands, rows):
        used: set[int] = set()
        spans: list[list[int]] = []
        for l in sorted(r, key=lambda x: x.x0):
            over = [i for i, (a, b) in enumerate(bounds) if l.x1 > a + 1 and l.x0 < b - 1]
            if not over:
                continue
            first, last = min(over), max(over)
            if any(i in used for i in range(first, last + 1)):
                continue
            used.update(range(first, last + 1))
            spans.append([first, last])
        if spans and len(r) < best and any(sp[1] > sp[0] for sp in spans):
            # A heading row: each heading covers its columns, up to where the
            # next one starts — "Initial Position" over all three of its
            # columns. Only a row whose items are already wider than a column
            # is stretched; a short row of ordinary words (a column that runs
            # out of words half way down) keeps its empty cells instead.
            spans.sort()
            for k, sp in enumerate(spans):
                sp[1] = (spans[k + 1][0] if k + 1 < len(spans) else len(bounds)) - 1
            spans[0][0] = 0
        for first, last in spans:
            cells.append((bounds[first][0], top, bounds[last][1], bottom))
        covered = {i for f, l2 in spans for i in range(f, l2 + 1)}
        for i, (a, b) in enumerate(bounds):         # the rest of the row stays empty
            if i not in covered:
                cells.append((a, top, b, bottom))
    return cells or None


def _block_cells(tb, lines: list) -> bool:
    """Does a cell hold a whole block of items instead of one value?

    Only asked of a grid with a handful of cells — a card ruled on the outside
    with a divider down the middle. A table that already has a cell per value is
    left exactly as it is, however its cells read."""
    cells = [c for c in (tb.cells or ()) if c]
    if len(cells) > 6:
        return False
    hits = 0
    for c in cells:
        inside = [l for l in lines if (l.text or "").strip()
                  and c[0] - 1 <= (l.x0 + l.x1) / 2 <= c[2] + 1
                  and c[1] - 1 <= (l.y0 + l.y1) / 2 <= c[3] + 1]
        if len(inside) < 6:
            continue
        if statistics.median([len(l.text.split()) for l in inside]) > 3:
            continue                                 # prose wrapped in a cell
        ys = sorted({round(l.y0 / 3) for l in inside})
        if len(ys) < 2:
            continue
        widths = [l.x1 - l.x0 for l in inside]
        gap = max(8.0, statistics.median(widths) * 0.5)
        xs = sorted(l.x0 for l in inside)
        cols = 1 + sum(1 for a, b in zip(xs, xs[1:]) if b - a > gap)
        if cols >= 2:
            hits += 1
    return hits > 0


def _attach_stacked_rows(page, tb, hrules, vrules, max_gap: float = 26.0):
    """Full-width boxed rows drawn under a table as separate boxes ("I can
    sit." / "We can sit." / "She can sit."): MuPDF does not see a one-column
    stack as a table, so they would fall out of it. Add them as rows that
    span every column (the gap to the grid is closed).

    `max_gap` is how far below the grid such a box may start. A worksheet often
    leaves a visible gap between the word grid and the sentence boxes under it;
    with a gap of a few points only, those sentences were left outside the
    table — printed inside the ruling, read after it."""
    cells = [tuple(c) for c in tb.cells if c]
    if not cells:
        return tb
    x0, y0, x1, y1 = _ubox(cells)
    w = x1 - x0
    if w < 30:
        return tb
    hs = list(hrules or [])
    vs = list(vrules or [])
    # row-sized boxes (filled or stroked) under the table give edges too
    for bx0, by0, bx1, by1 in _row_boxes(page):
        if abs(bx0 - x0) <= 4 and abs(bx1 - x1) <= 4 and by0 >= y1 - 2 and by1 - by0 <= 150:
            hs += [(by0, bx0, bx1), (by1, bx0, bx1)]
            vs += [(bx0, by0, by1), (bx1, by0, by1)]

    def full_h(y):
        return _cover([(rx0, rx1) for ry, rx0, rx1 in hs if abs(ry - y) <= 1.5], x0, x1) >= 0.9 * w

    def side_cov(x, a, b):
        return _cover([(ry0, ry1) for rx, ry0, ry1 in vs if abs(rx - x) <= 4], a, b)

    def band_text(a, b) -> str:
        """What is printed in this band of the table's own width.

        By the centre of each word, so a unit tab running down the margin — or
        any text that merely clips the edge of the band — does not count as
        content of the row."""
        try:
            words = page.get_text("words", clip=pymupdf.Rect(x0, a, x1, b), flags=WORD_FLAGS) or []
        except Exception:
            return ""
        return " ".join(w[4] for w in words
                        if x0 - 1 <= (w[0] + w[2]) / 2 <= x1 + 1
                        and a - 1 <= (w[1] + w[3]) / 2 <= b + 1).strip()

    ys = sorted({round(ry, 1) for ry, rx0, rx1 in hs if y1 - 1 <= ry <= y1 + 500})
    ys = [y for y in ys if full_h(y)]
    if not ys or ys[0] - y1 > max_gap:
        return tb
    new = []
    top = ys[0]
    for y in ys:
        if y <= top + 8:
            continue
        if y - top > 120:
            break
        h = y - top
        l, r = side_cov(x0, top, y), side_cov(x1, top, y)
        text = band_text(top, y)
        boxed = max(l, r) >= 0.8 * h and min(l, r) >= 0.4 * h
        if not boxed:
            # no sides: either the space between two drawn boxes — step over it
            # and carry on with the box underneath — or the end of the table
            if not text and h <= max_gap:
                top = y
                continue
            break
        # a boxed band with nothing printed in it is an answer row: it is part
        # of the table and is read as a blank cell, not the end of it
        if not text and h < 6:
            break
        new.append((x0, y1 if not new else top, x1, y))
        top = y
    if not new:
        return tb
    out = _CleanTable(cells + new, getattr(tb, "header", None))
    out.dropped = list(getattr(tb, "dropped", []))
    return out


def _clean_table(tb, rules=None):
    """Drop cells that are really fills behind the table.

    Designers put tables on white or tinted panels; MuPDF turns the panel edges
    into ruling lines, giving one giant cell around the whole grid (or a panel
    below it that swallows the text under the table). Those break row
    regularity (PDF/UA 7.5, PAC 'Irregular table row')."""
    cells = []
    seen = set()
    for c in tb.cells:
        if c is None:
            continue
        key = tuple(round(v) for v in c)
        if key not in seen:
            seen.add(key)
            cells.append(tuple(c))
    if len(cells) < 3:
        return tb
    orig = len(cells)
    # slivers (a doubled or offset border) cannot hold text
    cells = [c for c in cells if c[2] - c[0] >= 6 and c[3] - c[1] >= 5]
    if len(cells) < 3:
        return tb

    def centre_in(inner, outer, pad=1.0):
        cx, cy = (inner[0] + inner[2]) / 2, (inner[1] + inner[3]) / 2
        return outer[0] - pad <= cx <= outer[2] + pad and outer[1] - pad <= cy <= outer[3] + pad

    # 1. containers: a "cell" that holds two or more other cells
    cells = [c for c in cells
             if sum(1 for o in cells if o is not c and centre_in(o, c)) < 2]
    # 2. panels at the top/bottom edge spanning the full width, much taller than a row
    # "no rule line at its outer edge" only means something when the rule
    # detector can see this table's own row lines (else never drop a row)
    rules_trusted = False
    if rules:
        bounds = sorted({round(c[1]) for c in cells} | {round(c[3]) for c in cells})
        inner = bounds[1:-1]
        tx0, tx1 = min(c[0] for c in cells), max(c[2] for c in cells)
        seen = sum(1 for y in inner if any(abs(ry - y) <= 2 and min(rx1, tx1) - max(rx0, tx0) > 20
                                            for ry, rx0, rx1 in rules))
        rules_trusted = bool(inner) and seen >= 0.7 * len(inner)
    dropped: list = []
    changed = True
    while changed and len(cells) >= 3:
        changed = False
        hmed = statistics.median(c[3] - c[1] for c in cells)
        x0, y0, x1, y1 = _ubox(cells)
        ys = sorted({round(c[1]) for c in cells})
        if len(ys) < 3:
            break
        for c in list(cells):
            full_w = c[0] <= x0 + 2 and c[2] >= x1 - 2
            full_h = c[1] <= y0 + 1 and c[3] >= y1 - 1
            at_bottom, at_top = c[3] >= y1 - 1, c[1] <= y0 + 1
            edge_row = full_w and (at_top or at_bottom) and (c[3] - c[1]) > 1.5 * hmed
            # a panel under/over the table is clearly taller than a normal row;
            # a normal full-width row ("I see Sam.") is never dropped
            if full_w and (at_top or at_bottom) and not edge_row and rules_trusted and len(ys) >= 4:
                # its outer edge is only where a background panel ends — no rule line
                yy = c[3] if at_bottom else c[1]
                w = c[2] - c[0]
                ruled = any(abs(ry - yy) <= 2 and min(rx1, c[2]) - max(rx0, c[0]) >= 0.5 * w
                            for ry, rx0, rx1 in rules)
                edge_row = not ruled
            # a strip down the left/right side spanning every row, header row
            # included (panel margin, badge edge) — PAC: "Table structure is
            # possibly incomplete due to a missing TH or TD element"
            edge_col = full_h and (c[0] <= x0 + 2 or c[2] >= x1 - 2) and len(ys) >= 4 \
                and not full_w
            if edge_row or edge_col:
                cells.remove(c)
                if edge_row:
                    dropped.append(c)
                changed = True
                break
    if not cells:
        return None
    if len(cells) == orig:
        return tb
    header = None
    out = _CleanTable(cells, header)
    out.dropped = dropped            # full-width edge rows taken out (AI may restore)
    return out


# ========================================================== the analyzer
class Analyzer:
    def __init__(self, path: str, opts: Options, progress: Callable | None = None,
                 ai_cfg=None) -> None:
        self.path = path
        self.opts = opts
        if (getattr(opts, "house_style", "") or "").strip().lower() not in ("", "none", "off"):
            use_engine(opts)            # a client house style: its engine (core/compat.py)
        self.progress = progress or (lambda f, m: None)
        self.ai_cfg = ai_cfg
        self.only_pages: set | None = None      # analyse these pages only (V4.21.4)
        self.doc = pymupdf.open(path)
        # Work in unrotated page space: MuPDF's text/image APIs already do, but
        # table and drawing helpers answer in rotated space. Reset in memory only.
        for p in self.doc:
            if p.rotation:
                p.set_rotation(0)
        self.pdf = pikepdf.open(path)
        self.model = DocModel(path=path, page_count=self.doc.page_count)
        self.model.mode = "retag"
        self.heading_nodes: list[StructNode] = []
        self.toc = []
        try:
            self.toc = self.doc.get_toc(simple=True)
        except Exception:
            self.toc = []

    # ---------------------------------------------------------- doc stats
    def _doc_stats(self) -> None:
        sizes: collections.Counter = collections.Counter()
        self.page_lines: list[list[Line]] = []
        for page in self.doc:
            lines = collect_lines(page)
            self.page_lines.append(lines)
            for l in lines:
                sizes[l.size] += len(l.text)
        self.body = sizes.most_common(1)[0][0] if sizes else 10.0
        self.doc_body = self.body
        self.model.body_size = self.body
        self._page_bodies()
        self._edge_slots()
        self._stamps = self._count_stamps()
        self._repeated_images()

    def page_body_size(self, pno: int, doc_body: float) -> float:
        """The running text size on one page, for an editor action on that page.

        The editor opens a file it did not analyse end to end, so it has only
        the document's body size. On a large-print page that size is far too
        small — every question on it would read as a heading and a number would
        never become a label. Measuring that one page costs nothing."""
        lines = self.page_lines[pno] if getattr(self, "page_lines", None) else []
        c: collections.Counter = collections.Counter()
        for l in lines:
            c[round(l.size, 1)] += len(l.text.strip())
        if not c:
            return doc_body
        size, chars = c.most_common(1)[0]
        if size > doc_body + 1.5 and chars >= 60 and chars >= 0.5 * sum(c.values()) \
                and sum(1 for l in lines if abs(l.size - size) < 0.3) >= 3:
            return size
        return doc_body

    def _page_bodies(self) -> None:
        """Body text size per page-size group.

        One file can mix page formats (7 pt text on Letter pages, 12 pt text on
        a bigger sheet). A single document-wide body size then mistakes a
        12 pt footer date for a heading, or a 14 pt title for body text. Pages
        of the same format share a body size measured on those pages; a format
        used by a single page keeps the document size."""
        groups: dict[tuple, collections.Counter] = collections.defaultdict(collections.Counter)
        count: collections.Counter = collections.Counter()
        keys = []
        for pno, page in enumerate(self.doc):
            fr = frame(page)
            key = (round(fr.width / 6), round(fr.height / 6))
            keys.append(key)
            count[key] += 1
            for l in self.page_lines[pno]:
                groups[key][l.size] += len(l.text)
        body = {k: c.most_common(1)[0][0] for k, c in groups.items() if c and count[k] >= 2}
        self.page_body = [body.get(k, self.doc_body) for k in keys]
        self._page_mains()
        if len({round(b, 1) for b in self.page_body}) > 1:
            self.model.info["page_body_sizes"] = sorted({round(b, 1) for b in self.page_body})

    def _page_mains(self) -> None:
        """The size the running text is set in on each page.

        A large-print workbook sets the teacher pages at 11 pt and the student
        pages at 18 pt on the same sheet, so a body size measured per page
        format reads every question on the large pages as a heading — and then
        a numbered question never becomes a list item. When a page sets most of
        its own text at one bigger size, that size is this page's running text:
        the size the headings on that page have to beat.

        Only a page that really is set larger qualifies: at least three lines
        and 60 characters at that size, and more than half the page's text, so
        a chapter opener with two big title lines keeps its heading."""
        self.page_main = list(self.page_body)
        self.page_top = list(self.page_body)     # the raw dominant size, thresholds aside
        for pno, lines in enumerate(self.page_lines):
            c: collections.Counter = collections.Counter()
            for l in lines:
                c[round(l.size, 1)] += len(l.text.strip())
            if not c:
                continue
            size, chars = c.most_common(1)[0]
            self.page_top[pno] = size
            if size <= self.page_body[pno] + 1.5 or chars < 60 or chars < 0.5 * sum(c.values()):
                continue
            if sum(1 for l in lines if abs(l.size - size) < 0.3) >= 3:
                self.page_main[pno] = size
        if any(a != b for a, b in zip(self.page_main, self.page_body)):
            self.model.info["large_print_pages"] = [
                p + 1 for p, (a, b) in enumerate(zip(self.page_main, self.page_body)) if a != b]

    def _zones(self, page: pymupdf.Page) -> tuple[float, float]:
        h = frame(page).height
        return h * self.opts.header_zone, h * (1 - self.opts.footer_zone)

    def _edge_slots(self) -> None:
        """Running head/foot slots: y positions reused across pages."""
        self.edge_text: collections.Counter = collections.Counter()
        slot_y: dict[str, list[float]] = {"top": [], "bottom": []}
        for pno, page in enumerate(self.doc):
            top, bottom = self._zones(page)
            seen_top = seen_bottom = False
            for l in self.page_lines[pno]:
                if l.y1 <= top:
                    self.edge_text[("top", re.sub(r"\d+", "#", _norm(l.text)))] += 1
                    if l.size <= self.page_body[pno] + 0.5 and not seen_top:
                        slot_y["top"].append(round(l.y0))
                        seen_top = True
                elif l.y0 >= bottom:
                    self.edge_text[("bottom", re.sub(r"\d+", "#", _norm(l.text)))] += 1
                    if l.size <= self.page_body[pno] + 0.5 and not seen_bottom:
                        slot_y["bottom"].append(round(l.y0))
                        seen_bottom = True
        n = self.doc.page_count
        self.slots: dict[str, list[float]] = {"top": [], "bottom": []}
        for zone, ys in slot_y.items():
            c = collections.Counter(ys)
            for y, k in c.items():
                near = sum(v for yy, v in c.items() if abs(yy - y) <= 3)
                if n >= 3 and near >= max(2, n * 0.4):
                    self.slots[zone].append(y)

    def _repeated_images(self) -> None:
        self.image_pages: collections.Counter = collections.Counter()
        for page in self.doc:
            try:
                seen = set()
                for im in page.get_image_info(hashes=True, xrefs=True):
                    key = im.get("digest") or im.get("xref")
                    if key and key not in seen:
                        seen.add(key)
                        self.image_pages[key] += 1
            except Exception:
                continue

    # ---------------------------------------------------------- artifacts
    def _is_artifact_line(self, page: pymupdf.Page, l: Line) -> tuple[bool, str]:
        if l.rot and house() and self._margin_stamp(page, l):
            return True, "Watermark"
        top, bottom = self._zones(page)
        zone = "top" if l.y1 <= top else ("bottom" if l.y0 >= bottom else "")
        if not zone:
            return False, ""
        sub = "Header" if zone == "top" else "Footer"
        if PAGENO_RE.match(l.text):
            return True, sub
        if l.size > self.body + 1.5:
            return False, ""
        key = (zone, re.sub(r"\d+", "#", _norm(l.text)))
        if self.doc.page_count >= 2 and self.edge_text.get(key, 0) >= 2:
            return True, sub
        if any(abs(round(l.y0) - y) <= 3 for y in self.slots[zone]) and len(l.text.split()) <= 12 \
                and not (self.opts.detect_footnotes and NOTE_RE.match(l.text) and zone == "bottom"
                         and l.size < self.body):
            return True, sub
        return False, ""

    STAMP_WORDS = re.compile(r"https?://|www\.|download|copyright|first published|protected by|"
                             r"by guest|licen[cs]e|all rights reserved|\bdoi\b", re.I)

    def _count_stamps(self) -> collections.Counter:
        stamps: collections.Counter = collections.Counter()
        for lines in self.page_lines:
            seen = set()
            for x in lines:
                if x.rot and self.STAMP_WORDS.search(x.text or ""):
                    seen.add(_norm(x.text)[:40])
            stamps.update(seen)
        return stamps

    def _margin_stamp(self, page: pymupdf.Page, l: Line) -> bool:
        """Text set on its side in the outer margin that the site stamps on every
        page of a downloaded article ("BMJ Open: first published as … Downloaded
        from … Protected by copyright") — a watermark, not the article.

        A unit title turned on its side in a book's margin is not one: it has to
        sit in the outer tenth of the page, carry the words of such a stamp, and
        come back on most pages."""
        fr = frame(page)
        cx = (l.x0 + l.x1) / 2
        if fr.width * 0.1 < cx < fr.width * 0.9:
            return False
        stamps = getattr(self, "_stamps", None)
        if stamps is None:
            stamps = self._stamps = self._count_stamps()
        n = max(1, self.doc.page_count)
        key = _norm(l.text)[:40]
        if self.STAMP_WORDS.search(l.text or ""):
            return stamps.get(key, 0) >= max(2, 0.6 * n) or n == 1
        # the pieces between the stamped words ("by guest", "on 31 October 2023.")
        # sit in the same strip as a stamp on this page
        same = [x for x in self.page_lines[page.number] if x.rot and x is not l
                and self.STAMP_WORDS.search(x.text or "") and abs((x.x0 + x.x1) / 2 - cx) < 30
                and stamps.get(_norm(x.text)[:40], 0) >= max(2, 0.6 * n)]
        return bool(same)

    # ------------------------------------------------------------ figures
    def _figures(self, page: pymupdf.Page, pno: int, lines: list[Line],
                 tables: list) -> tuple[list[Block], list[StructNode]]:
        blocks: list[Block] = []
        artifacts: list[StructNode] = []
        pr = frame(page)
        parea = pr.width * pr.height
        n = self.doc.page_count
        try:
            infos = page.get_image_info(hashes=True, xrefs=True)
        except Exception:
            infos = []
        cands = []
        for im in infos:
            r = pymupdf.Rect(im["bbox"]) & pr
            if r.is_empty:
                continue
            key = im.get("digest") or im.get("xref")
            box = (r.x0, r.y0, r.x1, r.y1)
            if min(r.width, r.height) < self.opts.decorative_image_min_pt:
                artifacts.append(self._art(page, pno, box, "Layout", "", "decorative: tiny image"))
                continue
            if r.width * r.height >= 0.85 * parea:
                artifacts.append(self._art(page, pno, box, "Background", "", "full-page background image"))
                continue
            if self.opts.repeated_images_artifact and n >= 3 and \
                    self.image_pages.get(key, 0) >= max(3, 0.3 * n):
                artifacts.append(self._art(page, pno, box, "Layout", "", "image repeats on many pages (logo/ornament)"))
                continue
            cands.append(box)
        # merge touching image tiles
        merged = True
        while merged:
            merged = False
            for i in range(len(cands)):
                for j in range(i + 1, len(cands)):
                    a, b = cands[i], cands[j]
                    if a[0] - 2 <= b[2] and b[0] - 2 <= a[2] and a[1] - 2 <= b[3] and b[1] - 2 <= a[3]:
                        cands[i] = _ubox([a, b])
                        cands.pop(j)
                        merged = True
                        break
                if merged:
                    break
        for box in cands:
            inside = [l for l in lines if _center_in(l.bbox, box)]
            words = sum(len(l.text.split()) for l in inside)
            text_area = sum(area(l.bbox) for l in inside)
            if words > 30 or text_area > 0.35 * area(box):
                artifacts.append(self._art(page, pno, box, "Background", "",
                                           "image behind running text"))
                continue
            blocks.append(Block("figure", box, {"kind": "raster", "conf": 0.6}))

        if self.opts.detect_vector_figures:
            try:
                clusters = page.cluster_drawings()
            except Exception:
                clusters = []
            try:
                drawings = page.get_drawings()
            except Exception:
                drawings = []
            for c in clusters:
                r = pymupdf.Rect(c) & pr
                if r.width < 40 or r.height < 30 or r.width * r.height < 2500:
                    continue
                if r.width * r.height > 0.85 * parea:
                    continue
                box = (r.x0, r.y0, r.x1, r.y1)
                if any(intersection(box, t) > 0.3 * area(box) for t in tables):
                    continue
                if any(intersection(box, t) > 0.5 * area(t) for t in tables):
                    continue        # the panel/frame a table sits on
                if any(intersection(box, b.bbox) > 0.5 * area(box) for b in blocks):
                    continue
                items = [d for d in drawings if _center_in(tuple(d["rect"]), box, 1)]
                if len(items) < 3:
                    continue
                inside = [l for l in lines if _center_in(l.bbox, box)]
                words = sum(len(l.text.split()) for l in inside)
                floor = max(5.0, 0.4 * self.body)
                chars = sum(len(l.text) for l in inside)
                tiny_chars = sum(len(l.text) for l in inside if l.size < floor)
                # a lot of embedded text usually means this is a bordered block
                # of real prose, not a figure — UNLESS that text is far too
                # small to read: a miniature mock-up of a form/card (drawn with
                # the same ruled lines as a real table) legitimately packs in
                # tons of illustrative, illegibly-tiny "preview" text
                looks_illustrative = chars > 0 and tiny_chars / chars > 0.6
                if (words > 40 or sum(area(l.bbox) for l in inside) > 0.3 * area(box)) \
                        and not looks_illustrative:
                    continue        # a frame around text, not a figure
                # pull in short labels hugging the drawing
                grown = box
                for l in lines:
                    if len(l.text.split()) <= 4 and not CAPTION_RE.match(l.text) \
                            and l.size <= self.body + 0.5:
                        lb = l.bbox
                        if (lb[0] < box[2] + 16 and lb[2] > box[0] - 16 and
                                lb[1] < box[3] + 16 and lb[3] > box[1] - 16):
                            grown = _ubox([grown, lb])
                blocks.append(Block("figure", grown, {"kind": "vector", "conf": 0.4}))
        return blocks, artifacts

    # ------------------------------------------------------- write-in lines
    def _pages_parallel(self, procs: int) -> None:
        """Analyse page chunks in parallel processes and merge them in page order."""
        from .parallel import chunks, run_chunks
        n = self.doc.page_count
        shared = {"body": self.doc_body, "page_body": self.page_body, "edge_text": dict(self.edge_text), "slots": self.slots,
                  "stamps": dict(getattr(self, "_stamps", {}) or {}),
                  "image_pages": dict(self.image_pages), "toc": self.toc}
        ai = self.ai_cfg.to_dict() if self.ai_cfg is not None and hasattr(self.ai_cfg, "to_dict") else None
        parts = chunks(list(range(n)), procs)
        jobs = [(self.path, self.opts.to_dict(), ai, shared, pg, f"c{i}_") for i, pg in enumerate(parts)]
        sizes = [len(p) for p in parts]

        def prog(done, total):
            pages_done = sum(sizes[:done]) if done < total else n
            self.progress(0.05 + 0.85 * done / max(1, total),
                          f"pages analysed: {min(n, pages_done)} of {n} ({procs} CPU cores)")
        self.progress(0.05, f"analysing {n} pages on {procs} CPU cores")
        results = run_chunks(_analyze_chunk, jobs, procs, prog)
        m = self.model
        for res in results:
            part = DocModel.from_json(res["model"])
            m.nodes.update(part.nodes)
            m.root_children.extend(part.root_children)
            m.warnings.extend(part.warnings)
            for k, v in part.info.items():
                if isinstance(v, list):
                    m.info.setdefault(k, []).extend(v)
                elif isinstance(v, int) and isinstance(m.info.get(k), int):
                    m.info[k] += v
                else:
                    m.info.setdefault(k, v)
            self.heading_nodes.extend(m.nodes[h] for h in res["headings"] if h in m.nodes)
        m.info["parallel_page_procs"] = procs

    def _stem_label(self, page, l: Line) -> dict | None:
        """'1.' / '4. I can' before a write-in line -> label text and boxes (PDF space)."""
        mt = STEM_LABEL_RE.match(l.text)
        if not mt:
            return None
        lbl = mt.group("lbl")
        vis = [c for c in l.chars if c[0].strip()]
        need = len(lbl.strip())
        if len(vis) < need:
            return None
        lbox = _ubox(b for _, b in vis[:need])
        rest = vis[need:]
        out = {"lbl": lbl, "box": list(to_pdf(page, lbox)), "rest_text": None, "rest_box": None}
        if rest:
            out["rest_text"] = l.text[mt.end():].strip()
            out["rest_box"] = list(to_pdf(page, _ubox(b for _, b in rest)))
        return out

    def _numbered_order(self, roots: list) -> list:
        """Put numbered write-in items back in number order.

        Two answers per row ("1. ___  2. ___" / "3. ___  4. ___") look like two
        columns to the layout order (1, 3, 2, 4); the numbers say otherwise."""
        m = self.model
        groups: list[list] = []          # [label number or None, [nodes]]
        for nd in roots:
            lab = (nd.style or {}).get("stem_label") if nd.role == "P" and nd.parent is None else None
            num_ = None
            if lab:
                d = re.sub(r"\D", "", lab["lbl"])
                num_ = int(d) if d else None
            if num_ is not None:
                groups.append([num_, [nd]])
            elif groups and groups[-1][0] is not None and (
                    (nd.style or {}).get("blank_line") or PUNCT_ONLY_RE.match(nd.text or "")) \
                    and nd.parent is None:
                groups[-1][1].append(nd)
            else:
                groups.append([None, [nd]])
        out: list = []
        k = 0
        while k < len(groups):
            if groups[k][0] is None:
                out.extend(groups[k][1])
                k += 1
                continue
            j = k
            while j < len(groups) and groups[j][0] is not None:
                j += 1
            run = groups[k:j]
            nums = [g[0] for g in run]
            if len(run) >= 2 and nums != sorted(nums) and len(set(nums)) == len(nums) \
                    and max(nums) - min(nums) == len(nums) - 1:
                run = sorted(run, key=lambda g: g[0])
                old = [x.id for g in groups[k:j] for x in g[1]]
                new = [x.id for g in run for x in g[1]]
                pos = sorted(m.root_children.index(i) for i in old)
                if pos == list(range(pos[0], pos[0] + len(pos))):
                    m.root_children[pos[0]:pos[0] + len(pos)] = new
                else:
                    run = groups[k:j]
            for g in run:
                out.extend(g[1])
            k = j
        return out

    def _write_in_lists(self, pno: int, roots: list) -> list:
        """Numbered write-in lines -> L > LI > Lbl + LBody.

        "1. ______"          LI: Lbl "1." + LBody (the line, read as "Blank Line")
        "5. ______ + 2 more" LI: Lbl "5." + LBody > Span (one per line)
        "4. I can ______ ."  LI: Lbl "4." + LBody "I can" > Span (line), Span (".")
        """
        m = self.model
        out: list = []
        cur_L = None
        i, n = 0, len(roots)
        while i < n:
            nd = roots[i]
            lab = (nd.style or {}).get("stem_label") if nd.role == "P" and nd.parent is None else None
            j = i + 1
            blanks = []
            while j < n and (roots[j].style or {}).get("blank_line") and roots[j].parent is None:
                blanks.append(roots[j])
                j += 1
            if not (lab and blanks):
                cur_L = None
                out.append(nd)
                i += 1
                continue
            tail = []
            if j < n and roots[j].role == "P" and roots[j].parent is None and \
                    PUNCT_ONLY_RE.match(roots[j].text or ""):
                tail = [roots[j]]
                j += 1
            if cur_L is None:
                cur_L = StructNode(id=m.new_id("list"), role="L", page=pno, confidence=0.8,
                                   list_numbering=self._numbering(lab["lbl"]),
                                   note="numbered write-in lines")
                m.add(cur_L, None, m.root_children.index(nd.id))
                out.append(cur_L)
            li = StructNode(id=m.new_id("li"), role="LI", page=pno)
            m.add(li, cur_L.id)
            boxes = [x.bbox for x in [nd] + blanks + tail if x.bbox]
            # all answer lines of one question = ONE blank ("Blank Line" once),
            # holding every line drawing
            blank = blanks[0]
            for extra in blanks[1:]:
                m.absorb(blank.id, extra.id)
            if lab["rest_text"] is None:                       # "1." alone
                m.detach(nd.id)
                nd.role, nd.text = "Lbl", lab["lbl"]
                m.add(nd, li.id)
                m.detach(blank.id)
                if not tail:
                    blank.role = "LBody"                       # LBody = the line(s)
                    m.add(blank, li.id)
                else:
                    body = StructNode(id=m.new_id("lb"), role="LBody", page=pno)
                    m.add(body, li.id)
                    blank.role = "Span"
                    m.add(blank, body.id)
                    for t_ in tail:                            # "." is LBody text
                        m.absorb(body.id, t_.id)
            else:                                              # "4. I can ____ ."
                m.add(StructNode(id=m.new_id("lbl"), role="Lbl", page=pno, bbox=tuple(lab["box"]),
                                 text=lab["lbl"]), li.id)
                for t_ in tail:
                    m.absorb(nd.id, t_.id)                     # "." read with "I can"
                    nd.text = (lab["rest_text"] + " " + (t_.text or "")).strip()
                m.detach(nd.id)
                nd.role, nd.bbox = "LBody", tuple(lab["rest_box"])
                if not tail:
                    nd.text = lab["rest_text"]
                m.add(nd, li.id)
                m.detach(blank.id)
                blank.role = "Span"
                m.add(blank, nd.id)
            for bx in boxes:
                li.bbox = union(li.bbox, bx)
                cur_L.bbox = union(cur_L.bbox, bx)
            i = j
        return out


    # ------------------------------------------------- one list, not three
    @staticmethod
    def _label_value(lbl: str) -> tuple[str, int | None]:
        """('kind', number) of a label: ('d', 3) for "3:", ('a', 2) for "b)"."""
        core = lbl.strip().strip("().:")
        core = re.split(r"\s*(?:and|&|to|or|[–—-])\s*", core)[0]
        if core.isdigit():
            return "d", int(core)
        if re.fullmatch(r"[ivxlcIVXLC]+", core):
            from .xref import _roman
            return ("r", _roman(core)) if _roman(core) else ("b", None)
        if len(core) == 1 and core.isalpha():
            return "a", ord(core.lower()) - 96
        return "b", None

    def _line_for(self, pno: int, node: StructNode):
        """The page line a node starts with (for its label's character boxes)."""
        if node.bbox is None:
            return None
        top = node.bbox[3]
        best, bestd = None, 3.0
        for l in self.page_lines[pno]:
            lb = to_pdf(self.doc[pno], l.bbox)
            d = abs(lb[3] - top) + abs(lb[0] - node.bbox[0])
            if d < bestd:
                best, bestd = l, d
        return best

    def _join_numbered_runs(self, pno: int, roots: list, lines: list) -> list:
        """Items of one numbered list that ended up as separate tags join it.

        A rubric sets its first item at the margin and indents the rest; a
        question list has a write-in blank in item 1 and plain text in items 2
        and 3; a wrapped line lands beside the item it belongs to. Each of those
        leaves a run of neighbouring tags that are plainly one list — same kind
        of label, numbers going up — so they are put back together here, after
        everything else on the page has been decided."""
        m = self.model
        kinds: list = []
        for nd in roots:
            if nd.artifact or nd.parent is not None:
                kinds.append(("other", None, None))
                continue
            if nd.role == "L" and [c for c in nd.children if m.nodes[c].role == "LI"]:
                first = m.nodes[[c for c in nd.children if m.nodes[c].role == "LI"][0]]
                lbl = next((m.nodes[c].text for c in first.children
                            if m.nodes[c].role == "Lbl"), "")
                kinds.append(("L", lbl, self._label_value(lbl)))
                continue
            # a numbered line set in the page's own running text is an item,
            # whatever the first pass called it. A line set LARGER than the rest
            # of the page is a numbered heading ("2. Methods") and is left alone.
            running = nd.role in ("P", "Caption") or (
                nd.role == "H?" and abs(float((nd.style or {}).get("size", 0))
                                        - getattr(self, "page_top", self.page_body)[pno]) <= 0.6)
            if running and (nd.text or "").strip():
                mt = LABEL_RE.match(nd.text)
                if mt and nd.bbox is not None:
                    kinds.append(("P", mt.group("lbl"), self._label_value(mt.group("lbl"))))
                    continue
            kinds.append(("other", None, None))

        def compatible(a, b):
            """Does label b continue label a's sequence?"""
            (ka, va), (kb, vb) = a, b
            if ka != kb:
                return False
            if ka == "b":
                return True
            return va is not None and vb is not None and vb > va

        out: list = []
        i, n = 0, len(roots)
        while i < n:
            if kinds[i][0] == "other":
                out.append(roots[i])
                i += 1
                continue
            run = [i]
            j = i + 1
            while j < n:
                if kinds[j][0] != "other" and compatible(kinds[run[-1]][2], kinds[j][2]):
                    run.append(j)
                    j += 1
                    continue
                # a tag with no label, indented into the item above, is its tail
                nd = roots[j]
                prev = roots[run[-1]]
                if kinds[j][0] == "other" and nd.role == "P" and nd.parent is None and \
                        nd.bbox is not None and prev.bbox is not None and \
                        nd.bbox[0] >= prev.bbox[0] - 2 and 0 <= prev.bbox[1] - nd.bbox[3] <= 14:
                    run.append(j)
                    j += 1
                    continue
                break
            labelled = [k for k in run if kinds[k][0] != "other"]
            singles = [k for k in run if kinds[k][0] == "P"]
            if len(labelled) < 2 or not singles:
                out.extend(roots[k] for k in run)
                i = j
                continue
            out.append(self._build_run(pno, [roots[k] for k in run], [kinds[k] for k in run]))
            i = j
        return out

    def _fix_item_labels(self) -> int:
        """A list item whose number is still inside its LBody gets a real Lbl.

        PDF/UA wants the number in `Lbl` and the text in `LBody`; when it stays
        in the body a screen reader reads "one dot what might you see" as one
        run, and the list loses its numbering. Whichever pass built the item,
        this puts the label where it belongs — the Lbl box sits inside the body
        box, so the glyphs of the number go to it and the rest to the body."""
        m = self.model
        fixed = 0
        for li in [n for n in m.nodes.values() if n.role == "LI"]:
            kids = [m.nodes[c] for c in li.children if c in m.nodes]
            if any(k.role == "Lbl" for k in kids):
                continue
            body = next((k for k in kids if k.role == "LBody"), None)
            if body is None or body.page < 0 or body.bbox is None:
                continue
            mt = LABEL_RE.match(body.text or "")
            if not mt:
                continue
            line = self._line_for(body.page, body)
            # the structure already says this is a list item, so the size of the
            # text does not decide anything here — only the wording does
            lab = self._label(line, limit=None) if line is not None else None
            box = to_pdf(self.doc[body.page], lab[1]) if lab else None
            lbl_text = lab[0] if lab else mt.group("lbl")
            if box is None or not (body.bbox[0] - 3 <= box[0] and box[2] <= body.bbox[2] + 3):
                # no line matched the body box (a hand-drawn zone, a joined item,
                # a body that starts mid-line): measure the label's characters
                # inside the body itself
                box = self._label_box_in(body.page, body.bbox, lbl_text)
            if box is None:
                continue
            m.add(StructNode(id=m.new_id("lbl"), role="Lbl", page=body.page, bbox=box,
                             text=lbl_text, confidence=0.8,
                             note="number taken out of the item text"), li.id, 0)
            body.text = (body.text[len(mt.group(0)):]).strip()
            fixed += 1
        if fixed:
            m.info["item_labels_fixed"] = fixed
        return fixed

    def _label_box_in(self, pno: int, bbox, lbl: str) -> tuple | None:
        """The box of the label's own characters, measured inside `bbox`.

        The first line of the item is whatever sits highest in the box; the
        label is its first few visible characters. This is the fallback for an
        item whose body does not line up with a page line — a hand-drawn zone,
        or a body built by joining tags."""
        need = len((lbl or "").strip())
        if need <= 0 or bbox is None:
            return None
        page = self.doc[pno]
        chars: list = []
        for l in self.page_lines[pno]:
            lb = to_pdf(page, l.bbox)
            if lb[3] < bbox[1] - 1 or lb[1] > bbox[3] + 1:
                continue
            for ch, cbox in l.chars:
                if not ch.strip():
                    continue
                pb = to_pdf(page, cbox)
                cx, cy = (pb[0] + pb[2]) / 2, (pb[1] + pb[3]) / 2
                if bbox[0] - 1 <= cx <= bbox[2] + 1 and bbox[1] - 1 <= cy <= bbox[3] + 1:
                    chars.append(pb)
        if len(chars) <= need:
            return None
        # PDF coordinates run upwards: the item's first line is the highest one
        chars.sort(key=lambda b: (-round(b[3] / 3), b[0]))
        return _ubox(chars[:need])

    def _build_run(self, pno: int, nodes: list, kinds: list) -> StructNode:
        """Turn a run of labelled tags (and their tails) into one L."""
        m = self.model
        first = nodes[0]
        sibs = m.siblings(first.id)
        pos = sibs.index(first.id) if first.id in sibs else None
        parent = first.parent
        lbl0 = next((k[1] for k in kinds if k[1]), "")
        L = StructNode(id=m.new_id("list"), role="L", page=pno, confidence=0.75,
                       list_numbering=self._numbering(lbl0),
                       note="numbered items that were tagged separately, joined into one list")
        m.add(L, parent, pos)
        last_body = None
        for nd, (kind, lbl, _val) in zip(nodes, kinds):
            if kind == "L":
                for cid in list(nd.children):
                    child = m.nodes[cid]
                    m.detach(cid)
                    m.add(child, L.id)
                    if child.role == "LI":
                        last_body = next((c for c in child.children
                                          if m.nodes[c].role == "LBody"), last_body)
                m.detach(nd.id)
                m.nodes.pop(nd.id, None)
                continue
            if kind == "other":
                # a wrapped line: it belongs to the item above
                if last_body is not None:
                    body = m.nodes[last_body]
                    body.text = (body.text + " " + (nd.text or "")).strip()
                    body.bbox = union(body.bbox, nd.bbox)
                    for cid in list(nd.children):
                        c = m.nodes[cid]
                        m.detach(cid)
                        m.add(c, body.id)
                    if nd.bbox is not None:
                        m.nodes[body.parent].bbox = union(m.nodes[body.parent].bbox, nd.bbox)
                    m.detach(nd.id)
                    m.nodes.pop(nd.id, None)
                else:
                    m.detach(nd.id)
                    m.add(nd, L.id)
                continue
            li = StructNode(id=m.new_id("li"), role="LI", page=nd.page, bbox=nd.bbox)
            m.add(li, L.id)
            line = self._line_for(nd.page, nd)
            lab = self._label(line, limit=None) if line is not None else None
            lbox = to_pdf(self.doc[nd.page], lab[1]) if lab else None
            body_text = nd.text[len(LABEL_RE.match(nd.text).group(0)):].strip() \
                if LABEL_RE.match(nd.text or "") else (nd.text or "")
            m.add(StructNode(id=m.new_id("lbl"), role="Lbl", page=nd.page,
                             bbox=lbox or nd.bbox, text=lbl), li.id)
            body = StructNode(id=m.new_id("lb"), role="LBody", page=nd.page, text=body_text,
                              bbox=(lbox[2], nd.bbox[1], nd.bbox[2], nd.bbox[3]) if lbox else nd.bbox,
                              style=dict(nd.style or {}))
            m.add(body, li.id)
            for cid in list(nd.children):               # Spans of a write-in blank
                c = m.nodes[cid]
                m.detach(cid)
                m.add(c, body.id)
            last_body = body.id
            m.detach(nd.id)
            m.nodes.pop(nd.id, None)
        kids = [c for c in L.children if m.nodes[c].role == "LI"]
        boxes = [m.nodes[c].bbox for c in kids if m.nodes[c].bbox]
        if boxes:
            L.bbox = _ubox(boxes)
        # a line that turned out to be an item is no longer a heading candidate
        self.heading_nodes = [h for h in self.heading_nodes if h.id in m.nodes]
        return L

    def _blank_lines(self, page: pymupdf.Page, lines: list[Line], tables: list,
                     figs: list) -> list[tuple]:
        """Horizontal rules that are spaces to write in ("1. ________").

        A rule counts when text sits just left of it on the same row (the
        question number or sentence start), when text continues right after it
        ("I can ______ ."), or when it is stacked under such a rule with the
        same extent (extra answer lines). Underlines, table/box borders and
        separator rules are left alone."""
        if not self.opts.detect_blank_lines:
            return []
        try:
            drawings = page.get_drawings()
        except Exception:
            return []
        top, bottom = self._zones(page)
        segs, others = [], []
        for d in drawings:
            r = d.get("rect")
            if r is None:
                continue
            thin = r.height <= 2.5 and r.width >= 36
            simple = len(d.get("items") or []) <= 2
            if thin and simple and (d.get("color") is not None or d.get("fill") is not None):
                segs.append([r.x0, (r.y0 + r.y1) / 2, r.x1])
            elif not (r.width < 0.5 and r.height < 0.5):
                others.append((r.x0, r.y0, r.x1, r.y1))
        # join collinear pieces
        segs.sort(key=lambda s_: (round(s_[1]), s_[0]))
        merged: list[list[float]] = []
        for sg in segs:
            if merged and abs(merged[-1][1] - sg[1]) < 1 and sg[0] - merged[-1][2] < 3:
                merged[-1][2] = max(merged[-1][2], sg[2])
            else:
                merged.append(list(sg))
        cands = []
        for x0, y, x1 in merged:
            if y <= top or y >= bottom:
                continue
            if any(t[0] - 2 <= (x0 + x1) / 2 <= t[2] + 2 and t[1] - 2 <= y <= t[3] + 2 for t in tables):
                continue
            if any(b.bbox[0] <= (x0 + x1) / 2 <= b.bbox[2] and b.bbox[1] <= y <= b.bbox[3] for b in figs):
                continue
            # a border of a box / grid: other drawings touch its ends
            if any(o[1] - 2 <= y <= o[3] + 2 and (abs(o[0] - x0) < 2.5 or abs(o[2] - x1) < 2.5
                                                     or abs(o[0] - x1) < 2.5 or abs(o[2] - x0) < 2.5)
                   and (o[3] - o[1]) > 3 for o in others):
                continue
            # an underline: text sits on it
            under = False
            for l in lines:
                ov = min(l.x1, x1) - max(l.x0, x0)
                if ov > 0.5 * min(l.x1 - l.x0, x1 - x0) and -3 <= y - l.y1 <= 5 and \
                        l.text.strip("_ .") != "":
                    under = True
                    break
            if under:
                continue
            cands.append((x0, y, x1))

        def same_row(l, y):
            return l.y0 - 2 <= y <= l.y1 + 5

        accepted = {}
        for c in cands:
            x0, y, x1 = c
            left = [l for l in lines if same_row(l, y) and -2 <= x0 - l.x1 <= 60]
            right = [l for l in lines if same_row(l, y) and -2 <= l.x0 - x1 <= 20]
            if left or right:
                accepted[c] = left
        # stacked continuation lines under an accepted one
        changed = True
        while changed:
            changed = False
            for c in cands:
                if c in accepted:
                    continue
                for a in list(accepted):
                    if 0 < c[1] - a[1] <= 130 and (abs(c[2] - a[2]) <= 25 or c[0] - 25 <= a[0]) \
                            and not any(l.y0 > a[1] and l.y1 < c[1] and l.x0 < c[2] and l.x1 > c[0]
                                        and not (l.x1 <= c[0] + 30) for l in lines):
                        accepted[c] = []
                        changed = True
                        break
        out = []
        for (x0, y, x1), left in accepted.items():
            for l in left:
                l.stem = True
            # the zone: the line plus room above it for the invisible text,
            # without reaching into text above
            above = [l.y1 for l in lines if l.y1 <= y - 1 and l.x0 < x1 and l.x1 > x0]
            y0 = max([y - 12.0] + [a + 0.5 for a in above])
            zone = (x0, min(y0, y - 3), x1, y + 1.5)
            # for reading order the line belongs to the text row it sits on
            row = [l.y0 for l in lines if same_row(l, y) and (l.x1 <= x0 + 2 or l.x0 >= x1 - 2)]
            order = (x0, min([zone[1]] + row), x1, zone[3])
            out.append((zone, order))
        return out

    def _art(self, page, pno, box, typ, sub, note) -> StructNode:
        return StructNode(id=self.model.new_id("art"), role="Artifact", page=pno,
                          bbox=to_pdf(page, box), artifact=True, artifact_subtype=typ,
                          pagination_subtype=sub, confidence=0.8, note=note)

    # ------------------------------------------------------------- tables
    def _tiny_text_table(self, pno: int, bbox) -> bool:
        """A ruled grid whose text is far too small to read is almost always a
        miniature illustration of a form (a cover/mock-up thumbnail showing
        what a sheet looks like), not a real data table — those mock-ups get
        picked up by the line-based table finder because they are drawn with
        the same kind of ruled grid. Real tables in a document are set in
        legible text; reject anything whose text sits far below that."""
        chars = tiny = 0
        floor = max(5.0, 0.4 * self.body)
        for l in self.page_lines[pno]:
            if _center_in(l.bbox, bbox):
                chars += len(l.text)
                if l.size < floor:
                    tiny += len(l.text)
        return chars >= 20 and tiny / chars > 0.6

    def _tables(self, page: pymupdf.Page) -> list:
        if not self.opts.detect_tables:
            return []
        found = []
        try:
            found = list(page.find_tables(strategy="lines").tables)
        except Exception as exc:
            self.model.warnings.append(f"p{page.number + 1}: table detection failed ({exc})")
        if not found and self.opts.table_strategy == "lines_then_text":
            try:
                for tb in page.find_tables(strategy="text").tables:
                    rows = tb.extract()
                    if len(rows) < 3 or tb.col_count < 2:
                        continue
                    filled = [sum(1 for c in r if c and c.strip()) for r in rows]
                    longest = max((len((c or "").split()) for r in rows for c in r), default=0)
                    if statistics.median(filled) >= 2 and longest <= 20:
                        found.append(tb)
            except Exception:
                pass
        journal = self.opts.table_strategy == "journal"
        rules = _h_rules(page) if (found or journal) else []
        vrules = _v_rules(page) if found else []
        good = []
        if journal and rules:
            # tables ruled with horizontal lines only: no cell edges to find
            try:
                from .tablegrid import journal_cells, rule_regions
                for region, cells in rule_regions(rules, page.rect.width, [tuple(t.bbox) for t in found]):
                    res = journal_cells(page, region, cells)
                    if res is not None and res["rows"] >= 2:
                        jt = _CleanTable(res["cells"])
                        jt.journal = res
                        if jt.col_count >= 2 and not self._tiny_text_table(page.number, jt.bbox):
                            good.append(jt)
            except Exception as exc:
                self.model.warnings.append(f"p{page.number + 1}: ruled table: {exc}")
        for tb in found:
            if journal:
                # rows from the rules, columns from the words (core/tablegrid.py)
                try:
                    from .tablegrid import journal_cells
                    res = journal_cells(page, tb.bbox, [c for c in tb.cells if c])
                except Exception:
                    res = None
                if res is not None:
                    jt = _CleanTable(res["cells"])
                    jt.journal = res
                    if jt.col_count >= 2 and not self._tiny_text_table(page.number, jt.bbox):
                        good.append(jt)
                    continue
            try:
                tb = _clean_table(tb, rules)
                if tb is not None:
                    tb = _attach_stacked_rows(page, tb, rules, vrules)
                if tb is not None and getattr(tb, "dropped", None) and self.ai_cfg is not None \
                        and self.opts.ai_table_check:
                    tb = self._ai_restore_rows(page, tb)
                if tb is not None and tb.row_count >= 1 and tb.col_count >= 2 and len(tb.cells) >= 2 \
                        and not self._tiny_text_table(page.number, tb.bbox):
                    good.append(tb)
            except Exception:
                continue
        return good

    def _ai_restore_rows(self, page, tb):
        """Ask the vision model about each full-width edge row the cleaner took
        out: real table row (keep) or text under/over the table (drop)."""
        from .ai import OllamaError, table_row_inside
        keep = []
        for c in tb.dropped:
            text = " ".join(printed_text(page, c).split())
            if not text:
                continue
            clip = pymupdf.Rect(_ubox(list(tb.cells) + [c])) + (-12, -12, 12, 12)
            try:
                png = page.get_pixmap(matrix=pymupdf.Matrix(1.6, 1.6), clip=clip & page.rect).tobytes("png")
                ans = table_row_inside(self.ai_cfg, png, text)
            except OllamaError as exc:
                self.model.warnings.append(f"AI table check stopped: {exc}")
                self.ai_cfg = None
                break
            except Exception:
                ans = None
            self.model.info.setdefault("ai_table_check", []).append(
                {"page": page.number + 1, "text": text[:60], "inside": ans})
            if ans:
                keep.append(c)
        if not keep:
            return tb
        return _CleanTable(list(tb.cells) + keep)

    def _build_table(self, page, pno, tb, lines: list[Line], refine: bool = False) -> StructNode:
        m = self.model
        # When the person has drawn the area and said "this is a table", the
        # ruling may describe only the outside of a block of words; where the
        # text is laid out in rows and columns, that layout is the grid. The
        # automatic pass does not do this (`refine=False`): a panel ruled round a
        # block of words is not always meant to be a grid, and the page sweep
        # should not turn one into a 47-cell table on its own.
        if refine and getattr(tb, "cells", None):
            try:
                inside = [l for l in lines if (l.text or "").strip()
                          and tb.bbox[0] - 1 <= (l.x0 + l.x1) / 2 <= tb.bbox[2] + 1
                          and tb.bbox[1] - 1 <= (l.y0 + l.y1) / 2 <= tb.bbox[3] + 1]
                if _block_cells(tb, inside):
                    grid = _text_grid(inside, tb.bbox)
                    if grid and len(grid) > len([c for c in tb.cells if c]):
                        tb = _CleanTable(grid, getattr(tb, "header", None))
                        self.model.info.setdefault("tables_from_text_layout", 0)
                        self.model.info["tables_from_text_layout"] += 1
            except Exception:
                pass
        cells = []
        seen = set()
        for c in tb.cells:
            if c is None:
                continue
            key = tuple(round(v, 0) for v in c)
            if key in seen:
                continue
            seen.add(key)
            cells.append(tuple(c))

        def grid(vals):
            vals = sorted(vals)
            out = []
            for v in vals:
                if not out or v - out[-1] > 2.0:
                    out.append(v)
            return out
        xs = grid([c[0] for c in cells] + [c[2] for c in cells])
        ys = grid([c[1] for c in cells] + [c[3] for c in cells])

        # A rule drawn a few points away from the row above it adds a sliver
        # row to the grid: an empty strip along the top border that then has to
        # be tagged, and (worse) that reads as the table's header row. A band
        # far thinner than the rest with nothing printed in it is not a row —
        # it belongs to the row beside it.
        tb_box = tuple(tb.bbox)

        def _drop_slivers(g, printed) -> list:
            out = list(g)
            while len(out) > 2:
                spans = [out[i + 1] - out[i] for i in range(len(out) - 1)]
                med = statistics.median(spans)
                for i, sp in enumerate(spans):
                    if sp >= 0.4 * med or printed(out[i], out[i + 1]):
                        continue
                    out.pop(1 if i == 0 else i)
                    break
                else:
                    break
            return out

        def _row_printed(a, b):
            return any(a - 1 <= (l.y0 + l.y1) / 2 <= b + 1 and
                       tb_box[0] - 1 <= (l.x0 + l.x1) / 2 <= tb_box[2] + 1 for l in lines)

        def _col_printed(a, b):
            return any(a - 1 <= (l.x0 + l.x1) / 2 <= b + 1 and
                       tb_box[1] - 1 <= (l.y0 + l.y1) / 2 <= tb_box[3] + 1 for l in lines)

        before = (len(xs), len(ys))
        xs = _drop_slivers(xs, _col_printed)
        ys = _drop_slivers(ys, _row_printed)
        slivers = (before[0] - len(xs)) + (before[1] - len(ys))

        def idx(g, v):
            return min(range(len(g)), key=lambda i: abs(g[i] - v))

        placed = []
        for c in cells:
            c0, c1 = idx(xs, c[0]), idx(xs, c[2])
            r0, r1 = idx(ys, c[1]), idx(ys, c[3])
            if c1 <= c0 or r1 <= r0:
                continue
            placed.append({"box": c, "row": r0, "col": c0, "rs": r1 - r0, "cs": c1 - c0})
        nrows = len(ys) - 1
        ncols = len(xs) - 1
        # fill holes so every row spans the full grid (PDF/UA table regularity)
        covered = set()
        for p in placed:
            for rr in range(p["row"], p["row"] + p["rs"]):
                for cc in range(p["col"], p["col"] + p["cs"]):
                    covered.add((rr, cc))
        for rr in range(nrows):
            for cc in range(ncols):
                if (rr, cc) not in covered:
                    placed.append({"box": (xs[cc], ys[rr], xs[cc + 1], ys[rr + 1]), "row": rr,
                                   "col": cc, "rs": 1, "cs": 1, "filler": True})
                    covered.add((rr, cc))

        # what is printed in each cell: whole lines, and the pieces of a line
        # that runs across several cells (a letter grid printed row by row)
        cell_boxes = [p["box"] for p in placed]
        hits: dict[tuple, list] = {}
        for l in lines:
            if not (l.text or "").strip():
                continue
            pieces = _line_pieces(l, cell_boxes)
            if pieces:
                for b, t in pieces:
                    hits.setdefault(b, []).append((l, t))
            else:
                home = [b for b in cell_boxes if _center_in(l.bbox, b)]
                if home:
                    b = min(home, key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))
                    hits.setdefault(b, []).append((l, l.text))

        def cell_text(box):
            inside = hits.get(box) or []
            return " ".join(t for _, t in sorted(inside, key=lambda it: (it[0].y0, it[0].x0))).strip()

        def cell_bold(box):
            inside = hits.get(box) or []
            return bool(inside) and all(l.bold for l, _ in inside)

        def cell_size(box):
            inside = hits.get(box) or []
            return max((l.size for l, _ in inside), default=0.0)

        for p in placed:
            p["text"] = cell_text(p["box"])
            p["bold"] = cell_bold(p["box"])
            p["size"] = cell_size(p["box"])

        # A grid whose every cell is set the same way has no header at all. A
        # word grid in a phonics book prints every cell bold at one size, one
        # word to a cell — bold then says nothing about which cell is a header,
        # and calling the first row (or column) TH announces a column heading
        # that is not there. A header has to LOOK different from the body.
        row0 = [p for p in placed if p["row"] == 0]
        filled = lambda ps: [p for p in ps if p["text"]]

        def alike(a: list, b: list) -> bool:
            """Do these two sets of cells look the same — weight, size, length?

            Medians and proportions, not `all`, so one odd cell (an invisible
            "Blank cell" text left by an earlier tool, a stray caption) does not
            make a plain grid look like a table with a header."""
            if len(a) < 2 or len(b) < 2:
                return False
            if abs(sum(p["bold"] for p in a) / len(a) - sum(p["bold"] for p in b) / len(b)) > 0.34:
                return False
            if abs(statistics.median([p["size"] for p in a])
                   - statistics.median([p["size"] for p in b])) > 0.6:
                return False
            return abs(statistics.median([len(p["text"].split()) for p in a])
                       - statistics.median([len(p["text"].split()) for p in b])) <= 1.0

        plain_rows = alike(filled(row0), filled([p for p in placed if p["row"] > 0]))
        plain_cols = alike(filled([p for p in placed if p["col"] == 0 and p["row"] > 0]),
                           filled([p for p in placed if p["col"] > 0 and p["row"] > 0]))

        # header rows
        header_rows = 0
        conf = 0.45
        if plain_rows:
            pass                       # every row reads like the body: no header row
        elif row0 and all(p["bold"] or not p["text"] for p in row0) and any(p["bold"] for p in row0):
            header_rows = max(p["rs"] for p in row0)
            # extend over following all-bold rows that sit under spanning headers
            while header_rows < nrows:
                nxt = [p for p in placed if p["row"] == header_rows]
                if nxt and all(p["bold"] or not p["text"] for p in nxt) and \
                        any(p["row"] < header_rows and p["cs"] > 1 for p in placed):
                    header_rows += 1
                else:
                    break
            conf = 0.8
        else:
            try:
                hdr = tb.header
                if hdr is not None and not hdr.external and any(hdr.names):
                    header_rows = 1
                    conf = 0.6
            except Exception:
                pass
        if header_rows == 0 and not plain_rows and self.opts.ai_table_headers \
                and self.ai_cfg is not None:
            try:
                from .ai import table_has_header
                r1 = [p["text"] for p in sorted(row0, key=lambda p: p["col"])]
                r2 = [p["text"] for p in sorted((p for p in placed if p["row"] == 1), key=lambda p: p["col"])]
                ans = table_has_header(self.ai_cfg, r1, r2)
                if ans:
                    header_rows, conf = 1, 0.7
            except Exception:
                pass
        if header_rows == 0 and nrows >= 2 and not plain_rows \
                and any(p["text"] for p in row0):
            header_rows, conf = 1, 0.4        # tables need headers; flag for review
        header_cols = 0
        body = [p for p in placed if p["row"] >= header_rows and p["col"] == 0]
        if not plain_cols and body and all(p["bold"] for p in body if p["text"]) \
                and any(p["text"] for p in body) and ncols >= 2:
            header_cols = 1

        tbox = to_pdf(page, tb.bbox)
        table = StructNode(id=m.new_id("tbl"), role="Table", page=pno, bbox=tbox,
                           confidence=conf,
                           note=f"{nrows}×{ncols} grid, {header_rows} header row(s)" +
                                (", header column" if header_cols else "") +
                                (" — every cell is set the same way, so no header"
                                 if plain_rows and not header_rows else "") +
                                (f"; {slivers} empty sliver row/column dropped" if slivers else ""))
        table.style = {"header_rows": header_rows, "header_cols": header_cols}
        m.nodes[table.id] = table

        # an empty area spanning several columns/rows (e.g. the blank header
        # over "1 | c") becomes one blank cell per column/row, each read as
        # "Blank cell", so every row has the same cells a screen reader expects
        if self.opts.split_empty_merged_cells:
            split = []
            row_cells = collections.Counter(q["row"] for q in placed)
            for p in placed:
                # a blank row of its own (one cell across the whole grid, as a
                # worksheet rules an answer row) stays one cell, read once as
                # "Blank cell"; it is already as regular as the rows around it
                whole_row = p["cs"] >= ncols and p["rs"] == 1 and row_cells[p["row"]] == 1
                if not p["text"] and (p["rs"] > 1 or p["cs"] > 1) and not whole_row:
                    for rr in range(p["row"], p["row"] + p["rs"]):
                        for cc in range(p["col"], p["col"] + p["cs"]):
                            split.append({"box": (xs[cc], ys[rr], xs[cc + 1], ys[rr + 1]), "row": rr,
                                          "col": cc, "rs": 1, "cs": 1, "text": "", "bold": False,
                                          "split": True})
                else:
                    split.append(p)
            if len(split) != len(placed):
                table.note += "; empty merged cells split"
            placed = split

        use_ids = self.opts.table_headers_mode == "ids" or (
            self.opts.table_headers_mode == "auto" and any(p["rs"] > 1 or p["cs"] > 1 for p in placed))

        head_parent = body_parent = table.id
        if self.opts.table_thead_tbody and header_rows and nrows > header_rows:
            th = StructNode(id=m.new_id("thead"), role="THead", page=pno)
            tbd = StructNode(id=m.new_id("tbody"), role="TBody", page=pno)
            m.add(th, table.id)
            m.add(tbd, table.id)
            head_parent, body_parent = th.id, tbd.id
        elif self.opts.table_thead_tbody and not header_rows:
            # no header: the data rows still belong in a TBody
            tbd = StructNode(id=m.new_id("tbody"), role="TBody", page=pno)
            m.add(tbd, table.id)
            head_parent = body_parent = tbd.id

        # Two cells must never cover the same slot. A ruled form (a hairline box
        # drawn inside another) can give the grid a duplicate, and the row then
        # counts one column more than its neighbours — PAC's "irregular table
        # row" — and one of the two is left with no content at all.
        slots: set[tuple[int, int]] = set()
        kept = []
        changed = 0

        def free(row, rs, col, cs):
            return all((rr, cc) not in slots
                       for rr in range(row, row + rs) for cc in range(col, col + cs))

        for p in sorted(placed, key=lambda q: (not bool(q["text"]), q["rs"] * q["cs"])):
            row, rs, col, cs = p["row"], p["rs"], p["col"], p["cs"]
            if not free(row, rs, col, cs):
                # trim the columns that are already taken; what is left must be
                # one contiguous block, otherwise this cell is a duplicate
                while cs > 0 and not free(row, rs, col, 1):
                    col += 1
                    cs -= 1
                while cs > 0 and not free(row, rs, col + cs - 1, 1):
                    cs -= 1
                if cs <= 0 or not free(row, rs, col, cs):
                    changed += 1
                    continue
                p = dict(p, col=col, cs=cs, box=(xs[col], ys[row], xs[col + cs], ys[row + rs]))
                changed += 1
            for rr in range(row, row + rs):
                for cc in range(col, col + cs):
                    slots.add((rr, cc))
            kept.append(p)
        if changed:
            table.note += f"; {changed} overlapping cell(s) trimmed"
            # trimming can leave a hole: every row must still cover the full grid
            for rr in range(nrows):
                for cc in range(ncols):
                    if (rr, cc) in slots:
                        continue
                    box = (xs[cc], ys[rr], xs[cc + 1], ys[rr + 1])
                    kept.append({"box": box, "row": rr, "col": cc, "rs": 1, "cs": 1,
                                 "filler": True, "text": cell_text(box), "bold": False})
                    slots.add((rr, cc))
        placed = kept

        by_row = collections.defaultdict(list)
        for p in placed:
            by_row[p["row"]].append(p)
        cell_node: dict[tuple[int, int], StructNode] = {}
        for r in range(nrows):
            row_cells = sorted(by_row.get(r, []), key=lambda p: p["col"])
            if not row_cells:
                continue
            rb = _ubox(p["box"] for p in row_cells)
            tr = StructNode(id=m.new_id("tr"), role="TR", page=pno, bbox=to_pdf(page, rb))
            m.add(tr, head_parent if r < header_rows else body_parent)
            for p in row_cells:
                is_head = r < header_rows or (header_cols and p["col"] < header_cols)
                cell = StructNode(
                    id=m.new_id("cell"), role="TH" if is_head else "TD", page=pno,
                    bbox=to_pdf(page, p["box"]), text=p["text"],
                    rowspan=p["rs"], colspan=p["cs"],
                    scope=("Column" if r < header_rows else "Row") if is_head else "",
                    confidence=conf if is_head else 0.85)
                m.add(cell, tr.id)
                for rr in range(p["row"], p["row"] + p["rs"]):
                    for cc in range(p["col"], p["col"] + p["cs"]):
                        cell_node[(rr, cc)] = cell
        if getattr(self.opts, "lists_in_cells", True):
            done = 0
            for p in placed:
                node = cell_node.get((p["row"], p["col"]))
                if node is None or not p.get("text"):
                    continue
                inside = [l for l, _ in (hits.get(p["box"]) or [])]
                if len(inside) >= 3 and self.cell_list(page, pno, node, inside):
                    done += 1
            if done:
                table.note += f"; {done} cell(s) hold a list"

        if use_ids:
            for p in placed:
                node = cell_node.get((p["row"], p["col"]))
                if node is None or node.role != "TD":
                    continue
                hdrs = []
                for cc in range(p["col"], p["col"] + p["cs"]):
                    for rr in range(0, header_rows):
                        h = cell_node.get((rr, cc))
                        if h is not None and h.role == "TH" and h.id not in hdrs:
                            hdrs.append(h.id)
                for rr in range(p["row"], p["row"] + p["rs"]):
                    for cc in range(0, header_cols):
                        h = cell_node.get((rr, cc))
                        if h is not None and h.role == "TH" and h.id not in hdrs:
                            hdrs.append(h.id)
                node.headers = hdrs
        table.style["cells"] = {f"{r},{c}": n.id for (r, c), n in cell_node.items()}
        return table

    # ------------------------------------------------------------ lists
    def cell_list(self, page, pno: int, cell: StructNode, lines: list) -> bool:
        """A numbered or bulleted list printed inside one table cell.

        A rubric key is often set inside the cell under the grid:

            Rubric Score Key
            1 and 2: Student has not achieved an appropriate level of fluency
                     for the level of the passage.
            3: Fluent reading is being refined at the level of the passage.
            4: Fluent reading has been achieved for the level at which the
               passage is written.

        Read as one block of text, a screen reader gives no sense that these are
        three graded items. The cell keeps its place in the table; inside it the
        lead-in line becomes a `P` and the items an `L` > `LI` > `Lbl` + `LBody`,
        which is what PDF/UA asks for and what the page already shows."""
        m = self.model
        if cell.children or len(lines) < 3:
            return False
        rows = sorted(lines, key=lambda l: (round(l.y0, 1), l.x0))
        lead: list = []
        items: list[dict] = []
        for l in rows:
            lab = self._label(l, limit=None)
            if lab is None:
                (items[-1]["lines"] if items else lead).append(l)
                continue
            items.append({"lbl": lab[0], "lbox": lab[1], "lines": [l]})
        if len(items) < 2:
            return False
        # every item labelled the same way, set at the same margin, going up
        kinds = {self._numbering(it["lbl"]) for it in items}
        if len(kinds) != 1:
            return False
        left = [it["lbox"][0] for it in items]
        if max(left) - min(left) > 6.0:
            return False
        vals = [self._label_value(it["lbl"]) for it in items]
        if all(v[1] is not None for v in vals):
            if any(b[1] <= a[1] for a, b in zip(vals, vals[1:])):
                return False
        elif kinds == {"None"}:
            return False
        sizes = [l.size for it in items for l in it["lines"]]
        if max(sizes) - min(sizes) > 1.5:
            return False

        if lead:
            lb = _ubox(l.bbox for l in lead)
            text = " ".join(l.text for l in lead)
            cb = to_disp(page, cell.bbox) if cell.bbox is not None and house() else None
            if cb is not None and (lb[0] < cb.x0 - 1 or lb[2] > cb.x1 + 1):
                # a line MuPDF read across the cell's edge ("Composite coverage
                # index A composite variable …"): only what is printed in this cell
                inside = []
                for l in lead:
                    cs = [c for c in l.chars if cb.x0 - 0.5 <= (c[1][0] + c[1][2]) / 2 <= cb.x1 + 0.5]
                    if any(c[0].strip() for c in cs):
                        inside.append(("".join(c[0] for c in cs).strip(),
                                       (min(c[1][0] for c in cs), l.y0, max(c[1][2] for c in cs), l.y1)))
                if inside:
                    text = " ".join(t for t, _ in inside)
                    lb = _ubox(b for _, b in inside)
            m.add(StructNode(id=m.new_id("p"), role="P", page=pno, bbox=to_pdf(page, lb),
                             text=text, confidence=0.8,
                             note="lead-in line of the list inside this cell"), cell.id)
        lbl0 = items[0]["lbl"]
        L = StructNode(id=m.new_id("list"), role="L", page=pno,
                       bbox=to_pdf(page, _ubox(l.bbox for it in items for l in it["lines"])),
                       list_numbering=self._numbering(lbl0), confidence=0.75,
                       note="list printed inside a table cell")
        m.add(L, cell.id)
        for it in items:
            ibox = _ubox(l.bbox for l in it["lines"])
            first = it["lines"][0]
            after = [c for c in first.chars if c[0].strip() and c[1][0] > it["lbox"][2] + 0.5]
            text_x = after[0][1][0] if after else first.x1
            li = StructNode(id=m.new_id("li"), role="LI", page=pno, bbox=to_pdf(page, ibox))
            m.add(li, L.id)
            m.add(StructNode(id=m.new_id("lbl"), role="Lbl", page=pno, text=it["lbl"],
                             bbox=to_pdf(page, it["lbox"])), li.id)
            body_text = " ".join(l.text for l in it["lines"])
            mt = LABEL_RE.match(body_text)
            if mt:
                body_text = body_text[len(mt.group(0)):].strip()
            body = StructNode(id=m.new_id("lb"), role="LBody", page=pno, text=body_text,
                              bbox=to_pdf(page, (min(text_x - 0.5, ibox[0]) if len(it["lines"]) > 1
                                                 else text_x - 0.5, ibox[1], ibox[2], ibox[3])))
            m.add(body, li.id)
            if self.opts.detect_blank_lines and UNDERSCORE_RUN_RE.search(body_text):
                self._underscore_spans(page, pno, body, it["lines"])
        cell.note = (cell.note + "; " if cell.note else "") + \
            f"{len(items)} list item(s) tagged inside the cell"
        return True

    def _label(self, l: Line, limit: float | None = -1.0) -> tuple[str, tuple] | None:
        """The label this line starts with, and the box its characters sit in.

        `limit` is the largest size a label may be set in; the default is the
        running text of the page plus a little, because a numbered list is often
        set slightly larger ("1. pet" at 12 pt in an 11 pt book) while a real
        display size means a heading. Pass `limit=None` when the structure
        already says this is a list item — then only the wording decides."""
        if limit == -1.0:
            limit = self.body + 3.0
        if limit is not None and l.size > limit:
            return None
        m = LABEL_RE.match(l.text)
        if not m:
            return None
        lbl = m.group("lbl")
        vis = [c for c in l.chars if c[0].strip()]
        taken = []
        need = len(lbl.strip())
        for ch, box in vis:
            if need <= 0:
                break
            taken.append(box)
            need -= 1
        if not taken:
            return None
        return lbl, _ubox(taken)

    @staticmethod
    def _numbering(lbl: str) -> str:
        s = lbl.strip("().:").strip()
        if re.fullmatch(r"\d{1,3}(?:\s*(?:and|&|to|or|[–—-])\s*\d{1,3})?", s, re.I):
            return "Decimal"
        if lbl in ("◦", "○"):
            return "Circle"
        if lbl in ("▪", "■", "□", "◆", "◇"):
            return "Square"
        if s.isdigit():
            return "Decimal"
        if re.fullmatch(r"[ivxlc]+", s) and len(s) > 1 or s in ("i", "v", "x"):
            return "LowerRoman"
        if re.fullmatch(r"[IVXLC]+", s) and len(s) > 1 or s in ("I", "V", "X"):
            return "UpperRoman"
        if re.fullmatch(r"[a-z]", s):
            return "LowerAlpha"
        if re.fullmatch(r"[A-Z]", s):
            return "UpperAlpha"
        return "Disc"

    # ------------------------------------------------------------- page
    def _page(self, pno: int) -> None:
        page = self.doc[pno]
        if getattr(self, "page_body", None):
            # a large-print page is measured against its own running text
            self.body = max(self.page_body[pno], getattr(self, "page_main", self.page_body)[pno])
        m = self.model
        o = self.opts
        lines = self.page_lines[pno]
        tables = self._tables(page)
        tboxes = [tuple(t.bbox) for t in tables]
        fig_blocks, artifacts = self._figures(page, pno, lines, tboxes)
        blanks = self._blank_lines(page, lines, tboxes, fig_blocks)

        # partition lines
        header_art, footer_art, content = [], [], []
        top, _ = self._zones(page)
        for l in lines:
            if any(_center_in(l.bbox, t) for t in tboxes):
                continue
            if any(_center_in(l.bbox, b.bbox) for b in fig_blocks):
                continue
            is_art, sub = self._is_artifact_line(page, l)
            if is_art:
                node = self._art(page, pno, l.bbox, "Pagination", sub,
                                 "page number" if PAGENO_RE.match(l.text) else "running head/foot")
                node.text = l.text
                if PAGENO_RE.match(l.text) and getattr(o, "page_numbers", "artifact") == "P":
                    # this publisher wants the page number read: a P where it is
                    # printed — first on the page at the top, last at the bottom
                    node.role, node.artifact = "P", False
                    node.pagination_subtype = ""
                    num = re.sub(r"(?i)^\s*page\s*", "", l.text).strip()
                    word = (getattr(o, "page_number_text", "Page") or "").strip()
                    node.actual_text = f"{word} {num}".strip()
                    node.note = "page number, read as its /ActualText (option: page numbers)"
                    node.style = dict(node.style or {}, page_number=True)
                (header_art if l.y1 <= top else footer_art).append(node)
                continue
            content.append(l)

        items: list = content + fig_blocks + [Block("table", tuple(t.bbox), t) for t in tables] + \
            [Block("blank", order, zone) for zone, order in blanks]
        ordered: list = []
        if o.reading_order == "xycut":
            xycut(items, max(8.0, 0.8 * self.body), 0, ordered, [0])
            ordered = _grids_row_by_row(ordered, content)
        else:
            ordered = sorted(items, key=lambda i: (round(i.y0, 1), i.x0))

        for a in header_art:
            m.add(a)
        page_roots: list[StructNode] = []
        flows_right: dict[int, float] = collections.defaultdict(float)
        for it in ordered:
            if isinstance(it, Line):
                flows_right[it.flow] = max(flows_right[it.flow], it.x1)

        i = 0
        n = len(ordered)
        while i < n:
            it = ordered[i]
            if isinstance(it, Block):
                if it.kind == "table":
                    node = self._build_table(page, pno, it.payload, lines)
                    m.root_children.append(node.id)
                    node.parent = None
                elif it.kind == "blank":
                    node = StructNode(id=m.new_id("bl"), role="P", page=pno,
                                      bbox=to_pdf(page, it.payload), confidence=0.8,
                                      note="write-in line — read as the blank-line text")
                    node.style = {"blank_line": True}
                    m.add(node)
                else:
                    node = StructNode(id=m.new_id("fig"), role="Figure", page=pno,
                                      bbox=to_pdf(page, it.bbox), confidence=it.payload["conf"],
                                      note=("vector drawing — confirm it is a figure"
                                            if it.payload["kind"] == "vector" else
                                            "image — alt text required"))
                    inside = [l for l in lines if _center_in(l.bbox, it.bbox)]
                    node.text = " ".join(l.text for l in inside)[:200]
                    m.add(node)
                page_roots.append(node)
                i += 1
                continue
            # --- TOC run
            if o.detect_toc and TOC_RE.match(it.text):
                j = i
                run = []
                while j < n and isinstance(ordered[j], Line) and TOC_RE.match(ordered[j].text) \
                        and ordered[j].flow == it.flow:
                    run.append(ordered[j])
                    j += 1
                if len(run) >= 2:
                    page_roots.append(self._toc(page, pno, run))
                    i = j
                    continue
            # --- list run
            lab_it = self._label(it) if o.detect_lists else None
            if lab_it is not None and i > 0 and isinstance(ordered[i - 1], Line) \
                    and self._cut_sentence(ordered[i - 1], it, lab_it[0]):
                lab_it = None          # "… go on to Skill Set" / "2: Letter Sounds."
            if lab_it is not None and \
                    (not it.stem or self._list_follows(ordered, i)):
                j, node = self._list(page, pno, ordered, i)
                if node is not None:
                    page_roots.append(node)
                    i = j
                    continue
            # --- paragraph
            j = i + 1
            group = [it]
            while j < n:
                nx = ordered[j]
                if not isinstance(nx, Line) or self._breaks(group[-1], nx, flows_right[it.flow], group):
                    break
                group.append(nx)
                j += 1
            sp = self._split_pipes(page, pno, group[0]) if (len(group) == 1 and o.detect_blank_lines) else None
            if not sp and len(group) == 1 and o.detect_blank_lines:
                segs = split_prompts(group[0])         # "Name: ____  Date: ____"
                if segs:
                    sp = (segs, [])
            if sp:
                segs, arts = sp
                for seg in segs:
                    page_roots.append(self._paragraph(page, pno, [seg]))
                for a in arts:
                    m.add(a)
                i = j
                continue
            node = self._paragraph(page, pno, group)
            page_roots.append(node)
            i = j
        if o.detect_blank_lines:
            page_roots = self._numbered_order(page_roots)
            if o.blank_lines_as_list:
                page_roots = self._write_in_lists(pno, page_roots)
        if o.detect_lists:
            page_roots = self._join_numbered_runs(pno, page_roots, lines)
        # footnotes read after the page's main text
        notes = [nd for nd in page_roots if nd.role == "Note" and nd.parent is None]
        for nd in notes:
            m.root_children.remove(nd.id)
            m.root_children.append(nd.id)
        for a in footer_art:
            m.add(a)
        for a in artifacts:                    # decorative images etc. at their place
            m.add(a, None, position_index(m, m.root_children, a))

        self._attach_captions(page_roots)
        self._images_in_cells(page, pno)
        if o.tag_links or o.tag_form_fields or o.tag_other_annotations:
            self._annotations(page, pno, lines)

    def _breaks(self, a: Line, b: Line, col_right: float, group: list[Line]) -> bool:
        if a.flow != b.flow:
            return True
        size = max(a.size, b.size)
        if abs(a.size - b.size) > 0.6:
            return True
        if a.bold != b.bold and (len(a.text) < 90 or len(b.text) < 90):
            return True
        from .compat import house
        v5 = house()
        if v5 and a.bold and b.bold and len(a.text) < 40 and len(b.text) < 90 and _all_caps(a.text) \
                and not _all_caps(b.text):
            return True                # "METHODS" over "Study population and settings"
        gap = b.y0 - a.y1
        if gap > 0.62 * size:
            return True
        if gap < -0.5 * size:           # overlapping / side by side
            return True
        if b.x0 > a.x1 - 1:
            return True
        if self.opts.detect_lists and self._label(b) is not None:
            return True
        if self.opts.detect_toc and TOC_RE.match(b.text):
            return True
        short = a.x1 < col_right - 2.2 * size
        # "… Act 525.³⁻⁵" / "… output.¹⁸": the note marks after the full stop
        # do not hide the end of the sentence
        if short and (_without_marks(a) if v5 else a.text.rstrip()).endswith((".", "?", "!", ":", "”", '"')):
            return True
        if b.x0 > a.x0 + 0.8 * size and len(group) > 1 and short:
            return True
        # a journal's first-line indent after a full line (justified, or
        # filled out by its note marks: "… Act 525.³⁻⁵ ⁸ ¹⁰ ¹¹" | "  Ghana has …")
        if v5 and len(group) > 1 and a.x0 + 0.8 * size < b.x0 < a.x0 + 3 * size \
                and all(abs(g.x0 - a.x0) < 1.0 for g in group[1:]) \
                and _without_marks(a).endswith((".", "?", "!", ":", "”", '"')):
            return True
        return False

    def _vocab(self, pno: int) -> set:
        """The words this page prints normally.

        A line tracked out letter by letter may be spaced so evenly that the
        gaps say nothing about where one word ends; the words the page itself
        uses do — the logo line is the company name, and the company name is
        printed in full in the copyright line under it."""
        cache = getattr(self, "_vocab_cache", None)
        if cache is None:
            cache = self._vocab_cache = {}
        if pno not in cache:
            txt = " ".join(l.text for l in self.page_lines[pno]) if self.page_lines else ""
            cache[pno] = {w.lower() for w in re.findall(r"[A-Za-z]{3,}",
                                                        txt + " " + (self.model.title or ""))}
        return cache[pno]

    def _paragraph(self, page, pno, group: list[Line]) -> StructNode:
        m = self.model
        o = self.opts
        text = ""
        for l in group:
            if text.endswith("-") and l.text[:1].islower():
                text = text[:-1] + l.text
            else:
                text = (text + " " + l.text).strip()
        box = _ubox(l.bbox for l in group)
        size = max(l.size for l in group)
        bold = sum(l.bold for l in group) > len(group) / 2
        words = len(text.split())
        node = StructNode(id=m.new_id("p"), role="P", page=pno, bbox=to_pdf(page, box),
                          text=text, confidence=0.9)
        node.style = {"size": size, "bold": bold, "italic": group[0].italic,
                      "font": _font_family(group[0].font), "lines": len(group),
                      "ends": text.rstrip()[-1:] if text.strip() else ""}
        # a line tracked out letter by letter ("B e n c h m a r k   E d u c a t i o n")
        # is read letter by letter; its /ActualText says the words it spells
        if getattr(o, "join_spaced_letters", True) and looks_spaced(text):
            vocab = self._vocab(pno)
            said = spaced_words([(c, b) for l in group for c, b in l.chars], vocab)
            if said and reads_as_words(said, vocab) \
                    and said.replace(" ", "").lower() == text.replace(" ", "").lower():
                node.actual_text = said
                node.note = (node.note + "; " if node.note else "") + \
                    "letters set apart — read as the words they spell"
        ph = frame(page).height
        if any(l.stem for l in group):
            lab = self._stem_label(page, group[0])
            if lab:
                node.style["stem_label"] = lab
        if any(l.stem for l in group) or LABEL_ONLY_RE.match(text):
            node.note = "question number / text before a write-in line" if any(l.stem for l in group) \
                else "number or punctuation only"
        elif o.detect_formulas and _is_formula(text):
            # an equation is often set larger than the body text; that makes it a
            # display equation, not a heading, so this comes first
            node.role = "Formula"
            node.confidence = 0.6
            node.alt = mathspeak.speak(text)
            node.note = "looks like an equation — Alt is the formula read aloud; check it"
        elif size >= self.body + 0.9 and words <= 30 and len(group) <= 4 and not _is_prose(text):
            node.role = "H?"
            node.confidence = 0.8
            node.note = f"{size}pt text vs {self.body}pt body"
        elif (bold and size >= self.body - 0.3 and words <= 14 and len(group) <= 2
              and not _is_prose(text)
              and not text.rstrip().endswith((".", ",", ";"))
              and not CAPTION_RE.match(text)):
            node.role = "H?"
            node.confidence = 0.45
            node.note = "short bold line"
        elif o.detect_captions and CAPTION_RE.match(text) and words <= 80 and not _mentions_label(text, size, self.body):
            node.role = "Caption"
            node.confidence = 0.7
            node.note = "starts with a figure/table label"
        elif (o.detect_footnotes and box[1] > ph * 0.62 and size <= self.body * 0.9
              and NOTE_RE.match(text)):
            node.role = "Note"
            node.confidence = 0.55
            node.note = "small text at the page foot starting with a note mark"
        if node.role == "H?":
            self.heading_nodes.append(node)
            nm = NUMBERED_HEADING.match(text)
            node.style["num_depth"] = nm.group(1).count(".") + 1 if nm else 0
        m.add(node)
        if o.detect_blank_lines and UNDERSCORE_RUN_RE.search(text):
            self._underscore_spans(page, pno, node, group)
        return node

    def _underscore_spans(self, page, pno, node: StructNode, group: list[Line]) -> None:
        """'Number Accurate _______ of 26': the underscores become a Span read
        as the blank-line text (ActualText), the words stay in the P."""
        m = self.model
        for l in group:
            run: list = []
            before = " "
            for ch, box in list(l.chars) + [(" ", None)]:
                if ch == "_" and box is not None:
                    run.append(box)
                    continue
                # two underscores count when they start a word ("__73"), not when
                # they sit inside one
                if len(run) >= 3 or (len(run) == 2 and not (before.isalnum() or before == "_")):
                    # tight box: the spaces around the underscores stay in the
                    # P, so "… Accurate Blank Line of 26" keeps its word breaks
                    ub = _ubox(run)
                    d = min(2.0, (run[0][2] - run[0][0]) / 3)
                    ub = (ub[0] + d, ub[1], ub[2] - d, ub[3])
                    tick = len(run) == 2
                    sp = StructNode(id=m.new_id("bl"), role="Span", page=pno,
                                    bbox=to_pdf(page, ub), text="_" * len(run), confidence=0.8,
                                    note="underscore tick — read as the underscore text" if tick
                                    else "underscore blank — read as the blank-line text")
                    sp.style = {"blank_line": True, "underscores": True}
                    if tick:
                        sp.style["tick"] = True
                    m.add(sp, node.id)
                if not run:
                    before = ch
                run = []
        node.text = own_text(node.text, m, node)

    def _split_pipes(self, page, pno, l: Line):
        """'Number Accurate ___ of 26 | Number Automatic ___ of 26' -> two P
        (each with its blank) and the '|' divider as an artifact."""
        if "|" not in l.text or not UNDERSCORE_RUN_RE.search(l.text):
            return None
        segs: list[list] = [[]]
        pipes = []
        for ch, box in l.chars:
            if ch == "|":
                segs.append([])
                if box is not None:
                    pipes.append(box)
            else:
                segs[-1].append((ch, box))
        parts = [s for s in segs if "".join(c for c, _ in s).strip()]
        if len(parts) < 2 or not all(UNDERSCORE_RUN_RE.search("".join(c for c, _ in s)) for s in parts):
            return None
        out = []
        for s in parts:
            vis = [b for c, b in s if c.strip() and b is not None]
            if not vis:
                return None
            out.append(Line(text="".join(c for c, _ in s).strip(), bbox=_ubox(vis), size=l.size,
                            bold=l.bold, italic=l.italic, font=l.font, chars=list(s), flow=l.flow))
        arts = [self._art(page, pno, b, "Layout", "", "divider between two items") for b in pipes]
        for a in arts:
            a.text = "|"
        return out, arts

    @staticmethod
    def _row_values(run: list, built: list) -> list | None:
        """Pair each line of `run` with the item printed on the same row to its
        left — or None when any line is not such a value.

        A value is short (a score, "9/10 correct"), sits on one of the item's
        own rows, starts to the right of the item's text on that row and not
        far from it, and each item takes one value per row."""
        if not run:
            return None
        out, taken = [], set()
        for ln in run:
            if len((ln.text or "").split()) > 6:
                return None
            h = max(1.0, ln.y1 - ln.y0)
            best = None
            for k, it in enumerate(built):
                for r_i, row in enumerate(it["rows"]):
                    ov = min(ln.y1, row[3]) - max(ln.y0, row[1])
                    if ov < 0.5 * h or (k, r_i) in taken:
                        continue
                    gap = ln.x0 - row[2]
                    if -1.0 <= gap <= 12.0 * h:
                        if best is None or gap < best[0]:
                            best = (gap, k, r_i)
            if best is None:
                return None
            taken.add((best[1], best[2]))
            out.append((ln, built[best[1]]))
        return out

    def _column_top(self, pno: int, l) -> bool:
        """Is this line the first thing printed in its column (below the running
        head)? Only there can a list carry on from the column before."""
        lines = self.page_lines[pno] if getattr(self, "page_lines", None) else []
        if not lines:
            return True
        page_h = self.doc[pno].rect.height
        top = page_h * float(getattr(self.opts, "header_zone", 0.08) or 0.08)
        for o in lines:
            if o is l or not (o.text or "").strip() or o.y1 > l.y0 - 2 or o.y0 < top:
                continue
            ov = min(o.x1, l.x1) - max(o.x0, l.x0)
            if ov > 0.3 * max(1.0, min(o.x1 - o.x0, l.x1 - l.x0)):
                return False
        return True

    def _cut_sentence(self, prev, line, lbl: str) -> bool:
        """Does this line only carry on the sentence of the line above it?

        "If the score is 20 or more, go on to Skill Set" / "2: Letter Sounds." —
        a number at the start of a line is not a list of its own when the line
        above is the same paragraph (same margin, the next line down) and stops
        in the middle of a sentence, and the number is not where a list begins."""
        s = lbl.strip()
        if not re.fullmatch(r"\d{1,3}[:.)]", s) or self._label_value(s)[1] in (1, 0):
            return False
        tail = (prev.text or "").rstrip()
        if not tail or tail[-1] in ".:;!?)\u201d\"'" or self._label(prev) is not None \
                and prev.y1 > line.y0:
            return False
        gap = line.y0 - prev.y1
        if abs(prev.x0 - line.x0) > 3 or not (-0.5 * line.size <= gap <= 0.8 * line.size):
            return False
        return True

    def _wrapped_number(self, prev, lbl: str, cur_list) -> bool:
        """A line that only looks like it starts a new item.

        "…go on to Skill Set" / "2: Letter Sounds." — the number belongs to the
        sentence that was cut before it. It is taken for a wrapped line when the
        line before ends in the middle of a sentence and the number does not
        carry on the list it would join."""
        s = lbl.strip()
        if not re.fullmatch(r"\d{1,3}[:.)]", s):
            return False
        tail = (prev.text or "").rstrip()
        if not tail or tail[-1] in ".:;!?)\u201d\"'":
            return False
        kind = getattr(cur_list, "list_numbering", "") if cur_list is not None else ""
        if kind == "Decimal":
            last = None
            for c in reversed(cur_list.children):
                li = self.model.nodes.get(c)
                if li is None or li.role != "LI":
                    continue
                lb = next((self.model.nodes[k] for k in li.children
                           if self.model.nodes[k].role == "Lbl"), None)
                if lb is not None:
                    last = self._label_value(lb.text or "")[1]
                break
            if last is not None and self._label_value(s)[1] == last + 1:
                return False                  # 3. then 4. — a real next item
        return True

    def _list_follows(self, ordered, i) -> bool:
        """Is the labelled line at `i` part of a numbered list?

        A question with an answer line ("1. Tim kicked the ____") is normally
        handled as a write-in row, not as a list. But when the next numbered
        line carries on the same numbering, the page is a numbered list of
        questions and the answer line belongs inside its item."""
        first = self._label(ordered[i])
        if first is None:
            return False
        kind = self._numbering(first[0])
        for nx in ordered[i + 1:i + 8]:
            if not isinstance(nx, Line):
                continue
            lab = self._label(nx)
            if lab is None:
                continue
            return self._numbering(lab[0]) == kind and abs(nx.x0 - ordered[i].x0) <= 6
        return False

    def _list(self, page, pno, ordered, i) -> tuple[int, StructNode | None]:
        m = self.model
        n = len(ordered)
        stack: list[dict] = []
        top_list = None
        j = i
        flow = ordered[i].flow
        pending: list = []            # write-in lines met between two items
        last: dict | None = None      # the item built last, for side-by-side pieces
        built: list[dict] = []        # every item so far: its rows, for values set beside them

        def values_beside(at: int) -> int | None:
            """'• Task A:   9/10 correct' — the value is printed at a tab stop on
            the item's own row, and the reading order often puts the column of
            values after the items. Each value joins the item on its row; the
            list then carries on."""
            if not built:
                return None
            run, k2 = [], at
            while k2 < n and isinstance(ordered[k2], Line) and self._label(ordered[k2]) is None \
                    and len(run) < len(built):
                run.append(ordered[k2])
                k2 += 1
            hits = self._row_values(run, built)
            if not hits:
                return None
            for ln, it in hits:
                body = m.nodes[it["body"]]
                body.text = (body.text + " " + ln.text).strip()
                body.bbox = union(body.bbox, to_pdf(page, ln.bbox))
                m.nodes[it["li"]].bbox = union(m.nodes[it["li"]].bbox, body.bbox)
                for s_ in stack:
                    s_["L"].bbox = union(s_["L"].bbox, to_pdf(page, ln.bbox))
                it["rows"].append(ln.bbox)
            return at + len(hits)

        def blank_block(x) -> bool:
            return isinstance(x, Block) and x.kind == "blank"

        while j < n:
            l = ordered[j]
            if blank_block(l) and top_list is not None:
                # "1. Tim and Jim kicked the ______" — the answer line sits between
                # two numbered items; it belongs to the item above, and the list
                # carries on past it
                pending.append(l)
                j += 1
                continue
            if not isinstance(l, Line):
                break
            if l.flow != flow:
                lab_ahead = self._label(l)
                if lab_ahead is None:
                    nj = values_beside(j)
                    if nj is not None:
                        j = nj
                        continue
                # an answer line printed beside the question ("1. Tim kicked the
                # ______") is cut into its own column by the reading-order pass;
                # it is the end of that item, not the end of the list
                if lab_ahead is None and last is not None \
                        and l.y0 < last["box"][3] - 0.3 * l.size \
                        and l.y1 > last["box"][1] + 0.3 * l.size \
                        and l.x0 >= last["text_x"] - 3 \
                        and l.x0 - last["box"][2] < 4 * l.size:
                    # on the item's own rows and right beside it — not the top
                    # of the next column, which only starts higher up the page
                    body = m.nodes[last["body"]]
                    body.text = (body.text + " " + l.text).strip()
                    body.bbox = union(body.bbox, to_pdf(page, l.bbox))
                    m.nodes[last["li"]].bbox = union(m.nodes[last["li"]].bbox, body.bbox)
                    if self.opts.detect_blank_lines and UNDERSCORE_RUN_RE.search(l.text):
                        self._underscore_spans(page, pno, body, [l])
                    last["box"] = _ubox([last["box"], l.bbox])
                    j += 1
                    continue
                # a single bulleted/numbered list often runs from the bottom of
                # one newspaper column straight into the top of the next; only
                # bridge that one column change, and only when what follows is
                # still a label of the same kind (bullet/numbering) — anything
                # else ends the list here as before
                # "1. duck    The duck is swimming …" / "2. wing …": a second
                # column of text beside each item puts every row in a flow of its
                # own; the next label right under the last item, at the list's
                # margin, still carries the list on (V4.21.2)
                next_row = (top_list is not None and lab_ahead is not None and stack and last is not None
                            and self._numbering(lab_ahead[0]) == top_list.list_numbering
                            and abs(l.x0 - stack[0]["x"]) <= 3
                            and -0.3 * l.size <= l.y0 - last["box"][3] <= 2.5 * l.size)
                if not next_row and (top_list is None or lab_ahead is None
                                     or self._numbering(lab_ahead[0]) != top_list.list_numbering
                                     or not self._column_top(pno, l)):
                    # a list carries on into the next column only from the top
                    # of that column: a bulleted list further down it, under a
                    # paragraph and a heading of its own, is a different list
                    break
                flow = l.flow
                if stack and not next_row:
                    stack = [stack[0]]      # drop nested levels, keep the top list
                    stack[0]["x"] = l.x0    # re-baseline the indent to the new column
            lab = self._label(l)
            if lab is None:
                nj = values_beside(j)
                if nj is not None:
                    j = nj
                    continue
                break
            lbl, lbox = lab
            lx = l.x0
            while stack and lx < stack[-1]["x"] - 3:
                stack.pop()
            if stack and abs(lx - stack[-1]["x"]) <= 3:
                cur = stack[-1]
            else:
                if stack and lx <= stack[-1]["x"] + 3:
                    break
                parent_body = stack[-1]["last_body"] if stack else None
                L = StructNode(id=m.new_id("list"), role="L", page=pno, confidence=0.75,
                               list_numbering=self._numbering(lbl),
                               note="lines start with a bullet or number")
                m.add(L, parent_body)
                if top_list is None:
                    top_list = L
                cur = {"x": lx, "L": L, "last_body": None}
                stack.append(cur)
            # item lines
            vis = [c for c in l.chars if c[0].strip()]
            after = [c for c in vis if c[1][0] > lbox[2] + 0.5]
            text_x = after[0][1][0] if after else l.x1
            lx_label = l.x0
            item = [l]
            blanks: list = list(pending)
            pending = []
            k = j + 1
            while k < n:
                nx = ordered[k]
                if blank_block(nx) and nx.x0 > text_x - 24 and nx.y0 - item[-1].y1 < 3 * l.size:
                    blanks.append(nx)
                    k += 1
                    continue
                if not isinstance(nx, Line) or nx.flow != flow:
                    break
                nlab = self._label(nx)
                if nlab is not None and not self._wrapped_number(item[-1], nlab[0], cur["L"]):
                    break
                # a label line ending in ":" usually introduces a quoted/scripted
                # instruction on the next line, set with a little extra leading —
                # tolerate a bigger gap there than between two ordinary wrapped lines
                gap_limit = (1.0 if item[-1].text.rstrip().endswith(":") else 0.62) * nx.size
                # a wrapped line usually starts under the item text, but a
                # large-print book often indents it only a little past the
                # number ("3. Did Tim and Jim like Mom's story?" / "How can you
                # tell?"). Anything indented past the label belongs to the item;
                # a line back at the label margin starts something new.
                wrapped = nx.x0 >= text_x - 3 or nx.x0 > lx_label + 1
                if not wrapped or nx.y0 - item[-1].y1 > gap_limit or \
                        abs(nx.size - l.size) > 0.6:
                    break
                item.append(nx)
                k += 1
            ibox = _ubox(x.bbox for x in item)
            li = StructNode(id=m.new_id("li"), role="LI", page=pno, bbox=to_pdf(page, ibox))
            m.add(li, cur["L"].id)
            m.add(StructNode(id=m.new_id("lbl"), role="Lbl", page=pno,
                             bbox=to_pdf(page, lbox), text=lbl), li.id)
            body_text = " ".join(x.text for x in item)
            body_text = body_text[len(LABEL_RE.match(body_text).group(0)):].strip() \
                if LABEL_RE.match(body_text) else body_text
            body = StructNode(id=m.new_id("lb"), role="LBody", page=pno,
                              bbox=to_pdf(page, (text_x - 0.5, ibox[1], ibox[2], ibox[3])),
                              text=body_text)
            m.add(body, li.id)
            if self.opts.detect_blank_lines and UNDERSCORE_RUN_RE.search(body_text):
                self._underscore_spans(page, pno, body, item)
            for blk in blanks:
                bl = StructNode(id=m.new_id("bl"), role="P", page=pno,
                                bbox=to_pdf(page, blk.payload), confidence=0.8,
                                note="write-in line — read as the blank-line text")
                bl.style = {"blank_line": True}
                m.add(bl, body.id)
                li.bbox = union(li.bbox, bl.bbox)
                body.bbox = union(body.bbox, bl.bbox)
            cur["last_body"] = body.id
            last = {"body": body.id, "li": li.id, "box": ibox, "text_x": text_x}
            built.append({"body": body.id, "li": li.id, "rows": [x.bbox for x in item]})
            for s in stack:
                box = s["L"].bbox
                s["L"].bbox = union(box, to_pdf(page, ibox))
            j = k
        if top_list is None:
            return i, None
        items = [c for c in top_list.children if m.nodes[c].role == "LI"]
        # a single "1." or "a." line is far more often an initial, a numbered
        # heading or a stray reference than a genuine one-item list — UNLESS its
        # value clearly continues a sequence (e.g. a lone "4." on the page after
        # an earlier "1./2./3." list interrupted by a paragraph): that is almost
        # always the missing tail of the same numbered procedure, not a heading.
        # Restricted to plain digits: "UpperRoman"/"LowerRoman" single letters
        # false-match ordinary words (e.g. "CCVC.", a linguistics term, matches
        # the roman-numeral pattern), so continuation can't be told from a
        # coincidence there the way a bare digit count reliably can.
        num = lbl.strip("().")
        continues_sequence = top_list.list_numbering == "Decimal" and num.isdigit() and num != "1"
        if len(items) < 2 and top_list.list_numbering not in ("Disc", "Circle", "Square") \
                and not continues_sequence:
            m._drop_subtree(top_list.id)
            if top_list.id in m.root_children:
                m.root_children.remove(top_list.id)
            return i, None
        return j, top_list

    def _toc(self, page, pno, run: list[Line]) -> StructNode:
        m = self.model
        toc = StructNode(id=m.new_id("toc"), role="TOC", page=pno,
                         bbox=to_pdf(page, _ubox(l.bbox for l in run)), confidence=0.75,
                         note="dot-leader lines ending in page numbers")
        m.add(toc)
        stack = [(min(l.x0 for l in run), toc, None)]
        for l in run:
            while len(stack) > 1 and l.x0 < stack[-1][0] - 3:
                stack.pop()
            if l.x0 > stack[-1][0] + 6 and stack[-1][2] is not None:
                sub = StructNode(id=m.new_id("toc"), role="TOC", page=pno)
                m.add(sub, stack[-1][2].id)
                stack.append((l.x0, sub, None))
            item = StructNode(id=m.new_id("toci"), role="TOCI", page=pno,
                              bbox=to_pdf(page, l.bbox), text=l.text)
            m.add(item, stack[-1][1].id)
            x, t, _ = stack[-1]
            stack[-1] = (x, t, item)
            sb = t.bbox
            t.bbox = union(sb, to_pdf(page, l.bbox))
        return toc

    def _attach_captions(self, roots: list[StructNode]) -> None:
        m = self.model
        for k, node in enumerate(roots):
            if node.role != "Table":
                continue
            for d in (-1, 1):
                j = k + d
                if 0 <= j < len(roots) and roots[j].role == "Caption" and roots[j].parent is None:
                    cap = roots[j]
                    if not CAPTION_RE.match(cap.text) or not cap.text.lower().lstrip().startswith("tab"):
                        continue
                    gap = (cap.bbox[1] - node.bbox[3]) if d == -1 else (node.bbox[1] - cap.bbox[3])
                    if -2 <= gap <= 36:
                        m.detach(cap.id)
                        m.add(cap, node.id, 0 if d == -1 else None)
                        break

    def _images_in_cells(self, page, pno) -> None:
        m = self.model
        cells = [n for n in m.nodes.values() if n.page == pno and n.role in ("TD", "TH")]
        if not cells:
            return
        try:
            infos = page.get_image_info()
        except Exception:
            return
        for im in infos:
            box = to_pdf(page, im["bbox"])
            if min(box[2] - box[0], box[3] - box[1]) < self.opts.decorative_image_min_pt:
                continue
            for c in cells:
                if _center_in(box, c.bbox):
                    fig = StructNode(id=m.new_id("fig"), role="Figure", page=pno, bbox=box,
                                     confidence=0.5, note="image inside a table cell")
                    m.add(fig, c.id)
                    break
        # the picture is tagged in its cell — not a second time outside the table
        from .repair import dedupe_cell_figures
        dedupe_cell_figures(m)

    # -------------------------------------------------------- annotations
    def _annotations(self, page: pymupdf.Page, pno: int, lines: list[Line]) -> None:
        m = self.model
        o = self.opts
        ppage = self.pdf.pages[pno]
        annots = ppage.obj.get("/Annots")
        if not isinstance(annots, pikepdf.Array):
            return
        text_roles = {"P", "H1", "H2", "H3", "H4", "H5", "H6", "H?", "LBody", "Lbl", "TD", "TH",
                      "TOCI", "Caption", "Note", "Span", "Quote", "BlockQuote", "Reference"}
        leaves = [n for n in m.nodes.values()
                  if n.page == pno and not n.artifact and n.role in text_roles and n.bbox]
        figs = [n for n in m.nodes.values() if n.page == pno and n.role == "Figure" and n.bbox]
        for a in annots:
            if not isinstance(a, pikepdf.Dictionary) or not a.is_indirect:
                continue
            sub = str(a.get("/Subtype", ""))
            if sub in ("/Popup", "/PrinterMark", "/TrapNet"):
                continue
            if o.empty_markup_annots == "remove" and is_empty_markup(a):
                m.info["empty_review_marks"] = m.info.get("empty_review_marks", 0) + 1
                continue          # removed again at build time
            try:
                if int(a.get("/F", 0)) & 2:
                    continue
                x0, y0, x1, y1 = (float(v) for v in a.Rect)
            except Exception:
                continue
            rect = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
            quads = []
            qp = a.get("/QuadPoints")
            if isinstance(qp, pikepdf.Array) and len(qp) >= 8:
                vals = [float(v) for v in qp]
                for k in range(0, len(vals) - 7, 8):
                    xs, ys = vals[k:k + 8:2], vals[k + 1:k + 8:2]
                    quads.append((min(xs), min(ys), max(xs), max(ys)))
            rects = quads or [rect]
            objnum = a.objgen[0]
            if sub == "/Link" and o.tag_links:
                disp = [to_disp(page, r) for r in rects]
                text = " ".join(printed_text(page, r).strip() for r in disp).strip()
                text = re.sub(r"\s+", " ", text)
                node = StructNode(id=m.new_id("link"), role="Link", page=pno,
                                  bbox=_ubox(rects), annots=[objnum], text=text,
                                  confidence=0.85,
                                  note="link annotation" + ("" if text else " — no visible text"))
                if len(rects) > 1:
                    node.parts = [[pno, *r] for r in rects[1:]]
                    node.bbox = rects[0]
                host = max(leaves, key=lambda n: sum(intersection(r, n.bbox) for r in rects),
                           default=None)
                if host is not None and sum(intersection(r, host.bbox) for r in rects) > 0.2 * sum(area(r) for r in rects):
                    m.add(node, host.id)
                    continue
                fig = max(figs, key=lambda f: intersection(rect, f.bbox), default=None)
                if fig is not None and intersection(rect, fig.bbox) > 0.3 * area(fig.bbox):
                    sib = m.siblings(fig.id)
                    pos = sib.index(fig.id)
                    m.detach(fig.id)
                    node.bbox = fig.bbox
                    m.add(node, fig.parent, pos)
                    m.add(fig, node.id)
                    continue
                self._insert_by_position(node, pno)
            elif sub == "/Widget" and o.tag_form_fields:
                field = a if ("/T" in a or "/FT" in a) else a.get("/Parent")
                label = ""
                tu = str(field.get("/TU", "")) if isinstance(field, pikepdf.Dictionary) else ""
                disp = to_disp(page, rect)
                mid = (disp.y0 + disp.y1) / 2
                ft = str(field.get("/FT", "")) if isinstance(field, pikepdf.Dictionary) else ""
                left = [l for l in lines if l.x1 <= disp.x0 + 4 and disp.x0 - l.x1 < 200
                        and abs(l.cy - mid) < max(8, l.size)]
                right = [l for l in lines if l.x0 >= disp.x1 - 2 and l.x0 - disp.x1 < 60
                         and abs(l.cy - mid) < max(8, l.size)]
                above = [l for l in lines if l.y1 <= disp.y0 + 2 and disp.y0 - l.y1 < 24
                         and l.x0 < disp.x1 and l.x1 > disp.x0 - 20]
                order = [right, left] if ft == "/Btn" else [left, right]
                for group in order:
                    if group:
                        label = (min(group, key=lambda l: l.x0) if group is right
                                 else max(group, key=lambda l: l.x1)).text
                        break
                else:
                    if above:
                        label = max(above, key=lambda l: l.y1).text
                name = str(field.get("/T", "")) if isinstance(field, pikepdf.Dictionary) else ""
                node = StructNode(id=m.new_id("form"), role="Form", page=pno, bbox=rect,
                                  annots=[objnum], text=(tu or label),
                                  confidence=0.9 if tu else 0.5,
                                  note=f"form field {name}" + ("" if tu else
                                                                " — tooltip will come from the label; check it"))
                anchor = None
                if label:
                    for l in leaves:
                        if label and label[:20] in l.text:
                            anchor = l
                            break
                if anchor is not None:
                    top_anchor = anchor
                    while top_anchor.parent is not None and m.nodes[top_anchor.parent].role not in ("Document",):
                        top_anchor = m.nodes[top_anchor.parent]
                    sib = m.siblings(top_anchor.id)
                    m.add(node, top_anchor.parent, sib.index(top_anchor.id) + 1)
                else:
                    self._insert_by_position(node, pno)
            elif o.tag_other_annotations and sub not in ("/Link", "/Widget"):
                contents = str(a.get("/Contents", ""))
                node = StructNode(id=m.new_id("annot"), role="Annot", page=pno, bbox=rect,
                                  annots=[objnum], text=contents[:200],
                                  confidence=0.8 if contents else 0.4,
                                  note=f"{sub[1:]} annotation" + ("" if contents else " — no /Contents"))
                host = max(leaves, key=lambda n: intersection(rect, n.bbox), default=None)
                if host is not None and intersection(rect, host.bbox) > 0.3 * area(rect):
                    m.add(node, host.id)
                else:
                    self._insert_by_position(node, pno)

    def _insert_by_position(self, node: StructNode, pno: int) -> None:
        m = self.model
        top = node.bbox[3] if node.bbox else 0
        roots = m.root_children
        pos = len(roots)
        last_on_page = None
        for k, rid in enumerate(roots):
            r = m.nodes[rid]
            if r.artifact:
                continue
            if r.page == pno:
                last_on_page = k
                if r.bbox and r.bbox[3] < top - 1:
                    pos = k
                    break
            elif r.page > pno:
                pos = k
                break
        else:
            if last_on_page is not None:
                pos = last_on_page + 1
        m.add(node, None, pos)

    # ------------------------------------------------------------ headings
    def _demote_heading_grids(self) -> int:
        """A page of equally styled short blocks is a grid of items, not headings.

        Word cards, letter grids and spelling lists set every item in one large
        size; taken one by one each looks like a heading, and the file ends up
        with hundreds of them. A heading is rare on its page and introduces the
        content under it, so a large same-size group that fills the page is
        turned back into ordinary paragraphs."""
        by_page: dict[int, list] = collections.defaultdict(list)
        for h in self.heading_nodes:
            by_page[h.page].append(h)
        blocks: collections.Counter = collections.Counter()
        for n in self.model.nodes.values():
            if not n.artifact and n.page >= 0 and n.parent is None and \
                    n.role in ("P", "H?", "Caption", "Note", "L", "Table", "Figure", "Formula"):
                blocks[n.page] += 1
        demoted = set()
        for pno, hs in by_page.items():
            groups: dict[float, list] = collections.defaultdict(list)
            for h in hs:
                groups[round(float((h.style or {}).get("size", 0)), 1)].append(h)
            for size, g in groups.items():
                if len(g) < 8 or len(g) < 0.6 * max(1, blocks[pno]):
                    continue
                lens = sorted(len((x.text or "").strip()) for x in g)
                if lens[len(lens) // 2] > 12:
                    continue
                for x in g:
                    x.role = "P"
                    x.confidence = 0.75
                    x.note = f"one of {len(g)} same-size blocks on this page — a grid of items, not a heading"
                    x.style = dict(x.style or {}, grid_item=True)
                    demoted.add(x.id)
        if demoted:
            self.heading_nodes = [h for h in self.heading_nodes if h.id not in demoted]
            self.model.info["heading_grids_demoted"] = len(demoted)
        return len(demoted)

    def _assign_headings(self) -> None:
        hs = self.heading_nodes
        if not hs:
            return
        # pages of different formats (7 pt on Letter, 12 pt on a big sheet) rank
        # headings by size relative to their own page's body text
        pb = getattr(self, "page_body", None) or []
        relative = len({round(b, 1) for b in pb}) > 1

        def sig(h):
            if relative and 0 <= h.page < len(pb) and pb[h.page]:
                return (round(h.style["size"] / pb[h.page] * 4) / 4, h.style["bold"])
            return (h.style["size"], h.style["bold"])
        sigs = sorted({sig(h) for h in hs}, key=lambda s: (-s[0], not s[1]))
        rank = {s: i + 1 for i, s in enumerate(sigs)}
        maxl = max(1, min(6, int(self.opts.max_heading_levels)))

        # numbering depth offsets within one style
        by_sig = collections.defaultdict(list)
        for h in hs:
            by_sig[sig(h)].append(h)
        base_depth = {s: min((h.style.get("num_depth") or 99) for h in v) for s, v in by_sig.items()}

        bm = []
        if self.opts.headings_from_bookmarks and self.toc:
            bm = [(lvl, _norm(t), pg - 1) for lvl, t, pg in self.toc if t]
        for h in hs:
            s = sig(h)
            lvl = rank[s]
            if self.opts.headings_from_numbering:
                d = h.style.get("num_depth") or 0
                if d and base_depth[s] != 99:
                    lvl += d - base_depth[s]
            if bm:
                t = _norm(h.text)
                best = None
                for blvl, bt, bpg in bm:
                    if abs(bpg - h.page) > 1 or not bt:
                        continue
                    ratio = difflib.SequenceMatcher(None, t, bt).ratio() if t else 0
                    if t == bt or ratio > 0.86 or (len(bt) > 6 and t.startswith(bt)):
                        if best is None or ratio > best[1]:
                            best = (blvl, ratio)
                if best:
                    lvl = best[0]
                    h.confidence = 0.95
                    h.note = (h.note + "; " if h.note else "") + "matches a bookmark"
            h.style["level"] = min(max(1, lvl), maxl)
        # a style shared by both headings and plain paragraphs is weak evidence
        for h in hs:
            h.role = f"H{h.style['level']}"
        self._clamp_heading_levels()

    def _clamp_heading_levels(self) -> None:
        """No skipped levels, and the first heading is H1 (PDF/UA 7.4).

        In reading order, so it is run again after anything that moves a heading
        — a page that opens with its own title, say."""
        prev = 0
        for n in self.model.ordered_nodes():
            if n.role in ("H1", "H2", "H3", "H4", "H5", "H6") and not n.artifact:
                lv = int(n.role[1])
                new = 1 if prev == 0 else min(lv, prev + 1)
                if new != lv:
                    n.note = (n.note + "; " if n.note else "") + f"level adjusted from H{lv}"
                n.role = f"H{new}"
                prev = new

    # ------------------------------------------------------ joins, lang
    # ------------------------------------------- tables continued over pages
    def _table_rows(self, t: StructNode) -> tuple[list[StructNode], list[StructNode], StructNode]:
        """(header TRs, body TRs, node that holds the body rows) of a Table."""
        m = self.model
        k = int((t.style or {}).get("header_rows", 0) or 0)
        head: list[StructNode] = []
        body: list[StructNode] = []
        body_parent = t
        kids = [m.nodes[c] for c in t.children if c in m.nodes]
        if any(c.role in ("THead", "TBody") for c in kids):
            for c in kids:
                rows = [m.nodes[x] for x in c.children if x in m.nodes and m.nodes[x].role == "TR"]
                if c.role == "THead":
                    head += rows
                elif c.role == "TBody":
                    body += rows
                    body_parent = c
        else:
            rows = [c for c in kids if c.role == "TR"]
            head, body = rows[:k], rows[k:]
        return head, body, body_parent

    def _row_texts(self, rows: list[StructNode]) -> list[str]:
        m = self.model
        out = []
        for r in rows:
            for cid in r.children:
                c = m.nodes.get(cid)
                if c is not None:
                    out.append(_norm(c.text))
        return out

    def _col_edges(self, head: list[StructNode], page: int) -> list[float] | None:
        """Left edges of the header cells, as a fraction of the page width."""
        m = self.model
        if not head or page >= len(m.page_sizes):
            return None
        w = m.page_sizes[page][0] or 1.0
        xs = []
        for cid in head[-1].children:
            c = m.nodes.get(cid)
            if c is None or c.bbox is None:
                return None
            xs.append(c.bbox[0] / w)
        return sorted(xs)

    def _merge_continued_tables(self) -> int:
        """One Table for a table that runs over several pages.

        Each page gives one Table; a table whose header is printed again on the
        next page is still the same table. Whether page N+1 continues page N is
        decided from signals (same column positions and page format, header
        text, the title above the table, text after the table, how far down the
        page the table reached). Clear cases are decided by the rules; doubtful
        ones are asked to the AI model when `ai_table_merge` is on, otherwise
        they are flagged for review. Only the first page keeps the header (TH)
        and the title; repeated ones become artifacts."""
        m = self.model
        merged = 0
        roots = [m.nodes[r] for r in m.root_children if r in m.nodes]
        by_page: dict[int, list[StructNode]] = collections.defaultdict(list)
        for n in roots:
            if not n.artifact and n.page >= 0:
                by_page[n.page].append(n)
        tables = [n for n in roots if n.role == "Table" and n.parent is None and not n.artifact]
        per_page = collections.Counter(t.page for t in tables)

        def around(t: StructNode) -> tuple[list[StructNode], list[StructNode]]:
            seq = by_page[t.page]
            i = seq.index(t)
            keep = lambda xs: [x for x in xs if x.role != "Table" and (x.text or "").strip()]
            return keep(seq[:i]), keep(seq[i + 1:])

        def new_chain(t: StructNode) -> dict:
            head, body, _ = self._table_rows(t)
            lead, trail = around(t)
            return {"table": t, "lead": lead, "lead_text": self._lead_text(lead),
                    "sig": self._row_texts(head), "body_sig": self._rows_text(body)[:10],
                    "edges": self._col_edges(head, t.page),
                    "conf": t.confidence, "first_page": t.page, "last_page": t.page,
                    "last_bbox": t.bbox, "trail": trail, "ai_used": 0}

        chain = None
        self._ai_merge_on = bool(self.opts.ai_table_merge and self.ai_cfg is not None)
        v_ai = lambda v: bool(v and v.get("ai"))
        for u in tables:
            v = None
            if chain is not None and per_page[u.page] == 1 and per_page[chain["last_page"]] == 1 \
                    and u.page == chain["last_page"] + 1:
                lead_u, trail_u = around(u)
                v = self._table_verdict(chain, u, lead_u)
                if v["merge"]:
                    self._append_table(chain, u, lead_u, v)
                    chain["trail"] = trail_u
                    merged += 1
                    continue
            if chain is not None and u.page == chain["last_page"] + 1 and per_page[u.page] == 1 \
                    and per_page[chain["last_page"]] == 1 and v.get("doubtful"):
                u.note = (u.note + "; " if u.note else "") + \
                    f"check: not joined with the table on page {chain['last_page'] + 1} ({v['why']})"
                u.confidence = min(u.confidence, 0.5)
            elif chain is not None and v_ai(v):
                u.note = (u.note + "; " if u.note else "") + f"AI: new table — {v['ai']}"
                u.confidence = min(u.confidence, 0.6)
            chain = new_chain(u)
        if merged:
            self.model.info["merged_table_pages"] = merged
        return merged

    @staticmethod
    def _lead_text(nodes: list) -> str:
        txt = " ".join((n.text or "").strip() for n in nodes)
        txt = re.sub(r"\(?\s*(continued|cont\.?)\s*\)?", "", txt, flags=re.I)
        return _norm(txt)

    @staticmethod
    def _similar(a: str, b: str, lim: float = 0.9) -> bool:
        if a == b:
            return True
        if not a or not b:
            return False
        # "Level 1 …" vs "Level 2 …" or "Table 3" vs "Table 4" are different titles
        if re.findall(r"\d+", a) != re.findall(r"\d+", b):
            return False
        # short strings differing by a single letter ("Alignment A" / "B") are different
        # titles; fuzzy matching is only for long texts (OCR noise, hyphenation)
        if min(len(a), len(b)) < 40:
            return False
        return difflib.SequenceMatcher(None, a, b).ratio() >= lim

    def _table_verdict(self, chain: dict, u: StructNode, lead_u: list) -> dict:
        """Does the table on page `u.page` continue `chain`?  -> {merge, score, why, ai}"""
        m = self.model
        head, body, _ = self._table_rows(u)
        why: list[str] = []
        score = 0
        pa, pb = chain["last_page"], u.page
        sa, sb = m.page_sizes[pa], m.page_sizes[pb]
        if abs(sa[0] - sb[0]) > 6 or abs(sa[1] - sb[1]) > 6:
            return {"merge": False, "score": -9, "why": "page format changes", "ai": None}
        edges = self._col_edges(head, pb)
        if chain["edges"] is None or edges is None or len(edges) != len(chain["edges"]) or \
                any(abs(x - y) > 0.008 for x, y in zip(edges, chain["edges"])):
            return {"merge": False, "score": -9, "why": "columns differ", "ai": None}
        # the same rows printed again: a workbook that prints each worksheet
        # several times (one copy per type size) gives page after page with the
        # same columns and the same title. That is a repeated page, not a table
        # that carries on — joining them turns the second copy's first row into
        # a "repeated header" artifact and loses it.
        prev_rows = [r for r in (chain.get("body_sig") or []) if r.strip(" |")]
        new_rows = [r for r in self._rows_text(body)[:10] if r.strip(" |")]
        if len(prev_rows) >= 2 and len(new_rows) >= 2:
            same = len(set(prev_rows) & set(new_rows))
            if same >= max(2, int(0.6 * min(len(prev_rows), len(new_rows)))):
                return {"merge": False, "score": -9, "ai": None,
                        "why": f"{same} of the same rows are printed again — "
                               "a repeated page, not a continuation"}
        # header
        sig_u = self._row_texts(head)
        printed_u = u.confidence >= 0.5
        if printed_u and chain["conf"] >= 0.5 and any(sig_u) and any(chain["sig"]):
            if self._similar(" ".join(sig_u), " ".join(chain["sig"]), 0.9):
                score += 3
                why.append("same header")
            else:
                score -= 3
                why.append("different header")
        elif not printed_u and chain["conf"] >= 0.6:
            score += 1
            why.append("no header on this page")
        # title above the table
        lead_b = self._lead_text(lead_u)
        lead_a = chain["lead_text"]
        if lead_a and lead_b:
            if self._similar(lead_a, lead_b):
                score += 2
                why.append("same title")
            else:
                score -= 3
                why.append("title changed")
        elif lead_a and not lead_b:
            score += 1
            why.append("title only on the first page")
        elif lead_b and not lead_a:
            score -= 2
            why.append("new title appears")
        # text after the table on its last page
        if chain["trail"]:
            score -= 2
            why.append("text after the table")
        # how far down the page the previous table reached
        lb = chain["last_bbox"]
        if lb is not None:
            ph = sa[1] or 1.0
            if lb[1] < 0.3 * ph:
                score += 1
                why.append("table reaches the page bottom")
            elif lb[1] > 0.5 * ph:
                score -= 1
                why.append("table ends mid-page")
        verdict = {"score": score, "why": ", ".join(why), "ai": None}
        if score >= 3:
            verdict["merge"] = True
            return verdict
        if score <= -3:
            verdict["merge"] = False
            return verdict
        verdict["merge"] = score >= 2            # doubtful: default by score, flag
        if getattr(self, "_ai_merge_on", False):
            ans = self._ask_ai_merge(chain, u, lead_u, head, body, verdict)
            if ans is not None:
                verdict["merge"] = ans["same"]
                verdict["ai"] = ans["why"] or ("same table" if ans["same"] else "new table")
                return verdict
        verdict["doubtful"] = True
        return verdict

    def _rows_text(self, rows: list[StructNode]) -> list[str]:
        m = self.model
        return [" | ".join((m.nodes[c].text or "")[:60] for c in r.children if c in m.nodes) for r in rows]

    def _ask_ai_merge(self, chain: dict, u: StructNode, lead_u: list, head: list, body: list,
                      verdict: dict) -> dict | None:
        from .ai import OllamaError, table_continues
        m = self.model
        t = chain["table"]
        _, t_body, _ = self._table_rows(t)
        a_head = " | ".join(chain["sig"])
        ctx = {"a_title": chain["lead_text"], "a_head": a_head,
               "a_rows": self._rows_text(t_body[-2:]),
               "a_trail": " ".join((x.text or "") for x in chain["trail"])[:200],
               "b_title": self._lead_text(lead_u), "b_head": " | ".join(self._row_texts(head)),
               "b_rows": self._rows_text(body[:2]),
               "hints": f"score {verdict['score']} ({verdict['why']}); pages {chain['last_page'] + 1} and {u.page + 1}; "
                        f"titles: {'same' if verdict['why'].find('title changed') < 0 else 'different'}"}
        images = None
        if self.opts.ai_table_merge_vision:
            try:
                images = []
                for pno, part in ((chain["last_page"], (0.55, 1.0)), (u.page, (0.0, 0.45))):
                    page = self.doc[pno]
                    r = page.rect
                    clip = pymupdf.Rect(r.x0, r.y0 + r.height * part[0], r.x1, r.y0 + r.height * part[1])
                    images.append(page.get_pixmap(matrix=pymupdf.Matrix(1.2, 1.2), clip=clip).tobytes("png"))
            except Exception:
                images = None
        try:
            ans = table_continues(self.ai_cfg, ctx, images)
        except OllamaError as exc:
            self.model.warnings.append(f"AI table merge check stopped: {exc}")
            self._ai_merge_on = False
            return None
        except Exception:
            return None
        m.info.setdefault("ai_table_merge", []).append(
            {"pages": [chain["last_page"] + 1, u.page + 1], "same": None if ans is None else ans["same"],
             "why": (ans or {}).get("why", ""), "score": verdict["score"], "rules": verdict["why"]})
        return ans

    def _append_table(self, chain: dict, u: StructNode, lead_u: list | None = None,
                      verdict: dict | None = None) -> None:
        m = self.model
        t = chain["table"]
        head, body, _ = self._table_rows(u)
        _, _, t_body_parent = self._table_rows(t)
        same_head = u.confidence >= 0.5 and self._similar(" ".join(self._row_texts(head)),
                                                          " ".join(chain["sig"]), 0.85)
        # ---- id map: TH of the repeated header -> TH of the first header
        tcells = (t.style or {}).get("cells", {})
        ucells = (u.style or {}).get("cells", {})
        th_map = {}
        for key, uid in ucells.items():
            r = int(key.split(",")[0])
            if r < len(head) and key in tcells:
                th_map[uid] = tcells[key]
        if same_head:
            # repeated header rows -> running table heading (artifacts)
            for tr in head:
                if tr.bbox is None:
                    continue
                txt = " | ".join(m.nodes[c].text for c in tr.children if c in m.nodes and m.nodes[c].text)
                art = StructNode(id=m.new_id("art"), role="Artifact", page=tr.page, bbox=tr.bbox,
                                 text=txt, artifact=True, artifact_subtype="Pagination",
                                 pagination_subtype="Header", confidence=0.85,
                                 note=f"table header repeated on page {tr.page + 1} (running table heading)")
                m.add(art)
                self.model.info.setdefault("running_table_headings", 0)
                self.model.info["running_table_headings"] += 1
        else:
            # no real header on this page: its first row is data
            for tr in head:
                for cid in tr.children:
                    c = m.nodes.get(cid)
                    if c is not None and c.role == "TH":
                        c.role, c.scope, c.confidence = "TD", "", 0.6
                body.append(tr)
        # ---- move body rows
        first_row_shift = 0
        for key in tcells:
            first_row_shift = max(first_row_shift, int(key.split(",")[0]) + 1)
        for tr in body:
            m.detach(tr.id)
            m.add(tr, t_body_parent.id)
            for cid in tr.children:
                c = m.nodes.get(cid)
                if c is not None and c.headers:
                    c.headers = [th_map[h] for h in c.headers if h in th_map]
        # ---- drop what is left of the second table
        for tr in head if same_head else []:
            m.detach(tr.id)
            m._drop_subtree(tr.id)
        m.detach(u.id)
        m._drop_subtree(u.id)
        # ---- bookkeeping (editor grid, extent on later pages)
        skip = len(head) if same_head else 0
        for key, uid in ucells.items():
            r, c = (int(v) for v in key.split(","))
            if r < skip or uid not in m.nodes:
                continue
            tcells[f"{first_row_shift + r - skip},{c}"] = uid
        t.style["cells"] = tcells
        if u.bbox is not None:
            t.parts.append([u.page, *u.bbox])
        # ---- the title printed again above the table (or "continued") is a running head
        first_titles = {self._lead_text([n]) for n in chain["lead"]}
        for n in lead_u or []:
            key = self._lead_text([n])
            if (not key or key in first_titles or any(self._similar(key, k) for k in first_titles if k)) \
                    and n.id in m.nodes:
                n.role, n.artifact, n.artifact_subtype, n.pagination_subtype = "Artifact", True, "Pagination", "Header"
                n.children = []
                n.confidence = 0.8
                n.note = "table title repeated on the next page (running head)"
                self.heading_nodes = [h for h in self.heading_nodes if h.id != n.id]
        chain["last_page"] = u.page
        chain["last_bbox"] = u.bbox
        t.note = (t.note + "; " if t.note else "") + f"continues on page {u.page + 1}"
        if verdict:
            if verdict.get("ai"):
                t.note += f" (AI: same table — {verdict['ai']})"
                t.confidence = min(t.confidence, 0.6)
            elif verdict.get("doubtful"):
                t.note += f" (check: joined on weak evidence — {verdict['why']})"
                t.confidence = min(t.confidence, 0.5)

    def _join_split_paragraphs(self) -> int:
        m = self.model
        joined = 0
        seq = [m.nodes[r] for r in m.root_children]
        k = 0
        while k < len(seq):
            a = seq[k]
            if a.role != "P" or a.artifact or (a.style or {}).get("page_number"):
                k += 1
                continue
            j = k + 1
            # a page number read as a P sits between the two halves of the
            # paragraph; it is not part of either
            while j < len(seq) and (seq[j].artifact or seq[j].role == "Note"
                                    or (seq[j].style or {}).get("page_number")):
                j += 1
            if j >= len(seq):
                break
            b = seq[j]
            if b.role != "P" or not a.text or not b.text:
                k += 1
                continue
            ends_open = not a.text.rstrip().endswith((".", "?", "!", ":", ";", "”", '"', ")"))
            starts_low = b.text.lstrip()[:1].islower()
            # a word list or a grid of words ("discount", "index", "return"):
            # every word ends "open" and the next starts small, but nothing runs
            # on — a sentence carried over a column break is longer than a word
            # or two on each side, and is no item of a grid
            if (a.style or {}).get("grid_item") or (b.style or {}).get("grid_item") \
                    or len(a.text.split()) < 4 or len(b.text.split()) < 2 \
                    and len(a.text.split()) < 8:
                k += 1
                continue
            same = abs(a.style.get("size", 0) - b.style.get("size", 0)) < 0.35 and \
                a.style.get("font") == b.style.get("font")
            moved = b.page > a.page or (b.page == a.page and b.bbox and a.bbox and b.bbox[3] > a.bbox[3] + 5)
            if ends_open and starts_low and same and moved:
                if a.text.endswith("-"):
                    a.text = a.text[:-1] + b.text
                else:
                    a.text = a.text + " " + b.text
                a.parts.append([b.page, *b.bbox])
                a.parts.extend(b.parts)
                for cid in list(b.children):
                    m.nodes[cid].parent = a.id
                    a.children.append(cid)
                b.children = []
                m.remove(b.id)
                seq.pop(j)
                a.note = (a.note + "; " if a.note else "") + "joined with continuation on the next column/page"
                joined += 1
                continue
            k += 1
        return joined

    def _script_langs(self) -> int:
        doc_script = LANG_SCRIPT.get(self.model.lang.split("-")[0].lower(), "Latin")
        tagged = 0
        for n in self.model.nodes.values():
            if n.artifact or not n.text or n.role in ("Figure",):
                continue
            counts: collections.Counter = collections.Counter()
            letters = 0
            for ch in n.text:
                if not ch.isalpha():
                    continue
                letters += 1
                cp = ord(ch)
                for a, b, script, lang in SCRIPTS:
                    if a <= cp <= b:
                        counts[(script, lang)] += 1
                        break
                else:
                    if cp < 0x250:
                        counts[("Latin", "")] += 1
            if not letters:
                continue
            (script, lang), c = counts.most_common(1)[0] if counts else (("Latin", ""), 0)
            if script != doc_script and lang and c / letters >= 0.6:
                n.lang = lang
                tagged += 1
        return tagged

    def _doc_lang(self) -> str:
        lang = (self.opts.lang or "").strip()
        if lang and lang.lower() != "auto":
            return lang
        try:
            cat = str(self.pdf.Root.get("/Lang", "")).strip()
            if cat:
                return cat
        except Exception:
            pass
        counts: collections.Counter = collections.Counter()
        for lines in self.page_lines:
            for l in lines:
                for ch in l.text:
                    cp = ord(ch)
                    for a, b, script, lg in SCRIPTS:
                        if a <= cp <= b:
                            counts[lg] += 1
                            break
                    else:
                        if ch.isalpha():
                            counts["en"] += 1
        top = counts.most_common(1)[0][0] if counts else "en"
        return {"en": "en-US", "ta": "ta-IN", "hi": "hi-IN"}.get(top, top)

    def _title(self) -> str:
        o = self.opts
        meta = (self.doc.metadata or {}).get("title", "") or ""
        first_h1 = next((n.text for n in self.model.ordered_nodes() if n.role == "H1"), "")
        if o.title_source == "filename":
            return os.path.splitext(os.path.basename(self.path))[0].replace("_", " ")
        if o.title_source == "heading":
            return first_h1 or meta
        bad = not meta.strip() or re.search(r"\.(docx?|indd|qxp|pdf)$|^untitled|^microsoft word", meta, re.I)
        return first_h1 if bad and first_h1 else (meta or first_h1)

    # --------------------------------------------------------------- run
    def run(self) -> DocModel:
        self.progress(0.02, "reading text")
        self._doc_stats()
        self.model.lang = self._doc_lang()
        self.model.page_sizes = [(frame(p).width, frame(p).height) for p in self.doc]
        n = self.doc.page_count
        from .parallel import MIN_PAGES, page_procs
        procs = page_procs()
        done_parallel = False
        if self.only_pages is not None:
            # a few pages read again (Auto-tag pages → Re-analyse): the
            # document-wide statistics above, the pages one by one
            wanted = sorted(p for p in self.only_pages if 0 <= p < n)
            for k, pno in enumerate(wanted):
                self.progress(0.05 + 0.85 * k / max(1, len(wanted)), f"page {pno + 1} ({k + 1} of {len(wanted)})")
                try:
                    self._page(pno)
                except Exception as exc:
                    self.model.warnings.append(f"page {pno + 1}: analysis failed ({exc})")
            done_parallel = True
        elif procs > 1 and n >= MIN_PAGES:
            try:
                self._pages_parallel(procs)
                done_parallel = True
            except Exception as exc:          # fall back to one process
                self.model.warnings.append(f"parallel analysis unavailable ({exc}); used one process")
                self.model.nodes.clear()
                self.model.root_children.clear()
                self.heading_nodes = []
        if not done_parallel:
            for pno in range(n):
                self.progress(0.05 + 0.85 * pno / max(1, n), f"page {pno + 1} of {n}")
                try:
                    self._page(pno)
                except Exception as exc:
                    self.model.warnings.append(f"page {pno + 1}: analysis failed ({exc})")
        if self.opts.merge_continued_tables:
            try:
                self._merge_continued_tables()
            except Exception as exc:
                self.model.warnings.append(f"table merge across pages failed ({exc})")
        self._demote_heading_grids()
        if self.opts.detect_lists:
            try:
                self._fix_item_labels()
            except Exception as exc:
                self.model.warnings.append(f"list labels: {exc}")
        self._assign_headings()
        # any unassigned H? (should not happen)
        for node in self.model.nodes.values():
            if node.role == "H?":
                node.role = "P"
        if self.opts.table_thead_tbody:
            try:
                from .model import wrap_table_bodies
                wrap_table_bodies(self.model)
            except Exception as exc:
                self.model.warnings.append(f"table row groups: {exc}")
        if getattr(self.opts, "page_title_first", "margin") != "off":
            try:
                from .model import open_pages_with_heading
                if open_pages_with_heading(self.model, self.opts.page_title_first):
                    # the order decides the levels: a heading that moved to the
                    # front of its page must not leave a gap behind it
                    self._clamp_heading_levels()
            except Exception as exc:
                self.model.warnings.append(f"page title order: {exc}")
        if getattr(self.opts, "join_split_lists", True):
            # one list, printed with a note in the middle of it, and the points
            # set further in than the item they belong to
            try:
                from .repair import join_split_lists, nest_sub_items
                j2 = join_split_lists(self.model)
                n2 = nest_sub_items(self.model)
                if j2:
                    self.model.info["lists_joined"] = j2
                if n2:
                    self.model.info["sub_items_nested"] = n2
            except Exception as exc:
                self.model.warnings.append(f"list clean-up: {exc}")
        if self.opts.join_split_paragraphs:
            j = self._join_split_paragraphs()
            if j:
                self.model.info["joined_paragraphs"] = j
        if self.opts.script_language_tagging:
            t = self._script_langs()
            if t:
                self.model.info["lang_tagged"] = t
        house = (getattr(self.opts, "house_style", "") or "").strip()
        if house.lower() in ("none", "off"):
            house = ""
        if house:
            # the client's section headings, before the reference list and the
            # notes are looked for under them
            try:
                from .house import apply as house_apply
                st = house_apply(self, "early")
                if st:
                    self.model.info["house_style"] = {"style": house, **st}
            except Exception as exc:
                self.model.warnings.append(f"house style {house}: {exc}")
        if getattr(self.opts, "detect_bibliography", True):
            # a reference list: one BibEntry per entry, found from the hanging
            # indent / numbers / spacing, before the citations are matched to it
            try:
                from .biblio import tag_bibliography
                bib = tag_bibliography(self.model, self.doc, self.page_lines, self.opts, self.ai_cfg)
                if bib.get("entries"):
                    self.model.info["bibliography"] = bib
            except Exception as exc:
                self.model.warnings.append(f"reference list: {exc}")
        if getattr(self.opts, "detect_footnotes", True) or getattr(self.opts, "detect_index", True):
            # endnotes under a Notes heading, and the index as Index > L > LI
            try:
                from .backmatter import tag_endnotes, tag_index
                if getattr(self.opts, "detect_footnotes", True):
                    e = tag_endnotes(self.model)
                    if e:
                        self.model.info["endnotes"] = e
                if getattr(self.opts, "detect_index", True):
                    ix = tag_index(self.model, self.doc, self.page_lines, self.opts)
                    if ix.get("entries"):
                        self.model.info["index"] = ix
            except Exception as exc:
                self.model.warnings.append(f"notes / index: {exc}")
        if house:
            # the client's own tag structure (core/house.py), before the
            # cross-references are matched to their targets
            try:
                from .house import apply as house_apply
                st = house_apply(self, "pre")
                if st:
                    self.model.info.setdefault("house_style", {"style": house}).update(st)
            except Exception as exc:
                self.model.warnings.append(f"house style {house}: {exc}")
        if self.opts.detect_cross_references:
            # after the headings, captions and tables exist: a cross-reference can
            # only be matched once its target is in the tree
            try:
                from .xref import find_and_apply
                find_and_apply(self.model, self.page_lines, self.doc, self.opts)
            except Exception as exc:
                self.model.warnings.append(f"cross-reference search failed ({exc})")
        if house:
            try:
                from .house import apply as house_apply
                st = house_apply(self, "post")
                if st:
                    self.model.info.setdefault("house_style", {"style": house}).update(st)
            except Exception as exc:
                self.model.warnings.append(f"house style {house}: {exc}")
        self.model.title = self._title()
        self.model.check_integrity()
        self.progress(1.0, "analysis complete")
        self.doc.close()
        self.pdf.close()
        return self.model


def _analyze_chunk(path: str, opts_d: dict, ai_d: dict | None, shared: dict,
                   pages: list[int], ns: str) -> dict:
    """Worker process: analyse some pages with the document-wide statistics
    computed by the parent."""
    ai = None
    if ai_d:
        from .ai import AIConfig
        ai = AIConfig(**{k: v for k, v in ai_d.items() if k in AIConfig().__dict__})
    a = Analyzer(path, Options.from_dict(opts_d), None, ai)
    a.model.id_ns = ns
    a.body = a.doc_body = shared["body"]
    a.page_body = shared.get("page_body") or [a.body] * a.doc.page_count
    a.model.body_size = a.body
    a.edge_text = collections.Counter(shared["edge_text"])
    a._stamps = collections.Counter(shared.get("stamps") or {})
    a.slots = shared["slots"]
    a.image_pages = collections.Counter(shared["image_pages"])
    a.toc = shared["toc"]
    a.page_lines = [[] for _ in range(a.doc.page_count)]
    for p in pages:
        a.page_lines[p] = collect_lines(a.doc[p])
    for p in pages:
        try:
            a._page(p)
        except Exception as exc:
            a.model.warnings.append(f"page {p + 1}: analysis failed ({exc})")
    out = {"model": a.model.to_json(), "headings": [h.id for h in a.heading_nodes]}
    a.doc.close()
    a.pdf.close()
    return out


def split_body_columns(model: DocModel, path: str, ids=None) -> int:
    """An item set in two columns — "1. duck      The duck is swimming on the
    pond." — keeps its two texts apart: LBody > P "duck" + P "The duck is …"
    (V4.21.2, every client). The second text starts at a tab stop: a gap of
    1.5 em or more after a short first text (eight words at most)."""
    bodies = [n for n in model.nodes.values() if n.role == "LBody" and n.bbox is not None
              and n.page >= 0 and not n.children and n.source != "user" and (ids is None or n.id in ids)]
    if not bodies:
        return 0
    made = 0
    doc = pymupdf.open(path)
    try:
        cache: dict[int, list] = {}
        for b in bodies:
            if b.page >= doc.page_count:
                continue
            page = doc[b.page]
            if b.page not in cache:
                try:
                    cache[b.page] = collect_lines(page)
                except Exception:
                    cache[b.page] = []
            r = to_disp(page, b.bbox)
            rows = []
            for l in cache[b.page]:
                if l.rot or not (r.y0 - 1 <= l.cy <= r.y1 + 1):
                    continue
                ch = [(c, bx) for c, bx in l.chars if c.strip() and r.x0 - 1 <= (bx[0] + bx[2]) / 2 <= r.x1 + 1]
                if ch:
                    rows.append((l, ch))
            if not rows:
                continue
            rows.sort(key=lambda t: t[0].cy)
            # the first printed row (pieces on one baseline join)
            first_cy = rows[0][0].cy
            size = max(rows[0][0].size, 1.0)
            row1 = sorted([c for l, ch in rows if abs(l.cy - first_cy) < 0.4 * size for c in ch], key=lambda c: c[1][0])
            split = None
            for a, c in zip(row1, row1[1:]):
                gap = c[1][0] - a[1][2]
                if gap >= 1.5 * size and (split is None or gap > split[0]):
                    split = (gap, (a[1][2] + c[1][0]) / 2)
            if split is None:
                continue
            sx = split[1]
            left = [c for l, ch in rows for c in ch if (c[1][0] + c[1][2]) / 2 < sx]
            right = [c for l, ch in rows for c in ch if (c[1][0] + c[1][2]) / 2 >= sx]
            if not left or not right:
                continue

            def text_of(cs):
                lines_: dict = {}
                for c, bx in cs:
                    lines_.setdefault(round((bx[1] + bx[3]) / 2 / 2), []).append((c, bx))
                out = []
                for k in sorted(lines_):
                    seq = sorted(lines_[k], key=lambda t: t[1][0])
                    t = seq[0][0]
                    for p_, q in zip(seq, seq[1:]):
                        t += (" " if q[1][0] - p_[1][2] > 0.2 * size else "") + q[0]
                    out.append(t)
                return re.sub(r"\s+", " ", " ".join(out)).strip()
            lt, rt = text_of(left), text_of(right)
            # a short value at a tab stop ("Task A:   9/10 correct") stays with
            # its item's words; a second text of its own is three words or more
            if not lt or not rt or len(lt.split()) > 8 or len(rt.split()) < 3:
                continue
            for txt, cs, side in ((lt, left, "first"), (rt, right, "second")):
                p_ = StructNode(id=model.new_id("p"), role="P", page=b.page,
                                bbox=to_pdf(page, _ubox(bx for _, bx in cs)), text=txt, confidence=0.85,
                                note=f"the {side} text of an item set in two columns")
                model.add(p_, b.id)
            b.text = ""
            made += 1
    finally:
        doc.close()
    return made


def _chars_text(cs, size: float) -> str:
    """The words of some characters, line by line, left to right."""
    lines_: dict = {}
    for c, bx in cs:
        lines_.setdefault(round((bx[1] + bx[3]) / 2 / 2), []).append((c, bx))
    out = []
    for k in sorted(lines_):
        seq = sorted(lines_[k], key=lambda t: t[1][0])
        t = seq[0][0]
        for p_, q in zip(seq, seq[1:]):
            t += (" " if q[1][0] - p_[1][2] > 0.2 * size else "") + q[0]
        out.append(t)
    return re.sub(r"\s+", " ", " ".join(out)).strip()


PAIR_ROLES = {"P", "Span", "H?"} | set(HEADINGS)


def pair_item_rows(model: DocModel, path: str, lists=None) -> int:
    """A list whose items have a second text printed apart, at a tab stop on
    the right — "1. city        Traffic is heavy in the city." — when that
    text was read as paragraphs beside the list, or hung under one item
    (V4.21.4, every client): each line of it goes to the item on its row,
    LBody > P "city" + P "Traffic is heavy in the city.". Only when most items
    show the same tab stop on their own row, their first texts are short
    (eight words at most), and every line of the texts moved sits on an
    item's row (or wraps right under it) in the body size."""
    Ls = [n for n in model.nodes.values() if n.role == "L" and n.page >= 0 and n.bbox is not None
          and (lists is None or n.id in lists)]
    if not Ls:
        return 0
    made = 0
    doc = pymupdf.open(path)
    try:
        if lists is None:
            _join_row_lists(model, doc)
            _first_item_back(model, doc)
            Ls = [n for n in Ls if n.id in model.nodes]
        cache: dict[int, list] = {}
        blocked = ("L", "Table", "TOC", "TOCI", "Figure", "Form", "Index", "Caption", "Note")
        for L in Ls:
            if L.id not in model.nodes:
                continue
            items = [model.nodes[c] for c in L.children if c in model.nodes and model.nodes[c].role == "LI"]
            for pg in sorted({li.page for li in items if li.page >= 0}):
                if pg >= doc.page_count:
                    continue
                page = doc[pg]
                if pg not in cache:
                    try:
                        cache[pg] = collect_lines(page)
                    except Exception:
                        cache[pg] = []
                lines = [l for l in cache[pg] if not l.rot]
                rows = []
                for li in items:
                    if li.page != pg:
                        continue
                    kids = [model.nodes[c] for c in li.children if c in model.nodes]
                    lbl = next((k for k in kids if k.role == "Lbl" and k.bbox is not None), None)
                    body = next((k for k in kids if k.role == "LBody"), None)
                    if lbl is None or body is None or body.source == "user":
                        continue
                    if any(model.nodes[c].role not in PAIR_ROLES or model.nodes[c].children or model.nodes[c].source == "user"
                           for c in body.children if c in model.nodes):
                        continue
                    lr = to_disp(page, lbl.bbox)
                    cy = (lr.y0 + lr.y1) / 2
                    ch = sorted([(c, bx) for l in lines if abs(l.cy - cy) < 0.45 * max(l.size, 1.0)
                                 for c, bx in l.chars if c.strip() and bx[0] >= lr.x1 - 0.5], key=lambda t: t[1][0])
                    if not ch:
                        continue
                    sz = statistics.median([l.size for l in lines if abs(l.cy - cy) < 0.45 * max(l.size, 1.0)])
                    tab = None
                    for a_, b_ in zip(ch, ch[1:]):
                        if b_[1][0] - a_[1][2] >= 1.5 * sz:
                            tab = b_[1][0]
                            break
                    left = [c for c in ch if tab is None or c[1][0] < tab - 0.1]
                    own = [to_disp(page, x.bbox) for x in [body] + [model.nodes[c] for c in body.children if c in model.nodes]
                           if x.bbox is not None]
                    rows.append({"li": li, "body": body, "lr": lr, "tab": tab, "left": left, "all": ch, "size": sz,
                                 "own": own})
                    rows[-1]["mine"] = lambda x, own=own: any(
                        o.x0 - 1 <= (x[1][0] + x[1][2]) / 2 <= o.x1 + 1 and o.y0 - 1 <= (x[1][1] + x[1][3]) / 2 <= o.y1 + 1
                        for o in own)
                tabs = [r["tab"] for r in rows if r["tab"] is not None]
                if len(tabs) < max(2, 0.6 * len(rows)):
                    continue
                tab = statistics.median(tabs)
                size = statistics.median(r["size"] for r in rows)
                for r in rows:
                    if r["tab"] is None:
                        # a long first text leaves less room before the tab stop
                        ch = r["all"]
                        for a_, b_ in zip(ch, ch[1:]):
                            if abs(b_[1][0] - tab) <= 1.0 * size and b_[1][0] - a_[1][2] >= 0.4 * size:
                                r["tab"] = b_[1][0]
                                break
                    t_ = r["tab"]
                    allc = r["all"]
                    r["right"] = [c for c in allc if t_ is not None and c[1][0] >= t_ - 0.1]
                    r["left"] = [c for c in allc if t_ is None or c[1][0] < t_ - 0.1]
                # a short value at the tab stop ("Task A:   9/10 correct") stays with
                # its item's words; a second text of its own is three words or more
                ok_rows = [r for r in rows if r["tab"] is not None and abs(r["tab"] - tab) <= 1.5 * size
                           and 1 <= len(_chars_text(r["left"], size).split()) <= 8
                           and all(r["mine"](x) for x in r["left"])
                           and (not r["right"] or len(_chars_text(r["right"], size).split()) >= 3)]
                if len(ok_rows) < max(2, 0.6 * len(rows)):
                    continue
                ok_rows.sort(key=lambda r: r["lr"].y0)
                allowed = {r["body"].id for r in ok_rows}
                cands = []
                for n in list(model.nodes.values()):
                    if n.page != pg or n.bbox is None or n.artifact or n.children or n.source == "user" \
                            or n.role not in PAIR_ROLES:
                        continue
                    anc = model.ancestors(n.id)
                    if any(a.role in blocked for a in anc) and not (n.parent in allowed and n.role == "P"):
                        continue
                    d = to_disp(page, n.bbox)
                    if d.x0 < tab - 0.5 * size or d.y1 < ok_rows[0]["lr"].y0 - size \
                            or d.y0 > ok_rows[-1]["lr"].y1 + 3 * size:
                        continue
                    chars = []
                    ok = True
                    prev = None
                    for l in sorted(lines, key=lambda l_: l_.cy):
                        if not (d.y0 - 1 <= l.cy <= d.y1 + 1):
                            continue
                        ch = [(c, bx) for c, bx in l.chars if c.strip() and d.x0 - 1 <= (bx[0] + bx[2]) / 2 <= d.x1 + 1]
                        if not ch:
                            continue
                        hit = [r for r in rows if abs(l.cy - (r["lr"].y0 + r["lr"].y1) / 2) < 0.45 * max(l.size, 1.0)]
                        if hit and hit[0] not in ok_rows:
                            ok = False
                            break
                        if not hit and prev is None:
                            above = [r for r in ok_rows if r["lr"].y0 < l.cy]
                            prev = max(above, key=lambda r: r["lr"].y0) if above else None
                        if not hit and prev is not None:
                            # the wrapped line of the text above, before the next item's row
                            nxt = [r for r in rows if r["lr"].y0 > prev["lr"].y0]
                            if l.cy - prev["lr"].y1 < 2.5 * l.size and \
                                    (not nxt or l.cy < min(r["lr"].y0 for r in nxt) - 0.2 * l.size):
                                hit = [prev]
                        if len(hit) != 1 or not 0.8 * size <= l.size <= 1.25 * size:
                            ok = False
                            break
                        prev = hit[0]
                        chars.append((hit[0]["li"].id, ch))
                    if ok and chars and len({k for k, _ in chars}) > 1:
                        # one paragraph over several items: each item's part ends a
                        # sentence (not a paragraph of the next column cut in pieces)
                        parts_: dict = {}
                        for k, ch in chars:
                            parts_.setdefault(k, []).extend(ch)
                        order_ = sorted(parts_, key=lambda k: min(b[1] for _, b in parts_[k]))
                        if any(not re.search(r"[.!?:;\"\u201d\u2019)]$", _chars_text(parts_[k], size)) for k in order_[:-1]):
                            ok = False
                    if ok and chars:
                        cands.append((n, chars))
                cand_ids = {n.id for n, _ in cands}

                def own_left(c, r):
                    # the item's own first text, already a paragraph of its own
                    n_ = model.nodes[c]
                    if n_.bbox is None:
                        return False
                    d_ = to_disp(page, n_.bbox)
                    cy_ = (r["lr"].y0 + r["lr"].y1) / 2
                    return d_.x1 <= tab + 0.5 * size and abs((d_.y0 + d_.y1) / 2 - cy_) < 0.6 * size \
                        and d_.y1 - d_.y0 < 1.8 * size
                # an item keeps what it holds unless all of it is rebuilt
                good = [r for r in ok_rows if all(c in cand_ids or own_left(c, r) for c in r["body"].children)]
                bad_ids = {r["li"].id for r in ok_rows} - {r["li"].id for r in good}
                cands = [(n, ch) for n, ch in cands if not any(k in bad_ids for k, _ in ch)]
                per: dict[str, list] = {}
                for _, chars in cands:
                    for lid_, ch in chars:
                        per.setdefault(lid_, []).extend(ch)
                for r in good:
                    seen = {(c, round(b[0]), round(b[1])) for c, b in per.get(r["li"].id, [])}
                    # the item's own words at the tab stop (what other tags hold stays theirs)
                    extra = [x for x in r["right"] if (x[0], round(x[1][0]), round(x[1][1])) not in seen
                             and any(o.x0 - 1 <= (x[1][0] + x[1][2]) / 2 <= o.x1 + 1 and o.y0 - 1 <= (x[1][1] + x[1][3]) / 2 <= o.y1 + 1
                                     for o in r["own"])]
                    if extra:
                        per.setdefault(r["li"].id, []).extend(extra)
                # most items show the tab stop on their row (above); some may be
                # split already, the rest get their second texts here
                if not any(r["li"].id in per for r in good) or \
                        any(len({round(b[1] / 3) for _, b in v}) > 3 for v in per.values()):
                    continue
                todo = [r for r in good if per.get(r["li"].id) and r["left"]]
                if not todo:
                    continue
                for n, _ in cands:
                    if n.id in model.nodes:
                        model.remove(n.id, promote_children=False)
                for r in todo:
                    li, body = r["li"], r["body"]
                    ch = per[li.id]
                    for c in list(body.children):
                        if c in model.nodes:
                            model.remove(c, promote_children=False)
                    body.children = []
                    lt, rt = _chars_text(r["left"], size), _chars_text(ch, size)
                    lbox, rbox = _ubox(bx for _, bx in r["left"]), _ubox(bx for _, bx in ch)
                    model.add(StructNode(id=model.new_id("p"), role="P", page=pg, bbox=to_pdf(page, lbox), text=lt,
                                         confidence=0.85, note="the first text of an item set in two columns"), body.id)
                    model.add(StructNode(id=model.new_id("p"), role="P", page=pg, bbox=to_pdf(page, rbox), text=rt,
                                         confidence=0.85, note="the second text of the item, printed on its row"), body.id)
                    body.text = ""
                    body.bbox = to_pdf(page, _ubox([lbox, rbox]))
                    li.bbox = to_pdf(page, _ubox([tuple(to_disp(page, li.bbox)), lbox, rbox]))
                    made += 1
                if L.page == pg and not L.parts:
                    L.bbox = to_pdf(page, _ubox([tuple(to_disp(page, L.bbox))] +
                                                [tuple(to_disp(page, r["li"].bbox)) for r in rows]))
    finally:
        doc.close()
    return made


def _label_no(t: str):
    m_ = re.match(r"^\W*(\d{1,3}|[a-z]|[A-Z])\W*$", (t or "").strip())
    if not m_:
        return None
    v = m_.group(1)
    return int(v) if v.isdigit() else ord(v.lower()) - 96


def _first_item_back(model: DocModel, doc) -> int:
    """The first item of such a list read as a heading or a paragraph of its
    own over it ("1. band" H4, then "2. hum" … in the list): it goes back into
    the list when its number is the one before the list's first, it stands at
    the labels' left edge just over the list, and all between it and the list
    is printed to the right of the labels (V4.21.4)."""
    done = 0
    for L in [n for n in model.nodes.values() if n.role == "L" and 0 <= n.page < doc.page_count and not n.parts]:
        if L.id not in model.nodes:
            continue
        lis = [model.nodes[c] for c in L.children if c in model.nodes and model.nodes[c].role == "LI"]
        if len(lis) < 2:
            continue
        lb = next((model.nodes[c] for c in lis[0].children if c in model.nodes and model.nodes[c].role == "Lbl"), None)
        if lb is None or lb.bbox is None or lis[0].bbox is None:
            continue
        no = _label_no(lb.text)
        if no is None or no < 2:
            continue
        page = doc[L.page]
        lbd = to_disp(page, lb.bbox)
        first = to_disp(page, lis[0].bbox)
        sib = model.siblings(L.id)
        k = sib.index(L.id) - 1
        between = []
        while k >= 0 and len(between) <= len(lis) + 2:
            x = model.nodes.get(sib[k])
            if x is None or x.page != L.page or x.bbox is None or x.children or x.artifact or x.role not in PAIR_ROLES:
                break
            d = to_disp(page, x.bbox)
            if d.x0 > lbd.x1 + 2:
                between.append(x)
                k -= 1
                continue
            m_ = re.match(r"^\s*(\d{1,3}|[A-Za-z])([.)])\s+(\S.*)$", x.text or "")
            if not m_ or _label_no(m_.group(1) + m_.group(2)) != no - 1 or len(m_.group(3).split()) > 8 \
                    or abs(d.x0 - lbd.x0) > 6 or d.y1 > first.y0 + 2 or first.y0 - d.y1 > 2.5 * max(4.0, lbd.height):
                break
            # the label's own characters
            chars = [(c, bx) for l in collect_lines(page) if not l.rot and d.y0 - 1 <= l.cy <= d.y1 + 1
                     for c, bx in l.chars if c.strip() and d.x0 - 1 <= (bx[0] + bx[2]) / 2 <= d.x1 + 1]
            chars.sort(key=lambda t: t[1][0])
            lab = m_.group(1) + m_.group(2)
            if len(chars) <= len(lab) or "".join(c for c, _ in chars[:len(lab)]) != lab:
                break
            li = StructNode(id=model.new_id("li"), role="LI", page=L.page, bbox=x.bbox, confidence=0.85,
                            note="the first item, read apart from its list before")
            model.add(li, L.id, L.children.index(lis[0].id))
            model.add(StructNode(id=model.new_id("lbl"), role="Lbl", page=L.page, text=lab,
                                 bbox=to_pdf(page, _ubox(bx for _, bx in chars[:len(lab)]))), li.id)
            model.add(StructNode(id=model.new_id("lb"), role="LBody", page=L.page, text=m_.group(3).strip(),
                                 bbox=to_pdf(page, _ubox(bx for _, bx in chars[len(lab):]))), li.id)
            model.remove(x.id, promote_children=False)
            L.bbox = to_pdf(page, _ubox([tuple(to_disp(page, L.bbox)), tuple(d)]))
            done += 1
            break
    return done


def _join_row_lists(model: DocModel, doc) -> int:
    """A numbered list cut in pieces by the second texts of its items read
    between them ("7. gold" · "Everything the king …" · "8. found" …): the
    pieces join when the numbers go on (7 → 8) and all that is between them
    is printed to the right of the items, on the same page (V4.21.4)."""
    joined = 0
    parents = {n.parent for n in model.nodes.values() if n.role == "L"}
    for par in parents:
        if par is not None and par not in model.nodes:
            continue
        kids = model.root_children if par is None else model.nodes[par].children
        i = 0
        while i < len(kids):
            a = model.nodes.get(kids[i])
            if a is None or a.role != "L" or a.page < 0 or a.page >= doc.page_count or a.parts:
                i += 1
                continue
            j = i + 1
            between = []
            cap = len([c for c in a.children if c in model.nodes]) + 3
            while j < len(kids) and len(between) <= cap:
                b_ = model.nodes.get(kids[j])
                if b_ is None or b_.role == "L":
                    break
                between.append(b_)
                j += 1
            b = model.nodes.get(kids[j]) if j < len(kids) else None
            if b is None or b.role != "L" or len(between) > cap or b.page != a.page or b.parts:
                i += 1
                continue
            lia = [model.nodes[c] for c in a.children if c in model.nodes and model.nodes[c].role == "LI"]
            lib = [model.nodes[c] for c in b.children if c in model.nodes and model.nodes[c].role == "LI"]
            if not lia or not lib:
                i += 1
                continue

            def lbl(li):
                x = next((model.nodes[c] for c in li.children if c in model.nodes and model.nodes[c].role == "Lbl"), None)
                return _label_no(x.text) if x is not None else None
            na, nb = lbl(lia[-1]), lbl(lib[0])
            page = doc[a.page]
            boxes = [to_disp(page, li.bbox) for li in lia + lib if li.bbox is not None and li.page == a.page]
            if na is None or nb != na + 1 or not boxes:
                i += 1
                continue
            # the labels one under the other (the same left edge)
            la_ = [to_disp(page, x.bbox).x0 for li in (lia[-1], lib[0]) for x in model.descendants(li.id)
                   if x.role == "Lbl" and x.bbox is not None]
            if len(la_) != 2 or abs(la_[0] - la_[1]) > 6:
                i += 1
                continue
            # the item's own words end where its second text starts
            right = max(to_disp(page, x.bbox).x1 for li in lia + lib for x in model.descendants(li.id)
                        if x.role == "Lbl" and x.bbox is not None)
            ok = True
            for n in between:
                if n.role not in PAIR_ROLES or n.children or n.artifact or n.page != a.page or n.bbox is None:
                    ok = False
                    break
                # (the second texts of another list on the page may be read here too)
                if to_disp(page, n.bbox).x0 <= right:
                    ok = False
                    break
            if not ok:
                i += 1
                continue
            for li in lib:
                model.detach(li.id)
                model.add(li, a.id)
            ub = [tuple(to_disp(page, a.bbox)), tuple(to_disp(page, b.bbox))]
            a.bbox = to_pdf(page, _ubox(ub))
            model.remove(b.id, promote_children=False)
            joined += 1
            # the next piece may join too: look at `a` again
    return joined


def list_columns(model: DocModel, path: str, lists=None) -> int:
    """The two texts of list items set in two columns (V4.21.2, V4.21.4): on
    one line in the item, or read as paragraphs beside the list."""
    bodies = None
    if lists is not None:
        bodies = {d.id for x in lists if x in model.nodes for d in model.descendants(x) if d.role == "LBody"}
    n = split_body_columns(model, path, bodies)
    n += pair_item_rows(model, path, lists)
    model.info["rows_paired"] = 1
    return n


def analyze(path: str, opts: Options | None = None, progress: Callable | None = None,
            ai_cfg=None, pages=None) -> DocModel:
    """`pages`: analyse only these pages (0-based) — the rest of the document
    is still read for its statistics (body size, running heads …)."""
    a = Analyzer(path, opts or Options(), progress, ai_cfg)
    if pages is not None:
        a.only_pages = set(pages)
    m = a.run()
    from .model import convert_empty_headers
    convert_empty_headers(m)           # empty header cells are data cells (TD)
    try:
        n = list_columns(m, path)
        if n:
            m.info["two_column_items"] = n
    except Exception as exc:
        m.warnings.append(f"two-column list items: {exc}")
    # V4.24: an Academain Teacher's Guide gets the client's own structure
    try:
        from . import tguide
        if tguide.wanted(opts or Options(), path):
            if progress:
                progress(0.93, "Teacher's Guide structure")
            tguide.apply(m, path, pages=pages)
    except Exception as exc:
        m.warnings.append(f"Teacher's Guide structure: {exc}")
    return m
