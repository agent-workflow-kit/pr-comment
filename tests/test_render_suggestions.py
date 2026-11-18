"""Suggestion blocks: applicable, degraded and moved (docs/interface.md: Suggestions; issue #19)."""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from typing import Any

import pytest

from pr_comment.mentions import MentionNeutralizer
from pr_comment.render import (
    Anchor,
    MovedFinding,
    SuggestionBlock,
    degrade_reason,
    render_finding_text,
    render_moved_entry,
    render_suggestion,
)
from tests.test_render import (
    SHA_A,
    assert_no_raw_html_image_or_mention,
    cmark,
    finding,
    mn,
)

DEGRADED_NOTE = (
    "_Suggestion shown as code: it cannot be posted as an applicable suggestion safely._"
)
MOVED_NOTE = "_Suggestion shown as code: this finding could not be posted inline._"


def sug(*lines: str, **overrides: Any) -> dict[str, Any]:
    return finding(suggestion={"lines": list(lines)}, **overrides)


def block(f: dict[str, Any], host: str = "github", moved: bool = False) -> SuggestionBlock:
    result = render_suggestion(f, mn(), host=host, moved=moved)
    assert result is not None
    return result


def pre_blocks(markdown: str) -> list[tuple[str, str]]:
    """(lang, text) of every pre block cmark-gfm renders."""
    page = cmark(markdown)
    found = re.findall(r'<pre(?: lang="([^"]*)")?[^>]*><code>(.*?)</code></pre>', page, re.S)
    return [(lang, html.unescape(text)) for lang, text in found]


# --- applicable ---------------------------------------------------------------------------


def test_no_suggestion_renders_nothing() -> None:
    assert render_suggestion(finding(), mn(), host="github") is None


def test_github_single_line() -> None:
    r = block(sug("x = 1"))
    assert r == SuggestionBlock("```suggestion\nx = 1\n```", None)


def test_github_range_has_the_same_fence() -> None:
    r = block(sug("a", "b", start_line=8))
    assert r.block == "```suggestion\na\nb\n```"


def test_gitlab_single_line_offset_is_zero() -> None:
    assert block(sug("x"), host="gitlab").block == "```suggestion:-0+0\nx\n```"


def test_gitlab_range_offset_is_line_minus_start_line() -> None:
    assert block(sug("x", start_line=7), host="gitlab").block == "```suggestion:-3+0\nx\n```"


def test_empty_suggestion_deletes_the_range() -> None:
    assert block(sug()).block == "```suggestion\n```"
    assert block(sug(start_line=9), host="gitlab").block == "```suggestion:-1+0\n```"
    assert pre_blocks("```suggestion\n```") == [("suggestion", "")]


def test_a_single_empty_line_is_a_blank_replacement() -> None:
    assert block(sug("")).block == "```suggestion\n\n```"


@pytest.mark.parametrize("host", ["github", "gitlab"])
def test_applicable_lines_stay_raw(host: str) -> None:
    lines = ["\tif a < b:  ", "  # @someone ![x](u) <b>&lt;", "x y", "``", "~~", "é\U0001f600"]
    r = block(sug(*lines), host=host)
    assert r.degraded is None
    assert r.block.split("\n")[1:-1] == lines
    assert r.block.count("```") == 2


def test_applicable_suggestion_keeps_mentions_and_html_untouched_and_counts_no_mention() -> None:
    mentions = mn()
    render_suggestion(sug("@victim <i>"), mentions, host="github")
    assert mentions.count == 0


def test_applicable_suggestion_renders_as_code_in_cmark() -> None:
    lines = ["<script>@victim</script>", "![x](http://e)", "  indented", "~~ ``"]
    r = block(sug(*lines))
    assert pre_blocks(r.block) == [("suggestion", "\n".join(lines) + "\n")]


def test_suggestion_stays_applicable_after_a_closed_body() -> None:
    f = sug("new", body="Closed body with `code`.\n\n```py\nx\n```")
    text = render_finding_text(f, mn(), suggestion_block=block(f).block)
    blocks = pre_blocks(text)
    assert blocks == [("py", "x\n"), ("suggestion", "new\n")]


def test_suggestion_after_an_unclosed_body_fence_is_swallowed_not_applicable() -> None:
    # The body's own open fence makes the host see no suggestion block; nothing is applicable
    # that pr-comment did not write, and the text stays inside the code.
    f = sug("new", body="```\nopen")
    text = render_finding_text(f, mn(), suggestion_block=block(f).block)
    assert [lang for lang, _ in pre_blocks(text)] == [""]


