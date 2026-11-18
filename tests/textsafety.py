"""A rendered-output oracle and a whole-pipeline driver for the text-safety tests (issue #22).

The driver takes a draft (or a body file) through the real modules in the order of
docs/interface.md, Text pipeline: draft validation and the step 1 and 2 refusals (``draft.py``),
mentions, ``<`` and ``![`` (``render.py``), the attribution line, the layouts and forgectl's
pre-applied neutralizing (``assemble.py``). It returns the bodies pr-comment would hand to forgectl
on stdin. TODO(#27): once the posting commands build the plan, drive the plan builder instead.

The oracle renders such a body with the pinned ``cmarkgfm`` (GitHub's engine) in three ways, the
safe default, an unsafe probe that lets raw HTML through (with liberal tag recognition) and the safe
default with footnotes on (mentions and suggestions only), and lists
what a reader or a host could not tolerate. It decodes text the way an HTML5 parser does
(``html.unescape``, unterminated references included), because cmark copies raw HTML blocks
verbatim and a downstream parser decodes them. ``violations`` returns names, so a test can show
that the oracle can fail.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

import cmarkgfm  # pyright: ignore[reportMissingTypeStubs]
from cmarkgfm.cmark import Options  # pyright: ignore[reportMissingTypeStubs]

from pr_comment import textpipe
from pr_comment.assemble import (
    MARKER,
    Body,
    InlineFinding,
    build_body,
    build_inline,
    forgectl_neutralize,
    has_marker,
    render_attribution,
    render_inline_finding,
    render_text_part,
)
from pr_comment.config import DEFAULT_FOOTER_AUTO, DEFAULT_FOOTER_HUMAN
from pr_comment.draft import text_issues, validate_document
from pr_comment.mentions import MentionNeutralizer
from pr_comment.render import Anchor, MovedFinding, render_review_parts

SHA = "0123456789abcdef0123456789abcdef01234567"
SHA2 = "89abcdef0123456789abcdef0123456789abcdef"
DRAFT_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
# The line forgectl appends after the body (docs/interface.md: Text pipeline).
DEDUPE_LINE = "<!-- forgectl:dedupe:prc:rv:01ARZ3NDEKTSV4RRFFQ69G5FAV -->"
ALLOWED_COMMENTS = frozenset({"pr-comment:v1", "forgectl:dedupe:prc:rv:01ARZ3NDEKTSV4RRFFQ69G5FAV"})

# --- the driver ---------------------------------------------------------------------------------


class Refused(Exception):
    """The pipeline refused the input before building any body (exit 2 in the real command)."""

    def __init__(self, kind: str, pointer: str) -> None:
        super().__init__(f"{kind} at {pointer}")
        self.kind = kind
        self.pointer = pointer


@dataclass(frozen=True)
class Expect:
    """What the oracle knows about a body from the way it was built.

    ``attribution`` is the rendered line (None when the footer is off); ``has_rule`` whether the
    layout put ``---`` after it; ``suggestion`` the lines and info string of the applicable
    suggestion pr-comment wrote into an inline comment (None: no applicable suggestion exists);
    ``prefix`` whether an inline comment starts with a bold prefix.
    """

    attribution: str | None = None
    has_rule: bool = False
    suggestion: tuple[tuple[str, ...], str] | None = None
    prefix: bool = False


@dataclass(frozen=True)
class Posted:
    """One body handed to forgectl, with what the oracle expects of it."""

    label: str
    body: Body
    expect: Expect

    @property
    def text(self) -> str:
        return self.body.text


def make_document(
    *,
    body: str = "",
    findings: Sequence[Mapping[str, Any]] = (),
    unanchored: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """A valid draft around the given text fields."""
    document: dict[str, Any] = {
        "schema_version": 1,
        "draft_id": DRAFT_ID,
        "pr": "github.com/example/widgets#7",
        "anchor": {"head_sha": SHA, "base_ref": "main", "base_sha": SHA2, "merge_base_sha": None},
        "event": "comment",
        "body": body,
        "findings": [dict(f) for f in findings],
    }
    if unanchored:
        document["unanchored"] = [dict(u) for u in unanchored]
    return document


def make_finding(fid: str = "f1", **fields: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {"id": fid, "path": "src/a.py", "line": 10, "body": "Body text."}
    finding.update(fields)
    return finding


def make_unanchored(finding: Mapping[str, Any], reason: str = "changed") -> dict[str, Any]:
    return {"finding": dict(finding), "head_sha": SHA2, "reason": reason}


def refuse_text(document: object) -> None:
    """Draft validation, then Text pipeline steps 1 and 2, as ``draft.parse_draft`` does."""
    errors = validate_document(document)
    if errors:
        raise Refused("invalid_draft", errors[0]["pointer"])
    issues = text_issues(document)
    if issues:
        raise Refused(issues[0]["kind"], issues[0]["pointer"])


def _attribution(
    actor: str,
    mentions: MentionNeutralizer,
    *,
    footer_human: str | bool,
    footer_auto: str,
    decision_id: str | None,
) -> str | None:
    return render_attribution(
        actor,
        footer_human=footer_human,
        footer_auto=footer_auto,
        me_name="alice",
        head_sha=SHA,
        decision_id=decision_id,
        tool_version="0.1.0",
        mentions=mentions,
    )


def run_review(
    document: Mapping[str, Any],
    *,
    actor: str = "human:alice",
    host: str = "github",
    moved: Iterable[str] = (),
    allow: Iterable[str] = (),
    footer_human: str | bool = DEFAULT_FOOTER_HUMAN,
    footer_auto: str = DEFAULT_FOOTER_AUTO,
    decision_id: str | None = None,
) -> list[Posted]:
    """A draft through the pipeline: the review body first, then one body per inline comment in
    draft order. ``moved`` lists ids of findings that re-anchoring moved to the review body;
    ``unanchored`` entries always move. Raises ``Refused``."""
    refuse_text(document)
    mentions = MentionNeutralizer("neutralize", allow)
    attribution = _attribution(
        actor, mentions, footer_human=footer_human, footer_auto=footer_auto, decision_id=decision_id
    )
    moved_ids = set(moved)
    inline: list[Mapping[str, Any]] = []
    file_level: list[Mapping[str, Any]] = []
    moved_findings: list[MovedFinding] = []
    for finding in document["findings"]:
        if finding["line"] is None:
            file_level.append(finding)
        elif finding["id"] in moved_ids:
            anchor = Anchor(SHA, finding["line"], finding.get("start_line"))
            moved_findings.append(MovedFinding(finding, "changed", anchor))
        else:
            inline.append(finding)
    for entry in document.get("unanchored", []):
        finding = entry["finding"]
        anchor = Anchor(entry["head_sha"], finding["line"], finding.get("start_line"))
        moved_findings.append(MovedFinding(finding, entry["reason"], anchor))
    parts = render_review_parts(document["body"], file_level, moved_findings, mentions)
    review = build_body(parts, attribution)
    posted = [
        Posted(
            "review",
            review,
            Expect(attribution, has_rule=any(p.rstrip("\n") for p in parts)),
        )
    ]
    for finding in inline:
        rendered = render_inline_finding(finding, mentions, host=host)
        posted.append(
            Posted(
                f"inline {finding['id']}",
                build_inline(rendered.text),
                _inline_expect(finding, rendered, host),
            )
        )
    return posted


def _inline_expect(finding: Mapping[str, Any], rendered: InlineFinding, host: str) -> Expect:
    suggestion: tuple[tuple[str, ...], str] | None = None
    if "suggestion" in finding and rendered.degraded is None:
        line, start = finding["line"], finding.get("start_line")
        info = (
            "suggestion"
            if host == "github"
            else f"suggestion:-{0 if start is None else line - start}+0"
        )
        suggestion = (tuple(finding["suggestion"]["lines"]), info)
    has_prefix = finding.get("severity") is not None or bool((finding.get("title") or "").strip())
    return Expect(None, suggestion=suggestion, prefix=has_prefix)


def run_text_command(
    text: str,
    *,
    actor: str = "human:alice",
    allow: Iterable[str] = (),
    footer_human: str | bool = DEFAULT_FOOTER_HUMAN,
    footer_auto: str = DEFAULT_FOOTER_AUTO,
) -> list[Posted]:
    """A body file for ``comment``, ``reply`` or ``approve`` through the pipeline."""
    problem = textpipe.text_problem(text)
    if problem is not None:
        raise Refused(problem, "/body")
    mentions = MentionNeutralizer("neutralize", allow)
    attribution = _attribution(
        actor, mentions, footer_human=footer_human, footer_auto=footer_auto, decision_id=None
    )
    part = render_text_part(text, mentions)
    parts = [part] if part.rstrip("\n") else []
    body = build_body(parts, attribution)
    return [Posted("comment", body, Expect(attribution, has_rule=bool(parts)))]


# --- the oracle ---------------------------------------------------------------------------------

# Tags cmark-gfm itself writes. Anything else in the unsafe probe is raw HTML a part carried in.
CMARK_TAGS = frozenset(
    "p em strong a code pre ul ol li blockquote hr br del table thead tbody tr th td h1 h2 h3 h4 "
    "h5 h6 input sup section div".split()
)
VOID_TAGS = frozenset({"br", "hr", "img", "input", "meta", "link", "source", "area", "col"})
# Attributes that fetch or hide something. Nothing pr-comment writes uses them.
FORBIDDEN_ATTRIBUTES = frozenset({"src", "srcset", "style", "background", "poster", "data"})
LIVE_MENTION = re.compile(r"(?<![A-Za-z0-9])@")
MARKER_TEXT = re.compile(r"<U\+[0-9A-F]{4,6}>|<run split>")


@dataclass
class Node:
    text: str
    in_code: bool
    in_applicable: bool
    in_pre: bool = False


class Page(HTMLParser):
    """A cmark rendering as tags, comments, text nodes (decoded the HTML5 way) and pre blocks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.comments: list[str] = []
        self.pis: list[str] = []
        self.decls: list[str] = []
        self.nodes: list[Node] = []
        self.pres: list[tuple[str, str]] = []
        self._stack: list[str] = []
        self._pre_lang: list[str] = []
        self._pre_text: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        if tag in VOID_TAGS:
            return
        self._stack.append(tag)
        if tag == "pre":
            self._pre_lang.append(dict(attrs).get("lang") or "")
            self._pre_text.append([])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_endtag(self, tag: str) -> None:
        if tag not in self._stack:
            return
        while self._stack:
            top = self._stack.pop()
            if top == "pre" and self._pre_lang:
                self.pres.append((self._pre_lang.pop(), "".join(self._pre_text.pop())))
            if top == tag:
                break

    def handle_comment(self, data: str) -> None:
        self.comments.append(data)

    def handle_pi(self, data: str) -> None:
        self.pis.append(data)

    def handle_decl(self, decl: str) -> None:
        self.decls.append(decl)

    def unknown_decl(self, data: str) -> None:
        self.decls.append(data)

    def handle_data(self, data: str) -> None:
        applicable = any(is_suggestion_lang(lang) for lang in self._pre_lang)
        self.nodes.append(Node(data, "code" in self._stack, applicable, "pre" in self._stack))
        for chunk in self._pre_text:
            chunk.append(data)


