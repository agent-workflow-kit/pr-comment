"""Finding text, paths, review body sections and moved findings (text pipeline steps 3 and 4).

docs/interface.md: Finding text, Review body assembly, Moved findings. Every untrusted part (a
title, a displayed path, a body, the draft body) goes through the same steps, once and in this
order: line separators to LF (step 1), mentions, then ``<`` and ``![`` (step 3); a title is then
backslash-escaped, a path gets its ``<U+XXXX>`` markers and its code span. Parts are joined with
a blank line, so no untrusted part ever touches another part or a pr-comment fragment.

The neutralizers are applied in rendering order (path, title, body), so ``MentionNeutralizer``
reports names in the order the text appears. The body counts are taken from ``finding_parts``
(the parts as given, before any of this) with ``neutralize.count_parts``.

Suggestion blocks are ``render_suggestion`` (applicable, degraded or moved); the caller of
``render_finding_text`` passes the block as ``suggestion_block``, and ``render_moved_entry`` renders
a moved finding's own. Not here: the attribution line, the marker, the layout around the parts and
forgectl's neutralizing (assemble.py, #20), and the re-anchoring that decides a moved finding's
anchor and reason (TODO(#22)).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from pr_comment import textpipe
from pr_comment.display import is_marked, sanitize_line, sanitize_text
from pr_comment.draft import SEVERITIES
from pr_comment.mentions import MentionNeutralizer
from pr_comment.neutralize import neutralize_characters, neutralize_suggestion_lines

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
    title = _one_line(raw, "title").strip()
    if not title:
        return None
    return title


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
    if not path.strip():
        raise ValueError("path must not be empty")
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
    suggestion_block: str | Callable[[], str | None] | None = None,
) -> str:
    """The bold prefix (when present), a blank line, the body and the suggestion block (when
    given). ``suggestion_block`` is pr-comment's own text (``render_suggestion``) and is added
    as it is. A caller that renders the suggestion itself passes a zero-argument callable, called
    after the title and the body, so ``mentions`` reports names in the order the text appears.

    A fence the body leaves open cannot be seen without a parser. ``render_suggestion`` therefore
    renders a degraded or moved block with no fence run in its lines whenever the finding's body
    holds one, so no line of the block can end a fence the body left open and leave later lines
    as live Markdown. An applicable block is only written when ``open_fence_possible(body)`` is
    false; otherwise ``render_suggestion`` degrades it as ``open_fence``.
    """
    pieces: list[str] = []
    prefix = render_prefix(finding, mentions)
    if prefix is not None:
        pieces.append(prefix)
    body = _str_field(finding, "body")
    if body:
        pieces.append(_clean(textpipe.normalize_lines(body), mentions))
    block = suggestion_block() if callable(suggestion_block) else suggestion_block
    if block:
        pieces.append(block)
    return _SEPARATOR.join(pieces)


SUGGESTION_HOSTS: Final = ("github", "gitlab")
DEGRADE_NOTE: Final = (
    "_Suggestion shown as code: it cannot be posted as an applicable suggestion safely._"
)
MOVED_NOTE: Final = "_Suggestion shown as code: this finding could not be posted inline._"

# cmark-gfm (GitHub's engine) clamps a fence length to 255: a fence of 256 or more characters is
# closed by any run of 255 or more in the content. No fence is longer than this.
MAX_FENCE: Final = 255
# Shown inside a run of backticks that no fence can enclose (both backtick and tilde runs of
# 255 or more in one suggestion), every MAX_FENCE - 1 characters; an ASCII ``<`` only in ours.
_RUN_SPLIT: Final = "<run split>"
_FENCE_RUN_RE: Final = re.compile(r"`{3,}|~{3,}")
# What forgectl rewrites in the posted text (its second colon); an applicable suggestion holding
# it would be posted altered, so it is degraded.
_DEDUPE_TEXT_RE: Final = re.compile(r"forgectl:dedupe:", re.IGNORECASE | re.ASCII)


@dataclass(frozen=True)
class SuggestionBlock:
    """A rendered suggestion: the text to add to the finding text and, for an applicable
    suggestion that was degraded, the reason (None for an applicable or a moved one)."""

    block: str
    degraded: str | None


def _suggestion_lines(finding: Mapping[str, Any]) -> list[str] | None:
    """The finding's suggestion lines after step 1, or None when it has no suggestion."""
    suggestion = finding.get("suggestion")
    if suggestion is None:
        return None
    if not isinstance(suggestion, Mapping) or set(suggestion) != {"lines"}:  # pyright: ignore[reportUnknownArgumentType]
        raise ValueError("suggestion must be an object with only lines")
    lines: Any = suggestion["lines"]  # pyright: ignore[reportUnknownVariableType]
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):  # pyright: ignore[reportUnknownVariableType]
        raise ValueError("suggestion lines must be a list of str")
    return [_one_line(line, "suggestion line") for line in lines]  # pyright: ignore[reportUnknownVariableType]


