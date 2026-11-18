"""The supervisor of an ``async`` ``post-post`` hook (docs/interface.md, Hooks; docs/design.md D-17;
issue #82).

``hooks.HookRunner.spawn_detached`` starts ``python -I -m pr_comment.watchdog`` detached in its own
session and returns; the main run neither waits for it nor needs to outlive it. The supervisor is
the hook's real parent: it starts the hook with ``gateway.run_process(own_group=True)`` (the same
code, and so the same guards, as a synchronous hook), so a hook that outlives its timeout gets
SIGTERM to its process group, a grace period and SIGKILL to the group, signaled only while the
leader is unreaped and its start time matches the one recorded at launch. Nothing here signals a
PID it did not start, and nothing is ever signaled by pattern.

The job (command, timeout, environment, payload) reaches the supervisor on its stdin, an unlinked
file, never in argv or its environment (untrusted text travels only in files or stdin). The
supervisor also inherits three descriptors from the run: the state directory (for ``log.jsonl``),
``tmp/`` and the hook's working directory, which it removes when the hook has ended. The hook
itself inherits none of them (``close_fds``).

Ends: the hook ends on its own (the supervisor then ends, SIGKILLing whatever the hook left in its
group, as for a synchronous hook), its timeout expires (``hook_timeout`` warning), or the
supervisor gets SIGINT, SIGTERM or SIGHUP (the hook is ended, ``hook_interrupted`` warning). A
supervisor killed by SIGKILL cannot act: its hook runs on without a timeout and its directory is
left to ``files.sweep_stale_hook_dirs`` (an empty one is removed after a day).
"""

from __future__ import annotations

import json
import math
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

from .appendfile import AppendError, AppendFile
from .files import HOOK_NAME_RE, HookDir, StateDirs, StateUnsafeError
from .gateway import ForgectlInterrupted, GatewayInternalError, ToolMissing, run_process
from .jsonlog import LOG_NAME, LogWriter

JOB_VERSION: Final = 1
STDOUT_LIMIT: Final = 0  # a detached hook's output is never read: it is drained and dropped
STDERR_LIMIT: Final = 0
DRAIN_GRACE: Final = 1.0
MAX_JOB_BYTES: Final = 64 * 1024 * 1024


@dataclass(frozen=True)
class Job:
    """One async hook, as handed to the supervisor. ``hook``, ``index``, ``pr`` and ``attempt_id``
    only label the log line; ``log`` says whether the run writes ``log.jsonl`` at all."""

    shell: str
    cmd: str
    timeout: float
    term_grace: float
    env: dict[str, str]
    hook: str
    index: int
    pr: str
    attempt_id: str | None
    log: bool
    state_fd: int
    state_path: str
    tmp_fd: int
    dir_fd: int
    dir_name: str
    dir_path: str


def job_for(
    dirs: StateDirs,
    *,
    shell: str,
    cmd: str,
    timeout: float,
    term_grace: float,
    env: Mapping[str, str],
    hook: str,
    index: int,
    pr: str,
    attempt_id: str | None,
    log: bool,
    dir_fd: int,
    dir_name: str,
) -> Job:
    return Job(
        shell=shell,
        cmd=cmd,
        timeout=timeout,
        term_grace=term_grace,
        env=dict(env),
        hook=hook,
        index=index,
        pr=pr,
        attempt_id=attempt_id,
        log=log,
        state_fd=dirs.state_fd,
        state_path=str(dirs.state_path),
        tmp_fd=dirs.tmp_fd,
        dir_fd=dir_fd,
        dir_name=dir_name,
        dir_path=str(dirs.tmp_path / dir_name),
    )


def encode_job(job: Job, payload: bytes) -> bytes:
    """One ASCII JSON line (the job), then the payload bytes the hook gets on its stdin."""
    document: dict[str, Any] = {"v": JOB_VERSION, **asdict(job)}
    line = json.dumps(document, ensure_ascii=True, allow_nan=False)
    return line.encode("ascii") + b"\n" + payload


