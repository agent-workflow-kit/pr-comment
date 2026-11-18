"""Finding text, paths, review body sections and moved findings (docs/interface.md: Finding text,
Review body assembly, Moved findings; issue #18)."""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from typing import Any

import cmarkgfm  # pyright: ignore[reportMissingTypeStubs]
import pytest
from cmarkgfm.cmark import Options  # pyright: ignore[reportMissingTypeStubs]

from pr_comment import render, textpipe
from pr_comment.mentions import MentionNeutralizer
from pr_comment.neutralize import count_parts
from pr_comment.render import (
    Anchor,
    MovedFinding,
    escape_title,
    finding_parts,
    last_known_anchor,
    render_finding_text,
    render_moved_entry,
    render_path_span,
    render_prefix,
    render_review_parts,
)

SHA_A = "0123456789abcdef0123456789abcdef01234567"
SHA_B = "fedcba9876543210fedcba9876543210fedcba98"


def mn(*allow: str) -> MentionNeutralizer:
    return MentionNeutralizer("neutralize", allow)


def finding(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"id": "f1", "path": "src/a.py", "line": 10, "body": "Body text."}
    base.update(overrides)
    return base


def cmark(markdown: str) -> str:
    return cmarkgfm.github_flavored_markdown_to_html(markdown)  # pyright: ignore[reportUnknownMemberType]


_CMARK_TAGS = {
    "p", "em", "strong", "a", "code", "pre", "ul", "ol", "li", "blockquote", "hr", "br", "del",
    "table", "thead", "tbody", "tr", "th", "td", "h1", "h2", "h3", "h4", "h5", "h6", "input",
    "sup", "section", "div",
}  # fmt: skip
_TAG_RE = re.compile(r"<\s*/?\s*([A-Za-z][A-Za-z0-9-]*)")
LIVE_MENTION = re.compile(r"(?<![A-Za-z0-9])@")


def cmark_unsafe(markdown: str) -> str:
    return cmarkgfm.github_flavored_markdown_to_html(  # pyright: ignore[reportUnknownMemberType]
        markdown, options=Options.CMARK_OPT_UNSAFE
    )


def assert_no_raw_html_image_or_mention(body: str) -> None:
    unsafe = cmark_unsafe(body)
    assert "<img" not in unsafe.lower()
    assert "<!--" not in unsafe
    alien = {m.group(1).lower() for m in _TAG_RE.finditer(unsafe)} - _CMARK_TAGS
    assert alien == set()
    assert "raw HTML omitted" not in cmark(body)
    text = html.unescape(re.sub(r"<[^>]*>", "", cmark(body)))
    assert not LIVE_MENTION.search(text)


# --- bold prefix ---------------------------------------------------------------------------

PREFIX_CASES = [
    ({"severity": "major", "title": "Missing check"}, r"**major · Missing check**"),
    ({"severity": "nit"}, "**nit**"),
    ({"title": "Only a title"}, r"**Only a title**"),
    ({"severity": "info", "title": "  padded \t "}, "**info · padded**"),
    ({"title": "a.b"}, r"**a\.b**"),
    ({"title": "x"}, "**x**"),
]


@pytest.mark.parametrize(("fields", "expected"), PREFIX_CASES)
def test_bold_prefix(fields: dict[str, Any], expected: str) -> None:
    assert render_prefix(finding(**fields), mn()) == expected


@pytest.mark.parametrize("title", ["", "   ", "\t 　 "])
def test_empty_or_blank_title_counts_as_absent(title: str) -> None:
    assert render_prefix(finding(title=title), mn()) is None
    assert render_prefix(finding(severity="minor", title=title), mn()) == "**minor**"


@pytest.mark.parametrize("sep", [" ", "\n", "\r", "\x1c"])
@pytest.mark.parametrize("shape", ["{}a", "a{}", "{}"])
def test_a_title_with_a_leading_or_trailing_separator_is_refused(sep: str, shape: str) -> None:
    with pytest.raises(ValueError):
        render_prefix(finding(title=shape.format(sep)), mn())


@pytest.mark.parametrize("path", [" ", "\t", "  \t "])
def test_a_whitespace_only_path_is_refused(path: str) -> None:
    with pytest.raises(ValueError):
        render_path_span(path, mn())


def test_no_severity_and_no_title_has_no_prefix() -> None:
    assert render_prefix(finding(), mn()) is None
    assert render_prefix(finding(title=None), mn()) is None


