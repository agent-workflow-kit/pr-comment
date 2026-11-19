"""The posting orders up to the plan (docs/interface.md: Review order steps 1 to 7, Approve order,
Comment and reply order; issue #27).

``prepare`` runs one command's usage checks, its reads (``show``, and for a review ``compare`` and
``diff``), builds the plan and runs the pre-check rounds. It sends nothing: every forgectl call it
makes is a read or a ``--dry-run``. ``check_binding`` is step 6 (``--expect-plan``) and
``dry_run_outcome`` step 7 (the ``--dry-run`` result). The confirmation dialog and the writes are
not here: ``Prepared`` carries everything they need (the plan, whose ``input`` they send byte for
byte, the pinned values and the hint of the pre-check).

The actor, ``interactive`` and the human-only flag rules come from the CLI layer; this module takes
them as given, and so are the state and temporary directory checks of step 1 (``Env.before_reads``).
The automatic approval guard (``approve.py``) runs here for ``approve`` and ``review --approve``,
right after the head and base checks and before any compare, diff or pre-check call, so a refusal
(also on a dry run) sends nothing.

A failure ends the run with ``RunFailure`` (pr-comment's own refusals and usage errors) or
``LowerFailure`` (forgectl's, already lowered); both carry the ``Outcome``. ``ForgectlInterrupted``
and ``ToolMissing`` propagate.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, BinaryIO, Final

from pr_comment import approve, textpipe
from pr_comment.config import ConfigError, LoadedConfig
from pr_comment.diffmap import DiffMapper
from pr_comment.draft import MAX_INLINE_COMMENTS, DraftError, load_draft, read_limited
from pr_comment.errors import cut_bytes
from pr_comment.forge import Forge, PostRead, Show, match_draft_pr
from pr_comment.gateway import Gateway
from pr_comment.grammars import GrammarError, check_branch
from pr_comment.lower import LowerFailure
from pr_comment.output import Outcome, error_outcome
from pr_comment.plan import (
    Context,
    Plan,
    TooManyInlineComments,
    build_review_plan,
    build_text_plan,
    review_source,
)
from pr_comment.precheck import Prechecked, RoundLog, run_precheck
from pr_comment.reanchor import (
    CompareContractError,
    StaleHeadError,
    mapper_from_compare,
    reanchor,
)

MAX_BODY_BYTES: Final = 64 * 1024
POSTING_COMMANDS: Final = ("review", "comment", "reply", "approve")


class RunFailure(Exception):
    """pr-comment's own refusal or usage error: the run ends with ``outcome``."""

    def __init__(self, outcome: Outcome) -> None:
        super().__init__("run failed")
        self.outcome = outcome


def _usage(
    message: str, code: str = "usage", details: Mapping[str, Any] | None = None
) -> RunFailure:
    return RunFailure(error_outcome(code, message, details=details))


def _refused(reason: str, message: str, details: Mapping[str, Any]) -> RunFailure:
    return RunFailure(error_outcome("refused", message, reason=reason, details=details))


@dataclass(frozen=True)
class Request:
    """One posting command as the CLI parsed it (values already checked against their grammars)."""

    command: str
    pr: str
    actor: str
    decision_ids: tuple[str, ...] = ()
    draft_file: str | None = None
    body_file: str | None = None
    to: str | None = None
    approve: bool = False
    reanchor_to: str | None = None
    expected_head: str | None = None
    expected_base: str | None = None
    dedupe_key: str | None = None
    allow_mentions: tuple[str, ...] = ()
    expect_plan: str | None = None
    override_escalation: tuple[str, ...] = ()
    interactive: bool = False


@dataclass(frozen=True)
class Env:
    """What a run needs from outside: forgectl, the config, who and what version."""

    gateway: Gateway
    config: LoadedConfig
    me_name: str
    tool_version: str
    stdin: BinaryIO | None = None
    on_round: RoundLog | None = None
    # Step 1's state checks (docs/interface.md, Review order): called once the offline checks of
    # the command passed and before the first forgectl call; it raises ``RunFailure`` (exit 12
    # ``state_unsafe`` or ``audit_unavailable``).
    before_reads: Callable[[], None] | None = None


