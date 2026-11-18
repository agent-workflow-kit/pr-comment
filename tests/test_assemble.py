"""Body assembly: attribution line, layouts, marker, forgectl's pre-applied neutralizing, marker
detection and the open-fence guard (docs/interface.md: Footers and marker, Marker detection, Body
structure, Text pipeline steps 5 and 6; issue #20)."""

from __future__ import annotations

import random
import re
from collections.abc import Callable
from typing import Any

import pytest

from pr_comment.assemble import (
    MARKER,
    Body,
    build_body,
    build_inline,
    forgectl_neutralize,
    has_marker,
    render_attribution,
    render_inline_finding,
    render_text_part,
)
from pr_comment.config import (
    DEFAULT_FOOTER_AUTO,
    DEFAULT_FOOTER_HUMAN,
    ConfigError,
)
from pr_comment.mentions import MentionNeutralizer
from pr_comment.render import (
    Anchor,
    MovedFinding,
    render_moved_entry,
    render_review_parts,
)
from tests.test_render import (
    SHA_A,
    assert_no_raw_html_image_or_mention,
    cmark,
    cmark_unsafe,
    finding,
    mn,
)
from tests.test_render_suggestions import pre_blocks


def assert_inert(body: str) -> None:
    """No raw HTML, image or live mention anywhere but in pr-comment's own marker line."""
    assert body.endswith("\n" + MARKER)
    assert_no_raw_html_image_or_mention(body[: -len(MARKER)])


HUMAN = "human:alice"
AUTO = "auto:policy-1"
SHA12 = SHA_A[:12]


def attribution(
    actor: str = HUMAN,
    *,
    human: str | bool = DEFAULT_FOOTER_HUMAN,
    auto: str = DEFAULT_FOOTER_AUTO,
    me: str = "alice",
    decision_id: str | None = None,
    version: str = "0.1.0",
    mentions: MentionNeutralizer | None = None,
) -> str | None:
    return render_attribution(
        actor,
        footer_human=human,  # pyright: ignore[reportArgumentType]
        footer_auto=auto,
        me_name=me,
        head_sha=SHA_A,
        decision_id=decision_id,
        tool_version=version,
        mentions=mentions or mn(),
    )


# --- attribution line ------------------------------------------------------------------------


def test_default_human_line() -> None:
    assert attribution() == (
        f"Drafted with AI assistance · posted by `alice` after review · {SHA12}"
    )


def test_default_auto_line_uses_policy_and_the_sha_as_is() -> None:
    assert attribution(AUTO, me="bob") == (
        f"Posted automatically under policy `policy-1` · no human review · {SHA12}"
    )


def test_user_for_auto_is_me_name_and_policy_for_human_is_empty() -> None:
    line = attribution(AUTO, auto="Run by {user} p={policy}|", me="bob")
    assert line == "Run by `bob` p=`policy-1`|"
    line = attribution(HUMAN, human="Run by {user} p={policy}|")
    assert line == "Run by `alice` p=|"


def test_every_placeholder_and_escaped_braces() -> None:
    line = attribution(
        human="v {tool_version} d {decision_id} s {sha} {{literal}}",
        decision_id="ticket-1",
        version="1.2.3",
    )
    assert line == f"v 1.2.3 d `ticket-1` s {SHA12} {{literal}}"


def test_decision_id_empty_is_nothing() -> None:
    assert attribution(human="d{decision_id}|") == "d|"


def test_an_actor_name_with_an_at_sign_is_neutralized_in_the_line() -> None:
    m = mn()
    line = attribution("auto:@all", auto="Posted under {policy}", mentions=m)
    assert line == "Posted under `＠all`"
    assert m.names == ("all",)
    assert m.count == 1


def test_an_allowed_name_in_a_code_span_is_still_replaced() -> None:
    # The allow rule needs whitespace, end or a listed follow character after the name; the
    # closing backtick is none of them, so the value is replaced (the conservative reading).
    line = attribution(HUMAN, me="@alice", mentions=mn("alice"))
    assert line is not None and "`@alice`" not in line


def test_human_without_footer_has_no_line() -> None:
    assert attribution(HUMAN, human=False) is None


def test_auto_ignores_a_false_human_footer() -> None:
    assert attribution(AUTO, human=False) is not None


@pytest.mark.parametrize(
    ("template", "decision_id"),
    [("{decision_id}1. x", None), ("{user} did it", None)],
)
def test_a_line_that_does_not_start_with_a_letter_is_config_invalid(
    template: str, decision_id: str | None
) -> None:
    with pytest.raises(ConfigError) as err:
        attribution(HUMAN, human=template, decision_id=decision_id)
    assert err.value.key == "footer.human"
    with pytest.raises(ConfigError) as err:
        attribution(AUTO, auto=template, decision_id=decision_id)
    assert err.value.key == "footer.auto"


