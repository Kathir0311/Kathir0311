"""Mathematics in a PDF: spotting it, and writing it out in words.

A `Formula` element must carry an /Alt (PDF/UA 7.3), and "equation" is not an
alt text — a screen-reader user needs to hear the formula itself. Two things
happen here, both without a model:

  looks_like_math(text)  decides whether a line is an expression rather than a
                         sentence, from the symbols and the shape of the text
  speak(text)            reads an expression out: operators become words,
                         superscripts become "to the power of", a fraction bar
                         becomes "over"

The result is exact for the plain expressions a textbook sets inline (a² + b²
= c², 3 × 4 ÷ 2, x ≤ 5, 45°, H₂O) and honest about what it cannot see: a
stacked fraction or a big integral drawn as graphics is a Figure, not text, so
the vision model handles those (core/ai.py, `math_from_image`).
"""
from __future__ import annotations

import re
import unicodedata

# symbol -> how it is read, in the order a reader meets it
OPERATORS = {
    "+": "plus", "-": "minus", "−": "minus", "–": "minus", "—": "minus", "±": "plus or minus",
    "∓": "minus or plus", "×": "times", "⋅": "times", "·": "times", "∗": "times",
    "÷": "divided by", "/": "divided by", "=": "equals", "≠": "is not equal to",
    "≈": "is approximately", "≅": "is congruent to", "≡": "is identical to",
    "<": "is less than", ">": "is greater than", "≤": "is less than or equal to",
    "≥": "is greater than or equal to", "≪": "is much less than", "≫": "is much greater than",
    "√": "the square root of", "∛": "the cube root of", "∑": "the sum of", "∏": "the product of",
    "∫": "the integral of", "∂": "partial", "∞": "infinity", "∝": "is proportional to",
    "°": "degrees", "%": "percent", "‰": "per mille", "π": "pi", "θ": "theta", "α": "alpha",
    "β": "beta", "γ": "gamma", "δ": "delta", "Δ": "delta", "λ": "lambda", "μ": "mu",
    "σ": "sigma", "Σ": "the sum of", "φ": "phi", "ω": "omega", "Ω": "omega", "τ": "tau",
    "ρ": "rho", "ε": "epsilon", "→": "goes to", "↔": "is equivalent to", "∈": "is in",
    "∉": "is not in", "∪": "union", "∩": "intersection", "⊂": "is a subset of",
    "*": "times", "∠": "angle", "⊥": "is perpendicular to", "∥": "is parallel to", "∴": "therefore",
    "∵": "because", "¬": "not", "∧": "and", "∨": "or", "∀": "for all", "∃": "there exists",
    "|": "the absolute value of", "!": "factorial",
}
SUPERSCRIPT = {"⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5", "⁶": "6",
               "⁷": "7", "⁸": "8", "⁹": "9", "⁺": "+", "⁻": "-", "ⁿ": "n", "ⁱ": "i"}
SUBSCRIPT = {"₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4", "₅": "5", "₆": "6",
             "₇": "7", "₈": "8", "₉": "9", "₊": "+", "₋": "-", "ₙ": "n", "ₓ": "x"}
VULGAR = {"½": "one half", "⅓": "one third", "⅔": "two thirds", "¼": "one quarter",
          "¾": "three quarters", "⅕": "one fifth", "⅙": "one sixth", "⅛": "one eighth",
          "⅜": "three eighths", "⅝": "five eighths", "⅞": "seven eighths"}
POWER_WORD = {"2": "squared", "3": "cubed"}
# after these, a minus sign is the sign of what follows, not a subtraction
_BEFORE_NEGATIVE = set(OPERATORS.values()) | {"open bracket"}

