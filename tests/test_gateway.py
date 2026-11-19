"""The forgectl process gateway (docs/interface.md: forgectl edge).

Process tests start real fake executables (tests/fakes.py); every process they start is recorded
and ended at teardown by pid, group and start time, never by pattern.
"""

from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from pr_comment import gateway as gw_mod
from pr_comment.config import Config, LoadedConfig
from pr_comment.gateway import (
    ForgectlInterrupted,
    GatewayInternalError,
    GatewayUsageError,
    ProcessGateway,
    ToolMissing,
    build_argv,
    build_env,
    resolve_binary,
    validate_call,
)
from pr_comment.lower import failure_outcome
from tests.fake_gateway import FakeGateway
from tests.fakes import Tracker, alive, make_fake, recorded

SRC = str(Path(__file__).resolve().parent.parent / "src")
PR = "gh.example/o/r#1"


# --- argv ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["-x"],
        ["--expected-head"],  # a bare value flag: the value would be a separate element
        ["--Flag=1"],
        ["--json"],
        ["--config=/x"],
        ["--timeout=1"],
        ["--file"],
        ["-"],  # no preceding --file
        ["--file", "x"],
        [""],
        ["a\x00b"],
        ["\ud800"],
        ["x" * 9000],
        [PR, "--yes", "--body-file", "-", "-"],
        # The argv rules are closed: no `=` form for the stdin and boolean flags, no unknown
        # names, no abbreviations.
        ["--file=/etc/passwd"],
        ["--body-file=/etc/passwd"],
        ["--conf=/x"],
        ["--input=/x"],
        ["--dry-run=1"],
        ["--yes=true"],
        ["--override-escalation=pr-comment"],
        ["--dry"],
        ["--expected-hea=" + "a" * 40],
        ["--expected-head=" + "a" * 40 + "=x", "--allow-self"],
        ["--allow-self"],
        ["--no-such-flag=1"],
    ],
)
def test_validate_call_refuses_bad_arguments(args: list[str]) -> None:
    with pytest.raises(GatewayUsageError):
        validate_call("comment", args, b"body")


def test_validate_call_requires_stdin_exactly_with_dash() -> None:
    with pytest.raises(GatewayUsageError):
        validate_call("comment", [PR, "--body-file", "-"], None)
    with pytest.raises(GatewayUsageError):
        validate_call("show", [PR], b"x")
    with pytest.raises(GatewayUsageError):
        validate_call("show", [PR], "text")  # type: ignore[arg-type]


def test_validate_call_refuses_unknown_subcommand_and_string_args() -> None:
    for bad in ("merge", "--version", "", "show; id"):
        with pytest.raises(GatewayUsageError):
            validate_call(bad, [], None)
    with pytest.raises(GatewayUsageError):
        validate_call("show", PR, None)  # a string is not a list of arguments
    with pytest.raises(GatewayUsageError):
        validate_call("show", ["a"] * 1025, None)


def test_validate_call_accepts_the_documented_forms_and_reports_writes() -> None:
    forms = [PR, "--expected-head=" + "a" * 40, "--dedupe-key=prc:rv:x", "--to=gh:ic:abc"]
    assert validate_call("show", [PR], None) is False
    assert validate_call("review", [PR, "--file", "-", *forms[1:]], b"{}") is True
    assert validate_call("review", [PR, "--file", "-", "--dry-run"], b"{}") is False
    assert validate_call("resolve", [PR, "--thread=gh:rt:x", "--yes"], None) is True
    assert validate_call("reply", [PR, *forms[3:], "--body-file", "-"], b"hi") is True
    assert validate_call("compare", [PR, "--from=" + "a" * 40, "--to=" + "b" * 40], None) is False
    assert validate_call("whoami", ["--host=gh.example"], None) is False
    assert validate_call("approve", [PR, "--override-escalation", "--yes"], None) is True
    # A value starting with '-' is safe attached to its flag.
    assert validate_call("comment", [PR, "--dedupe-key=-x", "--body-file", "-"], b"") is True


def test_build_argv_puts_gateway_flags_before_caller_arguments() -> None:
    argv = build_argv("/b/forgectl", "show", [PR], config_path="/c.yaml", timeout="30s")
    assert argv == ["/b/forgectl", "show", "--json", "--config=/c.yaml", "--timeout=30s", PR]
    argv = build_argv("/b/forgectl", "show", [PR], config_path=None, timeout="1.5")
    assert argv == ["/b/forgectl", "show", "--json", "--timeout=1.5", PR]


