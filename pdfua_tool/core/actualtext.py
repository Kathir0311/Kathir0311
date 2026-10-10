"""Reading a tag aloud: /ActualText for letters, patterns and highlighted parts.

A sound-spelling card prints "cat" with the a in red, or the pattern "u_e" with
the letters red and the underscore black. Read as it stands, a screen reader
says "cat" and "u e" and the child never learns which part the card is about.
What the publisher asks for is the card read out:

    cat   ->  c, a and t, a highlighted in red
    u_e   ->  u underscore e, u, e, are highlighted in red
    oo    ->  o, o, are highlighted in red

All of that is on the page already — the letters and the colour each glyph is
printed in — so the sentence is built from the page, not guessed:

  glyphs(page, bbox)   every glyph in the tag, in reading order, with its colour
  suggest(...)         the sentence above, ready for the person to edit

Nothing is written anywhere by this module; the editor shows the suggestion and
the person applies it.
"""
from __future__ import annotations

import collections
import re
import statistics

import pymupdf

from .textflags import TEXT_FLAGS

# a colour is named by the nearest of these, in sRGB 0..1
COLOURS: list[tuple[str, tuple[float, float, float]]] = [
    ("black", (0.0, 0.0, 0.0)), ("white", (1.0, 1.0, 1.0)), ("grey", (0.5, 0.5, 0.5)),
    ("red", (0.8, 0.1, 0.1)), ("dark red", (0.5, 0.0, 0.0)), ("orange", (0.95, 0.55, 0.1)),
    ("yellow", (0.95, 0.9, 0.2)), ("green", (0.1, 0.6, 0.2)), ("dark green", (0.0, 0.35, 0.1)),
    ("blue", (0.1, 0.3, 0.8)), ("light blue", (0.3, 0.65, 0.9)), ("navy", (0.05, 0.1, 0.4)),
    ("purple", (0.5, 0.2, 0.7)), ("pink", (0.95, 0.5, 0.7)), ("brown", (0.45, 0.3, 0.15)),
]
# what a character is called when it is not a letter
SPOKEN = {"_": "underscore", "-": "hyphen", "'": "apostrophe", "’": "apostrophe",
          ".": "dot", "/": "slash", "·": "dot", "˘": "breve", "¯": "macron",
          "ˉ": "macron", "́": "accent", "̆": "breve"}


def _rgb(span) -> tuple[float, float, float]:
    """The colour of a text span as sRGB 0..1, whatever space it was set in."""
    col = [float(v) for v in (span.get("color") or ())]
    if len(col) == 1:
        return (col[0], col[0], col[0])
    if len(col) == 3:
        return (col[0], col[1], col[2])
    if len(col) == 4:
        c, m, y, k = col
        return (max(0.0, 1 - min(1.0, c + k)), max(0.0, 1 - min(1.0, m + k)),
                max(0.0, 1 - min(1.0, y + k)))
    return (0.0, 0.0, 0.0)


def colour_name(rgb: tuple[float, float, float]) -> str:
    """The everyday name of a colour — what a person would call it."""
    r, g, b = rgb
    best, bestd = "black", 9.0
    for name, ref in COLOURS:
        d = (r - ref[0]) ** 2 + (g - ref[1]) ** 2 + (b - ref[2]) ** 2
        if d < bestd:
            best, bestd = name, d
    return best


def _key(rgb) -> tuple[int, int, int]:
    return (round(rgb[0] * 20), round(rgb[1] * 20), round(rgb[2] * 20))


def glyphs(page, bbox=None) -> list[tuple[str, tuple[float, float, float], tuple]]:
    """Every printed glyph (character, colour, box), in reading order.

    `bbox` is in the page's own space; without it the whole page is read."""
    out: list[tuple[str, tuple, tuple]] = []
    try:
        trace = page.get_texttrace()
    except Exception:
        return out
    clip = pymupdf.Rect(bbox) if bbox is not None else None
    for sp in trace:
        if sp.get("type") == 3 or float(sp.get("opacity", 1.0) or 0) < 0.1:
            continue                     # invisible text (a cell's "Blank cell")
        rgb = _rgb(sp)
        for ch in sp.get("chars") or ():
            try:
                code, _gid, _origin, cb = ch[0], ch[1], ch[2], ch[3]
            except Exception:
                continue
            c = chr(code)
            if not c.strip():
                continue
            if clip is not None:
                cx, cy = (cb[0] + cb[2]) / 2, (cb[1] + cb[3]) / 2
                if not (clip.x0 - 1 <= cx <= clip.x1 + 1 and clip.y0 - 1 <= cy <= clip.y1 + 1):
                    continue
            out.append((c, rgb, tuple(cb)))
    out.sort(key=lambda g: (round(g[2][1] / 4), g[2][0]))
    return out


