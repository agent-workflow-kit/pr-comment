"""Re-anchoring a draft onto the PR's current head (docs/interface.md, Re-anchoring).

Pure functions over parsed forgectl results: no I/O. The caller reads ``compare`` and ``diff``
(posting plan, #27) and passes in what they returned. Re-anchoring is exact and never guesses: a
finding is kept at a new place only when the diff proves where its line went, and every other
finding moves, with a reason, to ``unanchored``, so none is dropped and none lands on other code.

Right-side findings go through ``DiffMapper.map_right``. Left-side findings refer to the base file
at the merge-base, so they are never mapped through the head-to-head diff: they keep their lines
only when both merge-bases are known and equal (step 3), and move (``file_removed``) when the
A-to-B diff deletes or renames their file, since a rename between A and B does not say which file
the base lines belonged to.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from .diffmap import DiffMapper, Mapped, MovedReason, printed_path
from .draft import MAX_LINE, Draft
from .output import Outcome, error_outcome

BASE_CHANGED: Final = "base_changed"
FILE_REMOVED: Final = "file_removed"

_SHA: Final = re.compile(r"[0-9a-f]{40}")


class StaleHeadError(Exception):
    """``compare`` answered for another head than B: exit 20 ``stale_head``."""

    def __init__(self, head_sha: str) -> None:
        super().__init__("compare did not answer for the pinned head")
        self.head_sha = head_sha

    def outcome(self) -> Outcome:
        return error_outcome(
            "refused",
            "the head moved while the draft was being re-anchored; rerun with the current head",
            reason="stale_head",
            details={"head_sha": self.head_sha},
        )


class CompareContractError(Exception):
    """``compare`` is not shaped as forgectl's schema says: exit 12 ``lower_contract_mismatch``."""

    def outcome(self) -> Outcome:
        return error_outcome(
            "lower_contract_mismatch",
            "forgectl compare returned a result that does not match its contract",
            details={"command": "compare", "lower_version": None},
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


def _sha_field(compare: Mapping[str, Any], name: str) -> str:
    value = compare.get(name)
    if not isinstance(value, str) or _SHA.fullmatch(value.lower()) is None:
        raise CompareContractError(name)
    return value.lower()


def mapper_from_compare(
    compare: Mapping[str, Any] | None, *, head_sha: str, from_sha: str
) -> DiffMapper:
    """The right-side mapper for a parsed ``forgectl compare`` result (step 1).

    ``compare`` is ``None`` when forgectl exited 20 ``too_large``. A result that is not shaped as
    the schema says (not a mapping, a ``to_sha`` that is not 40 hex characters, a ``diff`` that is
    neither text nor null, or a ``from_sha`` that is not the required ``from_sha`` argument (A))
    raises ``CompareContractError``, failing closed. Otherwise its ``to_sha`` (lowercased) must
    equal ``head_sha`` (B), else ``StaleHeadError``. ``diff: null`` (``behind``, ``diverged``,
    ``unavailable``), ``too_large`` and a diff that cannot be read unambiguously give an
    unavailable mapper, which moves every right-side finding (``diff_unavailable``). An
    ``identical`` compare has an empty diff.
    """
    if compare is None:
        return DiffMapper.unavailable()
    if not isinstance(compare, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise CompareContractError("compare")
    to_sha = _sha_field(compare, "to_sha")
    if _sha_field(compare, "from_sha") != from_sha:
        raise CompareContractError("from_sha")
    diff = compare.get("diff")
    if diff is not None and not isinstance(diff, str):
        raise CompareContractError("diff")
    if to_sha != head_sha:
        raise StaleHeadError(to_sha)
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
) -> Reanchored:
    """Move ``draft``'s findings from its anchor head (A) onto ``head_sha`` (B).

    ``base_ref`` and ``base_sha`` come from ``show`` at B; ``merge_base_sha`` is the ``diff --json``
    read of step 3 (``None`` when unknown or not read). ``mapper`` is the A-to-B diff; when B
    equals A it is ignored and lines are kept. A right-side line past the end of A's file is not
    checked here: it maps past the end of B's file and the pre-check at B moves it.
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
        placed, reason = _place(source, mapper, left_ok)
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
    source: dict[str, Any], mapper: DiffMapper, left_ok: bool
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
        if fate.removed or _followed(path, fate.path) != path:
            # A rename between A and B says nothing about which file the base lines belonged to.
            return None, FILE_REMOVED
        return placed, None
    start: int | None = source.get("start_line")
    result = mapper.map_right(path, line, start)
    if not isinstance(result, Mapped):
        return None, str(result.reason)
    if result.line > MAX_LINE:
        # shifted past the largest line a draft (or a plan) can hold: no real file has it
        return None, MovedReason.CHANGED.value
    placed["path"] = _followed(path, result.path)
    placed["line"] = result.line
    if start is not None:
        assert result.start_line is not None
        placed["start_line"] = result.start_line
    return placed, None