def test_every_ascii_punctuation_except_at_is_escaped() -> None:
    punctuation = "".join(chr(c) for c in range(0x21, 0x7F) if not chr(c).isalnum())
    out = escape_title(punctuation)
    # one escape per character but `@`, plus the backslash character itself
    assert out.count("\\") == len(punctuation)
    assert "@" in out
    assert "\\@" not in out
    for ch in punctuation:
        assert (ch == "@") or f"\\{ch}" in out


def test_title_letters_digits_space_and_non_ascii_are_not_escaped() -> None:
    assert escape_title("Abc 123 é ＠ ＜ ！ ·") == "Abc 123 é ＠ ＜ ！ ·"


HOSTILE_TITLES = [
    "**",
    "** x **",
    "# heading",
    "- item",
    "1. item",
    "> quote",
    "| a | b |",
    "---",
    "***",
    "___",
    "`code`",
    "``` fence",
    "~~~ fence",
    "[x](https://t.example/u)",
    "[x]: /u",
    "![i](https://t.example/p.png)",
    "![i][r]",
    "<b>x</b>",
    "<!-- c -->",
    "<img src=https://t.example/p>",
    "<https://t.example>",
    "&lt;b&gt; &#60;b&#62; &amp;",
    "&#64;octocat &#x40;octocat &commat;octocat &#0064;octocat",
    "@octocat @all @org/team \\@octocat",
    "\\",
    "\\\\",
    "\\*\\*",
    "x\\",
    "www.example.com",
    "https://t.example/u",
    "~~strike~~",
    "*em* _em_ __strong__",
    "‮ rtl ​ zw",
    "é ünï ＜ ！[ ",
    "a" * 256,
    "*" * 256,
    "\\" * 255,
    "`" * 256,
]


@pytest.mark.parametrize("title", HOSTILE_TITLES)
def test_a_hostile_title_stays_inside_one_bold_paragraph(title: str) -> None:
    prefix = render_prefix(finding(severity="major", title=title), mn())
    assert prefix is not None
    assert "\n" not in prefix
    page = cmark(prefix)
    assert re.fullmatch(r"<p><strong>major · [^<]*</strong></p>\n", page), page
    assert_no_raw_html_image_or_mention(prefix)


@pytest.mark.parametrize("title", HOSTILE_TITLES)
def test_a_hostile_title_alone_stays_inside_one_bold_paragraph(title: str) -> None:
    prefix = render_prefix(finding(title=title), mn())
    if prefix is None:
        pytest.skip("empty title")
    page = cmark(prefix)
    assert re.fullmatch(r"<p><strong>[^<]*</strong></p>\n", page), page
    assert_no_raw_html_image_or_mention(prefix)


def test_an_email_address_in_a_title_may_still_autolink_as_documented() -> None:
    # `@` is not escaped (interface: Cost, e-mail addresses are unchanged); counts.links reports it.
    page = cmark(render_prefix(finding(title="a@b.example"), mn()) or "")
    assert '<a href="mailto:a@b.example">' in page
    assert "<img" not in page


def test_the_title_oracle_can_fail_without_escaping() -> None:
    page = cmark("**major · # heading **bold** [x](http://t.example)**")
    assert not re.fullmatch(r"<p><strong>major · [^<]*</strong></p>\n", page)


def test_title_text_survives_in_the_rendering() -> None:
    prefix = render_prefix(finding(severity="major", title="a*b_c[d](e)#f<g>&h"), mn())
    assert prefix is not None
    text = html.unescape(re.sub(r"<[^>]*>", "", cmark(prefix)))
    assert text.strip() == "major · a*b_c[d](e)#f＜g>&h"


def test_title_with_a_backslash_n_text_stays_in_the_prefix() -> None:
    prefix = render_prefix(finding(severity="minor", title="a\\nb\\n\\n# c"), mn())
    assert prefix == "**minor · a\\\\nb\\\\n\\\\n\\# c**"
    assert "\n" not in prefix


@pytest.mark.parametrize(
    "title", ["a\nb", "a\rb", "a\x0bb", "a\x0cb", "a\x1cb", "a\x85b", "a\u2028b", "a b"]
)
def test_a_title_with_a_line_break_is_refused(title: str) -> None:
    with pytest.raises(ValueError, match="one line"):
        render_prefix(finding(title=title), mn())


