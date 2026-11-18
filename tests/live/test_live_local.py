"""Live checks that touch no host and no credentials: a real pager and a real filesystem (#38).

Marked ``live`` so they stay out of the default run, but they need no opt-in variable: each runs
``pr-comment export`` in a scratch HOME and state directory (under ``PR_COMMENT_LIVE_FS_DIR`` when
set, to test another filesystem such as a network or APFS volume), with no ``gh``, ``glab`` or
forgectl on ``PATH``. Run them with ``pytest -m live tests/live/test_live_local.py``.
"""

from __future__ import annotations

import os
import pty
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.live.settings import REAL_ENV
from tests.test_export import ATT1, audit_rec, line, log_rec

pytestmark = pytest.mark.live

DEADLINE = 60.0


def _scratch(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    base_dir = REAL_ENV.get("PR_COMMENT_LIVE_FS_DIR")
    base = Path(base_dir) / f"prc-live-{os.getpid()}" if base_dir else tmp_path / "fs"
    home = base / "home"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / "state"),
        "PATH": str(tmp_path),  # no gh, glab or forgectl
        "LANG": REAL_ENV.get("LANG", "C.UTF-8"),
    }
    return home / "state" / "pr-comment", env


def _export(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "pr_comment", "export", *args],
        capture_output=True,
        env=env,
        cwd="/",
        timeout=DEADLINE,
        check=False,
    )


def _plant_log(state: Path, env: dict[str, str]) -> Path:
    """The state directory (made by pr-comment itself) and a log of about 3 MiB."""
    assert _export(env).returncode == 0
    path = state / "log.jsonl"
    record = line(log_rec(details={"pad": "y" * 80}))
    path.write_bytes(record * 30_000)
    path.chmod(0o600)
    return path


def test_a_closed_pipe_with_head_ends_quietly_with_exit_zero(tmp_path: Path) -> None:
    """Row ex.closed_pipe: ``pr-comment export --log | head -n 1``."""
    state, env = _scratch(tmp_path)
    _plant_log(state, env)
    exporter = subprocess.Popen(
        [sys.executable, "-m", "pr_comment", "export", "--log"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd="/",
    )
    assert exporter.stdout is not None
    head = shutil.which("head", path=REAL_ENV.get("PATH", ""))
    if head is None:
        exporter.kill()  # this exact child
        exporter.communicate()
        pytest.skip("no head on PATH")
    reader = subprocess.Popen([head, "-n", "1"], stdin=exporter.stdout, stdout=subprocess.PIPE)
    exporter.stdout.close()
    first, _ = reader.communicate(timeout=DEADLINE)
    _, err = exporter.communicate(timeout=DEADLINE)
    assert first.endswith(b"\n")
    assert (exporter.returncode, err) == (0, b"")


def test_a_closed_pipe_with_less_on_a_terminal_ends_quietly(tmp_path: Path) -> None:
    """Row ex.closed_pipe: the pager is ``less`` on a pty and the reader quits at once with ``q``;
    bash's PIPESTATUS gives pr-comment's own exit status."""
    for tool in ("bash", "less"):
        if shutil.which(tool, path=REAL_ENV.get("PATH", "")) is None:
            pytest.skip(f"no {tool} on PATH")
    state, env = _scratch(tmp_path)
    _plant_log(state, env)
    status = tmp_path / "status"
    errors = tmp_path / "errors"
    script = (
        f"{sys.executable} -m pr_comment export --log 2>{errors} | less; "
        f"echo ${{PIPESTATUS[0]}} > {status}"
    )
    master, slave = pty.openpty()
    full_env = {**env, "PATH": REAL_ENV.get("PATH", ""), "TERM": "xterm", "LESS": ""}
    proc = subprocess.Popen(
        [shutil.which("bash", path=full_env["PATH"]) or "bash", "-c", script],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=full_env,
        cwd="/",
    )
    os.close(slave)
    try:
        seen = b""
        limit = time.monotonic() + DEADLINE
        quit_sent = False
        while time.monotonic() < limit and proc.poll() is None:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                seen += chunk
            if seen and not quit_sent:
                os.write(master, b"q")
                quit_sent = True
        proc.wait(timeout=DEADLINE)
    finally:
        if proc.poll() is None:
            proc.kill()  # this exact child
            proc.wait()
        os.close(master)
    assert status.read_text(encoding="utf-8").strip() == "0"
    assert errors.read_bytes() == b""


@pytest.mark.parametrize(
    "case",
    ["symlink", "dangling", "fifo", "directory", "hardlink", "group_writable", "foreign_owner"],
)
def test_unsafe_audit_files_are_refused_on_a_real_filesystem(case: str, tmp_path: Path) -> None:
    """Row ex.unsafe_files: each hostile ``audit.jsonl`` ends in exit 12 ``audit_unavailable``
    within the deadline (a FIFO must not block)."""
    state, env = _scratch(tmp_path)
    assert _export(env).returncode == 0
    audit = state / "audit.jsonl"
    good = tmp_path / "good.jsonl"
    good.write_bytes(line(audit_rec(ATT1)))
    good.chmod(0o600)
    if case == "symlink":
        audit.symlink_to(good)
    elif case == "dangling":
        audit.symlink_to(tmp_path / "nowhere")
    elif case == "fifo":
        os.mkfifo(audit, 0o600)
    elif case == "directory":
        audit.mkdir()
    elif case == "hardlink":
        shutil.copyfile(good, audit)
        audit.chmod(0o600)
        os.link(audit, tmp_path / "second-name")
    elif case == "group_writable":
        shutil.copyfile(good, audit)
        audit.chmod(0o620)
    else:
        if os.geteuid() != 0:
            pytest.skip("a file owned by another user needs root to create (manual step)")
        shutil.copyfile(good, audit)
        os.chown(audit, 65534, -1)
    done = _export(env, "--json")
    assert done.returncode == 12, done.stderr
    assert b'"audit_unavailable"' in done.stdout
    assert b"Traceback" not in done.stderr
