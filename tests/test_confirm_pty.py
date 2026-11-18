"""The confirmation dialog on real pseudo-terminals, against the fake forgectl process.

Each child's PID is recorded at launch; if it outlives the deadline, exactly that PID is killed.
The child's stdin and stderr are one pty (as in a terminal) or whatever a test names.
"""

from __future__ import annotations

import contextlib
import json
import os
import pty
import select
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests import forgectl_harness as h
from tests.forgectl_harness import FakeForgectl, ok
from tests.posting import PR, A, draft_doc, finding
from tests.schema_harness import validate

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pty")

DEADLINE = 40.0
PROMPT = "Post? [y/N] "


def _show() -> dict[str, Any]:
    doc = h.show_doc(PR)
    doc["head"] = {**doc["head"], "sha": A}
    return ok("show", doc)


def _script(forgectl: FakeForgectl, runs: int = 1) -> None:
    for _ in range(runs):
        forgectl.script(_show(), ok("review", h.post_doc("review", dry_run=True)))


def _draft(tmp_path: Path, findings: list[dict[str, Any]], **kw: Any) -> str:
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(draft_doc(findings, **kw)), encoding="utf-8")
    return str(path)


@dataclass
class Result:
    code: int
    document: dict[str, Any] | None
    terminal: str  # what the child wrote to the terminal, CRLF as LF
    pid: int

    def dialog(self) -> str:
        """The dialog up to the prompt (without the echo of the answer)."""
        text = self.terminal
        return text[: text.index(PROMPT)] if PROMPT in text else text


def run_cli(
    tmp_path: Path,
    forgectl: FakeForgectl,
    argv: list[str],
    *,
    answer: bytes | None = b"y\n",
    typeahead: bytes = b"",
    send: signal.Signals | None = None,
    stdin: str = "pty",
    stderr: str = "pty",
    stdin_bytes: bytes = b"",
    env_extra: dict[str, str] | None = None,
) -> Result:
    """Run pr-comment. On a pty, ``typeahead`` is typed before the child starts; at the prompt the
    test sends ``answer`` (bytes typed) or the signal ``send`` to the exact child PID."""
    master, slave = pty.openpty()
    out_r, out_w = os.pipe()
    in_r = in_w = -1
    err_r = err_w = -1
    fds: list[int] = [master, slave, out_r, out_w]
    try:
        if stdin == "pty":
            stdin_fd = slave
        else:
            in_r, in_w = os.pipe()
            fds += [in_r, in_w]
            stdin_fd = in_r
            os.write(in_w, stdin_bytes)
        if stderr == "pty":
            stderr_fd = slave
        else:
            err_r, err_w = os.pipe()
            fds += [err_r, err_w]
            stderr_fd = err_w
        if typeahead:
            os.write(master, typeahead)
        env = forgectl.environ(env_extra)
        proc = subprocess.Popen(
            [sys.executable, "-m", "pr_comment", *argv, "--json"],
            stdin=stdin_fd,
            stdout=out_w,
            stderr=stderr_fd,
            env=env,
            cwd=tmp_path,
        )
        pid = proc.pid  # recorded at launch: the only PID this test may signal
        os.close(out_w)
        out_w = -1
        os.close(slave)
        slave = -1
        terminal = b""
        acted = False
        deadline = time.monotonic() + DEADLINE
        while True:
            if time.monotonic() > deadline:
                if proc.pid == pid and proc.poll() is None:
                    os.kill(pid, signal.SIGKILL)
                    proc.wait()
                pytest.fail("pr-comment did not finish: " + terminal.decode("utf-8", "replace"))
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:  # every slave end is closed: the child is gone
                    chunk = b""
                if not chunk:
                    break
                terminal += chunk
            elif proc.poll() is not None:
                break
            if not acted and PROMPT.encode() in terminal:
                acted = True
                if send is not None:
                    os.kill(pid, send)
                elif answer is not None:
                    os.write(master, answer)
        code = proc.wait(timeout=DEADLINE)
        with os.fdopen(out_r, "r", encoding="utf-8") as handle:
            out_r = -1
            text = handle.read()
        if stderr != "pty":
            os.close(err_w)
            err_w = -1
            with os.fdopen(err_r, "rb") as handle:
                err_r = -1
                terminal = handle.read()
        document = json.loads(text) if text.strip() else None
        return Result(
            code, document, terminal.decode("utf-8", "replace").replace("\r\n", "\n"), pid
        )
    finally:
        for fd in (*fds, in_r, in_w, err_r, err_w):
            with contextlib.suppress(OSError):
                os.close(fd)


