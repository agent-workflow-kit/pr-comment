"""The review order's write: audit, the forgectl call and its result (docs/interface.md, Review
order steps 9 to 11; issue #29).

``post_review`` runs after the plan, the dialog (or ``--yes``) and the ``--expect-plan`` binding.
It posts exactly what the human confirmed: the call is ``prepared.plan.write_args()`` with
``prepared.plan.input`` on stdin, never a plan rebuilt after the dialog. The order of events::

    pre-post hooks  ->  audit ``attempted``  ->  forgectl review --yes  ->
    post-post hooks  ->  audit ``outcome``  ->  the result document

``comment`` and ``reply`` (issue #30) take the same path: ``post_text.py`` holds their human lines.

Rules that keep a post from happening twice or being lost:

- **Nothing is ever retried.** A timeout, a signal, a killed or unparseable answer is
  ``outcome_unknown`` (or ``lower_contract_mismatch`` with ``landed: null``); the caller reruns the
  same command and forgectl's dedupe key answers ``already_posted``.
- **The audit outcome is written whatever happened** after ``attempted``: success, refusal,
  failure, signal. A failed append after a write is ``audit_failed`` with the writes' own
  ``landed``/``retry`` (never a retry of something that landed).
- A signal during the write ends the run as ``outcome_unknown`` (exit 12), after the outcome record
  holds forgectl's own answer when it had one (``ForgectlInterrupted.result``).
- SIGINT, SIGTERM and SIGHUP are held from just before the ``attempted`` record until the outcome
  record is appended (``_DeferredSignals``), so a signal never leaves a landed write with only an
  ``attempted`` record. A signal held that way does not change the run: forgectl's answer is
  recorded and the run ends with it (a signal during the forgectl call is the gateway's). A
  signal already held when ``attempted`` returns means nothing was sent: the pair is closed as
  ``failed`` and the run exits 128 + signum with no envelope, before any forgectl write call.
- An unexpected error after ``attempted`` (the call, lowering, the result document) is recorded as
  ``outcome_unknown`` and ends the run as exit 12 ``outcome_unknown``: the write may have landed.
- A forgectl failure keeps forgectl's exit code, code, reason and details (``lower.py``);
  ``backend`` is passed so ``outcome_partial`` is read per backend.

``approve`` and ``review --approve`` use the same write (``Plan.action`` and the plan's args say
which forgectl command); the automatic approval guard (``approve.py``) has run before this module
is reached.

Hooks (``hooks.py``, docs/interface.md Hooks): ``pre-post`` runs once per forgectl write after the
confirmation and before ``attempted`` (a veto exits 11, an escalation 13 unless
``--override-escalation=pr-comment``: nothing was sent and no audit record is written);
``post-post`` runs after the write, also when it failed, and can change nothing. An id a hook
prints joins the write's forgectl call and the audit outcome, never the plan, the caller's ids or
the automatic approval guard (which ran before any hook).

Seam: TODO(#36) the GitLab review body thread resolve (``Plan.follow_ups``): until then it is
reported as not done (``body_thread_unresolved``).
"""

from __future__ import annotations

import hashlib
import os
import signal
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import FrameType, TracebackType
from typing import Any, Final

from .actor import RunContext
from .audit import (
    AuditFailedError,
    AuditInputs,
    AuditTrail,
    AuditUnavailableError,
    LowerResult,
    RunRecord,
    WriteRecord,
    audit_failed_error,
    audit_unavailable_error,
    format_ts,
    now_utc,
)
from .config import LoadedConfig
from .confirm import Interrupted
from .files import StateDirs
from .forge import PostRead, read_post
from .gateway import (
    FORWARDED_SIGNALS,
    ForgectlInterrupted,
    ForgectlResult,
    Gateway,
    GatewayInternalError,
    GatewayUsageError,
    ToolMissing,
)
from .hooks import (
    POST,
    PRE,
    DecisionIds,
    HookResult,
    HookRunner,
    HookSet,
    first_line,
    payload_for,
    run_post,
    run_pre,
)
from .jsonlog import LogWriter
from .lower import LowerFailure, require_ok, tool_missing_outcome
from .output import LOG, Outcome, error_outcome
from .plan import Plan
from .post_text import TEXT_ACTIONS
from .post_text import human_lines as text_lines
from .precheck import DRY_RUN_REASONS
from .prepare import Prepared, result_document

