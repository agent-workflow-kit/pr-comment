"""Real-terminal tests of interactive detection: the gates cannot be spoofed by the environment.

Each child runs ``pr-comment`` with real pseudo-terminals (or pipes) on stdin and stderr. The
child PID is recorded at launch and, if it outlives the timeout, signalled by that exact PID.
"""

from __future__ import annotations

import contextlib
import os
import pty
import signal
import subprocess
import sys
from pathlib import Path

import pytest

TIMEOUT = 20

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pty")

COMMENT = ["comment", "1", "--body-file", "b.md"]


def run_cli(
    tmp_path: Path,
    argv: list[str],
    *,
    stdin: str,
    stderr: str,
    env_extra: dict[str, str] | None = None,
) -> int:
    """Run the CLI; ``stdin`` and ``stderr`` are ``pty``, ``pipe`` or ``null``; returns the exit."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
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

    try:
        stdin_fd = endpoint(stdin, reading=True)
        stderr_fd = endpoint(stderr, reading=False)
        proc = subprocess.Popen(
            [sys.executable, "-m", "pr_comment", *argv],
            stdin=stdin_fd,
            stdout=subprocess.DEVNULL,
            stderr=stderr_fd,
            env=env,
            cwd=tmp_path,
        )
        pid = proc.pid  # recorded at launch; the only PID this test may signal
        try:
            return proc.wait(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            if proc.pid == pid and proc.poll() is None:
                os.kill(pid, signal.SIGKILL)
                proc.wait()
            pytest.fail("pr-comment did not exit")
    finally:
        for fd in (*slaves, *masters):
            with contextlib.suppress(OSError):
                os.close(fd)


def test_human_on_a_real_terminal_may_use_allow_mention(tmp_path: Path) -> None:
    # Accepted by validation; the handler is not written yet, so exit 1 (internal).
    assert run_cli(tmp_path, [*COMMENT, "--allow-mention", "x"], stdin="pty", stderr="pty") == 1


def test_yes_blocks_allow_mention_on_a_terminal(tmp_path: Path) -> None:
    argv = [*COMMENT, "--allow-mention", "x", "--yes"]
    assert run_cli(tmp_path, argv, stdin="pty", stderr="pty") == 2


def test_auto_actor_flag_blocks_allow_mention_on_a_terminal(tmp_path: Path) -> None:
    argv = [*COMMENT, "--allow-mention", "x", "--actor", "auto:bot"]
    assert run_cli(tmp_path, argv, stdin="pty", stderr="pty") == 2


def test_non_tty_stderr_is_not_interactive(tmp_path: Path) -> None:
    assert run_cli(tmp_path, [*COMMENT, "--allow-mention", "x"], stdin="pty", stderr="pipe") == 2


def test_non_tty_stdin_is_not_interactive_even_with_a_human_claim(tmp_path: Path) -> None:
    argv = [*COMMENT, "--allow-mention", "x", "--actor", "human:me"]
    assert run_cli(tmp_path, argv, stdin="pipe", stderr="pty") == 2
    assert run_cli(tmp_path, argv, stdin="null", stderr="pty") == 2


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
    assert run_cli(tmp_path, argv, stdin="pipe", stderr="pipe", env_extra=env) == 2
    assert run_cli(tmp_path, argv, stdin="null", stderr="pty", env_extra=env) == 2


def test_default_actor_without_a_terminal_is_auto_so_override_is_refused(tmp_path: Path) -> None:
    argv = [*COMMENT, "--override-escalation=forgectl", "--yes"]
    assert run_cli(tmp_path, argv, stdin="pipe", stderr="pipe") == 2


def test_override_escalation_with_yes_and_human_claim_is_accepted_without_terminal(
    tmp_path: Path,
) -> None:
    argv = [*COMMENT, "--override-escalation=forgectl", "--yes", "--actor", "human:me"]
    assert run_cli(tmp_path, argv, stdin="pipe", stderr="pipe") == 1


@pytest.mark.parametrize(
    "argv",
    [
        ["comment", "1", "--body-file", "-"],
        ["reply", "1", "--to", "gh:ic:abc", "--body-file", "-"],
        ["approve", "1", "--body-file", "-"],
        ["review", "1", "--draft-file", "-"],
        ["draft", "check", "-"],
        ["draft", "new", "1", "--from", "-"],
    ],
)
def test_dash_input_with_a_terminal_stdin_is_usage(tmp_path: Path, argv: list[str]) -> None:
    assert run_cli(tmp_path, argv, stdin="pty", stderr="pty") == 2
    assert run_cli(tmp_path, argv, stdin="pty", stderr="pipe") == 2


def test_dash_input_from_a_pipe_is_accepted(tmp_path: Path) -> None:
    assert run_cli(tmp_path, ["comment", "1", "--body-file", "-"], stdin="pipe", stderr="pipe") == 1
