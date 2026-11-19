"""Character neutralizing and body counts (docs/interface.md: Text pipeline step 3, Body structure).

The inputs are hostile on purpose: raw HTML and comments, entities, image openers in every form,
reference definitions split across parts, lookalike characters, huge and quadratic-shaped texts.
GitHub's own engine (``cmarkgfm``, pinned) is the oracle for what survives.
"""

from __future__ import annotations

import random
import re
import time

import cmarkgfm
import pytest
from cmarkgfm.cmark import Options

from pr_comment.neutralize import (
    TextCounts,
    count_parts,
    count_text,
    neutralize_characters,
    neutralize_suggestion_lines,
)

# --- the character pass ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<!-- x -->", "＜!-- x -->"),
        ("<details>", "＜details>"),
        ("<img src=x onerror=y>", "＜img src=x onerror=y>"),
        ("![](u)", "！[](u)"),
        ("![t][r]", "！[t][r]"),
        ("<<a", "＜＜a"),
        ("!![", "!！["),
        ("a!", "a!"),
        ("! [x]", "! [x]"),
        ("[x]!", "[x]!"),
        ("&lt;img&gt; &#60;img &#x3c;img", "&lt;img&gt; &#60;img &#x3c;img"),
        ("＜img ！[", "＜img ！["),
        ("", ""),
        ("plain text", "plain text"),
    ],
)
def test_character_pass_table(raw: str, expected: str) -> None:
    assert neutralize_characters(raw) == expected


def test_character_references_are_left_as_they_are() -> None:
    text = "&lt;script&gt; &amp;#60; &#33;[x] &excl;[x]"
    assert neutralize_characters(text) == text


HOSTILE = [
    "<!--",
    "<!-- x -->\n<details><summary>s</summary>x</details>",
    "<IMG SRC=x>",
    "<ImG\tsrc=x>",
    "<img/src=x onerror=alert(1)>",
    "<img\nsrc=x>",
    "<script>alert(1)</script>",
    '<a href="javascript:alert(1)">x</a>',
    "<https://evil.example/x>",
    "<mailto:a@b.co>",
    "<a@b.co>",
    "<?php x ?>",
    "<![CDATA[x]]>",
    "<!DOCTYPE html>",
    "</div>",
    "![](u)",
    "![alt](https://t.example/pic)",
    "![t][r]",
    "![t][]",
    "![t]",
    "!![](u)",
    "![[x]](u)",
    "[r]: /u\n    <details>",
    "[r]: /u\n\n![t][r]",
    "[r]: https://t.example/pic\n![r]",
    "> <img src=x>",
    "- <img src=x>",
    "1. <details>x",
    "| a | b |\n|---|---|\n| <img src=x> | ![](u) |",
    "```\n<img src=x>\n```",
    "`<img src=x>`",
    "    <img src=x>",
    "\t<details>",
    "<img src=x>" * 50,
    "<<<<img src=x>>>>",
    "<​img src=x>",
    "<‮img src=x>",
    "!​[](u)",
    "&lt;img src=x&gt;",
    "&#60;img src=x&#62;",
    "&#x3C;img src=x>",
    "!&#91;](u)",
    "&#33;[](u)",
    "\\<img src=x>",
    "\\![](u)",
    "<\\img src=x>",
    "<br>\n<hr>",
    "www.example.org <b>x</b>",
    "[x](javascript:alert(1))",
    "[x]: javascript:alert(1)\n[x]",
    "<!---->",
    "<!-->",
    "<!--->",
]

# Tags cmark itself writes. Anything else in the unsafe rendering is raw HTML a part carried in.
_CMARK_TAGS = {
    "p", "em", "strong", "a", "code", "pre", "ul", "ol", "li", "blockquote", "hr", "br", "del",
    "table", "thead", "tbody", "tr", "th", "td", "h1", "h2", "h3", "h4", "h5", "h6", "input",
    "sup", "section", "div",
}  # fmt: skip
_TAG_RE = re.compile(r"<\s*/?\s*([A-Za-z][A-Za-z0-9-]*)")
_UNSAFE = Options.CMARK_OPT_UNSAFE


def _alien_tags(parts: list[str]) -> list[str]:
    """Tags in the unsafe rendering of ``parts`` (blank-line separated) that cmark did not write."""
    html_out = cmarkgfm.github_flavored_markdown_to_html("\n\n".join(parts), options=_UNSAFE)
    if "<img" in html_out.lower():
        return ["img"]
    return sorted({m.group(1).lower() for m in _TAG_RE.finditer(html_out)} - _CMARK_TAGS)