def test_a_template_starting_with_a_placeholder_is_refused_even_with_a_value() -> None:
    # A non-empty value is written as a code span, so the line then starts with a backtick.
    with pytest.raises(ConfigError):
        attribution(human="{decision_id}1. x", decision_id="d")


@pytest.mark.parametrize(
    "template",
    ["x <img src=a>", "x `y`", "x [a](b)", "x \\@all", "", "x" * 513, "x\ny", "x‮y", "{nope}"],
)
def test_a_hostile_template_is_refused_at_render_time_too(template: str) -> None:
    with pytest.raises(ConfigError) as err:
        attribution(human=template)
    assert err.value.key == "footer.human"


@pytest.mark.parametrize(
    "bad",
    [
        {"actor": "human:a`b"},
        {"actor": "human:"},
        {"actor": "robot:x"},
        {"actor": "auto:x y"},
        {"me": "a`b"},
        {"me": "x\n"},
        {"decision_id": "a`b"},
        {"decision_id": "x" * 129},
        {"version": "1`"},
    ],
)
def test_placeholder_values_outside_their_grammars_are_refused(bad: dict[str, str]) -> None:
    kwargs: dict[str, Any] = {k: v for k, v in bad.items() if k != "actor"}
    with pytest.raises(ValueError):
        attribution(
            bad.get("actor", HUMAN), human="u {user} {decision_id} {tool_version}", **kwargs
        )


@pytest.mark.parametrize("sha", ["", "abc", SHA_A.upper(), SHA_A[:-1], SHA_A + "0", "g" * 40])
def test_a_bad_head_sha_is_refused(sha: str) -> None:
    with pytest.raises(ValueError):
        render_attribution(
            HUMAN,
            footer_human=DEFAULT_FOOTER_HUMAN,
            footer_auto=DEFAULT_FOOTER_AUTO,
            me_name="alice",
            head_sha=sha,
            decision_id=None,
            tool_version="0.1.0",
            mentions=mn(),
        )


def test_attribution_is_one_line_and_inert_in_cmark() -> None:
    line = attribution(
        AUTO, auto="Under {policy} {{x}} {decision_id}", me="m", decision_id="www.example.org"
    )
    assert line is not None and "\n" not in line
    page = cmark(line)
    assert page.count("<p>") == 1 and "<a " not in page
    assert page.count("<code>") == 2


# --- layouts -----------------------------------------------------------------------------------

LINE = "Attribution line"


def test_review_layout_with_parts() -> None:
    body = build_body(["part one", "part two"], LINE)
    assert body.text == f"{LINE}\n\n---\n\npart one\n\npart two\n\n{MARKER}"


def test_layout_without_parts_has_no_rule() -> None:
    assert build_body([], LINE).text == f"{LINE}\n\n{MARKER}"


def test_empty_parts_are_left_out() -> None:
    assert build_body(["", "x", ""], LINE).text == f"{LINE}\n\n---\n\nx\n\n{MARKER}"


def test_footer_false_leaves_out_line_and_rule() -> None:
    assert build_body(["x"], None).text == f"x\n\n{MARKER}"
    assert build_body([], None).text == MARKER


def test_inline_layout() -> None:
    assert build_inline("**nit**\n\nbody").text == f"**nit**\n\nbody\n\n{MARKER}"


def test_parts_never_touch_without_a_blank_line() -> None:
    body = build_body(["a", "b"], LINE).text
    assert "a\n\nb" in body and "---\n\na" in body and f"b\n\n{MARKER}" in body


def test_untrusted_marker_text_cannot_survive_the_pipeline() -> None:
    m = mn()
    part = render_text_part(f"x\n{MARKER}\n<!-- forgectl:dedupe:k -->", m)
    assert "<" not in part
    body = build_body([part], LINE).text
    assert body.endswith("\n" + MARKER) and body.count("<!--") == 1
    assert has_marker(body)


# --- forgectl's neutralizing, pre-applied -------------------------------------------------


