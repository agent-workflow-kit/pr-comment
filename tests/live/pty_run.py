"""Run a command on a pseudo-terminal and answer its confirmation prompt (the interactive human
approve of the live suite). Only the child this module started is ever signaled, by its Popen."""

from __future__ import annotations

import contextlib
import json
import os
import pty
import select
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import LiveFailure

PROMPT = "Post? [y/N] "
TAIL_WINDOW = 8192  # the prompt is looked for in the end of the terminal only
TERMINAL_KEEP = 1 << 20  # what is kept of the terminal for the report
STDOUT_CAP = 16 << 20  # the JSON document; more than this is a failure, never an unbounded read


@dataclass
class PtyResult:
    code: int
    document: dict[str, Any] | None
    terminal: str


def _prompt_is_up(terminal: bytes) -> bool:
    """The real prompt is flush left on a line; a draft's own text is shown behind a margin. Only
    the tail window is scanned, and its first (possibly cut) line is skipped."""
    if len(terminal) > TAIL_WINDOW:
        terminal = terminal[-TAIL_WINDOW:]
        terminal = terminal.partition(b"\n")[2]
    lines = terminal.replace(b"\r\n", b"\n").split(b"\n")
    return any(line.startswith(PROMPT.encode()) for line in lines)


def run_on_pty(
    cmd: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    answer: bytes,
    deadline: float = 120.0,
) -> PtyResult:
    """stdin and stderr are one pty; stdout is a pipe (the JSON document). ``answer`` is typed
    once the prompt is on the terminal. Both are read as they arrive, so a child that writes a lot
    to either cannot block on a full pipe."""
    __tracebackhide__ = True
    master, slave = pty.openpty()
    out_r, out_w = os.pipe()
    proc: subprocess.Popen[bytes] | None = None
    fds = {master, slave, out_r, out_w}

    def close(fd: int) -> None:
        if fd in fds:
            fds.discard(fd)
            os.close(fd)

    try:
        proc = subprocess.Popen(
            list(cmd), stdin=slave, stdout=out_w, stderr=slave, env=dict(env), cwd=cwd
        )
        close(out_w)
        close(slave)
        terminal = b""
        stdout = bytearray()
        truncated = False
        answered = False
        master_open = out_open = True
        limit = time.monotonic() + deadline
        while master_open or out_open:
            if time.monotonic() > limit:
                proc.kill()  # this exact child
                proc.wait()
                raise LiveFailure("pr-comment did not finish on the pty (killed)")
            watch = ([master] if master_open else []) + ([out_r] if out_open else [])
            ready, _, _ = select.select(watch, [], [], 0.1)
            for fd in ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    chunk = b""
                if fd == master:
                    if chunk:
                        terminal = (terminal + chunk)[-TERMINAL_KEEP:]
                    else:
                        master_open = False
                elif chunk:
                    if len(stdout) < STDOUT_CAP:
                        stdout += chunk[: STDOUT_CAP - len(stdout)]
                    else:
                        truncated = True
                else:
                    out_open = False
            if not ready and proc.poll() is not None:
                break
            if not answered and master_open and _prompt_is_up(terminal):
                answered = True
                os.write(master, answer)
        code = proc.wait(timeout=deadline)
        if truncated:
            raise LiveFailure("pr-comment printed more than the stdout cap on the pty run")
        text = stdout.decode("utf-8", "replace")
        document = None
        if text.strip():
            try:
                loaded = json.loads(text)
            except ValueError:
                raise LiveFailure(
                    "the pty run's stdout is not one JSON document (details withheld)"
                ) from None
            document = loaded if isinstance(loaded, dict) else {"value": loaded}
        return PtyResult(code, document, terminal.decode("utf-8", "replace").replace("\r\n", "\n"))
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        for fd in list(fds):
            with contextlib.suppress(OSError):
                close(fd)