def is_suggestion_lang(lang: str) -> bool:
    return lang.strip().lower().startswith("suggestion")


# The oracle keeps its own renderers rather than the ``cmark`` and ``cmark_unsafe`` helpers of
# tests/test_assemble.py and tests/test_mentions.py: the probe must also recognize tags liberally,
# which those helpers do not, and the pinned options live in this one place.


def render_safe(markdown: str) -> str:
    """What GitHub's engine shows: raw HTML omitted."""
    return cmarkgfm.github_flavored_markdown_to_html(markdown)  # pyright: ignore[reportUnknownMemberType]


def render_probe(markdown: str) -> str:
    """The same with raw HTML passed through verbatim and liberal tag recognition: what a
    downstream parser would be handed, were a construct to survive."""
    return cmarkgfm.github_flavored_markdown_to_html(  # pyright: ignore[reportUnknownMemberType]
        markdown, options=Options.CMARK_OPT_UNSAFE | Options.CMARK_OPT_LIBERAL_HTML_TAG
    )


def render_footnotes(markdown: str) -> str:
    """The safe rendering with footnotes on (GitHub supports them): a footnote definition holds
    block content that the plain rendering shows as a paragraph."""
    return cmarkgfm.github_flavored_markdown_to_html(  # pyright: ignore[reportUnknownMemberType]
        markdown, options=Options.CMARK_OPT_FOOTNOTES
    )


