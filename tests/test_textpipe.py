"""Text pipeline steps 1 and 2 on one text (docs/interface.md: Text pipeline).

The inputs are hostile on purpose: control characters, lone surrogates, every line separator,
fence and tilde runs of every length, quotes and list items around a fence, entity and backslash
disguises, and huge texts.
"""

from __future__ import annotations

import time

import pytest

from pr_comment.textpipe import (
    CONTROL_CHARACTER,
    SUGGESTION_FENCE,
    normalize_lines,
    text_problem,
)

SEPARATORS = ["\n", "\r\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "]


@pytest.mark.parametrize("separator", SEPARATORS)
def test_every_separator_becomes_lf(separator: str) -> None:
    assert normalize_lines(f"a{separator}b") == "a\nb"


def test_crlf_is_one_line_break_and_lone_cr_is_another() -> None:
    assert normalize_lines("a\r\nb\rc\n\rd") == "a\nb\nc\n\nd"


def test_ls_vt_and_nel_become_line_breaks() -> None:
    assert normalize_lines("a\u2028b\x0bc\x85d") == "a\nb\nc\nd"


def test_normalize_leaves_tab_and_plain_text() -> None:
    assert normalize_lines("a\tb é 😀") == "a\tb é 😀"


@pytest.mark.parametrize("separator", SEPARATORS)
def test_separators_are_not_control_characters(separator: str) -> None:
    assert text_problem(f"a{separator}b") is None


def test_tab_and_lf_are_allowed() -> None:
    assert text_problem("a\tb\nc") is None


@pytest.mark.parametrize(
    "char",
    [
        "\x00",
        "\x01",
        "\x08",
        "\x0e",
        "\x1b",
        "\x1f",
        "\x7f",
        "\x80",
        "\x84",
        "\x86",
        "\x9f",
        "\ud800",
        "\udbff",
        "\udc00",
        "\udfff",
    ],
)
def test_control_characters_and_lone_surrogates_are_refused(char: str) -> None:
    assert text_problem(f"before{char}after") == CONTROL_CHARACTER
    assert text_problem(char) == CONTROL_CHARACTER


@pytest.mark.parametrize("char", ["‮", "​", "⁦", "﻿", "\U0001f600", " "])
def test_other_unicode_is_not_this_steps_business(char: str) -> None:
    # Bidi and invisible characters are marked or counted later (display, degraded suggestions).
    assert text_problem(f"a{char}b") is None


def test_a_control_character_wins_over_a_fence() -> None:
    assert text_problem("```suggestion\x00") == CONTROL_CHARACTER


def test_empty_text_is_fine() -> None:
    assert text_problem("") is None


# --- suggestion fences -------------------------------------------------------------------------


@pytest.mark.parametrize("run", ["```", "````", "`" * 40, "~~~", "~~~~", "~" * 40])
@pytest.mark.parametrize("info", ["suggestion", "suggestion:-1+0", "Suggestion", "SUGGESTION"])
def test_fences_of_every_length_are_refused(run: str, info: str) -> None:
    assert text_problem(f"{run}{info}\ncode\n{run}") == SUGGESTION_FENCE
    assert text_problem(f"{run} {info}") == SUGGESTION_FENCE
    assert text_problem(f"{run}   \t{info}") == SUGGESTION_FENCE


@pytest.mark.parametrize(
    "line",
    [
        "> ```suggestion",
        "- ~~~suggestion",
        "1. > - ```suggestion",
        ">>> ```suggestion",
        "    ```suggestion",
        "text before ```suggestion",
        "* + - ~~~~suggestion",
        "```` ```suggestion",
    ],
)
def test_fences_inside_quotes_lists_and_prose_are_refused(line: str) -> None:
    assert text_problem(line) == SUGGESTION_FENCE
    assert text_problem(f"first line\n{line}\nlast") == SUGGESTION_FENCE


@pytest.mark.parametrize(
    "info",
    [
        "&#115;uggestion",
        "&#x73;uggestion",
        "&#X73;uggestion",
        "&#0000115;uggestion",
        "&#32;suggestion",
        "&Tab;suggestion",
        "&NewLine;suggestion",
        "\\suggestion",
        "s\\uggestion",
        "\\s\\u\\g\\g\\e\\s\\t\\i\\o\\n",
        "&#92;suggestion",
        "&#115;&#117;&#103;&#103;&#101;&#115;&#116;&#105;&#111;&#110;",
        " suggestion",
        "　suggestion",
        "suggestion‮",
    ],
)
def test_fences_written_with_entities_backslashes_and_spaces_are_refused(info: str) -> None:
    assert text_problem(f"```{info}") == SUGGESTION_FENCE
    assert text_problem(f"~~~ {info}") == SUGGESTION_FENCE


def test_the_text_after_the_run_is_taken_whatever_precedes_it() -> None:
    assert text_problem("suggestion ```suggestion") == SUGGESTION_FENCE
    assert text_problem("x```y```suggestion") == SUGGESTION_FENCE
    assert text_problem("```text\n```suggestion") == SUGGESTION_FENCE
    assert text_problem("``````suggestion") == SUGGESTION_FENCE
    assert text_problem("```~~~suggestion") == SUGGESTION_FENCE


def test_a_fence_opener_after_a_line_separator_is_a_fence_line() -> None:
    for separator in SEPARATORS:
        assert text_problem(f"ok{separator}```suggestion") == SUGGESTION_FENCE


@pytest.mark.parametrize(
    "text",
    [
        "suggestion",
        "``suggestion",
        "~~suggestion",
        "`suggestion`",
        "` ` `suggestion",
        "```text",
        "```diff\n+ suggestion\n```",
        "~~~python",
        "``` not a suggestion",
        "```suggest",
        "```suggestio",
        "```sugg estion",
        "```sugges\ntion",
        "```\nsuggestion",
        "~~~\nsuggestion",
        "``​`suggestion",
        "`\\``suggestion",
        "&#96;&#96;&#96;suggestion",
        "&grave;&grave;&grave;suggestion",
        "```;suggestion",
        "```x suggestion",
        "run of fences ``` and then ``` end",
    ],
)
def test_ordinary_text_with_fences_and_the_word_is_accepted(text: str) -> None:
    assert text_problem(text) is None


def test_the_run_must_be_one_character_kind() -> None:
    assert text_problem("`~`suggestion") is None


def test_the_search_matches_each_line_and_no_state_leaks_between_lines() -> None:
    assert text_problem("```\n```suggestion\n") == SUGGESTION_FENCE
    assert text_problem("```\n\n```\n") is None


def test_forms_that_degrade_later_are_not_this_step_s_refusal() -> None:
    # A fence in code is allowed; only a suggestion fence is refused here.
    assert text_problem("```python\nprint(1)\n```\n~~~\nx\n~~~") is None


def test_a_huge_line_of_runs_is_linear() -> None:
    text = "``` " * 16000 + "x"
    start = time.process_time()
    assert text_problem(text) is None
    assert time.process_time() - start < 10


def test_a_huge_line_of_entities_before_the_word_is_linear() -> None:
    text = "```" + "&#32;" * 12000 + "suggestion"
    start = time.process_time()
    assert text_problem(text) == SUGGESTION_FENCE
    assert time.process_time() - start < 10


def test_many_lines_are_linear() -> None:
    text = "```text\n" * 8000
    start = time.process_time()
    assert text_problem(text) is None
    assert time.process_time() - start < 10


def test_a_huge_text_without_fences() -> None:
    assert text_problem("a" * 1_000_000) is None
    assert text_problem("a" * 1_000_000 + "\x00") == CONTROL_CHARACTER
