"""Text-extraction flags: what is printed, not what a tag says it reads as.

A file tagged before (by this tool or another) carries /ActualText on its
write-in lines ("Blank Line"), its ticks ("Underscore") and its spelled words.
MuPDF puts that text in place of the glyphs unless told not to — "Name: ____"
came back as "Name:Blank Line", with the boxes of the underscores shared out
over the letters of "Blank Line" — so every place the tool reads the page uses
these flags.
"""
import pymupdf

IGNORE_ACTUALTEXT = getattr(pymupdf, "TEXT_IGNORE_ACTUALTEXT", 0)
_BASE = pymupdf.TEXT_PRESERVE_LIGATURES | pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP
RAW_FLAGS = _BASE | IGNORE_ACTUALTEXT
WORD_FLAGS = getattr(pymupdf, "TEXTFLAGS_WORDS", _BASE) | IGNORE_ACTUALTEXT
DICT_FLAGS = getattr(pymupdf, "TEXTFLAGS_DICT", _BASE) | IGNORE_ACTUALTEXT
TEXT_FLAGS = getattr(pymupdf, "TEXTFLAGS_TEXT", _BASE) | IGNORE_ACTUALTEXT


def printed_text(page: "pymupdf.Page", rect) -> str:
    """The glyphs printed inside `rect` (MuPDF space), never their /ActualText."""
    try:
        return page.get_textpage(flags=TEXT_FLAGS).extractTextbox(pymupdf.Rect(rect)) or ""
    except Exception:
        return page.get_textbox(pymupdf.Rect(rect)) or ""