# --- executable and environment -----------------------------------------------------------------


def _exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def test_relative_path_entry_is_never_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _exe(tmp_path / "rel" / "forgectl")
    _exe(tmp_path / "forgectl")
    monkeypatch.chdir(tmp_path)
    for path_value in ("rel", "", ":rel:", ".", "./rel", f"rel{os.pathsep}other"):
        with pytest.raises(ToolMissing):
            resolve_binary("forgectl", {"PATH": path_value})
    with pytest.raises(ToolMissing):
        resolve_binary("forgectl", {})


def test_absolute_path_entry_resolves_first_executable(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "forgectl").write_text("not executable")  # skipped
    good = _exe(tmp_path / "b" / "forgectl")
    path_value = os.pathsep.join(["rel", str(tmp_path / "a"), "", str(tmp_path / "b")])
    assert resolve_binary("forgectl", {"PATH": path_value}) == str(good)


def test_absolute_bin_is_used_without_path(tmp_path: Path) -> None:
    exe = _exe(tmp_path / "x" / "forgectl")
    assert resolve_binary(str(exe), {"PATH": ""}) == str(exe)


@pytest.mark.parametrize("name", ["", ".", "..", "rel/forgectl", "./forgectl", "a\x00b"])
def test_odd_binary_names_are_missing(name: str, tmp_path: Path) -> None:
    _exe(tmp_path / "rel" / "forgectl")
    with pytest.raises(ToolMissing):
        resolve_binary(name, {"PATH": str(tmp_path)})