def parse(page_html: str) -> Page:
    page = Page()
    page.feed(page_html)
    page.close()
    return page


def _unindented(text: str) -> str:
    """Each line without leading spaces and tabs: an open fence indented by up to three spaces
    strips that much from every content line (part of a tab included)."""
    return "\n".join(line.lstrip(" \t") for line in text.split("\n"))


_TRAILING_OK = re.compile(r"\s*(?:`{3,}|~{3,}|<!--.*-->)?\s*")


def live_mentions(page: Page, written: str | None = None) -> list[str]:
    """Text nodes (outside an applicable suggestion block) holding an `@` that is not preceded by
    an ASCII letter or digit within the node; the start of a node counts as a non-letter.

    ``written`` is the text of pr-comment's own applicable suggestion block, as the source has it,
    when an open fence of an earlier part swallowed it: its raw lines are replacement code that
    pr-comment leaves unneutralized by design, and in a ``pre`` block they are code (the oracle
    requires that they are shown there). Only that block, as one run of lines of the swallowing
    ``pre`` node, is exempt."""
    found: list[str] = []
    swallowing = -1
    if written is not None:
        # the swallowing node is the last ``pre`` holding the written block; an earlier ``pre``
        # holding the same text is the body's own and gets no exemption
        for index, node in enumerate(page.nodes):
            holds = _unindented(written) in _unindented(node.text)
            if node.in_pre and not node.in_applicable and holds:
                swallowing = index
    for index, node in enumerate(page.nodes):
        if node.in_applicable:
            continue
        text = node.text
        if index == swallowing and written is not None:
            # only the written block itself, one contiguous run of lines (the last, as pr-comment
            # appends it after the body), is exempt; a line of the body that merely equals one of
            # its lines is not. Only closing fences, blank lines and the marker comments may
            # follow it in the node.
            flat, tail = _unindented(text), _unindented(written)
            at = flat.rfind("\n" + tail)
            at = 0 if at < 0 and flat.startswith(tail) else at + 1 if at >= 0 else -1
            if at >= 0 and all(
                _TRAILING_OK.fullmatch(line) for line in flat[at + len(tail) :].split("\n")
            ):
                text = flat[:at] + flat[at + len(tail) :]
        if LIVE_MENTION.search(text):
            found.append(node.text)
    return found


