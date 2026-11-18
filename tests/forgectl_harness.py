"""Test harness for the fake forgectl (``tests/fakebin/forgectl``, ``tests/fake_forgectl.py``).

- ``FakeForgectl``: one test's fake: the scenario it replays, the environment that puts it first on
  ``PATH``, the call log it wrote and the checks run when the test ends. The ``forgectl`` fixture
  (``tests/conftest.py``) builds one per test.
- Builders for scripted outcomes (``ok``, ``fail``, ``partial``) and for documents that validate
  against the pinned forgectl schemas (``post_doc``, ``resolve_doc``, ``show_doc`` ...).
- The edge rules of docs/interface.md (forgectl edge) as assertion helpers over the logged calls:
  ``--flag=value`` forms, bodies and review input only on stdin, ``--yes`` only after
  pr-comment's own confirmation and never on a dry run or a read, the pinned ``--expected-head`` and
  ``--expected-base``, a real call's input equal byte for byte to the confirmed plan, and a private
  child environment.

The scenario file and the call log are plain JSON so the fake, which runs in its own process, shares
nothing with the tests but files.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests import fake_forgectl as ff
from tests.fakes import Tracker, record

TESTS = Path(__file__).resolve().parent
FAKEBIN = TESTS / "fakebin"
LAUNCHER = FAKEBIN / "forgectl"
PIN: dict[str, Any] = ff.PIN
PINNED_VERSION: str = ff.VERSION

HEAD = "a" * 40
OTHER_HEAD = "b" * 40
BASE_SHA = "c" * 40
PR = "github.com/acme/widgets#7"
GITLAB_PR = "gitlab.com/acme/widgets#7"

# --- builders: scripted outcomes -----------------------------------------------------------------


def ok(command: str, document: Mapping[str, Any], **outcome: Any) -> dict[str, Any]:
    """A step: forgectl ``command`` exits 0 printing ``document`` (checked against the schema)."""
    return {
        "command": command,
        "outcome": {"kind": "ok", "document": dict(document), **outcome},
    }


def partial(
    command: str, document: Mapping[str, Any], message: str = "a host failed", **outcome: Any
) -> dict[str, Any]:
    """A step: exit 12 ``partial_results`` printing the command's own result with ``errors``."""
    return {
        "command": command,
        "outcome": {"kind": "partial", "document": dict(document), "message": message, **outcome},
    }


