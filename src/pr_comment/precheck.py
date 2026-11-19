"""The pre-check: forgectl's own dry run of the real call, before the dialog (docs/interface.md:
Pre-check, Position validation; issue #27).

The plan is sent to forgectl with ``--dry-run`` in place of ``--yes`` and the same flags, its input
on stdin. forgectl then applies its usage and size checks, state and pin refusals, self-approval,
the ``--to`` and thread lookup, position validation and the dedupe lookup, and writes nothing.

Rounds run in a fixed order, each rebuilding the plan: at most one ``too_large`` round (reads
``files``, moves the findings whose path is not listed, ``files_incomplete``), then at most one
``invalid_position`` round (moves the findings at ``details.invalid[].index``). Any later refusal
passes through; a second ``invalid_position`` exits 12. ``already_posted`` is only a hint: it never
ends the command. Nothing is kept between rounds but the moves, and every round carries the whole
input. A round is reported to ``on_round`` (the ``precheck`` log event).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pr_comment.forge import Forge
from pr_comment.gateway import Gateway
from pr_comment.lower import LowerFailure, mismatch_outcome, require_ok
from pr_comment.output import LOG, Outcome, error_outcome
from pr_comment.plan import Plan, role_of

# A dry run never reaches the write, so these codes are a contract break (``lower.py`` narrows by
# ``reasons`` only: its ``codes`` argument means "a read" there and would refuse ``would_refuse``).
# forgectl's own ``tool_missing`` on a write is a contract mismatch already (docs/interface.md:
# forgectl edge). ``host_rejected`` is not listed: forgectl's reads give it too (any other 4xx),
# and ``merge_unverified``, ``audit_unavailable`` and the like are not in lower.py's pass-through
# at all, so they are mismatches there. ``invalid_position`` is a mismatch for a non-review action
# (see ``run_precheck``).
WRITE_ONLY_CODES: Final = frozenset(
    {
        "vetoed",
        "escalated",
        "outcome_unknown",
        "outcome_partial",
        "quick_action_ran",
        "approved_stale_head",
        "audit_failed",
        "confirmation_required",
    }
)
DRY_RUN_REASONS: Final = {
    "review": frozenset(
        {
            "stale_head",
            "stale_base",
            "not_open",
            "self_approval",
            "too_large",
            "drafts_present",
            "pending_review_exists",
            "unknown_host",
        }
    ),
    "approve": frozenset(
        {"stale_head", "stale_base", "not_open", "self_approval", "pending_review_exists"}
        | {"unknown_host"}
    ),
    "comment": frozenset({"unknown_host"}),
    "reply": frozenset({"unknown_host"}),
}

RoundLog = Callable[[dict[str, Any]], None]


class _DryRun(BaseModel):
    """The fields of a dry run's result that pr-comment uses; the rest is ignored."""

    model_config = ConfigDict(strict=True, frozen=True, extra="ignore")

    already_posted: bool
    neutralized_lines: int = Field(ge=0)
    markers_removed: int = Field(ge=0)
    threaded: bool | None = None


@dataclass(frozen=True)
class Prechecked:
    """The plan that passed, and what forgectl's dry run said about it."""

    plan: Plan
    already_posted: bool | None  # a hint: True when forgectl reported it, else None
    threaded: bool | None  # reply: forgectl's computed value
    rounds: tuple[str, ...]
    warnings: tuple[str, ...]
    moves: Mapping[str, str]  # this pre-check's moves (finding id to reason)


def _error_of(outcome: Outcome) -> Mapping[str, Any]:
    document = outcome.document
    error = document.get("error") if document is not None else None
    return error if isinstance(error, dict) else {}  # pyright: ignore[reportUnknownVariableType]


