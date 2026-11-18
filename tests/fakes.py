"""Fake forgectl executables for the gateway's process tests, and the teardown that ends only the
processes a test started.

A fake is a Python script (shebang: the interpreter running the tests) whose prelude records its
own pid, process group and start time in ``<dir>/pids``, and its argv in ``<dir>/argv.json``. The
``tracker`` fixture kills, at teardown, exactly the recorded processes whose start time still
matches: never by pattern.
"""

from __future__ import annotations

import os
import signal
import stat
import sys
import textwrap
from pathlib import Path

PRELUDE = """\
import json, os, subprocess, sys, time

D = {directory!r}


def start_of(pid):
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            return f.read().rpartition(b")")[2].split()[19].decode()
    except OSError:
        return "?"


def track(pid):
    with open(D + "/pids", "a") as f:
        f.write("%d %d %s\\n" % (pid, os.getpgid(pid), start_of(pid)))
        f.flush()


track(os.getpid())
with open(D + "/argv.json", "w") as f:
    json.dump(sys.argv[1:], f)


def grandchild(code, **kw):
    p = subprocess.Popen([sys.executable, "-c", code], **kw)
    track(p.pid)
    return p
"""


def make_fake(directory: Path, body: str, name: str = "forgectl") -> Path:
    """Write an executable fake at ``directory/bin/<name>`` running ``body`` after the prelude."""
    bindir = directory / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / name
    code = PRELUDE.format(directory=str(directory)) + textwrap.dedent(body)
    script.write_text(f"#!{sys.executable}\n{code}")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _start_of(pid: int) -> str | None:
    try:
        data = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return None
    return data.rpartition(b")")[2].split()[19].decode()


def alive(pid: int, start: str | None) -> bool:
    """Whether the process recorded as (pid, start) still runs (a zombie does not count)."""
    try:
        data = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return False
    fields = data.rpartition(b")")[2].split()
    return fields[0] != b"Z" and (start is None or fields[19].decode() == start)


def recorded(directory: Path) -> list[tuple[int, int, str]]:
    path = directory / "pids"
    if not path.exists():
        return []
    rows: list[tuple[int, int, str]] = []
    for line in path.read_text().splitlines():
        pid, pgid, start = line.split()
        rows.append((int(pid), int(pgid), start))
    return rows


class Tracker:
    def __init__(self) -> None:
        self.directories: list[Path] = []

    def watch(self, directory: Path) -> Path:
        self.directories.append(directory)
        return directory

    def cleanup(self) -> None:
        for directory in self.directories:
            for pid, pgid, start in recorded(directory):
                if _start_of(pid) != start:
                    continue  # gone, or the pid belongs to something else now
                try:
                    if pgid == pid:
                        os.killpg(pgid, signal.SIGKILL)
                    else:
                        os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