def test_absolute_bin_that_is_a_directory_or_not_executable_is_missing(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.write_text("x")
    for target in (tmp_path, plain, tmp_path / "absent"):
        with pytest.raises(ToolMissing):
            resolve_binary(str(target), {})


def test_build_env_keeps_environment_cuts_path_and_drops_own_settings() -> None:
    env = build_env(
        {
            "PATH": "rel:/usr/bin::/bin:.",
            "HOME": "/h",
            "MY_TOKEN": "t",
            "PR_COMMENT_FORGECTL_BIN": "x",
            "PR_COMMENT_HOOK": "x",
        }
    )
    assert env == {"PATH": "/usr/bin:/bin", "HOME": "/h", "MY_TOKEN": "t"}
    assert build_env({"PATH": "rel"})["PATH"] == "/usr/bin:/bin"
    assert build_env({})["PATH"] == "/usr/bin:/bin"


def test_relative_forgectl_config_is_made_absolute_against_the_working_directory(
    tmp_path: Path, tracker: Tracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker.watch(tmp_path)
    make_fake(tmp_path, "print('{}')")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    gateway = _gateway(tmp_path, config_path="conf/forgectl.yaml")
    assert gateway.call("show", [PR]).exit_code == 0  # forgectl itself runs with cwd "/"
    seen = json.loads((tmp_path / "argv.json").read_text())
    assert seen[2] == f"--config={work}/conf/forgectl.yaml"
    loaded = LoadedConfig(
        config=Config(forgectl_config="rel.yaml"), config_hash="x", config_path=None
    )
    from_config = ProcessGateway.from_config(loaded, environ={})
    assert from_config._config_path == str(work / "rel.yaml")  # pyright: ignore[reportPrivateUsage]


def test_from_config_uses_config_values(tmp_path: Path) -> None:
    loaded = LoadedConfig.defaults()
    gw = ProcessGateway.from_config(loaded, environ={"PATH": "/usr/bin"})
    assert gw._timeout == "120s"  # pyright: ignore[reportPrivateUsage]
    assert gw._deadline == 120 + gw_mod.DEADLINE_SLACK_SECONDS  # pyright: ignore[reportPrivateUsage]


# --- process tests ------------------------------------------------------------------------------


def _gateway(directory: Path, **kwargs: object) -> ProcessGateway:
    params: dict[str, object] = {
        "environ": {"PATH": str(directory / "bin"), "HOME": str(directory)},
        "deadline": 30.0,
        "term_grace": 0.5,
        "drain_grace": 0.5,
    }
    params.update(kwargs)
    return ProcessGateway(**params)  # type: ignore[arg-type]


def test_argv_is_exact_with_no_shell_expansion(tmp_path: Path, tracker: Tracker) -> None:
    tracker.watch(tmp_path)
    make_fake(tmp_path, "print(json.dumps({}))")
    canary = tmp_path / "pwn"
    args = [
        PR,
        f"--dedupe-key=prc:x;touch {canary}",
        "--actor=auto:$(touch " + str(canary) + ")",
        "--to=`touch " + str(canary) + "`",
        "--expected-base=*",
        "--expected-head=~",
        "--body-file",
        "-",
        "--decision-id=ünï cødé 日本",
        "--yes",
    ]
    result = _gateway(tmp_path, config_path="/etc/f.yaml", timeout="45s").call(
        "comment", args, stdin=b"hello"
    )
    assert result.exit_code == 0
    seen = json.loads((tmp_path / "argv.json").read_text())
    assert seen == ["comment", "--json", "--config=/etc/f.yaml", "--timeout=45s", *args]
    assert not canary.exists()


def test_reads_run_with_stdin_devnull_and_writes_get_all_input(
    tmp_path: Path, tracker: Tracker
) -> None:
    tracker.watch(tmp_path)
    make_fake(
        tmp_path,
        """
        data = sys.stdin.buffer.read()
        open(D + "/stdin", "wb").write(data)
        print(json.dumps({"n": len(data)}))
        """,
    )
    gateway = _gateway(tmp_path)
    assert json.loads(gateway.call("show", [PR]).stdout) == {"n": 0}
    payload = os.urandom(3 * 1024 * 1024) + b"\n\xff end"
    result = gateway.call("review", [PR, "--file", "-"], stdin=payload)
    assert json.loads(result.stdout) == {"n": len(payload)}
    assert (tmp_path / "stdin").read_bytes() == payload
    assert result.stdin_unread is False


def test_forgectl_exiting_without_reading_stdin_ends_with_its_exit_status(
    tmp_path: Path, tracker: Tracker
) -> None:
    tracker.watch(tmp_path)
    make_fake(tmp_path, "sys.stdout.write('{}'); sys.exit(3)")
    big = b"x" * (16 * 1024 * 1024)
    result = _gateway(tmp_path).call("comment", [PR, "--body-file", "-"], stdin=big)
    assert result.exit_code == 3
    assert result.stdout == b"{}"
    assert result.stdin_unread is True


def test_closed_stdin_with_exit_zero_is_not_a_pipe_error(tmp_path: Path, tracker: Tracker) -> None:
    tracker.watch(tmp_path)
    make_fake(tmp_path, "os.close(0); time.sleep(0.3); print(json.dumps({'ok': 1}))")
    result = _gateway(tmp_path).call("review", [PR, "--file", "-"], stdin=b"x" * (8 << 20))
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"ok": 1}


def test_large_stdout_and_stderr_do_not_deadlock(tmp_path: Path, tracker: Tracker) -> None:
    tracker.watch(tmp_path)
    make_fake(
        tmp_path,
        """
        chunk = b"a" * 65536
        for i in range(64):
            os.write(1, chunk)
            if i % 4 == 0:
                os.write(2, chunk)
        sys.exit(7)
        """,
    )
    result = _gateway(tmp_path, deadline=20.0).call("show", [PR])
    assert result.exit_code == 7
    assert len(result.stdout) == 64 * 65536 and len(result.stderr) == 16 * 65536
    assert not result.stdout_truncated and not result.timed_out


def test_output_is_bounded_and_the_rest_is_discarded(tmp_path: Path, tracker: Tracker) -> None:
    tracker.watch(tmp_path)
    make_fake(
        tmp_path,
        """
        chunk = b"b" * 65536
        for _ in range(80):
            os.write(1, chunk)
            os.write(2, chunk)
        """,
    )
    result = _gateway(tmp_path, stdout_limit=1000, stderr_limit=50, deadline=20.0).call(
        "show", [PR]
    )
    assert result.exit_code == 0
    assert result.stdout == b"b" * 1000 and result.stdout_truncated
    assert result.stderr == b"b" * 50 and result.stderr_truncated


def test_non_utf8_output_is_kept_as_bytes(tmp_path: Path, tracker: Tracker) -> None:
    tracker.watch(tmp_path)
    make_fake(tmp_path, "os.write(1, b'\\xff\\xfe{'); os.write(2, b'\\x80\\n')")
    result = _gateway(tmp_path).call("show", [PR])
    assert result.stdout == b"\xff\xfe{" and result.stderr == b"\x80\n"


def test_environment_reaches_forgectl_cut_to_allowed_form(tmp_path: Path, tracker: Tracker) -> None:
    tracker.watch(tmp_path)
    make_fake(tmp_path, "json.dump(dict(os.environ), open(D + '/env.json', 'w'))")
    gateway = ProcessGateway(
        environ={
            "PATH": f"rel:{tmp_path / 'bin'}::.",
            "HOME": str(tmp_path),
            "FORGECTL_TOKEN": "tok",
            "PR_COMMENT_FORGECTL_BIN": "other",
        },
        deadline=20.0,
    )
    assert gateway.call("show", [PR]).exit_code == 0
    seen = json.loads((tmp_path / "env.json").read_text())
    assert seen["PATH"] == str(tmp_path / "bin")
    assert seen["FORGECTL_TOKEN"] == "tok"
    assert not any(k.startswith("PR_COMMENT_") for k in seen)


def test_working_directory_is_root(tmp_path: Path, tracker: Tracker) -> None:
    tracker.watch(tmp_path)
    make_fake(tmp_path, "print(json.dumps({'cwd': os.getcwd()}))")
    assert json.loads(_gateway(tmp_path).call("show", [PR]).stdout) == {"cwd": "/"}


def test_missing_forgectl_is_tool_missing_before_any_process(tmp_path: Path) -> None:
    gateway = ProcessGateway(environ={"PATH": str(tmp_path)})
    with pytest.raises(ToolMissing):
        gateway.call("show", [PR])


def test_bad_arguments_are_refused_before_resolving(tmp_path: Path) -> None:
    gateway = ProcessGateway(environ={"PATH": str(tmp_path)})  # forgectl is absent too
    with pytest.raises(GatewayUsageError):
        gateway.call("show", ["-x"])


def test_unrunnable_file_is_tool_missing(tmp_path: Path) -> None:
    bad = tmp_path / "forgectl"
    bad.write_bytes(b"\x7fELF-not-really")
    bad.chmod(0o755)
    with pytest.raises(ToolMissing):
        ProcessGateway(forgectl_bin=str(bad), environ={}).call("show", [PR])


def test_hang_is_killed_at_the_deadline_with_its_group(tmp_path: Path, tracker: Tracker) -> None:
    tracker.watch(tmp_path)
    make_fake(
        tmp_path,
        """
        import signal
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        grandchild("import time; time.sleep(60)")
        time.sleep(60)
        """,
    )
    started = time.monotonic()
    result = _gateway(tmp_path, deadline=5.0, term_grace=0.3).call("show", [PR])
    assert time.monotonic() - started < 50  # ended by the deadline, not by the 60 s sleeps
    assert result.timed_out and result.exit_code is None
    rows = recorded(tmp_path)
    assert len(rows) == 2
    for pid, _pgid, start in rows:
        assert not alive(pid, start)


def test_deadline_terminates_the_leader_only_then_the_group_is_killed(
    tmp_path: Path, tracker: Tracker
) -> None:
    tracker.watch(tmp_path)
    (tmp_path / "grand.py").write_text(
        "import signal, time\n"
        f"D = {str(tmp_path)!r}\n"
        "signal.signal(signal.SIGTERM, lambda *a: open(D + '/grand-term', 'w').close())\n"
        "open(D + '/gready', 'w').close()\n"
        "time.sleep(60)\n"
    )
    make_fake(
        tmp_path,
        """
        import signal
        grandchild(open(D + "/grand.py").read())
        while not os.path.exists(D + "/gready"):
            time.sleep(0.02)
        def term(signum, frame):
            open(D + "/leader-term", "w").close()
            time.sleep(0.2)
            os._exit(0)
        signal.signal(signal.SIGTERM, term)
        time.sleep(60)
        """,
    )
    result = _gateway(tmp_path, deadline=10.0, term_grace=5.0).call("show", [PR])
    assert result.timed_out and result.leftover
    assert result.exit_code == 0  # forgectl's own status is kept (R7)
    assert (tmp_path / "leader-term").exists()  # the leader got the SIGTERM
    assert not (tmp_path / "grand-term").exists()  # the group did not; it got SIGKILL
    for pid, _pgid, start in recorded(tmp_path):
        assert not alive(pid, start)


def test_deadline_kill_keeps_what_forgectl_left(tmp_path: Path, tracker: Tracker) -> None:
    tracker.watch(tmp_path)
    make_fake(
        tmp_path,
        """
        import signal
        def term(signum, frame):
            sys.stdout.write(json.dumps({"error": {"code": "outcome_unknown", "message": "m"}}))
            sys.stdout.flush()
            os._exit(12)
        signal.signal(signal.SIGTERM, term)
        time.sleep(60)
        """,
    )
    result = _gateway(tmp_path, deadline=10.0, term_grace=5.0).call(
        "comment", [PR, "--body-file", "-"], stdin=b"x"
    )
    assert result.timed_out and result.exit_code == 12 and b"outcome_unknown" in result.stdout
    outcome = failure_outcome(result, gateway=FakeGateway())
    assert outcome.document is not None
    error = outcome.document["error"]
    assert error["code"] == "outcome_unknown" and error["lower_error"]["code"] == "outcome_unknown"


def test_deadline_leaves_a_child_that_ends_in_time_alone(tmp_path: Path, tracker: Tracker) -> None:
    tracker.watch(tmp_path)
    make_fake(tmp_path, "time.sleep(0.2); print('{}')")
    result = _gateway(tmp_path, deadline=10.0).call("show", [PR])
    assert result.exit_code == 0 and not result.timed_out


def test_child_killed_by_a_signal_has_no_exit_code(tmp_path: Path, tracker: Tracker) -> None:
    tracker.watch(tmp_path)
    make_fake(tmp_path, "os.kill(os.getpid(), 9)")
    result = _gateway(tmp_path).call("show", [PR])
    assert result.exit_code is None and result.signal == 9 and not result.timed_out


def test_group_member_holding_the_pipes_is_ended_and_the_call_returns(
    tmp_path: Path, tracker: Tracker
) -> None:
    tracker.watch(tmp_path)
    make_fake(
        tmp_path,
        """
        grandchild("import time; time.sleep(60)")
        print('{}')
        """,
    )
    started = time.monotonic()
    result = _gateway(tmp_path, drain_grace=0.3).call("show", [PR])
    assert time.monotonic() - started < 50  # not held for the grandchild's 60 s
    assert result.exit_code == 0 and result.leftover
    if hasattr(os, "waitid"):  # the group is signaled while its leader is still unreaped
        for pid, _pgid, start in recorded(tmp_path):
            assert not alive(pid, start)


def test_signal_handlers_are_restored_and_work_from_a_thread(
    tmp_path: Path, tracker: Tracker
) -> None:
    tracker.watch(tmp_path)
    make_fake(tmp_path, "print('{}')")
    gateway = _gateway(tmp_path)
    before = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
    assert gateway.call("show", [PR]).exit_code == 0
    assert (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)) == before
    results: list[int | None] = []
    thread = threading.Thread(target=lambda: results.append(gateway.call("show", [PR]).exit_code))
    thread.start()
    thread.join(20)
    assert results == [0]


