"""Audit records: one pair per command in ``audit.jsonl`` (docs/interface.md, Audit and log).

Interface for the posting orders (one ``AuditTrail`` per run)::

    trail = AuditTrail.open(state_dirs)       # appendability check, before confirmation
    ...                                       # (AuditUnavailableError: exit 12, nothing sent)
    trail.attempt_id                          # for log lines of this run
    trail.attempted(RunRecord(...))           # after the first write's pre-post hooks, right
                                              # before that write (AuditUnavailableError: exit 12)
    trail.outcome(writes, hook_decision_ids)  # after the last write (AuditFailedError: exit 12)
    trail.close()

TODO(#29): the posting orders call ``AuditTrail.open`` in step 1, before confirmation and before
any forgectl call, and ``attempted`` / ``outcome`` as described. A run with no forgectl write
calls neither ``attempted`` nor ``outcome`` (log only). Writes are
``WriteRecord``: ``from_lower`` for a forgectl call (its result follows ``classify_write``),
``blocked`` for a ``pre-post`` veto or escalation, which has no forgectl call. ``outcome`` takes
every write the run tried after confirmation, numbered from 0 in order, and returns the record's
result (``aggregate_result``). ``audit_failed_error`` builds the exit 12 envelope for an outcome
that could not be appended, with the result document in ``details.outcome``.

Decisions:

- **One line per record**, serialized with ``ensure_ascii``: the file is pure ASCII, so untrusted
  text (a base ref, forgectl ids, lone surrogates, control characters, ``U+2028``) can neither
  break a line nor reach a terminal raw, and nothing is lost: the escapes decode to the exact text.
- **Atomicity and size.** Appends go through ``appendfile``; a record is one ``write(2)`` of one
  line, at most ``appendfile.MAX_LINE_BYTES`` (1 MiB). A bigger record is refused, never cut.
  A torn line after a failed write is ended with a newline; readers skip malformed lines.
- **fsync.** Both records are fsynced before the call returns. The ``attempted`` record must be
  durable before a write that cannot be undone; the outcome is the only proof of what landed.
  A failing fsync counts as a failed append.
- **Pairing.** ``attempt_id`` is made once in ``open`` (128 random bits, URL-safe) and shared by
  both records and by the run's log lines. ``outcome`` before ``attempted``, either twice, and a
  run whose writes were all blocked before any forgectl call are programming errors
  (``ValueError``) and write nothing.
- **Failure mapping.** The ``attempted`` append, like ``open``, fails as ``audit_unavailable``
  (nothing was sent, ``landed: false``, ``retry: same_command``). An outcome that cannot be
  appended is ``audit_failed`` and always follows a write: ``landed`` and ``retry`` are the
  writes' own pair (``write_pair``), so a landed write is never offered as a retry.
- No bodies: the types hold hashes, counts, ids and the forgectl result fields only.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from . import __version__, grammars
from .actor import RunContext
from .appendfile import AppendError, AppendFile
from .errors import build_error, sanitize
from .files import StateDirs

AUDIT_NAME: Final = "audit.jsonl"
ACTIONS: Final = ("review", "approve", "comment", "reply", "resolve", "unresolve")
EVENTS: Final = ("comment", "request_changes", "approve", None)
ESCALATION_SCOPES: Final = ("pr-comment", "forgectl")
LANDED_RESULTS: Final = frozenset({"done", "already_posted", "partial"})
_WRITE_RESULTS: Final = (
    "done",
    "already_posted",
    "partial",
    "refused",
    "failed",
    "outcome_unknown",
    "vetoed",
    "escalated",
)
_BLOCKED: Final = ("vetoed", "escalated")
_PARTIAL_CODES: Final = frozenset(
    {"outcome_partial", "quick_action_ran", "approved_stale_head", "audit_failed"}
)

Clock = Callable[[], datetime]


class AuditError(Exception):
    """An audit file operation failed; ``details`` holds ``path`` and maybe ``errno``."""

    def __init__(self, message: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(sanitize(message))
        self.details: dict[str, Any] = dict(details or {})


class AuditUnavailableError(AuditError):
    """``audit.jsonl`` cannot be appended, nothing sent (exit 12 ``audit_unavailable``)."""


class AuditFailedError(AuditError):
    """The outcome record cannot be appended after a write (exit 12 ``audit_failed``).

    ``result`` is the record's result and ``write_pair`` the ``(landed, retry)`` pair of the
    writes, for ``errors.build_error(..., write=...)``.
    """

    def __init__(
        self,
        message: str,
        details: Mapping[str, Any] | None,
        result: str,
        write_pair: tuple[bool | None, str],
    ) -> None:
        super().__init__(message, details)
        self.result = result
        self.write_pair = write_pair


def now_utc() -> datetime:
    return datetime.now(UTC)


def format_ts(moment: datetime) -> str:
    """RFC 3339 UTC with milliseconds and a ``Z``."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --- write results -----------------------------------------------------------------------------


