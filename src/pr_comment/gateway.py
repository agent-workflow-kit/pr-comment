"""The only place pr-comment starts forgectl (docs/interface.md: forgectl edge).

Every external call goes through this module so tests can substitute fakes (design guide §14).
A call is an argv list run without a shell, with:

- ``forgectl_bin`` resolved here: an absolute path, or a bare name looked up only in the absolute
  entries of ``PATH``; the child gets a ``PATH`` of absolute entries only;
- stdin ``/dev/null`` unless the argv carries the stdin ``-`` element, in which case the whole
  input is written once and stdin is closed, while stdout and stderr are read concurrently
  and bounded;
- every value forwarded as ``--flag=value`` (a closed set of bare boolean flags and the ``-`` after
  ``--file`` or ``--body-file`` excepted), so nothing a caller passes can be read as an option;
- a deadline, SIGINT and SIGTERM forwarding to the exact child, and a child that leads its own
  process group, which is signaled only while its leader is not yet reaped (so the group id cannot
  have been reused) and only after its start time is verified where the platform shows it.

Untrusted text travels in stdin only. Nothing here reads or writes the host, a file or the audit.
``lower.py`` turns a result into pr-comment's own results and errors.
"""

from __future__ import annotations

import contextlib
import os
import re
import selectors
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import FrameType
from typing import IO, Final, Protocol

from .config import LoadedConfig
from .errors import cut_bytes, sanitize
from .grammars import duration_seconds

INSTALL_COMMAND: Final = "uv tool install git+https://github.com/agent-workflow-kit/forgectl"

SUBCOMMANDS: Final = frozenset(
    {
        "show",
        "diff",
        "files",
        "compare",
        "comments",
        "whoami",
        "review",
        "comment",
        "reply",
        "approve",
        "resolve",
        "unresolve",
    }
)
WRITE_SUBCOMMANDS: Final = frozenset(
    {"review", "comment", "reply", "approve", "resolve", "unresolve"}
)
# Flags the gateway adds itself; a caller passing one is a bug.
_OWN_FLAGS: Final = ("--json", "--config", "--timeout")
BOOLEAN_FLAGS: Final = frozenset({"--dry-run", "--yes", "--override-escalation"})
STDIN_FLAGS: Final = frozenset({"--file", "--body-file"})
STDIN_ARG: Final = "-"

MAX_ARGS: Final = 1024
MAX_ARG_BYTES: Final = 8192
MAX_STDOUT_BYTES: Final = 32 * 1024 * 1024
MAX_STDERR_BYTES: Final = 1024 * 1024
MAX_VERSION_CHARS: Final = 64

# What a run may take beyond forgectl's own --timeout budget: its write verification reads (up to
# 15 s) and a margin for its hooks and start-up. A hook can run longer: the kill then reports
# outcome_unknown for a write, never a guess.
DEADLINE_SLACK_SECONDS: Final = 60.0
MAX_DEADLINE_SECONDS: Final = 10 * 365 * 86400.0
VERSION_DEADLINE_SECONDS: Final = 10.0
TERM_GRACE_SECONDS: Final = 2.0  # forgectl itself waits 2 s between SIGTERM and SIGKILL
INTERRUPT_GRACE_SECONDS: Final = 10.0
DRAIN_GRACE_SECONDS: Final = 2.0
DEFAULT_CHILD_PATH: Final = "/usr/bin:/bin"

_FLAG_RE: Final = re.compile(r"--[a-z][a-z0-9-]*=")
_CHUNK: Final = 65536


class GatewayUsageError(ValueError):
    """The caller built an argv the gateway refuses (a bug in pr-comment, never user input)."""


