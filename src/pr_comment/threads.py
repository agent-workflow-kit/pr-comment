"""``resolve`` and ``unresolve`` (docs/interface.md: Resolve order, Resolve result; issue #35).

Real writes on review threads through forgectl. The order is::

    show -> comments -> whoami  ->  [--dry-run: print]  ->  one dialog  ->
    per thread to change:  pre-post -> audit ``attempted`` (first write) ->
    forgectl resolve|unresolve -> post-post  ->  audit outcome  ->  the result document

``prepare_threads`` sends nothing: it reads, then sorts every given thread into one of *to change*,
*already in the requested state* (``changed: false``, no write) and *failed before any write*
(``not_found`` for a thread absent from a complete listing, ``not_resolvable``). A thread absent
from an incomplete listing stays to change with its author, path and line unknown: forgectl's own
lookup decides. pr-comment never runs forgectl's ``--dry-run`` for these commands (forgectl's dry
run of ``resolve`` only reports a refusal), so a dry run here is built from the reads alone.

``write_threads`` is the write path of ``post.post_review`` run once per thread, with one audit pair
for the run (its ``writes`` has one entry per thread that reached its ``pre-post`` hooks after the
first ``attempted``):

- **Nothing is ever retried.** A timeout, a signal, a killed or unparseable answer is
  ``outcome_unknown`` (or ``lower_contract_mismatch`` with ``landed: null``) for that thread.
- **After any exit 12 the run stops.** The host, forgectl or the audit trail is in doubt, so the
  threads after it are not attempted (``resolved`` and ``changed`` null, no error of their own);
  the exit code and error are the first failing thread's. A refusal (20), a missing thread (10), a
  veto (11) or an escalation (13) is only that thread's: the next thread continues.
- SIGINT, SIGTERM and SIGHUP are held from the first thread's hooks until the audit outcome is
  appended. A signal held that way ends the run after the write in progress: the threads not yet
  tried are not attempted, the outcome is recorded and the run exits ``128 + signum`` with no
  document. A signal already held when ``attempted`` returns means nothing was sent: the pair is
  closed as ``failed`` and the run exits ``128 + signum``. A signal during the forgectl call is the
  gateway's: that thread is ``outcome_unknown``.
- A veto or escalation before the first write goes to the log only (no audit record exists yet);
  after it, the thread is a ``vetoed`` or ``escalated`` entry of ``writes``.
- The ids a hook prints join that thread's forgectl call and the audit outcome's
  ``hook_decision_id`` (at most 32 in all), never the caller's ids.

Open decisions are marked where they apply in ``post.py``; none is added here.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, ValidationError

from .audit import (
    AuditInputs,
    AuditUnavailableError,
    LowerResult,
    RunRecord,
    WriteRecord,
    audit_unavailable_error,
    format_ts,
    now_utc,
)
from .confirm import GUTTER, Interrupted, _gutter, confirm_with, encodable
from .display import sanitize_line
from .forge import Forge, Pr, Show, _why
from .gateway import (
    ForgectlInterrupted,
    ForgectlResult,
    Gateway,
    GatewayInternalError,
    GatewayUsageError,
    ToolMissing,
)
from .grammars import GrammarError
from .hooks import (
    POST,
    PRE,
    DecisionIds,
    HookRunner,
    ThreadTarget,
    payload_for_thread,
    run_post,
    run_pre,
)
from .lower import LowerFailure, mismatch_outcome, require_ok, tool_missing_outcome
from .output import LOG, Outcome, error_outcome
from .plan import empty_counts
from .post import (
    PostEnv,
    _DeferredSignals,
    _error_of,
    _interrupted_outcome,
    _record,
    log_failure,
    lower_result_of,
    pre_outcome,
)
from .prepare import Env

EXCERPT_CHARS: Final = 200
THREAD_REASONS: Final = frozenset({"not_resolvable", "unknown_host"})
MAX_HOOK_DECISION_IDS: Final = 32


@dataclass(frozen=True)
class ThreadRequest:
    """``resolve`` or ``unresolve`` as the CLI parsed it (each id already checked against the
    ``gh:rt:`` and ``gl:d:`` grammar, at most 100)."""

    command: str
    pr: str
    actor: str
    threads: tuple[str, ...]
    decision_ids: tuple[str, ...] = ()
    override_escalation: tuple[str, ...] = ()

    @property
    def want_resolved(self) -> bool:
        return self.command == "resolve"


@dataclass
class Item:
    """One given thread: what the reads showed, then what the write did."""

    thread_id: str
    target: ThreadTarget
    seen: bool = False  # listed by ``comments``
    author: str = "unknown"  # ``you``, ``another account`` or ``unknown``
    login: str | None = None
    excerpt: str | None = None
    noop: bool = False  # already in the requested state: no write
    failure: Outcome | None = None  # before or at the write
    resolved: bool | None = None
    changed: bool | None = None

    @property
    def to_change(self) -> bool:
        """A thread that reached the write step (it may still fail there)."""
        return self.failure is None and not self.noop


@dataclass
class ThreadRun:
    """The reads of a run and the sorted threads, in the order given with duplicates dropped."""

    request: ThreadRequest
    show: Show
    head_sha: str
    items: list[Item] = field(default_factory=lambda: [])

    def to_change(self) -> list[Item]:
        return [item for item in self.items if item.to_change]


# --- the reads ---------------------------------------------------------------------------------


def _not_found(thread_id: str) -> Outcome:
    return error_outcome(
        "not_found",
        "the thread was not found on this pull request",
        details={"what": "thread", "id": thread_id},
    )


def _not_resolvable(command: str) -> Outcome:
    return error_outcome(
        "refused",
        f"only review threads can be {command}d: this one is not resolvable",
        reason="not_resolvable",
    )


def _self_id(forge: Forge, show: Show, warn: Callable[[str], None]) -> str | None:
    """The authenticated account's id on the PR's host, or None (a warning): a ``whoami`` that
    fails, is partial or has no id for the host is not fatal."""
    try:
        me = forge.whoami(show.host)
    except (LowerFailure, GrammarError):
        warn("whoami_failed")
        return None
    if me.partial or me.id is None:
        warn("whoami_partial")
        return None
    return me.id


def prepare_threads(request: ThreadRequest, env: Env, warn: Callable[[str], None]) -> ThreadRun:
    """Step 2 of the Resolve order: ``show``, ``comments``, ``whoami``, then every thread sorted.
    Sends nothing. A failed ``show`` or ``comments`` ends the run (``LowerFailure``)."""
    if env.before_reads is not None:
        env.before_reads()
    forge = Forge(env.gateway)
    show = forge.show(request.pr)
    listing = forge.comments(show.pr)
    self_id = _self_id(forge, show, warn)
    listed = {thread.id: thread for thread in listing.threads}
    run = ThreadRun(request, show, listing.head_sha)
    for thread_id in dict.fromkeys(request.threads):
        thread = listed.get(thread_id)
        if thread is None:
            item = Item(thread_id, ThreadTarget(thread_id))
            if listing.complete:
                item.failure = _not_found(thread_id)
            run.items.append(item)
            continue
        first = thread.comments[0].author
        is_self = None if self_id is None or first.id is None else first.id == self_id
        item = Item(
            thread_id,
            ThreadTarget(thread_id, is_self, thread.path, thread.line),
            seen=True,
            author="unknown" if is_self is None else ("you" if is_self else "another account"),
            login=first.login,
            excerpt=thread.comments[0].body[:EXCERPT_CHARS],
        )
        if not thread.resolvable:
            item.failure = _not_resolvable(request.command)
        elif thread.resolved == request.want_resolved:
            item.noop = True
        run.items.append(item)
    return run


# --- the result --------------------------------------------------------------------------------


def _error_document(outcome: Outcome) -> dict[str, Any]:
    error = _error_of(outcome)
    return dict(error)


def result_document(run: ThreadRun, *, dry_run: bool) -> dict[str, Any]:
    """The ``--json`` result (``schemas/cli/resolve.json``, ``unresolve.json``). On a dry run
    ``resolved`` and ``changed`` are the intended change."""
    want = run.request.want_resolved
    threads: list[dict[str, Any]] = []
    for item in run.items:
        entry: dict[str, Any]
        if item.failure is not None:
            entry = {
                "thread_id": item.thread_id,
                "resolved": None,
                "changed": None,
                "error": _error_document(item.failure),
            }
        elif item.noop:
            entry = {"thread_id": item.thread_id, "resolved": want, "changed": False, "error": None}
        elif dry_run:
            changed = True if item.seen else None
            entry = {
                "thread_id": item.thread_id,
                "resolved": want,
                "changed": changed,
                "error": None,
            }
        else:
            entry = {
                "thread_id": item.thread_id,
                "resolved": item.resolved,
                "changed": item.changed,
                "error": None,
            }
        threads.append(entry)
    return {
        "pr": run.show.pr,
        "action": run.request.command,
        "dry_run": dry_run,
        "threads": threads,
    }


def _first_failure(run: ThreadRun) -> Outcome | None:
    return next((item.failure for item in run.items if item.failure is not None), None)


def _line(text: str) -> str:
    return sanitize_line(text)


def finish(run: ThreadRun, *, dry_run: bool) -> Outcome:
    """The run's outcome: exit 0, or the first failing thread's exit code, with the result
    document either way and the human lines (``--quiet`` keeps the failures)."""
    request = run.request
    verb = request.command
    past = f"{verb}d"
    human: list[str] = []
    errors: list[str] = []
    if dry_run:
        human.append("dry run: nothing was sent")
    for item in run.items:
        tid = _line(item.thread_id)
        if item.failure is not None:
            message = _error_document(item.failure).get("message", "failed")
            errors.append(f"thread {tid}: {_line(str(message))}")
        elif item.noop:
            human.append(f"thread {tid}: already {past}, nothing to do")
        elif dry_run:
            human.append(f"thread {tid}: would be {past}")
        elif item.changed is None and item.resolved is None:
            errors.append(f"thread {tid}: not attempted")
        elif item.changed is False:
            human.append(f"thread {tid}: already {past} (forgectl made no change)")
        else:
            human.append(f"thread {tid}: {past}")
    failure = _first_failure(run)
    return Outcome(
        0 if failure is None else failure.exit_code,
        result_document(run, dry_run=dry_run),
        tuple(human),
        tuple(errors) if failure is not None else (),
    )


# --- the dialog --------------------------------------------------------------------------------


def _who(item: Item) -> str:
    if item.author == "another account" and item.login:
        return f"another account ({_line(item.login)})"
    return item.author


def dialog_text(run: ThreadRun, width: int, encoding: str = "utf-8") -> str:
    """The one dialog: every thread to change with its first author, ``path:line`` and the first
    200 characters of its first comment (untrusted: display-sanitized, behind the gutter)."""
    request = run.request
    todo = run.to_change()
    out = [f"== {request.command} =="]
    for item in todo:
        where = ""
        if item.target.path is not None:
            where = f" at {_line(item.target.path)}"
            if item.target.line is not None:
                where += f":{item.target.line}"
        out.append(f"--- {_line(item.thread_id)}, first comment by {_who(item)}{where}")
        if item.excerpt is None:
            out.append(f"{GUTTER}(not in the listing: forgectl looks the thread up itself)")
        else:
            out += _gutter(item.excerpt, width, encoding)
    out.append("== summary ==")
    noun = "thread" if len(todo) == 1 else "threads"
    out.append(f"{request.command.upper()} {len(todo)} {noun} on {_line(run.show.pr)}")
    return encodable("\n".join(out) + "\n", encoding)


def confirm_threads(run: ThreadRun, *, stdin_fd: int, write: Callable[[str], None]) -> bool:
    """The dialog and its answer (``confirm.confirm_with``: the strict rules of every dialog)."""
    return confirm_with(
        lambda width, encoding: dialog_text(run, width, encoding), stdin_fd=stdin_fd, write=write
    )


# --- the write ---------------------------------------------------------------------------------


class _ThreadWrite(BaseModel):
    """The fields of a real ``resolve``/``unresolve`` result pr-comment uses (forgectl
    ``cli/resolve.json``)."""

    model_config = ConfigDict(strict=True, frozen=True, extra="ignore")

    pr: Pr
    action: str
    thread_id: str
    resolved: bool | None
    changed: bool
    dry_run: bool


@dataclass(frozen=True)
class _Lowered:
    read: _ThreadWrite | None
    failure: Outcome | None
    lower: LowerResult


def _checked_answer(
    document: Any, request: ThreadRequest, item: Item, show: Show, gateway: Gateway
) -> _ThreadWrite:
    """The checked result of a write that exited 0. An answer that is not a real run's for this
    PR, thread and action is ``lower_contract_mismatch`` with ``landed: true`` (forgectl said it
    succeeded), never a retry."""

    def mismatch(why: str) -> LowerFailure:
        return LowerFailure(
            mismatch_outcome(request.command, why, gateway=gateway, write=(True, "none"))
        )

    try:
        read = _ThreadWrite.model_validate(document)
    except ValidationError as exc:
        raise mismatch(_why(exc)) from None
    if (
        read.action != request.command
        or read.dry_run
        or read.pr != show.pr
        or read.thread_id != item.thread_id
        or read.resolved is not request.want_resolved
    ):
        raise mismatch("the document is not the real run's result for this thread")
    return read


def _name_the_thread(outcome: Outcome, thread_id: str) -> Outcome:
    """forgectl's exit 10 on a thread write is the thread: say so (``details.what``, ``id``)."""
    error = _error_of(outcome)
    if error.get("code") != "not_found" or outcome.document is None:
        return outcome
    details = error.get("details")
    merged: dict[str, Any] = dict(details) if isinstance(details, dict) else {}  # pyright: ignore[reportUnknownArgumentType]
    merged.update(what="thread", id=thread_id)
    return replace(outcome, document={"error": {**error, "details": merged}})


