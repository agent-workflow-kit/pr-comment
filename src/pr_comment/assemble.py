"""Body assembly: the attribution line, the layouts, the marker and forgectl's pre-applied
neutralizing (docs/interface.md: Footers and marker, Marker detection, Body structure, Text
pipeline steps 4 to 6; issue #20).

``render_attribution`` writes the line from a footer template (step 4) and passes it through
``MentionNeutralizer``. Call it before the parts are rendered, so the mention report lists names
in the order the posted text shows them. ``build_body`` lays out the already rendered parts
(step 5) and ``build_inline`` does the same for an inline comment; both then apply forgectl's
neutralizing (step 6), once, to the whole text, and report its counts. The result is exactly the
text handed to forgectl on stdin, so ``body_sha256`` and ``body_bytes`` of the plan and forgectl's
own recomputation agree, and forgectl's ``neutralized_lines`` and ``markers_removed`` are 0.

``has_marker`` is the normative marker detection. ``render_inline_finding`` renders an inline
comment's finding text with its suggestion and reports why the suggestion was degraded, if it was.

Not here: the dedupe key and the plan hash (TODO(#21)), the log event ``suggestion_degraded`` and
the ``suggestions_degraded`` list of the plan, which the posting commands write from
``InlineFinding.degraded`` (TODO(#27)), and the choice of ``host`` per review, which comes from
forgectl's ``show`` (TODO(#29)).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from pr_comment import textpipe
from pr_comment.config import ConfigError, check_footer_template
from pr_comment.mentions import MentionNeutralizer
from pr_comment.neutralize import neutralize_characters
from pr_comment.render import SuggestionBlock, render_finding_text, render_suggestion

MARKER: Final = "<!-- pr-comment:v1 -->"
RULE: Final = "---"
_SEPARATOR: Final = "\n\n"

_ACTOR_RE: Final = re.compile(r"(human|auto):([A-Za-z0-9._@-]{1,64})")
_NAME_RE: Final = re.compile(r"[A-Za-z0-9._@-]{1,64}")
_DECISION_ID_RE: Final = re.compile(r"[A-Za-z0-9._:@/-]{1,128}")
_VERSION_RE: Final = re.compile(r"[A-Za-z0-9._+-]{1,64}")
_SHA_RE: Final = re.compile(r"[0-9a-f]{40}")
_TOKEN_RE: Final = re.compile(r"\{\{|\}\}|\{([a-z_]+)\}")
_FIRST_CHAR_RE: Final = re.compile(r"[A-Za-z]")

_DEDUPE_LINE_RE: Final = re.compile(r"<!-- forgectl:dedupe:[A-Za-z0-9._:-]{1,128} -->")
_DEDUPE_TEXT_RE: Final = re.compile(r"(forgectl:dedupe):", re.IGNORECASE | re.ASCII)


def _span(value: str) -> str:
    """A placeholder value as a code span; an empty value is written as nothing. The values'
    grammars hold no backtick, so one backtick on each side is always a complete span."""
    return f"`{value}`" if value else ""


def render_attribution(
    actor: str,
    *,
    footer_human: str | bool,
    footer_auto: str,
    me_name: str,
    head_sha: str,
    decision_id: str | None,
    tool_version: str,
    mentions: MentionNeutralizer,
) -> str | None:
    """The attribution line for ``actor``, or None for a human actor with ``footer.human: false``.

    The template is ``footer_human`` for ``human:`` and ``footer_auto`` for ``auto:``; it is
    checked again here (it may come from anywhere), as are the placeholder values, which must
    match their grammars (so they hold no backtick, bracket, ``<`` or line break). ``{user}``,
    ``{policy}`` and ``{decision_id}`` are written as code spans when non-empty, ``{sha}`` is the
    first 12 hex of ``head_sha``. The line passes through ``mentions`` and must then start with an
    ASCII letter, else ``ConfigError`` with the template key (``footer.human`` or
    ``footer.auto``).
    """
    parsed = _ACTOR_RE.fullmatch(actor) if isinstance(actor, str) else None
    if parsed is None:
        raise ValueError("actor must be human:<name> or auto:<name>")
    kind, name = parsed.groups()
    if not isinstance(me_name, str) or not _NAME_RE.fullmatch(me_name):
        raise ValueError("me_name is not a valid name")
    if decision_id is not None and (
        not isinstance(decision_id, str) or not _DECISION_ID_RE.fullmatch(decision_id)
    ):
        raise ValueError("decision_id is not a valid decision id")
    if not isinstance(tool_version, str) or not _VERSION_RE.fullmatch(tool_version):
        raise ValueError("tool_version is not a valid version")
    if not isinstance(head_sha, str) or not _SHA_RE.fullmatch(head_sha):
        raise ValueError("head_sha must be 40 lowercase hex")

    key = f"footer.{kind}"
    template: str | bool = footer_human if kind == "human" else footer_auto
    if template is False:
        if kind == "human":
            return None
        raise ConfigError(key, "must be a non-empty string, never false")
    try:
        checked = check_footer_template(template, allow_false=False)
    except ValueError as err:
        raise ConfigError(key, str(err)) from None
    assert checked is not False

    values = {
        "user": _span(name if kind == "human" else me_name),
        "policy": _span(name if kind == "auto" else ""),
        "sha": head_sha[:12],
        "decision_id": _span(decision_id or ""),
        "tool_version": tool_version,
    }

    def fill(match: re.Match[str]) -> str:
        token = match[0]
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        return values[match[1]]

    line = mentions.neutralize(_TOKEN_RE.sub(fill, checked))
    if not _FIRST_CHAR_RE.match(line):
        raise ConfigError(key, "the rendered line must start with an ASCII letter")
    return line


def render_text_part(text: str, mentions: MentionNeutralizer) -> str:
    """One untrusted text (a body file) as a part: step 1's line split, mentions, then ``<`` and
    ``![`` (step 3). Control characters and suggestion fences are the caller's checks
    (``textpipe.text_problem``)."""
    return neutralize_characters(mentions.neutralize(textpipe.normalize_lines(text)))


@dataclass(frozen=True)
class InlineFinding:
    """An inline comment's finding text and, for a suggestion that was degraded, the reason."""

    text: str
    degraded: str | None


def render_inline_finding(
    finding: Mapping[str, Any], mentions: MentionNeutralizer, *, host: str
) -> InlineFinding:
    """The finding text of an inline comment with its suggestion block (``host`` is ``github`` or
    ``gitlab``, forgectl's backend for the PR). The suggestion is rendered after the title and the
    body, so mention names come out in the order the text shows them. A body that holds a fence run
    makes a degraded block's fence runs split (``render.render_suggestion``)."""
    rendered: list[SuggestionBlock] = []

    def block() -> str | None:
        result = render_suggestion(finding, mentions, host=host)
        if result is None:
            return None
        rendered.append(result)
        return result.block

    text = render_finding_text(finding, mentions, suggestion_block=block)
    return InlineFinding(text, rendered[0].degraded if rendered else None)


@dataclass(frozen=True)
class Neutralized:
    """Text after forgectl's neutralizing and its two counts (forgectl's ``neutralized_lines``
    and ``markers_removed``)."""

    text: str
    neutralized_lines: int
    markers_removed: int


def forgectl_neutralize(text: str) -> Neutralized:
    """Text pipeline step 6, as forgectl does it (forgectl, Neutralizing step 2 and Dedupe key):
    every line whose first character is ``/`` gets one leading space, and case-insensitive
    ``forgectl:dedupe:`` has its second colon replaced by ``-``. ``text`` is already split on LF
    (step 1). Applying it twice equals applying it once."""
    lines = text.split("\n")
    slashed = 0
    for i, line in enumerate(lines):
        if line.startswith("/"):
            lines[i] = " " + line
            slashed += 1
    joined = "\n".join(lines)
    replaced, markers = _DEDUPE_TEXT_RE.subn(r"\1-", joined)
    return Neutralized(replaced, slashed, markers)


@dataclass(frozen=True)
class Body(Neutralized):
    """The body handed to forgectl."""


def _finish(text: str) -> Body:
    result = forgectl_neutralize(text)
    return Body(result.text, result.neutralized_lines, result.markers_removed)


def build_body(parts: Sequence[str], attribution: str | None) -> Body:
    """A review, comment, reply or approve body: the attribution line, a blank line, ``---``, a
    blank line, the non-empty parts and the marker, each separated by one blank line. Without
    parts the ``---`` is left out; with ``attribution=None`` (``footer.human: false``) the line
    and the ``---`` are left out. Parts are already rendered and neutralized (step 3); trailing
    newlines of a part are dropped, so parts are separated by exactly one blank line."""
    pieces: list[str] = []
    shown = [stripped for part in parts if (stripped := part.rstrip("\n"))]
    if attribution is not None:
        pieces.append(attribution)
        if shown:
            pieces.append(RULE)
    pieces.extend(shown)
    pieces.append(MARKER)
    return _finish(_SEPARATOR.join(pieces))


def build_inline(finding_text: str) -> Body:
    """An inline comment: the rendered finding text, a blank line and the bare marker."""
    text = finding_text.rstrip("\n")
    pieces = [text, MARKER] if text else [MARKER]
    return _finish(_SEPARATOR.join(pieces))


def has_marker(body: str) -> bool:
    """Marker detection (docs/interface.md): after removing trailing whitespace and at most one
    last line of the form ``<!-- forgectl:dedupe:<key> -->``, the last non-empty line is exactly
    the marker. Line separators are split as forgectl does. The author check is the caller's."""
    lines = textpipe.normalize_lines(body.rstrip()).split("\n")
    if lines and _DEDUPE_LINE_RE.fullmatch(lines[-1]):
        lines.pop()
    while lines and not lines[-1].strip():
        lines.pop()
    return bool(lines) and lines[-1] == MARKER
