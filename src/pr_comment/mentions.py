"""Mention neutralizing (docs/interface.md: Mentions), text pipeline step 3.

Every `@` not directly after an ASCII letter or digit, and every entity form of `@`, becomes
the fullwidth `＠` (U+FF20) in every untrusted part and in the attribution line, code included.
The pass reads no Markdown, so no host parser difference can leave a mention live. A literal `@`
whose name is on the allowlist is kept.

``MentionNeutralizer`` is one run's state: call ``neutralize`` on each part in posting order (the
review body first, then inline comments in draft order), then read ``names`` and ``count``.

Callers not written yet: the renderer applies it to each untrusted part and to the attribution
line (TODO(#18), TODO(#20)); the posting plan reports ``names`` as ``mentions_neutralized`` and
``count`` as ``counts.mentions_neutralized`` (TODO(#27)); the review order builds it from
``LoadedConfig.for_repo`` plus ``--allow-mention`` (TODO(#29)).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final, Literal

REPLACEMENT: Final = "＠"

# A literal `@`, or an entity form of `@` with its `;` when present: `&#` with optional zeros and
# 64 not followed by a digit, `&#x` or `&#X` with optional zeros and 40 not followed by a hex
# digit, and `&commat`. Each alternative starts with a distinct character and the zero runs have
# one way to match, so matching is linear.
_FORM_RE: Final = re.compile(r"@|&#0*64(?![0-9]);?|&#[xX]0*40(?![0-9A-Fa-f]);?|&commat;?")

# The name after an `@` (the allow rule's N, before the trailing dots are removed).
_NAME_RE: Final = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*")

_ASCII_LOWER: Final = {ord(c): ord(c) + 32 for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}
_ASCII_ALNUM: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
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
        self._allow = frozenset(entry.translate(_ASCII_LOWER) for entry in allow)
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
            if literal and start > pos and text[start - 1] in _ASCII_ALNUM:
                continue  # e-mail-like: a letter or digit directly before the `@`
            name, follow_ok = self._name_after(text, end)
            if literal and follow_ok and name and name.translate(_ASCII_LOWER) in self._allow:
                continue
            out.append(text[pos:start])
            out.append(REPLACEMENT)
            pos = end
            self._count += 1
            if name:
                self._names.setdefault(name.translate(_ASCII_LOWER), None)
        if pos == 0:
            return text
        out.append(text[pos:])
        return "".join(out)

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