def review_argv(tmp_path: Path, *extra: str) -> list[str]:
    draft = _draft(tmp_path, [finding("f1", body="Check the bound."), finding("f2", "b.py", None)])
    return ["review", PR, "--draft-file", draft, "--actor", "human:alice", *extra]


def dry_run(tmp_path: Path, forgectl: FakeForgectl) -> dict[str, Any]:
    document = _dry(tmp_path, forgectl).document
    assert document is not None
    return document


def _dry(tmp_path: Path, forgectl: FakeForgectl) -> Result:
    env = forgectl.environ()
    proc = subprocess.Popen(
        [sys.executable, "-m", "pr_comment", *review_argv(tmp_path), "--dry-run", "--json"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=tmp_path,
    )
    pid = proc.pid
    try:
        out, err = proc.communicate(timeout=DEADLINE)
    except subprocess.TimeoutExpired:
        if proc.pid == pid and proc.poll() is None:
            os.kill(pid, signal.SIGKILL)
            proc.wait()
        raise
    assert proc.returncode == 0, err
    return Result(0, json.loads(out), err.decode(), pid)


def assert_nothing_written(forgectl: FakeForgectl) -> None:
    assert forgectl.writes() == []
    assert all("--yes" not in c.argv for c in forgectl.calls())


# --- the answer --------------------------------------------------------------------------------


def test_yes_confirms_and_the_run_stops_at_the_seam(tmp_path: Path, forgectl: FakeForgectl) -> None:
    _script(forgectl, 2)
    plan = dry_run(tmp_path, forgectl)
    result = run_cli(tmp_path, forgectl, review_argv(tmp_path), answer=b"y\n")
    assert result.code == 1 and result.document is not None
    assert result.document["error"]["code"] == "internal"  # TODO(#29): the write order
    assert PROMPT in result.terminal
    assert_nothing_written(forgectl)
    # the dialog shows the real plan: its hash and every body, byte for byte
    text = result.dialog()
    assert f"plan_sha256 {plan['plan_sha256']}\n" in text
    shown = _bodies(text)
    expected = {
        b["role"]: b["text"] + ("" if b["text"].endswith("\n") else "\n")
        for b in plan["plan"]["bodies"]
    }
    assert list(shown) == list(expected)
    for role, body in shown.items():
        assert body.startswith(expected[role].rstrip("\n")), role
        assert f"<!-- forgectl:dedupe:{plan['dedupe_key']} -->" in body


def _bodies(dialog: str) -> dict[str, str]:
    """role to the body text rebuilt from the gutter lines of the dialog."""
    bodies: dict[str, list[str]] = {}
    role = None
    for line in dialog.split("\n"):
        if line.startswith("--- "):
            role = line[4:].split(" ")[0].rstrip(",")
            bodies[role] = []
        elif line.startswith("== summary"):
            role = None
        elif role is not None and line.startswith("  | "):
            bodies[role].append(line[4:])
    return {r: "\n".join(lines) for r, lines in bodies.items()}


@pytest.mark.parametrize("answer", [b"y\n", b"Y\n", b"yes\n", b"YES\n", b"  yEs \t\n"])
def test_yes_answers_confirm(tmp_path: Path, forgectl: FakeForgectl, answer: bytes) -> None:
    _script(forgectl)
    result = run_cli(tmp_path, forgectl, review_argv(tmp_path), answer=answer)
    assert result.code == 1  # past the dialog; the seam ends the run
    assert_nothing_written(forgectl)


@pytest.mark.parametrize(
    "answer",
    [b"n\n", b"\n", b"no\n", b"yy\n", b"yess\n", b"y e s\n", b"sure\n", b"1\n", b"y;ls\n"],
)
def test_other_answers_decline(tmp_path: Path, forgectl: FakeForgectl, answer: bytes) -> None:
    _script(forgectl)
    result = run_cli(tmp_path, forgectl, review_argv(tmp_path), answer=answer)
    assert result.code == 13 and result.document is not None
    validate("pr-comment/cli/error.json", result.document)
    assert result.document["error"]["code"] == "confirmation_required"
    assert result.document["error"]["message"] == "declined"
    assert_nothing_written(forgectl)


def test_eof_declines(tmp_path: Path, forgectl: FakeForgectl) -> None:
    _script(forgectl)
    result = run_cli(tmp_path, forgectl, review_argv(tmp_path), answer=b"\x04")  # ^D at line start
    assert result.code == 13 and result.document is not None
    assert result.document["error"]["message"] == "declined"
    assert_nothing_written(forgectl)


def test_eof_after_a_partial_line_does_not_confirm(tmp_path: Path, forgectl: FakeForgectl) -> None:
    _script(forgectl)
    result = run_cli(tmp_path, forgectl, review_argv(tmp_path), answer=b"y\x04\x04")
    # "y" then ^D flushes "y" to the reader without a newline; a second ^D is EOF. The first read
    # returns b"y" (no newline) and the next EOF ends the line: the line was never completed.
    assert result.code == 13
    assert_nothing_written(forgectl)


def test_an_overlong_line_declines_and_is_not_left_for_the_shell(
    tmp_path: Path, forgectl: FakeForgectl
) -> None:
    _script(forgectl)
    answer = b"y" * 300 + b"\n"
    result = run_cli(tmp_path, forgectl, review_argv(tmp_path), answer=answer)
    assert result.code == 13
    assert_nothing_written(forgectl)


def test_typeahead_before_the_prompt_cannot_answer(tmp_path: Path, forgectl: FakeForgectl) -> None:
    _script(forgectl)
    result = run_cli(
        tmp_path, forgectl, review_argv(tmp_path), typeahead=b"y\nyes\n", answer=b"n\n"
    )
    assert result.code == 13
    assert_nothing_written(forgectl)


def test_the_answer_is_not_echoed_back_by_pr_comment(
    tmp_path: Path, forgectl: FakeForgectl
) -> None:
    _script(forgectl)
    hostile = b"\x1b[31mzzz\n"
    result = run_cli(tmp_path, forgectl, review_argv(tmp_path), answer=hostile)
    assert result.code == 13
    # after the prompt only the tty's own echo of the typed line (not pr-comment's output)
    after = result.terminal.split(PROMPT, 1)[1]
    assert "declined" in after  # the one-line message from the envelope
    assert after.count("zzz") <= 1


# --- who may answer ----------------------------------------------------------------------------


def test_a_piped_stdin_cannot_answer_even_with_yes_typed_into_it(
    tmp_path: Path, forgectl: FakeForgectl
) -> None:
    _script(forgectl)
    result = run_cli(
        tmp_path,
        forgectl,
        review_argv(tmp_path),
        stdin="pipe",
        stdin_bytes=b"y\ny\n",
        stderr="pty",
    )
    assert result.code == 13 and result.document is not None
    assert result.document["error"]["code"] == "confirmation_required"
    assert PROMPT not in result.terminal  # no dialog was offered
    assert_nothing_written(forgectl)


def test_a_non_terminal_stderr_is_not_interactive(tmp_path: Path, forgectl: FakeForgectl) -> None:
    _script(forgectl)
    result = run_cli(tmp_path, forgectl, review_argv(tmp_path), stderr="pipe", answer=None)
    assert result.code == 13
    assert PROMPT not in result.terminal
    assert_nothing_written(forgectl)


@pytest.mark.parametrize(
    "env",
    [
        {"PR_COMMENT_YES": "1"},
        {"PR_COMMENT_INTERACTIVE": "1"},
        {"PR_COMMENT_CONFIRM": "y"},
        {"CI": "", "YES": "1", "FORCE": "1"},
    ],
)
def test_no_environment_variable_answers_or_enables_a_run(
    tmp_path: Path, forgectl: FakeForgectl, env: dict[str, str]
) -> None:
    _script(forgectl)
    result = run_cli(
        tmp_path,
        forgectl,
        review_argv(tmp_path),
        stdin="pipe",
        stderr="pipe",
        env_extra=env,
        answer=None,
    )
    assert result.code == 13
    assert_nothing_written(forgectl)


def test_an_auto_actor_on_a_terminal_is_not_offered_a_dialog(
    tmp_path: Path, forgectl: FakeForgectl
) -> None:
    _script(forgectl)
    argv = review_argv(tmp_path)
    argv[argv.index("human:alice")] = "auto:bot"
    result = run_cli(tmp_path, forgectl, argv, answer=None)
    assert result.code == 13 and PROMPT not in result.terminal
    assert_nothing_written(forgectl)


def test_yes_skips_the_dialog_and_never_prompts(tmp_path: Path, forgectl: FakeForgectl) -> None:
    _script(forgectl)
    result = run_cli(tmp_path, forgectl, review_argv(tmp_path, "--yes"), answer=None)
    assert result.code == 1  # the seam: the write orders are later issues
    assert PROMPT not in result.terminal and "plan_sha256" not in result.terminal
    assert_nothing_written(forgectl)


def test_yes_with_an_auto_actor_is_accepted(tmp_path: Path, forgectl: FakeForgectl) -> None:
    _script(forgectl)
    argv = review_argv(tmp_path, "--yes", "--expected-head", A)
    argv[argv.index("human:alice")] = "auto:bot"
    result = run_cli(tmp_path, forgectl, argv, stdin="null", stderr="pipe", answer=None)
    assert result.code == 1  # past the gate
    assert_nothing_written(forgectl)


def test_dry_run_needs_no_terminal_and_shows_no_dialog(
    tmp_path: Path, forgectl: FakeForgectl
) -> None:
    _script(forgectl)
    result = run_cli(
        tmp_path, forgectl, [*review_argv(tmp_path), "--dry-run"], stdin="pipe", stderr="pipe"
    )
    assert result.code == 0 and PROMPT not in result.terminal


# --- signals -----------------------------------------------------------------------------------


@pytest.mark.parametrize(("sig", "code"), [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
def test_a_signal_at_the_prompt_sends_nothing_and_exits_128_plus(
    tmp_path: Path, forgectl: FakeForgectl, sig: signal.Signals, code: int
) -> None:
    _script(forgectl)
    result = run_cli(tmp_path, forgectl, review_argv(tmp_path), send=sig)
    assert result.code == code and result.document is None
    assert_nothing_written(forgectl)


# --- hostile summaries -------------------------------------------------------------------------


def test_hostile_text_is_marked_in_the_dialog_on_a_real_terminal(
    tmp_path: Path, forgectl: FakeForgectl
) -> None:
    # What the pipeline lets through: bidi, zero-width, private use, tab.
    hostile = "ok ‮evil⁩ a​b c d  e\tf"
    draft = _draft(tmp_path, [finding("f1", body=hostile)], body="Overall ‮ text")
    argv = ["review", PR, "--draft-file", draft, "--actor", "human:alice"]
    _script(forgectl)
    result = run_cli(tmp_path, forgectl, argv, answer=b"n\n")
    text = result.dialog()
    assert result.code == 13
    for char in ("‮", "⁩", "​", " ", ""):
        assert char not in text
    for mark in ("<U+202E>", "<U+2069>", "<U+200B>", "<U+E000>"):
        assert mark in text
    assert "invisible characters marked" in text
    # the control characters that would rewrite the screen are only the terminal's own (the echo
    # of the typed answer and its CRLF): nothing in the dialog text is an escape sequence
    assert "\x1b" not in text
