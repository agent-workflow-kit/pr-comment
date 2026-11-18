"""Result objects and rendering: JSON on stdout, human text on stderr, uncaught exceptions."""

from __future__ import annotations

import argparse
import json
import logging
import unicodedata
from typing import Any

import pytest

from pr_comment import cli
from pr_comment.output import Outcome, check_outcome, error_outcome, internal_outcome, render
from tests.schema_harness import validate

PR = "github.com/example/repo#1"


def test_error_outcome_derives_the_exit_code_from_the_code() -> None:
    outcome = error_outcome("refused", "no", reason="not_open")
    assert outcome.exit_code == 20
    assert outcome.document is not None
    assert outcome.document["error"]["reason"] == "not_open"
    assert error_outcome("tool_missing", "no forgectl").exit_code == 12


def test_json_stdout_is_one_ascii_line_and_nothing_goes_to_stderr_on_success() -> None:
    stdout, stderr = render(Outcome(0, {"name": "café"}), as_json=True)
    assert stdout == '{"name": "caf\\u00e9"}\n'
    assert stderr == ""


def test_exit_zero_without_json_prints_nothing_but_its_human_lines() -> None:
    assert render(Outcome(0, {"a": 1}), as_json=False) == ("", "")
    assert render(Outcome(0, {"a": 1}, human=("posted",)), as_json=False) == ("", "posted\n")


def test_quiet_drops_human_lines_on_success() -> None:
    outcome = Outcome(0, {"a": 1}, human=("posted",))
    assert render(outcome, as_json=False, quiet=True) == ("", "")


def test_a_failure_prints_the_envelope_on_stdout_and_the_message_on_stderr() -> None:
    outcome = error_outcome("not_found", "no such PR", details={"what": "pr", "id": "x"})
    stdout, stderr = render(outcome, as_json=True)
    assert json.loads(stdout) == outcome.document
    assert stderr == "pr-comment: no such PR\n"


def test_quiet_keeps_the_one_line_error_and_verbose_adds_the_details() -> None:
    outcome = error_outcome("outcome_unknown", "timed out", retry_after=5)
    assert render(outcome, as_json=False, quiet=True)[1] == "pr-comment: timed out\n"
    verbose = render(outcome, as_json=False, verbose=True)[1]
    assert verbose.splitlines()[0] == "pr-comment: timed out"
    assert "code=outcome_unknown" in verbose
    assert "retry=same_command" in verbose
    assert "retry_after=5" in verbose
    assert render(outcome, as_json=False, quiet=True, verbose=True)[1] == "pr-comment: timed out\n"


def test_human_lines_are_sanitized() -> None:
    stdout, stderr = render(Outcome(0, None, human=("a\x1b[31mb",)), as_json=False)
    assert stdout == ""
    assert "\x1b" not in stderr


def test_a_result_document_is_printed_on_a_non_zero_exit_without_an_error_message() -> None:
    document = {"pr": PR, "action": "resolve", "dry_run": False, "threads": []}
    stdout, stderr = render(Outcome(12, document), as_json=True)
    assert json.loads(stdout) == document
    assert stderr == ""


@pytest.mark.parametrize(
    "result",
    [
        None,
        (0, None),
        Outcome("0"),  # pyright: ignore[reportArgumentType]
        Outcome(True),  # pyright: ignore[reportArgumentType]
        Outcome(3),
        Outcome(21),
        Outcome(-1),
        Outcome(0, ["a"]),  # pyright: ignore[reportArgumentType]
        Outcome(0, None, human=["a"]),  # pyright: ignore[reportArgumentType]
        Outcome(0, None, human=(1,)),  # pyright: ignore[reportArgumentType]
    ],
)
def test_check_outcome_rejects_malformed_results(result: object) -> None:
    with pytest.raises(TypeError):
        check_outcome(result)


@pytest.mark.parametrize(
    "result",
    [
        Outcome(0, {"error": {"code": "usage", "message": "x"}}),
        Outcome(1),
        Outcome(12, None),
        Outcome(1, {"error": "boom"}),
        Outcome(1, {"error": {"code": "internal"}}),
        Outcome(1, {"error": {"message": "m"}}),
        Outcome(1, {"error": {"code": "internal", "message": 5}}),
        Outcome(1, {"error": {"code": 5, "message": "m"}}),
        Outcome(1, {"error": {"code": "made_up", "message": "m"}}),
        Outcome(1, {"error": {"code": "usage", "message": "m"}}),
        Outcome(12, {"error": {"code": "refused", "message": "m"}}),
        Outcome(0, {"x": float("nan")}),
        Outcome(0, {"x": float("inf")}),
        Outcome(0, {"x": {1, 2}}),
    ],
)
def test_check_outcome_rejects_inconsistent_or_unserializable_outcomes(result: Outcome) -> None:
    with pytest.raises((TypeError, ValueError)):
        check_outcome(result)


def test_check_outcome_allows_result_documents_without_an_error_key_on_non_zero_exits() -> None:
    outcome = Outcome(12, {"pr": PR, "action": "resolve", "threads": []})
    assert check_outcome(outcome) is outcome
    envelope = error_outcome("usage", "x")
    assert check_outcome(envelope) is envelope