def _refuse_suggestion_fences(lines: Sequence[str]) -> None:
    """Text pipeline step 2 for suggestion lines: a line that holds a suggestion fence could, with
    an open fence before it, forge an applicable block, so no render call emits one."""
    if any(textpipe.suggestion_fence(line) for line in lines):
        raise ValueError("a suggestion line holds a suggestion fence")


_FENCE_LINE_RE: Final = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
# What may precede a fence run on a line for the line to be able to start a fence: block quote
# and list markers and spacing. Any other text makes the run part of a paragraph.
_CONTAINER_STARTS: Final = frozenset(">-+*0123456789")
_CONTAINER_PREFIX_RE: Final = re.compile(r"[ \t>\-+*0-9.)]*")


def open_fence_possible(body: str) -> bool:
    """True when ``body`` can leave a code fence open at its end, so a block written after it may
    be swallowed. Linear time, over the body's lines only, and conservative.

    A line with up to three spaces, a run of three or more backticks or tildes and the rest of a
    fence line (no backtick in a backtick fence's info string) opens or closes a fence as
    CommonMark says; the state is followed to the end. A run on any other line that could still
    start a block (only spaces, tabs, block quote and list markers and digits before it: a quote,
    a list item, a deeper indent) makes the answer True, since pr-comment parses no containers.
    Once an earlier line outside a fence could start a container (it starts with ``>``, ``-``,
    ``+``, ``*`` or a digit), a fence line with one to three leading spaces also makes the answer
    True: the indent may belong to the container, so a dedented line would open a new fence.
    A run after other text on a line belongs to a paragraph and is ignored."""
    open_char = ""
    open_size = 0
    container_seen = False
    for line in textpipe.normalize_lines(body).split("\n"):
        match = _FENCE_LINE_RE.fullmatch(line)
        if match is not None:
            run, rest = match.groups()
            if not open_char and container_seen and line[0] == " ":
                # The indent may belong to a quote or list item above, so the fence this line
                # opens could be closed by a dedented line that then opens a top-level one.
                return True
            if open_char:
                if run[0] == open_char and len(run) >= open_size and not rest.strip(" \t"):
                    open_char = ""
                continue
            if run[0] == "`" and "`" in rest:
                continue
            open_char, open_size = run[0], len(run)
            continue
        if open_char:
            continue
        if line.lstrip(" \t")[:1] in _CONTAINER_STARTS:
            container_seen = True
        found = _FENCE_RUN_RE.search(line)
        if found is not None and _CONTAINER_PREFIX_RE.fullmatch(line[: found.start()]):
            return True
    return bool(open_char)


def degrade_reason(lines: Sequence[str]) -> str | None:
    """The first reason an applicable suggestion must be degraded, in the order
    ``invisible_chars``, ``fence``, ``slash_line``, ``dedupe_text``, or None. (``open_fence`` is
    decided by ``render_suggestion`` from the body, after these.)"""
    if any(is_marked(ch) for line in lines for ch in line):
        return "invisible_chars"
    if any(_FENCE_RUN_RE.search(line) for line in lines):
        return "fence"
    if any(line.startswith("/") for line in lines):
        return "slash_line"
    if any(_DEDUPE_TEXT_RE.search(line) for line in lines):
        return "dedupe_text"
    return None


