"""The ``comment`` and ``reply`` orders' write (docs/interface.md, Comment and reply order; issue
#30).

The write itself is ``post.post_review``: the same steps in the same order (audit ``attempted``,
one forgectl call with ``plan.write_args()`` and ``plan.input`` on stdin, the lowered answer, audit
``outcome``, the result document), the same signal holding and the same rules (nothing is ever
retried, a signal during the write is exit 12 ``outcome_unknown``, a failed audit append is
``audit_failed`` with the write's own ``landed`` and ``retry``). A comment or a reply has no
inline findings, no follow-up write and no event, so this module only holds what differs: the
human lines of a finished run.

Seam: TODO(#33) the ``pre-post`` and ``post-post`` hooks, in ``post.post_review``.
"""

from __future__ import annotations

from collections.abc import Sequence

from .forge import PostRead
from .prepare import Prepared

TEXT_ACTIONS = ("comment", "reply")


def human_lines(prepared: Prepared, read: PostRead, warnings: Sequence[str]) -> tuple[str, ...]:
    """The human output of a finished ``comment`` or ``reply``: what was posted (or found already
    posted), where, and the reply's thread outcome. No body text: it is the caller's own."""
    plan = prepared.plan
    head = "already posted" if read.already_posted else "posted"
    target = (
        f"{plan.action} to {plan.to} on {plan.pr}" if plan.to else f"{plan.action} on {plan.pr}"
    )
    lines = [f"{head}: {target} at {plan.head_sha[:12]}"]
    if plan.action == "reply" and read.threaded is False:
        lines.append(
            "not threaded: the host had no thread to join, so the reply is a new general comment"
        )
    if read.ids:
        lines.append("ids: " + ", ".join(read.ids))
    if plan.mentions_neutralized:
        lines.append(f"mentions neutralized: {len(plan.mentions_neutralized)}")
    if warnings:
        lines.append("warnings: " + ", ".join(warnings))
    return tuple(lines)