BODY_THREAD_UNRESOLVED: Final = "body_thread_unresolved"
NEUTRALIZING_MISMATCH: Final = "neutralizing_mismatch"


@dataclass(frozen=True)
class PostEnv:
    """What the write needs besides the plan: forgectl, the audit trail, the log, who runs it and
    the config (for ``config_hash`` and ``config_path``)."""

    gateway: Gateway
    trail: AuditTrail
    log: LogWriter
    run: RunContext
    config: LoadedConfig
    # the hooks of this run (``config.hooks_for`` plus ``--hook``), the opened state directories
    # their working directories live in, and the environment they start from (default: ours)
    hooks: HookSet = HookSet()
    dirs: StateDirs | None = None
    environ: Mapping[str, str] | None = None


# --- the audit record of a run ---------------------------------------------------------------


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def audit_record(prepared: Prepared, env: PostEnv) -> RunRecord:
    """The fields both audit records of this run share (hashes, ids and counts, never a body)."""
    plan, request = prepared.plan, prepared.request
    inputs = AuditInputs(
        event=plan.event,
        draft_id=plan.draft_id,
        draft_sha256=prepared.draft_sha256,
        anchor_head=plan.anchor_head,
        reanchor_to=request.reanchor_to,
        pinned_head=plan.head_sha,
        base_ref=plan.base_ref,
        dedupe_key=plan.dedupe_key,
        to=plan.to,
        threads=(),
        bodies=[{"role": b.role, "sha256": _sha256(b.text), "bytes": b.bytes} for b in plan.bodies],
        counts=plan.counts,
        plan_sha256=plan.plan_sha256,
        allowed_mentions=tuple(dict.fromkeys(request.allow_mentions)),
        override_escalation=tuple(dict.fromkeys(request.override_escalation)),
        config_hash=env.config.config_hash,
        config_path=env.config.config_path,
    )
    return RunRecord(
        run=env.run,
        decision_ids=plan.decision_ids,
        action=plan.action,
        target=plan.pr,
        pr_id=plan.pr_id,
        inputs=inputs,
    )


# --- lowering the answer ---------------------------------------------------------------------


def _error_of(outcome: Outcome) -> Mapping[str, Any]:
    document = outcome.document
    error = document.get("error") if document is not None else None
    return error if isinstance(error, dict) else {}  # pyright: ignore[reportUnknownVariableType]


def lower_result_of(outcome: Outcome, read: PostRead | None) -> LowerResult:
    """The audit's ``writes[].lower`` for a write that ended in ``outcome``.

    ``lower_contract_mismatch`` is forgectl's own answer made unusable: it is recorded by what is
    known of the write. Landed (``exit 0`` with a bad document) is ``done``; unknown is
    ``outcome_unknown`` (an answer with an unknown exit status, so the record never says ``failed``
    for something that may have landed, which would offer a rerun as safe).
    """
    if read is not None:
        return LowerResult(
            exit=0,
            code=None,
            reason=None,
            ids=tuple(read.ids),
            thread_ids=tuple(read.thread_ids),
            review_id=read.review_id,
            already_posted=read.already_posted,
        )
    error = _error_of(outcome)
    code, reason = error.get("code"), error.get("reason")
    if code == "lower_contract_mismatch":
        if error.get("landed") is True:
            return LowerResult(exit=0, code=None, reason=None)
        if error.get("landed") is None:
            return LowerResult(exit=12, code="outcome_unknown", reason=None)
    details: object = error.get("details")
    leftover = (
        code == "host_rejected" and isinstance(details, dict) and "leftover_drafts" in details
    )
    return LowerResult(
        exit=outcome.exit_code,
        code=code if isinstance(code, str) else None,
        reason=reason if isinstance(reason, str) else None,
        leftover_drafts=leftover,
    )


@dataclass(frozen=True)
class Lowered:
    """forgectl's answer to the write: ``read`` on success, else ``failure`` (the run's outcome)."""

    read: PostRead | None
    failure: Outcome | None
    lower: LowerResult


