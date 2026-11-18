"""The review order's write: audit, the forgectl call and its result (docs/interface.md, Review
order steps 9 to 11; issue #29).

``post_review`` runs after the plan, the dialog (or ``--yes``) and the ``--expect-plan`` binding.
It posts exactly what the human confirmed: the call is ``prepared.plan.write_args()`` with
``prepared.plan.input`` on stdin, never a plan rebuilt after the dialog. The order of events::

    pre-post hooks (TODO(#33))  ->  audit ``attempted``  ->  forgectl review --yes  ->
    post-post hooks (TODO(#33))  ->  audit ``outcome``  ->  the result document

Rules that keep a post from happening twice or being lost:

- **Nothing is ever retried.** A timeout, a signal, a killed or unparseable answer is
  ``outcome_unknown`` (or ``lower_contract_mismatch`` with ``landed: null``); the caller reruns the
  same command and forgectl's dedupe key answers ``already_posted``.
- **The audit outcome is written whatever happened** after ``attempted``: success, refusal,
  failure, signal. A failed append after a write is ``audit_failed`` with the writes' own
  ``landed``/``retry`` (never a retry of something that landed).
- A signal during the write ends the run as ``outcome_unknown`` (exit 12), after the outcome record
  holds forgectl's own answer when it had one (``ForgectlInterrupted.result``).
- A forgectl failure keeps forgectl's exit code, code, reason and details (``lower.py``);
  ``backend`` is passed so ``outcome_partial`` is read per backend.

Seams: TODO(#33) the ``pre-post`` and ``post-post`` hooks; TODO(#36) the GitLab review body thread
resolve (``Plan.follow_ups``): until then it is reported as not done (``body_thread_unresolved``);
TODO(#31) the automatic approval guard for ``review --approve``, which the CLI refuses before this
module is reached.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
from .forge import PostRead, read_post
from .gateway import (
    ForgectlInterrupted,
    ForgectlResult,
    Gateway,
    GatewayInternalError,
    GatewayUsageError,
    ToolMissing,
)
from .jsonlog import LogWriter
from .lower import LowerFailure, require_ok, tool_missing_outcome
from .output import Outcome, error_outcome
from .plan import Plan
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
    """A run's failure in ``log.jsonl``: ``refused`` for exit 20, ``warning`` for exit 12. Codes
    and reasons only, never text; other exits are logged where they happen."""
    error = _error_of(outcome)
    if not error:
        return
    details = {"code": error.get("code"), "reason": error.get("reason")}
    if outcome.exit_code == 20:
        log.write("refused", pr=pr, attempt_id=attempt_id, details=details)
    elif outcome.exit_code == 12:
        log.write("warning", pr=pr, attempt_id=attempt_id, details=details)


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
    head = "already posted" if read.already_posted else "posted"
    lines = [
        f"{head}: {plan.action} {(plan.event or '').upper()} on {plan.pr} at {plan.head_sha[:12]}",
        f"inline {len(plan.posted_inline)}, moved {len(plan.moved)}, "
        f"file-level {len(plan.file_level)}, suggestions degraded {len(plan.suggestions_degraded)}",
    ]
    if read.review_id is not None:
        lines.append(f"review {read.review_id}")
    if warnings:
        lines.append("warnings: " + ", ".join(warnings))
    return tuple(lines)


def _record(env: PostEnv, writes: Sequence[WriteRecord], outcome: Outcome) -> Outcome:
    """Append the audit outcome record. On ``AuditFailedError`` the run ends exit 12
    ``audit_failed`` with ``outcome``'s document in ``details.outcome`` and the writes' own
    ``landed``/``retry``: the write may have landed, so nothing here retries it."""
    try:
        env.trail.outcome(writes)
    except AuditFailedError as exc:
        document = outcome.document or {}
        failed = audit_failed_error(exc, document)
        return Outcome(12, failed, outcome.human)
    return outcome


def post_review(prepared: Prepared, env: PostEnv) -> Outcome:
    """Steps 9 to 11 of the review order for a confirmed plan (see the module docstring)."""
    plan = prepared.plan
    # Read once now, so a failure after the write never starts another forgectl call (a signal
    # there would lose the write's result): ``lower.mismatch_outcome`` reads the cached value.
    env.gateway.lower_version()
    # TODO(#33): the pre-post hooks run here (veto: exit 11, escalation: exit 13), before
    # ``attempted``; a veto or escalation before the first write goes to the log only.
    try:
        env.trail.attempted(audit_record(prepared, env))
    except AuditUnavailableError as exc:
        outcome = Outcome(12, audit_unavailable_error(exc))
        log_failure(env.log, env.trail.attempt_id, plan.pr, outcome)
        return outcome
    started = format_ts(now_utc())
    interrupted: ForgectlInterrupted | None = None
    outcome: Outcome
    lowered: Lowered
    try:
        result = env.gateway.call(plan.action, plan.write_args(), stdin=plan.input)
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
    # TODO(#33): the post-post hooks run here, also when the write failed.
    if lowered.failure is not None:
        outcome = lowered.failure
    else:
        assert lowered.read is not None
        warnings = _warnings(prepared, lowered.read)
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
    write = WriteRecord.from_lower(0, started, plan.action, None, plan.dedupe_key, lowered.lower)
    final = _record(env, [write], outcome)
    if final is outcome:
        log_failure(env.log, env.trail.attempt_id, plan.pr, outcome)
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