def violations(text: str, expect: Expect | None = None) -> list[str]:
    """Names of the properties a body violates when rendered as a host would, as posted (the
    dedupe line follows it). Empty when the body is safe."""
    expect = expect or Expect()
    posted = text + "\n" + DEDUPE_LINE
    safe_html = render_safe(posted)
    probe_html = render_probe(posted)
    safe, probe = parse(safe_html), parse(probe_html)
    footnotes = parse(render_footnotes(posted))
    found: list[str] = []

    for name, attributes in probe.tags:
        if name == "img":
            found.append("image")
        elif name not in CMARK_TAGS:
            found.append(f"raw HTML tag <{name}>")
        for attribute in attributes:
            if attribute in FORBIDDEN_ATTRIBUTES or attribute.startswith("on"):
                found.append(f"attribute {attribute}")
    stray = [c.strip() for c in probe.comments if c.strip() not in ALLOWED_COMMENTS]
    if stray:
        found.append("raw HTML comment")
    if len(probe.comments) > len(ALLOWED_COMMENTS):
        found.append("more comments than the marker and the dedupe line")
    if probe.pis:
        found.append("raw HTML processing instruction")
    if probe.decls:
        found.append("raw HTML declaration")
    # every piece of raw HTML the safe rendering omits must be the marker or the dedupe line
    allowed = [c for c in probe.comments if c.strip() in ALLOWED_COMMENTS]
    if safe_html.count("raw HTML omitted") != len(allowed):
        found.append("raw HTML omitted")

    suggestions = [(lang, body) for lang, body in safe.pres if is_suggestion_lang(lang)]
    if suggestions != [(lang, body) for lang, body in footnotes.pres if is_suggestion_lang(lang)]:
        found.append("footnotes change the applicable suggestions")
    written: str | None = None
    if expect.suggestion is None:
        if suggestions:
            found.append("an applicable suggestion pr-comment did not write")
    else:
        lines, info = expect.suggestion
        want = "".join(line + "\n" for line in lines)
        if len(suggestions) > 1 or any(s != (info, want) for s in suggestions):
            found.append("an applicable suggestion pr-comment did not write")
        if not suggestions:
            # swallowed by an open fence of the body: inert, and it must then show as code
            written = f"```{info}\n{want}"
            if not any(_unindented(written) in _unindented(n.text) for n in safe.nodes if n.in_pre):
                found.append(
                    "a suggestion pr-comment wrote is neither applicable nor shown as code"
                )

    for page_name, page in (("safe", safe), ("probe", probe), ("footnotes", footnotes)):
        if live_mentions(page, written):
            found.append(f"live mention ({page_name} rendering)")

    if expect.attribution is not None:
        prefix = render_safe(expect.attribution) + ("<hr />\n" if expect.has_rule else "")
        if not safe_html.startswith(prefix):
            found.append("the attribution line is not the first paragraph")
    if expect.prefix and not re.match(r"<p><strong>(?:(?!</p>).)*</strong></p>\n", safe_html, re.S):
        found.append("the bold prefix is not a paragraph of its own")

    source_markers = MARKER_TEXT.findall(text)
    shown = [m for n in safe.nodes if n.in_code for m in MARKER_TEXT.findall(n.text)]
    outside = [m for n in safe.nodes if not n.in_code for m in MARKER_TEXT.findall(n.text)]
    if len(shown) != len(source_markers) or outside:
        found.append("a marker of pr-comment is not visible in code")

    if not (text == MARKER or text.endswith("\n" + MARKER)) or not has_marker(text):
        found.append("the marker is not the last line")
    if forgectl_neutralize(text).text != text:
        found.append("forgectl would change the body")
    return found


