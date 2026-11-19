"""Text safety against GitHub's engine: the oracle itself, golden cases over the whole pipeline
(draft to body) and the open decisions the current behavior is pinned for (issue #22).

Every body is rendered with the pinned ``cmarkgfm`` and checked for: no raw HTML, no image, no live
mention, no suggestion pr-comment did not write, the attribution line as the first paragraph and
pr-comment's markers visible in code (``tests/textsafety.py``). The golden files hold the posted
body and cmark-gfm's rendering of it, byte for byte, with placeholder names only.

What cmark-gfm cannot prove about GitHub's and GitLab's own renderers is listed in
docs/design.md (Test strategy) and left to the live tests (issue #39).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import jsonschema
import pytest

from pr_comment.assemble import (
    MARKER,
    forgectl_neutralize,
    has_marker,
    render_inline_finding,
)
from pr_comment.mentions import MentionNeutralizer
from pr_comment.neutralize import count_text
from tests.golden_harness import find_real_names
from tests.schema_harness import build_registry, load_schemas
from tests.textsafety import (
    Expect,
    Posted,
    Refused,
    draft_json,
    live_mentions,
    make_document,
    make_finding,
    make_unanchored,
    parse,
    render_probe,
    render_report,
    render_safe,
    run_review,
    run_text_command,
    violations,
)

# --- the oracle can fail ------------------------------------------------------------------------

BAD_BODIES = [
    ("<span>x</span>", "raw HTML tag <span>"),
    ("<details>x", "raw HTML tag <details>"),
    ('<img src="https://t.example/p">', "image"),
    ("![](https://t.example/p)", "image"),
    ("[r]: /u\n\n![t][r]", "image"),
    ("<!-- hidden -->", "raw HTML comment"),
    ("<?php\nx", "raw HTML processing instruction"),
    ("<!DOCTYPE x>", "raw HTML declaration"),
    ("x <b>y</b>", "raw HTML omitted"),
    ('<a href="u" onclick="x()">y</a>', "attribute onclick"),
    ("@victim", "live mention (safe rendering)"),
    ("x[^1]\n\n[^1]: x\n    @victim", "live mention (footnotes rendering)"),
    ("&#64;victim", "live mention (safe rendering)"),
    ("> - @victim", "live mention (safe rendering)"),
    ("`@victim`", "live mention (safe rendering)"),
    ("<div>&amp@victim</div>", "live mention (probe rendering)"),
    ("```suggestion\nx\n```", "an applicable suggestion pr-comment did not write"),
    ("```suggestion:-0+0\nx\n```", "an applicable suggestion pr-comment did not write"),
]


@pytest.mark.parametrize(("markdown", "expected"), BAD_BODIES)
def test_the_oracle_flags_what_it_must(markdown: str, expected: str) -> None:
    assert expected in violations(markdown + "\n\n" + MARKER)


def test_the_oracle_flags_a_missing_marker_and_a_forgectl_change() -> None:
    assert "the marker is not the last line" in violations("text")
    assert "forgectl would change the body" in violations("/ping\n\n" + MARKER)


def test_the_oracle_flags_an_attribution_that_is_not_first() -> None:
    expect = Expect("Posted by `alice`", has_rule=True)
    assert violations("Posted by `alice`\n\n---\n\nx\n\n" + MARKER, expect) == []
    assert "the attribution line is not the first paragraph" in violations(
        "x\n\nPosted by `alice`\n\n---\n\n" + MARKER, expect
    )
    assert "the attribution line is not the first paragraph" in violations(
        "<!--x-->\n\nPosted by `alice`\n\n" + MARKER, Expect("Posted by `alice`")
    )


def test_the_oracle_accepts_an_applicable_suggestion_only_as_written() -> None:
    expect = Expect(suggestion=(("a < b", "@x"), "suggestion"))
    body = "**major**\n\n```suggestion\na < b\n@x\n```\n\n" + MARKER
    assert violations(body, expect) == []
    assert violations(body.replace("a < b", "a < c"), expect) != []
    assert violations(body.replace("```suggestion", "```suggestion:-1+0"), expect) != []
    twice = body.replace(MARKER, "```suggestion\na < b\n@x\n```\n\n" + MARKER)
    assert violations(twice, expect) != []


def test_the_oracle_flags_a_processing_instruction_that_swallows_the_marker() -> None:
    # The block runs to the end, so the marker is no comment of its own and the omitted count
    # still matches: only the instruction check can see it.
    assert "raw HTML processing instruction" in violations("<?php\nx\n\n" + MARKER)


def test_the_oracle_exempts_only_the_exact_written_suggestion_block() -> None:
    expect = Expect(suggestion=(("@victim",), "suggestion"))
    # swallowed by an open fence of the body: the written block is the tail of the code block;
    # no live mention, but since #88 the pipeline never writes an applicable block there
    swallowed = "```\nopen\n\n```suggestion\n@victim\n```\n\n" + MARKER
    assert violations(swallowed, expect) == [
        "an applicable suggestion was swallowed by an open fence"
    ]
    # the body's own line equal to a suggestion line is a live mention
    forged = "```\n@victim\n\n```suggestion\n@victim\n```\n\n" + MARKER
    assert "live mention (safe rendering)" in violations(forged, expect)


def test_the_oracle_exempts_only_the_swallowing_node_not_an_earlier_copy() -> None:
    expect = Expect(suggestion=(("@victim",), "suggestion"))
    earlier = "~~~suggestion\n@victim\n~~~\n\n```\nopen\n\n```suggestion\n@victim\n```\n\n" + MARKER
    assert "live mention (safe rendering)" in violations(earlier, expect)
    # the copy is not at the end of the node: other `@` text follows it
    inside = "```\nopen\n\n```suggestion\n@victim\n```\n@other\n\n" + MARKER
    assert "live mention (safe rendering)" in violations(inside, expect)


def test_the_oracle_flags_a_marker_of_pr_comment_that_is_not_visible() -> None:
    # An ASCII `<U+202E>` outside code is dropped from view; inside code it is shown.
    assert violations("`<U+202E>`\n\n" + MARKER) == []
    assert "a marker of pr-comment is not visible in code" in violations(
        "x <U+202E> y\n\n" + MARKER
    )


def test_the_oracle_decodes_text_the_html5_way() -> None:
    # cmark copies a raw HTML block verbatim; a parser decodes `&amp@x` to `&@x`.
    page = parse(render_probe("<div>&amp@victim</div>"))
    assert live_mentions(page) == ["&@victim"]
    assert live_mentions(parse(render_safe("<div>&amp@victim</div>"))) == []


def test_a_node_start_counts_as_a_non_letter() -> None:
    # `a` ends a text node, `@x` starts the next: a host's per-node mention filter sees `@x`.
    assert live_mentions(parse("<p>a<em>@x</em></p>")) == ["@x"]
    assert live_mentions(parse("<p>a@x.test</p>")) == []


# --- golden cases over the whole pipeline -------------------------------------------------------


@dataclass
class Case:
    name: str
    document: dict[str, Any] | None = None
    text: str | None = None  # a body file for comment, reply and approve
    options: dict[str, Any] = field(default_factory=dict)
    refused: str | None = None  # "<kind> <pointer>" the pipeline must refuse with


def review(
    name: str,
    *,
    body: str = "",
    findings: list[dict[str, Any]] | None = None,
    unanchored: list[dict[str, Any]] | None = None,
    refused: str | None = None,
    **options: Any,
) -> Case:
    document = make_document(body=body, findings=findings or [], unanchored=unanchored or [])
    return Case(name, document, options=options, refused=refused)


def everywhere(name: str, text: str) -> list[Case]:
    """The same hostile text as the draft body, as an inline finding's body (with an applicable
    suggestion after it) and as a moved finding's body (with a suggestion shown as code)."""
    suggestion = {"lines": ["x = 1"]}
    return [
        review(f"{name}/draft_body", body=text),
        review(
            f"{name}/inline_body",
            findings=[make_finding(body=text, severity="minor", suggestion=suggestion)],
        ),
        review(
            f"{name}/moved_body",
            unanchored=[make_unanchored(make_finding(body=text, suggestion=suggestion))],
        ),
    ]


