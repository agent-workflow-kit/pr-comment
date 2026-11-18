"""The frozen CLI surface (docs/interface.md, Commands and Global flags) and its dispatch."""

from __future__ import annotations

import argparse
import importlib.metadata
import io
import json
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from pr_comment import cli, errors
from pr_comment.cli import build_parser, main
from tests.schema_harness import validate

INTERFACE_DOC = Path(__file__).resolve().parent.parent / "docs" / "interface.md"


# --- the surface, read from docs/interface.md -----------------------------------------------


@dataclass(frozen=True)
class FlagSpec:
    takes_value: bool
    required: bool = False
    repeatable: bool = False
    choices: frozenset[str] | None = None


@dataclass(frozen=True)
class CommandSpec:
    positionals: tuple[str, ...]
    flags: dict[str, FlagSpec]


def _doc_section(text: str, heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## )", text, re.S | re.M)
    assert match, f"section {heading!r} not found in docs/interface.md"
    return match.group(1)


def _parse_group(
    text: str,
    *,
    required: bool,
    repeatable: bool,
    flags: dict[str, FlagSpec],
    positionals: list[str],
) -> None:
    tokens = text.split()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("--"):
            value: str | None = None
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                i += 1
                value = tokens[i]
            old = flags.get(token)
            flags[token] = FlagSpec(
                takes_value=value is not None,
                required=required or (old.required if old else False),
                repeatable=repeatable or (old is not None),
                choices=(
                    frozenset(value.split("|"))
                    if value is not None and not value.startswith("<")
                    else None
                ),
            )
        else:
            name = re.fullmatch(r"<([a-z]+)>(?:\|-)?", token)
            assert name, f"unparsable synopsis token {token!r}"
            positionals.append(name.group(1))
        i += 1


def parse_synopsis(synopsis: str) -> dict[str, CommandSpec]:
    """``pr-comment <words> <items>`` into one CommandSpec per command name."""
    body = synopsis.removeprefix("pr-comment ")
    words: list[str] = []
    rest = body
    while rest and rest[0] not in "<[-":
        word, _, rest = rest.partition(" ")
        words.append(word)
    flags: dict[str, FlagSpec] = {}
    positionals: list[str] = []
    for part in re.split(r"(\[[^\]]*\](?:\.\.\.)?)", rest):
        if part.startswith("["):
            inner = re.fullmatch(r"\[([^\]]*)\](\.\.\.)?", part)
            assert inner
            _parse_group(
                inner.group(1),
                required=False,
                repeatable=bool(inner.group(2)),
                flags=flags,
                positionals=positionals,
            )
        else:
            _parse_group(
                part, required=True, repeatable=False, flags=flags, positionals=positionals
            )
    spec = CommandSpec(tuple(positionals), flags)
    return {name: spec for name in " ".join(words).split("|")}


def doc_commands() -> dict[str, CommandSpec]:
    section = _doc_section(INTERFACE_DOC.read_text(encoding="utf-8"), "Commands")
    out: dict[str, CommandSpec] = {}
    for line in section.splitlines():
        if re.fullmatch(r"`pr-comment [^`]+`", line):
            for name, spec in parse_synopsis(line.strip("`")).items():
                assert name not in out
                out[name] = spec
    return out


def doc_globals() -> dict[str, FlagSpec]:
    section = _doc_section(INTERFACE_DOC.read_text(encoding="utf-8"), "Global flags")
    out: dict[str, FlagSpec] = {}
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        for span in re.findall(r"`([^`]*)`", cells[0]):
            name = re.match(r"--[a-z-]+", span)
            assert name, f"unparsable global flag {span!r}"
            out[name.group(0)] = FlagSpec(
                takes_value=len(span) > len(name.group(0)),
                repeatable="repeatable" in cells[1].lower(),
            )
    return out


def _parser_flags(parser: argparse.ArgumentParser) -> dict[str, FlagSpec]:
    out: dict[str, FlagSpec] = {}
    for action in parser._actions:  # pyright: ignore[reportPrivateUsage]
        if not action.option_strings or "-h" in action.option_strings:
            continue
        spec = FlagSpec(
            takes_value=action.nargs != 0,
            required=action.required,
            repeatable=isinstance(action, cli.AppendAction),
            choices=frozenset(action.choices) if action.choices else None,
        )
        for opt in action.option_strings:
            out[opt] = spec
    return out


def _positionals(parser: argparse.ArgumentParser) -> tuple[str, ...]:
    return tuple(
        a.dest
        for a in parser._actions  # pyright: ignore[reportPrivateUsage]
        if not a.option_strings and not isinstance(a, argparse._SubParsersAction)  # pyright: ignore[reportPrivateUsage]
    )


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:  # pyright: ignore[reportPrivateUsage]
        if isinstance(action, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage]
            return dict(action.choices)
    return {}


def parser_tree() -> dict[str, argparse.ArgumentParser]:
    """Every parser of the real CLI: ``""`` is the top, ``draft`` the group, the rest leaves."""
    top = build_parser()
    out: dict[str, argparse.ArgumentParser] = {"": top}
    for name, sub in _subparsers(top).items():
        out[name] = sub
        for inner, leaf in _subparsers(sub).items():
            out[f"{name} {inner}"] = leaf
    return out


def diff_flags(expected: dict[str, FlagSpec], actual: dict[str, FlagSpec], where: str) -> list[str]:
    problems = [f"{where}: missing {opt}" for opt in sorted(expected.keys() - actual.keys())]
    problems += [f"{where}: unexpected {opt}" for opt in sorted(actual.keys() - expected.keys())]
    for opt in sorted(expected.keys() & actual.keys()):
        if expected[opt] != actual[opt]:
            problems.append(f"{where}: {opt} differs: doc {expected[opt]}, parser {actual[opt]}")
    return problems


def diff_surface(
    commands: dict[str, CommandSpec],
    globals_: dict[str, FlagSpec],
    parsers: dict[str, argparse.ArgumentParser],
) -> list[str]:
    problems: list[str] = []
    leaves = {name: p for name, p in parsers.items() if name not in ("", "draft")}
    problems += [f"missing command {c}" for c in sorted(commands.keys() - leaves.keys())]
    problems += [f"unexpected command {c}" for c in sorted(leaves.keys() - commands.keys())]
    for name in ("", "draft"):
        where = f"globals of {name or 'top'}"
        problems += diff_flags(globals_, _parser_flags(parsers[name]), where)
    for name in sorted(commands.keys() & leaves.keys()):
        actual = _parser_flags(leaves[name])
        own = {opt: spec for opt, spec in actual.items() if opt not in globals_}
        problems += diff_flags(commands[name].flags, own, name)
        if _positionals(leaves[name]) != commands[name].positionals:
            problems.append(
                f"{name}: positionals differ: doc {commands[name].positionals}, "
                f"parser {_positionals(leaves[name])}"
            )
        problems += diff_flags(
            globals_, {o: s for o, s in actual.items() if o in globals_}, f"globals of {name}"
        )
    return problems


def test_parser_matches_the_commands_and_global_flags_of_interface_md() -> None:
    assert diff_surface(doc_commands(), doc_globals(), parser_tree()) == []


def test_doc_parse_finds_the_expected_shape() -> None:
    commands = doc_commands()
    assert set(commands) == {
        "review",
        "comment",
        "reply",
        "approve",
        "resolve",
        "unresolve",
        "draft check",
        "draft new",
        "export",
    }
    assert commands["resolve"] == commands["unresolve"]
    assert commands["resolve"].flags["--thread"] == FlagSpec(
        takes_value=True, required=True, repeatable=True
    )
    assert commands["draft new"].flags["--event"].choices == {"comment", "request_changes"}
    assert commands["draft check"].positionals == ("path",)
    assert commands["review"].flags["--draft-file"].required
    assert commands["review"].flags["--allow-mention"] == FlagSpec(
        takes_value=True, repeatable=True
    )
    assert doc_globals()["--decision-id"].repeatable
    assert not doc_globals()["--json"].takes_value


def test_surface_comparison_catches_a_missing_extra_or_changed_flag() -> None:
    commands = doc_commands()
    globals_ = doc_globals()
    parsers = parser_tree()
    missing = {
        **commands,
        "comment": replace(
            commands["comment"],
            flags={**commands["comment"].flags, "--ghost": FlagSpec(takes_value=True)},
        ),
    }
    assert any("missing --ghost" in p for p in diff_surface(missing, globals_, parsers))
    flags = dict(commands["export"].flags)
    del flags["--log"]
    extra = {**commands, "export": replace(commands["export"], flags=flags)}
    assert any("unexpected --log" in p for p in diff_surface(extra, globals_, parsers))
    changed = {
        **commands,
        "review": replace(
            commands["review"],
            flags={
                **commands["review"].flags,
                "--draft-file": FlagSpec(takes_value=True, required=False),
            },
        ),
    }
    assert any("--draft-file differs" in p for p in diff_surface(changed, globals_, parsers))
    no_json = {k: v for k, v in globals_.items() if k != "--json"}
    assert any("unexpected --json" in p for p in diff_surface(commands, no_json, parsers))


EXPECTED_VERSION_LINE = f"pr-comment {importlib.metadata.version('pr-comment')}"

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
SHA = "a" * 64

GLOBAL_ARGV = [
    "--json",
    "--config",
    "c.yaml",
    "--dry-run",
    "--yes",
    "--expect-plan",
    SHA,
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
    "--hook",
    "post-post=false",
    "--verbose",
    "--quiet",
]
GLOBAL_EXPECTED: dict[str, Any] = {
    "json": True,
    "config": "c.yaml",
    "dry_run": True,
    "yes": True,
    "expect_plan": SHA,
    "override_escalation": ["forgectl", "pr-comment"],
    "actor": "human:me",
    "decision_id": ["d1", "d2"],
    "hook": ["pre-post=true", "post-post=false"],
    "verbose": True,
    "quiet": True,
}


def _with_globals(command: str, placement: str) -> list[str]:
    argv = MINIMAL[command]
    if placement == "before":
        return [*GLOBAL_ARGV, *argv]
    if placement == "after":
        return [*argv, *GLOBAL_ARGV]
    assert argv[0] == "draft"
    return [argv[0], *GLOBAL_ARGV, *argv[1:]]


def test_global_argv_covers_every_documented_global_flag() -> None:
    used = {t.split("=")[0] for t in GLOBAL_ARGV if t.startswith("--")}
    assert used == set(doc_globals()) - {"--version"}


PLACEMENTS = [(c, p) for c in sorted(MINIMAL) for p in ("before", "after")] + [
    ("draft check", "between"),
    ("draft new", "between"),
]


@pytest.mark.parametrize(("command", "placement"), PLACEMENTS)
def test_every_command_accepts_every_global_flag_in_every_placement(
    command: str, placement: str
) -> None:
    args = build_parser().parse_args(_with_globals(command, placement))
    for dest, expected in GLOBAL_EXPECTED.items():
        assert getattr(args, dest) == expected, dest
    assert args.command == command


def test_global_flag_defaults() -> None:
    args = build_parser().parse_args(MINIMAL["comment"])
    assert (args.json, args.dry_run, args.yes, args.verbose, args.quiet) == (False,) * 5
    assert args.config is None
    assert args.expect_plan is None
    assert args.actor is None
    assert args.decision_id == []
    assert args.override_escalation == []
    assert args.hook == []


def test_global_flags_in_all_three_positions_of_draft_are_merged() -> None:
    argv = ["--decision-id", "a", "draft", "--decision-id", "b", "check", "x", "--decision-id", "c"]
    assert build_parser().parse_args(argv).decision_id == ["a", "b", "c"]


@pytest.mark.parametrize("command", sorted(MINIMAL))
def test_version_is_accepted_after_every_command(
    capsys: pytest.CaptureFixture[str], command: str
) -> None:
    with pytest.raises(SystemExit) as exc:
        main([*MINIMAL[command], "--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == EXPECTED_VERSION_LINE


def test_version_is_accepted_between_draft_and_its_subcommand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["draft", "--version", "check", "x"])
    assert exc.value.code == 0
    assert "pr-comment" in capsys.readouterr().out


def test_version_prints_the_installed_metadata_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == EXPECTED_VERSION_LINE


def test_version_falls_back_to_the_module_version_without_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    assert cli._version() == cli.__version__  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("argv", [["-h"], ["--help"], ["comment", "-h"], ["draft", "check", "-h"]])
@pytest.mark.parametrize("position", ["first", "last"])
def test_help_is_plain_text_and_exit_zero_even_with_json(
    capsys: pytest.CaptureFixture[str], argv: list[str], position: str
) -> None:
    full = ["--json", *argv] if position == "first" else [*argv, "--json"]
    with pytest.raises(SystemExit) as exc:
        main(full)
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage:")
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def _run_raw(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _run_json(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> tuple[int, dict[str, Any], str]:
    code, out, err = _run_raw(capsys, argv)
    return code, json.loads(out), err


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
    (
        "too many threads",
        ["resolve", PR, *[x for i in range(101) for x in ("--thread", f"gh:rt:t{i}")]],
    ),
    ("json with a value", ["comment", PR, "--body-file", "b", "--json=1"]),
    ("bad flag between draft and subcommand", ["draft", "--nope", "check", "x"]),
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
    assert "usage" in err.lower()


def _without(argv: list[str], item: str) -> list[str]:
    if item.startswith("--"):
        i = argv.index(item)
        return [*argv[:i], *argv[i + 2 :]]
    token = {"pr": PR, "path": "d.json"}[item]
    return [t for t in argv if t != token]


MISSING_REQUIRED = [
    (name, item)
    for name, spec in sorted(doc_commands().items())
    for item in (*spec.positionals, *(o for o, f in sorted(spec.flags.items()) if f.required))
]


@pytest.mark.parametrize(("command", "item"), MISSING_REQUIRED)
def test_each_documented_required_item_is_required(
    capsys: pytest.CaptureFixture[str], command: str, item: str
) -> None:
    code, doc, err = _run_json(capsys, [*_without(MINIMAL[command], item), "--json"])
    assert code == 2
    assert doc["error"]["code"] == "usage"
    assert "usage" in err.lower()


def test_a_command_with_nothing_required_still_refuses_a_stray_argument(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, doc, err = _run_json(capsys, ["export", "stray", "--json"])
    assert code == 2
    assert doc["error"]["code"] == "usage"
    assert "usage" in err.lower()


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


@pytest.mark.parametrize("flag", ["--json=1", "--json=", "--json=false"])
def test_json_with_a_value_still_gets_an_envelope(
    capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    code, doc, _ = _run_json(capsys, ["comment", PR, "--body-file", "b", flag])
    assert code == 2
    assert doc["error"]["code"] == "usage"


def test_json_scan_ignores_tokens_after_double_dash() -> None:
    assert cli._wants_json(["a", "--json"])  # pyright: ignore[reportPrivateUsage]
    assert cli._wants_json(["a", "--json=1"])  # pyright: ignore[reportPrivateUsage]
    assert not cli._wants_json(["a", "--", "--json"])  # pyright: ignore[reportPrivateUsage]
    assert not cli._wants_json(["a", "--", "--json=1"])  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "argv",
    [
        ["--", *MINIMAL["comment"]],
        ["--json", "--", *MINIMAL["comment"]],
        ["--config", "x", "--", *MINIMAL["comment"]],
        ["--config", "--", *MINIMAL["comment"]],
        ["draft", "--", "check", "x.json"],
        ["--json", "draft", "--", "new", PR],
    ],
)
def test_double_dash_before_the_command_is_a_usage_error(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    code, out, err = _run_raw(capsys, argv)
    assert code == 2
    assert "usage" in err.lower()
    if "--json" in argv:
        doc = json.loads(out)
        assert doc["error"]["code"] == "usage"
        assert "--" in doc["error"]["message"]
        validate("pr-comment/cli/error.json", doc)
    else:
        assert out == ""


def test_double_dash_after_the_command_token_is_left_to_the_parser() -> None:
    # Not before the command token: the rule for a leading `--` does not apply.
    assert not cli._dash_dash_before_command(["comment", PR, "--", "x"])  # pyright: ignore[reportPrivateUsage]
    assert not cli._dash_dash_before_command(["draft", "check", "--", "x"])  # pyright: ignore[reportPrivateUsage]


# --- message hardening -----------------------------------------------------------------------


def test_undecodable_and_control_characters_are_replaced_in_the_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Parser errors no longer echo caller text; the sanitizer still guards every other message.
    code = cli._fail_usage(["--json"], "bad \udcff\x1b[31m\u202e", "")  # pyright: ignore[reportPrivateUsage]
    out = capsys.readouterr().out
    assert code == 2
    assert out.isascii()
    message = json.loads(out)["error"]["message"]
    # --json is not display-sanitized: the characters are JSON-escaped, never raw on stdout.
    assert "\x1b" in message
    assert "\x1b" not in out
    assert "\\u001b" in out


def test_control_characters_never_reach_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli._fail_usage([], "bad \x1b[31m", "")  # pyright: ignore[reportPrivateUsage]
    captured = capsys.readouterr()
    out, err = captured.out, captured.err
    assert code == 2
    assert out == ""
    assert "\x1b" not in err
    assert "<U+001B>" in err


def test_a_huge_argument_cannot_inflate_the_output(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run_raw(capsys, ["x" * 5_000_000, "--json"])
    assert code == 2
    assert len(json.loads(out)["error"]["message"]) <= 200
    assert len(out) < 5_000
    assert len(err) < 5_000


def test_a_huge_argument_in_human_mode_is_truncated_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, err = _run_raw(capsys, ["comment", PR, "--body-file", "b", "--" + "y" * 5_000_000])
    assert len(err) < 5_000


def test_non_ascii_text_is_escaped_in_json_and_cannot_break_an_ascii_stream(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    code = cli._fail_usage(["--json"], "bad n\u00f6pe", "")  # pyright: ignore[reportPrivateUsage]
    out = capsys.readouterr().out
    assert code == 2
    assert out.isascii()
    assert "\u00f6" in json.loads(out)["error"]["message"]

    stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
    monkeypatch.setattr(sys, "stderr", stream)
    assert cli._fail_usage([], "bad n\u00f6pe", "") == 2  # pyright: ignore[reportPrivateUsage]
    stream.flush()
    assert b"\\xf6" in stream.buffer.getvalue()  # pyright: ignore[reportAttributeAccessIssue]


def test_sanitize_replaces_control_format_and_surrogate_characters_only() -> None:
    sanitize = errors.sanitize
    assert sanitize("a\tb\n") == "a<U+0009>b<U+000A>"
    assert sanitize("\u200b\ud800") == "<U+200B><U+D800>"
    assert sanitize("plain \u00e9\u4e2d") == "plain \u00e9\u4e2d"


# --- repeated flags --------------------------------------------------------------------------


def test_append_action_appends_in_place_and_stops_at_the_cap() -> None:
    parser = cli._Parser()  # pyright: ignore[reportPrivateUsage]
    action = cli.AppendAction(["--x"], "x", max_count=3, default=[])
    namespace = argparse.Namespace(x=action.default)
    action(parser, namespace, "1", "--x")
    first = namespace.x
    assert first is not action.default
    action(parser, namespace, "2", "--x")
    action(parser, namespace, "3", "--x")
    assert namespace.x is first
    assert first == ["1", "2", "3"]
    assert action.default == []
    with pytest.raises(cli.UsageError, match="at most 3"):
        action(parser, namespace, "4", "--x")


@pytest.mark.parametrize("flag", ["--yes", "--allow-mention"])
def test_huge_repeat_lists_are_refused_quickly(
    capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    argv = [*MINIMAL["comment"], *([flag] * 20000), "--json"]
    started = time.monotonic()
    code, doc, _ = _run_json(capsys, argv)
    assert time.monotonic() - started < 2
    assert code == 2
    assert doc["error"]["code"] == "usage"
    assert "too many arguments" in doc["error"]["message"]


def test_argument_count_limit_is_exactly_1024(capsys: pytest.CaptureFixture[str]) -> None:
    base = MINIMAL["comment"]
    ok = [*base, *(["--yes"] * (1024 - len(base) - 1)), "--json"]
    assert len(ok) == 1024
    _, doc_ok, _ = _run_json(capsys, ok)
    assert "too many arguments" not in json.dumps(doc_ok)
    code, doc, _ = _run_json(capsys, [*ok, "--yes"])
    assert code == 2
    assert "too many arguments" in doc["error"]["message"]


def test_repeat_cap_is_enforced_while_parsing(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["resolve", PR, *[x for i in range(500) for x in ("--thread", f"gh:rt:t{i}")]]
    started = time.monotonic()
    code, doc, _ = _run_json(capsys, [*argv, "--json"])
    assert time.monotonic() - started < 10
    assert code == 2
    assert "100" in doc["error"]["message"]


def test_duplicate_threads_count_toward_the_cap_before_duplicates_are_dropped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ok = ["resolve", PR, *["--thread", "gh:rt:1"] * 100]
    assert len(build_parser().parse_args(ok).thread) == 100
    code, doc, _ = _run_json(capsys, ["resolve", PR, *["--thread", "gh:rt:1"] * 101, "--json"])
    assert code == 2
    assert doc["error"]["code"] == "usage"


def test_the_parser_defaults_are_not_shared_between_parses() -> None:
    first = build_parser().parse_args([*MINIMAL["comment"], "--allow-mention", "a"])
    second = build_parser().parse_args(MINIMAL["comment"])
    assert first.allow_mention == ["a"]
    assert second.allow_mention == []


# --- repeated scalar global flags ------------------------------------------------------------

SCALARS = [
    ("--actor", "human:a", "human:b"),
    ("--config", "a.yaml", "b.yaml"),
    ("--expect-plan", "a" * 64, "b" * 64),
]


@pytest.mark.parametrize(("flag", "one", "two"), SCALARS)
@pytest.mark.parametrize("placement", ["both-before", "both-after", "split", "draft-split"])
def test_a_scalar_global_flag_given_twice_with_different_values_is_a_usage_error(
    capsys: pytest.CaptureFixture[str], flag: str, one: str, two: str, placement: str
) -> None:
    command = "draft check" if placement == "draft-split" else "comment"
    base = MINIMAL[command]
    if placement == "both-before":
        argv = [flag, one, flag, two, *base]
    elif placement == "both-after":
        argv = [*base, flag, one, flag, two]
    elif placement == "split":
        argv = [flag, one, *base, flag, two]
    else:
        argv = ["draft", flag, one, *base[1:], flag, two]
    code, doc, _ = _run_json(capsys, [*argv, "--json"])
    assert code == 2
    assert doc["error"]["code"] == "usage"
    assert f"{flag} given more than once with different values" in doc["error"]["message"]


@pytest.mark.parametrize(("flag", "one", "_two"), SCALARS)
def test_a_scalar_global_flag_given_twice_with_the_same_value_is_accepted(
    flag: str, one: str, _two: str
) -> None:
    args = build_parser().parse_args([flag, one, *MINIMAL["comment"], flag, one, f"{flag}={one}"])
    assert getattr(args, flag.lstrip("-").replace("-", "_")) == one


# --- dispatch --------------------------------------------------------------------------------


@pytest.mark.parametrize("command", NON_POSTING)
def test_expect_plan_refused_on_non_posting_commands(
    capsys: pytest.CaptureFixture[str], command: str
) -> None:
    argv = [*MINIMAL[command], "--expect-plan", SHA, "--json"]
    code, doc, _ = _run_json(capsys, argv)
    assert code == 2
    assert doc["error"]["code"] == "usage"
    assert "--expect-plan" in doc["error"]["message"]
    validate("pr-comment/cli/error.json", doc)


@pytest.mark.parametrize("command", sorted(POSTING))
def test_expect_plan_allowed_on_posting_commands(
    capsys: pytest.CaptureFixture[str], command: str
) -> None:
    argv = [*MINIMAL[command], "--expect-plan", SHA, "--json"]
    code, doc, _ = _run_json(capsys, argv)
    # Dispatch reached the (unimplemented) handler: internal, not usage.
    assert code == 1
    assert doc["error"]["code"] == "internal"


@pytest.mark.parametrize("command", sorted(set(MINIMAL) - {"draft check", "export"}))
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
    assert main(MINIMAL["draft check"]) == 1
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
    assert set(cli.HANDLERS) == set(doc_commands())


def test_repeatable_flags_collect() -> None:
    args = build_parser().parse_args(
        [*MINIMAL["comment"], "--allow-mention", "a", "--allow-mention", "b"]
    )
    assert args.allow_mention == ["a", "b"]
    args = build_parser().parse_args(["resolve", PR, "--thread", "gh:rt:x", "--thread", "gh:rt:y"])
    assert args.thread == ["gh:rt:x", "gh:rt:y"]


def test_exactly_100_threads_parse() -> None:
    argv = ["resolve", PR, *[x for i in range(100) for x in ("--thread", f"gh:rt:t{i}")]]
    assert len(build_parser().parse_args(argv).thread) == 100


def test_exactly_32_decision_ids_parse() -> None:
    argv = [*MINIMAL["comment"], *["--decision-id=x"] * 32]
    assert len(build_parser().parse_args(argv).decision_id) == 32


def test_decision_ids_split_before_and_after_the_command_count_together(
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = [*["--decision-id=x"] * 20, *MINIMAL["comment"], *["--decision-id=y"] * 20, "--json"]
    code, doc, _ = _run_json(capsys, argv)
    assert code == 2
    assert doc["error"]["code"] == "usage"


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


@pytest.mark.parametrize(
    ("label", "result", "exception"),
    [
        ("none", None, "TypeError"),
        ("not serializable", cli.Outcome(0, {"x": object()}), "TypeError"),
        ("circular", cli.Outcome(0, {}), "ValueError"),
        ("not a dict", cli.Outcome(0, ["a"]), "TypeError"),  # pyright: ignore[reportArgumentType]
        ("bad exit code", cli.Outcome("0"), "TypeError"),  # pyright: ignore[reportArgumentType]
    ],
)
def test_a_handler_returning_a_bad_outcome_fails_closed(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    result: Any,
    exception: str,
) -> None:
    if label == "circular":
        loop: dict[str, Any] = {}
        loop["self"] = loop
        result = cli.Outcome(0, loop)

    def handler(args: argparse.Namespace) -> Any:
        return result

    monkeypatch.setitem(cli.HANDLERS, "export", handler)
    # Without --json the document is never serialized, so only a bad Outcome itself fails there.
    modes = (["--json"], []) if label not in ("not serializable", "circular") else (["--json"],)
    for flags in [*modes, ["--json", "--quiet"]]:
        code, out, err = _run_raw(capsys, ["export", *flags])
        assert code == 1
        if "--quiet" in flags:
            assert "Traceback" not in err
        if flags:
            doc = json.loads(out)
            assert doc["error"]["code"] == "internal"
            assert doc["error"]["message"] == f"unexpected {exception}"
            validate("pr-comment/cli/error.json", doc)
        else:
            assert out == ""
            assert f"unexpected {exception}" in err
