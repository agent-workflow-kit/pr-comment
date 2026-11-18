"""Display sanitizing: forgectl's Sanitizing rules for untrusted text shown on a terminal.

Every untrusted string pr-comment prints for a human (dialog, ``--dry-run`` output, messages,
hook stderr, forgectl messages) goes through this module. It changes only what is displayed:
posted text and ``--json`` output are never passed through it.

Rules (forgectl interface, Sanitizing):

* Characters of Unicode categories Cc (except tab and LF), Cf, Cs, Co, Cn, Zl and Zp become a
  visible marker ``<U+XXXX>``.
* So do all variation selectors, U+034F, U+17B4, U+17B5, U+180B to U+180F, U+115F, U+1160,
  U+3164, U+FFA0 and U+2800.
* One-line fields also mark tab and LF.
* In text fields CRLF becomes LF; a lone CR becomes ``<U+000D>``.
* Bytes that are not valid UTF-8 become U+FFFD.

The Cn test depends on the Unicode database of the running Python, so a code point assigned in
a newer Unicode version can differ between Python versions; this only affects Cn.
"""

from __future__ import annotations

import unicodedata

_MARKED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})

# Not in a marked category but listed by forgectl. Variation selectors (U+FE00-FE0F and
# U+E0100-E01EF) are range-checked in is_marked; U+180B-180F is in the list for completeness.
_LISTED_INVISIBLE = frozenset(
    {0x034F, 0x17B4, 0x17B5, 0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800} | set(range(0x180B, 0x1810))
)

# Allowed to pass in multi-line text; marked in one-line fields.
_TEXT_ONLY_PASS = frozenset({"\t", "\n"})


def marker(ch: str) -> str:
    """The visible marker for one character, for example ``<U+202E>``."""
    return f"<U+{ord(ch):04X}>"


def is_marked(ch: str) -> bool:
    """Would forgectl's Sanitizing mark this character in a multi-line text field.

    Tab and LF are not marked here (one-line fields mark them on top of this predicate).
    This is the shared predicate for counts, suggestion degrading and path markers.
    """
    if ch in _TEXT_ONLY_PASS:
        return False
    code = ord(ch)
    if code in _LISTED_INVISIBLE or 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF:
        return True
    return unicodedata.category(ch) in _MARKED_CATEGORIES


def count_marked(text: str) -> int:
    """Number of characters in ``text`` that Sanitizing would mark (``counts.invisible``)."""
    return sum(1 for ch in text if is_marked(ch))


def _mark(text: str, *, one_line: bool) -> str:
    out: list[str] = []
    for ch in text:
        if is_marked(ch) or (one_line and ch in _TEXT_ONLY_PASS):
            out.append(marker(ch))
        else:
            out.append(ch)
    return "".join(out)


def sanitize_text(text: str) -> str:
    """Sanitize a multi-line text field: CRLF becomes LF, tab and LF stay, the rest as listed."""
    return _mark(text.replace("\r\n", "\n"), one_line=False)


def sanitize_line(text: str) -> str:
    """Sanitize a one-line field: like :func:`sanitize_text`, and tab, LF and CR are marked."""
    return _mark(text, one_line=True)


def sanitize_bytes(data: bytes, *, one_line: bool = False) -> str:
    """Decode bytes (invalid UTF-8 becomes U+FFFD), then sanitize."""
    text = data.decode("utf-8", errors="replace")
    return sanitize_line(text) if one_line else sanitize_text(text)