def _html_comments(parts: list[str]) -> bool:
    html_out = cmarkgfm.github_flavored_markdown_to_html("\n\n".join(parts), options=_UNSAFE)
    return "<!--" in html_out


def _safe_render_omits(parts: list[str]) -> bool:
    out = cmarkgfm.github_flavored_markdown_to_html("\n\n".join(parts))
    return "raw HTML omitted" in out


@pytest.mark.parametrize("raw", HOSTILE)
def test_neutralized_text_has_no_ascii_lt_and_no_image_opener(raw: str) -> None:
    out = neutralize_characters(raw)
    assert "<" not in out
    assert "![" not in out


@pytest.mark.parametrize("raw", HOSTILE)
def test_neutralized_text_renders_no_raw_html_or_image(raw: str) -> None:
    out = neutralize_characters(raw)
    assert _alien_tags([out]) == []
    assert not _html_comments([out])
    assert not _safe_render_omits([out])


def test_the_oracle_can_fail_on_unneutralized_text() -> None:
    # (GitHub's tagfilter already escapes <script>, <iframe>, <style> and a few more.)
    assert _alien_tags(["<span>x</span>"]) == ["span"]
    assert _alien_tags(["![](u)"]) == ["img"]
    assert _alien_tags(["<details>x</details>"]) == ["details"]
    assert _html_comments(["<!-- x -->"])
    assert _safe_render_omits(["<span>x</span>"])


def test_reference_image_defined_in_another_part_stays_text() -> None:
    parts = [neutralize_characters(p) for p in ["[r]: https://t.example/pic", "![t][r]"]]
    assert _alien_tags(parts) == []


def test_indented_tag_after_a_reference_definition_stays_text() -> None:
    parts = [neutralize_characters(p) for p in ["[r]: /u\n  <details>x", "<img src=x>"]]
    assert _alien_tags(parts) == []
    assert not _safe_render_omits(parts)


def test_golden_bodies(golden) -> None:  # type: ignore[no-untyped-def]
    cases = {
        "html_comment": "<!-- hidden -->",
        "details": "<details><summary>s</summary>x</details>",
        "img": '<img src="https://t.example/pic">',
        "image": "![](https://t.example/pic)",
        "reference_image": "![t][r]",
        "reference_definition": "[r]: /u\n    <details>",
    }
    lines = []
    for name, raw in cases.items():
        lines.append(f"== {name}\n{neutralize_characters(raw)}\n")
    golden("neutralize/character_pass.txt", "\n".join(lines))


def test_differential_fuzz_over_a_token_alphabet() -> None:
    tokens = [
        "<", "<!--", "<img src=x>", "<details>", "</p>", "![", "!", "[", "]", "(", ")", "[r]: /u",
        "![t][r]", "&lt;", "&#60;", "&#33;", "\\", "`", "```", "~~~", "> ", "- ", "1. ", "| a |",
        "|---|", "    ", "\t", "\n", "\n\n", " ", "x", "www.x.org", "https://x.example/a",
        "<U+202E>", "＜", "！", "<a href=x>", "<!--", "-->", "&", ";",
    ]  # fmt: skip
    rng = random.Random(20260617)
    for _ in range(1500):
        parts = [
            neutralize_characters("".join(rng.choice(tokens) for _ in range(rng.randint(1, 14))))
            for _ in range(rng.randint(1, 3))
        ]
        assert _alien_tags(parts) == [], parts
        assert not _html_comments(parts), parts
        assert not _safe_render_omits(parts), parts


def test_character_pass_is_idempotent() -> None:
    for raw in HOSTILE:
        once = neutralize_characters(raw)
        assert neutralize_characters(once) == once


def test_a_literal_marker_typed_by_an_attacker_cannot_stay_ascii() -> None:
    # pr-comment's own markers are inserted after this pass, so only they contain an ASCII "<".
    assert neutralize_characters("<U+202E>") == "＜U+202E>"


def test_only_lt_and_bang_before_bracket_change() -> None:
    raw = "abc é 😀 \t\n @x `y` [z](u) !! ! x"
    assert neutralize_characters(raw) == raw


# --- applicable suggestion lines ------------------------------------------------------------


def test_applicable_suggestion_lines_are_unchanged() -> None:
    lines = ["a < b", "![x](y)", "<div>", "x = '<'"]
    assert neutralize_suggestion_lines(lines, applicable=True) == lines


def test_degraded_or_moved_suggestion_lines_are_neutralized() -> None:
    lines = ["a < b", "![x](y)", "<div>"]
    assert neutralize_suggestion_lines(lines, applicable=False) == [
        "a ＜ b",
        "！[x](y)",
        "＜div>",
    ]


