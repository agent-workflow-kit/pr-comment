"""Mention neutralizing (docs/interface.md: Mentions), text pipeline step 3.

Every `@` not directly after an ASCII letter or digit, and every entity form of `@`, becomes
the fullwidth `＠` (U+FF20) in every untrusted part and in the attribution line, code included.
The pass reads no Markdown, so no host parser difference can leave a mention live. A literal `@`
whose name is on the allowlist is kept.

``MentionNeutralizer`` is one run's state: call ``neutralize`` on each part in posting order (the
review body first, then inline comments in draft order), then read ``names`` and ``count``.

Callers: render.py applies it to each untrusted part of finding text and sections (#18); the
attribution line is ``assemble.render_attribution`` (#20). ``plan.py`` builds one per plan from
``LoadedConfig.for_repo`` plus ``--allow-mention`` and reports ``names`` as
``mentions_neutralized`` and ``count`` as ``counts.mentions_neutralized``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final, Literal

from pr_comment.grammars import MENTION_NAME_RE

REPLACEMENT: Final = "＠"

# A literal `@`, or an entity form of `@` with its `;` when present: `&#` with optional zeros and
# 64 not followed by a digit, `&#x` or `&#X` with optional zeros and 40 not followed by a hex
# digit, and `&commat`. Each alternative starts with a distinct character and the zero runs have
# one way to match, so matching is linear.
_FORM_RE: Final = re.compile(r"@|&#0*64(?![0-9]);?|&#[xX]0*40(?![0-9A-Fa-f]);?|&commat;?")

# The name after an `@` (the allow rule's N, before the trailing dots are removed): the grammar
# of an allowlist entry, defined once in grammars.py.
_NAME_RE: Final = MENTION_NAME_RE

# Reporting caps (docs/interface.md: Mentions, Reporting): a silent cut in the report only.
_MAX_NAME_LENGTH: Final = 128
_MAX_NAMES: Final = 100

_ASCII_LOWER: Final = {ord(c): ord(c) + 32 for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}
_ASCII_LETTERS: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_ASCII_ALNUM: Final = _ASCII_LETTERS | frozenset("0123456789")
_DIGITS: Final = frozenset("0123456789")
_HEX: Final = _DIGITS | frozenset("abcdefABCDEF")
_WHITESPACE: Final = frozenset(" \t\n")
_FOLLOW: Final = _WHITESPACE | frozenset(",;:!?)]}'\"*~")

Policy = Literal["neutralize", "allow"]


class MentionNeutralizer:
    """Replaces mentions in one text after another and keeps the run's report.

    ``policy`` is the config ``mentions.policy``: ``allow`` turns the pass off. ``allow`` holds the
    effective allowlist (config ``mentions.allow`` plus ``--allow-mention``); entries are compared
    in ASCII lowercase.
    """

    def __init__(self, policy: Policy = "neutralize", allow: Iterable[str] = ()) -> None:
        if policy not in ("neutralize", "allow"):
            raise ValueError("invalid mentions policy (expected neutralize or allow)")
        self._active = policy == "neutralize"
        entries = tuple(allow)
        for entry in entries:
            if not _NAME_RE.fullmatch(entry):
                raise ValueError("invalid mentions allow entry (expected a name or org/team)")
        self._allow = frozenset(entry.translate(_ASCII_LOWER) for entry in entries)
        self._names: dict[str, None] = {}
        self._count = 0

    @property
    def names(self) -> tuple[str, ...]:
        """The names after the replaced forms: ASCII lowercase, first appearance, no repeats and
        no empty name (``mentions_neutralized``)."""
        return tuple(self._names)

    @property
    def count(self) -> int:
        """The replaced forms so far (``counts.mentions_neutralized``)."""
        return self._count

    def neutralize(self, text: str) -> str:
        """``text`` with every mention form replaced, the allowed names kept.

        A part boundary counts as a non-letter before the first character.
        """
        if not self._active or ("@" not in text and "&" not in text):
            return text
        out: list[str] = []
        pos = 0
        for match in _FORM_RE.finditer(text):
            start, end = match.span()
            literal = text[start] == "@"
            name, follow_ok = self._name_after(text, end)
            if literal and follow_ok and name and name.translate(_ASCII_LOWER) in self._allow:
                continue
            if (
                literal
                and start > pos
                and text[start - 1] in _ASCII_ALNUM
                and not _ends_entity_tail(text, start)
            ):
                continue  # e-mail-like: a letter or digit directly before the `@`
            out.append(text[pos:start])
            out.append(REPLACEMENT)
            pos = end
            self._count += 1
            if name:
                self._report(name)
        if pos == 0:
            return text
        out.append(text[pos:])
        return "".join(out)

    def _report(self, name: str) -> None:
        """Adds ``name`` to the report: cut to 128 characters before the repeat check, and
        dropped once 100 names are held. ``count`` is not affected."""
        key = name.translate(_ASCII_LOWER)[:_MAX_NAME_LENGTH]
        if key not in self._names and len(self._names) < _MAX_NAMES:
            self._names[key] = None

    @staticmethod
    def _name_after(text: str, at: int) -> tuple[str, bool]:
        """The name N that starts at ``at`` (trailing dots removed) and whether what follows it is
        an allowed follow: the end, whitespace or one of ``, ; : ! ? ) ] } ' " * ~``, or one or
        more dots and then whitespace or the end."""
        match = _NAME_RE.match(text, at)
        if match is None:
            return "", False
        end = match.end()
        name = match.group().rstrip(".")
        if end >= len(text):
            return name, True
        if len(name) == end - at:  # no trailing dot was removed
            return name, text[end] in _FOLLOW
        return name, text[end] in _WHITESPACE


def _ends_entity_tail(text: str, end: int) -> bool:
    """Whether ``text[:end]`` ends with an unterminated entity-like tail:
    ``&(?:#[0-9]+|#[xX][0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*)``. A downstream HTML parser decodes such
    a tail (without its ``;``) to a character that is not a letter or digit. The scan goes back over
    one run of letters and digits only, so a text of many ``@`` is scanned in linear time."""
    k = end
    while k > 0 and text[k - 1] in _ASCII_ALNUM:
        k -= 1
    run = text[k:end]
    if k == 0 or not run:
        return False
    before = text[k - 1]
    if before == "&":
        return run[0] in _ASCII_LETTERS
    if before == "#" and k >= 2 and text[k - 2] == "&":
        if all(c in _DIGITS for c in run):
            return True
        return run[0] in "xX" and len(run) > 1 and all(c in _HEX for c in run[1:])
    return False
