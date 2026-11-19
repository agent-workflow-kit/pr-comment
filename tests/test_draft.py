"""Loading and validating review drafts (docs/interface.md: Review draft, Draft versioning).

A draft is untrusted, agent-generated input: these tests feed hostile files and documents and
check that the outcome is a fixed-text ``invalid_draft`` (or ``unsupported_draft_version``) error
that never echoes draft content.
"""

from __future__ import annotations

import copy
import io
import json
import os
import re
import signal
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from pr_comment import draft as draft_module
from pr_comment.draft import (
    MAX_DRAFT_BYTES,
    Draft,
    DraftError,
    load_draft,
    parse_draft,
    validate_document,
)
from tests.schema_harness import load_schemas, validate

SHA = "0123456789abcdef0123456789abcdef01234567"
SHA2 = "89abcdef0123456789abcdef0123456789abcdef"
DRAFT_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
CANARY = "CANARY-9f3a"

Doc = dict[str, Any]


def good() -> Doc:
    return {
        "schema_version": 1,
        "draft_id": DRAFT_ID,
        "pr": "github.com/example/widgets#7",
        "anchor": {"head_sha": SHA, "base_ref": "main", "base_sha": SHA2, "merge_base_sha": None},
        "event": "comment",
        "body": "summary",
        "findings": [
            {"id": "f1", "path": "src/a.py", "line": 10, "body": "text"},
            {
                "id": "f2",
                "path": "src/b.py",
                "line": 12,
                "start_line": 10,
                "side": "right",
                "severity": "major",
                "title": "A title",
                "body": "text",
                "suggestion": {"lines": ["x = 1", "y = 2"]},
            },
            {"id": "f3", "path": "src/c.py", "line": None, "body": "file level"},
        ],
        "unanchored": [
            {
                "finding": {"id": "u1", "path": "src/d.py", "line": 3, "body": "moved"},
                "head_sha": SHA2,
                "reason": "changed",
            }
        ],
        "meta": {"anything": [1, {"a": None}], "n": 1.5},
    }


def errors_of(doc: object) -> list[tuple[str, str]]:
    return [(e["pointer"], e["message"]) for e in validate_document(doc)]


def pointers_of(doc: object) -> list[str]:
    return [pointer for pointer, _ in errors_of(doc)]


def dumps(doc: object) -> bytes:
    return json.dumps(doc).encode("utf-8")


def schema_accepts(doc: object) -> bool:
    try:
        validate("pr-comment/review-draft.json", doc)
    except jsonschema.ValidationError:
        return False
    return True


# --- a valid draft ---------------------------------------------------------------------------


def test_good_draft_is_accepted_by_schema_and_code() -> None:
    assert schema_accepts(good())
    assert validate_document(good()) == []


def test_minimal_draft_without_optional_fields() -> None:
    doc = good()
    del doc["unanchored"], doc["meta"]
    doc["findings"] = []
    doc["body"] = ""
    assert schema_accepts(doc)
    assert validate_document(doc) == []


def test_parse_returns_the_document_and_the_sha256_of_the_bytes() -> None:
    raw = dumps(good())
    result = parse_draft(raw)
    assert isinstance(result, Draft)
    assert result.data == good()
    import hashlib

    assert result.sha256 == hashlib.sha256(raw).hexdigest()


# --- rules, each with pointers and parity with the schema ------------------------------------

Mutator = Callable[[Doc], None]


def _set(path: list[Any], value: Any) -> Mutator:
    def apply(doc: Doc) -> None:
        node: Any = doc
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return apply


def _delete(path: list[Any]) -> Mutator:
    def apply(doc: Doc) -> None:
        node: Any = doc
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]

    return apply