MATH_CHARS = set(OPERATORS) | set(SUPERSCRIPT) | set(SUBSCRIPT) | set(VULGAR)
# a sentence may well contain one of these; an expression is built from them
STRONG = set("=≠≈≤≥<>±∑∏∫√∞∝∈∉⊂∪∩") | set(SUPERSCRIPT) | set(SUBSCRIPT) | set(VULGAR)
WORD_RE = re.compile(r"[A-Za-z]{3,}")
EQUATION_LABEL_RE = re.compile(r"^\s*\(?\s*(?:eq(?:uation)?\.?\s*)?\d+(?:\.\d+)*\s*\)?\s*$", re.I)


def _strip_label(text: str) -> tuple[str, str]:
    """'(3.1)  a² + b² = c²' -> ('a² + b² = c²', '3.1'): the number printed beside
    an equation is its label, not part of the expression."""
    m = re.match(r"^\s*\(([\d.]+)\)\s+(.+)$", text)
    if m:
        return m.group(2), m.group(1)
    m = re.match(r"^(.+?)\s+\(([\d.]+)\)\s*$", text)
    if m:
        return m.group(1), m.group(2)
    return text, ""


def looks_like_math(text: str, *, min_chars: int = 2) -> bool:
    """Is this line an expression rather than prose?

    Prose that merely mentions a number ("in 2019 we found 12 cases") must not
    match, so a line needs real mathematical symbols and must not read as a
    sentence of ordinary words."""
    t = (text or "").strip()
    if len(t) < min_chars or len(t) > 400:
        return False
    body, _ = _strip_label(t)
    body = body.strip()
    if not body:
        return False
    strong = sum(1 for c in body if c in STRONG)
    ops = sum(1 for c in body if c in MATH_CHARS or c in "+-*/^")
    words = WORD_RE.findall(body)
    long_words = [w for w in words if len(w) > 3]
    letters = sum(1 for c in body if c.isalpha())
    digits = sum(1 for c in body if c.isdigit())
    if not strong and ops < 2:
        return False
    # "x = 5" is maths; "Here 45° is a common angle and E = mc² is famous" is a
    # sentence that mentions maths — an expression carries almost no prose
    if len(long_words) > 2 or len(words) > 6:
        return False
    if letters and digits == 0 and strong <= 1 and len(words) > 2:
        return False
    # a run of slashes is phonics notation (/m/ /t/ /a/), not division: an
    # expression always carries at least one real mathematical symbol
    return bool(strong >= 1 and (digits or letters or ops >= 2))


def _number_word(tok: str) -> str:
    return tok


def speak(text: str) -> str:
    """Read an expression aloud, as words a screen reader can pronounce."""
    src, label = _strip_label((text or "").strip())
    out: list[str] = []
    i = 0
    n = len(src)
    while i < n:
        ch = src[i]
        if ch in SUPERSCRIPT:
            run = ""
            while i < n and src[i] in SUPERSCRIPT:
                run += SUPERSCRIPT[src[i]]
                i += 1
            out.append(POWER_WORD.get(run, f"to the power of {run}"))
            continue
        if ch in SUBSCRIPT:
            run = ""
            while i < n and src[i] in SUBSCRIPT:
                run += SUBSCRIPT[src[i]]
                i += 1
            out.append(f"sub {run}")
            continue
        if ch in VULGAR:
            out.append(VULGAR[ch])
            i += 1
            continue
        if ch == "^":
            j = i + 1
            run = ""
            while j < n and (src[j].isalnum() or src[j] in "+-"):
                run += src[j]
                j += 1
            if run:
                out.append(POWER_WORD.get(run, f"to the power of {run}"))
                i = j
                continue
        if ch == "_":
            j = i + 1
            run = ""
            while j < n and src[j].isalnum():
                run += src[j]
                j += 1
            if run:
                out.append(f"sub {run}")
                i = j
                continue
        if ch in OPERATORS:
            # a minus directly in front of a value is "negative", between two is "minus"
            if ch in "-−–" and (not out or out[-1] in _BEFORE_NEGATIVE):
                out.append("negative")
            else:
                out.append(OPERATORS[ch])
            i += 1
            continue
        if ch in "([{":
            out.append("open bracket")
            i += 1
            continue
        if ch in ")]}":
            out.append("close bracket")
            i += 1
            continue
        if ch.isspace():
            i += 1
            continue
        run = ""
        while i < n and not src[i].isspace() and src[i] not in OPERATORS and \
                src[i] not in SUPERSCRIPT and src[i] not in SUBSCRIPT and src[i] not in VULGAR \
                and src[i] not in "()[]{}^_":
            run += src[i]
            i += 1
        if run:
            out.append(_number_word(run))
        elif i < n:
            i += 1
    said = " ".join(x for x in out if x).strip()
    said = re.sub(r"\s+", " ", said)
    said = re.sub(r"\s+([,.;:])", r"\1", said)
    if label:
        said = f"{said}, equation {label}"
    return said[:400]


