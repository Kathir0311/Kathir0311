"""Which generation of the engine a job runs with.

The tool's own tagging (V4 — the Academain client's structure, and every
profile without a client house style) stays exactly as it was. What came with
the client house styles (V4.10 on: Kriyadocs) — how lines are pieced together,
narrow gutters, journal paragraph breaks, margin stamps, note links in table
cells, the inner tag winning its glyphs … — runs only in a job whose options
name a house style.

The switch is per job: set where a job's options come in (the analysis, the
build, an editor request), read wherever the engine decides. A context
variable keeps two jobs in two request threads apart; a new thread starts on
the V4 engine.
"""
from __future__ import annotations

import contextvars

_HOUSE: contextvars.ContextVar[str] = contextvars.ContextVar("pdfua_house_style", default="")


def style_of(opts_or_style) -> str:
    s = opts_or_style if isinstance(opts_or_style, str) else getattr(opts_or_style, "house_style", "")
    s = (s or "").strip().lower()
    return "" if s in ("none", "off") else s


def use(opts_or_style) -> str:
    """Run what follows (in this thread) with the engine the options ask for."""
    s = style_of(opts_or_style)
    _HOUSE.set(s)
    return s


def house() -> bool:
    """True in a job with a client house style (Kriyadocs …)."""
    return bool(_HOUSE.get())