def assert_safe(posted: Iterable[Posted], context: str = "") -> None:
    for item in posted:
        problems = violations(item.text, item.expect)
        assert not problems, f"{context} {item.label}: {problems}\n{item.text!r}"


# --- reproducers --------------------------------------------------------------------------------


def draft_json(document: Mapping[str, Any]) -> str:
    """A draft as ASCII JSON, so invisible characters stay visible in a golden or a failure."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True)


def render_report(posted: Sequence[Posted]) -> str:
    """The posted bodies and GitHub's engine's rendering of each, for a golden file."""
    out: list[str] = []
    for item in posted:
        out.append(f"-- {item.label} (neutralized_lines={item.body.neutralized_lines})")
        out.append(item.text)
        out.append("-- cmark-gfm")
        out.append(render_safe(item.text + "\n" + DEDUPE_LINE).rstrip("\n"))
    return "\n".join(out) + "\n"


def shrink(document: dict[str, Any], failing: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    """A smaller draft that still fails: whole findings, optional fields, then characters of each
    text field are dropped while ``failing`` stays true. Greedy, deterministic."""

    def paths(doc: Any, prefix: tuple[Any, ...] = ()) -> Iterable[tuple[Any, ...]]:
        if isinstance(doc, dict):
            for key, value in doc.items():  # pyright: ignore[reportUnknownVariableType]
                yield from paths(value, (*prefix, key))
        elif isinstance(doc, list):
            for index, value in enumerate(doc):  # pyright: ignore[reportUnknownVariableType]
                yield from paths(value, (*prefix, index))
        elif isinstance(doc, str):
            yield prefix

    def get(doc: Any, path: tuple[Any, ...]) -> Any:
        for key in path:
            doc = doc[key]
        return doc

    def with_value(doc: dict[str, Any], path: tuple[Any, ...], value: Any) -> dict[str, Any]:
        copy: Any = json.loads(json.dumps(doc))
        target = get(copy, path[:-1])
        target[path[-1]] = value
        return copy

    def without(doc: dict[str, Any], path: tuple[Any, ...]) -> dict[str, Any]:
        copy: Any = json.loads(json.dumps(doc))
        target = get(copy, path[:-1])
        del target[path[-1]]
        return copy

    def findings_of(doc: dict[str, Any]) -> Iterable[tuple[Any, ...]]:
        for index in range(len(doc["findings"])):
            yield ("findings", index)
        for index in range(len(doc.get("unanchored", []))):
            yield ("unanchored", index, "finding")

    current = document
    changed = True
    while changed:
        changed = False
        for container in ("findings", "unanchored"):
            index = len(current.get(container, [])) - 1
            while index >= 0:
                candidate = without(current, (container, index))
                if failing(candidate):
                    current, changed = candidate, True
                index -= 1
        for where in list(findings_of(current)):
            for key in ("severity", "title", "start_line", "side", "suggestion"):
                if key in get(current, where):
                    candidate = without(current, (*where, key))
                    if failing(candidate):
                        current, changed = candidate, True
            lines = get(current, where).get("suggestion", {}).get("lines", [])
            for index in range(len(lines) - 1, -1, -1):
                candidate = without(current, (*where, "suggestion", "lines", index))
                if failing(candidate):
                    current, changed = candidate, True
        for path in list(paths(current)):
            if path[-1] in ("id", "head_sha", "reason", "base_ref", "pr", "draft_id", "base_sha"):
                continue
            value = get(current, path)
            size = max(1, len(value) // 2)
            while size >= 1 and value:
                start = 0
                while start < len(value):
                    shorter = value[:start] + value[start + size :]
                    candidate = with_value(current, path, shorter)
                    if failing(candidate):
                        current, value, changed = candidate, shorter, True
                    else:
                        start += size
                size //= 2
    return current
