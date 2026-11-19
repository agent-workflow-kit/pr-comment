"""The watchdog of an ``async`` ``post-post`` hook (``pr_comment.watchdog``, issue #82).

Real tiny ``sh`` scripts run under a real detached supervisor process. Every test runs with a
private HOME and state directory (``conftest.private_home``). Each process a hook starts records
its PID in a file; ``pids`` signals exactly those PIDs at teardown if a test left one alive (never
by pattern), and each supervisor is a ``Popen`` this test holds (waited for or killed by PID).
Timeouts are fractions of a second. No forgectl is involved.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from pr_comment import hooks as hooks_module
from pr_comment.config import HookEntry
from pr_comment.files import HookDir, StateDirs, StateUnsafeError, create_hook_dir, open_state_dirs
from pr_comment.hooks import POST, HookRunner, HookSet
from pr_comment.jsonlog import LogWriter
from tests.test_hooks import (
    BASE_ENV,
    PAYLOAD,
    PR,
    alive,
    kill_recorded,
    start_mark,
    tmp_entries,
    wait_dead,
    wait_file,
)

GRACE = 0.3
# A hook timeout the hook must outlive its own start-up under (issue #94): never below 5 s.
TIMEOUT = 5.0


@pytest.fixture
def pids(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "pids"
    path.touch()
    mark = start_mark()
    yield path
    kill_recorded(path, mark)


@pytest.fixture
def dirs() -> Iterator[StateDirs]:
    with open_state_dirs() as opened:
        yield opened


@pytest.fixture
def bystanders() -> Iterator[list[subprocess.Popen[bytes]]]:
    """Processes of this test that no watchdog may touch; each is killed by its own Popen."""
    started: list[subprocess.Popen[bytes]] = []
    yield started
    for proc in started:
        with contextlib.suppress(OSError):
            proc.kill()
        proc.wait(5)


@pytest.fixture(autouse=True)
def supervisors() -> Iterator[None]:
    """Whatever supervisor a test left running is killed by its exact PID (a ``Popen`` of ours)
    and reaped; the detached list is emptied."""
    before = len(hooks_module._DETACHED)  # pyright: ignore[reportPrivateUsage]
    yield
    for proc in hooks_module._DETACHED[before:]:  # pyright: ignore[reportPrivateUsage]
        if proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.kill()
        proc.wait(5)
    del hooks_module._DETACHED[before:]  # pyright: ignore[reportPrivateUsage]


@pytest.fixture(autouse=True)
def signal_handlers() -> Iterator[None]:
    """``watchdog.supervise`` ignores SIGINT, SIGTERM and SIGHUP once the hook has ended; a test
    that runs it in this process gets the previous handlers back."""
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


def make_runner(dirs: StateDirs, *, log: bool = True) -> HookRunner:
    return HookRunner(
        HookSet(),
        dirs=dirs,
        environ=BASE_ENV,
        log=LogWriter.open(dirs) if log else LogWriter.disabled(),
        pr=PR,
        attempt_id="a" * 22,
        term_grace=GRACE,
        interrupt_grace=GRACE,
    )


def spawn(
    runner: HookRunner, cmd: str, timeout: float = TIMEOUT, index: int = 3
) -> subprocess.Popen[bytes]:
    entry = HookEntry(cmd, timeout, async_=True)
    assert runner.spawn_detached(POST, entry, PAYLOAD, index) is True
    return hooks_module._DETACHED[-1]  # pyright: ignore[reportPrivateUsage]


def log_lines(dirs: StateDirs) -> list[dict[str, Any]]:
    path = dirs.state_path / "log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def recorded(path: Path) -> list[int]:
    return [int(word) for word in path.read_text().split() if word.isdigit()]


def start_bystander(bystanders: list[subprocess.Popen[bytes]]) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        ["/bin/sleep", "60"], start_new_session=True, env={}, stdin=subprocess.DEVNULL
    )
    bystanders.append(proc)
    return proc


# --- the timeout --------------------------------------------------------------------------------


def test_an_async_hook_over_its_timeout_is_terminated_with_everything_in_its_group(
    dirs: StateDirs, pids: Path
) -> None:
    runner = make_runner(dirs)
    started = time.monotonic()
    supervisor = spawn(runner, f"echo $$ >> {pids}; sleep 60 & echo $! >> {pids}; wait")
    assert supervisor.wait(60) == 0
    assert time.monotonic() - started < 50  # ended by the timeout, not by the hook's 60 s
    hook_pid, child_pid = recorded(pids)
    assert wait_dead(hook_pid) and wait_dead(child_pid)
    # Neither is left as a zombie either: the supervisor reaped the leader, init the rest.
    assert not Path(f"/proc/{hook_pid}").exists()


def test_a_timeout_logs_one_warning_with_a_fixed_reason_and_no_hook_text(
    dirs: StateDirs, pids: Path
) -> None:
    secret = "do-not-log-this-hook-text"
    runner = make_runner(dirs)
    supervisor = spawn(runner, f"echo $$ >> {pids}; echo {secret} >&2; sleep 60", index=3)
    assert supervisor.wait(60) == 0
    lines = log_lines(dirs)
    assert [(r["event"], r["level"]) for r in lines] == [("warning", "warn")]
    assert lines[0]["details"] == {"hook": "post-post", "index": 3, "reason": "hook_timeout"}
    assert lines[0]["pr"] == PR and lines[0]["attempt_id"] == "a" * 22
    assert secret not in (dirs.state_path / "log.jsonl").read_text()


def test_a_hook_that_ignores_sigterm_is_killed_after_the_grace_period(
    dirs: StateDirs, pids: Path, tmp_path: Path
) -> None:
    runner = make_runner(dirs)
    trapped = tmp_path / "trapped"
    cmd = f"trap 'echo term >> {trapped}' TERM; echo $$ >> {pids}; while :; do sleep 0.05; done"
    started = time.monotonic()
    supervisor = spawn(runner, cmd)
    assert supervisor.wait(60) == 0
    elapsed = time.monotonic() - started
    (hook_pid,) = recorded(pids)
    assert wait_dead(hook_pid, 10)
    # SIGKILL came after the grace period, not at once. Load can only add time, so there is no
    # upper bound; the supervisor's own 60 s wait above bounds a hang.
    assert TIMEOUT + GRACE * 0.9 <= elapsed
    assert trapped.read_text().split() == ["term"]  # SIGTERM came first, then SIGKILL
    assert [r["details"]["reason"] for r in log_lines(dirs)] == ["hook_timeout"]


def test_the_working_directory_is_removed_when_the_watchdog_ends_the_hook(
    dirs: StateDirs, pids: Path, tmp_path: Path
) -> None:
    runner = make_runner(dirs)
    seen = tmp_path / "cwd"
    supervisor = spawn(
        runner, f"echo $$ >> {pids}; pwd -P > {seen}; echo x > scratch.txt; sleep 60"
    )
    assert wait_file(seen)
    (entry,) = tmp_entries(dirs)  # the hook's directory exists while it runs
    assert Path(seen.read_text().strip()) == (dirs.tmp_path / entry).resolve()
    assert supervisor.wait(60) == 0
    assert tmp_entries(dirs) == []  # removed with what the hook left in it


def test_only_the_hooks_own_group_is_signaled(
    dirs: StateDirs, pids: Path, bystanders: list[subprocess.Popen[bytes]]
) -> None:
    other = start_bystander(bystanders)
    runner = make_runner(dirs)
    supervisor = spawn(runner, f"echo $$ >> {pids}; sleep 60")
    assert supervisor.wait(60) == 0
    assert other.poll() is None  # a process in another group and session survives
    (hook_pid,) = recorded(pids)
    assert wait_dead(hook_pid)


def test_a_hook_that_starts_a_new_session_escapes_the_group_kill(
    dirs: StateDirs, pids: Path
) -> None:
    """Documented limit: only the group the supervisor created is signaled."""
    runner = make_runner(dirs)
    cmd = f"echo $$ >> {pids}; setsid sh -c 'echo $$ >> {pids}; sleep 60' & sleep 60"
    supervisor = spawn(runner, cmd)
    assert supervisor.wait(60) == 0
    hook_pid, escaped = recorded(pids)
    assert wait_dead(hook_pid) and alive(escaped)  # killed by ``pids`` at teardown


# --- normal ends --------------------------------------------------------------------------------


def test_a_hook_that_ends_before_its_timeout_ends_the_watchdog_quietly(
    dirs: StateDirs, pids: Path, tmp_path: Path
) -> None:
    runner = make_runner(dirs)
    done = tmp_path / "done"
    started = time.monotonic()
    supervisor = spawn(
        runner, f"echo $$ >> {pids}; cat > {tmp_path}/got.json; echo ok > {done}", 30
    )
    assert supervisor.wait(60) == 0
    assert time.monotonic() - started < 30  # it did not wait for the 30 s timeout
    assert done.read_text() == "ok\n"
    assert json.loads((tmp_path / "got.json").read_text()) == PAYLOAD
    assert log_lines(dirs) == [] and tmp_entries(dirs) == []


def test_a_failing_hook_is_not_a_warning_the_watchdog_owns(dirs: StateDirs, pids: Path) -> None:
    runner = make_runner(dirs)
    supervisor = spawn(runner, f"echo $$ >> {pids}; exit 7", 30)
    assert supervisor.wait(60) == 0
    assert log_lines(dirs) == [] and tmp_entries(dirs) == []


def test_what_a_hook_left_running_in_its_group_ends_with_it(dirs: StateDirs, pids: Path) -> None:
    runner = make_runner(dirs)
    supervisor = spawn(runner, f"echo $$ >> {pids}; sleep 60 & echo $! >> {pids}; exit 0", 30)
    assert supervisor.wait(60) == 0
    hook_pid, child_pid = recorded(pids)
    assert wait_dead(hook_pid) and wait_dead(child_pid)
    assert log_lines(dirs) == []  # a normal end, not a timeout


def test_the_hook_gets_no_descriptor_of_the_state_directory(
    dirs: StateDirs, tmp_path: Path
) -> None:
    runner = make_runner(dirs)
    listing = tmp_path / "fds"
    cmd = f'for f in /proc/$$/fd/*; do readlink "$f"; done > {listing}'
    supervisor = spawn(runner, cmd, 30)
    assert supervisor.wait(60) == 0
    targets = listing.read_text().splitlines()
    assert targets  # the hook did list its descriptors
    assert not [t for t in targets if "private-home" in t and t != str(listing)]


def test_the_hook_gets_exactly_the_environment_it_is_given(dirs: StateDirs, tmp_path: Path) -> None:
    runner = HookRunner(
        HookSet(deny=("DENIED",)),
        dirs=dirs,
        environ={**BASE_ENV, "KEEP": "k", "DENIED": "d", "GH_TOKEN": "t", "PYTHONPATH": "/x"},
        log=LogWriter.disabled(),
        pr=PR,
        attempt_id=None,
    )
    seen = tmp_path / "env"
    entry = HookEntry(f"env > {seen}", 30, async_=True)
    assert runner.spawn_detached(POST, entry, PAYLOAD, 0)
    assert hooks_module._DETACHED[-1].wait(60) == 0  # pyright: ignore[reportPrivateUsage]
    env = dict(line.split("=", 1) for line in seen.read_text().splitlines())
    expected = hooks_module.hook_environment(
        {**BASE_ENV, "KEEP": "k", "PYTHONPATH": "/x"}, (), hook=POST, pr=PR
    )
    assert {k: v for k, v in env.items() if k not in ("PWD", "SHLVL", "_", "OLDPWD")} == expected


def test_a_hook_that_cannot_be_started_leaves_a_warning_and_no_directory(
    dirs: StateDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hooks_module, "SHELL", "/nonexistent/sh")
    runner = make_runner(dirs)
    supervisor = spawn(runner, "exit 0", 30)
    assert supervisor.wait(60) == 0
    assert [r["details"]["reason"] for r in log_lines(dirs)] == ["hook_not_started"]
    assert tmp_entries(dirs) == []


def test_without_a_log_the_watchdog_writes_nothing(dirs: StateDirs, pids: Path) -> None:
    runner = make_runner(dirs, log=False)
    supervisor = spawn(runner, f"echo $$ >> {pids}; sleep 60")
    assert supervisor.wait(60) == 0
    assert log_lines(dirs) == [] and tmp_entries(dirs) == []


# --- the main process and the watchdog ending ---------------------------------------------------


def test_the_watchdog_outlives_the_main_process_and_still_enforces_the_timeout(
    dirs: StateDirs, pids: Path, tmp_path: Path
) -> None:
    script = tmp_path / "main.py"
    script.write_text(
        textwrap.dedent(
            f"""
            from pr_comment.config import HookEntry
            from pr_comment.files import open_state_dirs
            from pr_comment.hooks import HookRunner, HookSet
            from pr_comment.jsonlog import LogWriter

            with open_state_dirs() as dirs:
                runner = HookRunner(HookSet(), dirs=dirs, environ={dict(BASE_ENV)!r},
                                    log=LogWriter.open(dirs), pr={PR!r}, attempt_id=None,
                                    term_grace={GRACE})
                entry = HookEntry({f"echo $$ >> {pids}; sleep 60"!r}, 5.0, async_=True)
                assert runner.spawn_detached("post-post", entry, {{}}, 0)
            """
        )
    )
    env = {**os.environ}  # HOME and XDG_* are the private ones (conftest)
    done = subprocess.run([sys.executable, "-I", str(script)], env=env, check=False, timeout=30)
    assert done.returncode == 0
    assert wait_file(pids)
    (hook_pid,) = recorded(pids)
    assert alive(hook_pid)  # the main process is gone, the hook still runs
    assert wait_dead(hook_pid, 10)  # and the timeout still ends it
    end = time.monotonic() + 10  # the watchdog logs and cleans up just after the hook is gone
    while time.monotonic() < end and (tmp_entries(dirs) or not log_lines(dirs)):
        time.sleep(0.02)
    assert tmp_entries(dirs) == []
    assert [r["details"]["reason"] for r in log_lines(dirs)] == ["hook_timeout"]


def test_sigterm_to_the_watchdog_ends_the_hook_and_removes_the_directory(
    dirs: StateDirs, pids: Path
) -> None:
    runner = make_runner(dirs)
    supervisor = spawn(runner, f"echo $$ >> {pids}; sleep 60 & echo $! >> {pids}; wait", 60)
    assert wait_file(pids) and len(recorded(pids)) >= 1
    deadline = time.monotonic() + 5
    while len(recorded(pids)) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    supervisor.send_signal(signal.SIGTERM)  # an exact PID: our own Popen
    assert supervisor.wait(60) is not None
    hook_pid, child_pid = recorded(pids)
    assert wait_dead(hook_pid) and wait_dead(child_pid)
    assert tmp_entries(dirs) == []
    assert [r["details"]["reason"] for r in log_lines(dirs)] == ["hook_interrupted"]


def test_a_signal_to_the_watchdog_escalates_a_hook_that_traps_sigterm(
    dirs: StateDirs, pids: Path, tmp_path: Path
) -> None:
    runner = make_runner(dirs)
    trapped = tmp_path / "trapped"
    cmd = f"trap 'echo term >> {trapped}' TERM; echo $$ >> {pids}; while :; do sleep 0.05; done"
    supervisor = spawn(runner, cmd, 60)
    assert wait_file(pids)
    supervisor.send_signal(signal.SIGTERM)  # an exact PID: our own Popen
    assert supervisor.wait(60) == 0
    (hook_pid,) = recorded(pids)
    assert wait_dead(hook_pid, 10)
    assert set(trapped.read_text().split()) == {"term"}  # trapped (forwarded, then group), killed
    assert tmp_entries(dirs) == []
    assert [r["details"]["reason"] for r in log_lines(dirs)] == ["hook_interrupted"]


def test_a_killed_watchdog_leaves_the_hook_to_its_own_end(dirs: StateDirs, pids: Path) -> None:
    """SIGKILL gives the watchdog no chance to act: the hook runs on, as it did before the
    watchdog existed (documented); the directory is then swept by age as for any hook."""
    runner = make_runner(dirs)
    supervisor = spawn(runner, f"echo $$ >> {pids}; sleep 60")
    assert wait_file(pids)
    supervisor.kill()
    supervisor.wait(5)
    (hook_pid,) = recorded(pids)
    time.sleep(TIMEOUT + 1.0)  # past the timeout the dead watchdog would have enforced
    assert alive(hook_pid)  # torn down by ``pids``
    assert len(tmp_entries(dirs)) == 1


def test_finished_supervisors_are_reaped_not_left_as_zombies(dirs: StateDirs) -> None:
    runner = make_runner(dirs)
    first = spawn(runner, "exit 0", 30)
    assert first.wait(60) == 0
    spawn(runner, "exit 0", 30)  # starting another prunes the finished one
    assert first not in hooks_module._DETACHED  # pyright: ignore[reportPrivateUsage]


# --- the guards, in process ---------------------------------------------------------------------


def workdir_job(dirs: StateDirs, cmd: str, timeout: float, **kw: Any) -> Any:
    """A job for ``watchdog.supervise`` run in this process, with a real hook directory."""
    from pr_comment import watchdog

    workdir = create_hook_dir(dirs.tmp_fd, dirs.tmp_path)
    return watchdog.job_for(
        dirs,
        shell="/bin/sh",
        cmd=cmd,
        timeout=timeout,
        term_grace=kw.get("term_grace", GRACE),
        env=dict(BASE_ENV),
        hook=POST,
        index=0,
        pr=PR,
        attempt_id=None,
        log=False,
        dir_fd=workdir.fd,
        dir_name=workdir.name,
    )


def test_a_pid_whose_start_time_changed_is_never_signaled(
    dirs: StateDirs, pids: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recycled-PID guard: when the leader's start time no longer matches, nothing is sent."""
    from pr_comment import gateway, watchdog

    signaled: list[tuple[str, int, int]] = []
    real_kill, real_killpg = os.kill, os.killpg
    monkeypatch.setattr(os, "kill", lambda p, s: (signaled.append(("kill", p, s)), real_kill(p, s)))
    monkeypatch.setattr(
        os, "killpg", lambda p, s: (signaled.append(("killpg", p, s)), real_killpg(p, s))
    )
    calls = {"n": 0}
    real_start = gateway._proc_start  # pyright: ignore[reportPrivateUsage]

    def changing(pid: int) -> str | None:
        calls["n"] += 1
        return real_start(pid) if calls["n"] == 1 else "recycled"

    monkeypatch.setattr(gateway, "_proc_start", changing)
    done = tmp_path / "done"
    job = workdir_job(dirs, f"echo $$ >> {pids}; sleep 2; echo ok > {done}", 0.3, term_grace=0.2)
    watchdog.supervise(job, b"{}", LogWriter.disabled())
    assert signaled == []  # the hook ran to its own end
    assert done.read_text() == "ok\n"