@dataclass(frozen=True)
class Prepared:
    """A plan that passed the pre-check, with everything the dialog and the writes need."""

    request: Request
    show: Show
    precheck: Prechecked
    reanchored_draft: dict[str, Any] | None  # review with --reanchor-to
    draft_sha256: str | None = None  # review: the sha256 of the draft file's bytes

    @property
    def plan(self) -> Plan:
        return self.precheck.plan


# --- usage -----------------------------------------------------------------------------------


def _read_body_file(source: str, stdin: BinaryIO | None) -> str:
    """The body file as text: no-follow, regular, at most 64 KiB, strict UTF-8, Text pipeline
    steps 1 and 2 checked (``invalid_text`` with pointer ``/body``). ``-`` is stdin."""
    if source == "-":
        stream = stdin if stdin is not None else getattr(sys.stdin, "buffer", None)
        if stream is None:
            raise _usage("the body could not be read from stdin")
        raw = _limited(stream)
    else:
        raw = _limited_file(source)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _usage("the body is not valid UTF-8") from None
    kind = textpipe.text_problem(text)
    if kind is not None:
        raise _usage(
            "invalid body text: "
            + ("a control character" if kind == "control_character" else "a suggestion fence"),
            "invalid_text",
            {"pointer": "/body", "kind": kind},
        )
    return text


def _limited(stream: Any) -> bytes:
    try:
        raw = read_limited(stream, MAX_BODY_BYTES)
    except DraftError:
        raise _usage("the body could not be read or is larger than 64 KiB") from None
    return raw