@pytest.mark.parametrize(
    ("text", "out", "lines", "markers"),
    [
        ("/x", " /x", 1, 0),
        ("a\n/x\n/y\nb", "a\n /x\n /y\nb", 2, 0),
        (" /x", " /x", 0, 0),
        ("a/x", "a/x", 0, 0),
        ("//", " //", 1, 0),
        ("forgectl:dedupe:k", "forgectl:dedupe-k", 0, 1),
        ("FORGECTL:Dedupe:k x forgectl:dedupe:j", "FORGECTL:Dedupe-k x forgectl:dedupe-j", 0, 2),
        ("/forgectl:dedupe:k", " /forgectl:dedupe-k", 1, 1),
        ("forgectl:dedupe::", "forgectl:dedupe-:", 0, 1),
        ("forgectl:dedupe", "forgectl:dedupe", 0, 0),
        ("", "", 0, 0),
        ("```text\n/x\n```", "```text\n /x\n```", 1, 0),
        ("> /x", "> /x", 0, 0),
    ],
)
def test_forgectl_neutralize_table(text: str, out: str, lines: int, markers: int) -> None:
    result = forgectl_neutralize(text)
    assert (result.text, result.neutralized_lines, result.markers_removed) == (out, lines, markers)


def test_forgectl_neutralize_leaves_our_marker_alone() -> None:
    assert forgectl_neutralize(MARKER).text == MARKER


def test_forgectl_neutralize_is_idempotent_on_random_text() -> None:
    rng = random.Random(20)
    pieces = [
        "/",
        " ",
        "\n",
        "forgectl:dedupe:",
        "FORGECTL:DEDUPE:",
        "x",
        ":",
        "-",
        "\t",
        "//",
        "a/",
    ]
    for _ in range(400):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 30)))
        once = forgectl_neutralize(text)
        twice = forgectl_neutralize(once.text)
        assert twice.text == once.text
        assert (twice.neutralized_lines, twice.markers_removed) == (0, 0)
        assert not re.search(r"(?im)^/", once.text)
        assert not re.search(r"(?i)forgectl:dedupe:", once.text)


def test_forgectl_neutralize_huge_input_is_linear_and_exact() -> None:
    text = "/x\n" * 100_000 + "forgectl:dedupe:" * 1000
    result = forgectl_neutralize(text)
    assert result.neutralized_lines == 100_000 and result.markers_removed == 1000


def test_build_body_applies_step_6_and_reports_counts() -> None:
    body = build_body(["/approve\nforgectl:dedupe:k"], LINE)
    assert isinstance(body, Body)
    assert body.text == f"{LINE}\n\n---\n\n /approve\nforgectl:dedupe-k\n\n{MARKER}"
    assert (body.neutralized_lines, body.markers_removed) == (1, 1)


def test_degraded_block_slash_line_gets_its_space_in_the_body() -> None:
    f = finding(suggestion={"lines": ["ok", "/* c */"]}, body="b")
    inline = render_inline_finding(f, mn(), host="github")
    assert inline.degraded == "slash_line"
    body = build_inline(inline.text)
    assert "\n /* c */\n" in body.text and body.neutralized_lines == 1
    assert [lang for lang, _ in pre_blocks(body.text)] == ["text"]


def test_slash_line_in_the_fence_case_golden_shape() -> None:
    f = finding(suggestion={"lines": ["/x", "```"]}, body="b")
    body = build_inline(render_inline_finding(f, mn(), host="github").text)
    assert "````text\n /x\n```\n````" in body.text


# --- marker detection ----------------------------------------------------------------------

DEDUPE = "<!-- forgectl:dedupe:prc:rv:01ABC -->"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (f"text\n\n{MARKER}", True),
        (f"text\n\n{MARKER}\n", True),
        (f"text\n\n{MARKER}   \n\n", True),
        (f"text\n\n{MARKER}\n{DEDUPE}", True),
        (f"text\n\n{MARKER}\n{DEDUPE}\n", True),
        (f"text\n\n{MARKER}\n\n{DEDUPE}  \n\n", True),
        (f"text\r\n\r\n{MARKER}\r\n{DEDUPE}\r\n", True),
        (f"> {MARKER}", False),
        (f"text\n\n> quoted {MARKER}", False),
        (f"text\n{MARKER}\nmore text", False),
        (f"text\n{DEDUPE}\n{MARKER}\n{DEDUPE}", True),
        (f"{MARKER}\n{DEDUPE}\n{DEDUPE}", False),
        (f"text\n{DEDUPE}\nmore\n{MARKER}\ntail", False),
        (f"text\n {MARKER}", False),
        (f"text\n{MARKER} x", False),
        (f"text\n{MARKER.upper()}", False),
        ("<!-- pr-comment:v2 -->", False),
        (f"{MARKER}\n<!-- forgectl:dedupe:{'a' * 129} -->", False),
        (f"{MARKER}\n<!-- forgectl:dedupe: -->", False),
        (f"{MARKER}\n<!-- forgectl:dedupe:bad key -->", False),
        ("", False),
        ("   \n", False),
        (MARKER, True),
    ],
)
def test_marker_detection_table(body: str, expected: bool) -> None:
    assert has_marker(body) is expected