def _lower(
    result: ForgectlResult, request: ThreadRequest, item: Item, show: Show, gateway: Gateway
) -> _Lowered:
    try:
        document = require_ok(result, gateway=gateway, backend=show.backend, reasons=THREAD_REASONS)
        read = _checked_answer(document, request, item, show, gateway)
    except LowerFailure as failure:
        named = _name_the_thread(failure.outcome, item.thread_id)
        return _Lowered(None, named, _failed_lower(named))
    return _Lowered(read, None, LowerResult(exit=0, code=None, reason=None))


def _failed_lower(outcome: Outcome) -> LowerResult:
    """The audit's ``writes[].lower`` for a write that ended in ``outcome``."""
    return lower_result_of(outcome, None)


def audit_record(run: ThreadRun, env: PostEnv) -> RunRecord:
    """The fields both audit records of this run share: the threads to change, no body."""
    request = run.request
    inputs = AuditInputs(
        event=None,
        draft_id=None,
        draft_sha256=None,
        anchor_head=None,
        reanchor_to=None,
        pinned_head=run.head_sha,
        base_ref=None,
        dedupe_key=None,
        to=None,
        threads=[item.thread_id for item in run.to_change()],
        bodies=[],
        counts=empty_counts(),
        plan_sha256=None,
        allowed_mentions=(),
        override_escalation=tuple(dict.fromkeys(request.override_escalation)),
        config_hash=env.config.config_hash,
        config_path=env.config.config_path,
    )
    return RunRecord(
        run=env.run,
        decision_ids=request.decision_ids,
        action=request.command,
        target=run.show.pr,
        pr_id=run.show.pr_id,
        inputs=inputs,
    )


