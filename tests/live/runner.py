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

from .pty_run import run_on_pty
from .settings import REAL_ENV, LiveSettings, redact, repo_of_ref

PLACEHOLDER_RE = re.compile(r"^<[a-z][a-z_0-9]*>$")
PC_WRITES = frozenset({"review", "comment", "reply", "approve", "resolve", "unresolve"})
TAP_SOURCE = """#!{python}
import json, os, sys, time
log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calls.jsonl")
with open(log, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({{"ts": time.time(), "argv": sys.argv[1:]}}) + "\\n")
os.execv({real!r}, [{real!r}, *sys.argv[1:]])
"""


class LiveFailure(AssertionError):
    """A check of the live suite failed (a host behaved unexpectedly)."""


class LiveRefusal(Exception):
    """The suite refused to do something unsafe (a write to a target that is not verified)."""


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
        if self.proc is None:
            return Outcome(self.step)
        try:
            out, err = self.proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()  # this exact child
            out, err = self.proc.communicate()
        return self.runner._outcome(self.step, self.proc.returncode, out, err)


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
        self.ledger: list[dict[str, str]] = []
        self.observations: list[dict[str, Any]] = []
        self.cleanups: list[Callable[[], None]] = []
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
        if self._tap is None:
            return []
        log = self._tap.parent / "calls.jsonl"
        if not log.exists():
            return []
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        return [row["argv"] for row in rows]

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
        env = {k: v for k, v in self.env.items() if not k.startswith("PR_COMMENT_")}
        env["XDG_STATE_HOME"] = str(self.state_home)
        env.update(extra or {})
        return env

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

    def verify_target(self, ref: str) -> None:
        """Accept ``ref`` as a write target: its repo is allowlisted as written, and (outside plan
        mode) forgectl's canonical repo, from ``show``, is too."""
        repo = repo_of_ref(ref)
        if self.settings.mode == "plan" and not self.settings.allow:
            self.verified.add(
                ref
            )  # plan mode calls nothing; the allowlist is checked once it is set
            return
        if not self.settings.target_allowed(repo):
            raise LiveRefusal("a target repo is not on PR_COMMENT_LIVE_ALLOW; nothing was called")
        if self.settings.mode != "plan":
            out = self.fg("verify-target", "show", "--json", ref)
            canonical = (out.doc or {}).get("repo") if out.real else None
            if not out.real or out.code != 0 or not isinstance(canonical, str):
                raise LiveRefusal("forgectl show could not confirm a target's repo")
            if not self.settings.target_allowed(canonical):
                raise LiveRefusal("a target's canonical repo is not on PR_COMMENT_LIVE_ALLOW")
        self.verified.add(ref)

    def allow_ref_text(self, ref: str) -> None:
        """Accept a PR reference that names no existing PR (a refusal probe): its repo, as
        written, must be allowlisted; there is nothing for ``show`` to confirm."""
        if self.settings.mode == "plan" and not self.settings.allow:
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

    # --- running --------------------------------------------------------------------------------

    def _outcome(
        self, step: Step, code: int, out: bytes, err: bytes, *, strict: bool = True
    ) -> Outcome:
        text = out.decode("utf-8", "replace")
        outcome = Outcome(
            step, code, None, text, redact(err.decode("utf-8", "replace"), self.env), real=True
        )
        if text.strip() and strict:
            try:
                loaded = json.loads(text)
            except ValueError as exc:
                raise LiveFailure(
                    f"{step.label}: stdout is not one JSON document ({exc.__class__.__name__}); "
                    "first 200 characters: " + redact(text[:200], self.env)
                ) from None
            outcome.doc = loaded if isinstance(loaded, dict) else {"value": loaded}
        if step.write and step.applied == "real" and code == 0 and outcome.doc:
            self._ledger(step, outcome.doc)
        return outcome

    def _ledger(self, step: Step, doc: Mapping[str, Any]) -> None:
        ids: list[str] = [i for i in doc.get("ids") or [] if isinstance(i, str)]
        ids += [i for i in doc.get("thread_ids") or [] if isinstance(i, str)]
        review = doc.get("review_id")
        if isinstance(review, str):
            ids.append(review)
        ref = next((a for a in step.argv if a in self.verified), "")
        for item in dict.fromkeys(ids):
            row = {"run": self.settings.run_id, "scenario": step.scenario, "pr": ref, "id": item}
            self.ledger.append(row)
            report = self.settings.report_dir
            if report is not None:
                report.mkdir(parents=True, exist_ok=True)
                with (report / f"ledger-{self.settings.run_id}.jsonl").open(
                    "a", encoding="utf-8"
                ) as h:
                    h.write(json.dumps(row) + "\n")

    def _decide(self, step: Step) -> str:
        if self.settings.mode == "plan":
            return "plan"
        if any(PLACEHOLDER_RE.match(a) for a in step.argv):
            return "deferred"
        if step.write and self.settings.mode == "preflight":
            return "dry-run"
        return "real" if step.write else "read"

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
        done = subprocess.run(
            cmd,
            input=stdin if stdin is not None else b"",
            capture_output=True,
            env=self.child_env(env_extra),
            cwd=self.workdir,
            timeout=timeout,
            check=False,
        )
        return self._outcome(step, done.returncode, done.stdout, done.stderr)

    def pc_pty(
        self, label: str, *argv: str, answer: bytes = b"y\n", timeout: float = 120.0
    ) -> Outcome:
        """A write the way a human runs it: stdin and stderr are a pty, the confirmation dialog is
        shown and ``answer`` is typed at its prompt. Plan and preflight mode run it as a plain
        (dry-run) call, since no dialog is needed."""
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
        result = run_on_pty(
            cmd, env=self.child_env(), cwd=self.workdir, answer=answer, deadline=timeout
        )
        outcome = Outcome(
            step, result.code, result.document, "", redact(result.terminal, self.env), real=True
        )
        if result.code == 0 and result.document:
            self._ledger(step, result.document)
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
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.child_env(),
            cwd=self.workdir,
        )
        assert proc.stdin is not None
        proc.stdin.write(stdin or b"")
        proc.stdin.close()
        proc.stdin = None  # closed here: communicate() must not flush it
        running = Running(step, proc, self)
        self._running.append(running)
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
        done = subprocess.run(
            cmd,
            input=stdin if stdin is not None else b"",
            capture_output=True,
            env=self.child_env(),
            cwd="/",
            timeout=timeout,
            check=False,
        )
        return self._outcome(step, done.returncode, done.stdout, done.stderr, strict=strict)

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

    def wait_for_audit(
        self, kind: str = "attempted", timeout: float = 120.0, count: int = 1
    ) -> bool:
        """Poll (bounded) until at least ``count`` audit records have ``result`` ``kind``."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if sum(1 for r in self.audit_records() if r.get("result") == kind) >= count:
                return True
            time.sleep(0.01)
        return False

    # --- teardown -------------------------------------------------------------------------------

    def teardown(self) -> list[str]:
        """End what is still running (the exact Popen objects), run the cleanups and the user's
        cleanup command for each ledger id. Returns notes for what is left for a human."""
        notes: list[str] = []
        for running in self._running:
            if running.proc is not None and running.proc.poll() is None:
                running.proc.kill()
                running.proc.communicate()
        for cleanup in reversed(self.cleanups):
            try:
                cleanup()
            except Exception as exc:  # best effort: teardown never hides the test's own failure
                notes.append(f"cleanup step failed: {exc.__class__.__name__}")
        self.cleanups.clear()
        command = self.settings.cleanup_cmd
        for row in self.ledger:
            if command is None:
                notes.append(f"left on {row['pr']}: {row['id']} (set PR_COMMENT_LIVE_CLEANUP_CMD)")
                continue
            done = subprocess.run(
                [command, row["pr"], row["id"]],
                capture_output=True,
                env=self.child_env(),
                cwd=self.workdir,
                timeout=120,
                check=False,
            )
            if done.returncode != 0:
                notes.append(f"cleanup command failed (exit {done.returncode}) for {row['id']}")
        return notes


def signal_name(signum: int) -> str:
    return signal.Signals(signum).name


def sorted_ids(doc: Mapping[str, Any] | None, key: str = "ids") -> list[str]:
    return sorted(i for i in (doc or {}).get(key) or [] if isinstance(i, str))