def lower_answer(result: ForgectlResult, plan: Plan, gateway: Gateway) -> Lowered:
    """The checked answer of the real call. Every field forgectl printed goes through
    ``forge.PostRead``; a failure keeps forgectl's code, reason and details (``lower.py``)."""
    try:
        document = require_ok(
            result,
            gateway=gateway,
            backend=plan.backend,
            reasons=DRY_RUN_REASONS[plan.action],
        )
        read = read_post(document, command=plan.action, pr=plan.pr, gateway=gateway)
    except LowerFailure as failure:
        return Lowered(None, failure.outcome, lower_result_of(failure.outcome, None))
    return Lowered(read, None, lower_result_of(Outcome(0), read))


# --- the write -------------------------------------------------------------------------------


def log_failure(log: LogWriter, attempt_id: str | None, pr: str | None, outcome: Outcome) -> None:
    """A run's failure in ``log.jsonl`` (docs/interface.md, Audit and log): ``refused`` for exit 20,
    ``vetoed`` for exit 11, ``escalated`` for exit 13 (level ``warn``), ``warning`` for exit 12
    (``warn``) and for exit 1 (level ``error``). Codes and reasons only, never text. Other exits
    are logged where they happen (a declined dialog)."""
    error = _error_of(outcome)
    if not error:
        return
    code = error.get("code")
    details = {"code": code, "reason": error.get("reason")}
    exit_code = outcome.exit_code
    if exit_code == 20:
        log.write("refused", pr=pr, attempt_id=attempt_id, details=details)
    elif exit_code == 12:
        log.write("warning", pr=pr, attempt_id=attempt_id, details=details)
    elif exit_code == 11 and code == "vetoed":
        log.write("vetoed", pr=pr, attempt_id=attempt_id, details=details)
    elif exit_code == 13 and code == "escalated":
        log.write("escalated", pr=pr, attempt_id=attempt_id, details=details)
    elif exit_code == 1:
        log.write("warning", level="error", pr=pr, attempt_id=attempt_id, details=details)


def _warnings(prepared: Prepared, read: PostRead) -> list[str]:
    """forgectl's warnings, then pr-comment's own: the pre-check's, one for forgectl neutralizing
    text pr-comment already neutralized, and the unresolved GitLab body thread (TODO(#36))."""
    out: list[str] = list(read.warnings)
    out += prepared.precheck.warnings
    if read.neutralized_lines or read.markers_removed:
        out.append(NEUTRALIZING_MISMATCH)
    if prepared.plan.follow_ups:
        out.append(BODY_THREAD_UNRESOLVED)
    return list(dict.fromkeys(out))


def _human_lines(prepared: Prepared, read: PostRead, warnings: Sequence[str]) -> tuple[str, ...]:
    plan = prepared.plan
    if plan.action in TEXT_ACTIONS:
        return text_lines(prepared, read, warnings)
    head = "already posted" if read.already_posted else "posted"
    what = (
        plan.action if plan.action == "approve" else f"{plan.action} {(plan.event or '').upper()}"
    )
    lines = [f"{head}: {what} on {plan.pr} at {plan.head_sha[:12]}"]
    if plan.action == "review":
        lines.append(
            f"inline {len(plan.posted_inline)}, moved {len(plan.moved)}, "
            f"file-level {len(plan.file_level)}, "
            f"suggestions degraded {len(plan.suggestions_degraded)}"
        )
    if read.review_id is not None:
        lines.append(f"review {read.review_id}")
    if read.head_verified is False:
        lines.append(
            "head_verified is false: an earlier approval exists that forgectl cannot confirm "
            "counts for this head and base"
        )
    if warnings:
        lines.append("warnings: " + ", ".join(warnings))
    return tuple(lines)


def _record(
    env: PostEnv,
    writes: Sequence[WriteRecord],
    outcome: Outcome,
    hook_decision_ids: Sequence[str] = (),
) -> Outcome:
    """Append the audit outcome record. On ``AuditFailedError`` the run ends exit 12
    ``audit_failed`` with ``outcome``'s document in ``details.outcome`` and the writes' own
    ``landed``/``retry``: the write may have landed, so nothing here retries it."""
    try:
        env.trail.outcome(writes, hook_decision_ids)
    except AuditFailedError as exc:
        document = outcome.document or {}
        failed = audit_failed_error(exc, document)
        return Outcome(12, failed, outcome.human)
    return outcome


