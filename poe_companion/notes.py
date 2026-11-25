"""Bounded, non-markup parsing of the colour escapes used in PoB build notes."""
from __future__ import annotations

import re

MAX_COLOR_TAGS = 256
_ESCAPE = re.compile(r"\^x([0-9a-fA-F]{6})|\^([0-9])")
POB_PALETTE = {
    "0": "#000000", "1": "#ff0000", "2": "#00ff00", "3": "#0000ff", "4": "#ffff00",
    "5": "#ff00ff", "6": "#00ffff", "7": "default", "8": "#b3b3b3", "9": "#666666",
}


def _iter_tokens(value: str):
    """Yield literal text and recognized color changes without markup interpretation."""
    pos = 0
    for match in _ESCAPE.finditer(value):
        if match.start() > pos:
            yield "text", value[pos:match.start()]
        color = ("#" + match.group(1).lower()) if match.group(1) else POB_PALETTE[match.group(2)]
        yield "color", color
        pos = match.end()
    if pos < len(value):
        yield "text", value[pos:]


def format_note_spans(value: str) -> list[tuple[str, str]]:
    """Return display spans, dropping leading lines that contain color codes only."""
    spans: list[tuple[str, str]] = []
    color = "default"
    line_has_code = False
    line_has_text = False
    seen_prose = False
    for kind, content in _iter_tokens(value):
        if kind == "color":
            color = content
            line_has_code = True
            continue
        start = 0
        for index, char in enumerate(content):
            if char != "\n":
                continue
            if index > start:
                part = content[start:index]
                if part.strip():
                    seen_prose = True
                    line_has_text = True
                _append_span(spans, part, color)
            if seen_prose or line_has_text or not line_has_code:
                _append_span(spans, "\n", color)
            # A formatting-only first line is a color directive, not a blank heading.
            line_has_code = False
            line_has_text = False
            start = index + 1
        if start < len(content):
            part = content[start:]
            if part.strip():
                seen_prose = True
                line_has_text = True
            _append_span(spans, part, color)
    return spans


def _append_span(spans: list[tuple[str, str]], text: str, color: str):
    if not text:
        return
    if spans and spans[-1][1] == color:
        spans[-1] = (spans[-1][0] + text, color)
    else:
        spans.append((text, color))


def strip_color_escapes(value: str) -> str:
    """Remove only the color escapes recognized by PoB; retain all other text."""
    return _ESCAPE.sub("", value)


def plain_note_summary(value: str, limit: int = 180) -> str:
    """Produce a compact HUD-safe note with no formatting tokens or markup."""
    return re.sub(r"\s+", " ", strip_color_escapes(value)).strip()[:limit]
