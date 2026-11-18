"""The ``pre-post`` and ``post-post`` hooks of a forgectl write (docs/interface.md, Hooks; #33).

A hook is arbitrary code the user configured (or passed with ``--hook``) that runs around a real
write. This module is the only place pr-comment starts one. Rules:

- Each hook runs as ``/bin/sh -c <cmd>`` in a process group pr-comment creates, with a private
  empty 0700 working directory below the state ``tmp/`` (``files.HookDir``), removed afterwards
  (a failure to remove is a logged warning, never a change of the outcome).
- The environment is pr-comment's own minus forgectl's ambient token list and the config's
  ``hook_env_deny``, plus ``PR_COMMENT_HOOK``, ``PR_COMMENT_ID`` and ``PR_COMMENT_OBJECT_TYPE``.
  Untrusted text travels only in the JSON document on stdin (``with_text``), never in argv or env.
  The hook never inherits the write's stdin: its own stdin is that document.
- Output is bounded (``gateway.run_process``): stdout at most 64 KiB, stderr a few KiB; the report
  of stderr is cut to 4096 bytes. A hook that exceeds its timeout gets SIGTERM to its process group,
  a grace period and SIGKILL to the group; the group is signaled only while its leader is unreaped
  and its start time matches (``gateway._Child``). Nothing is ever signaled by pattern. The group
  is killed when the leader ends too, so a hook's background children do not outlive it (a hook
  that starts a new session or group, for example with ``setsid``, escapes).
- ``pre-post``: exit 0 continues, 13 escalates (the remaining hooks still run), anything else
  vetoes at once (later hooks do not run). A timeout, a death by signal, an unreadable status or a
  hook that cannot be started vetoes: a hook that did not answer never allows a write.
- ``post-post`` cannot undo anything: every failure is a logged warning and changes nothing.
- A decision id a hook prints (exit 0 only) is recorded in the audit outcome, passed to that write's
  forgectl call and shown to later hooks. It is never part of the plan or of the caller's ids, so
  it never satisfies the automatic approval guard (``approve.py``), which ran before any hook.
- ``async: true`` (``post-post`` only) starts the hook detached in its own session: pr-comment
  neither waits for it, nor reads its output, nor enforces its timeout, and its working directory
  is left to the hook (``files.sweep_stale_hook_dirs`` removes an empty one after a day).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from . import grammars
from .config import HookEntry
from .errors import cut_bytes, sanitize
from .files import StateDirs, StateUnsafeError, create_file_exclusive, create_hook_dir
from .gateway import ForgectlInterrupted, GatewayInternalError, ToolMissing, run_process
from .jsonlog import LogWriter
from .output import LOG
from .plan import Plan, empty_counts

PRE: Final = "pre-post"
POST: Final = "post-post"
SHELL: Final = "/bin/sh"
STDOUT_LIMIT: Final = 64 * 1024
# What is read of stderr; the report keeps at most ``errors.MAX_HOOK_STDERR_BYTES`` of it.
STDERR_READ_LIMIT: Final = 16 * 1024
TERM_GRACE_SECONDS: Final = 2.0
INTERRUPT_GRACE_SECONDS: Final = 2.0
DRAIN_GRACE_SECONDS: Final = 1.0
EXIT_ESCALATE: Final = 13
MAX_DECISION_IDS: Final = grammars.MAX_DECISION_IDS
# forgectl's ambient token variables (forgectl docs/interface.md, Credentials).
TOKEN_ENV: Final = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "GITLAB_TOKEN",
        "GITLAB_ACCESS_TOKEN",
        "OAUTH_TOKEN",
    }
)
PAYLOAD_FILE: Final = "payload.json"

# Detached (async) hooks: a reference keeps ``Popen`` from warning about a child still running.
_DETACHED: list[subprocess.Popen[bytes]] = []


@dataclass(frozen=True)
class HookSet:
    """The hooks of a run: configured entries followed by ``--hook`` ones, per hook name."""

    pre: tuple[HookEntry, ...] = ()
    post: tuple[HookEntry, ...] = ()
    deny: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.pre or self.post)


# --- environment and payload -----------------------------------------------------------------


def hook_environment(
    environ: Mapping[str, str], deny: Sequence[str], *, hook: str, pr: str
) -> dict[str, str]:
    """pr-comment's environment minus the ambient tokens and ``deny``, plus the three variables."""
    drop = TOKEN_ENV | set(deny)
    env = {k: v for k, v in environ.items() if k not in drop}
    env["PR_COMMENT_HOOK"] = hook
    env["PR_COMMENT_ID"] = pr
    env["PR_COMMENT_OBJECT_TYPE"] = "pr"
    return env