def _longest_run(lines: Sequence[str], char: str) -> int:
    run = re.compile(re.escape(char) + "+")
    return max((len(m.group()) for line in lines for m in run.finditer(line)), default=0)


def _fence_for(lines: Sequence[str]) -> tuple[str, int | None]:
    """The fence character and length for ``lines``: backticks one more than the longest run,
    else (when that is over ``MAX_FENCE``) tildes the same way, else no size."""
    for char in "`~":
        size = max(3, _longest_run(lines, char) + 1)
        if size <= MAX_FENCE:
            return char, size
    return "`", None


def _split_long_run(match: re.Match[str]) -> str:
    run = match.group()
    return _RUN_SPLIT.join(run[i : i + MAX_FENCE - 1] for i in range(0, len(run), MAX_FENCE - 1))


def _isolate_run(match: re.Match[str]) -> str:
    run = match.group()
    return _RUN_SPLIT.join(run[i : i + 2] for i in range(0, len(run), 2))


def _text_block(
    lines: Sequence[str], mentions: MentionNeutralizer, *, isolate: bool = False
) -> str:
    """The lines as a ``text`` block: mentions, then ``<`` and ``![``, then the markers (which
    are not neutralized), in a backtick fence one more than the longest run in the content."""
    shown = [
        sanitize_text(line)
        for line in neutralize_suggestion_lines(
            (mentions.neutralize(line) for line in lines), applicable=False
        )
    ]
    if isolate:
        shown = [_FENCE_RUN_RE.sub(_isolate_run, line) for line in shown]
    char, size = _fence_for(shown)
    if size is None:
        shown = [_BACKTICK_RUN_RE.sub(_split_long_run, line) for line in shown]
        char, size = "`", max(3, _longest_run(shown, "`") + 1)
    fence = char * size
    return "\n".join([fence + "text", *shown, fence])


def render_suggestion(
    finding: Mapping[str, Any],
    mentions: MentionNeutralizer,
    *,
    host: str,
    moved: bool = False,
    isolate: bool = False,
) -> SuggestionBlock | None:
    """The finding's suggestion block, or None when it has none.

    An applicable suggestion (an inline finding on the right side with a line) is a three-backtick
    fence: ``suggestion`` on GitHub, ``suggestion:-N+0`` on GitLab with ``N = line - start_line``;
    its lines are raw. When a line has an invisible character, a fence run or a leading ``/``, or
    when ``moved`` is true, or when the finding's own body can leave a fence open
    (``open_fence_possible``, reason ``open_fence``, last in the order), it is a ``text`` block
    with a note before it and its lines neutralized and marked. A moved block reports no reason;
    the other cases (``invisible_chars``, ``fence``, ``slash_line``, ``dedupe_text`` and
    ``open_fence``) report theirs. Text pipeline step 6's leading space for a ``/`` line is
    forgectl's pre-applied neutralizing, applied once to the whole body by
    ``assemble.build_body`` and ``assemble.build_inline``.

    A degraded or moved block has its fence runs split when the finding's own body holds a fence
    run or when ``isolate`` is true (the caller saw one in an earlier untrusted part of the same
    body, which could leave a fence open too).
    """
    if host not in SUGGESTION_HOSTS:
        raise ValueError("host must be github or gitlab")
    lines = _suggestion_lines(finding)
    if lines is None:
        return None
    _refuse_suggestion_fences(lines)
    body = _str_field(finding, "body")
    isolate = isolate or (body is not None and _FENCE_RUN_RE.search(body) is not None)
    if moved:
        block = _text_block(lines, mentions, isolate=isolate)
        return SuggestionBlock(MOVED_NOTE + _SEPARATOR + block, None)
    line: Any = finding.get("line")
    start: Any = finding.get("start_line")
    if not _is_int(line):
        raise ValueError("an applicable suggestion needs a line")
    if start is not None and (not _is_int(start) or start >= line):
        raise ValueError("start_line must be a positive integer below line")
    if finding.get("side") not in (None, "right"):
        raise ValueError("a suggestion is only for the right side")
    reason = degrade_reason(lines)
    if reason is None and body is not None and open_fence_possible(body):
        reason = "open_fence"
    if reason is not None:
        block = _text_block(lines, mentions, isolate=isolate)
        return SuggestionBlock(DEGRADE_NOTE + _SEPARATOR + block, reason)
    offset = 0 if start is None else int(line) - int(start)
    info = "suggestion" if host == "github" else f"suggestion:-{offset}+0"
    return SuggestionBlock("\n".join(["```" + info, *lines, "```"]), None)


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
    """A finding that went to the review body, its reason, its last anchor and, optionally, its
    rendered suggestion block (when None, ``render_moved_entry`` renders the finding's own).

    ``anchor.line=None`` renders as a file-level span. The caller must pass the line for a finding
    that was inline (TODO(#22): the re-anchoring that decides the anchor)."""

    finding: Mapping[str, Any]
    reason: str
    anchor: Anchor
    suggestion_block: str | Callable[[], str | None] | None = None


