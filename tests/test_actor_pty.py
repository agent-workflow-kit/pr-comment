"""Real-terminal tests of interactive detection: the gates cannot be spoofed by the environment.

Each child runs ``pr-comment`` with real pseudo-terminals (or pipes) on stdin and stderr. The
child PID is recorded at launch and, if it outlives the timeout, signalled by that exact PID.
"""

from __future__ import annotations

import contextlib
import json
import os
import pty
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

TIMEOUT = 20

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pty")

COMMENT = ["comment", "o/r#1", "--body-file", "b.md"]


def run_cli(
    tmp_path: Path,
    argv: list[str],
    *,
    stdin: str,
    stderr: str,
    env_extra: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Run the CLI with --json; ``stdin`` and ``stderr`` are ``pty``, ``pipe`` or ``null``.

    Returns the exit code and the parsed stdout envelope.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (tmp_path / "b.md").write_text("body\n", encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "xdg"),
    }
    env.update(env_extra or {})
    masters: list[int] = []
    slaves: list[int] = []

    def endpoint(kind: str, *, reading: bool) -> int:
        if kind == "pty":
            master, slave = pty.openpty()
            masters.append(master)
            slaves.append(slave)
            return slave
        if kind == "null":
            fd = os.open(os.devnull, os.O_RDONLY if reading else os.O_WRONLY)
            slaves.append(fd)
            return fd
        read_fd, write_fd = os.pipe()
        masters.append(write_fd if reading else read_fd)
        slaves.append(read_fd if reading else write_fd)
        return read_fd if reading else write_fd

    out_r, out_w = os.pipe()
    try:
        stdin_fd = endpoint(stdin, reading=True)
        stderr_fd = endpoint(stderr, reading=False)
        proc = subprocess.Popen(
            [sys.executable, "-m", "pr_comment", *argv, "--json"],
            stdin=stdin_fd,
            stdout=out_w,
            stderr=stderr_fd,
            env=env,
            cwd=tmp_path,
        )
        os.close(out_w)
        out_w = -1
        pid = proc.pid  # recorded at launch; the only PID this test may signal
        try:
            code = proc.wait(timeout=TIMEOUT)
            with os.fdopen(out_r, "r", encoding="utf-8") as fh:
                out_r = -1
                text = fh.read()
            return code, json.loads(text)
        except subprocess.TimeoutExpired:
            if proc.pid == pid and proc.poll() is None:
                os.kill(pid, signal.SIGKILL)
                proc.wait()
            pytest.fail("pr-comment did not exit")
    finally:
        for fd in (*slaves, *masters, out_r, out_w):
            with contextlib.suppress(OSError):
                os.close(fd)


def assert_usage(result: tuple[int, dict[str, Any]], needle: str) -> None:
    code, doc = result
    assert code == 2
    assert doc["error"]["code"] == "usage"
    assert needle in doc["error"]["message"]


def assert_accepted(result: tuple[int, dict[str, Any]], *, later_usage: str | None = None) -> None:
    """The actor and flag rules passed. Later steps may still fail (these runs have no forgectl),
    but never with a usage error, except the one named ``later_usage`` (an input problem that comes
    after the rules), and never with one of the rules' own messages."""
    _code, doc = result
    error = doc.get("error") or {}
    if error and error["message"] != later_usage:
        assert error["code"] != "usage", error["message"]
    message = error.get("message") or ""
    for needle in ("--allow-mention", "--override-escalation", "stdin is a terminal"):
        assert needle not in message


def test_human_on_a_real_terminal_may_use_allow_mention(tmp_path: Path) -> None:
    argv = [*COMMENT, "--allow-mention", "x"]
    assert_accepted(run_cli(tmp_path, argv, stdin="pty", stderr="pty"))


def test_yes_blocks_allow_mention_on_a_terminal(tmp_path: Path) -> None:
    argv = [*COMMENT, "--allow-mention", "x", "--yes"]
    assert_usage(run_cli(tmp_path, argv, stdin="pty", stderr="pty"), "--allow-mention")


