"""The hook runner (``pr_comment.hooks``, issue #33) against real tiny ``sh`` scripts.

Every test runs with a private HOME and state directory (``conftest.private_home``). A hook gets
an environment this test builds (``PATH=/usr/bin:/bin``), never the developer's. Each process a
hook starts records its PID in a file; ``killed_at_teardown`` signals exactly those PIDs if a test
leaves one alive (never by pattern). No forgectl is involved.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from pr_comment import hooks as hooks_module
from pr_comment.config import HookEntry
from pr_comment.files import StateDirs, create_file_exclusive, open_state_dirs
from pr_comment.gateway import ForgectlInterrupted
from pr_comment.hooks import (
    POST,
    PRE,
    DecisionIds,
    HookRunner,
    HookSet,
    hook_environment,
    parse_decision_ids,
    run_post,
    run_pre,
)
from pr_comment.jsonlog import LogWriter
from tests.forgectl_harness import REAL_PATH

BASE_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
PR = "example.test/o/r#7"


def _stat_fields(pid: int) -> list[str] | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return text.rpartition(")")[2].split()


def _state(pid: int) -> str | None:
    fields = _stat_fields(pid)
    return None if fields is None else fields[0]


def start_ticks(pid: int) -> int | None:
    """The start time of ``pid`` (field 22 of ``/proc/<pid>/stat``, clock ticks since boot)."""
    fields = _stat_fields(pid)
    return int(fields[19]) if fields is not None and len(fields) > 19 else None


def start_mark() -> int:
    """Now, in the unit of ``start_ticks``: a process that started later is one this test made."""
    uptime = float(Path("/proc/uptime").read_text().split()[0])
    return int(uptime * os.sysconf("SC_CLK_TCK"))


def alive(pid: int) -> bool:
    """A process that exists and is not a zombie."""
    return _state(pid) not in (None, "Z", "X")


def wait_dead(pid: int, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.02)
    return not alive(pid)


def wait_file(path: Path, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists() and path.read_text().strip():
            return True
        time.sleep(0.02)
    return False


def kill_recorded(path: Path, mark: int) -> None:
    """SIGKILL each PID in ``path`` that is still alive and started after ``mark`` (so a PID that
    went to an unrelated, older process is never signaled): exact PIDs, never a pattern."""
    for word in path.read_text().split():
        if not word.isdigit():
            continue
        pid = int(word)
        started = start_ticks(pid)
        if started is not None and started >= mark - 1 and alive(pid):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


@pytest.fixture
def pids(tmp_path: Path) -> Iterator[Path]:
    """A file hooks append their PIDs to (``echo $$ >> $PIDS``); after the test every recorded PID
    that is still alive and started after the test began gets SIGKILL."""
    path = tmp_path / "pids"
    path.touch()
    mark = start_mark()
    yield path
    kill_recorded(path, mark)


@contextlib.contextmanager
def signal_self_when_started(
    marker: Path, words: int, signum: int, seconds: float = 30.0
) -> Iterator[None]:
    """Send ``signum`` to this process once ``marker`` holds ``words`` words (the hook has started
    and said so), never on a timer. Gives up after ``seconds`` (the test then fails on its own)."""
    stop = threading.Event()

    def watch() -> None:
        end = time.monotonic() + seconds
        while not stop.is_set() and time.monotonic() < end:
            with contextlib.suppress(OSError):
                if len(marker.read_text().split()) >= words:
                    os.kill(os.getpid(), signum)
                    return
            stop.wait(0.01)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(5)


@pytest.fixture
def dirs() -> Iterator[StateDirs]:
    with open_state_dirs() as opened:
        yield opened


def runner(
    dirs: StateDirs,
    *,
    pre: Sequence[HookEntry] = (),
    post: Sequence[HookEntry] = (),
    environ: Mapping[str, str] | None = None,
    deny: Sequence[str] = (),
    term_grace: float = 0.3,
    interrupt_grace: float = 0.3,
) -> HookRunner:
    return HookRunner(
        HookSet(tuple(pre), tuple(post), tuple(deny)),
        dirs=dirs,
        environ={**BASE_ENV, **(environ or {})},
        log=LogWriter.disabled(),
        pr=PR,
        attempt_id="a" * 22,
        term_grace=term_grace,
        interrupt_grace=interrupt_grace,
    )


def entry(cmd: str, timeout: float = 10.0, **kw: Any) -> HookEntry:
    return HookEntry(cmd, timeout, **kw)


PAYLOAD: dict[str, Any] = {"tool": "pr-comment", "hook": "pre-post", "decision_ids": []}


def run_one(r: HookRunner, e: HookEntry, hook: str = PRE) -> Any:
    return r.run_sync(hook, 0, e, PAYLOAD)


def tmp_entries(dirs: StateDirs) -> list[str]:
    return sorted(os.listdir(dirs.tmp_path))


# --- exit codes -------------------------------------------------------------------------------


@pytest.mark.usefixtures("default_signals")  # ``kill -TERM $$`` needs TERM not inherited ignored
@pytest.mark.parametrize(
    ("cmd", "verdict", "reason"),
    [
        ("exit 0", "ok", "exit_0"),
        ("exit 13", "escalate", "exit_13"),
        ("exit 1", "fail", "exit_1"),
        ("exit 11", "fail", "exit_11"),
        ("exit 255", "fail", "exit_255"),
        ("kill -9 $$", "fail", "signal_9"),
        ("kill -TERM $$", "fail", "signal_15"),
        ("exit 128", "fail", "exit_128"),
    ],
)
def test_exit_status_maps_to_a_verdict(
    dirs: StateDirs, cmd: str, verdict: str, reason: str
) -> None:
    result = run_one(runner(dirs), entry(cmd))
    assert (result.verdict, result.reason) == (verdict, reason)


def test_a_command_that_cannot_be_passed_to_the_shell_vetoes_instead_of_raising(
    dirs: StateDirs,
) -> None:
    r = runner(dirs, pre=[entry("exit 0\0")])
    verdict = run_pre(r, DecisionIds(()), lambda e, d: PAYLOAD)
    assert verdict.vetoed is not None and verdict.vetoed.reason == "not_started"
    assert tmp_entries(dirs) == []


def test_a_hook_that_cannot_be_found_is_not_ok(dirs: StateDirs) -> None:
    result = run_one(runner(dirs), entry("/nonexistent/hook-binary"))
    assert result.verdict == "fail" and result.exit_code == 127


def test_without_a_state_directory_a_hook_does_not_run() -> None:
    r = HookRunner(
        HookSet(pre=(entry("exit 0"),)),
        dirs=None,
        environ=BASE_ENV,
        log=LogWriter.disabled(),
        pr=PR,
        attempt_id=None,
    )
    assert run_one(r, entry("exit 0")).verdict == "fail"


# --- timeout, process group and signals -------------------------------------------------------


def test_a_timeout_is_a_failure_and_ends_the_whole_group(dirs: StateDirs, pids: Path) -> None:
    cmd = f"echo $$ >> {pids}; sleep 60 & echo $! >> {pids}; wait"
    started = time.monotonic()
    result = run_one(runner(dirs), entry(cmd, timeout=0.3))
    assert time.monotonic() - started < 10
    assert (result.verdict, result.reason, result.timed_out) == ("fail", "timeout", True)
    recorded = [int(x) for x in pids.read_text().split()]
    assert len(recorded) == 2
    assert all(wait_dead(p) for p in recorded)


def test_a_hook_that_ignores_sigterm_is_killed_after_the_grace_period(
    dirs: StateDirs, pids: Path
) -> None:
    cmd = f"trap '' TERM; echo $$ >> {pids}; sleep 60 & echo $! >> {pids}; wait; sleep 60"
    started = time.monotonic()
    result = run_one(runner(dirs, term_grace=0.3), entry(cmd, timeout=0.3))
    elapsed = time.monotonic() - started
    assert result.timed_out and 0.5 <= elapsed < 10
    assert all(wait_dead(int(p)) for p in pids.read_text().split())


def test_a_background_child_does_not_outlive_a_hook_that_exits(dirs: StateDirs, pids: Path) -> None:
    cmd = f"sleep 60 >/dev/null 2>&1 & echo $! >> {pids}; exit 0"
    result = run_one(runner(dirs), entry(cmd))
    assert result.verdict == "ok"
    (child,) = (int(x) for x in pids.read_text().split())
    assert wait_dead(child)


def test_a_child_holding_the_pipes_open_is_cut_off(dirs: StateDirs, pids: Path) -> None:
    cmd = f"sleep 60 & echo $! >> {pids}; echo done; exit 0"
    started = time.monotonic()
    result = run_one(runner(dirs), entry(cmd))
    assert time.monotonic() - started < 10
    assert result.verdict == "ok"
    assert wait_dead(int(pids.read_text().split()[0]))


def test_the_hook_runs_in_its_own_process_group_and_session(
    dirs: StateDirs, tmp_path: Path
) -> None:
    out = tmp_path / "ids"
    cmd = f"ps -o pid=,pgid=,sid= -p $$ > {out}"
    assert run_one(runner(dirs), entry(cmd)).verdict == "ok"
    pid, pgid, sid = (int(x) for x in out.read_text().split())
    assert pid == pgid == sid
    assert pgid != os.getpgrp() and sid != os.getsid(0)


@pytest.mark.usefixtures("default_signals")
def test_a_signal_to_pr_comment_stops_the_hook_and_raises(dirs: StateDirs, pids: Path) -> None:
    cmd = f"echo $$ >> {pids}; sleep 60 & echo $! >> {pids}; wait"
    with signal_self_when_started(pids, 2, signal.SIGTERM):
        with pytest.raises(ForgectlInterrupted) as caught:
            run_one(runner(dirs), entry(cmd, timeout=30))
    assert caught.value.signum == signal.SIGTERM
    assert all(wait_dead(int(p)) for p in pids.read_text().split())
    assert tmp_entries(dirs) == []


@pytest.mark.usefixtures("default_signals")  # a hook inherits an ignored TERM, which it cannot trap
def test_a_timeout_sends_sigterm_to_the_whole_group_before_the_kill(
    dirs: StateDirs, tmp_path: Path, pids: Path
) -> None:
    # The leader ignores TERM and only a background child traps it: the child's file proves the
    # TERM reached the group (not the leader alone) before the SIGKILL that follows the grace.
    termed = tmp_path / "termed"
    ready = tmp_path / "ready"
    child = (
        f"trap 'echo termed > {termed}; exit 0' TERM; echo $$ >> {pids}; echo up > {ready}; "
        "while :; do sleep 0.05; done"
    )
    # (the child is started before the leader's trap: an ignored signal cannot be trapped)
    cmd = (
        f'echo $$ >> {pids}; sh -c "{child}" & '
        f"while [ ! -s {ready} ]; do sleep 0.02; done; trap '' TERM; "
        "while :; do sleep 0.05; done"
    )
    result = run_one(runner(dirs, term_grace=1.5), entry(cmd, timeout=1.0))
    assert result.timed_out
    assert termed.read_text().strip() == "termed"
    assert all(wait_dead(int(p)) for p in pids.read_text().split())


def test_a_timeout_never_touches_a_process_of_another_session(
    dirs: StateDirs, tmp_path: Path
) -> None:
    # the autouse private_home fixture empties os.environ PATH, so name the binary in full
    sleep = shutil.which("sleep", path=REAL_PATH)
    assert sleep is not None
    bystander = subprocess.Popen(
        [sleep, "60"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid, started = bystander.pid, start_ticks(bystander.pid)
    try:
        result = run_one(runner(dirs), entry("sleep 60", timeout=0.3))
        assert result.timed_out
        assert alive(pid) and start_ticks(pid) == started
        assert bystander.poll() is None
    finally:
        if start_ticks(pid) == started and bystander.poll() is None:
            bystander.kill()
        bystander.wait(10)


# --- input and output -------------------------------------------------------------------------


def test_stdin_is_the_payload_and_never_the_runs_own_stdin(dirs: StateDirs, tmp_path: Path) -> None:
    got = tmp_path / "in.json"
    link = tmp_path / "link"
    cmd = f"readlink /proc/$$/fd/0 > {link}; cat > {got}"
    result = run_one(runner(dirs), entry(cmd))
    assert result.verdict == "ok"
    assert json.loads(got.read_text()) == PAYLOAD
    assert link.read_text().startswith("pipe:")


def test_a_hook_that_never_reads_a_huge_payload_does_not_hang(dirs: StateDirs) -> None:
    big = {**PAYLOAD, "bodies": [{"text": "x" * (8 * 1024 * 1024)}]}
    started = time.monotonic()
    result = runner(dirs).run_sync(PRE, 0, entry("exit 0"), big)
    assert result.verdict == "ok" and time.monotonic() - started < 10


def test_a_hook_that_reads_part_of_its_stdin_and_exits_is_fine(dirs: StateDirs) -> None:
    big = {**PAYLOAD, "bodies": [{"text": "x" * (2 * 1024 * 1024)}]}
    result = runner(dirs).run_sync(PRE, 0, entry("head -c 10 >/dev/null"), big)
    assert result.verdict == "ok"


def test_stderr_is_cut_to_4096_bytes_and_binary_is_replaced(dirs: StateDirs) -> None:
    result = run_one(runner(dirs), entry("head -c 200000 /dev/zero | tr '\\0' 'e' >&2; exit 3"))
    assert result.verdict == "fail" and len(result.stderr.encode()) == 4096
    binary = run_one(runner(dirs), entry("printf '\\377\\376ab\\000' >&2; exit 4"))
    assert binary.stderr.startswith("��ab")


def test_a_multibyte_stderr_is_never_cut_inside_a_character(dirs: StateDirs) -> None:
    cmd = "i=0; while [ $i -lt 3000 ]; do printf '\\303\\251'; i=$((i+1)); done >&2; exit 3"
    result = run_one(runner(dirs), entry(cmd))
    assert len(result.stderr.encode()) <= 4096 and set(result.stderr) == {"é"}


def test_huge_stdout_is_bounded_and_its_ids_are_ignored(dirs: StateDirs) -> None:
    cmd = "echo '{\"decision_id\": \"x\"}'; head -c 1000000 /dev/zero | tr '\\0' ' '"
    result = run_one(runner(dirs), entry(cmd))
    assert result.verdict == "ok" and result.decision_ids == ()
    assert result.warnings == ("hook_stdout_too_large",)


def test_a_hook_cannot_flood_with_endless_output(dirs: StateDirs) -> None:
    started = time.monotonic()
    result = run_one(runner(dirs), entry("yes | head -c 300000000", timeout=30))
    assert time.monotonic() - started < 20 and result.verdict == "ok"
    assert result.warnings == ("hook_stdout_too_large",)


# --- decision ids -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stdout", "ids", "warnings"),
    [
        (b"", [], []),
        (b"  \n", [], []),
        (b'{"other": 1}', [], []),
        (b'{"decision_id": "d1"}', ["d1"], []),
        (b'{"decision_id": ["d1", "d2"]}', ["d1", "d2"], []),
        (
            b'{"decision_id": ["d1", "bad id", 5, null, "d2"]}',
            ["d1", "d2"],
            ["hook_decision_id_invalid"],
        ),
        (b'{"decision_id": "-- x"}', [], ["hook_decision_id_invalid"]),
        (b'{"decision_id": "' + b"a" * 129 + b'"}', [], ["hook_decision_id_invalid"]),
        (b'{"decision_id": "d1\\n"}', [], ["hook_decision_id_invalid"]),
        (b'{"decision_id": 7}', [], ["hook_stdout_invalid"]),
        (b'{"decision_id": {"a": 1}}', [], ["hook_stdout_invalid"]),
        (b"not json", [], ["hook_stdout_invalid"]),
        (b"[1, 2]", [], ["hook_stdout_invalid"]),
        (b'{"decision_id": NaN}', [], ["hook_stdout_invalid"]),
        (b"\xff\xfe", [], ["hook_stdout_invalid"]),
        (b"[" * 100000, [], ["hook_stdout_invalid"]),
    ],
)
def test_decision_id_parsing(stdout: bytes, ids: list[str], warnings: list[str]) -> None:
    assert parse_decision_ids(stdout, False) == (ids, warnings)


def test_a_truncated_stdout_is_never_parsed() -> None:
    assert parse_decision_ids(b'{"decision_id": "d1"}', True) == ([], ["hook_stdout_too_large"])


def test_the_total_of_decision_ids_is_capped_at_32_and_extras_are_dropped() -> None:
    ids = DecisionIds(caller=tuple(f"c{i}" for i in range(30)))
    problems = ids.add([f"h{i}" for i in range(5)])
    assert len(ids.all()) == 32 and ids.from_hooks == ["h0", "h1"]
    assert problems == ["decision_id_limit"]
    assert ids.add(["c1", "h0"]) == []  # already present: kept once, silently


def test_a_hook_id_is_never_added_to_the_callers_ids() -> None:
    ids = DecisionIds(caller=("c",))
    ids.add(["h"])
    assert ids.caller == ("c",) and ids.all() == ["c", "h"]


def test_only_a_hook_that_exits_zero_contributes_ids(dirs: StateDirs) -> None:
    printing = 'echo \'{"decision_id": "d-%s"}\'; exit %s'
    pre = [
        entry(printing % ("esc", 13)),
        entry(printing % ("ok", 0)),
    ]
    ids = DecisionIds(caller=("c",))
    verdict = run_pre(runner(dirs, pre=pre), ids, lambda e, d: {**PAYLOAD, "decision_ids": d})
    assert [r.index for r in verdict.escalated] == [0] and verdict.vetoed is None
    assert ids.all() == ["c", "d-ok"]
    vetoer = [entry(printing % ("veto", 3))]
    ids2 = DecisionIds(caller=())
    run_pre(runner(dirs, pre=vetoer), ids2, lambda e, d: PAYLOAD)
    assert ids2.all() == []


def test_later_hooks_see_earlier_hooks_ids(dirs: StateDirs, tmp_path: Path) -> None:
    seen = tmp_path / "seen.json"
    pre = [entry('echo \'{"decision_id": "first"}\''), entry(f"cat > {seen}")]
    ids = DecisionIds(caller=("c",))
    run_pre(runner(dirs, pre=pre), ids, lambda e, d: {**PAYLOAD, "decision_ids": d})
    assert json.loads(seen.read_text())["decision_ids"] == ["c", "first"]


# --- pre-post ordering ------------------------------------------------------------------------


def trail(dirs: StateDirs, tmp_path: Path, codes: Sequence[int]) -> tuple[Any, list[str]]:
    marker = tmp_path / "order"
    pre = [entry(f"echo {i} >> {marker}; exit {c}") for i, c in enumerate(codes)]
    verdict = run_pre(runner(dirs, pre=pre), DecisionIds(()), lambda e, d: PAYLOAD)
    ran = marker.read_text().split() if marker.exists() else []
    return verdict, ran


def test_a_veto_stops_the_later_hooks(dirs: StateDirs, tmp_path: Path) -> None:
    verdict, ran = trail(dirs, tmp_path, [0, 3, 0])
    assert ran == ["0", "1"] and verdict.vetoed is not None and verdict.vetoed.index == 1


def test_an_escalation_lets_the_rest_run_and_a_later_veto_wins(
    dirs: StateDirs, tmp_path: Path
) -> None:
    verdict, ran = trail(dirs, tmp_path, [13, 0, 13, 5, 0])
    assert ran == ["0", "1", "2", "3"]
    assert verdict.vetoed is not None and verdict.vetoed.index == 3
    assert [r.index for r in verdict.escalated] == [0, 2]


def test_several_escalations_are_all_reported_first_first(dirs: StateDirs, tmp_path: Path) -> None:
    verdict, ran = trail(dirs, tmp_path, [13, 13, 0])
    assert ran == ["0", "1", "2"] and verdict.vetoed is None
    assert [r.index for r in verdict.escalated] == [0, 1]


def test_a_timeout_in_pre_post_is_a_veto(dirs: StateDirs) -> None:
    r = runner(dirs, pre=[entry("sleep 30", timeout=0.2)])
    verdict = run_pre(r, DecisionIds(()), lambda e, d: PAYLOAD)
    assert verdict.vetoed is not None and verdict.vetoed.timed_out


def test_a_pre_post_with_no_hooks_allows(dirs: StateDirs) -> None:
    assert run_pre(runner(dirs), DecisionIds(()), lambda e, d: PAYLOAD).vetoed is None


# --- post-post --------------------------------------------------------------------------------


def test_post_post_failures_only_warn_and_every_hook_runs(dirs: StateDirs, tmp_path: Path) -> None:
    marker = tmp_path / "order"
    post = [
        entry(f"echo 0 >> {marker}; exit 9"),
        entry(f"echo 1 >> {marker}; kill -9 $$"),
        entry(f"echo 2 >> {marker}; sleep 30", timeout=0.2),
        entry(f'echo 3 >> {marker}; echo \'{{"decision_id": "late"}}\''),
    ]
    ids = DecisionIds(("c",))
    run_post(runner(dirs, post=post), ids, lambda e, d: {**PAYLOAD, "hook": POST})
    assert marker.read_text().split() == ["0", "1", "2", "3"]
    assert ids.from_hooks == ["late"]


@pytest.mark.usefixtures("default_signals")
def test_post_post_stops_starting_hooks_after_a_signal(dirs: StateDirs, tmp_path: Path) -> None:
    marker = tmp_path / "order"
    post = [entry(f"echo 0 >> {marker}; sleep 60", timeout=30), entry(f"echo 1 >> {marker}")]
    with signal_self_when_started(marker, 1, signal.SIGINT):
        run_post(runner(dirs, post=post), DecisionIds(()), lambda e, d: PAYLOAD)
    assert marker.read_text().split() == ["0"]


def test_an_async_hook_is_detached_and_not_waited_for(
    dirs: StateDirs, tmp_path: Path, pids: Path
) -> None:
    done = tmp_path / "done"
    got = tmp_path / "got.json"
    ids = tmp_path / "ids"
    cmd = (
        f"echo $$ >> {pids}; ps -o pid=,pgid=,sid= -p $$ > {ids}; "
        f"cat > {got}; sleep 1; echo ok > {done}"
    )
    r = runner(dirs, post=[entry(cmd, async_=True)])
    started = time.monotonic()
    run_post(r, DecisionIds(()), lambda e, d: PAYLOAD)
    assert time.monotonic() - started < 0.9 and not done.exists()
    assert wait_file(done, 10)
    assert json.loads(got.read_text()) == PAYLOAD
    (pid,) = (int(x) for x in pids.read_text().split())
    hook_pid, pgid, sid = (int(x) for x in ids.read_text().split())
    assert hook_pid == pid  # its own session and group: it leads both
    assert pid == pgid == sid
    assert pgid != os.getpgrp() and sid != os.getsid(0)


@pytest.mark.parametrize("failure", ["no_shell", "payload_write", "nul_in_command"])
def test_an_async_hook_that_cannot_start_leaves_no_directory_behind(
    dirs: StateDirs, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    cmd = "exit 0"
    if failure == "no_shell":
        monkeypatch.setattr(hooks_module, "SHELL", "/nonexistent/sh")
    elif failure == "nul_in_command":
        cmd = "exit 0\0"
    else:

        def broken(dir_fd: int, data: bytes) -> int:
            os.close(create_file_exclusive(dir_fd, hooks_module.PAYLOAD_FILE))  # left behind
            raise OSError("disk full")

        monkeypatch.setattr(hooks_module, "_payload_descriptor", broken)
    r = runner(dirs, post=[entry(cmd, async_=True)])
    assert r.spawn_detached(POST, entry(cmd, async_=True), PAYLOAD) is False
    assert tmp_entries(dirs) == []
    run_post(r, DecisionIds(()), lambda e, d: PAYLOAD)  # warns "hook_not_started", never raises
    assert tmp_entries(dirs) == []


def test_an_async_hooks_stdout_is_never_read_for_ids(dirs: StateDirs, tmp_path: Path) -> None:
    done = tmp_path / "done"
    cmd = f'echo \'{{"decision_id": "x"}}\'; echo ok > {done}'
    ids = DecisionIds(())
    run_post(runner(dirs, post=[entry(cmd, async_=True)]), ids, lambda e, d: PAYLOAD)
    assert wait_file(done) and ids.all() == []


# --- environment and working directory --------------------------------------------------------


def test_the_environment_drops_tokens_and_denied_names_and_adds_the_hook_variables() -> None:
    environ = {
        "PATH": "/usr/bin",
        "HOME": "/h",
        "GH_TOKEN": "t1",
        "GITHUB_TOKEN": "t2",
        "GH_ENTERPRISE_TOKEN": "t3",
        "GITHUB_ENTERPRISE_TOKEN": "t4",
        "GITLAB_TOKEN": "t5",
        "GITLAB_ACCESS_TOKEN": "t6",
        "OAUTH_TOKEN": "t7",
        "MY_SECRET": "s",
        "KEEP": "k",
        "PR_COMMENT_HOOK": "spoofed",
    }
    env = hook_environment(environ, ["MY_SECRET", "ABSENT"], hook="pre-post", pr=PR)
    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/h",
        "KEEP": "k",
        "PR_COMMENT_HOOK": "pre-post",
        "PR_COMMENT_ID": PR,
        "PR_COMMENT_OBJECT_TYPE": "pr",
    }


def test_a_real_hook_sees_the_scrubbed_environment(dirs: StateDirs, tmp_path: Path) -> None:
    out = tmp_path / "env"
    environ = {"GH_TOKEN": "tok-1", "GITLAB_TOKEN": "tok-2", "MY_SECRET": "s3", "KEEP": "k"}
    r = runner(dirs, environ=environ, deny=["MY_SECRET"])
    assert run_one(r, entry(f"env > {out}")).verdict == "ok"
    lines = out.read_text().splitlines()
    assert "KEEP=k" in lines and f"PR_COMMENT_ID={PR}" in lines
    assert "PR_COMMENT_HOOK=pre-post" in lines and "PR_COMMENT_OBJECT_TYPE=pr" in lines
    text = out.read_text()
    assert "tok-1" not in text and "tok-2" not in text and "s3" not in text


def test_the_working_directory_is_private_empty_and_removed_afterwards(
    dirs: StateDirs, tmp_path: Path
) -> None:
    out = tmp_path / "cwd"
    cmd = f"pwd -P > {out}; ls -A >> {out}; stat -c %a . >> {out}; touch scratch"
    assert run_one(runner(dirs), entry(cmd)).verdict == "ok"
    lines = out.read_text().splitlines()
    assert Path(lines[0]).parent == dirs.tmp_path.resolve()
    assert Path(lines[0]).name.startswith("prc-hook-")
    assert lines[1:] == ["700"]  # empty at the start, mode 0700
    assert tmp_entries(dirs) == []  # removed, the file it left included


def test_each_hook_gets_its_own_directory(dirs: StateDirs, tmp_path: Path) -> None:
    out = tmp_path / "dirs"
    r = runner(dirs, pre=[entry(f"pwd -P >> {out}; touch mark")] * 2)
    run_pre(r, DecisionIds(()), lambda e, d: PAYLOAD)
    first, second = out.read_text().split()
    assert first != second


def test_a_directory_made_unremovable_by_the_hook_is_a_warning_not_a_failure(
    dirs: StateDirs, tmp_path: Path
) -> None:
    deep = "d/" * 80
    cmd = f"mkdir -p {deep}; exit 0"
    result = run_one(runner(dirs), entry(cmd))
    assert result.verdict == "ok"  # the run's verdict is the hook's exit status
    # the too-deep tree is left in place (docs/interface.md, Files); the next sweep handles it
    leftover = tmp_entries(dirs)
    assert len(leftover) <= 1
    if leftover:
        mode = stat.S_IMODE(os.stat(dirs.tmp_path / leftover[0]).st_mode)
        assert mode & 0o077 == 0


def test_untrusted_text_reaches_a_hook_only_through_stdin(dirs: StateDirs, tmp_path: Path) -> None:
    marker = "UNTRUSTED-MARKER-9F3A"
    out = tmp_path / "view"
    cmd = f"env > {out}; tr '\\0' ' ' < /proc/$$/cmdline >> {out}; cat > {out}.stdin"
    payload = {**PAYLOAD, "bodies": [{"role": "body", "text": marker}]}
    assert runner(dirs).run_sync(PRE, 0, entry(cmd), payload).verdict == "ok"
    assert marker not in out.read_text()
    assert marker in Path(f"{out}.stdin").read_text()
