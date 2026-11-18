"""Text pipeline step 3, character part: ``<`` and ``![`` neutralizing, and the body counts.

docs/interface.md (Text pipeline step 3, Body structure): in every untrusted part, except the
lines of an applicable suggestion, every ``<`` becomes ``＜`` (U+FF1C) and every ``!`` immediately
followed by ``[`` becomes ``！`` (U+FF01). With ``<`` gone, untrusted text opens no raw HTML, HTML
comment, ``<details>``, ``<img>`` or angle autolink in any engine, and every Markdown image form
starts with a literal ``![``. Character references such as ``&lt;`` are left as they are.

The counts (``raw_html``, ``images``, ``links``, ``invisible``) are taken from the untrusted text
as given, before this pass, so a reader sees what the input tried. They include code and are
upper bounds (a URL with ``www.`` counts twice).

No Markdown is parsed here. Mentions (step 3, #16), the markers of degraded suggestions and the
rendering of parts (steps 4 to 6) are other modules' work.

todo(#18): render.py calls ``neutralize_characters`` on each untrusted part (title before its
backslash escaping, path before its code span, body, body file, degraded or moved suggestion
lines through ``neutralize_suggestion_lines``) and ``count_parts`` on every untrusted part,
suggestion lines included, before any of that.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from pr_comment.display import count_marked

_FULLWIDTH_LT: Final = "\uff1c"
_FULLWIDTH_BANG: Final = "\uff01"

# Each < followed by an ASCII letter, /, ! or ?.
_RAW_HTML_RE: Final = re.compile(r"<[A-Za-z/!?]")

# ASCII punctuation, the characters a backslash can escape in Markdown.
_ESCAPE_RE: Final = re.compile(r"\\([!-/:-@\[-`{-~])")

# `//` followed by a character other than whitespace and `/` (the lookahead keeps `///x` at one).
_SCHEME_RE: Final = re.compile(r"//(?=[^\s/])")
# `www.` at a word start: not after an ASCII letter or digit. Case-insensitive (an upper bound).
_WWW_RE: Final = re.compile(r"(?<![A-Za-z0-9])www\.", re.IGNORECASE)
_MAILTO_RE: Final = re.compile(r"mailto:", re.IGNORECASE)
# E-mail form, found at the character before each `@` so overlapping forms are counted too.
_EMAIL_RE: Final = re.compile(r"(?=[A-Za-z0-9._%+-]@[A-Za-z0-9-]+\.[A-Za-z])")


@dataclass(frozen=True)
class TextCounts:
    """The body counts of the plan's ``counts`` (names as in the schema)."""

    raw_html: int
    images: int
    links: int
    invisible: int

    def __add__(self, other: TextCounts) -> TextCounts:
        return TextCounts(
            self.raw_html + other.raw_html,
            self.images + other.images,
            self.links + other.links,
            self.invisible + other.invisible,
        )


def _require_str(text: object) -> None:
    if not isinstance(text, str):
        raise TypeError(f"expected str, got {type(text).__name__}")


def neutralize_characters(text: str) -> str:
    """``text`` with every ``<`` as ``＜`` and every ``!`` before ``[`` as ``！``.

    For an untrusted part, never for the lines of an applicable suggestion. Idempotent.
    """
    _require_str(text)
    return text.replace("<", _FULLWIDTH_LT).replace("![", _FULLWIDTH_BANG + "[")


def neutralize_suggestion_lines(lines: Iterable[str], *, applicable: bool) -> list[str]:
    """The suggestion lines as they are rendered: the lines of an applicable suggestion are raw
    replacement code and stay as they are; lines of a degraded or moved suggestion are untrusted
    text shown as code, so they are neutralized (before their ``<U+XXXX>`` markers are inserted).
    """
    if applicable:
        return list(lines)
    return [neutralize_characters(line) for line in lines]


def count_text(text: str) -> TextCounts:
    """The counts of one untrusted text, taken before neutralizing (pipeline-clean text)."""
    _require_str(text)
    decoded = _ESCAPE_RE.sub(r"\1", html.unescape(text))
    links = (
        len(_SCHEME_RE.findall(decoded))
        + len(_WWW_RE.findall(decoded))
        + len(_MAILTO_RE.findall(decoded))
        + len(_EMAIL_RE.findall(decoded))
    )
    return TextCounts(
        raw_html=len(_RAW_HTML_RE.findall(text)),
        images=text.count("!["),
        links=links,
        invisible=count_marked(text),
    )


def count_parts(parts: Iterable[str]) -> TextCounts:
    """The sum of ``count_text`` over the untrusted parts."""
    total = TextCounts(0, 0, 0, 0)
    for part in parts:
        total = total + count_text(part)
    return total
