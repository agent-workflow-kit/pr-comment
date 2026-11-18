"""Finding text, paths, review body sections and moved findings (text pipeline steps 3 and 4).

docs/interface.md: Finding text, Review body assembly, Moved findings. Every untrusted part (a
title, a displayed path, a body, the draft body) goes through the same steps, once and in this
order: line separators to LF (step 1), mentions, then ``<`` and ``![`` (step 3); a title is then
backslash-escaped, a path gets its ``<U+XXXX>`` markers and its code span. Parts are joined with
a blank line, so no untrusted part ever touches another part or a pr-comment fragment.

The neutralizers are applied in rendering order (path, title, body), so ``MentionNeutralizer``
reports names in the order the text appears. The body counts are taken from ``finding_parts``
(the parts as given, before any of this) with ``neutralize.count_parts``.

Not here: suggestion blocks (TODO(#19): the caller passes the rendered block as
``suggestion_block``), the attribution line, the marker, the layout around the parts and forgectl's
neutralizing (TODO(#20)), and the re-anchoring that decides a moved finding's anchor and reason
(TODO(#22)).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from pr_comment import textpipe
from pr_comment.display import sanitize_line
from pr_comment.draft import SEVERITIES
from pr_comment.mentions import MentionNeutralizer
from pr_comment.neutralize import neutralize_characters

FILE_LEVEL_HEADING: Final = "### File-level findings"
MOVED_HEADING: Final = "### Findings that could not be anchored"

# The text of each moved reason (docs/interface.md: Moved findings).
REASON_TEXT: Final = {
    "changed": "the commented line changed after this review was drafted",
    "file_removed": "the file was deleted or renamed after this review was drafted",
    "diff_unavailable": "the changes since this review was drafted could not be compared",
    "base_changed": "the base branch changed after this review was drafted",
    "invalid_position": "the line is not part of this pull request's diff",
    "files_incomplete": "the host's file list for this pull request is incomplete",
}

# ASCII punctuation other than `@`: ! to /, : to ?, [ to `, { to ~.
_TITLE_ESCAPE_RE: Final = re.compile(r"[!-/:-?\[-`{-~]")
_BACKTICK_RUN_RE: Final = re.compile(r"`+")
_SHA_RE: Final = re.compile(r"[0-9a-f]{40}")

_SEPARATOR = "\n\n"


def _is_int(value: object) -> bool:
    return type(value) is int and value >= 1


def _one_line(text: str, what: str) -> str:
    normalized = textpipe.normalize_lines(text)
    if "\n" in normalized:
        raise ValueError(f"{what} must be one line")
    return normalized


def _str_field(finding: Mapping[str, Any], key: str) -> str | None:
    value = finding.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"finding {key} must be a str, got {type(value).__name__}")
    return value


def _clean(text: str, mentions: MentionNeutralizer) -> str:
    """Text pipeline step 3 on one untrusted part: mentions first, then ``<`` and ``![``."""
    return neutralize_characters(mentions.neutralize(text))


def _title(finding: Mapping[str, Any]) -> str | None:
    raw = _str_field(finding, "title")
    if raw is None:
        return None
    title = raw.strip()
    if not title:
        return None
    return _one_line(title, "title")


def finding_parts(finding: Mapping[str, Any]) -> list[str]:
    """The untrusted parts of one finding as given (after step 1, before step 3): the title when
    present, the path, the body and each suggestion line. For ``neutralize.count_parts``, which
    must see them before any neutralizing or escaping."""
    parts: list[str] = []
    title = _str_field(finding, "title")
    if title is not None:
        parts.append(textpipe.normalize_lines(title))
    path = _str_field(finding, "path")
    if path is not None:
        parts.append(textpipe.normalize_lines(path))
    body = _str_field(finding, "body")
    if body is not None:
        parts.append(textpipe.normalize_lines(body))
    suggestion = finding.get("suggestion")
    if isinstance(suggestion, Mapping):
        lines: Any = suggestion.get("lines")  # pyright: ignore[reportUnknownMemberType]
        if isinstance(lines, list):
            parts.extend(line for line in lines if isinstance(line, str))  # pyright: ignore[reportUnknownVariableType]
    return parts


def escape_title(title: str) -> str:
    """Backslash-escape every ASCII punctuation character of ``title`` except ``@``, so it cannot
    close the bold or start other syntax."""
    return _TITLE_ESCAPE_RE.sub(lambda m: "\\" + m.group(), title)


def render_prefix(finding: Mapping[str, Any], mentions: MentionNeutralizer) -> str | None:
    """``**<severity> · <title>**`` with only the present parts, or None when neither exists."""
    severity = finding.get("severity")
    if severity is not None and (not isinstance(severity, str) or severity not in SEVERITIES):
        raise ValueError("finding severity is not a known severity")
    title = _title(finding)
    pieces: list[str] = []
    if severity is not None:
        pieces.append(severity)
    if title is not None:
        pieces.append(escape_title(_clean(title, mentions)))
    if not pieces:
        return None
    return "**" + " · ".join(pieces) + "**"


def render_path_span(
    path: str,
    mentions: MentionNeutralizer,
    *,
    line: int | None = None,
    start_line: int | None = None,
    side: str = "right",
) -> str:
    """The path as a code span of n backticks (one more than the longest run in it), with ``:L``
    or ``:S-E``, and ``(left)`` after the span for a left-side finding.

    The displayed path is ``path`` after step 3, with the characters forgectl's Sanitizing would
    mark (tab included, ``path`` being a one-line field) shown as ``<U+XXXX>`` markers. The markers
    are inserted after neutralizing, so only they hold an ASCII ``<``.
    """
    if side not in ("left", "right"):
        raise ValueError("side must be left or right")
    if line is not None and not _is_int(line):
        raise ValueError("line must be a positive integer")
    if start_line is not None and (not _is_int(start_line) or line is None or start_line >= line):
        raise ValueError("start_line must be a positive integer below line")
    shown = sanitize_line(_clean(_one_line(path, "path"), mentions))
    if line is None:
        suffix = ""
    elif start_line is None:
        suffix = f":{line}"
    else:
        suffix = f":{start_line}-{line}"
    longest = max((len(m.group()) for m in _BACKTICK_RUN_RE.finditer(shown)), default=0)
    fence = "`" * (longest + 1)
    span = f"{fence} {shown}{suffix} {fence}"
    return span + " (left)" if side == "left" else span


def _finding_span(
    finding: Mapping[str, Any], mentions: MentionNeutralizer, line: Any, start: Any
) -> str:
    path = _str_field(finding, "path")
    if path is None:
        raise ValueError("finding has no path")
    side = finding.get("side")
    return render_path_span(
        path, mentions, line=line, start_line=start, side="right" if side is None else side
    )


def render_finding_text(
    finding: Mapping[str, Any],
    mentions: MentionNeutralizer,
    *,
    suggestion_block: str | None = None,
) -> str:
    """The bold prefix (when present), a blank line, the body and the suggestion block (when
    given). ``suggestion_block`` is pr-comment's own text (TODO(#19)) and is added as it is."""
    pieces: list[str] = []
    prefix = render_prefix(finding, mentions)
    if prefix is not None:
        pieces.append(prefix)
    body = _str_field(finding, "body")
    if body:
        pieces.append(_clean(textpipe.normalize_lines(body), mentions))
    if suggestion_block:
        pieces.append(suggestion_block)
    return _SEPARATOR.join(pieces)


@dataclass(frozen=True)
class Anchor:
    """The last anchor at which a finding had a known line: its head sha and lines."""

    sha: str
    line: int | None
    start_line: int | None = None


def last_known_anchor(a: Anchor, b: Anchor | None, *, failed_validation_at_b: bool) -> Anchor:
    """B for a finding that mapped and then failed validation at B, else A (for an ``unanchored``
    entry, the anchor made of its ``head_sha``)."""
    if failed_validation_at_b and b is not None:
        return b
    return a


@dataclass(frozen=True)
class MovedFinding:
    """A finding that went to the review body, its reason, its last anchor and its rendered
    suggestion block, if any (TODO(#19))."""

    finding: Mapping[str, Any]
    reason: str
    anchor: Anchor
    suggestion_block: str | None = None


def render_moved_entry(moved: MovedFinding, mentions: MentionNeutralizer) -> str:
    """The entry line ``<path span> (at <12-hex sha>) — <reason text>``, a blank line and the
    rendered finding text."""
    if moved.reason not in REASON_TEXT:
        raise ValueError("unknown moved reason")
    if not _SHA_RE.fullmatch(moved.anchor.sha):
        raise ValueError("anchor sha must be 40 lowercase hex")
    span = _finding_span(moved.finding, mentions, moved.anchor.line, moved.anchor.start_line)
    entry = f"{span} (at {moved.anchor.sha[:12]}) — {REASON_TEXT[moved.reason]}"
    text = render_finding_text(moved.finding, mentions, suggestion_block=moved.suggestion_block)
    return _SEPARATOR.join([entry, text] if text else [entry])


def _file_level_entry(finding: Mapping[str, Any], mentions: MentionNeutralizer) -> str:
    span = _finding_span(finding, mentions, None, None)
    text = render_finding_text(finding, mentions)
    return _SEPARATOR.join([span, text] if text else [span])


def render_review_parts(
    body: str,
    file_level: Sequence[Mapping[str, Any]],
    moved: Sequence[MovedFinding],
    mentions: MentionNeutralizer,
) -> list[str]:
    """The review body's parts in order, each only when non-empty: the draft ``body``, the
    file-level section and the moved section (moved findings of this run, then ``unanchored``
    entries, as the caller orders them). Each is one block; the layout joins them with a blank
    line (TODO(#20))."""
    parts: list[str] = []
    if body:
        parts.append(_clean(textpipe.normalize_lines(body), mentions))
    if file_level:
        entries = [_file_level_entry(f, mentions) for f in file_level]
        parts.append(_SEPARATOR.join([FILE_LEVEL_HEADING, *entries]))
    if moved:
        entries = [render_moved_entry(m, mentions) for m in moved]
        parts.append(_SEPARATOR.join([MOVED_HEADING, *entries]))
    return parts
