"""Actor resolution, interactive detection and the human-only flag rules (issue #5)."""

from __future__ import annotations

import argparse
import io
import json
import sys
from typing import Any

import pytest

from pr_comment import actor as actor_mod
from pr_comment.actor import ActorError, RunContext, resolve_run
from pr_comment.cli import build_parser, main


def ns(**overrides: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "command": "comment",
        "actor": None,
        "yes": False,
        "allow_mention": [],
        "override_escalation": [],
        "body_file": "body.md",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def run(
    args: argparse.Namespace, *, stdin_tty: bool, stderr_tty: bool, user: str | None = "me"
) -> RunContext:
    return resolve_run(args, stdin_tty=stdin_tty, stderr_tty=stderr_tty, os_user=lambda: user)


# (stdin tty, stderr tty, --actor, --yes, input is "-") -> (actor, actor_source, interactive)
TABLE = [
    (True, True, None, False, False, "human:me", "tty", True),
    (True, True, None, True, False, "human:me", "tty", False),
    (True, True, None, False, True, None, None, None),  # "-" with a TTY stdin is usage
    (True, False, None, False, False, "human:me", "tty", False),
    (False, True, None, False, False, "auto:unknown", "default", False),
    (False, False, None, False, False, "auto:unknown", "default", False),
    (False, True, None, False, True, "auto:unknown", "default", False),
    (False, True, "human:x", False, False, "human:x", "flag", False),  # stdin not a TTY
    (False, True, "human:x", True, True, "human:x", "flag", False),
    (True, True, "human:x", False, False, "human:x", "flag", True),
    (True, True, "human:x", False, True, None, None, None),
    (True, True, "auto:bot", False, False, "auto:bot", "flag", False),
    (True, True, "auto:bot", True, False, "auto:bot", "flag", False),
    (True, False, "human:x", False, False, "human:x", "flag", False),
    (False, False, "auto:bot", False, True, "auto:bot", "flag", False),
]


@pytest.mark.parametrize(
    ("stdin_tty", "stderr_tty", "flag", "yes", "dash", "actor", "source", "interactive"), TABLE
)
def test_actor_table(
    stdin_tty: bool,
    stderr_tty: bool,
    flag: str | None,
    yes: bool,
    dash: bool,
    actor: str | None,
    source: str | None,
    interactive: bool | None,
) -> None:
    args = ns(actor=flag, yes=yes, body_file="-" if dash else "b.md")
    if actor is None:
        with pytest.raises(ActorError, match="stdin is a terminal"):
            run(args, stdin_tty=stdin_tty, stderr_tty=stderr_tty)
        return
    ctx = run(args, stdin_tty=stdin_tty, stderr_tty=stderr_tty)
    assert (ctx.actor, ctx.actor_source, ctx.interactive, ctx.stdin_tty) == (
        actor,
        source,
        interactive,
        stdin_tty,
    )


@pytest.mark.parametrize(
    ("command", "attr"),
    [
        ("review", "draft_file"),
        ("comment", "body_file"),
        ("reply", "body_file"),
        ("approve", "body_file"),
        ("draft new", "from_draft"),
    ],
)
def test_dash_input_with_tty_stdin_is_refused_for_every_input_flag(command: str, attr: str) -> None:
    with pytest.raises(ActorError):
        run(ns(command=command, **{attr: "-"}), stdin_tty=True, stderr_tty=True)


def test_draft_check_dash_with_tty_stdin_is_accepted_and_not_interactive() -> None:
    ctx = run(ns(command="draft check", path="-"), stdin_tty=True, stderr_tty=True)
    assert ctx.interactive is False
    assert ctx.actor == "human:me"


def test_allow_mention_with_dash_input_and_piped_stdin_is_refused() -> None:
    args = ns(allow_mention=["bob"], actor="human:x", body_file="-")
    with pytest.raises(ActorError, match="--allow-mention"):
        run(args, stdin_tty=False, stderr_tty=True)


def test_allow_mention_refusal_message_names_the_terminal_requirement() -> None:
    with pytest.raises(ActorError, match="stdin and stderr must be terminals"):
        run(ns(allow_mention=["bob"], yes=True), stdin_tty=True, stderr_tty=True)


def test_dash_input_makes_run_non_interactive_even_with_human_actor_flag() -> None:
    ctx = run(ns(actor="human:x", body_file="-"), stdin_tty=False, stderr_tty=True)
    assert ctx.interactive is False


def test_only_exact_dash_counts_as_stdin() -> None:
    ctx = run(ns(body_file="-x"), stdin_tty=True, stderr_tty=True)
    assert ctx.interactive is True
    ctx = run(ns(body_file="./-"), stdin_tty=True, stderr_tty=True)
    assert ctx.interactive is True


def test_commands_without_input_flags_are_interactive_capable() -> None:
    ctx = run(ns(command="resolve"), stdin_tty=True, stderr_tty=True)
    assert ctx.interactive is True


@pytest.mark.parametrize(
    "user", ["", "a b", "x" * 65, "ünï", "a/b", "a:b", "bad\n", "bad\nname", "dom\\user", None]
)
def test_os_username_outside_grammar_gives_human_unknown(user: str | None) -> None:
    ctx = run(ns(), stdin_tty=True, stderr_tty=True, user=user)
    assert ctx.actor == "human:unknown"
    assert ctx.actor_source == "tty"


@pytest.mark.parametrize("user", ["me", "a.b-c_d@e", "x" * 64, "A1"])
def test_os_username_inside_grammar_is_used(user: str) -> None:
    assert run(ns(), stdin_tty=True, stderr_tty=True, user=user).actor == f"human:{user}"


def test_os_username_lookup_error_gives_human_unknown() -> None:
    def boom() -> str:
        raise KeyError("no passwd entry")

    ctx = resolve_run(ns(), stdin_tty=True, stderr_tty=True, os_user=boom)
    assert ctx.actor == "human:unknown"


def test_default_username_ignores_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USER", "spoofed")
    monkeypatch.setenv("LOGNAME", "spoofed")
    assert actor_mod.os_username() != "spoofed"


def test_auto_default_does_not_look_up_the_user() -> None:
    def boom() -> str:
        raise AssertionError("must not be called")

    ctx = resolve_run(ns(), stdin_tty=False, stderr_tty=True, os_user=boom)
    assert ctx.actor == "auto:unknown"


# --- human-only flags ------------------------------------------------------------------------


def test_allow_mention_with_yes_is_refused() -> None:
    with pytest.raises(ActorError, match="--allow-mention"):
        run(ns(allow_mention=["bob"], yes=True), stdin_tty=True, stderr_tty=True)


def test_allow_mention_with_auto_actor_is_refused() -> None:
    with pytest.raises(ActorError, match="--allow-mention"):
        run(ns(allow_mention=["bob"], actor="auto:bot"), stdin_tty=True, stderr_tty=True)


def test_allow_mention_with_human_actor_but_not_interactive_is_refused() -> None:
    with pytest.raises(ActorError, match="--allow-mention"):
        run(ns(allow_mention=["bob"], actor="human:x"), stdin_tty=False, stderr_tty=True)
    with pytest.raises(ActorError, match="--allow-mention"):
        run(ns(allow_mention=["bob"]), stdin_tty=True, stderr_tty=False)


def test_allow_mention_interactive_human_is_accepted() -> None:
    ctx = run(ns(allow_mention=["bob"]), stdin_tty=True, stderr_tty=True)
    assert ctx.interactive is True


def test_override_escalation_with_yes_and_human_actor_is_accepted() -> None:
    ctx = run(
        ns(override_escalation=["forgectl"], yes=True, actor="human:x"),
        stdin_tty=False,
        stderr_tty=False,
    )
    assert (ctx.actor, ctx.interactive) == ("human:x", False)


def test_override_escalation_with_auto_actor_is_refused() -> None:
    with pytest.raises(ActorError, match="--override-escalation"):
        run(ns(override_escalation=["pr-comment"], actor="auto:bot", yes=True),
            stdin_tty=True, stderr_tty=True)  # fmt: skip


def test_override_escalation_with_default_auto_actor_is_refused() -> None:
    with pytest.raises(ActorError, match="--override-escalation"):
        run(ns(override_escalation=["forgectl"], yes=True), stdin_tty=False, stderr_tty=False)


def test_override_escalation_with_default_human_actor_on_tty_is_accepted() -> None:
    ctx = run(ns(override_escalation=["forgectl"]), stdin_tty=True, stderr_tty=True)
    assert ctx.actor == "human:me"


def test_run_context_audit_fields() -> None:
    ctx = run(ns(), stdin_tty=True, stderr_tty=True)
    assert ctx.audit_fields() == {"actor": "human:me", "actor_source": "tty", "stdin_tty": True}


# --- through main() --------------------------------------------------------------------------


class _Stream(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _main(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    *,
    stdin_tty: bool,
    stderr_tty: bool,
) -> tuple[int, dict[str, Any] | None]:
    monkeypatch.setattr(sys, "stdin", _Stream(stdin_tty))
    err = _Stream(stderr_tty)
    monkeypatch.setattr(sys, "stderr", err)
    code = main([*argv, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out) if out else None


def test_main_dash_body_with_tty_stdin_exits_usage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc = _main(
        monkeypatch,
        capsys,
        ["comment", "o/r#1", "--body-file", "-"],
        stdin_tty=True,
        stderr_tty=True,
    )
    assert code == 2
    assert doc is not None and doc["error"]["code"] == "usage"


def test_main_allow_mention_with_yes_exits_usage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["comment", "o/r#1", "--body-file", "b", "--allow-mention", "bob", "--yes"]
    code, doc = _main(monkeypatch, capsys, argv, stdin_tty=True, stderr_tty=True)
    assert code == 2
    assert doc is not None and doc["error"]["code"] == "usage"


@pytest.mark.parametrize(
    "argv",
    [
        ["review", "o/r#1", "--draft-file", "d.json", "--allow-mention", "bob"],
        ["comment", "o/r#1", "--body-file", "b", "--allow-mention", "bob"],
        ["reply", "o/r#1", "--to", "gh:ic:abc", "--body-file", "b", "--allow-mention", "bob"],
        ["approve", "o/r#1", "--body-file", "b", "--allow-mention", "bob"],
    ],
)
def test_main_allow_mention_without_tty_exits_usage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    code, doc = _main(monkeypatch, capsys, argv, stdin_tty=False, stderr_tty=False)
    assert code == 2
    assert doc is not None and doc["error"]["code"] == "usage"
    assert "--allow-mention" in doc["error"]["message"]


def test_main_override_escalation_yes_human_actor_is_accepted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["comment", "o/r#1", "--body-file", "b", "--override-escalation=forgectl", "--yes"]
    argv += ["--actor", "human:x"]
    code, doc = _main(monkeypatch, capsys, argv, stdin_tty=False, stderr_tty=False)
    # passes the actor and flag rules; the missing body file is a later, different usage error
    assert code == 2
    assert doc is not None and "--override-escalation" not in doc["error"]["message"]


def test_main_hostile_actor_claims_still_fail_grammar(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for claim in ("human:", "human:a b", "Human:x", "human:x\n", "admin:x", "human:" + "x" * 65):
        argv = ["comment", "o/r#1", "--body-file", "b", "--actor", claim]
        code, _ = _main(monkeypatch, capsys, argv, stdin_tty=True, stderr_tty=True)
        assert code == 2, claim


def test_main_stdin_without_isatty_is_not_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", None)
    code = main(["comment", "o/r#1", "--body-file", "b", "--allow-mention", "x", "--json"])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"


def test_main_closed_stdin_is_not_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    closed = io.StringIO()
    closed.close()
    monkeypatch.setattr(sys, "stdin", closed)
    code = main(["comment", "o/r#1", "--body-file", "b", "--allow-mention", "x", "--json"])
    assert code == 2


def test_parser_still_builds() -> None:
    assert build_parser() is not None