class _DeferredSignals:
    """Holds SIGINT, SIGTERM and SIGHUP (main thread only) while the audit pair of a write is open.

    The handler only records the number. Nothing is raised or re-sent afterwards: the run finishes
    its record and ends with its own answer. A signal that was ignored on entry stays ignored.
    """

    def __init__(self) -> None:
        self.received: list[int] = []
        self._saved: dict[int, Any] = {}

    def _handle(self, signum: int, _frame: FrameType | None) -> None:
        self.received.append(signum)

    def __enter__(self) -> _DeferredSignals:
        if threading.current_thread() is threading.main_thread():
            for sig in FORWARDED_SIGNALS:
                if signal.getsignal(sig) == signal.SIG_IGN:
                    continue
                self._saved[sig] = signal.signal(sig, self._handle)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        for sig, previous in self._saved.items():
            signal.signal(sig, signal.SIG_DFL if previous is None else previous)


# --- the hooks of the write ---------------------------------------------------------------------


def _why(result: HookResult) -> str:
    """A fixed phrase for how a hook ended (never hook output)."""
    if result.reason == "timeout":
        return "timed out"
    if result.reason.startswith("signal_"):
        return f"killed by signal {result.reason[7:]}"
    if result.reason.startswith("exit_"):
        return f"exit {result.reason[5:]}"
    return result.reason.replace("_", " ")


def _write_result(lowered: Lowered, warnings: Sequence[str]) -> dict[str, Any]:
    """The ``result`` of a ``post-post`` payload (``schemas/hooks/post-post.json``)."""
    lower, read = lowered.lower, lowered.read
    landed = lower.exit == 0
    return {
        "exit": lower.exit,
        "code": lower.code,
        "reason": lower.reason,
        "ids": list(lower.ids) if landed else [],
        "thread_ids": list(lower.thread_ids) if landed else [],
        "review_id": lower.review_id if landed else None,
        "already_posted": lower.already_posted if landed else None,
        "neutralized_lines": read.neutralized_lines if read is not None else None,
        "head_verified": read.head_verified if read is not None else None,
        "changed": None,
        "warnings": list(warnings) if landed else [],
    }


