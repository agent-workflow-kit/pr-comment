"""Forwarded flag values are validated at the CLI, before any handler or forgectl call runs."""

from __future__ import annotations

import argparse
import json
from typing import Any

import pytest

from pr_comment import cli
from pr_comment.cli import build_parser, main
from tests.schema_harness import validate

PR = "https://example.test/o/r/pull/1"
SHA = "0123456789abcdef0123456789abcdef01234567"
THREAD = "gh:rt:PRRT_abc"


def _run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, dict[str, Any], str]:
    code = main([*argv, "--json"])
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def _parse(argv: list[str]) -> argparse.Namespace:
    args = build_parser().parse_args(argv)
    cli.validate_args(args)
    return args


def _no_handler(monkeypatch: pytest.MonkeyPatch) -> list[argparse.Namespace]:
    """Replace every handler with one that records the call, so a reached handler is visible."""
    calls: list[argparse.Namespace] = []

    def handler(args: argparse.Namespace) -> cli.Outcome:
        calls.append(args)
        return cli.Outcome(0)

    for key in list(cli.HANDLERS):
        monkeypatch.setitem(cli.HANDLERS, key, handler)
    return calls


# --- (command, flag, valid value, invalid values) --------------------------------------------

FLAGS: list[tuple[list[str], str, str, list[str]]] = [
    (["review", PR, "--draft-file", "d"], "--expected-head", SHA, ["abc", SHA + "0", "-x", ""]),
    (["review", PR, "--draft-file", "d"], "--reanchor-to", SHA, ["abc", SHA[:39], "-x", ""]),
    (["review", PR, "--draft-file", "d"], "--dedupe-key", "prc:rv:K", ["a b", "k" * 129, ""]),
    (["comment", PR, "--body-file", "b"], "--expected-head", SHA, ["abc", "g" * 40]),
    (["comment", PR, "--body-file", "b"], "--dedupe-key", "k", ["a\nb", "a/b"]),
    (["reply", PR, "--body-file", "b"], "--to", "gh:rc:abc", ["abc", "gh:rc:", "-x", "gl:n:x"]),
    (["approve", PR], "--expected-head", SHA, ["abc"]),
    (["approve", PR], "--expected-base", "main", ["a..b", "a@{b", "-x", "refs/heads/m", ""]),
    (["approve", PR], "--dedupe-key", "k", ["a b"]),
    (
        ["review", PR, "--draft-file", "d"],
        "--allow-mention",
        "org/team",
        ["", "@john", "a/", "/a", "a//b", "a b", "é", "a​b", "＠x", "a\nb"],
    ),
    (
        ["comment", PR, "--body-file", "b"],
        "--allow-mention",
        "John.Doe",
        ["@johnmarker", "zzmarker@", "a,zzmarker"],
    ),
    (
        ["reply", PR, "--body-file", "b"],
        "--allow-mention",
        "a-b_c",
        ["a/", "zzmarker&#64;", "a\nzzmarker"],
    ),
    (["approve", PR], "--allow-mention", "g/sub/proj", ["@all", "a/b/"]),
    (["resolve", PR], "--thread", THREAD, ["gh:ic:abc", "gl:d:" + "a" * 39, "x", "-x"]),
    (["unresolve", PR], "--thread", "gl:d:" + SHA, ["gh:rt:" + "a" * 101, "gl:n:1"]),
]


def _complete(base: list[str], flag: str) -> list[str]:
    """The required flags of the command that the flag under test does not supply itself."""
    if base[0] == "reply" and flag != "--to":
        return ["--to", "gh:ic:a"]
    if base[0] in ("resolve", "unresolve") and flag != "--thread":
        return ["--thread", THREAD]
    return []