def payload_for(
    plan: Plan,
    *,
    hook: str,
    attempt_id: str,
    write_index: int,
    decision_ids: Sequence[str],
    override_escalation: Sequence[str],
    with_text: bool,
    result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The JSON document a hook gets on stdin (``schemas/hooks/<hook>.json``) for a posting plan.

    ``resolve`` and ``unresolve`` have no plan: ``payload_for_thread`` builds theirs. ``event``,
    ``anchor_head``, ``to`` and the thread fields are null where the interface says so.
    """
    bodies: list[dict[str, Any]] = []
    for body in plan.bodies:
        entry: dict[str, Any] = {
            "role": body.role,
            "sha256": _sha256(body.text),
            "bytes": body.bytes,
        }
        if with_text:
            entry["text"] = body.text
        bodies.append(entry)
    payload: dict[str, Any] = {
        "tool": "pr-comment",
        "hook": hook,
        "object_type": "pr",
        "object_id": plan.pr,
        "repo": plan.repo,
        "action": plan.action,
        "actor": plan.actor,
        "decision_ids": list(decision_ids),
        "attempt_id": attempt_id,
        "write_index": write_index,
        "plan_sha256": plan.plan_sha256,
        "override_escalation": list(dict.fromkeys(override_escalation)),
        "head_sha": plan.head_sha,
        "anchor_head": plan.anchor_head if plan.action == "review" else None,
        "base_ref": plan.base_ref,
        "event": plan.event if plan.action in ("review", "approve") else None,
        "dedupe_key": plan.dedupe_key,
        "counts": dict(plan.counts),
        "bodies": bodies,
        "to": plan.to if plan.action == "reply" else None,
        "thread_id": None,
        "thread_author_is_self": None,
        "thread_path": None,
        "thread_line": None,
    }
    if result is not None:
        payload["result"] = dict(result)
    return payload


@dataclass(frozen=True)
class ThreadTarget:
    """The thread a ``resolve`` or ``unresolve`` write is about, as the hooks see it. The author,
    path and line are unknown (None) for a thread absent from an incomplete ``comments`` listing."""

    thread_id: str
    author_is_self: bool | None = None
    path: str | None = None
    line: int | None = None


def payload_for_thread(
    *,
    action: str,
    pr: str,
    repo: str,
    actor: str,
    head_sha: str,
    target: ThreadTarget,
    hook: str,
    attempt_id: str,
    write_index: int,
    decision_ids: Sequence[str],
    override_escalation: Sequence[str],
    result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The hook payload of one ``resolve`` or ``unresolve`` write (docs/interface.md, Hooks,
    Nullability): no plan, bodies or dedupe key, ``base_ref`` null, ``head_sha`` the ``comments``
    read's, counts all zero."""
    if action not in ("resolve", "unresolve"):
        raise ValueError("not a thread action")
    payload: dict[str, Any] = {
        "tool": "pr-comment",
        "hook": hook,
        "object_type": "pr",
        "object_id": pr,
        "repo": repo,
        "action": action,
        "actor": actor,
        "decision_ids": list(decision_ids),
        "attempt_id": attempt_id,
        "write_index": write_index,
        "plan_sha256": None,
        "override_escalation": list(dict.fromkeys(override_escalation)),
        "head_sha": head_sha,
        "anchor_head": None,
        "base_ref": None,
        "event": None,
        "dedupe_key": None,
        "counts": empty_counts(),
        "bodies": [],
        "to": None,
        "thread_id": target.thread_id,
        "thread_author_is_self": target.author_is_self,
        "thread_path": target.path,
        "thread_line": target.line,
    }
    if result is not None:
        payload["result"] = dict(result)
    return payload


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def encode_payload(payload: Mapping[str, Any]) -> bytes:
    """ASCII JSON, so no untrusted character can be misread by a naive consumer."""
    return json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("ascii")


# --- one hook ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class HookResult:
    """What one synchronous hook run left, already judged.

    ``verdict`` is ``ok`` (exit 0), ``escalate`` (exit 13) or ``fail`` (anything else: a veto in
    ``pre-post``, a warning in ``post-post``). ``reason`` is a fixed word, never hook output.
    ``stderr`` is the hook's stderr cut to 4096 bytes (untrusted).
    """

    index: int
    verdict: str
    reason: str
    exit_code: int | None = None
    signal: int | None = None
    timed_out: bool = False
    stderr: str = ""
    decision_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass
class DecisionIds:
    """The ids in effect: the caller's, then those hooks printed (never the plan's own)."""

    caller: tuple[str, ...]
    from_hooks: list[str] = field(default_factory=lambda: [])

    def all(self) -> list[str]:
        return [*self.caller, *self.from_hooks]

    def add(self, found: Sequence[str]) -> list[str]:
        """Add ``found`` and return the reasons for what was dropped (a duplicate is kept once
        silently; ids past the total of 32 are dropped)."""
        problems: list[str] = []
        have = self.all()
        for item in found:
            if item in have:
                continue
            if len(have) >= MAX_DECISION_IDS:
                problems.append("decision_id_limit")
                break
            have.append(item)
            self.from_hooks.append(item)
        return problems


def parse_decision_ids(stdout: bytes, truncated: bool) -> tuple[list[str], list[str]]:
    """The ids a hook printed and the warnings about its output.

    Empty output, or an object without ``decision_id``, is silent. Output that is cut at 64 KiB,
    not UTF-8, not JSON, not an object, or a ``decision_id`` that is neither a string nor a list is
    ignored with a warning; an id (or list item) failing the grammar is dropped with a warning.
    """
    if truncated:
        return [], ["hook_stdout_too_large"]
    if not stdout.strip():
        return [], []
    try:
        document: object = json.loads(stdout.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return [], ["hook_stdout_invalid"]
    if not isinstance(document, dict):
        return [], ["hook_stdout_invalid"]
    record: dict[str, Any] = document  # pyright: ignore[reportUnknownVariableType]
    if "decision_id" not in record:
        return [], []
    value: object = record["decision_id"]
    items: list[object]
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = list(value)  # pyright: ignore[reportUnknownArgumentType]
    else:
        return [], ["hook_stdout_invalid"]
    found: list[str] = []
    warnings: list[str] = []
    for item in items:
        kept = grammars.hook_decision_id(item)
        if kept is None:
            warnings.append("hook_decision_id_invalid")
        else:
            found.append(kept)
    return found, list(dict.fromkeys(warnings))


def _reject_constant(name: str) -> object:
    raise ValueError(f"constant {name} is not JSON")


def stderr_report(raw: bytes) -> str:
    """A hook's stderr as text cut to 4096 bytes (invalid UTF-8 replaced, never raising)."""
    return cut_bytes(raw.decode("utf-8", errors="replace"))


def first_line(text: str, limit: int = 200) -> str:
    """The first non-empty line of ``text``, display-sanitized and cut, for a one-line message."""
    for line in text.splitlines():
        if line.strip():
            return sanitize(line.strip()[:limit])
    return ""


class HookRunner:
    """Runs the hooks of one run. ``log`` receives warnings (``warning`` events, no hook text)."""

    def __init__(
        self,
        hooks: HookSet,
        *,
        dirs: StateDirs | None,
        environ: Mapping[str, str],
        log: LogWriter,
        pr: str,
        attempt_id: str | None,
        term_grace: float = TERM_GRACE_SECONDS,
        interrupt_grace: float = INTERRUPT_GRACE_SECONDS,
    ) -> None:
        self.hooks = hooks
        self._dirs = dirs
        self._environ = dict(environ)
        self._log = log
        self._pr = pr
        self._attempt_id = attempt_id
        self._term_grace = term_grace
        self._interrupt_grace = interrupt_grace

    # -- logging --

    def warn(self, hook: str, index: int | None, reason: str) -> None:
        """A hook problem: a ``warning`` log line and on stderr (a fixed word, no hook text)."""
        LOG.warning("%s hook %s: %s", hook, "?" if index is None else index, reason)
        self._log.write(
            "warning",
            pr=self._pr,
            attempt_id=self._attempt_id,
            details={"hook": hook, "index": index, "reason": reason},
        )

    # -- running --

    def run_sync(
        self, hook: str, index: int, entry: HookEntry, payload: Mapping[str, Any]
    ) -> HookResult:
        """Run one hook to its end (or its timeout) and judge it.

        ``ForgectlInterrupted`` (SIGINT, SIGTERM or SIGHUP while the hook ran, after the signal was
        forwarded and the hook ended) propagates: the caller decides what a signal means there.
        """
        dirs = self._dirs
        if dirs is None:
            return HookResult(index, "fail", "no_state_directory")
        try:
            workdir = create_hook_dir(dirs.tmp_fd, dirs.tmp_path)
        except StateUnsafeError:
            return HookResult(index, "fail", "no_working_directory")
        env = hook_environment(self._environ, self.hooks.deny, hook=hook, pr=self._pr)
        try:
            try:
                raw = run_process(
                    [SHELL, "-c", entry.cmd],
                    command=hook,
                    write=False,
                    env=env,
                    stdin=encode_payload(payload),
                    deadline=entry.timeout,
                    stdout_limit=STDOUT_LIMIT,
                    stderr_limit=STDERR_READ_LIMIT,
                    term_grace=self._term_grace,
                    interrupt_grace=self._interrupt_grace,
                    drain_grace=DRAIN_GRACE_SECONDS,
                    cwd=str(workdir.path),
                    own_group=True,
                )
            except (ToolMissing, GatewayInternalError, ValueError):
                # ``ValueError`` (``UnicodeError`` included): a command that cannot be passed to
                # exec (a NUL, a lone surrogate). A hook that did not start never allows a write.
                return HookResult(index, "fail", "not_started")
        finally:
            try:
                workdir.remove(dirs.tmp_fd)
            except StateUnsafeError:
                self.warn(hook, index, "hook_directory_not_removed")
        warnings: list[str] = []
        if raw.leftover:
            warnings.append("hook_left_output_open")
        stderr = stderr_report(raw.stderr)
        if raw.timed_out:
            return HookResult(index, "fail", "timeout", timed_out=True, stderr=stderr,
                              warnings=tuple(warnings))  # fmt: skip
        if raw.exit_code is None:
            reason = f"signal_{raw.signal}" if raw.signal is not None else "status_unknown"
            return HookResult(index, "fail", reason, signal=raw.signal, stderr=stderr,
                              warnings=tuple(warnings))  # fmt: skip
        code = raw.exit_code
        if code == 0:
            ids, problems = parse_decision_ids(raw.stdout, raw.stdout_truncated)
            return HookResult(index, "ok", "exit_0", exit_code=0, stderr=stderr,
                              decision_ids=tuple(ids),
                              warnings=tuple([*warnings, *problems]))  # fmt: skip
        verdict = "escalate" if code == EXIT_ESCALATE else "fail"
        return HookResult(index, verdict, f"exit_{code}", exit_code=code, stderr=stderr,
                          warnings=tuple(warnings))  # fmt: skip

    def spawn_detached(
        self, hook: str, entry: HookEntry, payload: Mapping[str, Any], index: int | None = None
    ) -> bool:
        """Start an ``async`` hook detached; True when it was started. Never waits and never
        reads its output; its stdin is the payload, held by an unlinked file. When it cannot be
        started its working directory is removed again (a removal failure is a warning)."""
        dirs = self._dirs
        if dirs is None:
            return False
        try:
            workdir = create_hook_dir(dirs.tmp_fd, dirs.tmp_path)
        except StateUnsafeError:
            return False
        env = hook_environment(self._environ, self.hooks.deny, hook=hook, pr=self._pr)
        stdin_fd = -1
        proc: subprocess.Popen[bytes] | None = None
        try:
            stdin_fd = _payload_descriptor(workdir.fd, encode_payload(payload))
            proc = subprocess.Popen(
                [SHELL, "-c", entry.cmd],
                stdin=stdin_fd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                cwd=str(workdir.path),
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
        except (OSError, ValueError):
            return False
        else:
            _DETACHED.append(proc)
            return True
        finally:
            if stdin_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(stdin_fd)
            if proc is not None:
                with contextlib.suppress(OSError):
                    os.close(workdir.fd)  # the hook owns its directory from here on
            else:
                try:
                    workdir.remove(dirs.tmp_fd)  # closes the descriptor, once
                except StateUnsafeError:
                    self.warn(hook, index, "hook_directory_not_removed")


def _payload_descriptor(dir_fd: int, data: bytes) -> int:
    """A read descriptor positioned at the start of ``data``, held by an already unlinked file."""
    fd = create_file_exclusive(dir_fd, PAYLOAD_FILE)
    try:
        with os.fdopen(os.dup(fd), "wb") as out:  # fd stays open for the read below
            out.write(data)
    finally:
        os.close(fd)
    read = os.open(PAYLOAD_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    os.unlink(PAYLOAD_FILE, dir_fd=dir_fd)
    return read


# --- the two hook points ----------------------------------------------------------------------


@dataclass(frozen=True)
class PreVerdict:
    """The outcome of the ``pre-post`` hooks of one write.

    ``vetoed`` is the hook that vetoed (later ones did not run); ``escalated`` lists every hook
    that escalated, first first. Both empty: the write may go on.
    """

    vetoed: HookResult | None = None
    escalated: tuple[HookResult, ...] = ()


def run_pre(
    runner: HookRunner,
    ids: DecisionIds,
    payload_for_entry: Callable[[HookEntry, Sequence[str]], Mapping[str, Any]],
) -> PreVerdict:
    """Run the ``pre-post`` hooks in order. ``payload_for_entry(entry, decision_ids)`` builds the
    document for one hook (``with_text`` differs per entry); ``ids`` grows with the ids of every
    hook that exited 0, and is shown to later hooks."""
    escalated: list[HookResult] = []
    for index, entry in enumerate(runner.hooks.pre):
        result = runner.run_sync(PRE, index, entry, payload_for_entry(entry, ids.all()))
        for reason in result.warnings:
            runner.warn(PRE, index, reason)
        if result.verdict == "fail":
            return PreVerdict(vetoed=result, escalated=tuple(escalated))
        if result.verdict == "escalate":
            escalated.append(result)
            continue
        for reason in ids.add(result.decision_ids):
            runner.warn(PRE, index, reason)
    return PreVerdict(escalated=tuple(escalated))


def run_post(
    runner: HookRunner,
    ids: DecisionIds,
    payload_for_entry: Callable[[HookEntry, Sequence[str]], Mapping[str, Any]],
    *,
    interrupted: Callable[[], bool] = lambda: False,
) -> int | None:
    """Run the ``post-post`` hooks. Nothing here changes an outcome: every failure is a logged
    warning, an id a hook prints joins ``ids`` (recorded in the audit outcome). After a signal
    (``interrupted()``), the remaining hooks do not start. Returns the number of a signal that
    ended a hook (the caller records that it was received), else None."""
    stopped = False
    signum: int | None = None
    for index, entry in enumerate(runner.hooks.post):
        if stopped or interrupted():
            runner.warn(POST, index, "hook_skipped_after_signal")
            continue
        payload = payload_for_entry(entry, ids.all())
        if entry.async_:
            if not runner.spawn_detached(POST, entry, payload, index):
                runner.warn(POST, index, "hook_not_started")
            continue
        try:
            result = runner.run_sync(POST, index, entry, payload)
        except ForgectlInterrupted as exc:
            stopped = True
            signum = exc.signum
            runner.warn(POST, index, "hook_interrupted")
            continue
        for reason in result.warnings:
            runner.warn(POST, index, reason)
        if result.verdict != "ok":
            runner.warn(POST, index, f"hook_{result.reason}")
            continue
        for reason in ids.add(result.decision_ids):
            runner.warn(POST, index, reason)
    return signum
