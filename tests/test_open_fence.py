"""A suggestion that an open fence of its own body would swallow degrades as ``open_fence``
(docs/interface.md: Suggestions; issue #88). The cmark-gfm oracle decides what is live."""

from __future__ import annotations

import random

import pytest

from pr_comment.render import open_fence_possible, render_finding_text, render_suggestion
from tests.test_render import cmark, finding, mn
from tests.test_render_suggestions import pre_blocks, sug

SENTINEL = "SENTINEL_LINE"


def swallowed(body: str) -> bool:
    """True when cmark-gfm does not show an applicable block after ``body`` (a fence is open)."""
    page = pre_blocks(body + "\n\n```suggestion\n" + SENTINEL + "\n```")
    return not any(lang == "suggestion" and text == SENTINEL + "\n" for lang, text in page)


CLOSED = [
    "",
    "plain text",
    "x\n```\ncode\n```",
    "x\n```py\ncode\n```\nmore",
    "~~~\ncode\n~~~",
    "````\n```\nnested\n```\n````",
    "~~~\n```\n~~~",
    "```\n~~~\n```",
    "   ```\ncode\n   ```",
    "```\ncode\n`````",
    "``` \ncode\n```  ",
    "```\n```\n```\n```",
    "inline ```code``` here",
    "use ```x``` and ~~~y~~~",
    "a\n\n```\n\n```\n\nb",
    "```é\ncode\n```",
]

OPEN = [
    "```",
    "```\nopen",
    "x\n~~~\nopen",
    "````\n```\nstill open",
    "```\ncode\n``",
    "```\ncode\n~~~",
    "~~~\ncode\n```",
    "```py\ncode\n``` x",
    "   ```\ncode",
    "```\n```\n```",
]


@pytest.mark.parametrize("body", CLOSED)
def test_a_closed_body_is_not_open_and_cmark_agrees(body: str) -> None:
    assert not swallowed(body)
    assert not open_fence_possible(body)


@pytest.mark.parametrize("body", OPEN)
def test_an_open_body_is_open_and_cmark_agrees(body: str) -> None:
    assert swallowed(body)
    assert open_fence_possible(body)


@pytest.mark.parametrize(
    "body",
    [
        "> ```\n> code\n> ```",
        "- ```\n  code\n  ```",
        "1. ```\n   code\n   ```",
        "    ```",
        "\t```",
        "> ```\nopen",
        "```\n> ```",
    ],
)
def test_a_fence_run_inside_a_container_or_an_indent_is_conservatively_possible(
    body: str,
) -> None:
    assert open_fence_possible(body)


def test_a_backtick_fence_with_a_backtick_in_its_info_string_is_no_fence() -> None:
    assert not swallowed("```a`b\nx")
    assert not open_fence_possible("```a`b\nx")


def test_a_tilde_fence_may_have_backticks_in_its_info_string() -> None:
    assert swallowed("~~~a`b\nx")
    assert open_fence_possible("~~~a`b\nx")


@pytest.mark.parametrize("terminator", ["\r\n", "\r", " ", "\x0b", "\x85"])
def test_every_line_separator_splits_lines(terminator: str) -> None:
    body = "```" + terminator + "x"
    assert open_fence_possible(body)


def test_no_fence_run_is_never_open() -> None:
    assert not open_fence_possible("``\n~~\n`code`\n")


def test_open_body_degrades_the_suggestion_with_the_reason_and_the_text_block() -> None:
    f = sug("new", body="```\nopen")
    shown = render_suggestion(f, mn(), host="github")
    assert shown is not None
    assert shown.degraded == "open_fence"
    assert shown.block.startswith(
        "_Suggestion shown as code: it cannot be posted as an applicable suggestion safely._\n\n"
        "```text\nnew\n```"
    )
    langs = [
        lang for lang, _ in pre_blocks(render_finding_text(f, mn(), suggestion_block=shown.block))
    ]
    assert "suggestion" not in langs


def test_a_closed_body_keeps_the_suggestion_applicable() -> None:
    f = sug("new", body="x\n```py\ncode\n```")
    shown = render_suggestion(f, mn(), host="github")
    assert shown is not None
    assert shown.degraded is None
    assert shown.block == "```suggestion\nnew\n```"


def test_a_body_without_a_fence_run_is_untouched() -> None:
    f = sug("new", body="b")
    shown = render_suggestion(f, mn(), host="github")
    assert shown is not None and shown.degraded is None


def test_an_earlier_reason_wins_over_open_fence() -> None:
    f = sug("```", body="```\nopen")
    shown = render_suggestion(f, mn(), host="github")
    assert shown is not None and shown.degraded == "fence"


def test_a_later_reason_does_not_hide_open_fence() -> None:
    # open_fence is last in the order: dedupe_text comes first.
    f = sug("forgectl:dedupe:x", body="```\nopen")
    shown = render_suggestion(f, mn(), host="github")
    assert shown is not None and shown.degraded == "dedupe_text"


def test_a_moved_suggestion_is_not_reported() -> None:
    f = sug("new", body="```\nopen")
    shown = render_suggestion(f, mn(), host="github", moved=True)
    assert shown is not None and shown.degraded is None


def test_no_body_means_not_open() -> None:
    f = finding(suggestion={"lines": ["new"]})
    shown = render_suggestion(f, mn(), host="github")
    assert shown is not None and shown.degraded is None


# --- differential fuzz against cmark-gfm ---------------------------------------------------

CLEAN_LINES = [
    "```",
    "~~~",
    "````",
    "~~~~",
    "```py",
    "~~~ info `x`",
    "``` x",
    "```a`b",
    "``",
    "~~",
    "text",
    "",
    "  ```",
    "   ~~~",
    "text ```x```",
    "```  ",
    "`````",
    "~~~~~",
]
HARD_LINES = [
    "> ```",
    "> ~~~",
    "- ```",
    "1. ~~~",
    "    ```",
    "\t~~~",
    "> text",
    "- item",
    "    code",
    "a ```b",
]


def _bodies(pool: list[str], seed: int, count: int) -> list[str]:
    rng = random.Random(seed)
    return ["\n".join(rng.choice(pool) for _ in range(rng.randint(1, 7))) for _ in range(count)]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_fuzz_plain_top_level_lines_match_cmark_exactly(seed: int) -> None:
    for body in _bodies(CLEAN_LINES, seed, 250):
        assert open_fence_possible(body) == swallowed(body), body


@pytest.mark.parametrize("seed", [4, 5, 6])
def test_fuzz_any_line_is_never_applicable_while_swallowed(seed: int) -> None:
    for body in _bodies(CLEAN_LINES + HARD_LINES, seed, 250):
        if swallowed(body):
            assert open_fence_possible(body), body


def test_the_oracle_itself_sees_a_live_block_after_a_closed_body() -> None:
    assert "suggestion" in cmark("x\n\n```suggestion\ny\n```")