def test_a_normal_end_signals_only_the_hooks_own_group_and_never_waits_for_the_timeout(
    dirs: StateDirs, pids: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pr_comment import watchdog

    sent: list[tuple[int, int, bool]] = []
    real_killpg = os.killpg

    def recording(pgid: int, sig: int) -> None:
        # ``/proc/<pid>`` exists while the leader is running or a zombie we have not reaped.
        sent.append((pgid, sig, Path(f"/proc/{pgid}").exists()))
        real_killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", recording)
    job = workdir_job(dirs, f"echo $$ >> {pids}; exit 0", 30)
    started = time.monotonic()
    watchdog.supervise(job, b"{}", LogWriter.disabled())
    assert time.monotonic() - started < 30
    (hook_pid,) = recorded(pids)
    assert {pid for pid, _, _ in sent} <= {hook_pid}  # never another group, nor the supervisor's
    assert all(sig == signal.SIGKILL for _, sig, _ in sent)  # after a normal end: leftovers only
    assert all(unreaped for _, _, unreaped in sent)  # never after the leader was reaped


# --- the job document ---------------------------------------------------------------------------


def test_the_job_document_round_trips_and_hostile_input_is_refused(dirs: StateDirs) -> None:
    from pr_comment import watchdog

    job = watchdog.job_for(
        dirs,
        shell="/bin/sh",
        cmd="echo é \ud800 'x'\n",
        timeout=1.5,
        term_grace=0.2,
        env={"A": "b\nc", "U": "é"},
        hook=POST,
        index=2,
        pr=PR,
        attempt_id="x" * 22,
        log=True,
        dir_fd=9,
        dir_name="prc-hook-" + "a" * 32,
    )
    data = watchdog.encode_job(job, b'{"payload": true}')
    assert data.count(b"\n") >= 1 and data.isascii()
    parsed = watchdog.parse_job(data)
    assert parsed == (job, b'{"payload": true}')
    for bad in (
        b"",
        b"not json\n",
        b"[]\n",
        b"{}\n",
        data.replace(b'"timeout": 1.5', b'"timeout": -1'),
        data.replace(b'"timeout": 1.5', b'"timeout": "1"'),
        data.replace(b'"timeout": 1.5', b'"timeout": NaN'),
        data.replace(b"prc-hook-" + b"a" * 32, b"../../etc"),
        data.replace(b'"dir_fd": 9', b'"dir_fd": true'),
    ):
        assert watchdog.parse_job(bad) is None, bad


def test_the_hook_environment_cannot_leak_into_the_command_line(
    dirs: StateDirs, pids: Path
) -> None:
    """Untrusted text travels in a file, never argv or env of the supervisor."""
    runner = make_runner(dirs)
    secret = "payload-text-that-must-stay-out-of-argv"
    entry = HookEntry(f"echo $$ >> {pids}; sleep 60", 5, async_=True)
    payload: Mapping[str, Any] = {"bodies": [{"text": secret}]}
    assert runner.spawn_detached(POST, entry, payload, 0)
    supervisor = hooks_module._DETACHED[-1]  # pyright: ignore[reportPrivateUsage]
    assert wait_file(pids)
    argv = Path(f"/proc/{supervisor.pid}/cmdline").read_bytes()
    environ = Path(f"/proc/{supervisor.pid}/environ").read_bytes()
    assert secret.encode() not in argv and secret.encode() not in environ
    assert b"sleep" not in argv  # the command is in the file as well
    supervisor.send_signal(signal.SIGTERM)
    supervisor.wait(60)


def test_the_supervisor_leads_its_own_session_and_group(dirs: StateDirs, pids: Path) -> None:
    runner = make_runner(dirs)
    supervisor = spawn(runner, f"echo $$ >> {pids}; sleep 60", 5)
    assert wait_file(pids)
    assert os.getsid(supervisor.pid) == supervisor.pid
    assert os.getpgid(supervisor.pid) == supervisor.pid
    assert os.getsid(supervisor.pid) != os.getsid(0) and os.getpgid(supervisor.pid) != os.getpgrp()
    supervisor.send_signal(signal.SIGTERM)
    supervisor.wait(60)


def test_the_hook_environment_stays_out_of_the_supervisors_environment(
    dirs: StateDirs, pids: Path
) -> None:
    name, value = "WD_SEEDED_VARIABLE_NAME", "wd-seeded-variable-value"
    runner = HookRunner(
        HookSet(),
        dirs=dirs,
        environ={**BASE_ENV, name: value},
        log=LogWriter.disabled(),
        pr=PR,
        attempt_id=None,
    )
    entry = HookEntry(f"echo $$ >> {pids}; sleep 60", 5, async_=True)
    assert runner.spawn_detached(POST, entry, PAYLOAD, 0)
    supervisor = hooks_module._DETACHED[-1]  # pyright: ignore[reportPrivateUsage]
    assert wait_file(pids)
    environ = Path(f"/proc/{supervisor.pid}/environ").read_bytes()
    assert name.encode() not in environ, "a hook variable name is in the supervisor environment"
    assert value.encode() not in environ, "a hook variable value is in the supervisor environment"
    supervisor.send_signal(signal.SIGTERM)
    supervisor.wait(60)


# --- what a hook leaves behind -----------------------------------------------------------------

QUIET_LEFTOVER = "sleep 60 >/dev/null 2>&1 & echo $! >> {pids}"


def test_a_leftover_with_its_output_redirected_is_killed_at_a_normal_end(
    dirs: StateDirs, pids: Path
) -> None:
    runner = make_runner(dirs)
    cmd = f"echo $$ >> {pids}; {QUIET_LEFTOVER.format(pids=pids)}; exit 0"
    supervisor = spawn(runner, cmd, 30)
    assert supervisor.wait(60) == 0
    hook_pid, child_pid = recorded(pids)
    assert wait_dead(hook_pid) and wait_dead(child_pid, 2)


def test_a_leftover_with_its_output_redirected_is_killed_at_the_timeout(
    dirs: StateDirs, pids: Path
) -> None:
    runner = make_runner(dirs)
    cmd = f"echo $$ >> {pids}; {QUIET_LEFTOVER.format(pids=pids)}; wait"
    supervisor = spawn(runner, cmd)
    assert supervisor.wait(60) == 0
    hook_pid, child_pid = recorded(pids)
    assert wait_dead(hook_pid) and wait_dead(child_pid, 2)


# --- the supervisor's own failures --------------------------------------------------------------


def test_a_directory_that_cannot_be_removed_is_a_warning(
    dirs: StateDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pr_comment import watchdog

    def refuse(self: HookDir, tmp_fd: int) -> None:
        os.close(self.fd)
        raise StateUnsafeError("refused", {"path": str(self.path)})

    monkeypatch.setattr(HookDir, "remove", refuse)
    job = workdir_job(dirs, "exit 0", 30)
    log = LogWriter.open(dirs)
    watchdog.supervise(job, b"{}", log)
    log.close()
    assert [r["details"]["reason"] for r in log_lines(dirs)] == ["hook_directory_not_removed"]


def test_signals_are_ignored_while_the_directory_is_removed(
    dirs: StateDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pr_comment import watchdog

    seen: dict[int, Any] = {}
    real_remove = HookDir.remove

    def spy(self: HookDir, tmp_fd: int) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            seen[sig] = signal.getsignal(sig)
        real_remove(self, tmp_fd)

    monkeypatch.setattr(HookDir, "remove", spy)
    watchdog.supervise(workdir_job(dirs, "exit 0", 30), b"{}", LogWriter.disabled())
    assert set(seen.values()) == {signal.SIG_IGN} and len(seen) == 3
    assert tmp_entries(dirs) == []


def run_watchdog(data: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-I", "-m", "pr_comment.watchdog"],
        input=data,
        env={**os.environ},
        capture_output=True,
        check=False,
        timeout=30,
    )


def test_a_bad_job_on_stdin_ends_the_watchdog_with_exit_2(dirs: StateDirs) -> None:
    done = run_watchdog(b"garbage, no newline")
    assert done.returncode == 2
    assert done.stderr == b""


def plain_job_bytes(dirs: StateDirs) -> bytes:
    from pr_comment import watchdog

    job = watchdog.job_for(
        dirs,
        shell="/bin/sh",
        cmd="exit 0",
        timeout=1.5,
        term_grace=0.2,
        env={},
        hook=POST,
        index=2,
        pr=PR,
        attempt_id=None,
        log=False,
        dir_fd=12,
        dir_name="prc-hook-" + "a" * 32,
        state_fd=10,
        tmp_fd=11,
    )
    return watchdog.encode_job(job, b"")


def test_out_of_range_numbers_end_the_watchdog_with_exit_2_and_no_traceback(
    dirs: StateDirs,
) -> None:
    data = plain_job_bytes(dirs)
    huge = b"1" + b"0" * 400
    for old, new in (
        (b'"timeout": 1.5', b'"timeout": ' + huge),
        (b'"term_grace": 0.2', b'"term_grace": ' + huge),
        (b'"dir_fd": 12', b'"dir_fd": ' + huge),
        (b'"state_fd": 10', b'"state_fd": ' + huge),
    ):
        assert old in data
        done = run_watchdog(data.replace(old, new))
        assert done.returncode == 2, old
        assert done.stderr == b"", old


def test_an_overflow_inside_the_supervisor_ends_main_with_exit_2(
    dirs: StateDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pr_comment import watchdog

    def overflow(*_a: object, **_k: object) -> None:
        raise OverflowError("too large")

    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(plain_job_bytes(dirs))))
    monkeypatch.setattr(watchdog, "supervise", overflow)
    assert watchdog.main() == 2


# --- platforms without waitid -------------------------------------------------------------------


def test_without_waitid_a_timeout_still_ends_the_hook_and_removes_the_directory(
    dirs: StateDirs, pids: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pr_comment import gateway, watchdog

    monkeypatch.delattr(os, "waitid")
    monkeypatch.setattr(gateway, "_proc_start", lambda pid: None)
    sent: list[tuple[int, int]] = []
    real_killpg = os.killpg
    monkeypatch.setattr(os, "killpg", lambda p, s: (sent.append((p, s)), real_killpg(p, s)))
    job = workdir_job(dirs, f"echo $$ >> {pids}; sleep 60", TIMEOUT)
    log = LogWriter.open(dirs)
    watchdog.supervise(job, b"{}", log)
    log.close()
    (hook_pid,) = recorded(pids)
    assert wait_dead(hook_pid)
    assert sent and {pgid for pgid, _ in sent} == {hook_pid}  # only the hook's group, none after
    assert sent[0][1] == signal.SIGTERM  # the leader was reaped by the poll: no SIGKILL followed
    assert [r["details"]["reason"] for r in log_lines(dirs)] == ["hook_timeout"]
    assert tmp_entries(dirs) == []


def test_without_waitid_a_normal_end_does_not_kill_what_the_hook_left(
    dirs: StateDirs, pids: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the unverified fallback (todo #40): the poll reaps the leader, so none is signaled."""
    from pr_comment import gateway, watchdog

    monkeypatch.delattr(os, "waitid")
    monkeypatch.setattr(gateway, "_proc_start", lambda pid: None)
    sent: list[tuple[int, int]] = []
    real_killpg = os.killpg
    monkeypatch.setattr(os, "killpg", lambda p, s: (sent.append((p, s)), real_killpg(p, s)))
    cmd = f"echo $$ >> {pids}; {QUIET_LEFTOVER.format(pids=pids)}; exit 0"
    watchdog.supervise(workdir_job(dirs, cmd, 30), b"{}", LogWriter.disabled())
    hook_pid, child_pid = recorded(pids)
    assert sent == []
    assert alive(child_pid)  # killed by ``pids`` at teardown
    assert not alive(hook_pid) and tmp_entries(dirs) == []


# --- descriptors and size limits ----------------------------------------------------------------


def test_a_run_with_its_standard_descriptors_closed_still_runs_the_hook(
    dirs: StateDirs, pids: Path, tmp_path: Path
) -> None:
    """The state directories then sit on descriptors 0 to 2, which the supervisor's own standard
    streams replace; the job must carry duplicates."""
    done = tmp_path / "done"
    script = tmp_path / "closed.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import os
            from pr_comment.config import HookEntry
            from pr_comment.files import open_state_dirs
            from pr_comment.hooks import _DETACHED, HookRunner, HookSet
            from pr_comment.jsonlog import LogWriter

            for fd in (0, 1, 2):
                os.close(fd)
            with open_state_dirs() as dirs:
                runner = HookRunner(HookSet(), dirs=dirs, environ={dict(BASE_ENV)!r},
                                    log=LogWriter.disabled(), pr={PR!r}, attempt_id=None)
                entry = HookEntry({f"echo ok > {done}"!r}, 30, async_=True)
                ok = runner.spawn_detached("post-post", entry, {{}}, 0)
                with open({str(pids)!r}, "a") as out:
                    out.write(str(_DETACHED[-1].pid) + " ")
            os._exit(0 if ok else 3)
            """
        )
    )
    ran = subprocess.run(
        [sys.executable, "-I", str(script)], env={**os.environ}, check=False, timeout=30
    )
    assert ran.returncode == 0
    assert wait_file(done) and done.read_text() == "ok\n"
    end = time.monotonic() + 10
    while time.monotonic() < end and tmp_entries(dirs):
        time.sleep(0.02)
    assert tmp_entries(dirs) == []


def test_a_job_over_the_size_limit_is_refused_and_leaves_no_directory(
    dirs: StateDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pr_comment import watchdog

    monkeypatch.setattr(watchdog, "MAX_JOB_BYTES", 100)
    runner = make_runner(dirs)
    before = len(hooks_module._DETACHED)  # pyright: ignore[reportPrivateUsage]
    entry = HookEntry("exit 0", 30, async_=True)
    assert runner.spawn_detached(POST, entry, PAYLOAD, 0) is False
    assert len(hooks_module._DETACHED) == before  # pyright: ignore[reportPrivateUsage]
    assert tmp_entries(dirs) == []


def test_parse_job_refuses_unusable_numbers_and_descriptors(dirs: StateDirs) -> None:
    from pr_comment import watchdog

    data = plain_job_bytes(dirs)
    assert watchdog.parse_job(data) is not None
    huge = b"1" + b"0" * 400
    for old, new in (
        (b'"timeout": 1.5', b'"timeout": true'),
        (b'"term_grace": 0.2', b'"term_grace": true'),
        (b'"timeout": 1.5', b'"timeout": ' + huge),
        (b'"term_grace": 0.2', b'"term_grace": ' + huge),
        (b'"timeout": 1.5', b'"timeout": 1e999'),
        (b'"dir_fd": 12', b'"dir_fd": -1'),
        (b'"dir_fd": 12', b'"dir_fd": 0'),
        (b'"dir_fd": 12', b'"dir_fd": 1'),
        (b'"dir_fd": 12', b'"dir_fd": 2'),
        (b'"state_fd": 10', b'"state_fd": 2'),
        (b'"tmp_fd": 11', b'"tmp_fd": 0'),
        (b'"dir_fd": 12', b'"dir_fd": 1000000000000'),
        (b'"dir_fd": 12', b'"dir_fd": 65536'),
        (b'"dir_fd": 12', b'"dir_fd": ' + huge),
        (b'"tmp_fd": 11', b'"tmp_fd": 10'),  # the same descriptor twice
        (b'"dir_fd": 12', b'"dir_fd": 11'),
        (b'"v": 1', b'"v": 2'),
    ):
        assert old in data, old
        assert watchdog.parse_job(data.replace(old, new)) is None, new[:40]