def _write_args(run: ThreadRun, item: Item, ids: DecisionIds) -> list[str]:
    request = run.request
    return [
        run.show.pr,
        f"--thread={item.thread_id}",
        "--yes",
        f"--actor={request.actor}",
        *(f"--decision-id={d}" for d in ids.all()),
        *(["--override-escalation"] if "forgectl" in request.override_escalation else []),
    ]


class _Hooks:
    """The hooks of the run's writes: one runner, a fresh set of decision ids per thread."""

    def __init__(self, run: ThreadRun, env: PostEnv) -> None:
        self.run = run
        self.env = env
        self.runner = HookRunner(
            env.hooks,
            dirs=env.dirs,
            environ=os.environ if env.environ is None else env.environ,
            log=env.log,
            pr=run.show.pr,
            attempt_id=env.trail.attempt_id,
        )

    def payload(
        self,
        hook: str,
        item: Item,
        index: int,
        ids: Sequence[str],
        result: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        request = self.run.request
        return payload_for_thread(
            action=request.command,
            pr=self.run.show.pr,
            repo=self.run.show.repo,
            actor=request.actor,
            head_sha=self.run.head_sha,
            target=item.target,
            hook=hook,
            attempt_id=self.env.trail.attempt_id,
            write_index=index,
            decision_ids=ids,
            override_escalation=request.override_escalation,
            result=result,
        )

    def pre(self, item: Item, index: int, ids: DecisionIds) -> Outcome | None:
        if not self.env.hooks.pre:
            return None
        verdict = run_pre(
            self.runner, ids, lambda _entry, shown: self.payload(PRE, item, index, shown, None)
        )
        return pre_outcome(
            verdict,
            tuple(ids.from_hooks),
            override_escalation=self.run.request.override_escalation,
            log=self.env.log,
            pr=self.run.show.pr,
            attempt_id=self.env.trail.attempt_id,
        )

    def post(
        self,
        item: Item,
        index: int,
        ids: DecisionIds,
        lowered: _Lowered,
        *,
        skip: bool,
        interrupted: Callable[[], bool],
    ) -> None:
        if not self.env.hooks.post:
            return
        if skip:
            self.runner.warn(POST, None, "hooks_skipped_after_signal")
            return
        lower, read = lowered.lower, lowered.read
        landed = lower.exit == 0
        result = {
            "exit": lower.exit,
            "code": lower.code,
            "reason": lower.reason,
            "ids": [],
            "thread_ids": [],
            "review_id": None,
            "already_posted": False if landed else None,
            "neutralized_lines": 0 if landed else None,
            "head_verified": None,
            "changed": read.changed if read is not None else None,
            "warnings": [],
        }
        run_post(
            self.runner,
            ids,
            lambda _entry, shown: self.payload(POST, item, index, shown, result),
            interrupted=interrupted,
        )


def _call(
    run: ThreadRun, env: PostEnv, item: Item, ids: DecisionIds
) -> tuple[_Lowered, ForgectlInterrupted | None]:
    """The forgectl write of one thread and what it means."""
    request = run.request
    try:
        result = env.gateway.call(request.command, _write_args(run, item, ids))
    except ForgectlInterrupted as exc:
        return _lower(exc.result, request, item, run.show, env.gateway), exc
    except ToolMissing as exc:  # nothing was sent: forgectl could not be started
        failure = tool_missing_outcome(exc)
        return _Lowered(None, failure, LowerResult(exit=12, code="tool_missing", reason=None)), None
    except (GatewayInternalError, GatewayUsageError) as exc:
        failure = error_outcome("internal", type(exc).__name__)
        return _Lowered(None, failure, LowerResult(1, "internal", None)), None
    except Exception as exc:  # after ``attempted`` nothing is known: the write may have landed
        LOG.error("unexpected %s after the write was attempted", type(exc).__name__, exc_info=exc)
        failure = error_outcome(
            "outcome_unknown",
            f"unexpected {type(exc).__name__} after the write was attempted; "
            "whether it changed the thread is unknown",
        )
        return _Lowered(None, failure, LowerResult(12, "outcome_unknown", None)), None
    return _lower(result, request, item, run.show, env.gateway), None


def _apply(item: Item, lowered: _Lowered, request: ThreadRequest) -> None:
    """Set the thread's result from forgectl's lowered answer."""
    if lowered.failure is not None:
        item.failure = lowered.failure
        return
    assert lowered.read is not None
    item.resolved = request.want_resolved
    # a thread pr-comment could not see before the write: ``changed: false`` from forgectl (a
    # no-op) is reported null, since pr-comment cannot tell who changed it
    item.changed = True if lowered.read.changed else (False if item.seen else None)


def write_threads(run: ThreadRun, env: PostEnv) -> Outcome:
    """Step 5 and 6 of the Resolve order for a confirmed run (see the module docstring)."""
    request = run.request
    # read once now, so a failure after a write never starts another forgectl call
    env.gateway.lower_version()
    hooks = _Hooks(run, env)
    writes: list[WriteRecord] = []
    hook_ids: list[str] = []
    attempted = False
    stopped = False
    cut_short = False
    with _DeferredSignals() as held:
        for item in run.to_change():
            if stopped:
                break
            if held.received:
                cut_short = True
                break
            index = len(writes)
            ids = DecisionIds(request.decision_ids)
            try:
                blocked = hooks.pre(item, index, ids)
            except ForgectlInterrupted as exc:
                if not attempted:
                    raise
                held.received.append(exc.signum)
                cut_short = True
                break
            _keep(hook_ids, ids)
            if blocked is not None:
                log_failure(env.log, env.trail.attempt_id, run.show.pr, blocked)
                item.failure = blocked
                if attempted:
                    code = _error_of(blocked).get("code")
                    result = "escalated" if code == "escalated" else "vetoed"
                    writes.append(
                        WriteRecord.blocked(
                            index,
                            format_ts(now_utc()),
                            request.command,
                            item.thread_id,
                            None,
                            result,
                        )
                    )
                continue
            if not attempted:
                try:
                    env.trail.attempted(audit_record(run, env))
                except AuditUnavailableError as exc:
                    refused = Outcome(12, audit_unavailable_error(exc))
                    log_failure(env.log, env.trail.attempt_id, run.show.pr, refused)
                    return refused
                attempted = True
                if held.received:
                    # A signal arrived during the ``attempted`` append: nothing was sent and
                    # nothing will be. Close the pair as a failed write (exit 1, ``internal``),
                    # then end ``128 + signum`` with no document.
                    unsent = LowerResult(exit=1, code="internal", reason=None)
                    write = WriteRecord.from_lower(
                        index, format_ts(now_utc()), request.command, item.thread_id, None, unsent
                    )
                    stop = Outcome(1)
                    final = _record(env, [write], stop)
                    if final is not stop:  # the pair could not be closed: report that
                        log_failure(env.log, env.trail.attempt_id, run.show.pr, final)
                        return final
                    raise Interrupted(held.received[0])
            started = format_ts(now_utc())
            lowered, interrupted = _call(run, env, item, ids)
            hooks.post(
                item,
                index,
                ids,
                lowered,
                skip=interrupted is not None or bool(held.received),
                interrupted=lambda: bool(held.received),
            )
            _keep(hook_ids, ids)
            _apply(item, lowered, request)
            if interrupted is not None:
                # whatever forgectl left is in the audit outcome; the thread is outcome_unknown
                item.failure = _interrupted_outcome(interrupted, lowered)  # pyright: ignore[reportArgumentType]
            writes.append(
                WriteRecord.from_lower(
                    index, started, request.command, item.thread_id, None, lowered.lower
                )
            )
            if item.failure is not None:
                log_failure(env.log, env.trail.attempt_id, run.show.pr, item.failure)
                if item.failure.exit_code == 12:
                    stopped = True
        outcome = finish(run, dry_run=False)
        if not writes:
            return outcome  # every thread was vetoed, escalated, a no-op or failed before a write
        final = _record(env, writes, outcome, hook_ids)
        if final is not outcome:  # the audit failure replaced the outcome
            log_failure(env.log, env.trail.attempt_id, run.show.pr, final)
            return final
        if cut_short and held.received:
            raise Interrupted(held.received[0])
        return final


def _keep(hook_ids: list[str], ids: DecisionIds) -> None:
    """Collect the ids hooks printed (at most 32 in all, as the audit outcome allows)."""
    for item in ids.from_hooks:
        if item in hook_ids:
            continue
        if len(hook_ids) >= MAX_HOOK_DECISION_IDS:
            LOG.warning("more than %d hook decision ids: the rest are not recorded", 32)
            return
        hook_ids.append(item)