def to_latex(text: str) -> str:
    """A plain-text expression as LaTeX, for the /AF or a note beside the alt.

    Only what is unambiguous in the characters themselves is translated."""
    src, _ = _strip_label((text or "").strip())
    out = []
    i = 0
    greek = {"π": r"\pi", "θ": r"\theta", "α": r"\alpha", "β": r"\beta", "γ": r"\gamma",
             "δ": r"\delta", "Δ": r"\Delta", "λ": r"\lambda", "μ": r"\mu", "σ": r"\sigma",
             "Σ": r"\sum", "φ": r"\phi", "ω": r"\omega", "Ω": r"\Omega", "τ": r"\tau",
             "ρ": r"\rho", "ε": r"\epsilon"}
    sym = {"×": r"\times", "÷": r"\div", "±": r"\pm", "∓": r"\mp", "≤": r"\leq", "≥": r"\geq",
           "≠": r"\neq", "≈": r"\approx", "≡": r"\equiv", "∞": r"\infty", "→": r"\to",
           "∈": r"\in", "∉": r"\notin", "⊂": r"\subset", "∪": r"\cup", "∩": r"\cap",
           "∑": r"\sum", "∏": r"\prod", "∫": r"\int", "∂": r"\partial", "°": r"^{\circ}",
           "⋅": r"\cdot", "·": r"\cdot", "−": "-", "–": "-"}
    while i < len(src):
        c = src[i]
        if c in SUPERSCRIPT:
            run = ""
            while i < len(src) and src[i] in SUPERSCRIPT:
                run += SUPERSCRIPT[src[i]]
                i += 1
            out.append("^{" + run + "}")
            continue
        if c in SUBSCRIPT:
            run = ""
            while i < len(src) and src[i] in SUBSCRIPT:
                run += SUBSCRIPT[src[i]]
                i += 1
            out.append("_{" + run + "}")
            continue
        if c == "√":
            out.append(r"\sqrt")
            i += 1
            continue
        out.append(greek.get(c, sym.get(c, c)))
        i += 1
    return re.sub(r"\s+", " ", "".join(out)).strip()[:400]


def normalise(text: str) -> str:
    """Characters as the file stores them, so the comparisons above see the real
    symbols even when the font used a compatibility code point."""
    return unicodedata.normalize("NFKC", text or "")


# ------------------------------------------------- reading a set equation
_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve"]
_ORDINAL = {2: ("half", "halves"), 3: ("third", "thirds"), 4: ("quarter", "quarters"),
            5: ("fifth", "fifths"), 6: ("sixth", "sixths"), 7: ("seventh", "sevenths"),
            8: ("eighth", "eighths"), 9: ("ninth", "ninths"), 10: ("tenth", "tenths"),
            12: ("twelfth", "twelfths"), 100: ("hundredth", "hundredths")}


def fraction_words(num: str, den: str) -> str:
    """'1', '5' -> 'one-fifth'; '3', '4' -> 'three-quarters'; '1', '2' -> 'one half'."""
    try:
        a, b = int(num), int(den)
    except ValueError:
        return ""
    if not (0 <= a < len(_ONES)) or b not in _ORDINAL:
        return ""
    one, many = _ORDINAL[b]
    word = one if a == 1 else many
    return f"{_ONES[a]} {word}" if b == 2 else f"{_ONES[a]}-{word}"


