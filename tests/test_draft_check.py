"""``pr-comment draft check``: offline draft validation and its result document.

The draft is untrusted: no result, human line or error may echo its content.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pr_comment import cli
from pr_comment.cli import main
from tests.schema_harness import validate
from tests.test_draft import CANARY, DRAFT_ID, good

Doc = dict[str, Any]


def write(tmp_path: Path, doc: object, name: str = "draft.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, Doc, str]:
    code = main([*argv, "--json"])
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def test_valid_draft_exits_0_with_the_result_document(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, golden_json: Callable[[str, Any], None]
) -> None:
    code, doc, _ = run(capsys, ["draft", "check", write(tmp_path, good())])
    assert code == 0
    assert doc == {
        "valid": True,
        "schema_version": 1,
        "draft_id": DRAFT_ID,
        "findings": 3,
        "errors": [],
    }
    validate("pr-comment/cli/draft-check.json", doc)
    golden_json("draft/check-valid.json", doc)


def test_schema_error_exits_2_with_pointer_and_null_kind(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, golden_json: Callable[[str, Any], None]
) -> None:
    bad = good()
    bad["findings"][0]["line"] = 0
    code, doc, _ = run(capsys, ["draft", "check", write(tmp_path, bad)])
    assert code == 2
    assert doc["valid"] is False
    assert doc["errors"] == [
        {
            "pointer": "/findings/0/line",
            "message": "must be an integer of at least 1, or null",
            "kind": None,
        }
    ]
    assert doc["findings"] == 3
    validate("pr-comment/cli/draft-check.json", doc)
    golden_json("draft/check-schema-error.json", doc)


def test_blank_finding_body_exits_2_at_the_finding_body_pointer(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    bad = good()
    bad["findings"][0]["body"] = "\n\r\n "
    bad["unanchored"][0]["finding"]["body"] = " \t"
    code, doc, err = run(capsys, ["draft", "check", write(tmp_path, bad)])
    assert code == 2
    assert doc["valid"] is False
    assert doc["errors"] == [
        {
            "pointer": "/findings/0/body",
            "message": "must contain a character that is not blank",
            "kind": None,
        },
        {
            "pointer": "/unanchored/0/finding/body",
            "message": "must contain a character that is not blank",
            "kind": None,
        },
    ]
    validate("pr-comment/cli/draft-check.json", doc)


def test_invalid_text_error_carries_the_kind(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, golden_json: Callable[[str, Any], None]
) -> None:
    bad = good()
    bad["findings"][0]["body"] = f"{CANARY}\x00"
    bad["body"] = "```suggestion\nx\n```"
    code, doc, err = run(capsys, ["draft", "check", write(tmp_path, bad)])
    assert code == 2
    assert [(e["pointer"], e["kind"]) for e in doc["errors"]] == [
        ("/findings/0/body", "control_character"),
        ("/body", "suggestion_fence"),
    ]
    assert CANARY not in json.dumps(doc) + err
    validate("pr-comment/cli/draft-check.json", doc)
    golden_json("draft/check-invalid-text.json", doc)


def test_field_with_control_character_is_not_also_fence_checked(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    bad = good()
    bad["body"] = "```suggestion\nx\n```\x00"
    _, doc, _ = run(capsys, ["draft", "check", write(tmp_path, bad)])
    assert [e["kind"] for e in doc["errors"]] == ["control_character"]


def test_schema_and_text_errors_are_reported_together(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    bad = good()
    bad["event"] = "nope"
    bad["body"] = "a\x00"
    _, doc, _ = run(capsys, ["draft", "check", write(tmp_path, bad)])
    assert [(e["pointer"], e["kind"]) for e in doc["errors"]] == [
        ("/event", None),
        ("/body", "control_character"),
    ]
    validate("pr-comment/cli/draft-check.json", doc)


def test_unreadable_file_has_null_metadata(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, golden_json: Callable[[str, Any], None]
) -> None:
    code, doc, err = run(capsys, ["draft", "check", str(tmp_path / "missing.json")])
    assert code == 2
    assert doc == {
        "valid": False,
        "schema_version": None,
        "draft_id": None,
        "findings": None,
        "errors": [{"pointer": "", "message": "draft file cannot be opened", "kind": None}],
    }
    assert "missing.json" not in json.dumps(doc) + err
    validate("pr-comment/cli/draft-check.json", doc)
    golden_json("draft/check-unreadable.json", doc)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b"\xff\xfe",
        b"\xef\xbb\xbf{}",
        b'{"a": 1, "a": 2}',
        b"[1, 2]",
        b"NaN",
        b"[" * 100000,
    ],
)
def test_malformed_input_is_one_root_error(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, raw: bytes
) -> None:
    path = tmp_path / "d.json"
    path.write_bytes(raw)
    code, doc, _ = run(capsys, ["draft", "check", str(path)])
    assert code == 2
    assert doc["valid"] is False
    assert doc["errors"]
    assert doc["schema_version"] is None
    assert doc["draft_id"] is None
    assert doc["findings"] is None
    validate("pr-comment/cli/draft-check.json", doc)


def test_unsupported_version_reports_the_version(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    bad = good()
    bad["schema_version"] = 2
    code, doc, _ = run(capsys, ["draft", "check", write(tmp_path, bad)])
    assert code == 2
    assert doc["schema_version"] == 2
    assert doc["errors"][0]["pointer"] == "/schema_version"
    validate("pr-comment/cli/draft-check.json", doc)


def test_untrusted_metadata_is_nulled_not_echoed(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    bad = good()
    bad["draft_id"] = f"{CANARY}-not-an-id"
    bad["schema_version"] = "1"
    bad["findings"] = {"x": 1}
    _, doc, err = run(capsys, ["draft", "check", write(tmp_path, bad)])
    assert doc["draft_id"] is None
    assert doc["schema_version"] is None
    assert doc["findings"] is None
    assert CANARY not in json.dumps(doc) + err
    validate("pr-comment/cli/draft-check.json", doc)


def test_boolean_schema_version_is_not_an_integer(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    bad = good()
    bad["schema_version"] = True
    _, doc, _ = run(capsys, ["draft", "check", write(tmp_path, bad)])
    assert doc["schema_version"] is None


@pytest.mark.parametrize("base_ref", ["refs/heads/x", "refs", "@", ".hidden/x", "a/.b", "a..b"])
def test_base_ref_uses_the_branch_grammar(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, base_ref: str
) -> None:
    bad = good()
    bad["anchor"]["base_ref"] = base_ref
    code, doc, _ = run(capsys, ["draft", "check", write(tmp_path, bad)])
    assert code == 2
    assert [e["pointer"] for e in doc["errors"]] == ["/anchor/base_ref"]


def test_hostile_draft_never_echoes_content(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    bad = good()
    bad["pr"] = CANARY
    bad["findings"][0]["id"] = f"{CANARY} bad id"
    bad["findings"][1]["path"] = f"{CANARY}\n"
    bad["findings"][2]["title"] = f"{CANARY}‮"
    bad["extra"] = CANARY
    bad["meta"] = CANARY
    code = main(["draft", "check", write(tmp_path, bad), "--json"])
    captured = capsys.readouterr()
    assert code == 2
    assert CANARY not in captured.out + captured.err


def test_many_errors_are_capped(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    bad = good()
    bad["findings"] = [{"id": f"f{i}", "path": "a", "line": 1, "body": "x\x00"} for i in range(300)]
    _, doc, _ = run(capsys, ["draft", "check", write(tmp_path, bad)])
    assert len(doc["errors"]) == 101
    assert doc["errors"][-1] == {"pointer": "", "message": "further errors omitted", "kind": None}
    assert doc["findings"] == 300
    validate("pr-comment/cli/draft-check.json", doc)


def test_schema_and_text_errors_share_one_cap(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    bad = good()
    bad["findings"] = [
        {"id": f"f{i}", "path": "a", "line": 0 if i < 150 else 1, "body": "x\x00"}
        for i in range(300)
    ]
    _, doc, _ = run(capsys, ["draft", "check", write(tmp_path, bad)])
    errors = doc["errors"]
    assert len(errors) == 101
    omitted = [e for e in errors if e["message"] == "further errors omitted"]
    assert len(omitted) == 1
    assert errors[-1] is omitted[0]
    assert errors[-1]["kind"] is None
    assert all(e["kind"] is None for e in errors[:100])
    validate("pr-comment/cli/draft-check.json", doc)


@pytest.mark.parametrize("digits", [3000, 5000])
def test_huge_schema_version_does_not_crash(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, digits: int
) -> None:
    path = tmp_path / "draft.json"
    path.write_text('{"schema_version": ' + "9" * digits + "}", encoding="utf-8")
    code = main(["draft", "check", str(path), "--json"])
    captured = capsys.readouterr()
    assert code == 2
    assert "Traceback" not in captured.err
    doc = json.loads(captured.out)
    assert "error" not in doc
    assert doc["valid"] is False
    assert doc["schema_version"] is None
    validate("pr-comment/cli/draft-check.json", doc)


def test_stdin_input(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    class Stdin:
        buffer = io.BytesIO(json.dumps(good()).encode())

    monkeypatch.setattr(sys, "stdin", Stdin)
    code, doc, _ = run(capsys, ["draft", "check", "-"])
    assert code == 0
    assert doc["valid"] is True


def test_stdin_invalid_draft_has_a_pointer(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = good()
    bad["findings"][0]["id"] = bad["findings"][1]["id"]

    class Stdin:
        buffer = io.BytesIO(json.dumps(bad).encode())

    monkeypatch.setattr(sys, "stdin", Stdin)
    code, doc, _ = run(capsys, ["draft", "check", "-"])
    assert code == 2
    assert doc["errors"][0]["pointer"] == "/findings/1/id"
    assert doc["errors"][0]["kind"] is None


def test_meta_passes_through_without_error(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    doc = good()
    doc["meta"] = {"review_run": {"x": [1, 2, {"y": None}]}}
    code, result, _ = run(capsys, ["draft", "check", write(tmp_path, doc)])
    assert code == 0
    assert result["valid"] is True


def test_meta_text_is_not_pipeline_checked(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    doc = good()
    doc["meta"] = {"review_run": "a\x01b ```suggestion\nx\n```"}
    code, result, _ = run(capsys, ["draft", "check", write(tmp_path, doc)])
    assert code == 0
    assert result["valid"] is True
    assert result["errors"] == []


def test_non_object_meta_is_a_meta_error(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    doc = good()
    doc["meta"] = "text"
    code, result, _ = run(capsys, ["draft", "check", write(tmp_path, doc)])
    assert code == 2
    assert [e["pointer"] for e in result["errors"]] == ["/meta"]


# --- human output ----------------------------------------------------------------------------


def test_human_output_valid(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code = main(["draft", "check", write(tmp_path, good())])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == ""
    assert "valid" in captured.err
    assert "3 findings" in captured.err


def test_human_output_invalid_lists_pointer_and_message(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    bad = good()
    bad["findings"][0]["body"] = f"{CANARY}\x00"
    code = main(["draft", "check", write(tmp_path, bad)])
    captured = capsys.readouterr()
    assert code == 2
    assert "/findings/0/body" in captured.err
    assert "control character" in captured.err
    assert CANARY not in captured.err


def test_quiet_drops_the_human_lines(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code = main(["draft", "check", write(tmp_path, good()), "--quiet"])
    assert code == 0
    assert capsys.readouterr().err == ""


def test_quiet_drops_the_human_lines_of_an_invalid_draft(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    bad = good()
    bad["findings"][0]["line"] = 0
    code = main(["draft", "check", write(tmp_path, bad), "--quiet"])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.err == ""


def test_failure_without_error_details_falls_back_to_the_message() -> None:
    from pr_comment.draft import DraftError, _failure_entries

    entries = _failure_entries(DraftError("invalid_draft", "draft could not be read"))
    assert entries == [{"pointer": "", "message": "draft could not be read", "kind": None}]


@pytest.mark.parametrize(
    "mutate",
    [
        {"valid": True, "schema_version": None},
        {"valid": True, "draft_id": None},
        {"valid": True, "findings": None},
        {"valid": True, "errors": [{"pointer": "", "message": "m", "kind": None}]},
        {"valid": False, "errors": []},
    ],
)
def test_schema_rejects_inconsistent_results(mutate: Doc) -> None:
    from jsonschema import ValidationError

    doc: Doc = {
        "valid": True,
        "schema_version": 1,
        "draft_id": DRAFT_ID,
        "findings": 0,
        "errors": [],
    }
    doc.update(mutate)
    with pytest.raises(ValidationError):
        validate("pr-comment/cli/draft-check.json", doc)


def test_handler_is_registered() -> None:
    assert cli.HANDLERS["draft check"].__name__ != "_not_implemented"


# --- offline ---------------------------------------------------------------------------------


def test_draft_check_starts_no_forgectl_and_writes_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    fake = tmp_path / "bin"
    for directory in (home, xdg, fake):
        directory.mkdir()
    marker = tmp_path / "forgectl-ran"
    stub = fake / "forgectl"
    stub.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n", encoding="utf-8")
    stub.chmod(0o755)
    draft = tmp_path / "draft.json"
    draft.write_text(json.dumps(good()), encoding="utf-8")
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(xdg),
        "XDG_STATE_HOME": str(xdg),
        "XDG_DATA_HOME": str(xdg),
        "PATH": f"{fake}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    result = subprocess.run(
        [sys.executable, "-m", "pr_comment", "draft", "check", str(draft), "--json"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["valid"] is True
    assert not marker.exists()
    assert list(home.iterdir()) == []
    assert list(xdg.iterdir()) == []


def test_draft_check_does_not_use_the_gateway(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pr_comment import gateway

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("gateway used")

    monkeypatch.setattr(gateway, "run_process", boom)
    monkeypatch.setattr(gateway.ProcessGateway, "__init__", boom)
    assert main(["draft", "check", write(tmp_path, good()), "--json"]) == 0


# --- the inline comment cap (#70) --------------------------------------------------------------

CAP_MESSAGE = (
    "has more than 200 findings with a line; "
    "forgectl accepts at most 200 inline comments in one review"
)


def _inline_doc(inline: int, *, file_level: int = 0) -> Doc:
    doc = good()
    findings = [
        {"id": f"i{n}", "path": "src/a.py", "line": n + 1, "body": "text"} for n in range(inline)
    ]
    findings += [
        {"id": f"g{n}", "path": "src/a.py", "line": None, "body": "text"} for n in range(file_level)
    ]
    doc["findings"] = findings
    doc["unanchored"] = []
    return doc


def test_200_findings_with_a_line_are_valid(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code, doc, _ = run(capsys, ["draft", "check", write(tmp_path, _inline_doc(200))])
    assert code == 0 and doc["valid"] is True


def test_201_findings_with_a_line_exit_2_with_the_fixed_message(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code, doc, _ = run(capsys, ["draft", "check", write(tmp_path, _inline_doc(201))])
    assert code == 2
    assert doc["errors"] == [{"pointer": "/findings", "message": CAP_MESSAGE, "kind": None}]
    validate("pr-comment/cli/draft-check.json", doc)


def test_the_cap_entry_survives_the_100_entry_truncation(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    raw = _inline_doc(201)
    for item in raw["findings"]:
        item["start_line"] = 500  # a schema error per finding, so the schema errors fill the cap
    code, doc, _ = run(capsys, ["draft", "check", write(tmp_path, raw)])
    assert code == 2
    errors = doc["errors"]
    assert len(errors) == 101
    assert {"pointer": "/findings", "message": CAP_MESSAGE, "kind": None} in errors
    assert errors[-1]["message"] == "further errors omitted"
    validate("pr-comment/cli/draft-check.json", doc)


def test_a_path_marker_finding_still_counts_in_draft_check(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    # draft check is conservative: review would move this finding to the body, check cannot know
    raw = _inline_doc(201)
    raw["findings"][0]["path"] = "x<U+202E>y.py"
    code, doc, _ = run(capsys, ["draft", "check", write(tmp_path, raw)])
    assert code == 2
    assert {"pointer": "/findings", "message": CAP_MESSAGE, "kind": None} in doc["errors"]


def test_file_level_findings_and_unanchored_entries_do_not_count(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    raw = _inline_doc(200, file_level=100)
    raw["unanchored"] = [
        {
            "finding": {"id": f"u{n}", "path": "src/a.py", "line": 3, "body": "text"},
            "head_sha": "a" * 40,
            "reason": "changed",
        }
        for n in range(100)
    ]
    code, doc, _ = run(capsys, ["draft", "check", write(tmp_path, raw)])
    assert code == 0, doc