def _limited_file(path: str) -> bytes:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | getattr(os, "O_NOCTTY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _usage("the body file is a symbolic link") from None
        raise _usage("the body file cannot be opened") from None
    except ValueError:
        raise _usage("the body file cannot be opened") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _usage("the body file is not a regular file")
        with os.fdopen(fd, "rb", buffering=0, closefd=False) as handle:
            return _limited(handle)
    except OSError:
        raise _usage("the body file cannot be read") from None
    finally:
        os.close(fd)


def _pinned_base(ref: str) -> str:
    """The base ref as ``--expected-base`` and the plan take it (forgectl's branch grammar)."""
    try:
        return check_branch(ref)
    except GrammarError:
        raise _usage("the pull request's base ref cannot be used as a pinned base") from None


# --- the orders ------------------------------------------------------------------------------


def prepare(request: Request, env: Env) -> Prepared:
    """Run one posting command up to a plan that passed the pre-check."""
    if request.command == "review":
        return _prepare_review(request, env)
    if request.command in ("comment", "reply", "approve"):
        return _prepare_text(request, env)
    raise ValueError("not a posting command")


def _config_failure(exc: ConfigError) -> RunFailure:
    return _usage(cut_bytes(str(exc)), "config_invalid", {"key": exc.key})


def _guarded(build: Callable[[Mapping[str, str]], Plan]) -> Callable[[Mapping[str, str]], Plan]:
    """Turn a footer that renders to a non-letter (``ConfigError`` at run time) into the exit 2
    ``config_invalid`` envelope."""

    def run(moves: Mapping[str, str]) -> Plan:
        try:
            return build(moves)
        except ConfigError as exc:
            raise _config_failure(exc) from None
        except TooManyInlineComments as exc:
            raise _usage(
                str(exc), details={"limit": MAX_INLINE_COMMENTS, "inline_comments": exc.count}
            ) from None

    return run


def _is_too_large(outcome: Outcome) -> bool:
    error = (outcome.document or {}).get("error")
    return isinstance(error, dict) and error.get("reason") == "too_large"  # pyright: ignore[reportUnknownMemberType]


def _mapper(forge: Forge, pr: str, a: str, b: str) -> DiffMapper:
    """Step 1 of Re-anchoring: ``compare A..B``; ``too_large`` (exit 20) gives no mapping."""
    document: dict[str, Any] | None
    try:
        read = forge.compare(pr, a, b)
        document = {"from_sha": read.from_sha, "to_sha": read.to_sha, "diff": read.diff}
    except LowerFailure as failure:
        if not _is_too_large(failure.outcome):
            raise
        document = None
    try:
        return mapper_from_compare(document, head_sha=b, from_sha=a)
    except StaleHeadError as exc:
        raise RunFailure(exc.outcome()) from None
    except CompareContractError as exc:
        raise RunFailure(exc.outcome()) from None


def _merge_base(forge: Forge, pr: str, head: str) -> str | None:
    """Step 3: the merge-base from ``diff --json``; ``too_large`` makes it unknown."""
    try:
        return forge.diff(pr, expect_head=head).merge_base_sha
    except LowerFailure as failure:
        if _is_too_large(failure.outcome):
            return None
        raise


def _stale_head(head: str, message: str) -> RunFailure:
    return _refused("stale_head", message, {"head_sha": head})


def _guard(
    req: Request, env: Env, show: Show, *, pinned_head: str, anchor_head: str | None
) -> None:
    """The automatic approval guard (``approve.py``) for an approving command: ``approve``, or
    ``review --approve``. Raises ``RunFailure`` with its refusal."""
    refusal = approve.check_auto_approve(
        actor=req.actor,
        approving=req.command == "approve" or req.approve,
        allow=env.config.for_repo(show.repo).allow_auto_approve,
        decision_ids=req.decision_ids,
        expected_head=req.expected_head,
        pinned_head=pinned_head,
        anchor_head=anchor_head,
    )
    if refusal is not None:
        raise RunFailure(refusal)


def _prepare_review(req: Request, env: Env) -> Prepared:
    if req.draft_file is None:
        raise _usage("review needs --draft-file")
    try:
        draft = load_draft(req.draft_file, stdin=env.stdin)
    except DraftError as exc:
        raise RunFailure(exc.outcome()) from None
    data = draft.data
    if req.approve and data["event"] == "request_changes":
        raise _usage(
            "--approve cannot post a draft whose event is request_changes",
            "approve_conflicts_event",
        )
    if req.approve and req.actor.startswith("auto:") and req.expected_head is None:
        raise _usage("--expected-head is required to approve as an automatic actor")

    if env.before_reads is not None:
        env.before_reads()
    forge = Forge(env.gateway)
    show = forge.show(req.pr)
    match_draft_pr(forge, data["pr"], show)
    if show.state != "open":
        raise _refused("not_open", "the pull request is not open", {"state": show.state})
    anchor: Mapping[str, Any] = data["anchor"]
    head_a: str = anchor["head_sha"]
    head_b = req.reanchor_to or head_a
    if show.head_sha != head_b:
        hint = "" if req.reanchor_to else "; rerun with --reanchor-to <head>"
        raise _stale_head(show.head_sha, "the head moved after this review was drafted" + hint)
    if show.base_ref != anchor["base_ref"]:
        raise _refused(
            "stale_base",
            "the base branch was retargeted; this review needs a new draft",
            {"base_ref": show.base_ref},
        )
    if req.expected_head is not None and req.expected_head != head_b:
        raise _stale_head(head_b, "the head is not the one expected")
    # The automatic approval guard runs before any compare, diff or pre-check read.
    _guard(req, env, show, pinned_head=head_b, anchor_head=head_a)
    base_ref = _pinned_base(anchor["base_ref"])

    has_left = any(f.get("side") == "left" and f.get("line") is not None for f in data["findings"])
    mapper = (
        _mapper(forge, show.pr, head_a, head_b) if head_b != head_a else DiffMapper.from_text("")
    )
    merge_base = _merge_base(forge, show.pr, head_b) if has_left else None
    done = reanchor(
        draft,
        head_sha=head_b,
        base_ref=base_ref,
        base_sha=show.base_sha,
        merge_base_sha=merge_base,
        mapper=mapper,
    )

    ctx = Context(
        show=show,
        actor=req.actor,
        decision_ids=req.decision_ids,
        settings=env.config.for_repo(show.repo),
        me_name=env.me_name,
        tool_version=env.tool_version,
        allow_mentions=req.allow_mentions,
        dedupe_key=req.dedupe_key,
        override_forgectl="forgectl" in req.override_escalation,
    )
    source = review_source(data, done, head_sha=head_b, approve=req.approve)
    build = _guarded(lambda moves: build_review_plan(ctx, source, moves))
    prechecked = run_precheck(env.gateway, forge, build, on_round=env.on_round)
    return Prepared(
        req,
        show,
        prechecked,
        done.draft if req.reanchor_to is not None else None,
        draft.sha256,
    )


def _prepare_text(req: Request, env: Env) -> Prepared:
    command = req.command
    if command == "approve" and (req.expected_head is None or req.expected_base is None):
        if not req.interactive:
            raise _usage(
                "--expected-head and --expected-base are required outside an interactive run"
            )
    if command == "approve" and req.actor.startswith("auto:") and req.expected_head is None:
        raise _usage("--expected-head is required to approve as an automatic actor")
    text: str | None = None
    if req.body_file is not None:
        text = _read_body_file(req.body_file, env.stdin)
        if command == "approve" and textpipe.is_blank_text(text):
            text = None  # an empty or blank body file counts as no body file
        elif command != "approve" and textpipe.is_blank_text(text):
            raise _usage("the body is empty")
    elif command != "approve":
        raise _usage(f"{command} needs --body-file")

    if env.before_reads is not None:
        env.before_reads()
    forge = Forge(env.gateway)
    show = forge.show(req.pr)
    if command == "approve" and show.state != "open":
        raise _refused("not_open", "the pull request is not open", {"state": show.state})
    if req.expected_head is not None and req.expected_head != show.head_sha:
        raise _stale_head(show.head_sha, "the head is not the one expected")
    head = req.expected_head or show.head_sha
    if (
        command == "approve"
        and req.expected_base is not None
        and req.expected_base != show.base_ref
    ):
        raise _refused(
            "stale_base", "the base branch is not the one expected", {"base_ref": show.base_ref}
        )
    _guard(req, env, show, pinned_head=head, anchor_head=None)
    base_ref = _pinned_base(req.expected_base or show.base_ref)

    ctx = Context(
        show=show,
        actor=req.actor,
        decision_ids=req.decision_ids,
        settings=env.config.for_repo(show.repo),
        me_name=env.me_name,
        tool_version=env.tool_version,
        allow_mentions=req.allow_mentions,
        dedupe_key=req.dedupe_key,
        override_forgectl="forgectl" in req.override_escalation,
    )
    build = _guarded(
        lambda _moves: build_text_plan(
            ctx, action=command, text=text, head_sha=head, base_ref=base_ref, to=req.to
        )
    )
    prechecked = run_precheck(env.gateway, forge, build, on_round=env.on_round)
    return Prepared(req, show, prechecked, None)


# --- plan binding and the dry-run result -----------------------------------------------------


def check_binding(prepared: Prepared) -> Outcome | None:
    """Step 6: ``--expect-plan`` differing from the final plan's hash is exit 20 ``plan_changed``
    (``details.plan_sha256`` is the final plan's). Checked after the pre-check, before the dry-run
    output and any dialog or write."""
    expected = prepared.request.expect_plan
    if expected is None or expected == prepared.plan.plan_sha256:
        return None
    return error_outcome(
        "refused",
        "the plan changed since it was previewed",
        reason="plan_changed",
        details={"plan_sha256": prepared.plan.plan_sha256},
    )


def result_document(
    prepared: Prepared,
    *,
    dry_run: bool,
    posted: PostRead | None = None,
    warnings: Sequence[str] = (),
    body_thread_resolved: bool | None = None,
    body_thread_id: str | None = None,
) -> dict[str, Any]:
    """The ``--json`` result (docs/interface.md: Post result): a dry run's (``dry_run=True``, the
    plan in ``plan``), or a real run's from forgectl's checked answer ``posted`` (``plan`` is null).

    ``warnings`` is the real run's whole list (forgectl's and pr-comment's); a dry run's are the
    pre-check's.
    """
    if dry_run == (posted is not None):
        raise ValueError("a dry run has no forgectl answer, a real run needs one")
    plan, pre = prepared.plan, prepared.precheck
    doc: dict[str, Any] = {
        "pr": plan.pr,
        "action": plan.action,
        "dry_run": dry_run,
        "dedupe_key": plan.dedupe_key,
        "head_sha": plan.head_sha,
        "base_ref": plan.base_ref,
        "mentions_neutralized": list(plan.mentions_neutralized),
        "plan_sha256": plan.plan_sha256,
        "neutralized_lines": plan.neutralized_lines,
        "markers_removed": plan.markers_removed,
    }
    if posted is None:
        doc.update(
            ids=[],
            thread_ids=[],
            review_id=None,
            already_posted=pre.already_posted,
            follow_up_done=None,
            warnings=list(pre.warnings),
            plan=plan.document(),
        )
    else:
        doc.update(
            ids=list(posted.ids),
            thread_ids=list(posted.thread_ids),
            review_id=posted.review_id,
            already_posted=posted.already_posted,
            follow_up_done=posted.follow_up_done,
            warnings=list(warnings),
            plan=None,
        )
    if plan.action in ("review", "approve"):
        doc["event"] = plan.event
        doc["head_verified"] = None if posted is None else posted.head_verified
    if plan.action == "review":
        doc.update(
            draft_id=plan.draft_id,
            posted_inline=list(plan.posted_inline),
            moved=[{"id": m.id, "reason": m.reason} for m in plan.moved],
            file_level=list(plan.file_level),
            suggestions_degraded=[
                {"id": d.id, "reason": d.reason} for d in plan.suggestions_degraded
            ],
            body_thread_resolved=body_thread_resolved,
            body_thread_id=body_thread_id,
            reanchored_draft=prepared.reanchored_draft,
        )
    if plan.action == "reply":
        doc["to"] = plan.to
        doc["threaded"] = pre.threaded if posted is None else posted.threaded
    return doc


def _human_lines(prepared: Prepared) -> tuple[str, ...]:
    """The plan for a human run of ``--dry-run``, one display line each (``render`` marks every
    untrusted character): the summary, then each body as forgectl will post it."""
    plan, pre = prepared.plan, prepared.precheck
    what = plan.action.upper() + (f": {plan.event.upper()}" if plan.event else "")
    lines = [
        f"dry run: nothing was sent (plan_sha256 {plan.plan_sha256})",
        f"{what} {plan.pr} at {plan.head_sha[:12]}, base {plan.base_ref}",
    ]
    if plan.action == "review":
        lines.append(
            f"inline {len(plan.posted_inline)}, moved {len(plan.moved)}, "
            f"file-level {len(plan.file_level)}, suggestions degraded "
            f"{len(plan.suggestions_degraded)}"
        )
    if pre.already_posted:
        lines.append("forgectl reports this may already be posted")
    for body in plan.shown_bodies():
        lines.append(f"--- {body.role}")
        lines.extend(body.text.split("\n"))
    return tuple(lines)


def dry_run_outcome(prepared: Prepared) -> Outcome:
    """Step 7: exit 0 with the result and the plan; nothing was sent."""
    return Outcome(0, result_document(prepared, dry_run=True), _human_lines(prepared))