def speak_region(page, rect, flags: int | None = None, exclude=()) -> tuple[str, str]:
    """Read a displayed equation from where its glyphs sit on the page.

    The characters alone lose what the layout says: a fraction is a numerator
    set over a drawn bar over a denominator, a subscript is a smaller glyph
    below the line. Returns (spoken form, linear text) — "CCI equals one-fifth
    times open bracket FP plus the fraction ANC4 plus SBA over 2, …" and
    "CCI = 1/5 (FP + (ANC4+SBA)/2 + …)" — or ("", "") when nothing is there."""
    import pymupdf
    if flags is None:
        from .textflags import RAW_FLAGS as flags  # type: ignore[no-redef]
    rect = pymupdf.Rect(rect)
    try:
        raw = page.get_text("rawdict", clip=rect + (-1, -1, 1, 1), flags=flags)
    except Exception:
        return "", ""
    chars = []
    for b in raw.get("blocks", []):
        for l in b.get("lines", []):
            if tuple(l.get("dir", (1, 0))) != (1, 0) and abs(l.get("dir", (1, 0))[1]) > 0.1:
                continue
            for sp in l.get("spans", []):
                for ch in sp.get("chars", []):
                    c = ch.get("c", "")
                    if not c or not c.strip():
                        continue
                    bx = pymupdf.Rect(ch["bbox"])
                    pt = pymupdf.Point((bx.x0 + bx.x1) / 2, (bx.y0 + bx.y1) / 2)
                    if not rect.contains(pt) or any(pymupdf.Rect(x).contains(pt) for x in exclude):
                        continue
                    chars.append({"c": c, "box": bx, "size": float(sp.get("size", 0)) or bx.height,
                                  "oy": float(ch["origin"][1])})
    if not chars:
        return "", ""
    bars = []
    try:
        for d in page.get_drawings():
            for it in d.get("items", []):
                if it[0] == "re":
                    r = pymupdf.Rect(it[1])
                elif it[0] == "l":
                    r = pymupdf.Rect(it[1], it[2])
                    r.normalize()
                else:
                    continue
                if r.height <= 1.6 and r.width >= 3 and rect.x0 - 2 <= r.x0 and r.x1 <= rect.x1 + 2 \
                        and rect.y0 - 1 <= (r.y0 + r.y1) / 2 <= rect.y1 + 1:
                    bars.append(r)
    except Exception:
        bars = []
    parts = _linear(chars, bars)
    return (say_tree(parts) or _say(parts)), _write(parts)