def test_two_applicable_blocks_are_independent() -> None:
    a = block(sug("one")).block
    b = block(sug("two")).block
    assert [t for _, t in pre_blocks(a + "\n\n" + b)] == ["one\n", "two\n"]


@pytest.mark.parametrize("ending", ["x  ", "x\t", "x \t "])
def test_trailing_whitespace_is_kept(ending: str) -> None:
    assert block(sug(ending)).block == f"```suggestion\n{ending}\n```"


def test_crlf_lines_are_split_by_the_pipeline_not_left_in_a_line() -> None:
    with pytest.raises(ValueError):
        render_suggestion(sug("a\r\nb"), mn(), host="github")


SEPARATORS = ["\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]


@pytest.mark.parametrize("sep", SEPARATORS)
def test_a_separator_in_a_line_is_refused(sep: str) -> None:
    with pytest.raises(ValueError):
        render_suggestion(sug(f"a{sep}b"), mn(), host="github")


# --- anchoring requirements ---------------------------------------------------------------


def test_applicable_suggestion_needs_a_line() -> None:
    with pytest.raises(ValueError):
        render_suggestion(sug("x", line=None), mn(), host="github")


def test_applicable_suggestion_is_right_side_only() -> None:
    with pytest.raises(ValueError):
        render_suggestion(sug("x", side="left"), mn(), host="github")
    assert block(sug("x", side="right")).degraded is None


@pytest.mark.parametrize("start", [10, 11, 0, -1, True, "3"])
def test_a_bad_start_line_is_refused(start: Any) -> None:
    with pytest.raises(ValueError):
        render_suggestion(sug("x", start_line=start), mn(), host="gitlab")


@pytest.mark.parametrize("line", [0, -2, True, 1.0, "10"])
def test_a_bad_line_is_refused(line: Any) -> None:
    with pytest.raises(ValueError):
        render_suggestion(sug("x", line=line), mn(), host="github")


def test_unknown_host_is_refused() -> None:
    with pytest.raises(ValueError):
        render_suggestion(sug("x"), mn(), host="gitea")


@pytest.mark.parametrize(
    "suggestion",
    [{"lines": "abc"}, {"lines": [1]}, {"lines": [None]}, {}, [], "x", {"lines": ["a"], "x": 1}],
)
def test_a_malformed_suggestion_is_refused(suggestion: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        render_suggestion(finding(suggestion=suggestion), mn(), host="github")


def test_suggestion_none_is_no_suggestion() -> None:
    assert render_suggestion(finding(suggestion=None), mn(), host="github") is None


# --- degrade reasons ----------------------------------------------------------------------

REASON_CASES = [
    (["ok", "a‮b"], "invisible_chars"),
    (["​"], "invisible_chars"),
    (["x️"], "invisible_chars"),
    (["x­y"], "invisible_chars"),
    (["x\u0007y"], "invisible_chars"),
    ([""], "invisible_chars"),
    (["xㅤ"], "invisible_chars"),
    (["```"], "fence"),
    (["a ``` b"], "fence"),
    (["````"], "fence"),
    (["~~~"], "fence"),
    ([" ~~~~ x"], "fence"),
    (["```python"], "fence"),
    # step 6's leading space is applied to the whole body (assemble.py), not by render_suggestion
    (["/cmd"], "slash_line"),
    (["/"], "slash_line"),
    (["ok", "/* c */"], "slash_line"),
    # order: invisible_chars, fence, slash_line
    (["/x", "```", "‮"], "invisible_chars"),
    (["/x", "```"], "fence"),
    (["```", "/x"], "fence"),
]
INVISIBLE_CASES = [
    (["\ue000"], "invisible_chars"),  # Co U+E000
    (["\u0378"], "invisible_chars"),  # Cn U+0378 (unassigned in Python 3.12 and 3.13)
    (["\U000e0100"], "invisible_chars"),
    (["\u034f"], "invisible_chars"),
    (["\u2800"], "invisible_chars"),
    (["\u180b"], "invisible_chars"),
    (["\u180e"], "invisible_chars"),
]
OK_CASES = [
    [],
    [""],
    ["a/b", " /x", "\t/x", "x//y"],
    ["``", "~~", "`~`~`~"],
    ["a\tb"],
    ["a\tb\u00a0c"],  # tab and NBSP stay applicable
    ["é z "],
]


@pytest.mark.parametrize(("lines", "reason"), REASON_CASES + INVISIBLE_CASES)
def test_degrade_reason(lines: list[str], reason: str) -> None:
    assert degrade_reason(lines) == reason


@pytest.mark.parametrize("lines", OK_CASES)
def test_no_degrade_reason(lines: list[str]) -> None:
    assert degrade_reason(lines) is None


@pytest.mark.parametrize(("lines", "reason"), REASON_CASES + INVISIBLE_CASES)
@pytest.mark.parametrize("host", ["github", "gitlab"])
def test_degraded_suggestion_is_a_text_block_with_a_note(
    lines: list[str], reason: str, host: str
) -> None:
    r = block(sug(*lines), host=host)
    assert r.degraded == reason
    assert r.block.startswith(DEGRADED_NOTE + "\n\n")
    assert "suggestion" not in [lang for lang, _ in pre_blocks(r.block)]
    assert "```suggestion" not in r.block.replace("<U+", "").split("\n\n", 1)[1].split("\n")[0]
    assert pre_blocks(r.block)[0][0] == "text"


def test_the_first_unassigned_code_point_is_invisible_on_every_python() -> None:
    # Category Cn follows the running Python's Unicode tables (docs/interface.md: Suggestions);
    # U+0378 is unassigned in 3.12 and 3.13.
    assert degrade_reason(["a\u0378"]) == "invisible_chars"


def test_golden_per_reason(golden: Callable[[str, str], None]) -> None:
    out: list[str] = []
    for lines, reason in REASON_CASES[::3] + [(["‮"], "invisible_chars")]:
        r = block(sug(*lines))
        out.append(f"# {reason}\n{r.block}")
    golden("render/suggestion_degraded.txt", "\n\n---\n\n".join(out) + "\n")


def test_golden_applicable(golden: Callable[[str, str], None]) -> None:
    cases = [
        block(sug("a", "  b")),
        block(sug("x", start_line=7), host="gitlab"),
        block(sug()),
    ]
    golden("render/suggestion_applicable.txt", "\n\n---\n\n".join(c.block for c in cases) + "\n")


# --- degraded content ---------------------------------------------------------------------


def test_a_literal_marker_text_is_distinguishable_from_a_real_marker() -> None:
    r = block(sug("a<U+202E>b", "c‮d"))
    assert r.block.endswith("\n```")
    body = r.block.split("\n")[3:-1]
    assert body == ["a＜U+202E>b", "c<U+202E>d"]


def test_markers_for_every_markable_character_and_tabs_stay() -> None:
    r = block(sug("a\tb​", "x\u0000".replace("\u0000", "\u0001")))
    assert "a\tb<U+200B>" in r.block
    assert "x<U+0001>" in r.block


def test_degraded_lines_are_neutralized_before_markers() -> None:
    r = block(sug("```", "<b>x</b> ![i](u) @victim", "/cmd"))
    assert "＜b>x＜/b> ！[i](u) ＠victim" in r.block
    assert "/cmd" in r.block
    assert "<b>" not in r.block


def test_degraded_mentions_are_reported_and_allowlist_applies() -> None:
    mentions = MentionNeutralizer("neutralize", ("friend",))
    r = render_suggestion(sug("```", "@friend @victim"), mentions, host="github")
    assert r is not None
    assert "@friend ＠victim" in r.block
    assert mentions.names == ("victim",)


def test_degraded_block_is_inert_in_cmark() -> None:
    lines = ["```", "<img src=x onerror=1>", "![a](http://e/p.png)", "@all", "[l](javascript:1)"]
    r = block(sug(*lines))
    assert_no_raw_html_image_or_mention(r.block)
    ((lang, text),) = pre_blocks(r.block)
    assert lang == "text"
    assert text.count("\n") == len(lines)


@pytest.mark.parametrize(
    ("lines", "fence"),
    [
        (["```"], "````"),
        (["a ````` b", "``"], "``````"),
        (["~~~", "`"], "```"),
        (["```", "```", "``"], "````"),
    ],
)
def test_degraded_fence_length_is_one_more_than_the_longest_backtick_run(
    lines: list[str], fence: str
) -> None:
    r = block(sug(*lines))
    note_and_block = r.block.split("\n\n", 1)[1]
    assert note_and_block.startswith(fence + "text\n")
    assert note_and_block.endswith("\n" + fence)
    ((lang, text),) = pre_blocks(r.block)
    assert lang == "text"
    assert text == "\n".join(lines) + "\n"


def test_tildes_do_not_lengthen_a_backtick_fence() -> None:
    r = block(sug("~~~~~~~~", "/x"))
    assert r.block.split("\n\n", 1)[1].startswith("```text\n")


def test_a_long_backtick_run_gets_the_longest_fence_cmark_keeps() -> None:
    run = "`" * 254
    r = block(sug("a", run, "b"))
    assert r.block.split("\n\n", 1)[1].startswith("`" * 255 + "text\n")
    assert pre_blocks(r.block) == [("text", f"a\n{run}\nb\n")]


@pytest.mark.parametrize("n", [255, 256, 257, 511, 5000])
def test_a_backtick_run_no_fence_fits_uses_a_tilde_fence(n: int) -> None:
    # cmark-gfm clamps a fence length to 255, so a 256-long fence would be closed by a
    # shorter run in the content.
    run = "`" * n
    r = block(sug("a", run, "~~~~", "b"))
    assert r.block.split("\n\n", 1)[1].startswith("~~~~~text\n")
    assert pre_blocks(r.block) == [("text", f"a\n{run}\n~~~~\nb\n")]
    assert r.degraded == "fence"


@pytest.mark.parametrize("n", [255, 600])
def test_runs_of_both_kinds_too_long_for_any_fence_are_split_visibly(n: int) -> None:
    lines = ["a", "`" * n, "~" * n, "b"]
    r = block(sug(*lines))
    ((lang, text),) = pre_blocks(r.block)
    assert lang == "text"
    assert "<run split>" in text
    assert text.replace("<run split>", "") == "\n".join(lines) + "\n"
    assert max(len(m.group()) for m in re.finditer("`+", text)) <= 254
    assert r.block.split("\n\n", 1)[1].startswith("`" * 255 + "text\n")


def test_a_huge_suggestion_is_rendered_whole() -> None:
    lines = ["x" * 100] * 600 + ["```"]
    r = block(sug(*lines))
    ((_, text),) = pre_blocks(r.block)
    assert text == "\n".join(lines) + "\n"
    assert r.degraded == "fence"


def test_the_word_suggestion_in_lines_does_not_make_a_suggestion_block() -> None:
    r = block(sug("suggestion", "```", "~~~"))
    assert [lang for lang, _ in pre_blocks(r.block)] == ["text"]
    assert r.degraded == "fence"


@pytest.mark.parametrize(
    "lines",
    [
        ["```suggestion"],
        ["a", "~~~suggestion:-1+0"],
        ["> ```Suggestion"],
        ["~~~~", "~~~suggestion", "evil()"],
        ["```", "```suggestion", "evil()"],
    ],
)
@pytest.mark.parametrize("moved", [False, True])
def test_a_suggestion_line_with_a_suggestion_fence_is_refused(
    lines: list[str], moved: bool
) -> None:
    with pytest.raises(ValueError, match="suggestion fence"):
        render_suggestion(sug(*lines), mn(), host="github", moved=moved)


@pytest.mark.parametrize(
    ("body", "lines"),
    [
        ("~~~~", ["~~~~", "~~~suggestion", "evil()"]),
        ("```", ["```", "```suggestion", "evil()"]),
    ],
)
def test_an_open_body_fence_cannot_forge_an_applicable_block(body: str, lines: list[str]) -> None:
    # No render call may emit a suggestion block for these (the layout is assemble.py).
    f = sug(*lines, body=body)
    for moved in (False, True):
        with pytest.raises(ValueError, match="suggestion fence"):
            render_suggestion(f, mn(), host="github", moved=moved)


def test_applicable_with_the_word_suggestion_is_one_block() -> None:
    r = block(sug("suggestion", "# suggestion"))
    assert r.degraded is None
    assert pre_blocks(r.block) == [("suggestion", "suggestion\n# suggestion\n")]


def test_tilde_case_a_line_cannot_close_an_open_tilde_fence() -> None:
    f = sug("~~~", "@victim <img src=x>", body="~~~\nopen", side="right")
    r = block(f)
    assert r.degraded == "fence"
    text = render_finding_text(f, mn(), suggestion_block=r.block)
    assert "suggestion" not in [lang for lang, _ in pre_blocks(text)]
    # Everything after the body's open fence stays inside code, nothing is applicable and
    # neither the neutralized mention nor the HTML is live.
    assert_no_raw_html_image_or_mention(text)


def test_tilde_line_in_a_closed_body_is_still_not_applicable() -> None:
    f = sug("~~~", body="b")
    text = render_finding_text(f, mn(), suggestion_block=block(f).block)
    assert [lang for lang, _ in pre_blocks(text)] == ["text"]


# render_suggestion keeps the slash; the leading space is step 6 (assemble.py).
def test_slash_line_degrades_and_keeps_its_slash() -> None:
    r = block(sug("/approve"))
    assert r.degraded == "slash_line"
    assert "\n/approve\n" in r.block
    assert pre_blocks(r.block)[0][0] == "text"


def test_no_text_is_dropped_by_degrading() -> None:
    lines = ["alpha", "```", "beta\t", "gamma‮"]
    r = block(sug(*lines))
    for word in ("alpha", "beta\t", "gamma<U+202E>"):
        assert word in r.block


# --- moved findings -----------------------------------------------------------------------


def test_moved_suggestion_is_code_with_the_moved_note_and_not_degraded() -> None:
    r = block(sug("x = 1"), moved=True)
    assert r.degraded is None
    assert r.block == MOVED_NOTE + "\n\n```text\nx = 1\n```"


def test_moved_suggestion_is_neutralized_and_marked() -> None:
    r = block(sug("<b>@v‮"), moved=True)
    assert r.block.endswith("```text\n＜b>＠v<U+202E>\n```")
    assert r.degraded is None


def test_moved_empty_suggestion_is_an_empty_text_block() -> None:
    r = block(sug(), moved=True)
    assert r.block == MOVED_NOTE + "\n\n```text\n```"
    assert pre_blocks(r.block) == [("text", "")]


def test_moved_suggestion_with_fences_uses_a_longer_fence() -> None:
    r = block(sug("```", "~~~"), moved=True)
    assert r.degraded is None
    assert "````text\n```\n~~~\n````" in r.block


def test_moved_findings_need_no_anchor_for_their_suggestion() -> None:
    r = block(sug("x", line=None, side="left"), moved=True)
    assert r.block.endswith("```text\nx\n```")


def test_moved_entry_renders_its_suggestion_without_being_told() -> None:
    f = sug("new", body="Body.")
    entry = render_moved_entry(MovedFinding(f, "changed", Anchor(SHA_A, 10)), mn())
    assert entry.endswith("Body.\n\n" + MOVED_NOTE + "\n\n```text\nnew\n```")
    assert [lang for lang, _ in pre_blocks(entry)] == ["text"]


def test_moved_entry_without_body_keeps_the_suggestion() -> None:
    f = sug("new", body=None)
    entry = render_moved_entry(MovedFinding(f, "changed", Anchor(SHA_A, 10)), mn())
    assert entry.endswith(MOVED_NOTE + "\n\n```text\nnew\n```")


def test_moved_entry_uses_a_given_block_as_it_is() -> None:
    f = sug("new")
    entry = render_moved_entry(
        MovedFinding(f, "changed", Anchor(SHA_A, 10), suggestion_block="BLOCK"), mn()
    )
    assert entry.endswith("\n\nBLOCK")
    assert "new" not in entry


def test_moved_golden(golden: Callable[[str, str], None]) -> None:
    f = sug("a", "```", "b‮", id="m1", title="T", severity="minor", start_line=8)
    entry = render_moved_entry(MovedFinding(f, "changed", Anchor(SHA_A, 10, 8)), mn())
    golden("render/suggestion_moved.txt", entry + "\n")


def test_moved_suggestion_cannot_leak_in_cmark() -> None:
    f = sug("```", "</pre><img src=x>", "@all", "![a](u)", body="Done.")
    entry = render_moved_entry(MovedFinding(f, "changed", Anchor(SHA_A, 10)), mn())
    assert_no_raw_html_image_or_mention(entry)


# --- host-independence of the text pipeline ----------------------------------------------


def test_finding_text_joins_the_block_with_a_blank_line() -> None:
    f = sug("x", body="Body.", severity="minor", title="T")
    text = render_finding_text(f, mn(), suggestion_block=block(f).block)
    assert text == "**minor · T**\n\nBody.\n\n```suggestion\nx\n```"


def test_an_explicit_null_side_means_right() -> None:
    assert block(sug("x", side=None)).degraded is None


def test_mentions_are_reported_in_order_title_body_suggestion() -> None:
    f = sug("/x @bob", title="hi @alice", body="see @carol")
    mentions = MentionNeutralizer("neutralize")
    render_finding_text(f, mentions, suggestion_block=lambda: block_with(f, mentions))
    assert mentions.names == ("alice", "carol", "bob")


def block_with(f: dict[str, Any], mentions: MentionNeutralizer) -> str:
    r = render_suggestion(f, mentions, host="github")
    assert r is not None
    return r.block


def test_a_suggestion_callable_may_return_none() -> None:
    f = sug("x", body="b")
    assert render_finding_text(f, mn(), suggestion_block=lambda: None) == "b"
