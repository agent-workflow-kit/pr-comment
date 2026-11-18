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

PROMPT = "Post? [y/N] "


@dataclass
class PtyResult:
    code: int
    document: dict[str, Any] | None
    terminal: str


def _prompt_is_up(terminal: bytes) -> bool:
    """The real prompt is flush left on a line; a draft's own text is shown behind a margin."""
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
    once the prompt is on the terminal."""
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
        answered = False
        limit = time.monotonic() + deadline
        while True:
            if time.monotonic() > limit:
                proc.kill()  # this exact child
                proc.wait()
                raise AssertionError("pr-comment did not finish on the pty (killed)")
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                terminal += chunk
            elif proc.poll() is not None:
                break
            if not answered and _prompt_is_up(terminal):
                answered = True
                os.write(master, answer)
        code = proc.wait(timeout=deadline)
        fds.discard(out_r)
        with os.fdopen(out_r, "r", encoding="utf-8") as handle:
            text = handle.read()
        document = json.loads(text) if text.strip() else None
        return PtyResult(code, document, terminal.decode("utf-8", "replace").replace("\r\n", "\n"))
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        for fd in list(fds):
            with contextlib.suppress(OSError):
                close(fd)
