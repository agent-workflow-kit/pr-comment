"""Drives pr-comment and forgectl for the live suite, or only describes the calls.

Three modes (``settings.MODES``):

- ``plan``: nothing runs. Each step is recorded and described (its argv, whether it writes, the
  size and hash of its stdin), so a user sees what ``run`` would do.
- ``preflight``: reads run; every write gets ``--dry-run``, so nothing is written. A step that needs
  a value only a real write produces (an id, written ``<name>``) is deferred, not run.
- ``run``: writes happen, but only against a target ``verify_target`` accepted (allowlisted before
  and after ``forgectl show``).

pr-comment always runs from a private working directory with its own config file, state directory
and a forgectl tap (a shim that logs each argv, then executes the real forgectl), so the
developer's pr-comment config and audit trail are never read or written. The environment is passed
on for forgectl's credentials (never printed, and secret-looking values are redacted from every
message); every ``PR_COMMENT_*`` variable is dropped.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cleanup import delete_posted
from .errors import LiveFailure, LiveRefusal
from .pty_run import kill_group, run_on_pty
from .settings import (
    PLACEHOLDER_REPOS,
    REAL_ENV,
    LiveSettings,
    redact,
    repo_of_ref,
)

__all__ = ["LiveFailure", "LiveRefusal", "Outcome", "Running", "Runner", "Step"]

PLACEHOLDER_RE = re.compile(r"^<[a-z][a-z_0-9]*>$")
# an id as forgectl prints it, for the ledger of a run whose stdout could not be parsed
ID_RE = re.compile(r"\bg[hl]:[a-z]{1,3}:[A-Za-z0-9_=+/.-]+")
# exits after which nothing was written (usage, not found, veto, confirmation, refusal)
NOT_POSTED_EXITS = frozenset({2, 10, 11, 13, 20})
GRACE = 10.0  # how long a killed child gets to release its pipes
PC_WRITES = frozenset({"review", "comment", "reply", "approve", "resolve", "unresolve"})
TAP_SOURCE = """#!{python}
import json, os, sys, time
log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calls.jsonl")
with open(log, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({{"ts": time.time(), "argv": sys.argv[1:]}}) + "\\n")
os.execv({real!r}, [{real!r}, *sys.argv[1:]])
"""


@dataclass
class Step:
    scenario: str
    label: str
    tool: str  # pr-comment | forgectl
    argv: list[str]
    write: bool
    stdin_bytes: int = 0
    stdin_sha256: str = ""
    applied: str = "plan"  # plan | dry-run | real | deferred | read

    def describe(self) -> str:
        parts = [f"{self.tool} {' '.join(self.argv)}"]
        if self.stdin_bytes:
            parts.append(f"stdin {self.stdin_bytes} bytes sha256 {self.stdin_sha256[:12]}")
        kind = "WRITE" if self.write else "read"
        return f"[{self.applied}] {self.scenario}/{self.label} ({kind}): " + "; ".join(parts)


@dataclass
class Outcome:
    step: Step
    code: int | None = None
    doc: dict[str, Any] | None = None
    stdout: str = ""
    stderr: str = ""
    real: bool = False  # false: planned or deferred, nothing ran

    @property
    def error(self) -> dict[str, Any]:
        """The envelope's error, or for resolve and unresolve the first failing thread's."""
        doc = self.doc or {}
        error = doc.get("error")
        if isinstance(error, dict):
            return error
        for thread in doc.get("threads") or []:
            if isinstance(thread, dict) and isinstance(thread.get("error"), dict):
                return thread["error"]
        return {}

    @property
    def error_code(self) -> str | None:
        code = self.error.get("code")
        return code if isinstance(code, str) else None


@dataclass
class Running:
    """A pr-comment started in the background (the signal scenario). Only its own Popen is ever
    signaled."""

    step: Step
    proc: subprocess.Popen[bytes] | None
    runner: Runner

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def send(self, signum: int) -> bool:
        if self.proc is None or self.proc.poll() is not None:
            return False
        self.proc.send_signal(signum)
        return True

    def finish(self, timeout: float = 300.0) -> Outcome:
        __tracebackhide__ = True
        if self.proc is None:
            return Outcome(self.step)
        try:
            out, err = self.proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.runner._reap(self.proc)
            raise LiveFailure(
                f"{self.step.label}: still running after {timeout:.0f}s; its process group was "
                "killed (a write may have landed: see the ledger)"
            ) from None
        return self.runner._outcome(self.step, self.proc.returncode, out, err)


def landed_ids(doc: Mapping[str, Any] | None) -> list[str]:
    """Every id a result document says was written: the ids of a post, thread ids, a review id and
    what a failing envelope reports as landed (``error.landed``, ``error.details.landed``)."""
    found: list[str] = []

    def take(obj: Any) -> None:
        if isinstance(obj, dict):
            for key in ("ids", "thread_ids"):
                found.extend(i for i in obj.get(key) or [] if isinstance(i, str))
            for key in ("review_id", "body_thread_id"):
                if isinstance(obj.get(key), str):
                    found.append(obj[key])
            for entry in obj.get("threads") or []:
                if (
                    isinstance(entry, dict)
                    and entry.get("changed") is True
                    and isinstance(entry.get("thread_id"), str)
                ):
                    found.append(entry["thread_id"])
        elif isinstance(obj, list):
            found.extend(i for i in obj if isinstance(i, str))

    if not isinstance(doc, dict):
        return []
    take(doc)
    error = doc.get("error")
    if isinstance(error, dict):
        take(error.get("landed"))
        details = error.get("details")
        if isinstance(details, dict):
            take(details.get("landed"))
    return list(dict.fromkeys(found))


class Runner:
    def __init__(
        self,
        settings: LiveSettings,
        workdir: Path,
        *,
        env: Mapping[str, str] | None = None,
        scenario: str = "-",
    ) -> None:
        self.settings = settings
        self.workdir = workdir
        self.env: dict[str, str] = dict(REAL_ENV if env is None else env)
        self.scenario = scenario
        self.notes: list[str] = []
        self.steps: list[Step] = []
        self.verified: set[str] = set()
        self.ledger: list[dict[str, str]] = []  # ids a real write reported: cleaned up at the end
        self.possible: list[dict[str, str]] = []  # real writes whose outcome is not known
        self.left: list[str] = []  # state a human must undo (resolved threads, approvals)
        self.carry_notes: list[str] = []  # problems of the run itself, added to the teardown notes
        self.observations: list[dict[str, Any]] = []
        self.cleanups: list[Callable[[], None]] = []
        self.cleanup_timeout = 120.0
        self.cleanup_call: Callable[..., Any] = subprocess.run  # the default cleanup's calls
        self._running: list[Running] = []
        workdir.mkdir(parents=True, exist_ok=True)
        self.state_home = workdir / "state"
        self.state_home.mkdir(mode=0o700, exist_ok=True)
        self._real: str | None = None
        self._config_path: Path | None = None
        self._tap: Path | None = None

    # --- paths ----------------------------------------------------------------------------------

    @property
    def audit_path(self) -> Path:
        return self.state_home / "pr-comment" / "audit.jsonl"

    def real_forgectl(self) -> str:
        if self._real is None:
            found = shutil.which(self.settings.forgectl_bin, path=self.env.get("PATH", ""))
            if found is None:
                raise LiveRefusal(
                    "forgectl is not on PATH (PR_COMMENT_LIVE_FORGECTL names another)"
                )
            self._real = str(Path(found).resolve())
        return self._real

    def tap_path(self) -> Path:
        """The shim pr-comment runs as forgectl: it logs argv to ``calls.jsonl`` beside it."""
        if self._tap is None:
            directory = self.workdir / "tap"
            directory.mkdir(mode=0o700, exist_ok=True)
            shim = directory / "forgectl"
            shim.write_text(
                TAP_SOURCE.format(python=sys.executable, real=self.real_forgectl()),
                encoding="utf-8",
            )
            shim.chmod(0o700)
            self._tap = shim
        return self._tap

    def tap_calls(self) -> list[list[str]]:
        """The argv of every forgectl call logged so far; a half-written last line is skipped."""
        if self._tap is None:
            return []
        log = self._tap.parent / "calls.jsonl"
        if not log.exists():
            return []
        calls: list[list[str]] = []
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                argv = json.loads(line)["argv"]
            except (ValueError, KeyError, TypeError):
                continue
            if isinstance(argv, list):
                calls.append([str(a) for a in argv])
        return calls

    def config_path(self, extra: str = "", forgectl_config: str | None = None) -> Path:
        """pr-comment's config for these runs. ``extra`` is more YAML for one scenario and
        ``forgectl_config`` replaces the configured forgectl config file for it."""
        custom = bool(extra) or forgectl_config is not None
        if self._config_path is not None and not custom:
            return self._config_path
        lines = [f"forgectl_bin: {json.dumps(str(self.tap_path()))}"]
        chosen = forgectl_config if forgectl_config is not None else self.settings.forgectl_config
        if chosen:
            lines.append(f"forgectl_config: {json.dumps(chosen)}")
        path = self.workdir / ("pr-comment-custom.yaml" if custom else "pr-comment.yaml")
        path.write_text("\n".join(lines) + "\n" + extra, encoding="utf-8")
        path.chmod(0o600)
        if not custom:
            self._config_path = path
        return path

    def child_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """The environment of every child: no ``PR_COMMENT_*`` variable, the private state
        directory and, unless ``PR_COMMENT_LIVE_FORGECTL_CONFIG`` is set, a private HOME and
        XDG_CONFIG_HOME so that no login of the developer's own is found: credentials come from
        environment variables (or from that config file)."""
        env = {k: v for k, v in self.env.items() if not k.startswith("PR_COMMENT_")}
        env["XDG_STATE_HOME"] = str(self.state_home)
        if not self.settings.forgectl_config:
            home = self.workdir / "home"
            home.mkdir(mode=0o700, exist_ok=True)
            env["HOME"] = str(home)
            env["XDG_CONFIG_HOME"] = str(home / ".config")
        env.update(extra or {})
        return env

    def prepare_report(self) -> None:
        """Create the report directory and prove it is writable, before the first write: a ledger
        that cannot be written is found out here and not after a post."""
        directory = self.settings.report_dir
        if directory is None:
            return
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / f".probe-{self.settings.run_id}"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError:
            raise LiveRefusal(
                "PR_COMMENT_LIVE_REPORT_DIR cannot be created or written; nothing was called"
            ) from None

    # --- bookkeeping ----------------------------------------------------------------------------

    def _step(
        self, label: str, tool: str, argv: Sequence[str], write: bool, stdin: bytes | None
    ) -> Step:
        step = Step(self.scenario, label, tool, [str(a) for a in argv], write)
        if stdin:
            step.stdin_bytes = len(stdin)
            step.stdin_sha256 = hashlib.sha256(stdin).hexdigest()
        self.steps.append(step)
        return step

    def describe(self) -> list[str]:
        return [redact(s.describe(), self.env) for s in self.steps]

    def observe(self, row: str, key: str, value: Any) -> None:
        self.observations.append({"row": row, "key": key, "value": value})

    def leave(self, text: str) -> None:
        """Record state the suite cannot undo (a resolved thread, an approval): it is listed in the
        notes at the end."""
        if text not in self.left:
            self.left.append(text)

    def _is_plan_placeholder(self, ref: str) -> bool:
        """A plan with no configured target shows steps against placeholder repos, which need no
        allowlist because nothing is called. Once a target is configured every ref is checked."""
        return (
            self.settings.mode == "plan"
            and not self.settings.targets
            and repo_of_ref(ref) in PLACEHOLDER_REPOS
        )

    def verify_target(self, ref: str) -> None:
        """Accept ``ref`` as a write target: its repo is allowlisted as written, and (outside plan
        mode) forgectl's canonical repo, from ``show``, is too."""
        if self._is_plan_placeholder(ref):
            self.verified.add(ref)
            return
        repo = repo_of_ref(ref)
        if not self.settings.target_allowed(repo):
            raise LiveRefusal("a target repo is not on PR_COMMENT_LIVE_ALLOW; nothing was called")
        if self.settings.mode != "plan":
            out = self.fg("verify-target", "show", "--json", ref)
            canonical = (out.doc or {}).get("repo") if out.real else None
            if not out.real or out.code != 0 or not isinstance(canonical, str):
                raise LiveRefusal(
                    "forgectl show could not confirm a target's repo (if it found no credentials: "
                    "a forgectl token_env must name a variable outside the PR_COMMENT_ prefix, "
                    "such as PRCLIVE_TOKEN_<KEY>, which is dropped otherwise)"
                )
            if not self.settings.target_allowed(canonical):
                raise LiveRefusal("a target's canonical repo is not on PR_COMMENT_LIVE_ALLOW")
        self.verified.add(ref)

    def allow_ref_text(self, ref: str) -> None:
        """Accept a PR reference that names no existing PR (a refusal probe): its repo, as
        written, must be allowlisted; there is nothing for ``show`` to confirm."""
        if self._is_plan_placeholder(ref):
            self.verified.add(ref)
            return
        if not self.settings.target_allowed(repo_of_ref(ref)):
            raise LiveRefusal("a probe names a repo that is not on PR_COMMENT_LIVE_ALLOW")
        self.verified.add(ref)

    def _gate(self, step: Step) -> None:
        if not step.write or step.applied != "real":
            return
        if not any(arg in self.verified for arg in step.argv):
            raise LiveRefusal(f"{step.label}: a write names no verified target; it did not run")

    # --- the ledger -----------------------------------------------------------------------------

    def _ledger_file(self, row: Mapping[str, Any]) -> None:
        """Append one row to the ledger file (before it is kept in memory); a file that cannot be
        written is reported at the end instead of masking what happened."""
        report = self.settings.report_dir
        if report is None:
            return
        try:
            report.mkdir(parents=True, exist_ok=True)
            with (report / f"ledger-{self.settings.run_id}.jsonl").open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write(json.dumps(row) + "\n")
        except OSError:
            note = "the ledger file could not be written: the rows below are all there is"
            if note not in self.carry_notes:
                self.carry_notes.append(note)

    def ledger_event(self, row: Mapping[str, str], status: str) -> None:
        """Note in the ledger file what became of one posted id (the cleanup deleted it)."""
        self._ledger_file({**row, "status": status})

    def _ledger_ids(self, step: Step, ids: Sequence[str]) -> None:
        ref = next((a for a in step.argv if a in self.verified), "")
        for item in dict.fromkeys(ids):
            row = {"run": self.settings.run_id, "scenario": step.scenario, "pr": ref, "id": item}
            self._ledger_file(row)
            self.ledger.append(row)

    def _begin_write(self, step: Step) -> None:
        """Before a real write starts: a 'possibly posted' entry, kept until the answer says what
        landed (so a timeout or an interrupt still leaves a trace of what may be on the PR)."""
        if not (step.write and step.applied == "real"):
            return
        ref = next((a for a in step.argv if a in self.verified), "")
        row = {
            "run": self.settings.run_id,
            "scenario": step.scenario,
            "pr": ref,
            "step": step.label,
            "status": "possibly_posted",
        }
        self._ledger_file(row)
        self.possible.append(row)

    def _settle(self, step: Step, code: int | None, doc: Mapping[str, Any] | None) -> None:
        """After a real write answered: its ids go to the ledger; the 'possibly posted' entry is
        dropped when the answer is complete (a parsed exit 0 document, ids, or an exit that wrote
        nothing)."""
        if not (step.write and step.applied == "real"):
            return
        ids = landed_ids(doc)
        self._ledger_ids(step, ids)
        error = (doc or {}).get("error")
        # resolve and unresolve of several threads exit 10/11/13/20 per thread and carry on, so
        # those exits do not prove that nothing changed
        per_thread = "resolve" in step.argv[:1] or "unresolve" in step.argv[:1]
        not_landed = (code in NOT_POSTED_EXITS and not per_thread) or (
            isinstance(error, dict) and error.get("landed") is False
        )
        if ids or not_landed or (code == 0 and doc is not None):
            self._resolve(step)

    def _resolve(self, step: Step) -> None:
        for row in list(self.possible):
            if row["step"] == step.label and row["scenario"] == step.scenario:
                self.possible.remove(row)
                self._ledger_file(
                    {
                        "run": row["run"],
                        "scenario": row["scenario"],
                        "step": row["step"],
                        "status": "resolved",
                    }
                )
                return

    # --- running --------------------------------------------------------------------------------

    def _outcome(
        self, step: Step, code: int, out: bytes, err: bytes, *, strict: bool = True
    ) -> Outcome:
        __tracebackhide__ = True
        text = out.decode("utf-8", "replace")
        outcome = Outcome(
            step,
            code,
            None,
            redact(text, self.env),
            redact(err.decode("utf-8", "replace"), self.env),
            real=True,
        )
        if text.strip() and strict:
            try:
                loaded = json.loads(text)
            except ValueError as exc:
                if step.write and step.applied == "real":
                    self._ledger_ids(step, ID_RE.findall(text))  # what can be read, before failing
                raise LiveFailure(
                    f"{step.label}: stdout is not one JSON document ({exc.__class__.__name__}); "
                    "first 200 characters: " + redact(text[:200], self.env)
                ) from None
            outcome.doc = loaded if isinstance(loaded, dict) else {"value": loaded}
        self._settle(step, code, outcome.doc)
        return outcome

    def _decide(self, step: Step) -> str:
        if self.settings.mode == "plan":
            return "plan"
        if any(PLACEHOLDER_RE.match(a) for a in step.argv):
            if self.settings.writes:
                raise LiveFailure(
                    f"{step.label}: a step still holds a placeholder value, so an earlier step "
                    "did not produce what this one needs; nothing ran"
                )
            return "deferred"
        if step.write and self.settings.mode == "preflight":
            return "dry-run"
        return "real" if step.write else "read"

    def _kill_group(self, proc: subprocess.Popen[bytes]) -> None:
        kill_group(proc)

    def _reap(self, proc: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
        """Kill ``proc``'s group and collect what it printed; a child that keeps its pipes open
        past ``GRACE`` seconds is given up on and reported."""
        self._kill_group(proc)
        try:
            return proc.communicate(timeout=GRACE)
        except subprocess.TimeoutExpired:
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    with contextlib.suppress(OSError, ValueError):
                        pipe.close()
            self.carry_notes.append(
                f"a child (pid {proc.pid}) kept its pipes open after being killed: given up on"
            )
            return b"", b""

    def _call(
        self,
        step: Step,
        cmd: Sequence[str],
        *,
        stdin: bytes | None,
        env: Mapping[str, str],
        cwd: str | Path,
        timeout: float,
        strict: bool = True,
    ) -> Outcome:
        """Run ``cmd`` in a process group of its own and parse its answer. On a timeout or an
        interrupt the group is killed; the 'possibly posted' entry of a write stays."""
        __tracebackhide__ = True
        self._begin_write(step)
        proc = subprocess.Popen(
            list(cmd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
            cwd=cwd,
            start_new_session=True,
        )
        try:
            out, err = proc.communicate(input=stdin if stdin is not None else b"", timeout=timeout)
        except subprocess.TimeoutExpired:
            self._reap(proc)
            raise LiveFailure(
                f"{step.label}: no answer after {timeout:.0f}s; its process group was killed "
                "(a write may have landed: see the ledger)"
            ) from None
        except BaseException:
            self._reap(proc)
            raise
        return self._outcome(step, proc.returncode, out, err, strict=strict)

    def pc(
        self,
        label: str,
        *argv: str,
        stdin: bytes | None = None,
        write: bool | None = None,
        actor: str | None = "auto:live-suite",
        timeout: float = 300.0,
        extra_config: str = "",
        forgectl_config: str | None = None,
        env_extra: Mapping[str, str] | None = None,
    ) -> Outcome:
        """Run ``pr-comment <argv>``. A command in ``PC_WRITES`` is a write unless ``write``
        says otherwise; the actor, ``--json``, ``--config`` and (for a real write) ``--yes`` are
        added."""
        __tracebackhide__ = True
        command = argv[0] if argv else ""
        is_write = command in PC_WRITES if write is None else write
        step = self._step(label, "pr-comment", list(argv), is_write, stdin)
        step.applied = self._decide(step)
        full = self._finalize(step, actor if command not in ("export", "draft") else None)
        if step.applied in ("plan", "deferred"):
            return Outcome(step)
        self._gate(step)
        cmd = [
            sys.executable,
            "-m",
            "pr_comment",
            *full,
            "--json",
            "--config",
            str(self.config_path(extra_config, forgectl_config)),
        ]
        return self._call(
            step,
            cmd,
            stdin=stdin,
            env=self.child_env(env_extra),
            cwd=self.workdir,
            timeout=timeout,
        )

    def pc_pty(
        self, label: str, *argv: str, answer: bytes = b"y\n", timeout: float = 120.0
    ) -> Outcome:
        """A write the way a human runs it: stdin and stderr are a pty, the confirmation dialog is
        shown and ``answer`` is typed at its prompt. Plan and preflight mode run it as a plain
        (dry-run) call, since no dialog is needed."""
        __tracebackhide__ = True
        full = list(argv)
        step = self._step(label, "pr-comment", full, True, None)
        step.applied = self._decide(step)
        if step.applied != "real":
            self.steps.pop()
            return self.pc(label, *full, actor=None)
        self._gate(step)
        cmd = [
            sys.executable,
            "-m",
            "pr_comment",
            *full,
            "--json",
            "--config",
            str(self.config_path()),
        ]
        self._begin_write(step)
        result = run_on_pty(
            cmd, env=self.child_env(), cwd=self.workdir, answer=answer, deadline=timeout
        )
        outcome = Outcome(
            step, result.code, result.document, "", redact(result.terminal, self.env), real=True
        )
        self._settle(step, result.code, result.document)
        return outcome

    def _finalize(self, step: Step, actor: str | None) -> list[str]:
        """The arguments pr-comment gets (before ``--json`` and ``--config``): a dry run in
        preflight mode, ``--yes`` on a real write, the actor. They are also what a plan shows."""
        full = list(step.argv)
        if step.applied == "dry-run" and "--dry-run" not in full:
            full.append("--dry-run")
        if step.write and "--dry-run" not in full and "--yes" not in full:
            full.append("--yes")
        if actor and "--actor" not in full:
            full += ["--actor", actor]
        step.argv = list(full)
        return full

    def start_pc(
        self,
        label: str,
        *argv: str,
        stdin: bytes | None = None,
        actor: str = "auto:live-suite",
        extra_config: str = "",
    ) -> Running:
        """Start a write in the background (never in plan or preflight mode: those return a
        ``Running`` that did nothing)."""
        step = self._step(label, "pr-comment", list(argv), True, stdin)
        step.applied = self._decide(step)
        full = self._finalize(step, actor)
        if step.applied != "real":
            return Running(step, None, self)
        self._gate(step)
        cmd = [
            sys.executable,
            "-m",
            "pr_comment",
            *full,
            "--json",
            "--config",
            str(self.config_path(extra_config)),
        ]
        self._begin_write(step)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.child_env(),
            cwd=self.workdir,
            start_new_session=True,
        )
        running = Running(step, proc, self)
        self._running.append(running)  # before the first write to it: teardown can always end it
        assert proc.stdin is not None
        try:
            proc.stdin.write(stdin or b"")
            proc.stdin.close()
        except BrokenPipeError:
            pass  # it already ended; finish() reports its answer
        proc.stdin = None  # closed here: communicate() must not flush it
        return running

    def fg(
        self,
        label: str,
        *argv: str,
        stdin: bytes | None = None,
        write: bool = False,
        timeout: float = 300.0,
        config: str | None = None,
        yes: bool = True,
        strict: bool = True,
    ) -> Outcome:
        """Run the real forgectl directly (reads, raw probes). A write needs a verified target and
        is a dry run in preflight mode; in plan mode nothing runs."""
        __tracebackhide__ = True
        step = self._step(label, "forgectl", list(argv), write, stdin)
        step.applied = self._decide(step)
        full = list(step.argv)
        if step.applied == "dry-run" and "--dry-run" not in full:
            full.append("--dry-run")
        if write and yes and "--dry-run" not in full and "--yes" not in full:
            full.append("--yes")
        step.argv = list(full)
        if step.applied in ("plan", "deferred"):
            return Outcome(step)
        self._gate(step)
        cmd = [self.real_forgectl(), *full]
        chosen = config if config is not None else self.settings.forgectl_config
        if chosen:
            cmd.insert(2, f"--config={chosen}")  # after the subcommand, as the gateway puts it
        return self._call(
            step,
            cmd,
            stdin=stdin,
            env=self.child_env(),
            cwd="/",
            timeout=timeout,
            strict=strict,
        )

    # --- state read back ------------------------------------------------------------------------

    def audit_records(self) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        found = []
        for line in self.audit_path.read_text(encoding="utf-8").splitlines():
            try:
                found.append(json.loads(line))
            except ValueError:
                continue
        return found

    def audit_count(self, kind: str = "attempted") -> int:
        return sum(1 for r in self.audit_records() if r.get("result") == kind)

    def wait_for_audit(
        self, kind: str = "attempted", timeout: float = 120.0, after: int = 0
    ) -> bool:
        """Poll (bounded) until more than ``after`` audit records have ``result`` ``kind``. Take
        ``after`` from ``audit_count`` before starting a run, so that the records of earlier runs
        are not taken for its own."""
        deadline = time.monotonic() + timeout
        while True:
            if self.audit_count(kind) > after:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    # --- teardown -------------------------------------------------------------------------------

    def _run_cleanup_cmd(self, command: str, pr: str, item: str) -> str | None:
        """Call the user's cleanup command for one id; the problem as a note, or None."""
        try:
            proc = subprocess.Popen(
                [command, pr, item],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self.child_env(),
                cwd=self.workdir,
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"cleanup command could not be started ({exc.__class__.__name__}) for {item}"
        try:
            code = proc.wait(timeout=self.cleanup_timeout)
        except subprocess.TimeoutExpired:
            self._kill_group(proc)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=GRACE)
            return f"cleanup command timed out after {self.cleanup_timeout:.0f}s for {item}"
        except BaseException:
            self._kill_group(proc)
            raise
        return None if code == 0 else f"cleanup command failed (exit {code}) for {item}"

    def teardown(self) -> list[str]:
        """End what is still running (the exact Popen objects), run the cleanups and the user's
        cleanup command for each ledger id. Never raises for a failing cleanup: a problem becomes
        a note. Returns notes for what is left for a human."""
        notes: list[str] = []
        for running in self._running:
            if running.proc is not None and running.proc.poll() is None:
                self._reap(running.proc)
        for cleanup in reversed(self.cleanups):
            try:
                cleanup()
            except Exception as exc:  # best effort: teardown never hides the test's own failure
                notes.append(f"cleanup step failed: {exc.__class__.__name__}")
        self.cleanups.clear()
        command = self.settings.cleanup_cmd
        for row in self.ledger:
            if command is not None:
                problem = self._run_cleanup_cmd(command, row["pr"], row["id"])
            elif self.settings.keep:
                problem = f"left on {row['pr']}: {row['id']} (PR_COMMENT_LIVE_KEEP=1)"
            else:
                problem = delete_posted(self, row, self.cleanup_call)
            if problem:
                notes.append(problem)
        for row in self.possible:
            notes.append(
                f"possibly posted on {row['pr'] or 'a target'} by step {row['step']}: "
                f"search its comments and reviews for {self.settings.tag}"
            )
        notes.extend(f"left for a human: {text}" for text in self.left)
        notes.extend(self.carry_notes)
        self.carry_notes = []
        return notes


def signal_name(signum: int) -> str:
    return signal.Signals(signum).name


def sorted_ids(doc: Mapping[str, Any] | None, key: str = "ids") -> list[str]:
    return sorted(i for i in (doc or {}).get(key) or [] if isinstance(i, str))