def lines(*parts: str) -> str:
    return "\n".join(parts)


# The three divergence classes of review pass 2, and the tilde case.
DIVERGENCE: list[Case] = [
    *everywhere("definition_then_indented_mention", "[a]: /u\n    @victim"),
    *everywhere("table_rowless_pipe_then_indented_mention", "| a |\n|---|\n| b |\n|\n    @victim"),
    *everywhere("quote_then_tab_quote_fence_mention", "> x\n\t> ```\n@victim"),
    *everywhere("open_tilde_fence", "~~~\n@victim"),
    review(
        "tilde_fence_then_suggestion_tilde_line",
        findings=[make_finding(body="~~~\nx", suggestion={"lines": ["~~~", "@victim"]})],
    ),
    review(
        "tilde_fence_then_suggestion_tilde_line_moved",
        unanchored=[make_unanchored(make_finding(body="~~~\nx", suggestion={"lines": ["~~~"]}))],
    ),
    review(
        "open_fence_in_an_earlier_part_then_moved_suggestion",
        body="```\nopen",
        unanchored=[
            make_unanchored(make_finding(body="fine", suggestion={"lines": ["```", "@victim"]}))
        ],
    ),
]

# Body structure (D-22).
STRUCTURE: list[Case] = [
    *everywhere("open_fence", "```\nopen"),
    *everywhere("html_comment", "<!-- hidden"),
    *everywhere("details", "<details><summary>s</summary>hidden"),
    *everywhere("image", "![](https://t.example/pic)"),
    *everywhere("open_mermaid_fence", "```mermaid\ngraph TD; A-->B"),
    *everywhere("open_tilde_mermaid_fence", "~~~mermaid\ngraph TD; A-->B"),
    *everywhere("open_quote_and_list", "> - ```\n>   open"),
    *everywhere("indented_tag_after_definition", "[r]: /u\n  <details>x\n  <img src=x>"),
    review(
        "reference_image_split_across_findings",
        findings=[
            make_finding("f1", line=None, body="[r]: https://t.example/pic"),
            make_finding("f2", line=None, body="![t][r]"),
        ],
    ),
    review(
        "reference_image_split_across_body_and_moved_finding",
        body="![t][r]",
        unanchored=[make_unanchored(make_finding(body="[r]: https://t.example/pic"))],
    ),
    review(
        "definition_then_tags_in_two_findings",
        findings=[
            make_finding("f1", line=None, body="[r]: /u\n  <details>x"),
            make_finding("f2", line=None, body="<img src=x>"),
        ],
    ),
    review(
        "path_with_backticks_and_a_link",
        findings=[make_finding(line=None, path="a`b``c [x](u).py", body="x")],
    ),
    review(
        "path_with_bidi_and_zero_width_markers",
        findings=[make_finding(line=None, path="src/\u202ea\u200bb.py", body="x")],
    ),
    review(
        "title_with_bold_and_backslash_n",
        findings=[make_finding(severity="major", title="x** \\n *y* [z](u) <b>", body="Body.")],
    ),
    review(
        "suggestion_stays_applicable_after_a_closed_body",
        findings=[
            make_finding(
                severity="minor", body="```\ncode\n```\n\ntext", suggestion={"lines": ["x = 1"]}
            )
        ],
    ),
    review(
        "suggestion_on_gitlab_with_a_range",
        findings=[make_finding(start_line=8, body="Body.", suggestion={"lines": ["a", "@b"]})],
        host="gitlab",
    ),
    review(
        "degraded_suggestion_kinds",
        findings=[
            make_finding("f1", body="a", suggestion={"lines": ["x\u202ey"]}),
            make_finding("f2", body="b", suggestion={"lines": ["```", "<img src=x>"]}),
            make_finding("f3", body="c", suggestion={"lines": ["/ping"]}),
            make_finding("f4", body="d", suggestion={"lines": ["forgectl:dedupe:x"]}),
        ],
    ),
    review(
        "moved_suggestion_with_a_long_fence",
        unanchored=[
            make_unanchored(make_finding(body="```\nopen", suggestion={"lines": ["`" * 300]}))
        ],
    ),
]