def fail(
    command: str,
    code: str,
    message: str = "scripted failure",
    *,
    reason: str | None = None,
    details: Mapping[str, Any] | None = None,
    plan: Mapping[str, Any] | None = None,
    fields: Mapping[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A step: the error envelope of ``code`` (or, for a ``refused`` on a dry run, the plan with
    ``would_refuse``). ``extra`` takes ``retry_after``, ``hook``, ``hook_stderr``, ``decision_ids``,
    ``escalated_hooks`` and the outcome options ``hang``, ``delay``, ``stderr_lines`` and
    ``phase``. ``phase`` ("read": before confirmation, "write": after it) says when in the post
    order an error that can happen in both does; most errors have one place (see
    ``fake_forgectl.error_phases``), and the rest default to "write" for a real write ("read" in a
    dry run). Without ``--yes`` a write-phase error is replaced by exit 13."""
    options = {k: extra.pop(k) for k in ("hang", "delay", "stderr_lines", "phase") if k in extra}
    error: dict[str, Any] = {"code": code, "message": message, **extra}
    if reason is not None:
        error["reason"] = reason
    if details is not None:
        error["details"] = dict(details)
    outcome: dict[str, Any] = {"kind": "error", "error": error, **options}
    if plan is not None:
        outcome["plan"] = dict(plan)
    if fields is not None:
        outcome["fields"] = dict(fields)
    return {"command": command, "outcome": outcome}


def refuse(
    command: str, reason: str, message: str = "scripted refusal", **kw: Any
) -> dict[str, Any]:
    return fail(command, "refused", message, reason=reason, **kw)


# --- builders: documents the pinned schemas accept -----------------------------------------------


def _repo_of(pr: str) -> str:
    return pr.rsplit("#", 1)[0]


def post_doc(
    command: str,
    pr: str = PR,
    *,
    backend: str | None = None,
    dry_run: bool = False,
    ids: Sequence[str] = (),
    thread_ids: Sequence[str] = (),
    review_id: str | None = None,
    already_posted: bool = False,
    follow_up_done: bool = False,
    warnings: Sequence[str] = (),
    head_sha: str | None = HEAD,
    head_verified: bool | None = None,
    threaded: bool | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """The result of ``review``, ``comment``, ``reply`` or ``approve``. ``neutralized_lines``,
    ``markers_removed`` and a dry run's ``plan`` are left out and filled in by the fake from the
    real stdin; pass them to assert them."""
    doc: dict[str, Any] = {
        "pr": pr,
        "action": command,
        "ids": list(ids),
        "thread_ids": list(thread_ids),
        "review_id": review_id,
        "already_posted": already_posted,
        "follow_up_done": follow_up_done,
        "warnings": list(warnings),
        "head_sha": head_sha,
        "dry_run": dry_run,
    }
    if command in ("review", "approve"):
        doc["head_verified"] = head_verified
    if command == "reply":
        doc["threaded"] = True if threaded is None else threaded
    doc.update(extra)
    return doc


def resolve_doc(
    command: str, thread: str, pr: str = PR, *, changed: bool = True, dry_run: bool = False
) -> dict[str, Any]:
    """The result of ``resolve`` or ``unresolve``; ``changed`` is false for a thread already in the
    requested state and always for a dry run."""
    doc: dict[str, Any] = {
        "pr": pr,
        "action": command,
        "thread_id": thread,
        "resolved": command == "resolve",
        "changed": changed and not dry_run,
        "dry_run": dry_run,
    }
    return doc


def show_doc(pr: str = PR, *, backend: str = "github", **overrides: Any) -> dict[str, Any]:
    number = int(pr.rsplit("#", 1)[1])
    repo = _repo_of(pr)
    host = repo.split("/", 1)[0]
    path = repo.split("/", 1)[1]
    gitlab = backend == "gitlab"
    doc: dict[str, Any] = {
        "pr": pr,
        "repo": repo,
        "repo_id": "gid://gitlab/Project/1" if gitlab else "R_kgDOAAAAAQ",
        "pr_id": "gid://gitlab/MergeRequest/1" if gitlab else "PR_kwDOAAAAAQ",
        "number": number,
        "url": f"https://{host}/{path}/-/merge_requests/{number}"
        if gitlab
        else f"https://{host}/{path}/pull/{number}",
        "backend": backend,
        "title": "A change",
        "body": "",
        "author": {"login": "octo", "id": "1", "is_bot": False},
        "state": "open",
        "draft": False,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "closed_at": None,
        "merged_at": None,
        "merged_by": None,
        "labels": [],
        "author_association": None,
        "head": {"sha": HEAD, "ref": "topic", "repo": repo, "is_fork": False},
        "base": {"ref": "main", "sha": BASE_SHA, "is_default": True},
        "merged_head_sha": None,
        "merge_commit_sha": None,
        "head_fetch_ref": f"refs/{'merge-requests' if gitlab else 'pull'}/{number}/head",
        "mergeable": True,
        "blocked_by": [],
        "auto_merge": {"enabled": False, "by": None},
        "merge_queue": False,
        "merge_methods_allowed": ["merge", "squash", "rebase"],
        "additions": None if gitlab else 3,
        "deletions": None if gitlab else 1,
        "changed_files": 1,
        "review_decision": "none",
        "approvals": 0,
        "approvals_at_head": None if gitlab else 0,
        "approvals_left": 1 if gitlab else None,
    }
    doc.update(overrides)
    return doc


def files_doc(
    pr: str = PR, *, head_sha: str = HEAD, files: Sequence[Mapping[str, Any]] | None = None
) -> dict[str, Any]:
    listed = list(
        files
        if files is not None
        else [
            {
                "path": "a.py",
                "previous_path": None,
                "status": "modified",
                "additions": 3,
                "deletions": 1,
            }
        ]
    )
    return {
        "pr": pr,
        "head_sha": head_sha,
        "files": listed,
        "files_complete": True,
        "changed_files": len(listed),
    }


def comments_doc(
    pr: str = PR, *, head_sha: str = HEAD, threads: Sequence[Mapping[str, Any]] = ()
) -> dict[str, Any]:
    return {"pr": pr, "head_sha": head_sha, "threads": list(threads), "complete": True}


def comment_doc(cid: str, body: str = "hello", *, review_id: str | None = None) -> dict[str, Any]:
    """One comment of a ``comments`` thread (``comments.json``)."""
    return {
        "id": cid,
        "author": {"login": "octo", "id": "1", "is_bot": False},
        "body": body,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "minimized": False,
        "reply_to": None,
        "review_id": review_id,
    }


def thread_doc(
    thread: str,
    comments: Sequence[Mapping[str, Any]],
    *,
    resolvable: bool = True,
    resolved: bool = False,
    head_sha: str = HEAD,
) -> dict[str, Any]:
    """One thread of a ``comments`` read. A thread without a path is a general one (a review body
    on GitLab); a resolved thread must be resolvable."""
    return {
        "id": thread,
        "resolvable": resolvable,
        "resolved": resolved,
        "outdated": False,
        "path": None,
        "line": None,
        "side": None,
        "start_line": None,
        "commit_sha": head_sha,
        "comments": [dict(c) for c in comments],
    }


def diff_doc(pr: str = PR, *, head_sha: str = HEAD, diff: str = "") -> dict[str, Any]:
    return {
        "pr": pr,
        "head_sha": head_sha,
        "base_sha": BASE_SHA,
        "merge_base_sha": BASE_SHA,
        "diff": diff,
    }


def compare_doc(
    pr: str = PR,
    *,
    from_sha: str = HEAD,
    to_sha: str = OTHER_HEAD,
    status: str = "ahead",
    diff: str | None = "",
    include_diff: bool = True,
) -> dict[str, Any]:
    doc: dict[str, Any] = {"pr": pr, "from_sha": from_sha, "to_sha": to_sha, "status": status}
    if include_diff:
        doc["diff"] = diff
    return doc


def whoami_doc(
    hosts: Sequence[str] = ("github.com", "gitlab.com"),
    *,
    backends: Mapping[str, str] | None = None,
    errors: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    kinds = {"github.com": "github", "gitlab.com": "gitlab", **(backends or {})}
    return {
        "hosts": [
            {
                "host": h,
                "backend": kinds[h],
                "login": "octo",
                "id": "1",
                "me": "octo",
                "me_matches_login": True,
                "token_source": "stored_login",
                "scopes": None,
            }
            for h in hosts
        ],
        "errors": [dict(e) for e in errors],
    }


# --- the call log --------------------------------------------------------------------------------


@dataclass
class ProcessCall:
    """One run of the fake, as it logged itself."""

    n: int
    argv: list[str] = field(default_factory=list)
    cwd: str | None = None
    stdin_kind: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    stdin: bytes | None = None  # what forgectl read of its stdin (None: it read nothing)
    exit_code: int | None = None
    kind: str | None = None  # ok, error, usage, refusal, partial, version, signal, fake_error
    code: str | None = None
    reason: str | None = None
    fake_error: str | None = None
    confirmation_blocked: bool = False
    crash: str | None = None  # the traceback of a crash of the fake itself
    hung_phase: str | None = None
    step: int | None = None

    @property
    def subcommand(self) -> str:
        return self.argv[0]

    @property
    def forwarded(self) -> list[str]:
        """The arguments after the gateway's own ``--json``, ``--config`` and ``--timeout``."""
        rest = self.argv[1:]
        while rest and rest[0].split("=", 1)[0] in ("--json", "--config", "--timeout"):
            rest = rest[1:]
        return rest

    @property
    def is_write(self) -> bool:
        return self.subcommand in ff.WRITE_COMMANDS and "--dry-run" not in self.argv

    @property
    def is_dry_run(self) -> bool:
        return self.subcommand in ff.WRITE_COMMANDS and "--dry-run" in self.argv


def read_calls(directory: Path) -> list[ProcessCall]:
    path = directory / "calls.jsonl"
    calls: dict[int, ProcessCall] = {}
    if not path.exists():
        return []
    for line in path.read_text(encoding="ascii").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        call = calls.setdefault(row["n"], ProcessCall(n=row["n"]))
        event = row["event"]
        if event == "start":
            call.argv = row["argv"]
            call.cwd = row["cwd"]
            call.stdin_kind = row["stdin_kind"]
            call.env = row["env"]
        elif event == "stdin":
            call.stdin = base64.b64decode(row["b64"])
        elif event == "step":
            call.step = row["index"]
        elif event == "end":
            call.exit_code = row["exit"]
            call.kind = row.get("kind")
            call.code = row.get("code")
            call.reason = row.get("reason")
        elif event == "fake_error":
            call.fake_error = row["message"]
        elif event == "crash":
            call.crash = row["traceback"]
        elif event == "confirmation_blocked":
            call.confirmation_blocked = True
        elif event == "hang":
            call.hung_phase = row["phase"]
    return [calls[n] for n in sorted(calls)]


# --- one test's fake -----------------------------------------------------------------------------


class FakeForgectl:
    """The fake forgectl of one test. ``environ()`` puts it first on ``PATH``; ``script`` and
    ``add`` set what the host answers; ``calls()`` is what ran. At the end of the test the
    fixture calls ``assert_clean()``, which fails the test on ``check()``'s problems: a fake error
    or crash, a call that never ended, an argv forgectl would refuse, a write that stopped at
    confirmation (exit 13), a child environment that is not private, or a scripted step nobody
    asked for. So a scenario cannot drift from what the code under test does."""

    def __init__(self, directory: Path, tracker: Tracker) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        tracker.watch(directory)
        self.steps: list[dict[str, Any]] = []
        self.hosts: dict[str, str] = {}
        self.version: str | None = None
        self.allow_usage_errors = False
        self.allow_unused_steps = False
        self.allow_fake_errors = False  # also covers a crash of the fake and a call with no end
        self.allow_confirmation_blocked = False  # a test of what happens on exit 13 says so
        self._write()

    # scenario -------------------------------------------------------------------------------

    def _write(self) -> None:
        scenario: dict[str, Any] = {"steps": self.steps, "hosts": self.hosts}
        if self.version is not None:
            scenario["version"] = self.version
        (self.directory / "scenario.json").write_text(json.dumps(scenario), encoding="utf-8")

    def script(self, *steps: Mapping[str, Any]) -> FakeForgectl:
        """Add steps, replayed in order: each call of forgectl takes the next one."""
        self.steps.extend(dict(s) for s in steps)
        self._write()
        return self

    add = script

    def declare_host(self, host: str, backend: str) -> None:
        """A host the config declares (the closed host list is ``github.com``, ``gitlab.com``
        and these)."""
        self.hosts[host] = backend
        self._write()

    def set_version(self, version: str) -> None:
        self.version = version
        self._write()

    # environment ----------------------------------------------------------------------------

    def environ(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """An environment in which ``forgectl`` is the fake: ``PATH`` is the fakebin directory only
        (absolute, as the gateway needs), ``HOME`` private."""
        env = {
            "PATH": str(FAKEBIN),
            "HOME": str(self.directory / "home"),
            "XDG_CONFIG_HOME": str(self.directory / "home" / ".config"),
            "FAKE_FORGECTL_DIR": str(self.directory),
            "FAKE_FORGECTL_PYTHON": sys.executable,
        }
        env.update(extra or {})
        return env

    # running it directly ----------------------------------------------------------------------

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes = b"",
        env: Mapping[str, str] | None = None,
        timeout: float = 60.0,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run the fake as a gateway would (cwd ``/``), for the tests of the fake itself."""
        return subprocess.run(
            [str(LAUNCHER), *argv],
            input=stdin,
            capture_output=True,
            env=self.environ(env),
            cwd="/",
            timeout=timeout,
            check=False,
        )

    def start(
        self, argv: Sequence[str], *, stdin: bytes = b"", env: Mapping[str, str] | None = None
    ) -> subprocess.Popen[bytes]:
        """Start the fake and leave it running (a hang); the caller signals its exact pid. It
        leads its own session; the tracker ends it if a test forgets."""
        proc = subprocess.Popen(
            [str(LAUNCHER), *argv],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.environ(env),
            cwd="/",
            start_new_session=True,
        )
        record(self.directory, proc.pid)
        assert proc.stdin is not None
        proc.stdin.write(stdin)
        proc.stdin.close()
        proc.stdin = None  # closed here: communicate() must not flush it (Python 3.12 would raise)
        return proc

    def next_call_number(self) -> int:
        """The number the next run of the fake will log under."""
        count = self.directory / "count"
        return int(count.read_text()) if count.exists() else 0

    def wait_ready(self, n: int, timeout: float = 30.0) -> str:
        """Wait (bounded) until call ``n`` hangs; the phase it hangs in."""
        import time

        marker = self.directory / f"ready-{n}"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if marker.exists() and marker.read_text():
                return marker.read_text()
            time.sleep(0.02)
        raise AssertionError(f"fake forgectl call {n} never reached its hang")

    # the log -------------------------------------------------------------------------------------

    def calls(self) -> list[ProcessCall]:
        return read_calls(self.directory)

    def writes(self) -> list[ProcessCall]:
        return [c for c in self.calls() if c.is_write]

    def unused_steps(self) -> int:
        used = self.directory / "steps"
        return len(self.steps) - (int(used.read_text()) if used.exists() else 0)

    def check(self) -> list[str]:
        """What is wrong with this test's use of the fake (empty when clean)."""
        problems: list[str] = []
        for call in self.calls():
            if call.fake_error and not self.allow_fake_errors:
                problems.append(f"call {call.n} {call.argv[:3]}: fake error: {call.fake_error}")
            if call.crash and not self.allow_fake_errors:
                problems.append(f"call {call.n} {call.argv[:3]}: the fake crashed: {call.crash}")
            if call.exit_code is None and not self.allow_fake_errors:
                problems.append(f"call {call.n} {call.argv[:3]}: no end event (it did not finish)")
            if call.confirmation_blocked and not self.allow_confirmation_blocked:
                problems.append(
                    f"call {call.n} {call.argv[:3]}: a write without --yes stopped at "
                    "confirmation (exit 13); the code under test never confirmed it"
                )
            problems += home_violations(call, self.directory.parent)
            if call.kind == "usage" and not self.allow_usage_errors:
                problems.append(
                    f"call {call.n}: forgectl would refuse this argv (exit {call.exit_code} "
                    f"{call.code}): {call.argv}"
                )
        if self.unused_steps() and not self.allow_unused_steps:
            problems.append(f"{self.unused_steps()} scripted step(s) were never asked for")
        return problems

    def assert_clean(self) -> None:
        problems = self.check()
        assert not problems, "\n".join(problems)


# --- the edge rules of docs/interface.md (forgectl edge), as checks over logged calls ------------
#
# Stated here from the interface, not taken from pr-comment's gateway, so a mistake in one is caught
# by the other.

BARE_FLAGS = frozenset({"--dry-run", "--yes", "--override-escalation"})
VALUE_FLAGS = frozenset(
    {
        "--actor",
        "--decision-id",
        "--dedupe-key",
        "--expected-base",
        "--expected-head",
        "--from",
        "--host",
        "--thread",
        "--to",
    }
)
STDIN_FLAGS = frozenset({"--file", "--body-file"})


def form_violations(call: ProcessCall) -> list[str]:
    """Every flag is ``--flag=value`` from the closed list (a bare boolean flag excepted, and
    ``--file``/``--body-file`` followed by the stdin ``-``); the gateway's own flags come first."""
    out: list[str] = []
    argv = call.argv
    if len(argv) < 2 or argv[1] != "--json":
        out.append(f"call {call.n}: --json must follow the subcommand: {argv[:3]}")
    forwarded = call.forwarded
    i = 0
    while i < len(forwarded):
        arg = forwarded[i]
        name, equals, value = arg.partition("=")
        if arg in STDIN_FLAGS:
            if forwarded[i + 1 : i + 2] != ["-"]:
                out.append(f"call {call.n}: {arg} must be followed by the stdin '-'")
            i += 2
            continue
        if name in STDIN_FLAGS:
            out.append(f"call {call.n}: {name} must not use the = form (a path is never forwarded)")
        elif arg in BARE_FLAGS:
            pass
        elif name in BARE_FLAGS:
            out.append(f"call {call.n}: {name} is a bare flag, not {arg!r}")
        elif arg.startswith("-"):
            if name not in VALUE_FLAGS:
                out.append(f"call {call.n}: {name} is not a flag pr-comment forwards")
            elif not equals:
                out.append(f"call {call.n}: {name} must be written {name}=<value>")
            elif not value:
                out.append(f"call {call.n}: {name}= has an empty value")
        i += 1
    return out


def text_violations(call: ProcessCall, texts: Iterable[str]) -> list[str]:
    """Untrusted text (a body, a finding, a review's JSON) is never in argv or the environment:
    the call log holds the child's whole environment (but the harness's own FAKE_FORGECTL_*
    variables), and every value and name is checked."""
    out: list[str] = []
    for text in texts:
        if not text:
            continue
        for arg in call.argv:
            if text in arg:
                out.append(f"call {call.n}: body text {text[:30]!r} is in argv")
        for key, value in call.env.items():
            if text in value or text in key:
                out.append(f"call {call.n}: body text {text[:30]!r} is in the environment ({key})")
    return out


def home_violations(call: ProcessCall, root: Path) -> list[str]:
    """``HOME`` and every ``XDG_*`` variable of the child lie under ``root`` (the test's own
    directory), so the developer's real configuration never reaches forgectl."""
    out: list[str] = []
    base = os.path.realpath(root)
    for key, value in call.env.items():
        if key != "HOME" and not key.startswith("XDG_"):
            continue
        path = os.path.realpath(value)
        if path != base and not path.startswith(base + os.sep):
            out.append(f"call {call.n}: {key}={value!r} is not under the test directory {root}")
    return out


def environment_violations(call: ProcessCall, root: Path | None = None) -> list[str]:
    """forgectl runs with a private child environment: no ``PR_COMMENT_*`` variable, a ``PATH`` of
    absolute entries only, working directory ``/``, stdin ``/dev/null`` for a read and a pipe for a
    call that carries input. With ``root``, ``HOME`` and ``XDG_*`` must be under it too."""
    out: list[str] = []
    if root is not None:
        out += home_violations(call, root)
    for key in call.env:
        if key.startswith("PR_COMMENT_"):
            out.append(f"call {call.n}: {key} reached forgectl")
    for entry in call.env.get("PATH", "").split(os.pathsep):
        if not entry.startswith("/"):
            out.append(f"call {call.n}: PATH entry {entry!r} is not absolute")
    if call.cwd != "/":
        out.append(f"call {call.n}: working directory {call.cwd!r}, not '/'")
    carries_input = "-" in call.argv
    want = "pipe" if carries_input else "devnull"
    if call.stdin_kind != want:
        out.append(f"call {call.n}: stdin is {call.stdin_kind}, expected {want}")
    return out


def yes_violations(calls: Sequence[ProcessCall], *, confirmed: bool) -> list[str]:
    """``--yes`` is passed only after pr-comment's own confirmation, and then only on the real
    write: never on a read, never on a ``--dry-run`` pre-check. With ``confirmed`` true every real
    write carries it (forgectl exits 13 otherwise); with it false none may."""
    out: list[str] = []
    for call in calls:
        has = "--yes" in call.argv
        if has and call.subcommand not in ff.WRITE_COMMANDS:
            out.append(f"call {call.n}: --yes on a read ({call.subcommand})")
        elif has and "--dry-run" in call.argv:
            out.append(f"call {call.n}: --yes on a pre-check (--dry-run)")
        elif has and not confirmed:
            out.append(f"call {call.n}: --yes before pr-comment's confirmation")
        elif confirmed and call.is_write and not has:
            out.append(f"call {call.n}: a confirmed write without --yes (forgectl exits 13)")
    return out


def pin_violations(call: ProcessCall, *, head: str | None, base: str | None) -> list[str]:
    """``review`` and ``approve`` carry the pinned ``--expected-head`` and ``--expected-base``."""
    if call.subcommand not in ("review", "approve"):
        return []
    out: list[str] = []
    for flag, want in (("--expected-head", head), ("--expected-base", base)):
        if want is None:
            continue
        values = [a.split("=", 1)[1] for a in call.argv if a.startswith(flag + "=")]
        if values != [want]:
            out.append(f"call {call.n}: {flag} is {values}, the pinned value is {want!r}")
    return out


def plan_violations(calls: Sequence[ProcessCall], confirmed_input: bytes) -> list[str]:
    """The real write's stdin equals, byte for byte, the confirmed plan's input; so does every
    pre-check that carried input (a dry run that saw other bytes proved nothing about the write)."""
    out: list[str] = []
    for call in calls:
        if "-" not in call.argv or call.subcommand not in ff.WRITE_COMMANDS:
            continue
        if (call.stdin or b"") != confirmed_input:
            out.append(
                f"call {call.n} ({'dry run' if call.is_dry_run else 'write'}): stdin is "
                f"{len(call.stdin or b'')} bytes that differ from the confirmed "
                f"{len(confirmed_input)}"
            )
    return out


def edge_violations(
    calls: Sequence[ProcessCall],
    *,
    texts: Iterable[str] = (),
    confirmed: bool = False,
    head: str | None = None,
    base: str | None = None,
    confirmed_input: bytes | None = None,
    root: Path | None = None,
) -> list[str]:
    """Every edge rule over ``calls``; empty when pr-comment kept them all. ``root``: the test's
    directory, which ``HOME`` and ``XDG_*`` must be under."""
    out: list[str] = []
    texts = list(texts)
    for call in calls:
        if call.subcommand not in ff.MODELED:
            continue
        out += form_violations(call)
        out += text_violations(call, texts)
        out += environment_violations(call, root)
        out += pin_violations(call, head=head, base=base)
    out += yes_violations([c for c in calls if c.subcommand in ff.MODELED], confirmed=confirmed)
    if confirmed_input is not None:
        out += plan_violations(calls, confirmed_input)
    return out


def assert_edge_rules(calls: Sequence[ProcessCall], **kwargs: Any) -> None:
    problems = edge_violations(calls, **kwargs)
    assert not problems, "\n".join(problems)