def test_suggestion_lines_result_is_a_new_list() -> None:
    lines = ["a"]
    assert neutralize_suggestion_lines(lines, applicable=True) is not lines


# --- counts ---------------------------------------------------------------------------------


def _c(text: str) -> TextCounts:
    return count_text(text)


@pytest.mark.parametrize(
    ("text", "raw_html"),
    [
        ("", 0),
        ("<a", 1),
        ("<A", 1),
        ("</x", 1),
        ("<!-- x -->", 1),
        ("<?php", 1),
        ("<!DOCTYPE", 1),
        ("<<a", 1),
        ("<a<b", 2),
        ("< a", 0),
        ("<1", 0),
        ("<", 0),
        ("a <", 0),
        ("<-", 0),
        ("<​a", 0),
        ("＜a", 0),
        ("&lt;a", 0),
        ("&#60;a", 0),
        ("`<a>`", 1),
        ("```\n<a>\n```", 1),
        ("<a><b></b><!-- --><?x", 4 + 1),
        ("<é", 0),
    ],
)
def test_raw_html_count(text: str, raw_html: int) -> None:
    assert _c(text).raw_html == raw_html


@pytest.mark.parametrize(
    ("text", "images"),
    [
        ("![](u)", 1),
        ("![t][r]", 1),
        ("![a](u)![b](v)", 2),
        ("!![", 1),
        ("![![", 2),
        ("! [", 0),
        ("!\\[", 0),
        ("！[", 0),
        ("`![x]`", 1),
        ("&#33;[", 0),
        ("[!", 0),
    ],
)
def test_image_count(text: str, images: int) -> None:
    assert _c(text).images == images


@pytest.mark.parametrize(
    ("text", "links"),
    [
        ("", 0),
        ("no link here", 0),
        ("https://example.org/a", 1),
        ("http://example.org", 1),
        ("//example.org", 1),
        ("// comment", 0),
        ("//", 0),
        ("///x", 1),
        ("////", 0),
        ("a//b", 1),
        ("www.example.org", 1),
        ("WWW.example.org", 1),
        ("http://www.example.org", 2),  # the upper bound: a www. URL counts twice
        ("(www.example.org)", 1),
        ("_www.example.org", 1),
        ("awww.example.org", 0),
        ("1www.example.org", 0),
        ("www", 0),
        ("mailto:a@b.co", 2),  # mailto: and the e-mail form
        ("MAILTO:x", 1),
        ("mailto:", 1),
        ("a@b.co", 1),
        ("a@b", 0),
        ("a@b.", 0),
        ("a@b.1", 1),
        ("@b.co", 0),
        ("a.b+c%d-e_f@x-y.zz", 1),
        ("a@b.co c@d.ee", 2),
        ("a@b.co@d.ee", 2),
        ("x@y.com/a", 1),
        ("&#47;&#47;example.org", 1),
        ("&sol;&sol;example.org", 1),
        ("&#x2f;&#x2F;x", 1),
        ("\\/\\/example.org", 1),
        ("h&#116;tp://x", 1),
        ("&#119;ww.example.org", 1),
        ("w\\ww.example.org", 0),  # a backslash before a letter stays
        ("www\\.example.org", 1),
        ("&#x6D;ailto:x", 1),
        ("mailto\\:x", 1),
        ("a&commat;b.co", 1),
        ("a&#64;b.co", 1),
        ("a\\@b.co", 1),
        ("a&amp;#64;b.co", 0),
        ("[x](https://example.org)", 2),  # `](` and the `//`
        ("`https://example.org`", 1),
        ("```\nwww.example.org\n```", 1),
        ("<https://example.org>", 2),  # the angle autolink and the `//`
        ("javascript:alert(1)", 0),  # a bare scheme without `//` is no link in cmark-gfm
    ],
)
def test_link_count(text: str, links: int) -> None:
    assert _c(text).links == links


def test_links_are_counted_after_decoding_so_a_split_url_still_counts() -> None:
    assert _c("ht&#116;ps:&#47;&#47;evil.example").links == 1
    assert _c("https:\\/\\/evil.example").links == 1


def test_invisible_counts_characters_forgectl_would_mark() -> None:
    assert _c("a​b").invisible == 1
    assert _c("a‮b‬c").invisible == 2
    assert _c("a️͏ㅤ").invisible == 3
    assert _c("\U000e0100").invisible == 1
    assert _c("").invisible == 1
    assert _c("a\tb\nc").invisible == 0
    assert _c("plain é 😀 ＜！＠").invisible == 0


def test_invisible_marks_unassigned_and_surrogate_free_text() -> None:
    assert _c("͸").invisible == 1  # Cn