@pytest.mark.parametrize(("base", "flag", "good", "bad"), FLAGS, ids=lambda v: str(v)[:24])
def test_valid_values_parse_in_both_forms(
    monkeypatch: pytest.MonkeyPatch, base: list[str], flag: str, good: str, bad: list[str]
) -> None:
    # --allow-mention needs an interactive run (docs/interface.md, Actors and interactive runs).
    monkeypatch.setattr(cli, "stream_is_tty", lambda _stream: True)
    extra = _complete(base, flag)
    _parse([*base, *extra, flag, good])
    _parse([*base, *extra, f"{flag}={good}"])


@pytest.mark.parametrize(("base", "flag", "good", "bad"), FLAGS, ids=lambda v: str(v)[:24])
def test_invalid_values_exit_two_naming_the_flag(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    base: list[str],
    flag: str,
    good: str,
    bad: list[str],
) -> None:
    calls = _no_handler(monkeypatch)
    extra = _complete(base, flag)
    for value in bad:
        code, doc, err = _run(capsys, [*base, *extra, f"{flag}={value}"])
        assert code == 2, value
        assert doc["error"]["code"] == "usage", value
        assert flag in doc["error"]["message"], value
        validate("pr-comment/cli/error.json", doc)
        if flag == "--allow-mention" and value:
            # the value is not echoed anywhere (docs/interface.md: Mentions)
            assert value not in doc["error"]["message"], value
            assert value not in json.dumps(doc), value
            assert value not in err, value
    assert calls == []


def test_uppercase_sha_is_lowered_for_head_and_reanchor() -> None:
    args = _parse(["review", PR, "--draft-file", "d", f"--expected-head={SHA.upper()}"])
    assert args.expected_head == SHA
    args = _parse(["review", PR, "--draft-file", "d", f"--reanchor-to={SHA.upper()}"])
    assert args.reanchor_to == SHA


def test_absent_values_stay_none() -> None:
    args = _parse(["approve", PR])
    assert args.expected_head is None
    assert args.expected_base is None
    assert args.dedupe_key is None


def test_a_valid_command_reaches_its_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _no_handler(monkeypatch)
    assert main(["approve", PR, f"--expected-head={SHA}", "--expected-base=main"]) == 0
    assert len(calls) == 1


# --- the PR argument --------------------------------------------------------------------------

PR_COMMANDS = {
    "review": ["--draft-file", "d"],
    "comment": ["--body-file", "b"],
    "reply": ["--to", "gh:ic:a", "--body-file", "b"],
    "approve": [],
    "resolve": ["--thread", THREAD],
    "unresolve": ["--thread", THREAD],
}

BAD_PRS = [
    "example.test/o/r",
    "example.test/o/r#x",
    "example.test/o/r#1\n",
    "http://example.test/o/r/pull/1",
    "https://",
    "#1",
    "example.test/o/r#1 ",
]


@pytest.mark.parametrize("command", sorted(PR_COMMANDS))
@pytest.mark.parametrize("pr", BAD_PRS, ids=repr)
def test_malformed_pr_is_invalid_pr(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    pr: str,
) -> None:
    calls = _no_handler(monkeypatch)
    code, doc, _ = _run(capsys, [command, pr, *PR_COMMANDS[command]])
    assert code == 2
    assert doc["error"]["code"] == "invalid_pr"
    validate("pr-comment/cli/error.json", doc)
    assert calls == []


LEADING_DASH_PRS = ["-x", "--nope", "-", "-https://example.test/o/r/pull/1", "-a#1", "--x=1"]


@pytest.mark.parametrize("command", sorted(PR_COMMANDS))
@pytest.mark.parametrize("pr", LEADING_DASH_PRS)
def test_leading_dash_pr_before_the_flags_is_invalid_pr(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    pr: str,
) -> None:
    calls = _no_handler(monkeypatch)
    code, doc, _ = _run(capsys, [command, pr, *PR_COMMANDS[command]])
    assert code == 2
    assert doc["error"]["code"] == "invalid_pr", pr
    assert calls == []


