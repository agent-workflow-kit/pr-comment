"""Text pipeline steps 1 and 2 on the text fields of a draft (invalid_text).

Refusals carry fixed text, a pointer and a kind, and never any draft content.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from pr_comment.draft import DraftError, parse_draft, text_issues
from pr_comment.textpipe import text_problem
from tests.test_draft import CANARY, good

Doc = dict[str, Any]


def dumps(doc: object) -> bytes:
    return json.dumps(doc).encode("utf-8")


def refusal(doc: Doc) -> DraftError:
    with pytest.raises(DraftError) as info:
        parse_draft(dumps(doc))
    return info.value


def with_unanchored(doc: Doc) -> Doc:
    # good() already has one unanchored entry (id u1).
    assert doc["unanchored"][0]["finding"]["id"] == "u1"
    return doc


def test_a_clean_draft_loads_and_keeps_its_text_unchanged() -> None:
    doc = good()
    doc["body"] = "line one\r\nline two tab\there"
    assert parse_draft(dumps(doc)).data["body"] == doc["body"]


@pytest.mark.parametrize(
    ("setup", "pointer"),
    [
        (lambda d: d.update(body=f"{CANARY}\x00"), "/body"),
        (lambda d: d["findings"][0].update(body=f"{CANARY}\ud800"), "/findings/0/body"),
        (lambda d: d["findings"][0].update(path=f"a/{CANARY}\x1b"), "/findings/0/path"),
        (lambda d: d["findings"][1].update(title=f"{CANARY}\x7f"), "/findings/1/title"),
        (
            lambda d: d["findings"][1].update(suggestion={"lines": ["ok", f"{CANARY}\x00"]}),
            "/findings/1/suggestion/lines/1",
        ),
        (
            lambda d: with_unanchored(d)["unanchored"][0]["finding"].update(body="a\x00"),
            "/unanchored/0/finding/body",
        ),
        (
            lambda d: with_unanchored(d)["unanchored"][0]["finding"].update(
                suggestion={"lines": ["\ude00"]}
            ),
            "/unanchored/0/finding/suggestion/lines/0",
        ),
    ],
)
def test_control_character_in_every_text_field(setup: Any, pointer: str) -> None:
    doc = good()
    setup(doc)
    error = refusal(doc)
    assert error.code == "invalid_text"
    assert error.details == {"pointer": pointer, "kind": "control_character"}
    assert CANARY not in error.message
    assert CANARY not in json.dumps(error.outcome().document)


def test_the_lone_surrogate_escape_of_the_issue() -> None:
    raw = dumps(good()).replace(b'"summary"', b'"\\ud800"')
    with pytest.raises(DraftError) as info:
        parse_draft(raw)
    assert info.value.code == "invalid_text"
    assert info.value.details == {"pointer": "/body", "kind": "control_character"}


def test_a_valid_surrogate_pair_escape_is_fine() -> None:
    raw = dumps(good()).replace(b'"summary"', b'"\\ud83d\\ude00"')
    assert parse_draft(raw).data["body"] == "\U0001f600"


def test_a_separator_only_in_a_one_line_field_is_invalid_draft_not_invalid_text() -> None:
    doc = good()
    doc["findings"][1]["title"] = "x\n"
    error = refusal(doc)
    assert error.code == "invalid_draft"


def test_a_vt_in_a_suggestion_line_is_invalid_draft() -> None:
    doc = good()
    doc["findings"][0]["suggestion"] = {"lines": ["a\x0bb"]}
    assert refusal(doc).code == "invalid_draft"


def test_schema_errors_win_over_text_errors() -> None:
    doc = good()
    doc["body"] = "a\x00"
    doc["event"] = "nope"
    assert refusal(doc).code == "invalid_draft"


@pytest.mark.parametrize(
    "line",
    [
        "```suggestion",
        "~~~suggestion",
        "> ```suggestion",
        "- ~~~suggestion",
        "1. > - ```suggestion",
        "````suggestion:-1+0",
        "```&#115;uggestion",
        "```\\suggestion",
        "x ``` Suggestion",
    ],
)
@pytest.mark.parametrize("field", ["body", "finding_body", "unanchored_body"])
def test_suggestion_fence_in_multi_line_text(field: str, line: str) -> None:
    doc = with_unanchored(good())
    text = f"before\n{line}\nafter {CANARY}"
    pointer = "/body"
    if field == "body":
        doc["body"] = text
    elif field == "finding_body":
        doc["findings"][0]["body"] = text
        pointer = "/findings/0/body"
    else:
        doc["unanchored"][0]["finding"]["body"] = text
        pointer = "/unanchored/0/finding/body"
    error = refusal(doc)
    assert error.code == "invalid_text"
    assert error.details == {"pointer": pointer, "kind": "suggestion_fence"}
    assert CANARY not in error.message
    assert CANARY not in json.dumps(error.outcome().document)


@pytest.mark.parametrize("separator", ["\r", "\x0b", "\x0c", "\x85", " ", " ", "\r\n"])
def test_a_fence_after_any_separator_is_refused(separator: str) -> None:
    doc = good()
    doc["body"] = f"ok{separator}```suggestion"
    assert refusal(doc).details == {"pointer": "/body", "kind": "suggestion_fence"}


def test_a_suggestion_line_may_hold_a_plain_fence_it_degrades_later() -> None:
    doc = good()
    doc["findings"][0]["suggestion"] = {"lines": ["```", "~~~~", "```python"]}
    assert parse_draft(dumps(doc)).data["findings"][0]["suggestion"]["lines"][0] == "```"


@pytest.mark.parametrize("line", ["```suggestion", "~~~suggestion:-1+0", "> ~~~~ Suggestion"])
def test_a_suggestion_fence_in_a_suggestion_line_is_refused(line: str) -> None:
    doc = good()
    doc["findings"][0]["suggestion"] = {"lines": ["ok", line]}
    error = refusal(doc)
    assert error.code == "invalid_text"
    assert error.details == {
        "pointer": "/findings/0/suggestion/lines/1",
        "kind": "suggestion_fence",
    }


def test_a_suggestion_fence_in_an_unanchored_suggestion_line_is_refused() -> None:
    doc = with_unanchored(good())
    doc["unanchored"][0]["finding"]["suggestion"] = {"lines": ["```suggestion"]}
    assert refusal(doc).details == {
        "pointer": "/unanchored/0/finding/suggestion/lines/0",
        "kind": "suggestion_fence",
    }


def test_text_issues_reports_the_forging_suggestion_lines() -> None:
    doc = {"findings": [{"body": "~~~~", "suggestion": {"lines": ["~~~~", "~~~suggestion", "x"]}}]}
    assert text_issues(doc) == [
        {"pointer": "/findings/0/suggestion/lines/1", "kind": "suggestion_fence"}
    ]


def test_a_title_and_path_are_not_multi_line_text() -> None:
    doc = good()
    doc["findings"][1]["title"] = "```suggestion"
    assert parse_draft(dumps(doc))


def test_a_fence_in_a_finding_path_loads() -> None:
    doc = good()
    doc["findings"][0]["path"] = "a/```suggestion"
    assert parse_draft(dumps(doc))


def test_a_fence_in_an_unanchored_finding_path_and_title_loads() -> None:
    doc = with_unanchored(good())
    doc["unanchored"][0]["finding"]["path"] = "a/```suggestion"
    doc["unanchored"][0]["finding"]["title"] = "~~~suggestion"
    assert parse_draft(dumps(doc))


def test_a_fence_in_an_anchored_finding_title_loads() -> None:
    doc = good()
    doc["findings"][0]["title"] = "```suggestion"
    assert parse_draft(dumps(doc))


@pytest.mark.parametrize(
    "separator", ["\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]
)
def test_a_fence_split_by_a_separator_is_not_a_fence(separator: str) -> None:
    assert text_problem(f"```{separator}suggestion") is None
    doc = good()
    doc["body"] = f"```{separator}suggestion"
    assert parse_draft(dumps(doc)).data["body"] == doc["body"]


def test_ordinary_code_fences_in_a_body_load() -> None:
    doc = good()
    doc["body"] = "see\n```python\nx = 1\n```\n\n~~~\nraw\n~~~\n"
    assert parse_draft(dumps(doc))


def test_step_1_is_reported_before_step_2_across_fields() -> None:
    doc = good()
    doc["body"] = "```suggestion"
    doc["findings"][0]["body"] = "a\x00"
    error = refusal(doc)
    assert error.details == {"pointer": "/findings/0/body", "kind": "control_character"}


def test_text_issues_lists_every_problem_in_document_order() -> None:
    doc = good()
    doc["body"] = "```suggestion"
    doc["findings"][0]["body"] = "a\x00"
    doc["findings"][1]["title"] = "b\x01"
    assert text_issues(doc) == [
        {"pointer": "/findings/0/body", "kind": "control_character"},
        {"pointer": "/findings/1/title", "kind": "control_character"},
        {"pointer": "/body", "kind": "suggestion_fence"},
    ]


def test_a_control_character_and_a_fence_in_one_field_report_once() -> None:
    doc = good()
    doc["body"] = "a\x00\n```suggestion"
    assert text_issues(doc) == [{"pointer": "/body", "kind": "control_character"}]


def test_text_issues_is_empty_for_a_clean_draft() -> None:
    assert text_issues(good()) == []


def test_text_issues_ignores_a_malformed_document() -> None:
    assert text_issues(None) == []
    assert text_issues({"body": 1, "findings": "x", "unanchored": [None, {"finding": 3}]}) == []
    assert text_issues({"findings": [{"suggestion": {"lines": "x"}}, {"suggestion": 1}]}) == []


def test_meta_is_not_a_text_field() -> None:
    doc = good()
    doc["meta"] = {"note": "a\x00 ```suggestion"}
    assert parse_draft(dumps(doc))


def test_a_huge_hostile_draft_is_bounded() -> None:
    doc = good()
    doc["body"] = "``` " * 16000
    doc["findings"] = [
        {"id": f"f{i}", "path": "a", "line": 1, "body": "`~" * 30000} for i in range(200)
    ]
    start = time.process_time()
    assert parse_draft(dumps(doc))
    assert time.process_time() - start < 20