def test_auto_actor_flag_blocks_allow_mention_on_a_terminal(tmp_path: Path) -> None:
    argv = [*COMMENT, "--allow-mention", "x", "--actor", "auto:bot"]
    assert_usage(run_cli(tmp_path, argv, stdin="pty", stderr="pty"), "--allow-mention")


def test_non_tty_stderr_is_not_interactive(tmp_path: Path) -> None:
    argv = [*COMMENT, "--allow-mention", "x"]
    assert_usage(run_cli(tmp_path, argv, stdin="pty", stderr="pipe"), "--allow-mention")


def test_non_tty_stdin_is_not_interactive_even_with_a_human_claim(tmp_path: Path) -> None:
    argv = [*COMMENT, "--allow-mention", "x", "--actor", "human:me"]
    assert_usage(run_cli(tmp_path, argv, stdin="pipe", stderr="pty"), "--allow-mention")
    assert_usage(run_cli(tmp_path, argv, stdin="null", stderr="pty"), "--allow-mention")


@pytest.mark.parametrize(
    "env",
    [
        {"TERM": "xterm-256color"},
        {"TERM": "dumb"},
        {"TERM": ""},
        {"USER": "root", "LOGNAME": "root"},
        {"PR_COMMENT_INTERACTIVE": "1", "PR_COMMENT_ACTOR": "human:root", "CI": ""},
        {"SSH_TTY": "/dev/pts/0", "PS1": "$ ", "TERM": "xterm"},
    ],
)
def test_environment_cannot_fake_an_interactive_run(tmp_path: Path, env: dict[str, str]) -> None:
    argv = [*COMMENT, "--allow-mention", "x"]
    piped = run_cli(tmp_path, argv, stdin="pipe", stderr="pipe", env_extra=env)
    nulled = run_cli(tmp_path, argv, stdin="null", stderr="pty", env_extra=env)
    assert_usage(piped, "--allow-mention")
    assert_usage(nulled, "--allow-mention")


def test_default_actor_without_a_terminal_is_auto_so_override_is_refused(tmp_path: Path) -> None:
    argv = [*COMMENT, "--override-escalation=forgectl", "--yes"]
    assert_usage(run_cli(tmp_path, argv, stdin="pipe", stderr="pipe"), "--override-escalation")


def test_override_escalation_with_yes_and_human_claim_is_accepted_without_terminal(
    tmp_path: Path,
) -> None:
    argv = [*COMMENT, "--override-escalation=forgectl", "--yes", "--actor", "human:me"]
    assert_accepted(run_cli(tmp_path, argv, stdin="pipe", stderr="pipe"))


@pytest.mark.parametrize(
    "argv",
    [
        ["comment", "o/r#1", "--body-file", "-"],
        ["reply", "o/r#1", "--to", "gh:ic:abc", "--body-file", "-"],
        ["approve", "o/r#1", "--body-file", "-"],
        ["review", "o/r#1", "--draft-file", "-"],
        ["draft", "new", "o/r#1", "--from", "-"],
    ],
)
def test_dash_input_with_a_terminal_stdin_is_usage(tmp_path: Path, argv: list[str]) -> None:
    assert_usage(run_cli(tmp_path, argv, stdin="pty", stderr="pty"), "stdin is a terminal")
    assert_usage(run_cli(tmp_path, argv, stdin="pty", stderr="pipe"), "stdin is a terminal")


def test_draft_check_dash_with_a_terminal_stdin_is_accepted(tmp_path: Path) -> None:
    argv = ["draft", "check", "-"]
    assert_accepted(run_cli(tmp_path, argv, stdin="pty", stderr="pty"))


def test_dash_input_from_a_pipe_is_accepted(tmp_path: Path) -> None:
    argv = ["comment", "o/r#1", "--body-file", "-"]
    # /dev/null is a non-terminal that reads as EOF (an open pipe would block the body read).
    assert_accepted(
        run_cli(tmp_path, argv, stdin="null", stderr="pipe"), later_usage="the body is empty"
    )