def test_a_dedupe_line_inside_untrusted_text_does_not_match() -> None:
    part = render_text_part(f"hi\n{DEDUPE}\nbye", mn())
    body = build_body([part], LINE).text
    assert has_marker(body)
    assert not has_marker(f"{part}\n")


def test_assembled_bodies_always_have_marker_last_and_attribution_first() -> None:
    for parts in ([], ["a"], ["a", "b"], ["x" * 10_000]):
        for line in (LINE, None):
            text = build_body(parts, line).text
            assert has_marker(text) and text.endswith(MARKER)
            if line:
                assert text.startswith(line + "\n\n")


# --- open body fence -----------------------------------------------------------------------

OPEN_FENCES = [
    "```",
    "```python",
    "````",
    "~~~",
    "~~~~~~~~",
    "   ```",
    "> ```",
    "> > ```",
    "- ```",
    "1. ~~~",
    "> - ```",
    "* * ```",
    "```" + " " * 3,
]
HOSTILE_LINES = ["```", "# HEAD", "[l](http://example.com)", "**bold**", "~~~~~", "````````"]


def _moved_text(f: dict[str, Any]) -> str:
    return render_moved_entry(MovedFinding(f, "changed", Anchor(SHA_A, 10)), mn())


@pytest.mark.parametrize("indent", ["", "  ", "    "])
@pytest.mark.parametrize("opener", OPEN_FENCES)
@pytest.mark.parametrize("moved", [False, True])
def test_open_body_fence_cannot_pull_the_suggestion_into_live_markdown(
    opener: str, indent: str, moved: bool
) -> None:
    f = finding(body=f"before\n{opener}\n{indent}open code", suggestion={"lines": HOSTILE_LINES})
    text = _moved_text(f) if moved else render_inline_finding(f, mn(), host="github").text
    page = cmark(text)
    after = page[page.index("open code") :]
    assert "<h1" not in after and "<a " not in after and "<strong>bold" not in after
    assert_inert(build_inline(text).text)


def test_a_body_with_a_fence_run_gets_a_block_with_no_run_in_it() -> None:
    f = finding(body="x\n```\nopen", suggestion={"lines": ["a", "```", "~~~~~~", "b"]})
    inline = render_inline_finding(f, mn(), host="github")
    assert inline.degraded == "fence"
    block = inline.text.split("\n\n")[-1]
    shown = block.split("\n")[1:-1]
    assert shown == ["a", "``<run split>`", "~~<run split>~~<run split>~~", "b"]
    assert block.startswith("```text\n") and block.endswith("\n```")


def test_a_body_without_a_fence_run_keeps_the_verbatim_longer_fence() -> None:
    f = finding(body="plain", suggestion={"lines": ["```"]})
    assert render_inline_finding(f, mn(), host="github").text.endswith("````text\n```\n````")


def test_a_closed_body_fence_leaves_an_applicable_block_applicable() -> None:
    f = finding(body="x\n```\ncode\n```", suggestion={"lines": ["x = 1"]})
    inline = render_inline_finding(f, mn(), host="github")
    assert inline.degraded is None
    langs = [lang for lang, _ in pre_blocks(build_inline(inline.text).text)]
    assert langs == ["", "suggestion"] or langs[-1] == "suggestion"


def test_an_applicable_block_after_an_open_fence_is_inert_never_live() -> None:
    f = finding(body="x\n```\nopen", suggestion={"lines": ["# H", "y"]})
    page = cmark(build_inline(render_inline_finding(f, mn(), host="github").text).text)
    assert "<h1" not in page and 'lang="suggestion"' not in page


# --- inline findings and degraded reports ----------------------------------------------------


def test_render_inline_finding_reports_the_degraded_reason() -> None:
    f = finding(severity="minor", suggestion={"lines": ["a", "```"]})
    inline = render_inline_finding(f, mn(), host="gitlab")
    assert inline.degraded == "fence"
    plain = render_inline_finding(finding(), mn(), host="gitlab")
    assert plain.degraded is None
    applicable = render_inline_finding(finding(suggestion={"lines": ["z"]}), mn(), host="gitlab")
    assert applicable.degraded is None and "```suggestion:-0+0" in applicable.text


def test_mention_names_come_out_in_text_order() -> None:
    m = mn()
    f = finding(title="@t", body="@b", suggestion={"lines": ["@s", "/x", "```"]})
    render_inline_finding(f, m, host="github")
    assert m.names == ("t", "b", "s")