@dataclass(frozen=True)
class LowerResult:
    """What forgectl returned for one write (``writes[].lower``)."""

    exit: int
    code: str | None
    reason: str | None
    ids: tuple[str, ...] = ()
    thread_ids: tuple[str, ...] = ()
    review_id: str | None = None
    already_posted: bool | None = None
    #: forgectl's ``host_rejected`` with the ``details.leftover_drafts`` key present (whatever its
    #: value): a rerun is refused until a human acts. Not part of the record; it only decides the
    #: own ``audit_failed`` pair.
    leftover_drafts: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit": self.exit,
            "code": self.code,
            "reason": self.reason,
            "ids": list(self.ids),
            "thread_ids": list(self.thread_ids),
            "review_id": self.review_id,
            "already_posted": self.already_posted,
        }


def classify_write(lower: LowerResult) -> str:
    """A write's result from forgectl's: exit 0 ``done`` (``already_posted`` when it reported so),
    20 ``refused``, 12 ``outcome_unknown`` is ``outcome_unknown``, 12 ``outcome_partial``,
    ``quick_action_ran``, ``approved_stale_head`` and ``audit_failed`` are ``partial``, anything
    else non-zero is ``failed``."""
    if lower.exit == 0:
        return "already_posted" if lower.already_posted is True else "done"
    if lower.exit == 20:
        return "refused"
    if lower.exit == 12 and lower.code == "outcome_unknown":
        return "outcome_unknown"
    if lower.exit == 12 and lower.code in _PARTIAL_CODES:
        return "partial"
    return "failed"


def aggregate_result(results: Sequence[str]) -> str:
    """The record result of the writes' results.

    One write: its own. Several: ``done`` when all are ``done`` or ``already_posted``, else
    ``partial`` when any landed (``done``, ``already_posted`` or ``partial``), else
    ``outcome_unknown`` when any write's outcome is unknown, else the first write that is not
    ``done``. Raises ``ValueError`` for no writes or an unknown result.
    """
    if not results:
        raise ValueError("a record has at least one write")
    for item in results:
        if item not in _WRITE_RESULTS:
            raise ValueError(f"unknown write result {item!r}")
    if len(results) == 1:
        return results[0]
    clean = ("done", "already_posted")
    if all(item in clean for item in results):
        return "done"
    if any(item in LANDED_RESULTS for item in results):
        return "partial"
    if "outcome_unknown" in results:
        return "outcome_unknown"
    return next(item for item in results if item not in clean)


def write_pair_for(result: str, *, leftover_drafts: bool = False) -> tuple[bool | None, str]:
    """The ``(landed, retry)`` pair of a record result, for an own ``audit_failed``.

    Anything that landed (or partly landed) is ``(True, "none")``: a rerun could post twice
    (forgectl's code-specific ``outcome_partial`` variants are not carried: this is the side that
    can never double-post). ``leftover_drafts`` (a write forgectl rejected with leftover drafts,
    and no write landed) is ``(False, "none")``: a rerun is refused until a human acts; when another
    write's outcome is unknown too (that write may have landed) it is ``(None, "none")``. An unknown
    outcome alone is ``(None, "same_command")``; a write that did not land is
    ``(False, "same_command")``.
    """
    if result in LANDED_RESULTS:
        return True, "none"
    if leftover_drafts:
        return (None if result == "outcome_unknown" else False), "none"
    if result == "outcome_unknown":
        return None, "same_command"
    return False, "same_command"


