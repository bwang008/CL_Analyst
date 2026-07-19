"""ASCII-safe output helpers.

Windows consoles (cp1252/cp437) and some log viewers render our em-dashes,
arrows, and any stray emoji as mojibake (``â€"`` or the U+FFFD replacement
box). We keep the nicer Unicode in source and docstrings but transliterate to
plain 7-bit ASCII at the OUTPUT boundary — the log formatters and the Telegram
sender — so what the operator actually reads is always clean text.

Non-mutating by design: ``AsciiFormatter`` sanitizes the FINISHED log line,
never the ``LogRecord`` itself, so other handlers and pytest's ``caplog`` still
see the original message.

GUIDANCE FOR AUTHORS
--------------------
Write log messages and Telegram/operator text in **plain ASCII**. Do NOT add
emoji, decorative box-drawing, or "nice" typographic punctuation (em dashes,
arrows, curly quotes) to anything that gets logged or sent, and keep the text
lean — no ornamental filler. This is not just cosmetic: non-ASCII in operator
output has caused real crashes/exceptions in the past when a downstream sink
could not render or encode the character (e.g. a cp1252 console raising
``UnicodeEncodeError``, or a viewer turning ``—`` into the ``�`` box). The
transliteration in this module is a safety NET at the output boundary, not a
license to sprinkle Unicode into new strings — treat it as defense-in-depth,
and prefer ASCII (``-``, ``->``, ``x``, ``+/-``) at the source.
"""
from __future__ import annotations

import logging
import unicodedata

# Punctuation / symbols we spell out rather than drop. Anything not listed
# here that is non-ASCII gets NFKD-folded to an ASCII base if one exists,
# else dropped (this is how emoji and variation selectors vanish).
_TRANSLITERATE = {
    "—": "-",      # em dash
    "–": "-",      # en dash
    "−": "-",      # minus sign
    "→": "->",     # rightwards arrow
    "←": "<-",     # leftwards arrow
    "↔": "<->",    # left-right arrow
    "⇒": "=>",     # rightwards double arrow
    "⇔": "<=>",    # left-right double arrow
    "─": "-",      # box drawings light horizontal
    "│": "|",      # box drawings light vertical
    "├": "|-",     # box drawings light vertical-and-right
    "└": "`-",     # box drawings light up-and-right
    "×": "x",      # multiplication sign
    "±": "+/-",    # plus-minus sign
    "≈": "~",      # almost equal to
    "≥": ">=",     # greater-than or equal to
    "≤": "<=",     # less-than or equal to
    "§": "S",      # section sign
    "…": "...",    # horizontal ellipsis
    " ": " ",      # non-breaking space
    "‘": "'",      # left single quotation mark
    "’": "'",      # right single quotation mark
    "“": '"',      # left double quotation mark
    "”": '"',      # right double quotation mark
    "•": "*",      # bullet
}


def to_ascii(text: str) -> str:
    """Transliterate ``text`` to plain ASCII.

    Known punctuation is spelled out (em dash -> ``-``, arrow -> ``->``);
    other non-ASCII is NFKD-folded to its ASCII base where one exists, and
    anything left over (emoji, variation selectors) is dropped. Pure-ASCII
    input is returned unchanged (fast path).
    """
    if text.isascii():
        return text
    out = []
    for ch in text:
        if ord(ch) < 128:
            out.append(ch)
        elif ch in _TRANSLITERATE:
            out.append(_TRANSLITERATE[ch])
        else:
            out.append(
                unicodedata.normalize("NFKD", ch)
                .encode("ascii", "ignore")
                .decode("ascii")
            )
    return "".join(out)


class AsciiFormatter(logging.Formatter):
    """``logging.Formatter`` that emits ASCII-only lines.

    Sanitizes the finished line rather than the ``LogRecord``, so it never
    mutates shared record state (other handlers and ``caplog`` see the
    original message).
    """

    def format(self, record: logging.LogRecord) -> str:
        return to_ascii(super().format(record))