def page_text_colour(page) -> tuple[float, float, float]:
    """The colour the page sets its text in — what counts as *not* highlighted."""
    seen: collections.Counter = collections.Counter()
    colours: dict = {}
    for _c, rgb, _b in glyphs(page):
        k = _key(rgb)
        seen[k] += 1
        colours[k] = rgb
    if not seen:
        return (0.0, 0.0, 0.0)
    return colours[seen.most_common(1)[0][0]]


def _spoken(c: str) -> str:
    return SPOKEN.get(c, c)


def spell(chars: list[str]) -> str:
    """The characters as a reader should say them.

    Every character is its own item, separated by commas, so a reader pauses
    between them: "t, a, hyphen b, l, e". A symbol is said with the letter it
    belongs to ("hyphen b", "underscore e"), because that is how the spelling
    pattern is taught. Plain letters get "and" before the last when there are
    three or more ("c, a and t") and a plain comma when there are two ("o, o");
    once a symbol is in the mix the list stays commas all through."""
    parts: list[str] = []
    i = 0
    while i < len(chars):
        c = chars[i]
        if c in SPOKEN and i + 1 < len(chars) and chars[i + 1] not in SPOKEN:
            parts.append(f"{SPOKEN[c]} {chars[i + 1]}")      # "hyphen b"
            i += 2
            continue
        parts.append(_spoken(c))
        i += 1
    if not parts:
        return ""
    if any(c in SPOKEN for c in chars):
        return ", ".join(parts)
    if len(parts) >= 3:
        return ", ".join(parts[:-1]) + " and " + parts[-1]
    return ", ".join(parts)


def describe(chars: list[str], highlighted: list[int], colour: str) -> str:
    """The sentence: the characters, then which of them are in colour."""
    said = spell(chars)
    if not said or not highlighted:
        return said
    marked = [chars[i] for i in highlighted]
    if len(highlighted) >= len(chars):          # the whole thing is in colour
        return f"{said}, {'are' if len(chars) > 1 else ''} highlighted in {colour}".replace(", ,", ",") \
            .replace("  ", " ")
    names = [_spoken(c) for c in marked]
    if len(names) == 1:
        return f"{said}, {names[0]} highlighted in {colour}"
    return f"{said}, {', '.join(names)}, are highlighted in {colour}"


def page_vocabulary(page, extra: str = "") -> set:
    """The words this page prints normally — the dictionary for a tracked line."""
    try:
        txt = page.get_text(flags=TEXT_FLAGS) or ""
    except Exception:
        txt = ""
    return {w.lower() for w in re.findall(r"[A-Za-z]{3,}", txt + " " + (extra or ""))}


def looks_spaced(text: str) -> bool:
    """Is this line set as separated letters — "B e n c h m a r k"?

    A publisher's logo line is often tracked out letter by letter. The glyphs
    carry no word spaces of their own, so a screen reader reads "B, e, n, c…"
    one letter at a time."""
    words = (text or "").split()
    if len(words) < 6:
        return False
    singles = sum(1 for w in words if len(w) == 1)
    return singles >= max(5, 0.7 * len(words))


def _split_with_vocab(word: str, vocab: set) -> str:
    """"benchmarkeducationcompany" -> "benchmark education company".

    Only with words the file itself uses elsewhere — the logo line is the
    company name, and the company name is printed in full in the copyright line
    on the same page. Longest match from the left, and the whole run has to come
    out as real words or nothing is changed."""
    low = word.lower()
    if len(low) < 10 or not vocab:
        return ""
    out: list[str] = []
    i = 0
    while i < len(low):
        for j in range(len(low), i + 2, -1):
            if low[i:j] in vocab:
                out.append(word[i:j])
                i = j
                break
        else:
            return ""
    return " ".join(out) if len(out) > 1 else ""


