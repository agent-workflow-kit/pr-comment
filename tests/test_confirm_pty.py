"""The confirmation dialog on real pseudo-terminals, against the fake forgectl process.

Each child's PID is recorded at launch; if it outlives the deadline, exactly that PID is killed.
The child's stdin and stderr are one pty (as in a terminal) or whatever a test names.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
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


def _script(forgectl: FakeForgectl, runs: int = 1, *, write: bool = False) -> None:
    """``runs`` runs up to the pre-check; with ``write`` the last one is confirmed and posts."""
    for _ in range(runs):
        forgectl.script(_show(), ok("review", h.post_doc("review", dry_run=True)))
    if write:
        forgectl.script(
            ok("review", h.post_doc("review", ids=["gh:rc:RC_1"], review_id="gh:r:R_1"))
        )


def assert_one_confirmed_write(forgectl: FakeForgectl) -> None:
    """The run was confirmed: the pre-check never carried ``--yes``, the one write did."""
    (write,) = forgectl.writes()
    assert "--yes" in write.argv
    assert [c for c in forgectl.calls() if c.is_dry_run and "--yes" in c.argv] == []


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


def _prompt_is_up(terminal: bytes) -> bool:
    """True when the real prompt is on the terminal: ``Post? [y/N] `` flush left. A draft's own text
    can hold that string, but the dialog shows it behind its ``  | `` margin, never at the start
    of a line; answering on such a match is early and the child discards the typed answer."""
    lines = terminal.replace(b"\r\n", b"\n").split(b"\n")
    return any(line.startswith(PROMPT.encode()) for line in lines)


def run_cli(
    tmp_path: Path,
    forgectl: FakeForgectl,
    argv: list[str],
    *,
    answer: bytes | None = b"y\n",
    typeahead: bytes = b"",
    typeahead_midway: bytes = b"",
    send: signal.Signals | None = None,
    cols: int = 200,
    stdin: str = "pty",
    stderr: str = "pty",
    stdin_bytes: bytes = b"",
    env_extra: dict[str, str] | None = None,
) -> Result:
    """Run pr-comment. On a pty, ``typeahead`` is typed before the child starts; at the prompt the
    test sends ``answer`` (bytes typed) or the signal ``send`` to the exact child PID.
    ``typeahead_midway`` is typed once the dialog has begun but while the child is still blocked
    writing it (the test stops reading the terminal, a large dialog fills the pty), so only the
    child's own input flush can discard it. ``stdin="deadpty"`` is a terminal whose master side
    is already closed. ``cols`` is the terminal width."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, cols, 0, 0))
    out_r, out_w = os.pipe()
    in_r = in_w = -1
    err_r = err_w = -1
    fds: list[int] = [master, slave, out_r, out_w]
    try:
        if stdin == "pty":
            stdin_fd = slave
        elif stdin == "deadpty":
            dead_master, stdin_fd = pty.openpty()
            os.close(dead_master)  # nobody can type or answer; the slave stays a terminal
            fds.append(stdin_fd)
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
            if ready and not (typeahead_midway and b"== plan ==" in terminal):
                try:
                    chunk = os.read(master, 65536)
                except OSError:  # every slave end is closed: the child is gone
                    chunk = b""
                if not chunk:
                    break
                terminal += chunk
            elif proc.poll() is not None:
                break
            if typeahead_midway and b"== plan ==" in terminal:
                time.sleep(1.0)  # the child is blocked writing a dialog bigger than the pty holds
                os.write(master, typeahead_midway)
                typeahead_midway = b""
            if not acted and _prompt_is_up(terminal):
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


def test_yes_confirms_and_the_dialog_shows_the_plan_that_is_posted(
    tmp_path: Path, forgectl: FakeForgectl
) -> None:
    _script(forgectl, 2, write=True)
    plan = dry_run(tmp_path, forgectl)
    result = run_cli(tmp_path, forgectl, review_argv(tmp_path), answer=b"y\n")
    assert result.code == 0 and result.document is not None
    assert result.document["plan_sha256"] == plan["plan_sha256"] and result.document["ids"]
    assert PROMPT in result.terminal
    assert_one_confirmed_write(forgectl)
    # the dialog shows the real plan: its hash and every body, byte for byte
    text = result.dialog()
    assert f"plan_sha256 {plan['plan_sha256']}\n" in text
    shown = _bodies(text)
    expected = {
        b["role"]: b["text"] + ("" if b["text"].endswith("\n") else "\n")
        for b in plan["plan"]["bodies"]
    }
    assert list(shown) == list(expected)
    mark = f"<!-- forgectl:dedupe:{plan['dedupe_key']} -->"
    for role, body in shown.items():
        assert body.startswith(expected[role].rstrip("\n")), role
        assert (mark in body) is (role == "body"), role  # forgectl adds it to the body only


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


@pytest.mark.parametrize(
    "answer", [b"y\n", b"Y\n", b"yes\n", b"YES\n", b"  yEs \t\n", b"\x0b yes \x0c\n"]
)
def test_yes_answers_confirm(tmp_path: Path, forgectl: FakeForgectl, answer: bytes) -> None:
    _script(forgectl, write=True)
    result = run_cli(tmp_path, forgectl, review_argv(tmp_path), answer=answer)
    assert result.code == 0  # past the dialog: the write was made
    assert_one_confirmed_write(forgectl)