@dataclass(frozen=True)
class WriteRecord:
    """One entry of the outcome's ``writes``."""

    index: int
    ts: str
    action: str
    thread_id: str | None
    dedupe_key: str | None
    result: str
    lower: LowerResult | None

    @property
    def leftover_drafts(self) -> bool:
        return self.lower is not None and self.lower.leftover_drafts

    def __post_init__(self) -> None:
        if type(self.index) is not int or self.index < 0:
            raise ValueError("write index must be an integer >= 0")
        if self.action not in ACTIONS:
            raise ValueError(f"unknown action {self.action!r}")
        if self.result not in _WRITE_RESULTS:
            raise ValueError(f"unknown write result {self.result!r}")
        if (self.result in _BLOCKED) != (self.lower is None):
            raise ValueError("a vetoed or escalated write has no forgectl result, others have one")

    @classmethod
    def from_lower(
        cls,
        index: int,
        ts: str,
        action: str,
        thread_id: str | None,
        dedupe_key: str | None,
        lower: LowerResult,
    ) -> WriteRecord:
        return cls(index, ts, action, thread_id, dedupe_key, classify_write(lower), lower)

    @classmethod
    def blocked(
        cls,
        index: int,
        ts: str,
        action: str,
        thread_id: str | None,
        dedupe_key: str | None,
        result: str,
    ) -> WriteRecord:
        """A write stopped by a ``pre-post`` veto (``vetoed``) or escalation (``escalated``)."""
        if result not in _BLOCKED:
            raise ValueError("a blocked write is vetoed or escalated")
        return cls(index, ts, action, thread_id, dedupe_key, result, None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "ts": self.ts,
            "action": self.action,
            "thread_id": self.thread_id,
            "dedupe_key": self.dedupe_key,
            "result": self.result,
            "lower": None if self.lower is None else self.lower.to_dict(),
        }


# --- the run's fields --------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditInputs:
    """The record's ``inputs``: hashes, ids and counts, never text of a body.

    ``bodies`` entries are ``{role, sha256, bytes}`` and ``counts`` is the plan's counts object
    (``defs.json#/$defs/counts``). ``config_hash`` and ``config_path`` come from ``LoadedConfig``.
    """

    event: str | None
    draft_id: str | None
    draft_sha256: str | None
    anchor_head: str | None
    reanchor_to: str | None
    pinned_head: str
    base_ref: str | None
    dedupe_key: str | None
    to: str | None
    threads: Sequence[str]
    bodies: Sequence[Mapping[str, Any]]
    counts: Mapping[str, Any]
    plan_sha256: str | None
    allowed_mentions: Sequence[str]
    override_escalation: Sequence[str]
    config_hash: str
    config_path: str | None

    def __post_init__(self) -> None:
        if self.event not in EVENTS:
            raise ValueError(f"unknown event {self.event!r}")
        scopes = list(self.override_escalation)
        if any(item not in ESCALATION_SCOPES for item in scopes) or len(set(scopes)) != len(scopes):
            raise ValueError(
                "override_escalation holds each of pr-comment and forgectl at most once"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "draft_id": self.draft_id,
            "draft_sha256": self.draft_sha256,
            "anchor_head": self.anchor_head,
            "reanchor_to": self.reanchor_to,
            "pinned_head": self.pinned_head,
            "base_ref": self.base_ref,
            "dedupe_key": self.dedupe_key,
            "to": self.to,
            "threads": list(self.threads),
            "bodies": [dict(item) for item in self.bodies],
            "counts": dict(self.counts),
            "plan_sha256": self.plan_sha256,
            "allowed_mentions": list(self.allowed_mentions),
            "override_escalation": list(self.override_escalation),
            "config_hash": self.config_hash,
            "config_path": self.config_path,
        }


@dataclass(frozen=True)
class RunRecord:
    """The fields both records of a pair share. ``override`` is derived from the inputs."""

    run: RunContext
    decision_ids: Sequence[str]
    action: str
    target: str
    pr_id: str
    inputs: AuditInputs

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"unknown action {self.action!r}")
        if len(self.decision_ids) > grammars.MAX_DECISION_IDS:
            raise ValueError("too many decision ids")
        for item in self.decision_ids:
            grammars.check_decision_id(item)

    @property
    def override(self) -> list[str]:
        out: list[str] = []
        if self.inputs.override_escalation:
            out.append("override_escalation")
        if self.inputs.allowed_mentions:
            out.append("allow_mention")
        return out


# --- the trail ---------------------------------------------------------------------------------


def _line(record: Mapping[str, Any]) -> bytes:
    """One ASCII line. Raises ``ValueError`` for what JSON cannot hold (NaN, other types, depth)."""
    try:
        text = json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"record cannot be serialized ({type(exc).__name__})") from None
    return text.encode("ascii") + b"\n"