def test_json_output_is_strict_and_never_nan() -> None:
    with pytest.raises(ValueError):
        render(Outcome(0, {"x": float("nan")}), as_json=True)


def test_the_envelope_message_is_raw_in_json_and_marked_in_human_output() -> None:
    outcome = error_outcome("usage", "a\x1b[31mb\u202e")
    stdout, stderr = render(outcome, as_json=True)
    assert json.loads(stdout)["error"]["message"] == "a\x1b[31mb\u202e"
    assert "\x1b" not in stdout
    assert "\x1b" not in stderr
    assert "<U+001B>" in stderr
    assert "<U+202E>" in stderr


def test_check_outcome_accepts_every_defined_exit_code() -> None:
    for exit_code in (0, 1, 2, 10, 11, 12, 13, 20):
        outcome = Outcome(exit_code, {"x": 1})
        assert check_outcome(outcome) is outcome
    assert check_outcome(Outcome(0)).document is None


def test_an_uncaught_exception_is_logged_at_error_with_its_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger="pr_comment"):
        try:
            raise RuntimeError("secret detail")
        except RuntimeError as exc:
            outcome = internal_outcome(exc)
    assert outcome.exit_code == 1
    assert outcome.document is not None
    assert outcome.document["error"]["code"] == "internal"
    assert "secret detail" not in json.dumps(outcome.document)
    records = [r for r in caplog.records if r.name == "pr_comment"]
    assert [r.levelno for r in records] == [logging.ERROR]
    assert records[0].exc_info is not None


def test_main_logs_a_crashing_handler_and_exits_one(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom(args: argparse.Namespace) -> Outcome:
        raise RuntimeError("secret detail")

    monkeypatch.setitem(cli.HANDLERS, "export", boom)
    with caplog.at_level(logging.ERROR, logger="pr_comment"):
        code = cli.main(["export", "--json"])
    out = capsys.readouterr()
    assert code == 1
    document = json.loads(out.out)
    validate("pr-comment/cli/error.json", document)
    assert document["error"]["code"] == "internal"
    assert any(r.levelno == logging.ERROR and r.exc_info for r in caplog.records)


def _run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str, str]:
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_main_honors_quiet_and_verbose_on_a_failure(capsys: pytest.CaptureFixture[str]) -> None:
    base = ["resolve", PR, "--thread", "gh:rt:1"]
    code, out, err = _run(capsys, base)
    assert (code, out) == (1, "")
    assert err == "pr-comment: resolve is not implemented yet\n"
    assert _run(capsys, [*base, "--quiet"])[2] == err
    verbose = _run(capsys, [*base, "--verbose"])[2]
    assert verbose.startswith(err)
    assert "code=internal" in verbose


def test_a_usage_error_honors_quiet_and_verbose(capsys: pytest.CaptureFixture[str]) -> None:
    _, _, plain = _run(capsys, ["--nope"])
    _, _, verbose = _run(capsys, ["--nope", "--verbose"])
    assert "code=usage" in verbose
    assert "code=usage" not in plain


def test_the_full_envelope_reaches_json_stdout_for_the_unimplemented_stub(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = _run(
        capsys, ["resolve", "https://example.test/o/r/pull/1", "--thread", "gh:rt:1", "--json"]
    )
    document: dict[str, Any] = json.loads(out)
    assert code == 1
    assert len(document["error"]) == 13
    assert document["error"]["landed"] is None
    validate("pr-comment/cli/error.json", document)


def test_an_uncaught_exception_never_leaks_control_characters_to_stderr(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(args: argparse.Namespace) -> Outcome:
        raise RuntimeError("evil \x1b[31m \x07 \u202e text")

    monkeypatch.setitem(cli.HANDLERS, "export", boom)
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers.clear()  # pytest's capture handlers hide Python's last-resort handler
    try:
        code = cli.main(["export", "--json"])
    finally:
        root.handlers[:] = saved
    err = capsys.readouterr().err
    assert code == 1
    assert "Traceback" in err
    assert "RuntimeError" in err
    assert not any(unicodedata.category(c) in ("Cc", "Cf") and c not in "\n\t" for c in err)


def test_quiet_keeps_the_traceback_off_stderr(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(args: argparse.Namespace) -> Outcome:
        raise RuntimeError("x")

    monkeypatch.setitem(cli.HANDLERS, "export", boom)
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers.clear()
    try:
        cli.main(["export", "--quiet"])
    finally:
        root.handlers[:] = saved
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert err == "pr-comment: unexpected RuntimeError\n"


def test_main_leaves_no_log_handler_behind() -> None:
    before = list(logging.getLogger("pr_comment").handlers)
    cli.main(["export", "--json"])
    assert list(logging.getLogger("pr_comment").handlers) == before


def test_a_flag_abbreviation_is_a_usage_error_and_not_taken_for_quiet(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(["export", "--quie", "--verbose"])
    err = capsys.readouterr().err
    assert code == 2
    assert "code=usage" in err