# --- whole-body differential checks --------------------------------------------------------

HOSTILE = (
    "<img src=x onerror=1> ![i](http://example.com/a.png) @octocat &#64;octocat\n"
    "<details><summary>x</summary>\n<!-- c -->\n```suggestion\n"
)


def test_hostile_text_never_forges_a_suggestion_in_a_part() -> None:
    from pr_comment.textpipe import text_problem

    assert text_problem(HOSTILE) == "suggestion_fence"


def test_whole_review_body_is_inert_in_cmark() -> None:
    m = mn()
    hostile = "<img src=x> ![i](http://example.com/a.png) @octocat &#64;octocat <!-- x --> <b>"
    inline_like = finding(body=hostile, title="<script>@a", suggestion={"lines": ["<b>", "@z"]})
    moved = MovedFinding(inline_like, "changed", Anchor(SHA_A, 3))
    parts = render_review_parts(hostile, [finding(line=None, body=hostile)], [moved], m)
    line = attribution(
        AUTO, auto="Under {policy} @all {decision_id}", me="m", mentions=m, decision_id="1."
    )
    body = build_body(parts, line)
    assert_inert(body.text)
    unsafe = cmark_unsafe(body.text)
    assert "<img" not in unsafe and "<script" not in unsafe
    assert body.text.startswith("Under `")
    assert has_marker(body.text)


def test_attribution_cannot_be_hidden_by_what_follows() -> None:
    line = attribution() or ""
    parts = [render_text_part("```\nunclosed <details>", mn())]
    page = cmark(build_body(parts, line).text)
    assert page.startswith("<p>Drafted with AI assistance")


def test_huge_parts_are_linear() -> None:
    big = "a\n" * 30_000
    body = build_body([render_text_part(big, mn()) for _ in range(5)], LINE)
    assert has_marker(body.text)
    nasty = "`" * 100_000 + "\n" + "~" * 100_000
    f = finding(body=nasty, suggestion={"lines": [nasty.replace("\n", "")]})
    assert render_inline_finding(f, mn(), host="github").degraded == "fence"


# --- goldens ------------------------------------------------------------------------------------


def _golden_bodies() -> dict[str, Body]:
    m = lambda: mn("octocat")  # noqa: E731
    human = attribution()
    auto = attribution(AUTO, me="bob")
    review_parts = render_review_parts(
        "Overall: **solid**, see @octocat and /ping.",
        [finding(line=None, path="docs/readme.md", body="File-level note.")],
        [
            MovedFinding(
                finding(
                    id="m1", severity="major", body="Moved body.", suggestion={"lines": ["x = 1"]}
                ),
                "changed",
                Anchor(SHA_A, 10),
            )
        ],
        m(),
    )
    inline = render_inline_finding(
        finding(
            severity="minor",
            title="Use a guard",
            body="Check it.\n/forgectl:dedupe:k",
            suggestion={"lines": ["if x:", "/* c */"]},
        ),
        m(),
        host="github",
    )
    applicable = render_inline_finding(
        finding(severity="nit", body="Rename.", suggestion={"lines": ["y = 2"]}), m(), host="gitlab"
    )
    return {
        "review": build_body(review_parts, human),
        "review_auto": build_body(review_parts, auto),
        "review_no_footer": build_body(review_parts, None),
        "review_empty": build_body([], human),
        "comment": build_body([render_text_part("A comment.", m())], human),
        "reply": build_body([render_text_part("A reply.\n/approve", m())], auto),
        "approve_with_body": build_body([render_text_part("Looks good.", m())], human),
        "approve_bare": build_body([], human),
        "approve_bare_no_footer": build_body([], None),
        "inline_degraded": build_inline(inline.text),
        "inline_applicable_gitlab": build_inline(applicable.text),
    }


def test_golden_bodies(golden: Callable[[str, str], None]) -> None:
    out = []
    for name, body in _golden_bodies().items():
        out.append(
            f"# {name} (neutralized_lines={body.neutralized_lines} "
            f"markers_removed={body.markers_removed})\n{body.text}"
        )
    golden("assemble/bodies.txt", "\n\n=====\n\n".join(out) + "\n")


def test_every_golden_body_has_attribution_first_and_marker_last() -> None:
    for name, body in _golden_bodies().items():
        assert body.text.endswith(MARKER), name
        assert has_marker(body.text), name
        if name in ("review_no_footer", "approve_bare_no_footer") or name.startswith("inline"):
            continue
        assert re.match(r"(Drafted|Posted)", body.text), name