def _linear(chars: list, bars: list) -> list:
    """Characters and fraction bars -> parts in reading order:
    ("t", text) on the line, ("sub"|"sup", text), ("frac", num parts, den parts)."""
    import statistics as st
    if not chars:
        return []
    sizes = [round(c["size"], 1) for c in chars]
    main = max(set(sizes), key=sizes.count)
    if not bars:
        # more than one line (an equation set over two lines): each in turn
        big = [c for c in chars if c["size"] >= 0.9 * main] or chars
        rows: list[list[float]] = []
        for v in sorted(c["oy"] for c in big):
            if rows and v - rows[-1][-1] < 1.15 * main:
                rows[-1].append(v)
            else:
                rows.append([v])
        if len(rows) > 1:
            bases = [st.median(r) for r in rows]
            by: dict[int, list] = {}
            for c in chars:
                k = min(range(len(bases)), key=lambda j: abs(c["oy"] - bases[j]))
                by.setdefault(k, []).append(c)
            out: list = []
            for k in sorted(by):
                seg = _linear(by[k], [])
                if out and seg:
                    out.append(("t", ", "))
                out.extend(seg)
            return out
    # the widest bar first: a fraction inside a numerator is handled there
    items: list = []
    left = list(chars)
    used: set = set()
    for k, bar in sorted(enumerate(bars), key=lambda kb: -kb[1].width):
        if k in used:
            continue
        y = (bar.y0 + bar.y1) / 2
        reach = 2.6 * main
        inside = lambda c: bar.x0 - 1 <= (c["box"].x0 + c["box"].x1) / 2 <= bar.x1 + 1
        mid = lambda c: (c["box"].y0 + c["box"].y1) / 2
        num = [c for c in left if inside(c) and mid(c) < y and y - mid(c) < reach]
        den = [c for c in left if inside(c) and mid(c) > y and mid(c) - y < reach]
        if not num or not den:
            continue
        inner = [j for j, b in enumerate(bars) if j != k and b.x0 >= bar.x0 - 1 and b.x1 <= bar.x1 + 1
                 and abs((b.y0 + b.y1) / 2 - y) < reach]
        used.update(inner)
        nb = [bars[j] for j in inner if (bars[j].y0 + bars[j].y1) / 2 < y]
        db = [bars[j] for j in inner if (bars[j].y0 + bars[j].y1) / 2 > y]
        ids = {id(c) for c in num + den}
        left = [c for c in left if id(c) not in ids]
        items.append((bar.x0, ("frac", _linear(num, nb), _linear(den, db))))
    rest: list = []
    if left:
        big = [c for c in left if c["size"] >= 0.9 * main] or left
        base = st.median(c["oy"] for c in big)
        if items:
            # a line of its own under (or over) the one with the fractions
            band = (base - 1.0 * main, base + 0.4 * main)
            rest = [c for c in left if c["box"].y1 < band[0] or c["box"].y0 > band[1]]
            left = [c for c in left if not (c["box"].y1 < band[0] or c["box"].y0 > band[1])]
        for c in left:
            kind = "t"
            if c["size"] < 0.85 * main:
                if c["oy"] > base + 0.12 * main:
                    kind = "sub"
                elif c["oy"] < base - 0.2 * main:
                    kind = "sup"
            items.append((c["box"].x0, (kind, c)))
    items.sort(key=lambda t: t[0])
    parts: list = []
    prev_x1 = None
    for _, it in items:
        if it[0] == "frac":
            parts.append(it)
            prev_x1 = None
            continue
        kind, c = it
        gap = (c["box"].x0 - prev_x1) if prev_x1 is not None else 0.0
        sp = " " if prev_x1 is not None and gap > 0.18 * c["size"] else ""
        if parts and parts[-1][0] == kind:
            parts[-1] = (kind, parts[-1][1] + sp + c["c"])
        else:
            parts.append((kind, (sp if kind == "t" else "") + c["c"]))
        prev_x1 = c["box"].x1
    if rest:
        tail = _linear(rest, [])
        if tail:
            parts = parts + [("t", ", ")] + tail
    return parts


def _plain(parts: list) -> str:
    return "".join(p[1] for p in parts if p[0] == "t" and isinstance(p[1], str)).strip()


def _say(parts: list) -> str:
    out: list[str] = []
    for k, p in enumerate(parts):
        if p[0] == "t":
            seg = p[1]
            if k > 0 and seg.lstrip()[:1] in ("-", "−", "–"):
                # "V_i − µ_i": a minus after a term is a subtraction
                said = speak("0 " + seg.lstrip())
                out.append(re.sub(r"^0\s+", "", said))
            else:
                out.append(speak(seg))
        elif p[0] == "sub":
            out.append("sub " + speak(p[1]))
        elif p[0] == "sup":
            run = p[1].strip()
            out.append(POWER_WORD.get(run, "to the power of " + speak(run)))
        elif p[0] == "frac":
            n, d = p[1], p[2]
            simple = len(n) == 1 and len(d) == 1 and n[0][0] == "t" and d[0][0] == "t"
            words = fraction_words(n[0][1].strip(), d[0][1].strip()) if simple else ""
            nxt = parts[k + 1] if k + 1 < len(parts) else None
            if words:
                out.append(words)
                if nxt is not None and nxt[0] == "t" and re.match(r"\s*[(\[A-Za-z0-9]", nxt[1]):
                    out.append("times")
            elif simple and re.fullmatch(r"[\w.]+", n[0][1].strip()) and re.fullmatch(r"[\w.]+", d[0][1].strip()):
                out.append(f"{_say(n)} over {_say(d)}")
            else:
                out.append(f"the fraction {_say(n)} over {_say(d)},")
    said = " ".join(x for x in out if x).strip()
    said = re.sub(r"\s+", " ", said)
    said = re.sub(r",\s*(close bracket|,)", r" \1", said)
    said = re.sub(r"\s+([,.;:])", r"\1", said)
    return said.rstrip(",").strip()[:600]