@pytest.mark.parametrize("severity", ["", "MAJOR", "major\n", "x", "**", None.__class__])
def test_an_unknown_severity_is_refused(severity: Any) -> None:
    with pytest.raises(ValueError, match="severity"):
        render_prefix(finding(severity=severity), mn())


@pytest.mark.parametrize("bad", [1, b"x", ["x"], {"a": 1}])
def test_a_non_string_title_is_a_type_error(bad: Any) -> None:
    with pytest.raises(TypeError):
        render_prefix(finding(title=bad), mn())


def test_title_mentions_and_characters_are_neutralized_before_escaping() -> None:
    mentions = mn()
    out = render_prefix(finding(title="hi @octocat <b> ![x](u) &#64;y"), mentions)
    assert out == "**hi ＠octocat ＜b\\> ！\\[x\\]\\(u\\) ＠y**"
    assert mentions.count == 2
    assert mentions.names == ("octocat", "y")


def test_an_allowed_mention_in_a_title_stays_and_is_not_escaped() -> None:
    assert render_prefix(finding(title="ask @octocat now"), mn("octocat")) == "**ask @octocat now**"


def test_an_escaped_at_in_a_title_is_neutralized_not_left_live() -> None:
    out = render_prefix(finding(title="\\@octocat"), mn())
    assert out == "**\\\\＠octocat**"


def test_title_with_mention_policy_allow_keeps_at() -> None:
    out = render_prefix(finding(title="@octocat"), MentionNeutralizer("allow"))
    assert out == "**@octocat**"


# --- path span -------------------------------------------------------------------------------


def test_path_span_plain() -> None:
    assert render_path_span("src/a.py", mn()) == "` src/a.py `"


def test_path_span_with_a_line_a_range_and_left() -> None:
    assert render_path_span("src/a.py", mn(), line=10) == "` src/a.py:10 `"
    assert render_path_span("src/a.py", mn(), line=12, start_line=10) == "` src/a.py:10-12 `"
    assert render_path_span("src/a.py", mn(), line=3, side="left") == "` src/a.py:3 ` (left)"
    assert render_path_span("src/a.py", mn(), side="left") == "` src/a.py ` (left)"
    assert render_path_span("src/a.py", mn(), side="right") == "` src/a.py `"


@pytest.mark.parametrize(
    ("path", "fence"),
    [
        ("a`b", "``"),
        ("a``b", "```"),
        ("`", "``"),
        ("a`b``c```d", "````"),
        ("`" * 40, "`" * 41),
    ],
)
def test_the_span_uses_one_more_backtick_than_the_longest_run(path: str, fence: str) -> None:
    out = render_path_span(path, mn(), line=1)
    assert out == f"{fence} {path}:1 {fence}"


@pytest.mark.parametrize(
    "path",
    [
        "a`b",
        "`x`",
        "``` ``",
        "`[x](https://t.example/u)`",
        "[x](u)",
        "x`[x](u)`y",
        "![i](u)",
        "<img src=x>",
        "a b  c",
        " lead",
        "trail ",
        "back\\slash\\",
        "tab\there",
        "@octocat/@all",
        "#h|*_~",
        "‮​⁠x",
    ],
)
def test_a_path_renders_as_one_code_span_with_nothing_live(path: str) -> None:
    span = render_path_span(path, mn(), line=3, side="left")
    page = cmark(span)
    assert re.fullmatch(r"<p><code>[^<]*</code> \(left\)</p>\n", page), page
    assert page.startswith("<p><code>")
    assert page.count("<code>") == 1
    for tag in ("<a ", "<img", "<h1", "<ul", "<blockquote", "<em>", "<strong>"):
        assert tag not in page
    assert_no_raw_html_image_or_mention(span)


def test_path_with_backticks_and_a_link_is_a_golden(golden: Callable[[str, str], None]) -> None:
    paths = ["src/`x`/[x](u).py", "a``b[x](u)", "docs/![i](u).md"]
    lines = [render_path_span(p, mn(), line=2) for p in paths]
    golden("render/path_backticks_and_link.txt", "\n\n".join(lines) + "\n")


def test_path_with_bidi_and_zero_width_has_markers_in_the_span(
    golden: Callable[[str, str], None],
) -> None:
    out = render_path_span("src/a‮b​c.py", mn(), line=7)
    assert out == "` src/a<U+202E>b<U+200B>c.py:7 `"
    golden("render/path_markers.txt", out + "\n")