def test_lower_version_is_first_line_sanitized_and_cached(tmp_path: Path, tracker: Tracker) -> None:
    tracker.watch(tmp_path)
    make_fake(
        tmp_path,
        "sys.stdout.write('forgectl 0.1.2\\u202e' + 'x' * 100 + '\\nsecond line\\n')",
    )
    gateway = _gateway(tmp_path)
    version = gateway.lower_version()
    assert version is not None and version.startswith("forgectl 0.1.2<U+202E>")
    assert len(version) <= 64 and "\n" not in version
    assert gateway.lower_version() == version


def test_lower_version_is_none_when_forgectl_is_missing_or_fails(
    tmp_path: Path, tracker: Tracker
) -> None:
    assert ProcessGateway(environ={"PATH": str(tmp_path)}).lower_version() is None
    tracker.watch(tmp_path)
    make_fake(tmp_path, "sys.exit(2)")
    assert _gateway(tmp_path).lower_version() is None


# --- signals ------------------------------------------------------------------------------------

DRIVER = """\
import sys
from pr_comment.gateway import ForgectlInterrupted, ProcessGateway

gateway = ProcessGateway(environ={{"PATH": {path!r}}}, deadline=60.0, interrupt_grace=20.0)
try:
    result = gateway.call({sub!r}, {args!r})
except ForgectlInterrupted as exc:
    print("interrupted")
    sys.exit(128 + exc.signum)
print("finished", result.exit_code)
"""