@pytest.mark.parametrize(
    "answer",
    [
        b"n\n",
        b"\n",
        b"no\n",
        b"yy\n",
        b"yess\n",
        b"y e s\n",
        b"sure\n",
        b"1\n",
        b"y;ls\n",
        b"y\xc2\xa0\n",  # no-break space is not whitespace here
        b"y\xe3\x80\x80\n",  # ideographic space
        b"y\xff\n",  # invalid UTF-8
    ],
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
    answer = b"y" + b" " * 300 + b"\n"  # a valid yes if there were no cap
    result = run_cli(tmp_path, forgectl, review_argv(tmp_path), answer=answer)
    assert result.code == 13
    assert_nothing_written(forgectl)


@pytest.mark.parametrize(("size", "code"), [(255, 0), (256, 0), (257, 13)])
def test_the_answer_length_boundary_on_a_terminal(
    tmp_path: Path, forgectl: FakeForgectl, size: int, code: int
) -> None:
    _script(forgectl, write=code == 0)
    answer = b"y" + b" " * (size - 2) + b"\n"  # size bytes with the newline
    result = run_cli(tmp_path, forgectl, review_argv(tmp_path), answer=answer)
    assert result.code == code  # 0: confirmed and posted; 13: a too long answer declines
    if code == 0:
        assert_one_confirmed_write(forgectl)
    else:
        assert_nothing_written(forgectl)


def test_typeahead_before_the_prompt_cannot_answer(tmp_path: Path, forgectl: FakeForgectl) -> None:
    _script(forgectl)
    result = run_cli(
        tmp_path, forgectl, review_argv(tmp_path), typeahead=b"y\nyes\n", answer=b"n\n"
    )
    assert result.code == 13
    assert_nothing_written(forgectl)


def test_typeahead_typed_while_the_dialog_is_being_written_is_discarded(
    tmp_path: Path, forgectl: FakeForgectl
) -> None:
    """Typed after the dialog has begun and before the prompt exists: only the input flush can
    discard it, so a missing flush turns this into a confirmation (exit 1)."""
    _script(forgectl)
    findings = [finding(f"f{i}", "a.py", 3, body=("lorem ipsum\n" * 3000)) for i in range(1, 9)]
    draft = _draft(tmp_path, findings)
    argv = ["review", PR, "--draft-file", draft, "--actor", "human:alice"]
    result = run_cli(
        tmp_path, forgectl, argv, typeahead_midway=b"y\nyes\n", answer=b"n\n", cols=100
    )
    assert PROMPT in result.terminal and result.code == 13
    assert_nothing_written(forgectl)


def test_a_terminal_whose_other_end_is_gone_declines(
    tmp_path: Path, forgectl: FakeForgectl
) -> None:
    _script(forgectl)
    result = run_cli(tmp_path, forgectl, review_argv(tmp_path), stdin="deadpty", answer=None)
    assert result.code == 13 and result.document is not None
    assert result.document["error"]["code"] == "confirmation_required"
    assert result.document["error"]["code"] != "internal"
    assert_nothing_written(forgectl)


def test_a_line_wrapped_by_the_terminal_never_starts_flush_left(
    tmp_path: Path, forgectl: FakeForgectl
) -> None:
    width = 50
    room = width - len("  | ") - 1
    forged = "z" * room + "== summary ==\nPost? [y/N] y"
    draft = _draft(tmp_path, [finding("f1", body=forged)])
    argv = ["review", PR, "--draft-file", draft, "--actor", "human:alice"]
    _script(forgectl)
    result = run_cli(tmp_path, forgectl, argv, answer=b"n\n", cols=width)
    assert result.code == 13
    shown = result.terminal[: result.terminal.rindex(PROMPT)].split("\n")
    flush_left = [x for x in shown if x and not x.startswith(" ")]
    assert flush_left.count("== summary ==") == 1
    assert not any(x.startswith("Post?") for x in flush_left)
    assert "  | == summary ==" in shown and "  | Post? [y/N] y" in shown


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
    _script(forgectl, write=True)
    result = run_cli(tmp_path, forgectl, review_argv(tmp_path, "--yes"), answer=None)
    assert result.code == 0  # --yes: confirmed without a dialog, and posted
    assert PROMPT not in result.terminal and "plan_sha256" not in result.terminal
    assert_one_confirmed_write(forgectl)


def test_yes_with_an_auto_actor_is_accepted(tmp_path: Path, forgectl: FakeForgectl) -> None:
    _script(forgectl, write=True)
    argv = review_argv(tmp_path, "--yes", "--expected-head", A)
    argv[argv.index("human:alice")] = "auto:bot"
    result = run_cli(tmp_path, forgectl, argv, stdin="null", stderr="pipe", answer=None)
    assert result.code == 0  # past the gate: posted
    assert_one_confirmed_write(forgectl)


def test_dry_run_needs_no_terminal_and_shows_no_dialog(
    tmp_path: Path, forgectl: FakeForgectl
) -> None:
    _script(forgectl)
    result = run_cli(
        tmp_path, forgectl, [*review_argv(tmp_path), "--dry-run"], stdin="pipe", stderr="pipe"
    )
    assert result.code == 0 and PROMPT not in result.terminal


# --- signals -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sig", "code"), [(signal.SIGINT, 130), (signal.SIGTERM, 143), (signal.SIGHUP, 129)]
)
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