def _annotated(outcome: Outcome, plan: Plan, gateway: Gateway) -> Outcome:
    """What the pre-check adds to forgectl's error (docs/interface.md: Pre-check, Error envelope):
    ``body_too_large`` gets ``details.role`` (``body``, or ``inline:<finding id>`` mapped from
    forgectl's ``details.index``) and ``not_found`` names what was not found: the ``--to`` of a
    reply (``details.what: to``), else the PR."""
    error = _error_of(outcome)
    code = error.get("code")
    details: Any = error.get("details")
    own: dict[str, Any] = dict(details) if isinstance(details, dict) else {}  # pyright: ignore[reportUnknownArgumentType]
    if code == "body_too_large":
        if plan.action == "review" and "index" not in own:
            return mismatch_outcome(
                plan.action, "body_too_large names no body of the plan", gateway=gateway
            )
        index: Any = own.get("index")
        try:
            own["role"] = role_of(plan, index) if plan.action == "review" else "body"
        except IndexError:
            return mismatch_outcome(
                plan.action, "body_too_large names no body of the plan", gateway=gateway
            )
    elif code == "not_found":
        own["what"], own["id"] = ("to", plan.to) if plan.to is not None else ("pr", plan.pr)
    else:
        return outcome
    return replace(outcome, document={"error": {**error, "details": own}})


def _fail(outcome: Outcome) -> LowerFailure:
    return LowerFailure(outcome)


def _parse(plan: Plan, document: Any, gateway: Gateway) -> _DryRun:
    try:
        parsed = _DryRun.model_validate(document)
    except ValidationError as exc:
        fields = ", ".join(
            sorted({"/" + "/".join(str(p) for p in e["loc"]) for e in exc.errors()})[:5]
        )
        raise _fail(
            mismatch_outcome(plan.action, f"invalid or missing: {fields}", gateway=gateway)
        ) from None
    if plan.action == "reply" and parsed.threaded is None:
        raise _fail(mismatch_outcome("reply", "invalid or missing: /threaded", gateway=gateway))
    return parsed