class ToolMissing(Exception):
    """forgectl was not found or cannot be started: exit 12 ``tool_missing`` (``lower.py``)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ForgectlInterrupted(Exception):
    """SIGINT or SIGTERM arrived during a call. The signal was forwarded to the child, which has
    ended (or was killed after a grace period); ``result`` is what it left.

    Before any write the run exits ``128 + signum`` with no envelope (``cli.main`` does that for
    a call that does not catch this); a write maps it to ``outcome_unknown``.
    """

    def __init__(self, signum: int, result: ForgectlResult) -> None:
        super().__init__(f"interrupted by signal {signum}")
        self.signum = signum
        self.result = result


@dataclass(frozen=True)
class ForgectlResult:
    """What one forgectl call left. Bytes are as read; nothing is decoded or trusted here."""

    command: str  # the forgectl subcommand
    write: bool  # the call could have changed the host
    exit_code: int | None  # None when forgectl died by a signal or was killed
    signal: int | None = None
    stdout: bytes = b""
    stderr: bytes = b""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False
    leftover: bool = False  # a process of the group kept the pipes open after forgectl exited
    stdin_unread: bool = field(default=False)  # forgectl ended before taking all of stdin


class Gateway(Protocol):
    """The seam tests substitute (``tests/fake_gateway.py``, and a fake forgectl binary)."""

    def call(
        self, subcommand: str, args: Sequence[str] = (), *, stdin: bytes | None = None
    ) -> ForgectlResult: ...

    def lower_version(self) -> str | None: ...


# --- argv ---------------------------------------------------------------------------------------


def _check_text(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise GatewayUsageError(f"{what} must be a string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise GatewayUsageError(f"{what} holds a lone surrogate") from None
    if b"\x00" in encoded:
        raise GatewayUsageError(f"{what} holds a NUL")
    if len(encoded) > MAX_ARG_BYTES:
        raise GatewayUsageError(f"{what} is too long")
    return value


def validate_call(subcommand: str, args: Sequence[str], stdin: bytes | None) -> bool:
    """Check one call's shape and return whether it can write.

    Every argument is one of: a positional that does not start with ``-``; ``--flag=value``; a
    bare boolean flag from ``BOOLEAN_FLAGS``; ``--file`` or ``--body-file`` immediately followed by
    the stdin ``-``. The stdin ``-`` appears exactly when ``stdin`` is given. The gateway adds
    ``--json``, ``--config`` and ``--timeout`` itself. ``--dry-run`` makes a write a read.
    """
    if subcommand not in SUBCOMMANDS:
        raise GatewayUsageError(f"unknown forgectl subcommand {subcommand!r}")
    if isinstance(args, (str, bytes)):
        raise GatewayUsageError("args must be a sequence of strings")
    if len(args) > MAX_ARGS:
        raise GatewayUsageError("too many arguments")
    if stdin is not None and not isinstance(stdin, bytes):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise GatewayUsageError("stdin must be bytes")
    items = [_check_text(a, "argument") for a in args]
    stdin_marks = 0
    for index, arg in enumerate(items):
        if arg == STDIN_ARG:
            if index == 0 or items[index - 1] not in STDIN_FLAGS:
                raise GatewayUsageError("'-' is only valid after --file or --body-file")
            stdin_marks += 1
        elif arg in STDIN_FLAGS:
            if index + 1 >= len(items) or items[index + 1] != STDIN_ARG:
                raise GatewayUsageError(f"{arg} is only valid with '-' (stdin)")
        elif arg in BOOLEAN_FLAGS:
            continue
        elif arg.startswith("-"):
            match = _FLAG_RE.match(arg)
            if match is None:
                raise GatewayUsageError("a flag must be --flag=value or a known boolean flag")
            if match[0][:-1] in _OWN_FLAGS:
                raise GatewayUsageError(f"{match[0][:-1]} is added by the gateway")
        elif not arg:
            raise GatewayUsageError("an argument must not be empty")
    if stdin_marks > 1 or (stdin_marks == 1) != (stdin is not None):
        raise GatewayUsageError("stdin is given exactly when the arguments carry '-'")
    return subcommand in WRITE_SUBCOMMANDS and "--dry-run" not in items


def build_argv(
    binary: str,
    subcommand: str,
    args: Sequence[str],
    *,
    config_path: str | None,
    timeout: str,
) -> list[str]:
    """``[binary, subcommand, --json, [--config=<p>], --timeout=<d>, *args]``, no shell.

    The gateway's own flags come before the caller's arguments, so a value can never reach them.
    """
    argv = [binary, subcommand, "--json"]
    if config_path is not None:
        argv.append(f"--config={_check_text(config_path, 'config path')}")
    argv.append(f"--timeout={_check_text(timeout, 'timeout')}")
    argv.extend(args)
    return argv


# --- executable and environment -----------------------------------------------------------------


def _is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def absolute_path_entries(path_value: str) -> list[str]:
    """The absolute entries of a ``PATH`` value: an empty or relative entry means the working
    directory, so it is dropped."""
    return [p for p in path_value.split(os.pathsep) if p and os.path.isabs(p) and "\x00" not in p]


def resolve_binary(forgectl_bin: str, environ: Mapping[str, str]) -> str:
    """The absolute path of forgectl; ``ToolMissing`` when there is none.

    An absolute ``forgectl_bin`` is used as is. A bare name is looked up only in the absolute
    ``PATH`` entries. Anything else (a relative path, ``.``, ``..``, NUL) is refused.
    """
    missing = ToolMissing("forgectl was not found; install it to use pr-comment")
    if not forgectl_bin or "\x00" in forgectl_bin or forgectl_bin in (".", ".."):
        raise missing
    if os.path.isabs(forgectl_bin):
        if _is_executable(forgectl_bin):
            return forgectl_bin
        raise missing
    if "/" in forgectl_bin:
        raise missing
    for entry in absolute_path_entries(environ.get("PATH", "")):
        candidate = os.path.join(entry, forgectl_bin)
        if _is_executable(candidate):
            return candidate
    raise missing


def build_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The child's environment: pr-comment's own (forgectl selects the per-host credential from it,
    including a ``token_env`` of any name), with ``PATH`` cut to its absolute entries and
    pr-comment's own ``PR_COMMENT_*`` settings removed. No untrusted text is added."""
    env = {k: v for k, v in environ.items() if not k.startswith("PR_COMMENT_") and k != "PATH"}
    entries = absolute_path_entries(environ.get("PATH", ""))
    env["PATH"] = os.pathsep.join(entries) if entries else DEFAULT_CHILD_PATH
    return env