def _many(n: int, template: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
    return [{**template, "id": f"{prefix}{i}"} for i in range(n)]


def _unanchored(n: int, prefix: str) -> list[dict[str, Any]]:
    return [
        {
            "finding": {"id": f"{prefix}{i}", "path": "p", "line": 1, "body": "b"},
            "head_sha": SHA,
            "reason": "changed",
        }
        for i in range(n)
    ]


def _left_over_int32(doc: Doc) -> None:
    doc["findings"][0]["side"] = "left"
    doc["findings"][0]["line"] = 2**31


FINDING = {"id": "x", "path": "p", "line": 1, "body": "b"}
LONG_LINE = "x" * 1000

# (name, mutator, pointer of the first error, does the JSON schema reject it too)
RULES: list[tuple[str, Mutator, str, bool]] = [
    ("missing-draft-id", _delete(["draft_id"]), "", True),
    ("missing-pr", _delete(["pr"]), "", True),
    ("missing-anchor", _delete(["anchor"]), "", True),
    ("missing-event", _delete(["event"]), "", True),
    ("missing-body", _delete(["body"]), "", True),
    ("missing-findings", _delete(["findings"]), "", True),
    ("unknown-top-level", _set(["extra"], 1), "", True),
    ("draft-id-lowercase", _set(["draft_id"], DRAFT_ID.lower()), "/draft_id", True),
    ("draft-id-short", _set(["draft_id"], DRAFT_ID[:-1]), "/draft_id", True),
    ("draft-id-trailing-newline", _set(["draft_id"], DRAFT_ID + "\n"), "/draft_id", False),
    ("draft-id-type", _set(["draft_id"], 5), "/draft_id", True),
    ("pr-bad", _set(["pr"], "not a pr"), "/pr", True),
    ("pr-number-zero", _set(["pr"], "github.com/example/w#0"), "/pr", True),
    ("pr-type", _set(["pr"], ["x"]), "/pr", True),
    ("anchor-type", _set(["anchor"], []), "/anchor", True),
    ("anchor-missing-head", _delete(["anchor", "head_sha"]), "/anchor", True),
    ("anchor-missing-merge-base", _delete(["anchor", "merge_base_sha"]), "/anchor", True),
    ("anchor-unknown", _set(["anchor", "x"], 1), "/anchor", True),
    ("anchor-head-uppercase", _set(["anchor", "head_sha"], SHA.upper()), "/anchor/head_sha", True),
    ("anchor-head-short", _set(["anchor", "head_sha"], SHA[:39]), "/anchor/head_sha", True),
    ("anchor-base-sha-bad", _set(["anchor", "base_sha"], "zz"), "/anchor/base_sha", True),
    (
        "anchor-merge-base-bad",
        _set(["anchor", "merge_base_sha"], 1),
        "/anchor/merge_base_sha",
        True,
    ),
    ("base-ref-empty", _set(["anchor", "base_ref"], ""), "/anchor/base_ref", True),
    ("base-ref-leading-dash", _set(["anchor", "base_ref"], "-x"), "/anchor/base_ref", True),
    ("base-ref-dotdot", _set(["anchor", "base_ref"], "a..b"), "/anchor/base_ref", True),
    ("base-ref-space", _set(["anchor", "base_ref"], "a b"), "/anchor/base_ref", True),
    ("base-ref-lone-at", _set(["anchor", "base_ref"], "@"), "/anchor/base_ref", True),
    ("base-ref-too-long", _set(["anchor", "base_ref"], "a" * 1025), "/anchor/base_ref", True),
    ("base-ref-refs", _set(["anchor", "base_ref"], "refs"), "/anchor/base_ref", False),
    (
        "base-ref-refs-prefix",
        _set(["anchor", "base_ref"], "refs/heads/x"),
        "/anchor/base_ref",
        False,
    ),
    ("base-ref-control", _set(["anchor", "base_ref"], "a\x01b"), "/anchor/base_ref", True),
    ("base-ref-separator", _set(["anchor", "base_ref"], "a b"), "/anchor/base_ref", False),
    ("base-ref-format-char", _set(["anchor", "base_ref"], "a​b"), "/anchor/base_ref", False),
    ("base-ref-type", _set(["anchor", "base_ref"], None), "/anchor/base_ref", True),
    ("event-approve", _set(["event"], "approve"), "/event", True),
    ("event-case", _set(["event"], "Comment"), "/event", True),
    ("event-type", _set(["event"], 1), "/event", True),
    ("body-type", _set(["body"], None), "/body", True),
    ("body-too-long", _set(["body"], "a" * 65537), "/body", True),
    ("body-multibyte-over-cap", _set(["body"], "é" * 32768 + "a"), "/body", False),
    ("findings-type", _set(["findings"], {}), "/findings", True),
    ("findings-501", _set(["findings"], _many(501, FINDING, "a")), "/findings", True),
    (
        "items-501-together",
        lambda d: d.update(findings=_many(300, FINDING, "a"), unanchored=_unanchored(201, "u")),
        "/unanchored",
        False,
    ),
    ("finding-type", _set(["findings", 0], "x"), "/findings/0", True),
    ("finding-missing-id", _delete(["findings", 0, "id"]), "/findings/0", True),
    ("finding-missing-path", _delete(["findings", 0, "path"]), "/findings/0", True),
    ("finding-missing-line", _delete(["findings", 0, "line"]), "/findings/0", True),
    ("finding-missing-body", _delete(["findings", 0, "body"]), "/findings/0", True),
    ("finding-unknown", _set(["findings", 0, "x"], 1), "/findings/0", True),
    ("finding-id-bad", _set(["findings", 0, "id"], "a b"), "/findings/0/id", True),
    ("finding-id-long", _set(["findings", 0, "id"], "a" * 65), "/findings/0/id", True),
    ("finding-id-empty", _set(["findings", 0, "id"], ""), "/findings/0/id", True),
    ("path-empty", _set(["findings", 0, "path"], ""), "/findings/0/path", True),
    ("path-newline", _set(["findings", 0, "path"], "a\nb"), "/findings/0/path", True),
    ("path-cr", _set(["findings", 0, "path"], "a\rb"), "/findings/0/path", True),
    ("path-vt", _set(["findings", 0, "path"], "a\x0bb"), "/findings/0/path", True),
    ("path-nel", _set(["findings", 0, "path"], "a\x85b"), "/findings/0/path", True),
    ("path-ls", _set(["findings", 0, "path"], "a b"), "/findings/0/path", True),
    ("path-ps", _set(["findings", 0, "path"], "a b"), "/findings/0/path", True),
    ("path-trailing-newline", _set(["findings", 0, "path"], "a\n"), "/findings/0/path", False),
    ("path-4097-bytes", _set(["findings", 0, "path"], "a" * 4097), "/findings/0/path", True),
    (
        "path-multibyte-over-cap",
        _set(["findings", 0, "path"], "é" * 2049),
        "/findings/0/path",
        False,
    ),
    ("path-type", _set(["findings", 0, "path"], 3), "/findings/0/path", True),
    ("line-zero", _set(["findings", 0, "line"], 0), "/findings/0/line", True),
    ("line-negative", _set(["findings", 0, "line"], -1), "/findings/0/line", True),
    ("line-string", _set(["findings", 0, "line"], "1"), "/findings/0/line", True),
    ("line-bool", _set(["findings", 0, "line"], True), "/findings/0/line", True),
    ("line-float", _set(["findings", 0, "line"], 1.5), "/findings/0/line", True),
    ("start-line-zero", _set(["findings", 0, "start_line"], 0), "/findings/0/start_line", True),
    ("start-line-type", _set(["findings", 0, "start_line"], None), "/findings/0/start_line", True),
    (
        "start-line-equals-line",
        _set(["findings", 0, "start_line"], 10),
        "/findings/0/start_line",
        False,
    ),
    (
        "start-line-after-line",
        _set(["findings", 0, "start_line"], 11),
        "/findings/0/start_line",
        False,
    ),
    ("side-bad", _set(["findings", 0, "side"], "both"), "/findings/0/side", True),
    ("side-null", _set(["findings", 0, "side"], None), "/findings/0/side", True),
    ("severity-bad", _set(["findings", 0, "severity"], "critical"), "/findings/0/severity", True),
    ("title-257-chars", _set(["findings", 0, "title"], "t" * 257), "/findings/0/title", True),
    ("path-space-only", _set(["findings", 0, "path"], " "), "/findings/0/path", False),
    ("path-tab-only", _set(["findings", 0, "path"], "\t \t"), "/findings/0/path", False),
    (
        "unanchored-path-space-only",
        _set(["unanchored", 0, "finding", "path"], " "),
        "/unanchored/0/finding/path",
        False,
    ),
    ("line-over-int32", _set(["findings", 0, "line"], 2**31), "/findings/0/line", True),
    (
        "left-line-over-int32",
        _left_over_int32,
        "/findings/0/line",
        True,
    ),
    (
        "unanchored-line-over-int32",
        _set(["unanchored", 0, "finding", "line"], 2**31),
        "/unanchored/0/finding/line",
        True,
    ),
    (
        "start-line-over-int32",
        _set(["findings", 1, "start_line"], 2**31),
        "/findings/1/start_line",
        True,
    ),
    ("title-newline", _set(["findings", 0, "title"], "a\nb"), "/findings/0/title", True),
    ("title-type", _set(["findings", 0, "title"], 1), "/findings/0/title", True),
    ("finding-body-empty", _set(["findings", 0, "body"], ""), "/findings/0/body", True),
    ("finding-body-newline-only", _set(["findings", 0, "body"], "\n"), "/findings/0/body", False),
    (
        "finding-body-separators-only",
        _set(["findings", 1, "body"], "\r\n  \t\x0b\x0c\x1c\x1d\x1e\x85 \r"),
        "/findings/1/body",
        False,
    ),
    ("finding-body-spaces-only", _set(["findings", 0, "body"], " \t  "), "/findings/0/body", False),
    (
        "file-level-finding-body-blank",
        _set(["findings", 2, "body"], "\n\n"),
        "/findings/2/body",
        False,
    ),
    (
        "unanchored-body-blank",
        _set(["unanchored", 0, "finding", "body"], "\r\n"),
        "/unanchored/0/finding/body",
        False,
    ),
    (
        "finding-body-blank-and-over-cap",
        _set(["findings", 0, "body"], " " * 65537),
        "/findings/0/body",
        True,
    ),
    ("finding-body-type", _set(["findings", 0, "body"], 1), "/findings/0/body", True),
    ("finding-body-over-cap", _set(["findings", 0, "body"], "a" * 65537), "/findings/0/body", True),
    (
        "finding-body-multibyte-over-cap",
        _set(["findings", 0, "body"], "é" * 32768 + "a"),
        "/findings/0/body",
        False,
    ),
    (
        "file-level-with-start-line",
        _set(["findings", 2, "start_line"], 1),
        "/findings/2/start_line",
        True,
    ),
    ("file-level-with-side", _set(["findings", 2, "side"], "right"), "/findings/2/side", True),
    (
        "file-level-with-suggestion",
        _set(["findings", 2, "suggestion"], {"lines": []}),
        "/findings/2/suggestion",
        True,
    ),
    (
        "suggestion-on-left",
        _set(["findings", 1, "side"], "left"),
        "/findings/1/suggestion",
        True,
    ),
    ("suggestion-type", _set(["findings", 1, "suggestion"], []), "/findings/1/suggestion", True),
    (
        "suggestion-missing-lines",
        _set(["findings", 1, "suggestion"], {}),
        "/findings/1/suggestion",
        True,
    ),
    (
        "suggestion-unknown-key",
        _set(["findings", 1, "suggestion", "x"], 1),
        "/findings/1/suggestion",
        True,
    ),
    (
        "suggestion-lines-type",
        _set(["findings", 1, "suggestion", "lines"], "x"),
        "/findings/1/suggestion/lines",
        True,
    ),
    (
        "suggestion-line-type",
        _set(["findings", 1, "suggestion", "lines", 1], 5),
        "/findings/1/suggestion/lines/1",
        True,
    ),
    (
        "suggestion-line-newline",
        _set(["findings", 1, "suggestion", "lines", 1], "a\nb"),
        "/findings/1/suggestion/lines/1",
        True,
    ),
    (
        "suggestion-line-ls",
        _set(["findings", 1, "suggestion", "lines", 0], "a b"),
        "/findings/1/suggestion/lines/0",
        True,
    ),
    (
        "suggestion-over-64kib",
        _set(["findings", 1, "suggestion", "lines"], [LONG_LINE] * 66),
        "/findings/1/suggestion",
        False,
    ),
    ("duplicate-id-in-findings", _set(["findings", 1, "id"], "f1"), "/findings/1/id", False),
    (
        "duplicate-id-with-unanchored",
        _set(["unanchored", 0, "finding", "id"], "f2"),
        "/unanchored/0/finding/id",
        False,
    ),
    ("unanchored-type", _set(["unanchored"], {}), "/unanchored", True),
    ("unanchored-item-type", _set(["unanchored", 0], 1), "/unanchored/0", True),
    ("unanchored-missing-reason", _delete(["unanchored", 0, "reason"]), "/unanchored/0", True),
    ("unanchored-missing-head", _delete(["unanchored", 0, "head_sha"]), "/unanchored/0", True),
    (
        "unanchored-bad-reason",
        _set(["unanchored", 0, "reason"], "nope"),
        "/unanchored/0/reason",
        True,
    ),
    (
        "unanchored-bad-head",
        _set(["unanchored", 0, "head_sha"], "x"),
        "/unanchored/0/head_sha",
        True,
    ),
    ("unanchored-unknown", _set(["unanchored", 0, "x"], 1), "/unanchored/0", True),
    (
        "unanchored-finding-bad-line",
        _set(["unanchored", 0, "finding", "line"], 0),
        "/unanchored/0/finding/line",
        True,
    ),
    (
        "unanchored-finding-start-after-line",
        _set(["unanchored", 0, "finding", "start_line"], 5),
        "/unanchored/0/finding/start_line",
        False,
    ),
    ("meta-type", _set(["meta"], []), "/meta", True),
    ("meta-null", _set(["meta"], None), "/meta", True),
]


@pytest.mark.parametrize(("name", "mutate", "pointer", "schema_rejects"), RULES)
def test_rule_refuses_with_pointer(
    name: str, mutate: Mutator, pointer: str, schema_rejects: bool
) -> None:
    doc = good()
    mutate(doc)
    errors = validate_document(doc)
    assert errors, name
    assert errors[0]["pointer"] == pointer, errors
    assert schema_accepts(doc) is (not schema_rejects), name
    for error in errors:
        assert error["pointer"] == "" or error["pointer"].startswith("/")
        assert error["message"]


@pytest.mark.parametrize("body", ["a", "\na\n", " a ", " ", "​", "　", "﻿"])
def test_finding_body_with_a_non_blank_character_is_accepted(body: str) -> None:
    """Only line separators, space and tab are blank; other whitespace is left to the maintainer."""
    doc = good()
    doc["findings"][0]["body"] = body
    assert validate_document(doc) == []


def test_blank_finding_body_message_is_fixed_and_never_echoes_the_body() -> None:
    doc = good()
    doc["findings"][0]["body"] = " \n\t"
    errors = validate_document(doc)
    assert errors == [
        {"pointer": "/findings/0/body", "message": "must contain a character that is not blank"}
    ]


def test_boundary_values_accepted() -> None:
    doc = good()
    doc["body"] = "é" * 32768  # exactly 64 KiB of UTF-8
    doc["findings"][0]["path"] = "a" * 4096
    doc["findings"][0]["title"] = "t" * 256
    doc["findings"][0]["body"] = "é" * 32768
    doc["findings"][1]["suggestion"]["lines"] = ["x" * 1023] * 64  # 64*1023 + 63 newlines = 65535
    doc["anchor"]["base_ref"] = "a" * 1024
    assert validate_document(doc) == []
    assert schema_accepts(doc)


def test_500_items_together_accepted_and_501_refused() -> None:
    doc = good()
    doc["findings"] = _many(300, FINDING, "a")
    doc["unanchored"] = _unanchored(200, "u")
    assert validate_document(doc) == []
    doc["unanchored"] = _unanchored(201, "u")
    assert pointers_of(doc)


def test_empty_suggestion_lines_means_delete_and_is_valid() -> None:
    doc = good()
    doc["findings"][1]["suggestion"] = {"lines": []}
    assert validate_document(doc) == []
    assert schema_accepts(doc)


def test_suggestion_on_explicit_right_side_and_single_line_is_valid() -> None:
    doc = good()
    del doc["findings"][1]["start_line"]
    assert validate_document(doc) == []


def test_left_side_without_suggestion_is_valid() -> None:
    doc = good()
    del doc["findings"][1]["suggestion"]
    doc["findings"][1]["side"] = "left"
    assert validate_document(doc) == []


def test_all_errors_are_reported_not_just_the_first() -> None:
    doc = good()
    doc["draft_id"] = "x"
    doc["findings"][0]["line"] = 0
    doc["findings"][1]["id"] = "f1"
    assert set(pointers_of(doc)) == {"/draft_id", "/findings/0/line", "/findings/1/id"}


def test_error_list_is_capped() -> None:
    doc = good()
    doc["findings"] = [{"id": "x"}] * 500
    errors = validate_document(doc)
    assert 1 <= len(errors) <= draft_module.MAX_REPORTED_ERRORS + 1


def test_document_must_be_an_object() -> None:
    for doc in ([], None, "x", 1, 1.5, True):
        assert errors_of(doc)[0][0] == "", doc


# --- versions --------------------------------------------------------------------------------


def test_schema_version_2_is_unsupported_with_supported_list() -> None:
    doc = good()
    doc["schema_version"] = 2
    with pytest.raises(DraftError) as info:
        parse_draft(dumps(doc))
    assert info.value.code == "unsupported_draft_version"
    assert info.value.details == {"supported": [1]}


def test_unsupported_version_is_reported_before_other_problems() -> None:
    with pytest.raises(DraftError) as info:
        parse_draft(dumps({"schema_version": 0, "garbage": True}))
    assert info.value.code == "unsupported_draft_version"


@pytest.mark.parametrize("version", [-1, 0, 3, 10**20])
def test_other_integer_versions_are_unsupported(version: int) -> None:
    doc = good()
    doc["schema_version"] = version
    with pytest.raises(DraftError) as info:
        parse_draft(dumps(doc))
    assert info.value.code == "unsupported_draft_version"


@pytest.mark.parametrize("version", ["1", 1.0, True, None, [1], {}])
def test_non_integer_version_is_an_invalid_draft(version: object) -> None:
    doc = good()
    doc["schema_version"] = version
    with pytest.raises(DraftError) as info:
        parse_draft(dumps(doc))
    assert info.value.code == "invalid_draft"
    assert info.value.details is not None
    assert info.value.details["errors"][0]["pointer"] == "/schema_version"


def test_missing_version_is_an_invalid_draft() -> None:
    doc = good()
    del doc["schema_version"]
    with pytest.raises(DraftError) as info:
        parse_draft(dumps(doc))
    assert info.value.code == "invalid_draft"


# --- hostile bytes ---------------------------------------------------------------------------


def _invalid(raw: bytes) -> DraftError:
    with pytest.raises(DraftError) as info:
        parse_draft(raw)
    assert info.value.code == "invalid_draft"
    assert info.value.details is not None
    assert info.value.details["errors"]
    return info.value


def test_invalid_utf8_is_refused() -> None:
    raw = dumps(good()).replace(b"summary", b"sum\xff\xfemary")
    _invalid(raw)


def test_overlong_and_surrogate_utf8_are_refused() -> None:
    _invalid(b'{"a": "\xc0\xaf"}')
    _invalid(b'{"a": "\xed\xa0\x80"}')


def test_utf8_bom_is_refused() -> None:
    _invalid(b"\xef\xbb\xbf" + dumps(good()))


def test_utf16_is_refused() -> None:
    _invalid(dumps(good()).decode().encode("utf-16"))


def test_empty_and_whitespace_only_are_refused() -> None:
    _invalid(b"")
    _invalid(b"  \n ")


def test_not_json_and_trailing_garbage_are_refused() -> None:
    _invalid(b"not json")
    _invalid(dumps(good()) + b" x")
    _invalid(dumps(good()) + dumps(good()))
    _invalid(b"{'a': 1}")
    _invalid(b'{"a": 1,}')


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
def test_nan_and_infinity_are_refused(constant: bytes) -> None:
    raw = dumps(good()).replace(b'"n": 1.5', b'"n": ' + constant)
    assert constant in raw
    _invalid(raw)


def test_float_overflow_is_refused() -> None:
    raw = dumps(good()).replace(b'"n": 1.5', b'"n": 1e999')
    _invalid(raw)


def test_duplicate_keys_are_refused_at_any_depth() -> None:
    _invalid(b'{"schema_version": 1, "schema_version": 2}')
    raw = dumps(good()).replace(b'"meta": {', b'"meta": {"k": 1, "k": 2, ')
    _invalid(raw)
    raw = dumps(good()).replace(b'"id": "f1"', b'"id": "f1", "id": "f9"')
    _invalid(raw)


def test_duplicate_key_with_escape_spelling_is_refused() -> None:
    # "a" and "a" are the same key after parsing.
    _invalid(b'{"a": 1, "\\u0061": 2}')


def test_deep_nesting_is_refused_without_crashing() -> None:
    depth = 200_000
    _invalid(b"[" * depth + b"]" * depth)
    raw = dumps(good()).replace(
        b'"meta": {', b'"meta": {"d": ' + b"[" * depth + b"]" * depth + b", "
    )
    _invalid(raw)
    _invalid(b'{"a":' * depth)


def test_moderate_nesting_inside_meta_is_accepted() -> None:
    doc = good()
    nested: Any = 1
    for _ in range(50):
        nested = [nested]
    doc["meta"] = {"n": nested}
    assert parse_draft(dumps(doc)).data["meta"] == doc["meta"]


def test_huge_line_number_is_refused_without_crashing() -> None:
    # An integer past Python's digit limit loads, and is out of range like any line over 2**31-1.
    raw = dumps(good()).replace(b'"line": 10', b'"line": ' + b"9" * 5000)
    with pytest.raises(DraftError) as caught:
        parse_draft(raw)
    assert caught.value.code == "invalid_draft"


def test_the_largest_line_is_int32_max() -> None:
    doc = good()
    doc["findings"][0]["line"] = 2**31 - 1
    doc["findings"][1]["line"] = 2**31 - 1
    doc["findings"][1]["start_line"] = 2**31 - 2
    assert validate_document(doc) == []
    doc["findings"][0]["line"] = 2**31
    assert pointers_of(doc) == ["/findings/0/line"]


def test_lone_surrogate_escape_does_not_crash_loading() -> None:
    # validate_document does not refuse the character (parse_draft does: invalid_text, see
    # tests/test_draft_text.py); it must still not crash on the byte count of such a string.
    raw = dumps(good()).replace(b'"summary"', b'"\\ud800"')
    with pytest.raises(DraftError) as info:
        parse_draft(raw)
    assert info.value.code == "invalid_text"
    doc = good()
    doc["findings"][0]["path"] = "a\ud800"
    doc["findings"][0]["body"] = "\udfff" * 20000
    assert validate_document(doc) == []


def test_non_bmp_text_is_counted_in_bytes() -> None:
    doc = good()
    doc["body"] = "\U0001f600" * 16384  # exactly 64 KiB
    assert validate_document(doc) == []
    doc["body"] += "a"
    assert pointers_of(doc) == ["/body"]


def test_big_valid_draft_parses() -> None:
    doc = good()
    doc["findings"] = [
        {"id": f"f{i}", "path": "src/a.py", "line": i + 1, "body": "é" * 10000} for i in range(500)
    ]
    del doc["unanchored"]
    raw = json.dumps(doc, ensure_ascii=False).encode("utf-8")
    assert 5_000_000 < len(raw) < MAX_DRAFT_BYTES
    assert len(parse_draft(raw).data["findings"]) == 500


# --- no echo ---------------------------------------------------------------------------------


def test_error_text_never_echoes_draft_content() -> None:
    doc = good()
    doc["draft_id"] = CANARY
    doc["pr"] = CANARY
    doc["event"] = CANARY
    doc["body"] = 5
    doc[CANARY] = CANARY
    doc["anchor"] = {"head_sha": CANARY, "base_ref": CANARY + " x", "base_sha": CANARY, CANARY: 1}
    doc["findings"] = [
        {
            "id": CANARY + " bad id",
            "path": CANARY + "\n",
            "line": CANARY,
            "title": CANARY * 100,
            "severity": CANARY,
            "side": CANARY,
            "body": 1,
            "suggestion": {"lines": [CANARY + "\n"], CANARY: 1},
            CANARY: 1,
        },
        {"id": CANARY, "path": "p", "line": 1, "body": "b"},
        {"id": CANARY, "path": "p", "line": 1, "body": "b"},
    ]
    doc["unanchored"] = [{"finding": {CANARY: 1}, "head_sha": CANARY, "reason": CANARY, CANARY: 1}]
    with pytest.raises(DraftError) as info:
        parse_draft(dumps(doc))
    rendered = json.dumps(info.value.outcome().document) + str(info.value) + info.value.message
    assert CANARY not in rendered
    assert len(info.value.details["errors"]) > 5  # type: ignore[index]


def test_unknown_key_pointer_does_not_include_the_key() -> None:
    doc = good()
    doc["findings"][0][CANARY + "/~"] = 1
    assert pointers_of(doc) == ["/findings/0"]


def test_parse_error_does_not_echo_content() -> None:
    err = _invalid(b'{"a": ' + CANARY.encode() + b"}")
    assert CANARY not in json.dumps(err.outcome().document)


# --- error envelope --------------------------------------------------------------------------


def test_outcome_is_the_exit_2_envelope_and_matches_the_schema() -> None:
    doc = good()
    doc["draft_id"] = "x"
    with pytest.raises(DraftError) as info:
        parse_draft(dumps(doc))
    outcome = info.value.outcome()
    assert outcome.exit_code == 2
    assert outcome.document is not None
    validate("pr-comment/cli/error.json", outcome.document)
    error = outcome.document["error"]
    assert error["code"] == "invalid_draft"
    assert error["details"]["errors"] == [
        {"pointer": "/draft_id", "message": error["details"]["errors"][0]["message"]}
    ]
    assert error["message"].startswith("invalid draft: ")


def test_unsupported_version_outcome_matches_the_schema() -> None:
    doc = good()
    doc["schema_version"] = 2
    with pytest.raises(DraftError) as info:
        parse_draft(dumps(doc))
    outcome = info.value.outcome()
    assert outcome.exit_code == 2
    assert outcome.document is not None
    validate("pr-comment/cli/error.json", outcome.document)
    assert outcome.document["error"]["code"] == "unsupported_draft_version"
    assert outcome.document["error"]["details"] == {"supported": [1]}


def test_golden_error_lists_for_every_rule(golden_json: Callable[[str, Any], None]) -> None:
    listing: dict[str, Any] = {}
    for name, mutate, _pointer, _schema_rejects in RULES:
        doc = good()
        mutate(doc)
        listing[name] = validate_document(doc)
    golden_json("draft/rule-errors.json", listing)


def test_golden_error_envelope(golden_json: Callable[[str, Any], None]) -> None:
    doc = good()
    doc["findings"][0]["start_line"] = 10
    doc["findings"][1]["id"] = "f1"
    with pytest.raises(DraftError) as info:
        parse_draft(dumps(doc))
    document = info.value.outcome().document
    assert document is not None
    validate("pr-comment/cli/error.json", document)
    golden_json("draft/error-envelope.json", document)


# --- patterns stay in step with the schema ---------------------------------------------------


def _schema_pattern(schema_id: str, *keys: str) -> str:
    node: Any = load_schemas()[schema_id]
    for key in keys:
        node = node[key]
    return node["pattern"]


def test_code_patterns_equal_the_schema_patterns() -> None:
    defs = "pr-comment/defs.json"
    draft = "pr-comment/review-draft.json"
    assert "^" + draft_module.DRAFT_ID_PATTERN + "$" == _schema_pattern(defs, "$defs", "draft_id")
    assert "^" + draft_module.FINDING_ID_PATTERN + "$" == _schema_pattern(
        defs, "$defs", "finding_id"
    )
    assert "^" + draft_module.PR_PATTERN + "$" == _schema_pattern(defs, "$defs", "pr")
    # The one-line class is spelled with JSON escapes in the schema; compare what they match.
    schema_one_line = re.compile(
        _schema_pattern(draft, "properties", "findings", "items", "properties", "path")
    )
    code_one_line = re.compile(draft_module.ONE_LINE_PATTERN)
    for code_point in range(0x3000):
        char = chr(code_point)
        assert bool(schema_one_line.fullmatch(char)) == bool(code_one_line.fullmatch(char))


def test_enums_equal_the_schema() -> None:
    defs = load_schemas()["pr-comment/defs.json"]["$defs"]
    draft = load_schemas()["pr-comment/review-draft.json"]
    item = draft["properties"]["findings"]["items"]["properties"]
    assert set(draft_module.SEVERITIES) == set(item["severity"]["enum"])
    assert set(draft_module.SIDES) == set(defs["side"]["enum"])
    assert set(draft_module.MOVED_REASONS) == set(defs["moved_reason"]["enum"])
    assert set(draft_module.EVENTS) == set(draft["properties"]["event"]["enum"])
    assert list(draft_module.SUPPORTED_VERSIONS) == draft["properties"]["schema_version"]["enum"]
    assert draft_module.MAX_ITEMS == draft["properties"]["findings"]["maxItems"]
    assert draft_module.MAX_ITEMS == draft["properties"]["unanchored"]["maxItems"]
    assert draft_module.MAX_TITLE_CHARS == item["title"]["maxLength"]
    assert draft_module.MAX_TEXT_BYTES == item["body"]["maxLength"]
    assert draft_module.MAX_PATH_BYTES == item["path"]["maxLength"]


# --- opening the file ------------------------------------------------------------------------


@pytest.fixture
def private_dir(tmp_path: Path) -> Path:
    return tmp_path


class _Alarm:
    """Fails a test that blocks, instead of hanging the suite."""

    def __enter__(self) -> None:
        def fire(_signum: int, _frame: object) -> None:
            raise TimeoutError("blocked")

        signal.signal(signal.SIGALRM, fire)
        signal.alarm(10)

    def __exit__(self, *_exc: object) -> None:
        signal.alarm(0)


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def test_load_reads_a_regular_file(private_dir: Path) -> None:
    path = _write(private_dir / "d.json", dumps(good()))
    assert load_draft(str(path)).data == good()


def test_load_refuses_a_missing_file(private_dir: Path) -> None:
    err = _load_error(str(private_dir / "nope.json"))
    assert err.details is not None
    assert err.details["errors"][0]["pointer"] == ""


def _load_error(source: str, stdin: Any = None) -> DraftError:
    with pytest.raises(DraftError) as info:
        load_draft(source, stdin=stdin)
    assert info.value.code == "invalid_draft"
    return info.value


def test_load_refuses_a_symlink_to_a_valid_draft(private_dir: Path) -> None:
    target = _write(private_dir / "real.json", dumps(good()))
    link = private_dir / "link.json"
    link.symlink_to(target)
    err = _load_error(str(link))
    assert "symbolic link" in err.message


def test_load_refuses_a_dangling_symlink(private_dir: Path) -> None:
    link = private_dir / "dangling.json"
    link.symlink_to(private_dir / "missing")
    _load_error(str(link))


def test_load_follows_symlinks_in_parent_directories_only(private_dir: Path) -> None:
    real = private_dir / "real"
    real.mkdir()
    _write(real / "d.json", dumps(good()))
    (private_dir / "alias").symlink_to(real)
    assert load_draft(str(private_dir / "alias" / "d.json")).data == good()


def test_load_refuses_a_fifo_without_blocking(private_dir: Path) -> None:
    fifo = private_dir / "pipe"
    os.mkfifo(fifo)
    with _Alarm():
        err = _load_error(str(fifo))
    assert "regular file" in err.message


def test_load_refuses_a_fifo_with_a_writer_without_blocking(private_dir: Path) -> None:
    fifo = private_dir / "pipe"
    os.mkfifo(fifo)
    writer = os.open(fifo, os.O_RDWR)
    try:
        os.write(writer, dumps(good()))
        with _Alarm():
            _load_error(str(fifo))
    finally:
        os.close(writer)


def test_load_refuses_a_directory(private_dir: Path) -> None:
    _load_error(str(private_dir))


def test_load_refuses_a_device() -> None:
    if not os.path.exists("/dev/zero"):
        pytest.skip("no /dev/zero")
    with _Alarm():
        _load_error("/dev/zero")


def test_load_refuses_paths_that_cannot_be_opened() -> None:
    _load_error("")
    _load_error("a\x00b")
    _load_error("a\udc80b")
    _load_error("x" * 5000)


def test_load_refuses_a_file_over_16_mib(private_dir: Path) -> None:
    path = private_dir / "big.json"
    with path.open("wb") as handle:
        handle.truncate(MAX_DRAFT_BYTES + 1)
    err = _load_error(str(path))
    assert "16 MiB" in err.message


def test_load_accepts_exactly_16_mib(private_dir: Path) -> None:
    body = dumps(good())
    padding = b" " * (MAX_DRAFT_BYTES - len(body))
    path = _write(private_dir / "max.json", body + padding)
    assert path.stat().st_size == MAX_DRAFT_BYTES
    assert load_draft(str(path)).data == good()


def test_load_does_not_trust_the_stat_size(private_dir: Path) -> None:
    # A file that grows after fstat is still cut at the limit: read at most limit + 1 bytes.
    raw = b" " * (MAX_DRAFT_BYTES + 5000)
    with pytest.raises(DraftError):
        draft_module.read_limited(io.BytesIO(raw))


def test_load_reads_stdin_for_dash() -> None:
    stdin = io.BytesIO(dumps(good()))
    assert load_draft("-", stdin=stdin).data == good()


def test_load_from_stdin_over_16_mib_is_refused() -> None:
    stdin = io.BytesIO(b" " * (MAX_DRAFT_BYTES + 1))
    err = _load_error("-", stdin=stdin)
    assert "16 MiB" in err.message


def test_load_from_stdin_applies_every_check() -> None:
    _load_error("-", stdin=io.BytesIO(b"\xef\xbb\xbf{}"))
    with pytest.raises(DraftError) as info:
        load_draft("-", stdin=io.BytesIO(dumps({**good(), "schema_version": 2})))
    assert info.value.code == "unsupported_draft_version"


def test_load_from_stdin_reports_a_read_error() -> None:
    class Broken(io.RawIOBase):
        def readable(self) -> bool:
            return True

        def read(self, size: int = -1) -> bytes:
            raise OSError("boom " + CANARY)

    err = _load_error("-", stdin=Broken())
    assert CANARY not in json.dumps(err.outcome().document)


def test_file_path_text_is_not_echoed(private_dir: Path) -> None:
    err = _load_error(str(private_dir / (CANARY + ".json")))
    assert CANARY not in json.dumps(err.outcome().document)
    assert CANARY not in str(err)


def test_loading_does_not_leak_descriptors(private_dir: Path) -> None:
    path = _write(private_dir / "d.json", dumps(good()))
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(50):
        load_draft(str(path))
        with pytest.raises(DraftError):
            load_draft(str(private_dir))
    assert len(os.listdir("/proc/self/fd")) == before


@pytest.fixture(autouse=True)
def _no_signal_leak() -> Iterator[None]:
    yield
    signal.alarm(0)


def test_message_has_no_control_characters() -> None:
    doc = good()
    doc["findings"][0]["id"] = "a\x1b[31m"
    for error in validate_document(doc):
        assert re.fullmatch(r"[ -~]*", error["message"])
        assert re.fullmatch(r"[ -~]*", error["pointer"])


def test_validate_document_does_not_mutate_its_input() -> None:
    doc = good()
    snapshot = copy.deepcopy(doc)
    validate_document(doc)
    doc["findings"][0]["line"] = 0
    snapshot["findings"][0]["line"] = 0
    validate_document(doc)
    assert doc == snapshot


def test_hostile_long_pr_is_refused_quickly() -> None:
    import time

    for pr in ("a" * 1_000_000 + "!", "a." * 500_000 + "#", "a-" * 500_000 + "/x#0"):
        doc = good()
        doc["pr"] = pr
        started = time.process_time()  # CPU time: descheduling under load is not counted
        assert pointers_of(doc) == ["/pr"]
        assert time.process_time() - started < 0.1  # linear ~0.001 s; no guard 0.2 s
    doc = good()
    doc["pr"] = "a" * 250 + "/" + "b" * 100 + "#1"
    assert validate_document(doc) == []


# --- review round 1 --------------------------------------------------------------------------


def _chain_text(exc: BaseException | None) -> str:
    parts: list[str] = []
    while exc is not None:
        parts.append(repr(exc))
        parts.append(str(exc))
        exc = exc.__cause__ or exc.__context__
    return " ".join(parts)


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff" + CANARY.encode() + b"\xfe",
        ("{" + CANARY + "}").encode(),
        b'{"a": "' + CANARY.encode() + b'" "b": 1}',
    ],
)
def test_parse_failure_leaves_no_draft_content_on_the_exception_chain(raw: bytes) -> None:
    with pytest.raises(DraftError) as info:
        parse_draft(raw)
    assert info.value.__context__ is None
    assert info.value.__cause__ is None
    assert CANARY not in _chain_text(info.value)