def _invalid_moves(plan: Plan, error: Mapping[str, Any], gateway: Gateway) -> dict[str, str]:
    """The inline findings forgectl found invalid (``details.invalid[].index``), as moves."""
    details: Any = error.get("details")
    invalid: Any = details.get("invalid") if isinstance(details, dict) else None  # pyright: ignore[reportUnknownMemberType]
    moves: dict[str, str] = {}
    if isinstance(invalid, list):
        for item in invalid:  # pyright: ignore[reportUnknownVariableType]
            index: Any = item.get("index") if isinstance(item, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if type(index) is not int or not 0 <= index < len(plan.inline_comments):
                moves = {}
                break
            moves[plan.inline_comments[index].finding_id] = "invalid_position"
    if not moves:
        raise _fail(
            mismatch_outcome(
                "review",
                "invalid_position names no inline comment of the plan",
                gateway=gateway,
            )
        )
    return moves


def _files_moves(plan: Plan, forge: Forge) -> dict[str, str]:
    """The ``too_large`` round: read ``files``; findings whose path is not listed move as
    ``files_incomplete`` when the list is incomplete. Empty when forgectl's refusal stands (a
    complete list, or every path listed). ``LowerFailure`` for a failed read or another head."""
    listing = forge.files(plan.pr)
    if listing.head_sha != plan.head_sha:
        raise _fail(
            error_outcome(
                "refused",
                "the head moved; rerun with the current head",
                reason="stale_head",
                details={"head_sha": listing.head_sha},
            )
        )
    if listing.files_complete:
        return {}
    # a renamed file matches by its new path (forgectl, Position validation), on either side
    listed = {f.path for f in listing.files}
    return {c.finding_id: "files_incomplete" for c in plan.inline_comments if c.path not in listed}


def _dry_run_cannot_give(plan: Plan, code: object) -> bool:
    """Whether ``code`` is an answer a dry run of ``plan`` can never give (a contract break).

    One write-only code is real on a dry run: forgectl's dedupe read (post order step 4) comes
    before the dry run stops (step 6), so on GitHub a ``review`` or ``approve`` whose dedupe key
    already posted and whose state step is still needed (an event change on a reused key) exits 12
    ``outcome_partial`` with nothing sent. It is passed through with the table's GitHub pair
    (``landed: false``, ``retry: new_key``). GitLab runs that state step instead, and a text action
    has none, so there it stays a mismatch.

    Evidence: the ``review`` dedupe hit was observed live against forgectl 0.55.2. ``approve`` is
    inferred from the shared forgectl code path and not yet observed live. forgectl's doc orders
    this error after the dry-run stop, so the observed behavior is a recorded deviation from it.
    todo(#39): capture a live approve dry-run dedupe hit and record both cases in
    tests/forgectl_cases.json
    """
    if code == "outcome_partial":
        return not (plan.action in ("review", "approve") and plan.backend == "github")
    return code in WRITE_ONLY_CODES or (code == "invalid_position" and plan.action != "review")


def run_precheck(
    gateway: Gateway,
    forge: Forge,
    build: Callable[[Mapping[str, str]], Plan],
    *,
    on_round: RoundLog | None = None,
) -> Prechecked:
    """Run the pre-check rounds. ``build(moves)`` rebuilds the plan with those inline findings
    moved to the review body (finding id to reason). Raises ``LowerFailure`` with the outcome the
    run ends with: forgectl's error (exit 20 ``would_refuse`` lowered to ``refused``), the second
    ``invalid_position``, or ``lower_contract_mismatch`` for an answer pr-comment cannot use.
    ``ForgectlInterrupted`` and ``ToolMissing`` propagate."""
    moves: dict[str, str] = {}
    rounds: list[str] = []
    files_done = invalid_done = False
    label = "initial"
    while True:
        plan = build(moves)
        result = gateway.call(plan.action, plan.precheck_args(), stdin=plan.input)
        rounds.append(label)
        try:
            document = require_ok(
                result,
                gateway=gateway,
                backend=plan.backend,
                reasons=DRY_RUN_REASONS[plan.action],
            )
        except LowerFailure as failure:
            error = _error_of(failure.outcome)
            code, reason = error.get("code"), error.get("reason")
            if _dry_run_cannot_give(plan, code):
                raise _fail(
                    mismatch_outcome(plan.action, "an error a dry run cannot give", gateway=gateway)
                ) from None
            _log(on_round, label, plan, str(code), str(reason) if reason else None, moves)
            if (
                plan.action == "review"
                and reason == "too_large"
                and not (files_done or invalid_done)
            ):
                files_done = True
                found = _files_moves(plan, forge)
                if found:
                    moves.update(found)
                    label = "too_large"
                    continue
            elif plan.action == "review" and code == "invalid_position" and not invalid_done:
                invalid_done = True
                moves.update(_invalid_moves(plan, error, gateway))
                label = "invalid_position"
                continue
            raise _fail(_annotated(failure.outcome, plan, gateway)) from None
        parsed = _parse(plan, document, gateway)
        warnings: list[str] = []
        if parsed.neutralized_lines or parsed.markers_removed:
            # forgectl changed text pr-comment had already neutralized: a forgectl change it does
            # not know (docs/interface.md: Text pipeline)
            warnings.append("neutralizing_mismatch")
            LOG.warning("neutralizing_mismatch: forgectl changed text that was already neutralized")
        _log(on_round, label, plan, "ok", None, moves, already_posted=parsed.already_posted)
        return Prechecked(
            plan=plan,
            already_posted=True if parsed.already_posted else None,
            threaded=parsed.threaded,
            rounds=tuple(rounds),
            warnings=tuple(warnings),
            moves=dict(moves),
        )


def _log(
    on_round: RoundLog | None,
    label: str,
    plan: Plan,
    result: str,
    reason: str | None,
    moves: Mapping[str, str],
    *,
    already_posted: bool | None = None,
) -> None:
    """One ``precheck`` log event: the round, forgectl's answer and the moves so far (ids and
    reasons only, never text)."""
    if on_round is None:
        return
    on_round(
        {
            "event": "precheck",
            "pr": plan.pr,
            "round": label,
            "result": result,
            "reason": reason,
            "moved": [{"id": i, "reason": r} for i, r in moves.items()],
            "already_posted": already_posted,
        }
    )