def test_counts_are_taken_from_the_input_not_from_the_neutralized_text() -> None:
    raw = "<img src=x> ![](u) www.example.org"
    before = _c(raw)
    after = _c(neutralize_characters(raw))
    assert (before.raw_html, before.images) == (1, 1)
    assert (after.raw_html, after.images) == (0, 0)


def test_count_parts_sums_over_parts() -> None:
    total = count_parts(["<a ![](u)", "www.x.org​", "", "<b"])
    assert total == TextCounts(raw_html=2, images=1, links=2, invisible=1)  # `](` and `www.`


def test_count_parts_of_nothing_is_zero() -> None:
    assert count_parts([]) == TextCounts(raw_html=0, images=0, links=0, invisible=0)


def test_text_counts_add() -> None:
    a = TextCounts(raw_html=1, images=2, links=3, invisible=4)
    b = TextCounts(raw_html=10, images=20, links=30, invisible=40)
    assert a + b == TextCounts(raw_html=11, images=22, links=33, invisible=44)


def test_counts_have_the_schema_names() -> None:
    assert set(TextCounts.__dataclass_fields__) == {"raw_html", "images", "links", "invisible"}


@pytest.mark.parametrize("bad", [None, b"x", 3])
def test_non_str_is_a_type_error(bad: object) -> None:
    with pytest.raises(TypeError):
        count_text(bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        neutralize_characters(bad)  # type: ignore[arg-type]


# --- size and shape ---------------------------------------------------------------------------


SHAPES = [
    "<a" * 200_000,
    "![" * 200_000,
    "www." * 100_000,
    "//" * 200_000,
    "///" * 100_000,
    "a@" * 100_000,
    "a@b" * 100_000,
    "@" + "b" * 500_000,
    "a@" + "b-" * 250_000,
    "a@b." * 100_000,
    "&" * 200_000,
    "&#" * 100_000,
    "&amp" * 100_000,
    "&" + "a" * 500_000,
    "\\" * 200_000,
    "\\/" * 100_000,
    "mailto:" * 50_000,
    "<" * 500_000,
    "!" * 500_000,
    "​" * 500_000,
]


@pytest.mark.parametrize("text", SHAPES, ids=[f"shape{i}" for i in range(len(SHAPES))])
def test_huge_and_quadratic_shaped_inputs_run_in_linear_time(text: str) -> None:
    started = time.monotonic()
    count_text(text)
    neutralize_characters(text)
    assert time.monotonic() - started < 5.0


def test_a_megabyte_of_text_is_counted_exactly() -> None:
    text = "<a ![](u) www.x.org​\n" * 40_000
    got = count_text(text)
    assert got == TextCounts(raw_html=40_000, images=40_000, links=80_000, invisible=40_000)


# --- review round 1 fixes ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["&#" + "1" * 5000, "&#x" + "1" * 5000 + ";", "&#" + "9" * 5000 + ";//x"],
)
def test_huge_numeric_references_do_not_crash_count_text(text: str) -> None:
    count_text(text)


def test_huge_decimal_reference_still_lets_later_links_count() -> None:
    assert _c("&#" + "1" * 5000 + ";https://evil.example").links == 1


def test_zero_padded_decimal_reference_does_not_crash_count_text() -> None:
    assert _c("&#" + "0" * 5000 + "65;").links == 0


def test_zero_padded_decimal_reference_still_decodes() -> None:
    assert _c("&#" + "0" * 5000 + "119;ww.example.com").links == 1


def test_zero_padded_hex_reference_still_decodes() -> None:
    assert _c("&#x" + "0" * 5000 + "77;ww.example.com").links == 1


def test_all_zero_padded_reference_does_not_crash_count_text() -> None:
    count_text("&#" + "0" * 5000 + ";&#x" + "0" * 5000 + ";")


def test_numeric_reference_with_leading_zeros_decodes_as_before() -> None:
    assert _c("&#0000000119;ww.example.com").links == 1


@pytest.mark.parametrize(
    "text",
    ["a@1.2.com", "a@b_c.com", "a@b.1", ";b@.X"],
)
def test_email_forms_cmark_linkifies_are_counted(text: str) -> None:
    assert _c(text).links == 1


@pytest.mark.parametrize("text", ["a@b", "a@", "@b.co"])
def test_non_email_forms_are_not_counted(text: str) -> None:
    assert _c(text).links == 0


@pytest.mark.parametrize("lines", ["abc", b"abc"])
def test_suggestion_lines_reject_a_bare_string(lines: object) -> None:
    with pytest.raises(TypeError):
        neutralize_suggestion_lines(lines, applicable=True)  # type: ignore[arg-type]