def _write(parts: list) -> str:
    out = []
    for p in parts:
        if p[0] == "t":
            out.append(p[1])
        elif p[0] == "sub":
            out.append("_" + p[1] if len(p[1]) == 1 else "_(" + p[1] + ")")
        elif p[0] == "sup":
            out.append("^" + p[1] if len(p[1]) == 1 else "^(" + p[1] + ")")
        elif p[0] == "frac":
            n, d = _write(p[1]), _write(p[2])
            n = n if re.fullmatch(r"\w+", n) else f"({n})"
            d = d if re.fullmatch(r"\w+", d) else f"({d})"
            out.append(f" {n}/{d} ")
    return re.sub(r"\s+", " ", "".join(out)).strip()


# ------------------------------------------------ an equation as a sentence
# The parts above become a small expression tree, which is then said the way
# a person reads the equation aloud: "CCI equals one-fifth times the sum of
# FP, the average of ANC4 and SBA, …" rather than symbol by symbol.
FUNCTIONS = {"ln": "the natural log of", "log": "the log of", "lg": "the log of", "exp": "the exponential of",
             "sin": "the sine of", "cos": "the cosine of", "tan": "the tangent of", "sqrt": "the square root of",
             "√": "the square root of", "max": "the maximum of", "min": "the minimum of"}
_REL = {"=": "equals", "≠": "is not equal to", "≈": "is approximately", "<": "is less than", ">": "is greater than",
        "≤": "is less than or equal to", "≥": "is greater than or equal to", "∝": "is proportional to",
        "≡": "is identical to"}
_ADD = {"+": "plus", "-": "minus", "−": "minus", "–": "minus", "±": "plus or minus", "∓": "minus or plus"}
_MUL = {"×": "times", "·": "times", "⋅": "times", "*": "times", "∗": "times"}
_DIV = {"/": "divided by", "÷": "divided by"}
_NAMES = {"µ": "mu"}                 # the micro sign set for the Greek mu
_TOKEN_RE = re.compile(r"\s*(?:(?P<num>\d+(?:\.\d+)?)|(?P<id>[^\W\d_][\w']*)|(?P<op>[^\s\w]))")