class AuditTrail:
    """The run's ``audit.jsonl``: its appendability check, then the ``attempted``/outcome pair."""

    def __init__(self, file: AppendFile, clock: Clock = now_utc) -> None:
        self._file = file
        self._clock = clock
        self.attempt_id = secrets.token_urlsafe(16)
        self._record: RunRecord | None = None
        self._attempted = False
        self._done = False
        # A write that may be on disk (partial, or fsync failed): never appended a second time.
        self._attempted_error: AuditUnavailableError | None = None
        self._outcome_error: AuditFailedError | None = None

    @classmethod
    def open(cls, dirs: StateDirs, *, clock: Clock = now_utc) -> AuditTrail:
        """Check ``audit.jsonl`` can be appended (no-follow, regular file, 0600 on creation).

        Raises ``AuditUnavailableError``.
        """
        try:
            file = AppendFile.open(dirs.state_fd, AUDIT_NAME, dirs.state_path / AUDIT_NAME)
        except AppendError as exc:
            raise AuditUnavailableError(
                f"audit.jsonl cannot be appended: {exc}", exc.details
            ) from exc
        return cls(file, clock)

    def close(self) -> None:
        self._file.close()

    def _base(self, record: RunRecord, result: str) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ts": format_ts(self._clock()),
            "tool": "pr-comment",
            "version": __version__,
            "attempt_id": self.attempt_id,
            **record.run.audit_fields(),
            "decision_id": list(record.decision_ids),
            "override": record.override,
            "action": record.action,
            "target": record.target,
            "pr_id": record.pr_id,
            "inputs": record.inputs.to_dict(),
            "result": result,
        }
        return out

    def attempted(self, record: RunRecord) -> None:
        """Append the ``attempted`` record, immediately before the first forgectl write.

        Raises ``AuditUnavailableError`` (nothing was sent). When the append failed before any
        byte could be on disk the call may be repeated; when the line may already be on disk
        (a partial write, or an fsync that failed) a repeat raises the same error without
        writing, so one ``attempt_id`` never has two ``attempted`` records.
        """
        if self._attempted:
            raise ValueError("the attempted record was already written")
        if self._attempted_error is not None:
            raise self._attempted_error
        try:
            line = _line(self._base(record, "attempted"))
            self._file.append(line, sync=True)
        except ValueError as exc:
            raise AuditUnavailableError(str(exc), {"path": str(AUDIT_NAME)}) from None
        except AppendError as exc:
            error = AuditUnavailableError(f"audit.jsonl cannot be appended: {exc}", exc.details)
            if exc.written:
                self._attempted_error = error
            raise error from exc
        self._record = record
        self._attempted = True

    def outcome(self, writes: Sequence[WriteRecord], hook_decision_ids: Sequence[str] = ()) -> str:
        """Append the outcome record with every write the run tried; return its result.

        Raises ``AuditFailedError`` when it cannot be appended and ``ValueError`` for a call out
        of order or malformed ``writes``. When the append failed before any byte could be on disk
        the call may be repeated; when the line may already be on disk (a partial write, or an
        fsync that failed) a repeat raises the same error without writing.
        """
        if self._record is None or not self._attempted:
            raise ValueError("outcome needs the attempted record first")
        if self._done:
            raise ValueError("the outcome record was already written")
        if self._outcome_error is not None:
            raise self._outcome_error
        if not writes or [w.index for w in writes] != list(range(len(writes))):
            raise ValueError("writes are numbered 0, 1, 2, ... in order, at least one")
        if all(w.lower is None for w in writes):
            raise ValueError("every write was blocked before forgectl: nothing to record")
        if len(hook_decision_ids) > grammars.MAX_DECISION_IDS:
            raise ValueError("too many hook decision ids")
        for item in hook_decision_ids:
            grammars.check_decision_id(item)
        result = aggregate_result([w.result for w in writes])
        leftover = any(w.leftover_drafts for w in writes)
        pair = write_pair_for(result, leftover_drafts=leftover)
        doc = self._base(self._record, result)
        doc["hook_decision_id"] = list(hook_decision_ids)
        doc["writes"] = [w.to_dict() for w in writes]
        try:
            self._file.append(_line(doc), sync=True)
        except ValueError as exc:
            raise AuditFailedError(str(exc), {"path": str(AUDIT_NAME)}, result, pair) from None
        except AppendError as exc:
            error = AuditFailedError(
                f"the audit outcome could not be appended: {exc}", exc.details, result, pair
            )
            if exc.written:
                self._outcome_error = error
            raise error from exc
        self._done = True
        return result


def audit_failed_error(
    exc: AuditFailedError, outcome_document: Mapping[str, Any]
) -> dict[str, Any]:
    """The exit 12 ``audit_failed`` envelope: the command's result document goes in
    ``details.outcome``; ``landed`` and ``retry`` are the writes' own pair."""
    details = dict(exc.details)
    details["outcome"] = dict(outcome_document)
    return build_error("audit_failed", str(exc), details=details, write=exc.write_pair)


def audit_unavailable_error(exc: AuditUnavailableError) -> dict[str, Any]:
    """The exit 12 ``audit_unavailable`` envelope (nothing was sent: ``retry: same_command``)."""
    return build_error("audit_unavailable", str(exc), details=exc.details)
