"""The automatic approval guard (docs/interface.md: Automatic approval guard; issue #31).

An approval by an agent stands in for a human's review, so the guard is code that runs before the
dialog, before the pre-check and before any forgectl write, and nothing can skip it: ``--yes`` is
not a human, a hook cannot satisfy it (it runs before any hook and reads only the caller's
``--decision-id`` flags), and a dry run applies it too.

``check_auto_approve`` is the rule, pure. ``prepare.py`` calls it right after the reads that give
it its inputs (the head and base checks), and ``guard_prepared`` calls it again on the confirmed
plan itself before the dialog, so a later change to a posting order cannot let an automatic
approval through.

An actor is guarded unless it is exactly ``human:...``: the actor grammar (``grammars.check_actor``)
allows only ``human:`` and ``auto:``, and this module fails closed on anything else. As in the
interface, the human rule is advisory against a same-user process that claims
``--actor human:<user>``: the audit record shows ``actor_source`` and ``stdin_tty``.

The ``--decision-id`` check is a presence check only, not proof of what the gate evaluated.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from pr_comment.output import Outcome, error_outcome

if TYPE_CHECKING:
    from pr_comment.config import LoadedConfig
    from pr_comment.prepare import Prepared

DISABLED: Final = "auto_approve_disabled"
REANCHORED: Final = "auto_approve_reanchored"
HUMAN_PREFIX: Final = "human:"


def is_guarded(actor: str) -> bool:
    """True for every actor that is not a human (fail closed)."""
    return not actor.startswith(HUMAN_PREFIX)


def _refused(reason: str, message: str, details: dict[str, str] | None = None) -> Outcome:
    return error_outcome("refused", message, reason=reason, details=details)


def check_auto_approve(
    *,
    actor: str,
    approving: bool,
    allow: bool,
    decision_ids: Sequence[str],
    expected_head: str | None,
    pinned_head: str,
    anchor_head: str | None,
) -> Outcome | None:
    """The refusal of an approving action by an automatic actor, or None when it may go on.

    ``allow`` is ``allow_auto_approve`` for the PR's repo; ``decision_ids`` are the caller's
    ``--decision-id`` values; ``expected_head`` is the caller's ``--expected-head`` (the head its
    gate decided on); ``pinned_head`` is the head the approval is pinned to; ``anchor_head`` is the
    draft's ``anchor.head_sha`` for ``review --approve``, None for ``approve``.
    """
    if not approving or not is_guarded(actor):
        return None
    if not allow:
        return _refused(
            DISABLED, "automatic approval is not enabled for this repository (allow_auto_approve)"
        )
    if not decision_ids:
        return _refused(DISABLED, "an automatic approval needs a --decision-id from the caller")
    if expected_head is None:
        return error_outcome(
            "usage", "--expected-head is required to approve as an automatic actor"
        )
    if expected_head != pinned_head:
        return _refused(
            "stale_head",
            "the head is not the one the gate decided on",
            {"head_sha": pinned_head},
        )
    if anchor_head is not None and anchor_head != pinned_head:
        return _refused(
            REANCHORED,
            "an automatic approval cannot approve a re-anchored draft: the gate saw the old head",
        )
    return None


def guard_prepared(prepared: Prepared, config: LoadedConfig) -> Outcome | None:
    """The guard applied to a plan that passed the pre-check, right before the dialog: its actor,
    decision ids, pinned head and anchor head are the plan's own, the opt-in is the repo's."""
    plan, request = prepared.plan, prepared.request
    return check_auto_approve(
        actor=plan.actor,
        approving=plan.event == "approve",
        allow=config.for_repo(plan.repo).allow_auto_approve,
        decision_ids=plan.decision_ids,
        expected_head=request.expected_head,
        pinned_head=plan.head_sha,
        anchor_head=plan.anchor_head,
    )