class _Parse:
    def __init__(self, toks: list):
        self.t = toks
        self.i = 0

    def peek(self, k: int = 0):
        return self.t[self.i + k] if self.i + k < len(self.t) else None

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def is_op(self, tok, ops) -> bool:
        return tok is not None and tok[0] == "op" and tok[1] in ops

    def relation(self):
        items = [self.sum()]
        rels = []
        while self.is_op(self.peek(), _REL):
            rels.append(self.take()[1])
            items.append(self.sum())
        return items[0] if not rels else ("rel", items, rels)

    def sum(self):
        if self.is_op(self.peek(), ("-", "−", "–")):
            self.take()
            first = ("neg", self.term())
        else:
            first = self.term()
        items, ops = [first], []
        while self.is_op(self.peek(), _ADD):
            ops.append(self.take()[1])
            items.append(self.term())
        return first if not ops else ("sum", items, ops)

    def starts_primary(self, tok) -> bool:
        return tok is not None and (tok[0] in ("num", "id", "frac") or self.is_op(tok, "([{√"))

    def term(self):
        items, ops = [self.factor()], []
        while True:
            tok = self.peek()
            if self.is_op(tok, _MUL) or self.is_op(tok, _DIV):
                ops.append(self.take()[1])
                items.append(self.factor())
            elif self.starts_primary(tok):
                ops.append("")                      # side by side: implied product
                items.append(self.factor())
            else:
                break
        return items[0] if not ops else ("prod", items, ops)

    def factor(self):
        node = self.primary()
        while self.peek() is not None and self.peek()[0] in ("sub", "sup"):
            kind, val = self.take()
            node = (kind, node, val)
        return node

    def primary(self):
        tok = self.take()
        if tok is None:
            raise ValueError("end")
        if tok[0] == "num":
            return ("num", tok[1])
        if tok[0] == "frac":
            return tok
        if tok[0] == "id" or self.is_op(tok, "√"):
            name = tok[1]
            if name.lower() in FUNCTIONS and self.starts_primary(self.peek()):
                return ("func", name.lower(), self.factor())
            return ("id", name)
        if self.is_op(tok, "([{"):
            close = {"(": ")", "[": "]", "{": "}"}[tok[1]]
            inner = self.relation()
            end = self.take()
            if not self.is_op(end, close):
                raise ValueError("bracket")
            return ("group", inner)
        raise ValueError(f"unexpected {tok}")


def _tokens(parts: list) -> list:
    out: list = []
    for p in parts:
        if p[0] == "t":
            pos = 0
            s = p[1]
            while pos < len(s):
                mt = _TOKEN_RE.match(s, pos)
                if not mt or mt.end() == pos:
                    if s[pos:].strip():
                        raise ValueError("token")
                    break
                pos = mt.end()
                if mt.group("num"):
                    out.append(("num", mt.group("num")))
                elif mt.group("id"):
                    out.append(("id", mt.group("id")))
                elif mt.group("op"):
                    if mt.group("op") == ",":
                        raise ValueError("comma")
                    out.append(("op", mt.group("op")))
        elif p[0] in ("sub", "sup"):
            inner = _Parse(_tokens([("t", p[1])]))
            node = inner.relation()
            if inner.peek() is not None:
                raise ValueError("script")
            out.append((p[0], node))
        elif p[0] == "frac":
            n, d = _Parse(_tokens(p[1])), _Parse(_tokens(p[2]))
            nn, dd = n.relation(), d.relation()
            if n.peek() is not None or d.peek() is not None:
                raise ValueError("fraction")
            out.append(("frac", nn, dd))
    return out


def _atomic(x) -> bool:
    return x[0] in ("num", "id") or (x[0] in ("sub", "sup") and _atomic(x[1]))


def _coefficient(x):
    """2 × DPT3 -> (2, DPT3); DPT3 -> (1, DPT3)."""
    if x[0] == "prod" and len(x[1]) == 2 and x[1][0][0] == "num" and x[2][0] in ("", "×", "·", "⋅", "*", "∗") \
            and re.fullmatch(r"\d+", x[1][0][1]):
        return int(x[1][0][1]), x[1][1]
    return 1, x