# --- process ------------------------------------------------------------------------------------


def _proc_start(pid: int) -> str | None:
    """The start time of ``pid`` (field 22 of ``/proc/<pid>/stat``), or None where unavailable."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    # The command name (field 2) is in parentheses and may hold anything; split after the last ')'.
    rest = data.rpartition(b")")[2].split()
    return rest[19].decode("ascii", "replace") if len(rest) > 19 else None


class _Reader(threading.Thread):
    """Reads one pipe to EOF into at most ``limit`` bytes and discards the rest (so the child
    never blocks on a full pipe). Stops early when ``stop`` is set."""

    def __init__(self, fd: int, limit: int, stop: threading.Event) -> None:
        super().__init__(daemon=True)
        self._fd = fd
        self._limit = limit
        self._stop_event = stop
        self.chunks: list[bytes] = []
        self.truncated = False
        self.eof = False

    def run(self) -> None:
        total = 0
        with selectors.DefaultSelector() as selector:
            selector.register(self._fd, selectors.EVENT_READ)
            while not self._stop_event.is_set():
                if not selector.select(0.05):
                    continue
                try:
                    data = os.read(self._fd, _CHUNK)
                except OSError:
                    break
                if not data:
                    self.eof = True
                    break
                room = self._limit - total
                if room > 0:
                    self.chunks.append(data[:room])
                    total += min(len(data), room)
                if len(data) > room:
                    self.truncated = True

    def data(self) -> bytes:
        return b"".join(self.chunks)


class _Writer(threading.Thread):
    """Writes the whole input, then closes stdin. A closed pipe (forgectl exited early) ends the
    write quietly: the call then ends with forgectl's exit status."""

    def __init__(self, pipe: IO[bytes], data: bytes) -> None:
        super().__init__(daemon=True)
        self._pipe = pipe
        self._data = data
        self.finished = False  # every byte was written

    def run(self) -> None:
        pipe = self._pipe
        try:
            view = memoryview(self._data)
            for start in range(0, len(view), _CHUNK):
                pipe.write(view[start : start + _CHUNK])
            pipe.flush()
            self.finished = True
        except (OSError, ValueError):  # EPIPE, or closed under us at teardown
            pass
        finally:
            with contextlib.suppress(OSError, ValueError):
                pipe.close()