def reads_as_words(said: str, vocab: set) -> bool:
    """Did the letters really come back together as words?

    A phonics card prints rows of single letters too ("a b c d e f"), and those
    are letters, not a word — joining them would invent "abcdef". The result is
    only used when every piece of it is a word the page itself uses."""
    toks = [t for t in (said or "").split() if len(t) >= 3]
    return bool(toks) and any(len(t) >= 4 for t in toks) and all(t.lower() in vocab for t in toks)


def spaced_words(pairs: list, vocab: set | None = None) -> str:
    """"B e n c h m a r k  e d u c a t i o n" -> "Benchmark education".

    The letters are put back into words by the gaps between them: inside a word
    the tracking is even, and a word break is a gap half again as wide. Where the
    line is tracked so evenly that the gaps say nothing, the words the file uses
    elsewhere do (`vocab`). Takes [(character, box)] in reading order."""
    items = [(c, b) for c, b in pairs if c and not c.isspace() and b]
    if len(items) < 6:
        return ""
    rows: list[list] = []
    for c, b in items:
        if rows and abs(b[1] - rows[-1][-1][1][1]) <= max(2.0, (b[3] - b[1]) * 0.5):
            rows[-1].append((c, b))
        else:
            rows.append([(c, b)])
    out: list[str] = []
    for row in rows:
        row = sorted(row, key=lambda x: x[1][0])
        gaps = [row[i + 1][1][0] - row[i][1][2] for i in range(len(row) - 1)]
        inner = [g for g in gaps if g > -1]
        if not inner:
            continue
        step = statistics.median(inner)
        limit = max(step * 1.6, step + (row[0][1][3] - row[0][1][1]) * 0.22, 0.8)
        word = row[0][0]
        for (c, _b), gap in zip(row[1:], gaps):
            if gap > limit:
                out.append(word)
                word = c
            else:
                word += c
        out.append(word)
    said = [w for w in out if w]
    if vocab:                            # the gaps may not mark the words at all
        said = [(_split_with_vocab(w, vocab) or w) for w in said]
    return " ".join(said).strip()


def suggest(page, bbox, text: str = "", normal=None) -> dict:
    """What this tag should be read as, from what is printed in it.

    Returns {"text": the sentence, "chars": [...], "highlighted": [...],
    "colour": name}. Falls back to the tag's own text when the glyphs cannot be
    read (an image of a word, for instance)."""
    gs = glyphs(page, bbox)
    if not gs:
        chars = [c for c in (text or "").strip() if not c.isspace()]
        return {"text": spell(chars), "chars": chars, "highlighted": [], "colour": ""}
    # a line tracked out letter by letter is read as the words it spells
    printed = " ".join(g[0] for g in gs)
    if looks_spaced(text) or looks_spaced(printed):
        vocab = page_vocabulary(page)
        said = spaced_words([(g[0], g[2]) for g in gs], vocab)
        if said and reads_as_words(said, vocab):
            return {"text": said, "chars": [g[0] for g in gs], "highlighted": [],
                    "colour": "", "spaced": True}
    base = normal if normal is not None else page_text_colour(page)
    chars = [g[0] for g in gs]

    def far(rgb) -> bool:
        return sum((a - b) ** 2 for a, b in zip(rgb, base)) ** 0.5 > 0.25

    # Colour is the highlight, not whichever colour happens to be commonest.
    # A card of syllables prints half of every word in red ("muf-fin",
    # "num-ber"), so on that page red is the commonest colour and the black
    # hyphen was being announced as the highlighted part. Where a tag holds both
    # coloured and plain black glyphs, the coloured ones are the highlight.
    chroma = [i for i, (_c, rgb, _b) in enumerate(gs) if max(rgb) - min(rgb) >= 0.22]
    plain = [i for i, (_c, rgb, _b) in enumerate(gs) if max(rgb) - min(rgb) < 0.12]
    if chroma and plain:
        marked = list(chroma)
    elif chroma:                       # every glyph is coloured: against the page
        marked = [i for i in chroma if far(gs[i][1])]
        if not marked and max(base) - min(base) < 0.12:
            marked = list(chroma)
    else:                              # nothing coloured here
        marked = [i for i, (_c, rgb, _b) in enumerate(gs) if far(rgb)]
    colours: collections.Counter = collections.Counter()
    for i in marked:
        colours[colour_name(gs[i][1])] += 1
    name = colours.most_common(1)[0][0] if colours else ""
    return {"text": describe(chars, marked, name), "chars": chars,
            "highlighted": marked, "colour": name}