def test_path_markers_cover_forgectl_sanitizing_including_tab() -> None:
    out = render_path_span("a\tb⁠c️d⠀ef\x00".replace("\x00", ""), mn())
    assert out == "` a<U+0009>b<U+2060>c<U+FE0F>d<U+2800>e<U+E000>f `"


def test_a_literal_marker_in_a_path_is_distinguishable_from_a_real_one() -> None:
    out = render_path_span("a<U+202E>b‮", mn())
    assert out == "` a＜U+202E>b<U+202E> `"


def test_path_neutralizes_lt_bang_bracket_and_mentions() -> None:
    mentions = mn()
    out = render_path_span("a/@types/![x](u)/<b>/&#64;q", mentions)
    assert out == "` a/＠types/！[x](u)/＜b>/＠q `"
    assert mentions.count == 2


def test_an_allowed_mention_in_a_path_stays() -> None:
    assert render_path_span("a/@octocat", mn("octocat")) == "` a/@octocat `"


@pytest.mark.parametrize("path", ["a\nb", "a\rb", "a\x0bb", "a\u2028b", "a\u0085b"])
def test_a_path_with_a_line_break_is_refused(path: str) -> None:
    with pytest.raises(ValueError, match="one line"):
        render_path_span(path, mn())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"line": 0},
        {"line": -1},
        {"line": True},
        {"line": 2, "start_line": 2},
        {"line": 2, "start_line": 3},
        {"start_line": 1},
        {"line": 2, "start_line": 0},
        {"side": "both"},
        {"line": "2"},
    ],
)
def test_bad_position_values_are_refused(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        render_path_span("a", mn(), **kwargs)


def test_a_huge_path_renders() -> None:
    path = "`" * 2000 + "a" * 2000
    out = render_path_span(path, mn(), line=1)
    assert out.startswith("`" * 2001 + " ")
    assert out.endswith(" " + "`" * 2001)


# --- finding text ----------------------------------------------------------------------------


def test_finding_text_is_prefix_blank_line_body() -> None:
    out = render_finding_text(
        finding(severity="major", title="T", body="Line one.\nLine two."), mn()
    )
    assert out == "**major · T**\n\nLine one.\nLine two."


def test_finding_text_without_prefix_is_just_the_body() -> None:
    assert render_finding_text(finding(body="Only body."), mn()) == "Only body."


def test_finding_text_appends_a_given_suggestion_block_after_a_blank_line() -> None:
    block = "```suggestion\nx\n```"
    out = render_finding_text(finding(severity="nit", body="b"), mn(), suggestion_block=block)
    assert out == "**nit**\n\nb\n\n" + block


def test_body_line_separators_become_lf() -> None:
    body = "a\r\nb\rc\x0bd\x0ce\x1cf\x1dg\x1eh\x85i\u2028j k"
    out = render_finding_text(finding(body=body), mn())
    assert out == "a\nb\nc\nd\ne\nf\ng\nh\ni\nj\nk"


def test_body_mentions_and_characters_are_neutralized() -> None:
    mentions = mn()
    out = render_finding_text(finding(body="hi @all <b>x</b> ![i](u) &lt;ok&gt;"), mentions)
    assert out == "hi ＠all ＜b>x＜/b> ！[i](u) &lt;ok&gt;"
    assert mentions.count == 1


def test_a_body_with_hostile_markdown_is_kept_but_neutralized() -> None:
    body = "```\n<details>\n![](u)\n@all\n"
    out = render_finding_text(finding(body=body), mn())
    assert out == "```\n＜details>\n！[](u)\n＠all\n"


def test_body_is_neutralized_exactly_once() -> None:
    mentions = mn()
    render_finding_text(finding(body="@a @b"), mentions)
    assert mentions.count == 2


def test_parts_are_neutralized_in_title_then_body_order() -> None:
    mentions = mn()
    render_finding_text(finding(title="@tt", body="@bb"), mentions)
    assert mentions.names == ("tt", "bb")


def test_finding_text_does_not_mutate_the_finding() -> None:
    f = finding(title=" @t ", body="a\r\nb")
    before = dict(f)
    render_finding_text(f, mn())
    assert f == before


def test_the_title_and_body_do_not_run_together() -> None:
    # a title ending in `!` and a body starting with `[x](u)` must not form an image
    out = render_finding_text(finding(title="Look!", body="[x](https://t.example/p.png)"), mn())
    assert "<img" not in cmark_unsafe(out)


def test_a_suggestion_block_is_not_neutralized_again() -> None:
    block = "```suggestion\nif a < b: @x\n```"
    out = render_finding_text(finding(body="b"), mn(), suggestion_block=block)
    assert out.endswith(block)


# --- finding parts (for counts) --------------------------------------------------------------


def test_finding_parts_lists_every_untrusted_part_for_the_counts() -> None:
    f = finding(
        title="T <b>",
        path="p/![x](u)",
        body="a\r\nb",
        suggestion={"lines": ["x", "<i>"]},
    )
    assert finding_parts(f) == ["T <b>", "p/![x](u)", "a\nb", "x", "<i>"]
    counts = count_parts(finding_parts(f))
    assert counts.raw_html == 2
    assert counts.images == 1


def test_finding_parts_skips_absent_fields() -> None:
    assert finding_parts(finding()) == ["src/a.py", "Body text."]


# --- moved findings ----------------------------------------------------------------------------

REASONS = {
    "changed": "the commented line changed after this review was drafted",
    "file_removed": "the file was deleted or renamed after this review was drafted",
    "diff_unavailable": "the changes since this review was drafted could not be compared",
    "base_changed": "the base branch changed after this review was drafted",
    "invalid_position": "the line is not part of this pull request's diff",
    "files_incomplete": "the host's file list for this pull request is incomplete",
}


def test_reason_texts_are_the_interface_table() -> None:
    assert render.REASON_TEXT == REASONS


@pytest.mark.parametrize("reason", list(REASONS))
def test_moved_entry_line_for_each_reason_golden(
    reason: str, golden: Callable[[str, str], None]
) -> None:
    moved = MovedFinding(
        finding(severity="minor", title="Reason " + reason, body="Moved body."),
        reason,
        Anchor(SHA_A, 10, None),
    )
    out = render_moved_entry(moved, mn())
    assert out.startswith(f"` src/a.py:10 ` (at 0123456789ab) — {REASONS[reason]}\n\n**minor")
    golden(f"render/moved_{reason}.txt", out + "\n")


def test_the_interface_example_entry_line() -> None:
    moved = MovedFinding(
        finding(path="src/a.py", line=12, start_line=10, side="left", body="b"),
        "base_changed",
        Anchor(SHA_A, 12, 10),
    )
    first = render_moved_entry(moved, mn()).split("\n")[0]
    assert first == (
        "` src/a.py:10-12 ` (left) (at 0123456789ab) — the base branch changed after this review "
        "was drafted"
    )


def test_the_entry_shows_the_anchors_line_not_the_drafts() -> None:
    moved = MovedFinding(
        finding(line=10, start_line=8, body="b"), "invalid_position", Anchor(SHA_B, 14, 12)
    )
    assert render_moved_entry(moved, mn()).startswith("` src/a.py:12-14 ` (at fedcba987654) —")


def test_a_file_level_moved_entry_has_no_line() -> None:
    moved = MovedFinding(
        finding(line=None, body="b"), "files_incomplete", Anchor(SHA_A, None, None)
    )
    assert render_moved_entry(moved, mn()).startswith("` src/a.py ` (at 0123456789ab) —")


def test_moved_entry_with_a_suggestion_block_puts_it_last() -> None:
    block = (
        "_Suggestion shown as code: this finding could not be posted inline._\n\n```text\nx\n```"
    )
    moved = MovedFinding(finding(body="b"), "changed", Anchor(SHA_A, 10, None), block)
    assert render_moved_entry(moved, mn()).endswith("\n\nb\n\n" + block)


def test_an_unknown_reason_is_refused() -> None:
    with pytest.raises(ValueError, match="reason"):
        render_moved_entry(MovedFinding(finding(), "nope", Anchor(SHA_A, 1, None)), mn())


@pytest.mark.parametrize(
    "sha", ["", "abc", SHA_A.upper(), SHA_A + "0", "g" * 40, SHA_A[:-1] + "\n"]
)
def test_a_bad_sha_is_refused(sha: str) -> None:
    with pytest.raises(ValueError, match="sha"):
        render_moved_entry(MovedFinding(finding(), "changed", Anchor(sha, 1, None)), mn())


def test_last_known_anchor_rule() -> None:
    a = Anchor(SHA_A, 10, None)
    b = Anchor(SHA_B, 14, 12)
    assert last_known_anchor(a, b, failed_validation_at_b=True) is b
    assert last_known_anchor(a, b, failed_validation_at_b=False) is a
    assert last_known_anchor(a, None, failed_validation_at_b=False) is a
    assert last_known_anchor(a, None, failed_validation_at_b=True) is a


def test_moved_entry_with_hostile_text_is_neutralized_once_in_order() -> None:
    mentions = mn()
    moved = MovedFinding(
        finding(path="@p/<x>", title="@t", body="@b <i>"), "changed", Anchor(SHA_A, 1, None)
    )
    out = render_moved_entry(moved, mentions)
    assert "@" not in out
    assert "<i>" not in out
    assert mentions.names == ("p", "t", "b")
    assert mentions.count == 3


# --- review body parts -------------------------------------------------------------------------


def test_no_parts_when_everything_is_empty() -> None:
    assert render_review_parts("", [], [], mn()) == []


def test_each_part_only_when_non_empty() -> None:
    assert render_review_parts("Summary.", [], [], mn()) == ["Summary."]
    only_file = render_review_parts("", [finding(line=None, body="fb")], [], mn())
    assert only_file == ["### File-level findings\n\n` src/a.py `\n\nfb"]
    moved = MovedFinding(finding(body="mb"), "changed", Anchor(SHA_A, 10, None))
    only_moved = render_review_parts("", [], [moved], mn())
    assert len(only_moved) == 1
    assert only_moved[0].startswith(
        "### Findings that could not be anchored\n\n` src/a.py:10 ` (at"
    )


def test_part_order_is_body_then_file_level_then_moved() -> None:
    moved = MovedFinding(finding(body="mb"), "changed", Anchor(SHA_A, 10, None))
    parts = render_review_parts("Summary.", [finding(line=None, body="fb")], [moved], mn())
    assert [p.split("\n")[0] for p in parts] == [
        "Summary.",
        "### File-level findings",
        "### Findings that could not be anchored",
    ]


def test_entries_keep_draft_order_in_each_section() -> None:
    files = [finding(id=f"f{i}", path=f"p{i}.py", line=None, body=f"b{i}") for i in (3, 1, 2)]
    moved = [
        MovedFinding(
            finding(id=f"m{i}", path=f"q{i}.py", body=f"c{i}"), "changed", Anchor(SHA_A, 1, None)
        )
        for i in (2, 3, 1)
    ]
    parts = render_review_parts("", files, moved, mn())
    assert re.findall(r"` (p\d)\.py `", parts[0]) == ["p3", "p1", "p2"]
    assert re.findall(r"` (q\d)\.py:1 `", parts[1]) == ["q2", "q3", "q1"]


def test_entries_in_a_section_are_separated_by_a_blank_line() -> None:
    files = [finding(path="a.py", line=None, body="x"), finding(path="b.py", line=None, body="y")]
    (part,) = render_review_parts("", files, [], mn())
    assert part == "### File-level findings\n\n` a.py `\n\nx\n\n` b.py `\n\ny"


def test_the_draft_body_is_normalized_and_neutralized() -> None:
    mentions = mn()
    (part,) = render_review_parts("a\r\n@all\u2028<b>", [], [], mentions)
    assert part == "a\n＠all\n＜b>"
    assert mentions.count == 1


def test_review_parts_neutralize_the_body_before_the_sections() -> None:
    mentions = mn()
    render_review_parts("@one", [finding(line=None, body="@two")], [], mentions)
    assert mentions.names == ("one", "two")


def test_parts_join_with_a_newline_so_a_bang_and_a_link_never_touch() -> None:
    # `!` ends the draft body, `[x](u)` opens the first entry: the sections are separate parts.
    parts = render_review_parts(
        "Danger!", [finding(line=None, body="[x](https://t.example/i.png)")], [], mn()
    )
    joined = "\n\n".join(parts)
    assert "<img" not in cmark_unsafe(joined)
    assert parts[0].endswith("!")
    assert not parts[1].startswith("[")


def test_mention_allow_does_not_run_parts_together() -> None:
    parts = render_review_parts("@octocat", [finding(line=None, body="-x")], [], mn("octocat"))
    assert parts[0] == "@octocat"


# --- the whole body renders with nothing live --------------------------------------------------

HOSTILE_BODIES = [
    "```\nopen fence <details>",
    "~~~\n@all\n~~~",
    "<!-- c -->\n<img src=https://t.example/p>",
    "![](https://t.example/p.png)\n![t][r]\n\n[r]: https://t.example/p.png",
    "[r]: /u\n    <details>x</details>\n    <img src=u>",
    "> x\n\t> ```\n@victim",
    "&#64;victim &commat;victim @victim \\@victim @&#111;ctocat",
    "| a |\n|---|\n| b |\n|\n    @victim",
    "# heading\n- list\n1. one\n---",
    "<script>alert(1)</script>",
    "‮​\u2028 @victim",
    "x" * 70000,
    "@a " * 20000,
    "![" * 20000,
    "<" * 20000,
]


@pytest.mark.parametrize("hostile", HOSTILE_BODIES)
def test_a_whole_review_body_with_hostile_text_renders_nothing_live(hostile: str) -> None:
    first = textpipe.normalize_lines(hostile).split("\n")[0]
    f = finding(
        severity="blocker",
        title=first[:256],
        path="p/" + first[:100],
        body=hostile,
    )
    moved = MovedFinding(f, "changed", Anchor(SHA_A, 3, None))
    parts = render_review_parts(hostile, [dict(f, line=None)], [moved], mn())
    body = "\n\n".join(parts)
    # The text after an untrusted part can be pulled into a block it left open (documented), but
    # no raw HTML, image or live mention appears anywhere.
    assert_no_raw_html_image_or_mention(body)


def test_the_oracle_can_fail_on_the_raw_text() -> None:
    with pytest.raises(AssertionError):
        assert_no_raw_html_image_or_mention("<span>x</span>")
    with pytest.raises(AssertionError):
        assert_no_raw_html_image_or_mention("![](u)")
    with pytest.raises(AssertionError):
        assert_no_raw_html_image_or_mention("@victim")


def test_the_sections_render_as_headings_when_the_draft_body_is_closed() -> None:
    parts = render_review_parts("Closed summary.", [finding(line=None, body="fb")], [], mn())
    page = cmark("\n\n".join(parts))
    assert "<h3>File-level findings</h3>" in page
    assert page.count("<code>src/a.py</code>") == 1


def test_a_title_cannot_start_a_heading_or_list_in_an_entry() -> None:
    f = finding(line=None, severity="major", title="# h\\n- l", body="b")
    (part,) = render_review_parts("", [f], [], mn())
    page = cmark(part)
    assert page.count("<h3>") == 1
    assert "<ul>" not in page
    assert "<h1>" not in page


def test_many_findings_render() -> None:
    files = [finding(id=f"f{i}", line=None, title=f"t{i}", body=f"body {i}") for i in range(500)]
    (part,) = render_review_parts("", files, [], mn())
    assert part.count("### ") == 1
    assert part.count("` src/a.py `") == 500


def test_counts_over_finding_parts_see_what_the_input_tried() -> None:
    f = finding(title="<b>", path="p/‮", body="![x](u) www.e.com", suggestion={"lines": ["<i>"]})
    counts = count_parts(["draft <body>", *finding_parts(f)])
    assert counts.raw_html == 3
    assert counts.images == 1
    assert counts.invisible == 1
    assert counts.links >= 1


def test_title_with_stars_and_backslash_n_text_is_a_golden(
    golden: Callable[[str, str], None],
) -> None:
    titles = ["a ** b **", "line one\\nline two\\n# not a heading", "**bold** `tick` [l](u) <b>"]
    out = [
        render_finding_text(finding(severity="major", title=t, body="Body."), mn()) for t in titles
    ]
    golden("render/titles.txt", "\n\n---\n\n".join(out) + "\n")


def test_a_review_body_is_a_golden(golden: Callable[[str, str], None]) -> None:
    moved = [
        MovedFinding(
            finding(
                id="m1", path="src/b`.py", line=5, start_line=3, side="left", body="Moved <b>."
            ),
            "changed",
            Anchor(SHA_B, 5, 3),
        )
    ]
    files = [
        finding(id="f1", path="docs/@guide.md", line=None, severity="info", body="See ![x](u).")
    ]
    parts = render_review_parts("Summary for @someone.", files, moved, mn())
    golden("render/review_parts.txt", "\n\n".join(parts) + "\n")