def _require_main_thread() -> None:
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signals are delivered to the main thread")


def _signal_self_when_ready(ready: Path, signum: int) -> threading.Thread:
    """Signal this very process once the fake has started (so the gateway's handlers are
    installed). Only our own pid is ever signaled."""
    me = os.getpid()

    def run() -> None:
        end = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < end:
            time.sleep(0.02)
        if ready.exists():
            os.kill(me, signum)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _wait_for(path: Path, seconds: float = 15.0) -> None:
    end = time.monotonic() + seconds
    while not path.exists():
        assert time.monotonic() < end, f"{path.name} never appeared"
        time.sleep(0.05)


def _run_driver(
    tmp_path: Path, sub: str, args: list[str], tracker: Tracker
) -> subprocess.Popen[bytes]:
    tracker.watch(tmp_path)
    script = tmp_path / "driver.py"
    script.write_text(DRIVER.format(path=str(tmp_path / "bin"), sub=sub, args=args))
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": SRC, "HOME": str(tmp_path)}
    return subprocess.Popen(
        [sys.executable, str(script)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_signal_during_a_read_is_forwarded_and_exits_128_plus_n(
    tmp_path: Path, tracker: Tracker, signum: signal.Signals
) -> None:
    make_fake(
        tmp_path,
        """
        import signal
        def on(signum, frame):
            open(D + "/got", "w").write(str(signum))
            os._exit(0)
        signal.signal(signal.SIGTERM, on)
        signal.signal(signal.SIGINT, on)
        signal.signal(signal.SIGHUP, on)
        open(D + "/ready", "w").close()
        time.sleep(60)
        """,
    )
    driver = _run_driver(tmp_path, "show", [PR], tracker)
    try:
        _wait_for(tmp_path / "ready")
        driver.send_signal(signum)  # to the exact driver pid this test started
        out, err = driver.communicate(timeout=30)
    finally:
        if driver.poll() is None:
            driver.kill()
            driver.wait()
    assert driver.returncode == 128 + signum
    assert out.strip() == b"interrupted"  # no envelope, no result on stdout
    assert (tmp_path / "got").read_text() == str(int(signum))


def test_signal_with_a_child_that_ignores_it_ends_it_after_the_grace(
    tmp_path: Path, tracker: Tracker
) -> None:
    make_fake(
        tmp_path,
        """
        import signal
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        open(D + "/ready", "w").close()
        time.sleep(60)
        """,
    )
    tracker.watch(tmp_path)
    script = tmp_path / "driver.py"
    script.write_text(
        DRIVER.format(path=str(tmp_path / "bin"), sub="show", args=[PR]).replace(
            "interrupt_grace=20.0", "interrupt_grace=0.5, term_grace=0.3"
        )
    )
    driver = subprocess.Popen(
        [sys.executable, str(script)],
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": SRC, "HOME": str(tmp_path)},
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _wait_for(tmp_path / "ready")
        driver.send_signal(signal.SIGTERM)
        out, _ = driver.communicate(timeout=30)
    finally:
        if driver.poll() is None:
            driver.kill()
            driver.wait()
    assert driver.returncode == 128 + signal.SIGTERM
    for pid, _pgid, start in recorded(tmp_path):
        assert not alive(pid, start)


def test_interrupted_exception_carries_the_result_for_writes(
    tmp_path: Path, tracker: Tracker
) -> None:
    """A write that was interrupted can still report what forgectl left (for outcome_unknown)."""
    _require_main_thread()  # the test signals its own process: only the main thread handles it
    tracker.watch(tmp_path)
    make_fake(tmp_path, "open(D + '/ready', 'w').close(); time.sleep(60)")
    gateway = _gateway(tmp_path, deadline=60.0)
    thread = _signal_self_when_ready(tmp_path / "ready", signal.SIGTERM)
    with pytest.raises(ForgectlInterrupted) as info:
        gateway.call("comment", [PR, "--body-file", "-"], stdin=b"hi")
    thread.join(5)
    assert info.value.signum == signal.SIGTERM
    assert info.value.result.write is True
    assert info.value.result.signal == signal.SIGTERM


def test_sigterm_during_the_version_read_propagates(tmp_path: Path, tracker: Tracker) -> None:
    _require_main_thread()
    tracker.watch(tmp_path)
    make_fake(tmp_path, "open(D + '/ready', 'w').close(); time.sleep(60)")
    thread = _signal_self_when_ready(tmp_path / "ready", signal.SIGTERM)
    with pytest.raises(ForgectlInterrupted) as info:
        _gateway(tmp_path).lower_version()
    thread.join(5)
    assert info.value.signum == signal.SIGTERM and info.value.result.command == "--version"
    # and a failure outcome built on that gateway does not swallow it
    assert recorded(tmp_path)  # forgectl really ran


def test_sighup_is_forwarded_and_reported_as_129(tmp_path: Path, tracker: Tracker) -> None:
    _require_main_thread()
    tracker.watch(tmp_path)
    make_fake(
        tmp_path,
        """
        import signal
        def on(signum, frame):
            open(D + "/got", "w").write(str(signum))
            os._exit(0)
        signal.signal(signal.SIGHUP, on)
        open(D + "/ready", "w").close()
        time.sleep(60)
        """,
    )
    thread = _signal_self_when_ready(tmp_path / "ready", signal.SIGHUP)
    previous = signal.getsignal(signal.SIGHUP)
    with pytest.raises(ForgectlInterrupted) as info:
        _gateway(tmp_path, deadline=60.0).call("show", [PR])
    thread.join(5)
    assert info.value.signum == signal.SIGHUP and 128 + info.value.signum == 129
    assert (tmp_path / "got").read_text() == str(int(signal.SIGHUP))
    assert signal.getsignal(signal.SIGHUP) == previous


def test_handler_only_records_and_forwarding_stops_after_the_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_main_thread()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        forwarder = gw_mod._Forwarder()  # pyright: ignore[reportPrivateUsage]
        child = gw_mod._Child(proc)  # pyright: ignore[reportPrivateUsage]
        forwarder._handle(signal.SIGTERM, None)  # pyright: ignore[reportPrivateUsage]
        time.sleep(0.2)
        assert proc.poll() is None  # recording the signal sent nothing
        forwarder.forward_pending(child)  # the loop forwards it, to the exact pid we started
        proc.wait(timeout=10)
        assert proc.returncode == -signal.SIGTERM
        child.reap()
        sent: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
        forwarder._handle(signal.SIGINT, None)  # pyright: ignore[reportPrivateUsage]
        forwarder.forward_pending(child)
        assert sent == []  # never after the reap
    finally:
        monkeypatch.undo()
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_previous_handlers_that_python_did_not_install_are_restored_as_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_main_thread()
    calls: list[tuple[int, object]] = []

    def fake_signal(sig: int, handler: object) -> None:
        calls.append((sig, handler))

    monkeypatch.setattr(signal, "signal", fake_signal)  # every `previous` is None
    with gw_mod._Forwarder():  # pyright: ignore[reportPrivateUsage]
        pass
    installed = [sig for sig, handler in calls if callable(handler)]
    restored = [(sig, handler) for sig, handler in calls if not callable(handler)]
    assert set(installed) == {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
    assert sorted(restored) == sorted((sig, signal.SIG_DFL) for sig in installed)


def test_the_signal_mask_is_left_exactly_as_it_was(tmp_path: Path, tracker: Tracker) -> None:
    _require_main_thread()
    tracker.watch(tmp_path)
    make_fake(tmp_path, "print('{}')")
    blocked = {signal.SIGINT, signal.SIGUSR2}
    old = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
    try:
        assert _gateway(tmp_path).call("show", [PR]).exit_code == 0
        assert blocked <= signal.pthread_sigmask(signal.SIG_BLOCK, set())
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)


# --- start failures, ignored SIGCHLD, unreadable status -----------------------------------------


@pytest.mark.parametrize("number", [errno.ENOENT, errno.ENOEXEC, errno.EACCES, errno.ENOTDIR])
def test_start_errors_that_mean_the_program_is_unusable_are_tool_missing(
    tmp_path: Path, tracker: Tracker, monkeypatch: pytest.MonkeyPatch, number: int
) -> None:
    tracker.watch(tmp_path)
    make_fake(tmp_path, "print('{}')")

    def boom(*_a: object, **_k: object) -> None:
        raise OSError(number, "x")

    monkeypatch.setattr(subprocess, "Popen", boom)
    with pytest.raises(ToolMissing):
        _gateway(tmp_path).call("show", [PR])


@pytest.mark.parametrize("number", [errno.EMFILE, errno.ENOMEM, errno.EPERM, errno.E2BIG])
def test_other_start_errors_are_internal_not_tool_missing(
    tmp_path: Path, tracker: Tracker, monkeypatch: pytest.MonkeyPatch, number: int
) -> None:
    tracker.watch(tmp_path)
    make_fake(tmp_path, "print('{}')")

    def boom(*_a: object, **_k: object) -> None:
        raise OSError(number, "x")

    monkeypatch.setattr(subprocess, "Popen", boom)
    with pytest.raises(GatewayInternalError):
        _gateway(tmp_path).call("show", [PR])


IGNORING_DRIVER = """\
import signal, sys
signal.signal(signal.SIGCHLD, signal.SIG_IGN)
from pr_comment.gateway import ProcessGateway

gateway = ProcessGateway(environ={{"PATH": {path!r}}}, deadline=60.0)
result = gateway.call("show", ["gh.example/o/r#1"])
still_ignored = signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN
print("finished", result.exit_code, result.signal, still_ignored)
"""


SIGNAL_IGNORING_DRIVER = """\
import os, signal, sys
signal.signal({signum}, signal.SIG_IGN)
from pr_comment.gateway import ForgectlInterrupted, ProcessGateway

gateway = ProcessGateway(environ={{"PATH": {path!r}}}, deadline=60.0, interrupt_grace=20.0)
try:
    result = gateway.call("show", ["gh.example/o/r#1"])
except ForgectlInterrupted:
    print("interrupted")
    sys.exit(1)
print("finished", result.exit_code, signal.getsignal({signum}) == signal.SIG_IGN)
"""


@pytest.mark.parametrize("signum", [signal.SIGHUP, signal.SIGINT])
def test_an_inherited_ignored_signal_is_kept_and_does_not_interrupt(
    tmp_path: Path, tracker: Tracker, signum: signal.Signals
) -> None:
    make_fake(
        tmp_path,
        """
        open(D + "/ready", "w").close()
        for _ in range(1200):  # held until the test has sent its signal (bounded at 60 s)
            if os.path.exists(D + "/go"):
                break
            time.sleep(0.05)
        print('{}')
        """,
    )
    tracker.watch(tmp_path)
    script = tmp_path / "ignoring_signal.py"
    script.write_text(SIGNAL_IGNORING_DRIVER.format(signum=int(signum), path=str(tmp_path / "bin")))
    driver = subprocess.Popen(
        [sys.executable, str(script)],
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": SRC, "HOME": str(tmp_path)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _wait_for(tmp_path / "ready")
        driver.send_signal(signum)  # to the exact driver pid this test started
        (tmp_path / "go").write_text("go", encoding="utf-8")
        out, err = driver.communicate(timeout=90)
    finally:
        if driver.poll() is None:
            driver.kill()
            driver.wait()
    assert out.strip() == b"finished 0 True", err


def test_an_inherited_ignored_sigchld_does_not_turn_a_failure_into_success(
    tmp_path: Path, tracker: Tracker
) -> None:
    tracker.watch(tmp_path)
    make_fake(tmp_path, "sys.exit(7)")
    script = tmp_path / "ignoring.py"
    script.write_text(IGNORING_DRIVER.format(path=str(tmp_path / "bin")))
    done = subprocess.run(
        [sys.executable, str(script)],
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": SRC, "HOME": str(tmp_path)},
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert done.stdout.strip() == b"finished 7 None True", done.stderr  # and SIG_IGN restored


def test_an_ignored_sigchld_off_the_main_thread_is_refused(
    tmp_path: Path, tracker: Tracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker.watch(tmp_path)
    make_fake(tmp_path, "print('{}')")
    real = signal.getsignal
    monkeypatch.setattr(
        signal, "getsignal", lambda sig: signal.SIG_IGN if sig == signal.SIGCHLD else real(sig)
    )
    errors: list[BaseException] = []

    def run() -> None:
        try:
            _gateway(tmp_path).call("show", [PR])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(20)
    assert len(errors) == 1 and isinstance(errors[0], GatewayInternalError)
    assert not recorded(tmp_path)  # nothing was started


def test_an_unreadable_child_status_is_unknown_never_zero_and_never_signaled(
    tmp_path: Path, tracker: Tracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker.watch(tmp_path)
    make_fake(tmp_path, "print('{}')")

    def no_status(*_a: object, **_k: object) -> None:
        raise ChildProcessError()

    sent: list[int] = []
    monkeypatch.setattr(os, "waitid", no_status)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append(pgid))
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append(pid))
    result = _gateway(tmp_path).call("comment", [PR, "--body-file", "-"], stdin=b"x")
    monkeypatch.undo()
    assert result.exit_code is None and result.signal is None and not result.timed_out
    assert sent == []
    outcome = failure_outcome(result, gateway=FakeGateway())
    assert outcome.document is not None
    assert outcome.document["error"]["code"] == "outcome_unknown"