# Spike B: entity forms, backslashes, stray backticks, line separators.
SPIKE_B: list[Case] = [
    *everywhere("entity_decimal", "&#64;victim and &#064;victim and &#64victim"),
    *everywhere("entity_hex_and_named", "&#x40;victim &#X40;victim &commat;victim &commat victim"),
    *everywhere("entity_tail_in_raw_block", "<div>&amp@octocat</div>\n\n<p>&#38@octocat"),
    *everywhere("entity_tail_in_prose", "R&D@corp &amp@octocat &#x26@octocat"),
    *everywhere("backslash_at", "\\@victim and \\\\@victim and @\\_name"),
    *everywhere("stray_backticks", "a`@victim and ``@victim` and `\n@victim\n`"),
    *everywhere("code_spans_and_fences", "`@victim`\n\n```\n@victim\n```\n\n    @victim"),
    *everywhere("line_separators", "a\u2028```\u2028@victim\u2029@victim\x0b@victim\x85@victim"),
    *everywhere("quote_with_vt_and_nel", "> x\x0b\t> ```\x85@victim"),
    *everywhere("carriage_returns", "x\r```\r@victim\r\n@victim"),
    *everywhere("fullwidth_forms", "＠victim ＜b> ！[x](u)"),
    *everywhere(
        "emphasis_and_links",
        "*@victim* [@victim](https://example.org/@victim) www.example.org/@victim",
    ),
    *everywhere("tables_and_lists", "- @victim\n1. @victim\n| a |\n|---|\n| @victim |"),
    *everywhere("headings_and_rules", "# @victim\n---\n@victim\n==="),
    *everywhere("footnote_definition", "x[^1]\n\n[^1]: x\n    @victim"),
    *everywhere("autolinks", "https://example.org/@victim <https://example.org> user@example.org"),
]

