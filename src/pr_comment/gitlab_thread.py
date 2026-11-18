"""Finding the GitLab review body thread (docs/interface.md, GitLab review body thread; issue #36).

On GitLab the published review body is a resolvable discussion. forgectl appends its dedupe line
(``<!-- forgectl:dedupe:<key> -->``) as the last line of the review body, so the body thread is the
one whose first comment ends with this run's line. The reading is conservative: more or fewer than
one such thread means the thread is not known, and nothing is resolved (an inline finding thread
is never resolved automatically).

After a fresh post the candidates are the review's ``thread_ids``; after ``already_posted`` they
are the threads holding a comment of forgectl's ``ids``. Both also need the marker line (the spec
names the thread by its comment; the marker is kept as a second check so a thread of inline
findings is never taken for the body).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .forge import CommentsRead, ThreadRead


@dataclass(frozen=True)
class BodyThread:
    """The body thread as the ``comments`` read showed it."""

    id: str
    resolvable: bool
    resolved: bool
    path: str | None
    line: int | None


def marker_line(key: str) -> str:
    return f"<!-- forgectl:dedupe:{key} -->"


def _last_line(body: str) -> str | None:
    lines = [line.rstrip() for line in body.splitlines() if line.strip()]
    return lines[-1] if lines else None


def _has_marker(thread: ThreadRead, key: str) -> bool:
    return _last_line(thread.comments[0].body) == marker_line(key)


def find_body_thread(
    comments: CommentsRead,
    *,
    key: str,
    thread_ids: Sequence[str],
    ids: Sequence[str],
    already_posted: bool,
) -> BodyThread | None:
    """The body thread of the review posted with ``key``, or None when it is not exactly one."""
    if already_posted:
        wanted = set(ids)
        candidates = [t for t in comments.threads if any(c.id in wanted for c in t.comments)]
    else:
        wanted = set(thread_ids)
        candidates = [t for t in comments.threads if t.id in wanted]
    found = [t for t in candidates if _has_marker(t, key)]
    if len(found) != 1:
        return None
    thread = found[0]
    return BodyThread(thread.id, thread.resolvable, thread.resolved, thread.path, thread.line)