@pytest.mark.parametrize("command", sorted(PR_COMMANDS))
def test_leading_dash_pr_after_the_flags_is_invalid_pr(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    _no_handler(monkeypatch)
    code, doc, _ = _run(capsys, [command, *PR_COMMANDS[command], "-x"])
    assert code == 2
    assert doc["error"]["code"] == "invalid_pr"


@pytest.mark.parametrize("command", sorted(PR_COMMANDS))
def test_leading_dash_pr_after_double_dash_is_invalid_pr(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    _no_handler(monkeypatch)
    code = main(["--json", command, *PR_COMMANDS[command], "--", "-x"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 2
    assert doc["error"]["code"] == "invalid_pr"


def test_the_motivating_case_comment_dash_x_body_file() -> None:
    assert main(["comment", "-x", "--body-file", "b"]) == 2


def test_global_flags_before_the_command_do_not_confuse_the_scan(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, doc, _ = _run(capsys, ["--actor", "human:me", "--config", "-c", "comment", "-x"])
    assert doc["error"]["code"] == "invalid_pr"
    assert code == 2


def test_draft_new_pr_is_checked_too(capsys: pytest.CaptureFixture[str]) -> None:
    code, doc, _ = _run(capsys, ["draft", "new", "-x"])
    assert (code, doc["error"]["code"]) == (2, "invalid_pr")
    code, doc, _ = _run(capsys, ["draft", "new", "example.test/o/r"])
    assert (code, doc["error"]["code"]) == (2, "invalid_pr")


def test_commands_without_a_pr_are_not_checked(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _no_handler(monkeypatch)
    assert main(["draft", "check", "-"]) == 0
    assert main(["export"]) == 0
    assert len(calls) == 2


def test_a_value_flag_named_like_a_pr_does_not_count_as_the_pr(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _no_handler(monkeypatch)
    # `-x` is the value of --body-file= here, so the PR is the later token.
    assert main(["comment", "--body-file=-x", PR]) == 0
    assert calls[0].pr == PR
    assert calls[0].body_file == "-x"


def test_a_missing_pr_is_still_usage(capsys: pytest.CaptureFixture[str]) -> None:
    code, doc, _ = _run(capsys, ["comment", "--body-file", "b"])
    assert (code, doc["error"]["code"]) == (2, "usage")


def test_an_unknown_flag_after_a_valid_pr_is_usage(capsys: pytest.CaptureFixture[str]) -> None:
    code, doc, _ = _run(capsys, ["comment", PR, "--body-file", "b", "-x"])
    assert (code, doc["error"]["code"]) == (2, "usage")


def test_valid_prs_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _no_handler(monkeypatch)
    for pr in (PR, "example.test/o/r#12", "host/g/sub/p#1"):
        assert main(["comment", pr, "--body-file", "b"]) == 0
    assert len(calls) == 3


def test_invalid_pr_message_is_sanitized_and_does_not_echo_the_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, doc, _ = _run(capsys, ["comment", "x\x1b[31m‮#", "--body-file", "b"])
    assert code == 2
    assert "\x1b" not in doc["error"]["message"]
    assert "‮" not in doc["error"]["message"]


def test_the_scan_value_flags_match_the_parser() -> None:
    parser = build_parser()
    seen: set[str] = set()
    pending = [parser]
    while pending:
        current = pending.pop()
        for action in current._actions:  # pyright: ignore[reportPrivateUsage]
            if isinstance(action, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage, reportUnknownMemberType]
                pending.extend(action.choices.values())  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            elif action.nargs != 0 and action.option_strings:
                seen.update(action.option_strings)
    assert seen == set(cli.VALUE_FLAGS), seen ^ set(cli.VALUE_FLAGS)


# --- the grammars moved from parse time: behavior is unchanged --------------------------------


def test_actor_decision_id_hook_scope_and_plan_still_validate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = ["comment", PR, "--body-file", "b"]
    for extra in (
        ["--actor=robot:x"],
        ["--decision-id=a b"],
        ["--hook=bogus"],
        ["--override-escalation=all"],
        ["--expect-plan=ABC"],
    ):
        code, doc, _ = _run(capsys, [*base, *extra])
        assert (code, doc["error"]["code"]) == (2, "usage"), extra


def test_thread_cap_and_decision_id_cap_are_enforced_on_the_merged_total(
    capsys: pytest.CaptureFixture[str],
) -> None:
    threads = [x for i in range(101) for x in ("--thread", f"gh:rt:T{i}")]
    code, doc, _ = _run(capsys, ["resolve", PR, *threads])
    assert (code, doc["error"]["code"]) == (2, "usage")
    assert "100" in doc["error"]["message"]


def _run_json_first(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> tuple[int, dict[str, Any], str]:
    """Like ``_run`` with ``--json`` ahead of the command, so a ``--`` in argv stays last."""
    code = main(["--json", *argv])
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


# --- an unknown flag before a valid PR is usage, not invalid_pr -------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["comment", "--bogus", "a#1", "--body-file", "b"],
        ["comment", "--dry", "a#1", "--body-file", "b"],
        ["comment", "--body=b", "a#1"],
        ["resolve", "--thr", "gh:rt:a", "a#1"],
        ["comment", "-x", PR, "--body-file", "b"],
        ["comment", "--bogus", "--body-file", "b", PR],
        ["comment", "--body-file", "b", "--bogus", "--", PR],
    ],
    ids=str,
)
def test_an_unknown_flag_before_a_valid_pr_is_usage(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    code, doc, _ = _run_json_first(capsys, argv)
    assert (code, doc["error"]["code"]) == (2, "usage")


def test_the_unknown_flag_is_reported_as_unrecognized(capsys: pytest.CaptureFixture[str]) -> None:
    _, doc, _ = _run_json_first(capsys, ["comment", "--bogus", "a#1", "--body-file", "b"])
    assert doc["error"]["message"] == "unrecognized arguments"


@pytest.mark.parametrize(
    "argv",
    [
        ["comment", "-x", "--body-file", "b"],
        ["comment", "--body-file", "b", "--", "-x#1"],
        ["comment", "--bogus", "--body-file", "b"],
    ],
    ids=str,
)
def test_a_dash_token_in_the_pr_slot_stays_invalid_pr(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    code, doc, _ = _run_json_first(capsys, argv)
    assert (code, doc["error"]["code"]) == (2, "invalid_pr")


def test_a_known_flag_with_no_pr_is_usage(capsys: pytest.CaptureFixture[str]) -> None:
    code, doc, _ = _run(capsys, ["comment", "--body-file", "b", "--json"])
    assert (code, doc["error"]["code"]) == (2, "usage")


# --- help and version before a bad PR reach argparse ------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["comment", "-x#1", "-h"],
        ["comment", "-x#1", "--help"],
        ["review", "-h", "-x"],
        ["comment", "-x", "--help"],
    ],
    ids=str,
)
def test_help_reaches_argparse_before_the_pr_check(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    with pytest.raises(SystemExit) as info:
        main(argv)
    assert info.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_version_reaches_argparse_before_the_pr_check(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["comment", "-x#1", "--version"])
    assert info.value.code == 0
    assert "pr-comment" in capsys.readouterr().out


def test_help_after_double_dash_does_not_count(capsys: pytest.CaptureFixture[str]) -> None:
    code, doc, _ = _run_json_first(capsys, ["comment", "--body-file", "b", "--", "-h"])
    assert (code, doc["error"]["code"]) == (2, "invalid_pr")


# --- a repeated single-valued flag with a different value is usage ----------------------------

SHA_UPPER = SHA.upper()
OTHER_SHA = "f" * 40

REPEATED: list[tuple[list[str], str, str, str]] = [
    (["review", PR, "--draft-file", "d"], "--expected-head", SHA, OTHER_SHA),
    (["review", PR, "--draft-file", "d"], "--reanchor-to", SHA, OTHER_SHA),
    (["review", PR, "--draft-file", "d"], "--dedupe-key", "k1", "k2"),
    (["review", PR], "--draft-file", "d1", "d2"),
    (["comment", PR], "--body-file", "b1", "b2"),
    (["reply", PR, "--body-file", "b"], "--to", "gh:ic:a", "gh:ic:b"),
    (["approve", PR], "--expected-base", "main", "dev"),
    (["draft", "new", PR], "--from", "a", "b"),
    (["draft", "new", PR], "--event", "comment", "request_changes"),
    (["export"], "--since", "7d", "8d"),
]


@pytest.mark.parametrize(("base", "flag", "one", "two"), REPEATED, ids=lambda v: str(v)[:24])
def test_a_repeated_flag_with_different_values_is_usage(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    base: list[str],
    flag: str,
    one: str,
    two: str,
) -> None:
    calls = _no_handler(monkeypatch)
    extra = _complete(base, flag)
    code, doc, _ = _run(capsys, [*base, *extra, f"{flag}={one}", flag, two])
    assert (code, doc["error"]["code"]) == (2, "usage")
    assert flag in doc["error"]["message"]
    assert calls == []


@pytest.mark.parametrize(("base", "flag", "one", "two"), REPEATED, ids=lambda v: str(v)[:24])
def test_a_repeated_flag_with_the_same_value_is_accepted(
    base: list[str], flag: str, one: str, two: str
) -> None:
    _parse([*base, *_complete(base, flag), f"{flag}={one}", flag, one])


def test_differently_cased_shas_are_the_same_value() -> None:
    args = _parse(
        [
            "comment",
            PR,
            "--body-file",
            "b",
            f"--expected-head={SHA_UPPER}",
            f"--expected-head={SHA}",
        ]
    )
    assert args.expected_head == SHA


def test_thread_and_decision_id_stay_repeatable() -> None:
    args = _parse(["resolve", PR, "--thread", THREAD, "--thread", "gh:rt:other"])
    assert args.thread == [THREAD, "gh:rt:other"]


# --- parser errors never echo a caller value --------------------------------------------------

SECRET = "SECRETVALUE"


@pytest.mark.parametrize(
    "argv",
    [
        ["comment", PR, "--body-file", "b", SECRET],
        ["comment", PR, "--body-file", "b", f"--{SECRET}"],
        ["comment", PR, "--body-file", "b", f"--{SECRET}=x"],
        [SECRET],
        ["draft", SECRET],
        ["draft", "new", PR, "--event", SECRET],
        ["comment", PR, "--body-file", "b", f"--json={SECRET}"],
        ["comment", PR, "--body-file", "b", f"--dry-run={SECRET}"],
        ["review", PR, "--draft-file", "d", f"--approve={SECRET}"],
    ],
    ids=str,
)
def test_parser_errors_do_not_echo_caller_values(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    code = main([*argv, "--json"])
    captured = capsys.readouterr()
    assert code == 2
    assert SECRET not in captured.err
    assert SECRET not in captured.out
    doc = json.loads(captured.out)
    assert doc["error"]["code"] == "usage"
    validate("pr-comment/cli/error.json", doc)


def test_unrecognized_and_invalid_choice_messages_are_fixed_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, doc, _ = _run(capsys, ["comment", PR, "--body-file", "b", SECRET])
    assert doc["error"]["message"] == "unrecognized arguments"
    _, doc, _ = _run(capsys, ["draft", "new", PR, "--event", SECRET])
    assert doc["error"]["message"].startswith("argument --event: invalid choice")
    _, doc, _ = _run(capsys, [SECRET])
    assert doc["error"]["message"].startswith("argument <command>: invalid choice")