def test_huge_schema_version_is_unsupported_not_invalid_json() -> None:
    raw = dumps(good()).replace(b'"schema_version": 1', b'"schema_version": ' + b"9" * 5000)
    with pytest.raises(DraftError) as info:
        parse_draft(raw)
    assert info.value.code == "unsupported_draft_version"
    assert info.value.details == {"supported": [1]}


def test_huge_negative_schema_version_is_unsupported() -> None:
    raw = dumps(good()).replace(b'"schema_version": 1', b'"schema_version": -' + b"9" * 5000)
    with pytest.raises(DraftError) as info:
        parse_draft(raw)
    assert info.value.code == "unsupported_draft_version"


def test_huge_integer_in_meta_does_not_make_the_draft_unloadable() -> None:
    doc = good()
    doc["meta"] = {"n": 1}
    raw = dumps(doc).replace(b'"n": 1', b'"n": ' + b"9" * 5000)
    assert parse_draft(raw).data["meta"]["n"] > 0


def test_pr_over_the_cap_has_its_own_message_without_echo() -> None:
    doc = good()
    doc["pr"] = "github.com/" + CANARY + "x" * 2100 + "#7"
    errors = validate_document(doc)
    assert errors[0] == {"pointer": "/pr", "message": "is longer than 2048 characters"}
    assert CANARY not in repr(errors)
