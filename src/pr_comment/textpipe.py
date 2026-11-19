"""Text pipeline steps 1 and 2 on one untrusted text (docs/interface.md: Text pipeline).

Step 1 splits the text on every line separator forgectl knows and rejoins with LF
(``normalize_lines``), then refuses a character of category Cc other than tab and LF, or of
category Cs (a lone surrogate from a JSON ``\\uD800`` escape). Step 2 refuses a suggestion fence
anywhere in a line.

``text_problem`` answers with a fixed kind and never returns or raises any part of the text.
Neutralizing, counts and rendering (steps 3 to 6) are not here.
"""

from __future__ import annotations

import html
import re
from typing import Final

from pr_comment.display import is_marked

CONTROL_CHARACTER: Final = "control_character"
SUGGESTION_FENCE: Final = "suggestion_fence"

# LF, CRLF, lone CR, VT, FF, FS, GS, RS, NEL, LS and PS (forgectl, section Neutralizing, step 1).
_SEPARATOR_RE: Final = re.compile("\r\n|[\n\r\x0b\x0c\x1c\x1d\x1e\x85  ]")

# After the split VT, FF, FS, GS, RS, NEL and CR are gone, so every remaining Cc other than tab
# and LF is in this class: C0 without tab (0x09) and LF (0x0a), DEL and C1. Cs is the surrogates.
_FORBIDDEN_RE: Final = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]")

# A run of three or more backticks, or of three or more tildes, and the text after it up to the
# next fence character or the end of the line. Matching segment by segment is linear in the text.
_FENCE_RE: Final = re.compile(r"(?:`{3,}|~{3,})([^`~\n]*)")

_WORD: Final = "suggestion"
_ASCII_LOWER: Final = {ord(c): ord(c) + 32 for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}


def normalize_lines(text: str) -> str:
    """``text`` with every line separator replaced by one LF (a CRLF is one break)."""
    return _SEPARATOR_RE.sub("\n", text)


def is_blank_text(text: str) -> bool:
    """Whether nothing visible is left in ``text`` after step 1 (the one blank-body rule).

    Blank means every character is whitespace (``str.isspace``) or one forgectl's Sanitizing marks
    (``display.is_marked``: Cf, Co, Cn, Zl, Zp, variation selectors, U+034F, U+17B4, U+17B5,
    U+180B to U+180F, U+115F, U+1160, U+3164, U+FFA0 and U+2800), so such a text shows as empty.
    The empty text is blank. A character step 1 refuses (Cc other than tab and LF, Cs) is not
    blank: the text is refused for that, with its own kind, rather than as an empty body.
    """
    normalized = normalize_lines(text)
    if has_forbidden_character(normalized):
        return False
    return all(ch.isspace() or is_marked(ch) for ch in normalized)


def _is_suggestion_info(segment: str) -> bool:
    decoded = html.unescape(segment).replace("\\", "").lstrip()
    return decoded[: len(_WORD)].translate(_ASCII_LOWER) == _WORD


def text_problem(text: str) -> str | None:
    """The kind of the first problem of ``text`` (step 1 before step 2), or None when fine.

    The kind is ``CONTROL_CHARACTER`` or ``SUGGESTION_FENCE``.

    todo(#29): body-file check (pointer /body) calls text_problem
    """
    normalized = normalize_lines(text)
    if has_forbidden_character(normalized):
        return CONTROL_CHARACTER
    if suggestion_fence(normalized):
        return SUGGESTION_FENCE
    return None


def has_forbidden_character(normalized: str) -> bool:
    """Whether ``normalized`` (split done) holds a Cc other than tab and LF, or a Cs."""
    return _FORBIDDEN_RE.search(normalized) is not None


def suggestion_fence(normalized: str) -> bool:
    """Whether a line of ``normalized`` (split done) holds a suggestion fence, wherever it is."""
    return any(_is_suggestion_info(match.group(1)) for match in _FENCE_RE.finditer(normalized))