def _list_words(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + ", and " + items[-1]


def _tree_say(x) -> str:
    kind = x[0]
    if kind == "num":
        return x[1]
    if kind == "id":
        return _NAMES.get(x[1], OPERATORS.get(x[1], x[1])) if len(x[1]) == 1 else x[1]
    if kind == "sub":
        base, idx = x[1], x[2]
        if base[0] == "group" and _atomic(base[1]):
            base = base[1]                     # (output)_i: output sub i
        return f"{_tree_say(base)} sub {_tree_say(idx)}"
    if kind == "sup":
        p = _tree_say(x[2])
        word = POWER_WORD.get(p)
        return f"{_tree_say(x[1])} {word}" if word else f"{_tree_say(x[1])} to the power of {p}"
    if kind == "func":
        arg = x[2]
        if arg[0] == "group" and (_atomic(arg[1]) or arg[1][0] == "func"):
            arg = arg[1]
        return f"{FUNCTIONS[x[1]]} {_tree_say(arg)}"
    if kind == "group":
        inner = x[1]
        return f"open bracket {_tree_say(inner)} close bracket"
    if kind == "neg":
        return f"negative {_tree_say(x[1])}"
    if kind == "frac":
        n, d = x[1], x[2]
        if n[0] == "num" and d[0] == "num":
            w = fraction_words(n[1], d[1])
            if w:
                return w
        if n[0] == "sum" and d[0] == "num" and all(o == "+" for o in n[2]) and re.fullmatch(r"\d+", d[1]):
            coef = [_coefficient(t) for t in n[1]]
            if sum(c for c, _ in coef) == int(d[1]):
                if all(c == 1 for c, _ in coef):
                    return "the average of " + _list_words([_tree_say(t) for _, t in coef])
                said = [(_ONES[c] + " " if 1 < c < len(_ONES) else (f"{c} " if c > 1 else "")) + _tree_say(t)
                        for c, t in coef]
                return "the average of " + " plus ".join(said)
        if _atomic(n) and _atomic(d):
            return f"{_tree_say(n)} over {_tree_say(d)}"
        return f"the fraction {_tree_say(n)}, over {_tree_say(d)},"
    if kind == "sum":
        out = _tree_say(x[1][0])
        for op, t in zip(x[2], x[1][1:]):
            out += f" {_ADD[op]} {_tree_say(t)}"
        return out
    if kind == "rel":
        out = _tree_say(x[1][0])
        for op, t in zip(x[2], x[1][1:]):
            out += f" {_REL[op]} {_tree_say(t)}"
        return out
    if kind == "prod":
        items, ops = x[1], x[2]
        out = _tree_say(items[0])
        for k, (op, t) in enumerate(zip(ops, items[1:]), 1):
            prev = items[k - 1]
            if t[0] == "group" and t[1][0] == "sum" and all(o == "+" for o in t[1][2]):
                # a factor times (a + b + c): "times the sum of a, b, and c"
                said = "the sum of " + _list_words([_tree_say(s) for s in t[1][1]])
                out += f" times {said}"
                continue
            if op in _DIV:
                out += f" divided by {_tree_say(t)}"
                continue
            if prev[0] == "num" and re.fullmatch(r"\d+", prev[1]) and k == 1 and t[0] in ("id", "sub", "sup") \
                    and 1 < int(prev[1]) < len(_ONES):
                out = f"{_ONES[int(prev[1])]} {_tree_say(t)}"     # 2 × DPT3: two DPT3
                continue
            if op == "" and prev[0] in ("num", "id", "sub", "sup") and t[0] in ("id", "sub", "sup") \
                    and prev[0] != "num":
                out += f" {_tree_say(t)}"                        # side by side letters
                continue
            out += f" times {_tree_say(t)}"
        return out
    return ""


def say_tree(parts: list) -> str:
    """The parts of a set equation said as a sentence, or "" when they do not
    read as one expression (the symbol-by-symbol reading is used then)."""
    try:
        toks = _tokens(parts)
        if not toks:
            return ""
        ps = _Parse(toks)
        tree = ps.relation()
        if ps.peek() is not None:
            return ""
    except (ValueError, KeyError, IndexError):
        return ""
    said = _tree_say(tree)
    said = re.sub(r"\s+", " ", said).strip()
    said = re.sub(r",\s*([,.])", r"\1", said)
    said = re.sub(r",\s*(close bracket|plus|minus|equals|times)\b", r", \1", said)
    said = said.rstrip(", ")
    return (said[0].upper() + said[1:] + ".") if said and said[0].islower() and said.startswith("the ") \
        else (said + "." if said else "")