def _reject_constant(name: str) -> object:
    raise ValueError(f"constant {name} is not JSON")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse_job(data: bytes) -> tuple[Job, bytes] | None:
    """The job and the payload in ``data``, or None for anything malformed. Only ``spawn_detached``
    writes this document; a supervisor started by hand with something else does nothing."""
    head, newline, payload = data.partition(b"\n")
    if not newline:
        return None
    try:
        raw: object = json.loads(head.decode("ascii"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(raw, dict):
        return None
    doc: dict[str, Any] = raw  # pyright: ignore[reportUnknownVariableType]
    expected = {"v", *Job.__dataclass_fields__}
    if set(doc) != expected or doc["v"] != JOB_VERSION:
        return None
    env: object = doc["env"]
    strings = ("shell", "cmd", "hook", "pr", "state_path", "dir_name", "dir_path")
    if not isinstance(env, dict) or not all(isinstance(doc[k], str) for k in strings):
        return None
    env_map: dict[str, Any] = env  # pyright: ignore[reportUnknownVariableType]
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in env_map.items()):
        return None
    attempt = doc["attempt_id"]
    if attempt is not None and not isinstance(attempt, str):
        return None
    numbers = ("timeout", "term_grace")
    if not all(_is_number(doc[k]) and math.isfinite(doc[k]) and doc[k] > 0 for k in numbers):
        return None
    if not all(_is_int(doc[k]) for k in ("index", "state_fd", "tmp_fd", "dir_fd")):
        return None
    if min(doc["state_fd"], doc["tmp_fd"], doc["dir_fd"]) < 0 or not isinstance(doc["log"], bool):
        return None
    if not HOOK_NAME_RE.fullmatch(doc["dir_name"]):
        return None
    job = Job(
        shell=doc["shell"],
        cmd=doc["cmd"],
        timeout=float(doc["timeout"]),
        term_grace=float(doc["term_grace"]),
        env=dict(env_map),
        hook=doc["hook"],
        index=doc["index"],
        pr=doc["pr"],
        attempt_id=attempt,
        log=doc["log"],
        state_fd=doc["state_fd"],
        state_path=doc["state_path"],
        tmp_fd=doc["tmp_fd"],
        dir_fd=doc["dir_fd"],
        dir_name=doc["dir_name"],
        dir_path=doc["dir_path"],
    )
    return job, payload


def _warn(job: Job, log: LogWriter, reason: str) -> None:
    """A ``warning`` log line with a fixed reason word (never hook output or text)."""
    log.write(
        "warning",
        pr=job.pr,
        attempt_id=job.attempt_id,
        details={"hook": job.hook, "index": job.index, "reason": reason},
    )


def supervise(job: Job, payload: bytes, log: LogWriter) -> None:
    """Run the hook of ``job`` to its end or its timeout, then remove its working directory.

    Nothing here raises for the hook's sake: a hook that cannot be started, or a signal that ended
    it, is a log line. The hook's exit status is of no interest to anyone (an async hook is
    fire-and-forget), only a timeout and the cleanup are reported.
    """
    try:
        try:
            result = run_process(
                [job.shell, "-c", job.cmd],
                command=job.hook,
                write=False,
                env=job.env,
                stdin=payload,
                deadline=job.timeout,
                stdout_limit=STDOUT_LIMIT,
                stderr_limit=STDERR_LIMIT,
                term_grace=job.term_grace,
                interrupt_grace=job.term_grace,
                drain_grace=DRAIN_GRACE,
                cwd=job.dir_path,
                own_group=True,
            )
        except ForgectlInterrupted:
            _warn(job, log, "hook_interrupted")
        except (ToolMissing, GatewayInternalError, ValueError):
            _warn(job, log, "hook_not_started")
        else:
            if result.timed_out:
                _warn(job, log, "hook_timeout")
    finally:
        try:
            HookDir(job.dir_name, Path(job.dir_path), job.dir_fd).remove(job.tmp_fd)
        except StateUnsafeError:
            _warn(job, log, "hook_directory_not_removed")


def _open_log(job: Job) -> LogWriter:
    if not job.log:
        return LogWriter.disabled()
    try:
        file = AppendFile.open(job.state_fd, LOG_NAME, Path(job.state_path) / LOG_NAME)
    except (AppendError, ValueError, OSError):
        return LogWriter.disabled()
    return LogWriter(file)


def main() -> int:
    """Entry point of ``python -I -m pr_comment.watchdog``: the job is on stdin."""
    data = sys.stdin.buffer.read(MAX_JOB_BYTES + 1)
    parsed = parse_job(data) if len(data) <= MAX_JOB_BYTES else None
    if parsed is None:
        return 2
    job, payload = parsed
    log = _open_log(job)
    try:
        supervise(job, payload, log)
    finally:
        log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