# Refusals: a suggestion fence in untrusted text exits 2 and builds no body (Text pipeline step 2).
REFUSED: list[Case] = [
    review(
        "suggestion_fence_in_a_body",
        body="```suggestion\nx\n```",
        refused="suggestion_fence /body",
    ),
    review(
        "suggestion_fence_with_an_entity_info_string",
        body="```&#115;uggestion\nx\n```",
        refused="suggestion_fence /body",
    ),
    review(
        "suggestion_fence_in_a_quote",
        findings=[make_finding(body="> ~~~ suggestion\n> x")],
        refused="suggestion_fence /findings/0/body",
    ),
    review(
        "suggestion_fence_in_a_suggestion_line",
        findings=[make_finding(body="~~~\nx", suggestion={"lines": ["```suggestion"]})],
        refused="suggestion_fence /findings/0/suggestion/lines/0",
    ),
    review(
        "control_character_in_a_body",
        body="x\x1b[31m",
        refused="control_character /body",
    ),
]

ATTRIBUTION: list[Case] = [
    review("auto_actor_named_like_a_mention", body="ok", actor="auto:@all"),
    review("human_actor_with_dots", body="ok", actor="human:www.example.org"),
    review("no_human_footer_untrusted_text_first", body="@victim", footer_human=False),
    review(
        "footer_with_every_placeholder",
        body="ok",
        footer_human="Posted by {user} {policy} {sha} {decision_id} v{tool_version} {{braces}}",
        decision_id="d-1",
    ),
]

COMMANDS: list[Case] = [
    Case("comment_hostile", text="<!-- x -->\n![](u)\n@victim\n```\nopen"),
    Case("comment_footer_off", text="[a]: /u\n    @victim", options={"footer_human": False}),
    Case("comment_empty", text=""),
    Case("approve_body_with_slash_lines", text="/approve\n/assign @victim"),
    Case("comment_refused_fence", text="```suggestion\nx\n```", refused="suggestion_fence /body"),
]

GROUPS: dict[str, list[Case]] = {
    "divergence_classes": DIVERGENCE,
    "body_structure": STRUCTURE,
    "spike_b_and_review_passes": SPIKE_B,
    "refusals": REFUSED,
    "attribution": ATTRIBUTION,
    "commands": COMMANDS,
}


def run_case(case: Case) -> list[Posted]:
    if case.text is not None:
        return run_text_command(case.text, **case.options)
    assert case.document is not None
    return run_review(case.document, **case.options)


TEXT_KEYS = ("body", "findings", "unanchored")


def report(case: Case) -> str:
    # only the fields that carry text; the anchor, ids and event are the same in every case
    shown = {k: v for k, v in (case.document or {}).items() if k in TEXT_KEYS}
    source = draft_json(shown) if case.document is not None else repr(case.text)
    header = f"== {case.name}\n-- input ({case.options or 'default options'})\n{source}\n"
    try:
        posted = run_case(case)
    except Refused as err:
        return header + f"-- refused: {err.kind} {err.pointer}\n"
    return header + render_report(posted)


def case_ids(cases: list[Case]) -> list[str]:
    return [case.name for case in cases]


ALL_CASES = [case for cases in GROUPS.values() for case in cases]


def test_case_names_are_unique() -> None:
    names = [case.name for case in ALL_CASES]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("case", ALL_CASES, ids=case_ids(ALL_CASES))
def test_every_golden_body_passes_the_oracle(case: Case) -> None:
    if case.refused is not None:
        with pytest.raises(Refused) as caught:
            run_case(case)
        assert f"{caught.value.kind} {caught.value.pointer}" == case.refused
        return
    posted = run_case(case)
    assert posted
    for item in posted:
        assert violations(item.text, item.expect) == [], (case.name, item.label, item.text)
        assert has_marker(item.text)