class _Forwarder:
    """Forwards SIGINT and SIGTERM to the exact child PID while a call runs (main thread only).

    The handler records the first signal and never raises. ``reaped`` is set, with the signals
    blocked, in the same step that reaps the child, so a forwarded signal can never reach a PID
    that was already released.
    """

    def __init__(self) -> None:
        self.signum: int | None = None
        self.pid: int | None = None
        self.reaped = False
        self._saved: dict[int, object] = {}

    def __enter__(self) -> _Forwarder:
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                self._saved[sig] = signal.signal(sig, self._handle)
        return self

    def __exit__(self, *exc: object) -> None:
        for sig, previous in self._saved.items():
            if previous is not None:
                signal.signal(sig, previous)  # type: ignore[arg-type]

    def attach(self, pid: int) -> None:
        self.pid = pid
        if self.signum is not None:  # arrived between fork and here
            self._forward(self.signum)

    def _handle(self, signum: int, _frame: FrameType | None) -> None:
        if self.signum is None:
            self.signum = signum
        self._forward(signum)

    def _forward(self, signum: int) -> None:
        if self.pid is not None and not self.reaped:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(self.pid, signum)


_BLOCKED = {signal.SIGINT, signal.SIGTERM}


class _Child:
    """One started forgectl and its process group."""

    def __init__(self, proc: subprocess.Popen[bytes], forwarder: _Forwarder) -> None:
        self.proc = proc
        self.pid = proc.pid
        self.start = _proc_start(proc.pid)
        self.forwarder = forwarder
        self.reaped = False

    def exited(self) -> bool:
        """Whether the leader has exited. Where ``waitid`` exists the zombie is kept (so its PID
        and group id stay ours); elsewhere the poll reaps it."""
        if self.reaped:
            return True
        if hasattr(os, "waitid"):
            try:
                info = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                return True
            return info is not None
        return self._reap(wait=False)

    def _reap(self, *, wait: bool) -> bool:
        signal.pthread_sigmask(signal.SIG_BLOCK, _BLOCKED)
        try:
            code = self.proc.wait() if wait else self.proc.poll()
            if code is not None:
                self.reaped = True
                self.forwarder.reaped = True
            return code is not None
        finally:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, _BLOCKED)

    def reap(self) -> None:
        self._reap(wait=True)

    def can_signal_group(self) -> bool:
        """The leader is not reaped (its PID is still ours, so the group id is) and, where the
        platform shows it, its start time is the one recorded at launch."""
        if self.reaped:
            return False
        return self.start is None or _proc_start(self.pid) in (None, self.start)

    def signal_group(self, signum: int) -> None:
        if self.can_signal_group():
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.pid, signum)

    def wait_exit(self, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while not self.exited():
            if time.monotonic() >= end:
                return False
            time.sleep(0.01)
        return True

    def terminate(self, term_grace: float) -> None:
        """SIGTERM to the group, SIGKILL after the grace period. Only while the leader is
        unreaped."""
        if self.exited():
            return
        self.signal_group(signal.SIGTERM)
        if self.wait_exit(term_grace):
            return
        self.signal_group(signal.SIGKILL)
        self.wait_exit(5.0)


def run_process(
    argv: Sequence[str],
    *,
    command: str,
    write: bool,
    env: Mapping[str, str],
    stdin: bytes | None,
    deadline: float | None,
    stdout_limit: int = MAX_STDOUT_BYTES,
    stderr_limit: int = MAX_STDERR_BYTES,
    term_grace: float = TERM_GRACE_SECONDS,
    interrupt_grace: float = INTERRUPT_GRACE_SECONDS,
    drain_grace: float = DRAIN_GRACE_SECONDS,
) -> ForgectlResult:
    """Run ``argv`` (no shell) to its end and return what it left.

    Raises ``ToolMissing`` when the program cannot be started, ``ForgectlInterrupted`` after
    SIGINT or SIGTERM (once the child has ended). The child leads its own process group.
    """
    stop = threading.Event()
    with _Forwarder() as forwarder:
        try:
            proc = subprocess.Popen(
                list(argv),
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(env),
                cwd="/",
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
        except OSError:
            raise ToolMissing("forgectl could not be started") from None
        forwarder.attach(proc.pid)
        child = _Child(proc, forwarder)
        assert proc.stdout is not None and proc.stderr is not None
        out = _Reader(proc.stdout.fileno(), stdout_limit, stop)
        err = _Reader(proc.stderr.fileno(), stderr_limit, stop)
        writer: _Writer | None = None
        try:
            out.start()
            err.start()
            if stdin is not None:
                assert proc.stdin is not None
                writer = _Writer(proc.stdin, stdin)
                writer.start()
            timed_out = _wait(child, forwarder, deadline, term_grace, interrupt_grace)
            leftover = _drain(child, (out, err), stop, drain_grace)
        finally:
            # Whatever happened (even an unexpected error), nothing of ours keeps running.
            child.terminate(term_grace)
            stop.set()
            if child.exited():
                child.reap()
            for thread in (out, err):
                thread.join(2.0)
            if writer is not None:
                writer.join(0.5)
            for pipe in (proc.stdout, proc.stderr):
                with contextlib.suppress(OSError, ValueError):
                    pipe.close()
    code = proc.returncode
    result = ForgectlResult(
        command=command,
        write=write,
        exit_code=None if timed_out or code is None or code < 0 else code,
        signal=-code if (code is not None and code < 0 and not timed_out) else None,
        stdout=out.data(),
        stderr=err.data(),
        stdout_truncated=out.truncated,
        stderr_truncated=err.truncated,
        timed_out=timed_out,
        leftover=leftover,
        stdin_unread=writer is not None and not writer.finished,
    )
    if forwarder.signum is not None:
        raise ForgectlInterrupted(forwarder.signum, result)
    return result


def _wait(
    child: _Child,
    forwarder: _Forwarder,
    deadline: float | None,
    term_grace: float,
    interrupt_grace: float,
) -> bool:
    """Wait for the leader to exit; True when the deadline killed it."""
    started = time.monotonic()
    interrupted_at: float | None = None
    while not child.exited():
        now = time.monotonic()
        if deadline is not None and now - started >= deadline:
            child.terminate(term_grace)
            return True
        if forwarder.signum is not None:
            interrupted_at = interrupted_at if interrupted_at is not None else now
            if now - interrupted_at >= interrupt_grace:
                child.terminate(term_grace)
                return False
        time.sleep(0.01)
    return False


def _drain(
    child: _Child, readers: tuple[_Reader, _Reader], stop: threading.Event, grace: float
) -> bool:
    """Wait for both pipes to end after forgectl exited; True when a member of the group kept
    them open past the grace period (it is killed, with the group, while the leader is still ours,
    and the readers are stopped)."""
    end = time.monotonic() + grace
    while not all(r.eof or not r.is_alive() for r in readers):
        if time.monotonic() >= end:
            child.signal_group(signal.SIGKILL)
            time.sleep(0.1)
            stop.set()
            return True
        time.sleep(0.01)
    return False


# --- the gateway --------------------------------------------------------------------------------


class ProcessGateway:
    """Runs the real forgectl. ``call`` raises ``ToolMissing``, ``ForgectlInterrupted`` or
    ``GatewayUsageError``; anything forgectl itself does (including failing) is a result."""

    def __init__(
        self,
        *,
        forgectl_bin: str = "forgectl",
        config_path: str | None = None,
        timeout: str = "120s",
        environ: Mapping[str, str] | None = None,
        deadline: float | None = None,
        stdout_limit: int = MAX_STDOUT_BYTES,
        stderr_limit: int = MAX_STDERR_BYTES,
        term_grace: float = TERM_GRACE_SECONDS,
        interrupt_grace: float = INTERRUPT_GRACE_SECONDS,
        drain_grace: float = DRAIN_GRACE_SECONDS,
    ) -> None:
        self._bin = forgectl_bin
        self._config_path = config_path
        self._timeout = timeout
        self._environ: dict[str, str] = dict(os.environ if environ is None else environ)
        self._deadline = deadline
        self._limits = (stdout_limit, stderr_limit)
        self._graces = (term_grace, interrupt_grace, drain_grace)
        self._version: str | None = None
        self._version_read = False

    @classmethod
    def from_config(
        cls, loaded: LoadedConfig, *, environ: Mapping[str, str] | None = None
    ) -> ProcessGateway:
        """The gateway for a loaded config: ``forgectl_bin``, ``forgectl_config`` and ``timeout``
        (already validated, environment overrides folded in). The per-call deadline is forgectl's
        own budget plus ``DEADLINE_SLACK_SECONDS``."""
        config = loaded.config
        budget = float(duration_seconds(config.timeout))
        return cls(
            forgectl_bin=config.forgectl_bin,
            config_path=config.forgectl_config,
            timeout=config.timeout,
            environ=environ,
            deadline=min(budget + DEADLINE_SLACK_SECONDS, MAX_DEADLINE_SECONDS),
        )

    def resolve(self) -> str:
        """The forgectl path, or ``ToolMissing``; callers run this before any other step."""
        return resolve_binary(self._bin, self._environ)

    def call(
        self, subcommand: str, args: Sequence[str] = (), *, stdin: bytes | None = None
    ) -> ForgectlResult:
        write = validate_call(subcommand, args, stdin)
        binary = self.resolve()
        argv = build_argv(
            binary, subcommand, args, config_path=self._config_path, timeout=self._timeout
        )
        return run_process(
            argv,
            command=subcommand,
            write=write,
            env=build_env(self._environ),
            stdin=stdin,
            deadline=self._deadline,
            stdout_limit=self._limits[0],
            stderr_limit=self._limits[1],
            term_grace=self._graces[0],
            interrupt_grace=self._graces[1],
            drain_grace=self._graces[2],
        )

    def lower_version(self) -> str | None:
        """The first line of ``forgectl --version``, display-sanitized, at most 64 characters, or
        None when it cannot be read. Read once; never raises."""
        if self._version_read:
            return self._version
        self._version_read = True
        try:
            result = run_process(
                [self.resolve(), "--version"],
                command="--version",
                write=False,
                env=build_env(self._environ),
                stdin=None,
                deadline=VERSION_DEADLINE_SECONDS,
                stdout_limit=4096,
                stderr_limit=4096,
                term_grace=0.5,
                interrupt_grace=2.0,
                drain_grace=0.5,
            )
        except (ToolMissing, ForgectlInterrupted):
            return None
        if result.exit_code != 0 or result.timed_out:
            return None
        line = result.stdout.decode("utf-8", "replace").splitlines()[:1]
        text = sanitize(line[0].strip()) if line else ""
        self._version = cut_bytes(text[:MAX_VERSION_CHARS]) or None
        return self._version