def render_moved_entry(
    moved: MovedFinding, mentions: MentionNeutralizer, *, isolate: bool = False
) -> str:
    """The entry line ``<path span> (at <12-hex sha>) — <reason text>``, a blank line and the
    rendered finding text. ``isolate`` is passed to ``render_suggestion``."""
    if moved.reason not in REASON_TEXT:
        raise ValueError("unknown moved reason")
    if not _SHA_RE.fullmatch(moved.anchor.sha):
        raise ValueError("anchor sha must be 40 lowercase hex")
    span = _finding_span(moved.finding, mentions, moved.anchor.line, moved.anchor.start_line)
    entry = f"{span} (at {moved.anchor.sha[:12]}) — {REASON_TEXT[moved.reason]}"
    text = render_finding_text(moved.finding, mentions, suggestion_block=moved.suggestion_block)
    if moved.suggestion_block is None:
        # After the title and body, so mentions are reported in the order the text appears.
        # host is irrelevant for a moved block (always code, never a suggestion fence).
        shown = render_suggestion(
            moved.finding, mentions, host="github", moved=True, isolate=isolate
        )
        if shown is not None:
            text = _SEPARATOR.join([text, shown.block] if text else [shown.block])
    return _SEPARATOR.join([entry, text] if text else [entry])


def _file_level_entry(finding: Mapping[str, Any], mentions: MentionNeutralizer) -> str:
    span = _finding_span(finding, mentions, None, None)
    text = render_finding_text(finding, mentions)
    return _SEPARATOR.join([span, text] if text else [span])


def _has_fence_run(finding: Mapping[str, Any]) -> bool:
    body = _str_field(finding, "body")
    return body is not None and _FENCE_RUN_RE.search(body) is not None


def render_review_parts(
    body: str,
    file_level: Sequence[Mapping[str, Any]],
    moved: Sequence[MovedFinding],
    mentions: MentionNeutralizer,
) -> list[str]:
    """The review body's parts in order, each only when non-empty: the draft ``body``, the
    file-level section and the moved section (moved findings of this run, then ``unanchored``
    entries, as the caller orders them). Each is one block; ``assemble.build_body`` joins them
    with a blank line. A fence run in the draft body, a file-level body or an earlier moved body
    makes each later moved suggestion block split its fence runs (``render_suggestion``)."""
    parts: list[str] = []
    open_fence = _FENCE_RUN_RE.search(body) is not None or any(
        _has_fence_run(f) for f in file_level
    )
    if body:
        parts.append(_clean(textpipe.normalize_lines(body), mentions))
    if file_level:
        entries = [_file_level_entry(f, mentions) for f in file_level]
        parts.append(_SEPARATOR.join([FILE_LEVEL_HEADING, *entries]))
    if moved:
        moved_entries: list[str] = []
        for m in moved:
            moved_entries.append(render_moved_entry(m, mentions, isolate=open_fence))
            open_fence = open_fence or _has_fence_run(m.finding)
        parts.append(_SEPARATOR.join([MOVED_HEADING, *moved_entries]))
    return parts