@pytest.mark.parametrize("group", sorted(GROUPS))
def test_golden_files(group: str, golden: Callable[[str, str], None]) -> None:
    golden(f"text_safety/{group}.txt", "\n".join(report(case) for case in GROUPS[group]))


def test_golden_files_hold_placeholder_names_only() -> None:
    for cases in GROUPS.values():
        for case in cases:
            assert find_real_names(report(case)) == [], case.name


def test_golden_drafts_validate_against_the_schema() -> None:
    # Only the input drafts are validated here. todo(#27): validate the posting command's --json
    # output against schemas/ once the plan builder lands; no command emits it yet.
    # control_character_in_a_body is exempt because its body holds a control character that the
    # draft schema itself forbids: the case exists to show the pipeline refuses it.
    schemas = load_schemas()
    schema = next(s for s in schemas.values() if s.get("$id", "").endswith("review-draft.json"))
    validator = jsonschema.Draft202012Validator(schema, registry=build_registry())
    for case in ALL_CASES:
        if case.document is not None and case.name != "control_character_in_a_body":
            errors = list(validator.iter_errors(case.document))
            assert errors == [], (case.name, [e.message for e in errors])


# --- properties of particular cases -------------------------------------------------------------


def only(case_name: str) -> list[Posted]:
    (case,) = [c for c in ALL_CASES if c.name == case_name]
    return run_case(case)


def suggestion_blocks(item: Posted) -> list[tuple[str, str]]:
    return [
        (lang, text) for lang, text in parse(render_safe(item.text)).pres if lang.startswith("sug")
    ]


def test_a_suggestion_after_a_closed_body_stays_applicable() -> None:
    review_body, inline = only("suggestion_stays_applicable_after_a_closed_body")
    assert suggestion_blocks(review_body) == []
    assert suggestion_blocks(inline) == [("suggestion", "x = 1\n")]
    assert inline.expect.suggestion == (("x = 1",), "suggestion")


def test_a_gitlab_range_suggestion_names_its_offset() -> None:
    _, inline = only("suggestion_on_gitlab_with_a_range")
    assert suggestion_blocks(inline) == [("suggestion:-2+0", "a\n@b\n")]


def test_every_degraded_suggestion_is_code_that_is_not_applicable() -> None:
    review_body, *inline = only("degraded_suggestion_kinds")
    assert suggestion_blocks(review_body) == []
    for item in inline:
        assert suggestion_blocks(item) == []
        assert "_Suggestion shown as code" in item.text


def test_an_ascii_marker_is_visible_in_the_rendering() -> None:
    (body,) = only("path_with_bidi_and_zero_width_markers")[:1]
    page = parse(render_safe(body.text))
    shown = [n.text for n in page.nodes if n.in_code and "<U+" in n.text]
    assert shown == ["src/<U+202E>a<U+200B>b.py"]


def test_the_open_fence_cases_leave_the_attribution_first_and_the_marker_visible() -> None:
    for name in ("open_fence/draft_body", "open_mermaid_fence/draft_body"):
        (body,) = only(name)
        html_out = render_safe(body.text)
        assert html_out.startswith("<p>Drafted with AI assistance")
        # swallowed by the open fence: the marker line shows as code, not as a hidden comment
        assert "&lt;!-- pr-comment:v1 --&gt;" in html_out


def test_a_closed_body_leaves_the_marker_a_hidden_comment() -> None:
    (body,) = only("suggestion_stays_applicable_after_a_closed_body")[:1]
    assert render_probe(body.text).rstrip().endswith("<!-- pr-comment:v1 -->")


def test_forgectl_finds_nothing_left_to_neutralize_in_any_body() -> None:
    """The fake forgectl's report for pr-comment's output: 0 lines and 0 markers."""
    for case in ALL_CASES:
        if case.refused is None:
            for item in run_case(case):
                again = forgectl_neutralize(item.text)
                assert (again.text, again.neutralized_lines, again.markers_removed) == (
                    item.text,
                    0,
                    0,
                ), case.name


def test_the_pre_applied_neutralizing_is_counted() -> None:
    (body,) = run_text_command("/ping\nforgectl:dedupe:x")
    assert (body.body.neutralized_lines, body.body.markers_removed) == (1, 1)
    assert " /ping" in body.text and "forgectl:dedupe-x" in body.text