class _WriteHooks:
    """The hooks of the run's one forgectl write (index 0). The ids they print are kept apart from
    the plan's: ``plan.decision_ids`` (the caller's) is never touched."""

    def __init__(self, prepared: Prepared, env: PostEnv) -> None:
        self._prepared = prepared
        self._env = env
        plan = prepared.plan
        self.ids = DecisionIds(plan.decision_ids)
        self.runner = HookRunner(
            env.hooks,
            dirs=env.dirs,
            environ=os.environ if env.environ is None else env.environ,
            log=env.log,
            pr=plan.pr,
            attempt_id=env.trail.attempt_id,
        )

    def _payload(
        self,
        hook: str,
        entry_text: bool,
        decision_ids: Sequence[str],
        result: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        request = self._prepared.request
        return payload_for(
            self._prepared.plan,
            hook=hook,
            attempt_id=self._env.trail.attempt_id,
            write_index=0,
            decision_ids=decision_ids,
            override_escalation=request.override_escalation,
            with_text=entry_text,
            result=result,
        )

    def write_args(self) -> list[str]:
        """The plan's arguments plus one ``--decision-id`` per id a hook printed."""
        extra = [f"--decision-id={item}" for item in self.ids.from_hooks]
        return [*self._prepared.plan.write_args(), *extra]

    def pre(self) -> Outcome | None:
        """Run the ``pre-post`` hooks: None to go on, else the run's outcome (veto or escalation:
        nothing was sent, and with no write there is no audit record, only the log)."""
        if not self._env.hooks.pre:
            return None
        verdict = run_pre(
            self.runner,
            self.ids,
            lambda entry, ids: self._payload(PRE, entry.with_text, ids, None),
        )
        found = tuple(self.ids.from_hooks)
        if verdict.vetoed is not None:
            vetoed = verdict.vetoed
            message = f"pre-post hook {vetoed.index} vetoed the write ({_why(vetoed)})"
            detail = first_line(vetoed.stderr)
            if detail:
                message += f": {detail}"
            return error_outcome(
                "vetoed", message, hook=PRE, hook_stderr=vetoed.stderr, decision_ids=found
            )
        if not verdict.escalated:
            return None
        first = verdict.escalated[0]
        hooks = [{"hook": PRE, "index": item.index} for item in verdict.escalated]
        if "pr-comment" in self._prepared.request.override_escalation:
            self._env.log.write(
                "escalated",
                level="info",
                pr=self._prepared.plan.pr,
                attempt_id=self._env.trail.attempt_id,
                details={"overridden": True, "hooks": hooks},
            )
            return None
        message = (
            f"pre-post hook {first.index} escalated: a human must decide; "
            "rerun with --override-escalation=pr-comment to allow"
        )
        detail = first_line(first.stderr)
        if detail:
            message += f": {detail}"
        return error_outcome(
            "escalated",
            message,
            hook=PRE,
            hook_stderr=first.stderr,
            escalated_by="pr-comment",
            escalated_hooks=hooks,
            decision_ids=found,
        )

    def post(
        self,
        lowered: Lowered,
        warnings: Sequence[str],
        *,
        skip: bool,
        interrupted: Callable[[], bool],
    ) -> None:
        """Run the ``post-post`` hooks (also after a failed write). Never changes the outcome.
        ``interrupted()`` is true once a signal was held: no further hook starts."""
        if not self._env.hooks.post:
            return
        if skip:
            self.runner.warn(POST, None, "hooks_skipped_after_signal")
            return
        result = _write_result(lowered, warnings)
        run_post(
            self.runner,
            self.ids,
            lambda entry, ids: self._payload(POST, entry.with_text, ids, result),
            interrupted=interrupted,
        )


def post_review(prepared: Prepared, env: PostEnv) -> Outcome:
    """Steps 9 to 11 of the review order for a confirmed plan (see the module docstring)."""
    # Read once now, so a failure after the write never starts another forgectl call (a signal
    # there would lose the write's result): ``lower.mismatch_outcome`` reads the cached value.
    env.gateway.lower_version()
    # The ``pre-post`` hooks run before ``attempted`` and before signals are held: a signal during
    # one ends the run (128 + signum, nothing was sent). A veto or escalation before the first
    # write goes to the log only. An id a hook prints never feeds the automatic approval guard
    # (``approve.py``): it has already run, on the caller's ``--decision-id`` flags only.
    hooks = _WriteHooks(prepared, env)
    blocked = hooks.pre()
    if blocked is not None:
        log_failure(env.log, env.trail.attempt_id, prepared.plan.pr, blocked)
        return blocked
    with _DeferredSignals() as held:
        return _post_with_audit(prepared, env, held, hooks)


def _call_and_lower(
    prepared: Prepared, env: PostEnv, hooks: _WriteHooks, held: _DeferredSignals
) -> tuple[Lowered, Outcome]:
    """The forgectl write and what it means: forgectl's lowered answer and the run's outcome."""
    plan = prepared.plan
    interrupted: ForgectlInterrupted | None = None
    lowered: Lowered
    try:
        result = env.gateway.call(plan.action, hooks.write_args(), stdin=plan.input)
    except ForgectlInterrupted as exc:
        interrupted = exc
        result = exc.result
        lowered = lower_answer(result, plan, env.gateway)
    except ToolMissing as exc:  # nothing was sent: forgectl could not be started
        failure = tool_missing_outcome(exc)
        lowered = Lowered(None, failure, LowerResult(exit=12, code="tool_missing", reason=None))
    except (GatewayInternalError, GatewayUsageError) as exc:
        # a bug before forgectl ran: record the failed write, then let the run end as ``internal``
        lowered = Lowered(
            None, error_outcome("internal", type(exc).__name__), LowerResult(1, "internal", None)
        )
    else:
        lowered = lower_answer(result, plan, env.gateway)
    warnings = () if lowered.read is None else _warnings(prepared, lowered.read)
    # After a signal the user wants out: the ``post-post`` hooks do not start. Otherwise they run
    # whatever the write's result was, and nothing they do changes it.
    hooks.post(
        lowered,
        warnings,
        skip=interrupted is not None or bool(held.received),
        interrupted=lambda: bool(held.received),
    )
    if lowered.failure is not None:
        outcome = lowered.failure
    else:
        assert lowered.read is not None
        if lowered.read.neutralized_lines or lowered.read.markers_removed:
            env.log.write(
                NEUTRALIZING_MISMATCH,
                pr=plan.pr,
                attempt_id=env.trail.attempt_id,
                details={
                    "neutralized_lines": lowered.read.neutralized_lines,
                    "markers_removed": lowered.read.markers_removed,
                },
            )
        document = result_document(
            prepared,
            dry_run=False,
            posted=lowered.read,
            warnings=warnings,
            body_thread_resolved=False if plan.follow_ups else None,
        )
        outcome = Outcome(0, document, _human_lines(prepared, lowered.read, warnings))
    if interrupted is not None:
        # The run was interrupted: whatever forgectl left is in the audit outcome; the run ends as
        # outcome_unknown with forgectl's envelope when it printed one. A rerun answers
        # ``already_posted`` through the dedupe key.
        outcome = _interrupted_outcome(interrupted, lowered)
    return lowered, outcome


def _post_with_audit(
    prepared: Prepared, env: PostEnv, held: _DeferredSignals, hooks: _WriteHooks
) -> Outcome:
    plan = prepared.plan
    try:
        env.trail.attempted(audit_record(prepared, env))
    except AuditUnavailableError as exc:
        outcome = Outcome(12, audit_unavailable_error(exc))
        log_failure(env.log, env.trail.attempt_id, plan.pr, outcome)
        return outcome
    started = format_ts(now_utc())
    if held.received:
        # A signal arrived during the ``attempted`` append: nothing was sent and nothing will be.
        # Close the pair as a failed write (exit 1, ``internal``: no forgectl call, no code of its
        # own), then end 128 + signum with no envelope, as a signal before any write does.
        unsent = LowerResult(exit=1, code="internal", reason=None)
        write = WriteRecord.from_lower(0, started, plan.action, None, plan.dedupe_key, unsent)
        stopped = Outcome(1)
        final = _record(env, [write], stopped)
        if final is not stopped:  # the pair could not be closed: report that
            log_failure(env.log, env.trail.attempt_id, plan.pr, final)
            return final
        raise Interrupted(held.received[0])
    try:
        lowered, outcome = _call_and_lower(prepared, env, hooks, held)
    except Exception as exc:  # after ``attempted`` nothing is known: the write may have landed
        LOG.error("unexpected %s after the write was attempted", type(exc).__name__, exc_info=exc)
        outcome = error_outcome(
            "outcome_unknown",
            f"unexpected {type(exc).__name__} after the write was attempted; "
            "whether it posted is unknown",
        )
        lowered = Lowered(None, outcome, LowerResult(exit=12, code="outcome_unknown", reason=None))
    write = WriteRecord.from_lower(0, started, plan.action, None, plan.dedupe_key, lowered.lower)
    if outcome.exit_code == 0 and plan.follow_ups:
        # the main write landed and its follow-up was not performed (TODO(#36)): partial, landed
        write = replace(write, result="partial")
    final = _record(env, [write], outcome, hooks.ids.from_hooks)
    if final is not outcome:  # the audit failure replaced the outcome: the original is logged too
        log_failure(env.log, env.trail.attempt_id, plan.pr, outcome)
    log_failure(env.log, env.trail.attempt_id, plan.pr, final)
    return final


def _interrupted_outcome(exc: ForgectlInterrupted, lowered: Lowered) -> Outcome:
    """Exit 12 ``outcome_unknown`` for a write interrupted by a signal (docs/interface.md,
    forgectl edge: Signals)."""
    lower_error: Mapping[str, Any] | None = None
    if lowered.failure is not None:
        found = _error_of(lowered.failure).get("lower_error")
        lower_error = found if isinstance(found, dict) else None  # pyright: ignore[reportUnknownVariableType]
    return error_outcome(
        "outcome_unknown",
        f"interrupted by signal {exc.signum} during the write; whether it posted is unknown",
        lower_error=lower_error,
    )
