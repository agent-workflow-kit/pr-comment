"""Re-anchoring a draft onto the PR's current head (docs/interface.md, Re-anchoring).

Pure functions over parsed forgectl results: no I/O. The caller reads ``compare`` and ``diff``
(posting plan, #27) and passes in what they returned. Re-anchoring is exact and never guesses: a
finding is kept at a new place only when the diff proves where its line went, and every other
finding moves, with a reason, to ``unanchored``, so none is dropped and none lands on other code.

Right-side findings go through ``DiffMapper.map_right``. Left-side findings refer to the base file
at the merge-base, so they are never mapped through the head-to-head diff: they keep their lines
only when both merge-bases are known and equal (step 3), and follow a rename of their file.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from .diffmap import DiffMapper, Mapped, printed_path
from .draft import Draft
from .output import Outcome, error_outcome

BASE_CHANGED: Final = "base_changed"
CHANGED: Final = "changed"
FILE_REMOVED: Final = "file_removed"

_SHA: Final = re.compile(r"[0-9a-f]{40}")


class StaleHeadError(Exception):
    """``compare`` answered for another head than B (or not at all): exit 20 ``stale_head``."""

    def __init__(self, head_sha: str | None) -> None:
        super().__init__("compare did not answer for the pinned head")
        self.head_sha = head_sha

    def outcome(self) -> Outcome:
        details: dict[str, Any] = {"head_sha": self.head_sha}
        return error_outcome(
            "refused",
            "the head moved while the draft was being re-anchored; rerun with the current head",
            reason="stale_head",
            details=details,
        )


@dataclass(frozen=True, slots=True)
class MovedFinding:
    """A finding that did not map, as it was in the source draft, and why."""

    finding: dict[str, Any]
    reason: str


@dataclass(frozen=True, slots=True)
class Reanchored:
    """``draft`` is the ``reanchored_draft``; ``moved`` is this run's new moves, in draft order."""

    draft: dict[str, Any]
    moved: tuple[MovedFinding, ...]


def mapper_from_compare(compare: Mapping[str, Any] | None, *, head_sha: str) -> DiffMapper:
    """The right-side mapper for a parsed ``forgectl compare`` result (step 1).

    ``compare`` is ``None`` when forgectl exited 20 ``too_large``. Its ``to_sha`` must equal
    ``head_sha`` (B), else ``StaleHeadError``; a result that is not shaped as the schema says fails
    the same way, closed. ``diff: null`` (``behind``, ``diverged``, ``unavailable``), ``too_large``
    and a diff that cannot be read unambiguously give an unavailable mapper, which moves every
    right-side finding (``diff_unavailable``). An ``identical`` compare has an empty diff.
    """
    if compare is None:
        return DiffMapper.unavailable()
    to_sha = compare.get("to_sha")
    if not isinstance(to_sha, str) or to_sha != head_sha:
        raise StaleHeadError(to_sha if isinstance(to_sha, str) else None)
    diff = compare.get("diff")
    if diff is not None and not isinstance(diff, str):
        raise StaleHeadError(None)
    return DiffMapper.from_text(diff)


def left_side_ok(recorded: str | None, current: str | None) -> bool:
    """Whether left-side lines still hold: both merge-bases known and equal (step 3)."""
    return recorded is not None and current is not None and recorded == current


def _check_sha(name: str, value: object, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError(f"{name} must be 40 lowercase hex characters")


def _followed(path: str, fate_path: str) -> str:
    """The path at B; an unchanged file keeps the draft's own spelling."""
    return path if fate_path in (path, printed_path(path)) else fate_path


def reanchor(
    draft: Draft,
    *,
    head_sha: str,
    base_ref: str,
    base_sha: str | None,
    merge_base_sha: str | None,
    mapper: DiffMapper,
    old_lengths: Mapping[str, int] | None = None,
) -> Reanchored:
    """Move ``draft``'s findings from its anchor head (A) onto ``head_sha`` (B).

    ``base_ref`` and ``base_sha`` come from ``show`` at B; ``merge_base_sha`` is the ``diff --json``
    read of step 3 (``None`` when unknown or not read). ``mapper`` is the A-to-B diff; when B
    equals A it is ignored and lines are kept. ``old_lengths`` optionally gives the line count of
    files at A (keyed by path); a right-side line past it moves ``changed``. Without it a line past
    the end of an unchanged file is left to the pre-check at B (``invalid_position``).
    Raises ``ValueError`` for a malformed sha. The source draft is not modified.
    """
    _check_sha("head_sha", head_sha)
    _check_sha("base_sha", base_sha, nullable=True)
    _check_sha("merge_base_sha", merge_base_sha, nullable=True)
    data = draft.data
    anchor: dict[str, Any] = data["anchor"]
    head_a: str = anchor["head_sha"]
    if head_sha == head_a:
        mapper = DiffMapper.from_text("")
    findings: list[dict[str, Any]] = data["findings"]
    has_left = any(f.get("side") == "left" and f["line"] is not None for f in findings)
    left_ok = left_side_ok(anchor["merge_base_sha"], merge_base_sha)

    kept: list[dict[str, Any]] = []
    moved: list[MovedFinding] = []
    for source in findings:
        placed, reason = _place(source, mapper, left_ok, old_lengths)
        if placed is not None:
            kept.append(placed)
        else:
            assert reason is not None
            moved.append(MovedFinding(copy.deepcopy(source), reason))

    new_anchor = dict(anchor)
    new_anchor.update(
        head_sha=head_sha,
        base_ref=base_ref,
        base_sha=base_sha,
        merge_base_sha=merge_base_sha if has_left else None,
    )
    unanchored: list[dict[str, Any]] | None = copy.deepcopy(data.get("unanchored"))
    if moved:
        unanchored = (unanchored or []) + [
            {"finding": m.finding, "head_sha": head_a, "reason": m.reason} for m in moved
        ]
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key == "anchor":
            out[key] = new_anchor
        elif key == "findings":
            out[key] = kept
            if unanchored is not None and "unanchored" not in data:
                out["unanchored"] = unanchored
        elif key == "unanchored":
            out[key] = unanchored
        else:
            out[key] = copy.deepcopy(value)
    return Reanchored(out, tuple(moved))


def _place(
    source: dict[str, Any],
    mapper: DiffMapper,
    left_ok: bool,
    old_lengths: Mapping[str, int] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """The finding at B, or ``(None, reason)`` when it moves."""
    path: str = source["path"]
    line: int | None = source["line"]
    placed = copy.deepcopy(source)
    if line is None:
        # File-level: follows a detected rename, is never validated, never moves.
        fate = mapper.follow_file(path)
        if not fate.removed:
            placed["path"] = _followed(path, fate.path)
        return placed, None
    if source.get("side") == "left":
        if not left_ok:
            return None, BASE_CHANGED
        fate = mapper.follow_file(path)
        if fate.removed:
            return None, FILE_REMOVED
        placed["path"] = _followed(path, fate.path)
        return placed, None
    start: int | None = source.get("start_line")
    if old_lengths is not None:
        length = old_lengths.get(path)
        if length is not None and line > length:
            return None, CHANGED
    result = mapper.map_right(path, line, start)
    if not isinstance(result, Mapped):
        return None, str(result.reason)
    placed["path"] = _followed(path, result.path)
    placed["line"] = result.line
    if start is not None:
        assert result.start_line is not None
        placed["start_line"] = result.start_line
    return placed, None
