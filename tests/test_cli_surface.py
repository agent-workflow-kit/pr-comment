"""The frozen CLI surface (docs/interface.md, Commands and Global flags) and its dispatch."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from typing import Any

import pytest

from pr_comment import __version__, cli
from pr_comment.cli import build_parser, main
from tests.schema_harness import validate

GLOBAL_FLAGS = {
    "--json",
    "--config",
    "--dry-run",
    "--yes",
    "--expect-plan",
    "--override-escalation",
    "--actor",
    "--decision-id",
    "--hook",
    "--verbose",
    "--quiet",
    "--version",
    "--help",
    "-h",
}

# Command-specific flags, copied from the Commands section of docs/interface.md.
COMMAND_FLAGS: dict[str, set[str]] = {
    "review": {
        "--draft-file",
        "--approve",
        "--reanchor-to",
        "--expected-head",
        "--dedupe-key",
        "--allow-mention",
    },
    "comment": {"--body-file", "--expected-head", "--dedupe-key", "--allow-mention"},
    "reply": {"--to", "--body-file", "--expected-head", "--dedupe-key", "--allow-mention"},
    "approve": {
        "--body-file",
        "--expected-head",
        "--expected-base",
        "--dedupe-key",
        "--allow-mention",
    },
    "resolve": {"--thread"},
    "unresolve": {"--thread"},
    "draft check": set(),
    "draft new": {"--event", "--from"},
    "export": {"--since", "--log"},
}

# Minimal valid argv per command, enough to pass parsing.
PR = "https://example.test/o/r/pull/1"
MINIMAL: dict[str, list[str]] = {
    "review": ["review", PR, "--draft-file", "d.json"],
    "comment": ["comment", PR, "--body-file", "b.md"],
    "reply": ["reply", PR, "--to", "gh:ic:1", "--body-file", "b.md"],
    "approve": ["approve", PR],
    "resolve": ["resolve", PR, "--thread", "gh:rt:1"],
    "unresolve": ["unresolve", PR, "--thread", "gh:rt:1"],
    "draft check": ["draft", "check", "d.json"],
    "draft new": ["draft", "new", PR],
    "export": ["export"],
}
POSTING = {"review", "comment", "reply", "approve"}
NON_POSTING = sorted(set(MINIMAL) - POSTING)


def _flags(parser: argparse.ArgumentParser) -> set[str]:
    return {
        opt
        for action in parser._actions  # pyright: ignore[reportPrivateUsage]
        for opt in action.option_strings
    }


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:  # pyright: ignore[reportPrivateUsage]
        if isinstance(action, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage]
            return dict(action.choices)
    return {}


def _surface() -> dict[str, set[str]]:
    """Every command path of the real parser with its own (non-global) flags."""
    top = build_parser()
    out: dict[str, set[str]] = {}
    for name, sub in _subparsers(top).items():
        nested = _subparsers(sub)
        if nested:
            for inner, subsub in nested.items():
                out[f"{name} {inner}"] = _flags(subsub) - GLOBAL_FLAGS
        else:
            out[name] = _flags(sub) - GLOBAL_FLAGS
    return out


def test_parser_has_exactly_the_interface_commands_and_flags() -> None:
    assert _surface() == COMMAND_FLAGS


def test_top_level_flags_are_exactly_the_global_flags() -> None:
    assert _flags(build_parser()) == GLOBAL_FLAGS


@pytest.mark.parametrize("command", sorted(MINIMAL))
def test_every_command_accepts_every_global_flag(command: str) -> None:
    sha = "a" * 64
    argv = [
        *MINIMAL[command],
        "--json",
        "--config",
        "c.yaml",
        "--dry-run",
        "--yes",
        "--expect-plan",
        sha,
        "--override-escalation=forgectl",
        "--override-escalation=pr-comment",
        "--actor",
        "human:me",
        "--decision-id",
        "d1",
        "--decision-id",
        "d2",
        "--hook",
        "pre-post=true",
        "--verbose",
    ]
    args = build_parser().parse_args(argv)
    assert args.json is True
    assert args.decision_id == ["d1", "d2"]
    assert args.override_escalation == ["forgectl", "pr-comment"]
    assert args.hook == ["pre-post=true"]


def test_global_flags_work_before_the_command() -> None:
    args = build_parser().parse_args(["--json", "--yes", *MINIMAL["comment"]])
    assert args.json is True
    assert args.yes is True


def test_global_flag_defaults() -> None:
    args = build_parser().parse_args(MINIMAL["comment"])
    assert (args.json, args.dry_run, args.yes, args.verbose, args.quiet) == (False,) * 5
    assert args.config is None
    assert args.expect_plan is None
    assert args.actor is None
    assert args.decision_id == []
    assert args.override_escalation == []
    assert args.hook == []


def test_version_prints_version_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"pr-comment {__version__}"


def _run_json(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> tuple[int, dict[str, Any], str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


USAGE_CASES: list[tuple[str, list[str]]] = [
    ("no command", []),
    ("unknown command", ["frobnicate"]),
    ("unknown flag", ["comment", PR, "--body-file", "b", "--nope"]),
    ("missing required flag", ["comment", PR]),
    ("missing pr", ["comment", "--body-file", "b"]),
    ("flag of another command", ["comment", PR, "--body-file", "b", "--approve"]),
    ("draft without subcommand", ["draft"]),
    ("override without value", ["comment", PR, "--body-file", "b", "--override-escalation"]),
    ("override empty value", ["comment", PR, "--body-file", "b", "--override-escalation="]),
    ("override unknown scope", ["comment", PR, "--body-file", "b", "--override-escalation=x"]),
    ("bad actor", ["comment", PR, "--body-file", "b", "--actor", "robot:x"]),
    ("bad actor chars", ["comment", PR, "--body-file", "b", "--actor", "human:a b"]),
    ("bad decision id", ["comment", PR, "--body-file", "b", "--decision-id", "a b"]),
    ("empty decision id", ["comment", PR, "--body-file", "b", "--decision-id", ""]),
    ("long decision id", ["comment", PR, "--body-file", "b", "--decision-id", "a" * 129]),
    (
        "too many decision ids",
        ["comment", PR, "--body-file", "b", *["--decision-id=x"] * 33],
    ),
    ("bad hook shape", ["comment", PR, "--body-file", "b", "--hook", "pre-post"]),
    ("bad hook name", ["comment", PR, "--body-file", "b", "--hook", "other=true"]),
    ("empty hook cmd", ["comment", PR, "--body-file", "b", "--hook", "pre-post="]),
    ("bad expect-plan", ["comment", PR, "--body-file", "b", "--expect-plan", "abc"]),
    ("bad event", ["draft", "new", PR, "--event", "approve"]),
    ("too many threads", ["resolve", PR, *[x for i in range(101) for x in ("--thread", f"t{i}")]]),
]


@pytest.mark.parametrize(("label", "argv"), USAGE_CASES, ids=[c[0] for c in USAGE_CASES])
def test_usage_errors_exit_two_with_error_envelope_on_json_stdout(
    capsys: pytest.CaptureFixture[str], label: str, argv: list[str]
) -> None:
    code, doc, err = _run_json(capsys, [*argv, "--json"])
    assert code == 2
    assert doc["error"]["code"] == "usage"
    assert doc["error"]["message"]
    validate("pr-comment/cli/error.json", doc)
    assert err == "" or "usage" in err.lower()


def test_usage_error_without_json_goes_to_stderr_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["comment"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage" in captured.err.lower()


def test_json_flag_value_is_not_mistaken_for_the_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `--config --json` is a usage error (no value); the token is still present, so a caller that
    # asked for JSON gets the envelope rather than nothing.
    code = main(["comment", PR, "--body-file", "b", "--config", "--json"])
    captured = capsys.readouterr()
    assert code == 2
    assert json.loads(captured.out)["error"]["code"] == "usage"


@pytest.mark.parametrize("command", NON_POSTING)
def test_expect_plan_refused_on_non_posting_commands(
    capsys: pytest.CaptureFixture[str], command: str
) -> None:
    argv = [*MINIMAL[command], "--expect-plan", "a" * 64, "--json"]
    code, doc, _ = _run_json(capsys, argv)
    assert code == 2
    assert doc["error"]["code"] == "usage"
    assert "--expect-plan" in doc["error"]["message"]
    validate("pr-comment/cli/error.json", doc)


@pytest.mark.parametrize("command", sorted(POSTING))
def test_expect_plan_allowed_on_posting_commands(
    capsys: pytest.CaptureFixture[str], command: str
) -> None:
    argv = [*MINIMAL[command], "--expect-plan", "a" * 64, "--json"]
    code, doc, _ = _run_json(capsys, argv)
    # Dispatch reached the (unimplemented) handler: internal, not usage.
    assert code == 1
    assert doc["error"]["code"] == "internal"


@pytest.mark.parametrize("command", sorted(MINIMAL))
def test_unimplemented_handlers_return_internal_envelope(
    capsys: pytest.CaptureFixture[str],
    golden_json: Callable[[str, Any], None],
    command: str,
) -> None:
    code, doc, _ = _run_json(capsys, [*MINIMAL[command], "--json"])
    assert code == 1
    assert doc["error"]["code"] == "internal"
    validate("pr-comment/cli/error.json", doc)
    golden_json(f"cli/unimplemented-{command.replace(' ', '-')}.json", doc)


def test_unimplemented_handler_human_output_goes_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(MINIMAL["export"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not implemented" in captured.err


def test_dispatch_calls_the_registered_handler_with_parsed_args(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[argparse.Namespace] = []

    def handler(args: argparse.Namespace) -> cli.Outcome:
        seen.append(args)
        return cli.Outcome(0, {"ok": True})

    monkeypatch.setitem(cli.HANDLERS, "draft check", handler)
    code, doc, _ = _run_json(capsys, ["draft", "check", "x.json", "--json"])
    assert code == 0
    assert doc == {"ok": True}
    assert seen[0].path == "x.json"
    assert seen[0].command == "draft check"


def test_every_command_has_a_handler_entry() -> None:
    assert set(cli.HANDLERS) == set(COMMAND_FLAGS)


def test_repeatable_flags_collect(capsys: pytest.CaptureFixture[str]) -> None:
    args = build_parser().parse_args(
        [*MINIMAL["comment"], "--allow-mention", "a", "--allow-mention", "b"]
    )
    assert args.allow_mention == ["a", "b"]
    args = build_parser().parse_args(["resolve", PR, "--thread", "x", "--thread", "y"])
    assert args.thread == ["x", "y"]


def test_exactly_100_threads_parse() -> None:
    argv = ["resolve", PR, *[x for i in range(100) for x in ("--thread", f"t{i}")]]
    assert len(build_parser().parse_args(argv).thread) == 100


def test_exactly_32_decision_ids_parse() -> None:
    argv = [*MINIMAL["comment"], *["--decision-id=x"] * 32]
    assert len(build_parser().parse_args(argv).decision_id) == 32


def test_global_list_flags_before_and_after_the_command_are_merged() -> None:
    argv = ["--decision-id", "a", "--hook", "pre-post=x", *MINIMAL["comment"], "--decision-id", "b"]
    args = build_parser().parse_args(argv)
    assert args.decision_id == ["a", "b"]
    assert args.hook == ["pre-post=x"]


def test_flag_abbreviations_are_refused(capsys: pytest.CaptureFixture[str]) -> None:
    code, doc, _ = _run_json(capsys, [*MINIMAL["comment"], "--dry", "--json"])
    assert code == 2
    assert doc["error"]["code"] == "usage"


def test_a_crashing_handler_still_yields_the_internal_envelope(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(args: argparse.Namespace) -> cli.Outcome:
        raise RuntimeError("secret detail")

    monkeypatch.setitem(cli.HANDLERS, "export", boom)
    code, doc, _ = _run_json(capsys, ["export", "--json"])
    assert code == 1
    assert doc["error"]["code"] == "internal"
    assert "secret detail" not in json.dumps(doc)
    validate("pr-comment/cli/error.json", doc)


def test_json_scan_ignores_tokens_after_double_dash() -> None:
    assert cli._wants_json(["a", "--json"])  # pyright: ignore[reportPrivateUsage]
    assert not cli._wants_json(["a", "--", "--json"])  # pyright: ignore[reportPrivateUsage]