def test_a_footer_that_would_start_a_list_is_refused_at_run_time() -> None:
    from pr_comment.config import ConfigError

    with pytest.raises(ConfigError):
        run_review(make_document(body="x"), footer_human="{decision_id}1. x")


def test_the_report_names_the_posted_body_and_the_rendering() -> None:
    text = report(review("one", body="@victim"))
    assert "＠victim" in text and "@victim" not in text.split("-- review")[1]
    assert "-- cmark-gfm" in text


def test_golden_text_has_no_live_ascii_at_outside_applicable_suggestions() -> None:
    # The oracle already enforces it per body; here on the raw golden text of the refusal-free
    # review bodies, ignoring the cmark section and the pr-comment marker line.
    for case in ALL_CASES:
        if case.refused is not None or case.document is None:
            continue
        for item in run_case(case):
            if item.expect.suggestion is None:
                assert not re.search(r"(?<![A-Za-z0-9])@", item.text), (case.name, item.label)


# --- open decisions: the current behavior, written down so it can change with the decision ------


def test_decision_1_ordinary_and_javascript_links_count_zero() -> None:
    """characterizes open decision 1: ``counts.links`` counts 0 for an ordinary link and a
    ``javascript:`` link. A decision may count them; change this test with it."""
    for text in ("[x](u)", "[x](/relative/path)", "[x](javascript:alert(1))", "[x]: javascript:x"):
        assert count_text(text).links == 0, text
    # a scheme with `//` or a `www.` form does count, which is why the decision is open
    assert count_text("[x](https://example.org)").links == 1


def test_decision_1_cmark_gfm_drops_the_javascript_href_itself() -> None:
    """characterizes open decision 1: cmark-gfm renders ``[x](javascript:alert(1))`` as a link
    with an empty href (an engine fact, not pr-comment's); the count stays 0."""
    page = parse(render_safe("[x](javascript:alert(1))"))
    assert [t for t in page.tags if t[0] == "a"] == [("a", {"href": ""})]


def test_a_suggestion_after_an_open_body_fence_is_degraded_and_reported() -> None:
    """issue #88: a suggestion that a fence left open by the body would swallow is written as a
    ``text`` block (never an applicable block) and reported as ``open_fence``."""
    (case,) = [c for c in ALL_CASES if c.name == "open_fence/inline_body"]
    review_body, inline = run_case(case)
    assert suggestion_blocks(inline) == []
    code = [text for lang, text in parse(render_safe(inline.text)).pres]
    # the body's open fence ends at the degraded block's own closing fence
    assert len(code) == 1 and code[0].endswith("```text\nx = 1\n")
    assert case.document is not None
    result = render_inline_finding(
        case.document["findings"][0], MentionNeutralizer(), host="github"
    )
    assert result.degraded == "open_fence"
    assert "x = 1" in code[0]


def test_decision_3_a_fullwidth_at_passes_unchanged() -> None:
    """characterizes open decision 3: ``＠`` (U+FF20) is pr-comment's own replacement, so text
    that already holds it is not touched, counted or reported; a host that folded it to ASCII
    would make it live (live test, issue #39)."""
    neutralizer = MentionNeutralizer()
    assert neutralizer.neutralize("＠victim ＠all") == "＠victim ＠all"
    assert (neutralizer.count, neutralizer.names) == (0, ())
    (body,) = run_text_command("＠victim")
    assert "＠victim" in body.text
    assert violations(body.text, body.expect) == []


# --- findings of the fuzz about earlier modules (reported, not safety holes) --------------------


@pytest.mark.parametrize("blank", ["\n", "\n\n", "\r\n", "\u2028", " \t", "\x85 "])
def test_finding_a_blank_body_is_refused_so_no_marker_only_comment_posts(blank: str) -> None:
    """Issue #67: ``build_inline`` drops trailing line feeds, so a body with no non-blank
    character would post as the bare hidden marker. Draft validation refuses it instead, for a
    finding and for an ``unanchored`` finding, at the body pointer."""
    with pytest.raises(Refused) as caught:
        run_review(make_document(findings=[make_finding(body=blank)]))
    assert (caught.value.kind, caught.value.pointer) == ("invalid_draft", "/findings/0/body")
    unanchored = make_document(unanchored=[make_unanchored(make_finding(body=blank))])
    with pytest.raises(Refused) as caught:
        run_review(unanchored)
    assert (caught.value.kind, caught.value.pointer) == (
        "invalid_draft",
        "/unanchored/0/finding/body",
    )
