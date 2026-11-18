"""``pr-comment export``: print ``audit.jsonl`` (or, with ``--log``, ``log.jsonl``) records.

``handle_export`` reads no config and no network. Both files are local but untrusted on read:

- The file is opened relative to the checked state directory with ``O_NOFOLLOW | O_NONBLOCK`` and
  must be a regular file owned by the user, with one hard link and not writable by group or others
  (the writers' check, ``appendfile.refuse_unsafe``): a symlink, a FIFO, a directory or a shared
  file is exit 12 ``audit_unavailable``. A missing file is empty output.
- Lines are read with a size limit (the writers' own caps: 1 MiB for audit, 64 KiB for the log); a
  longer line is skipped without being held whole. A line without its newline (a torn last line),
  or one that is not pure ASCII JSON, not an object, or not a record of its file's schema in the
  fields export relies on, is skipped with one warning per line that names only the line number.
  Warnings stop after ``MAX_WARNINGS``; a count of the rest follows.
- Every record is re-serialized as ASCII JSON (and DEL escaped), so file content never reaches the
  terminal raw whatever the file held.
- Memory is bounded: the log streams line by line; the audit file takes one pass that remembers
  only the line number of the last record per ``attempt_id`` and kind (at most ``MAX_TRACKED``),
  then a second pass that prints those lines. The records are printed in file order (append
  order), each pair member at the place of its last copy.

``--since`` keeps records whose ``ts`` is at or after the instant (compared as instants, not text);
the last record per ``attempt_id`` and kind decides first, so a superseded copy never shows.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, BinaryIO, Final, Literal

from . import grammars
from .appendfile import MAX_LINE_BYTES, AppendError, refuse_unsafe
from .audit import AUDIT_NAME, now_utc
from .files import StateDirs, StateUnsafeError, open_state_dirs
from .jsonlog import EVENTS as LOG_EVENTS
from .jsonlog import LEVELS as LOG_LEVELS
from .jsonlog import LOG_NAME, MAX_LOG_LINE_BYTES
from .output import LOG, Outcome, error_outcome

MAX_WARNINGS: Final = 20
#: Distinct (attempt_id, kind) pairs held at once while scanning audit.jsonl; more is an error
#: that asks for a narrower ``--since``.
MAX_TRACKED: Final = 200_000
_FLUSH_BYTES: Final = 64 * 1024
_SKIP_CHUNK: Final = 64 * 1024

AUDIT_RESULTS: Final = (
    "attempted",
    "done",
    "already_posted",
    "partial",
    "refused",
    "failed",
    "outcome_unknown",
    "vetoed",
    "escalated",
)
AUDIT_ACTIONS: Final = ("review", "approve", "comment", "reply", "resolve", "unresolve")
AUDIT_ACTOR_SOURCES: Final = ("flag", "tty", "default")
_ATTEMPT_ID_RE: Final = re.compile(r"[A-Za-z0-9_-]{8,64}")

LineStatus = Literal["ok", "blank", "long", "torn"]


class _TooManyRecords(Exception):
    pass


# --- reading lines ---------------------------------------------------------------------------


def _read_lines(fh: BinaryIO, limit: int) -> Iterator[tuple[int, LineStatus, bytes]]:
    """Numbered lines of ``fh`` (from 1), never holding more than ``limit`` bytes of one.

    ``limit`` counts the newline. A longer line is read past (in chunks) and reported ``long``
    with no content; a last line without its newline is ``torn``; a whitespace-only one is
    ``blank``.
    """
    number = 0
    while True:
        raw = fh.readline(limit + 1)
        if not raw:
            return
        number += 1
        if len(raw) > limit:
            while not raw.endswith(b"\n"):
                raw = fh.readline(_SKIP_CHUNK)
                if not raw:
                    break
            yield number, "long", b""
        elif not raw.endswith(b"\n"):
            yield number, "torn", b""
        elif not raw.strip():
            yield number, "blank", b""
        else:
            yield number, "ok", raw


# --- parsing and checking one record -----------------------------------------------------------


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _no_constant(_: str) -> Any:
    raise ValueError("not JSON")


def _is_attempt_id(value: object) -> bool:
    return type(value) is str and _ATTEMPT_ID_RE.fullmatch(value) is not None


def _all_str(rec: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return all(type(rec.get(key)) is str for key in keys)


def _check_audit(rec: dict[str, Any]) -> str | None:
    """The reason ``rec`` is not an audit record, or ``None``.

    Checks the fields export relies on and the container types; deeper content is the writer's
    schema (schemas/audit.json), and extra fields pass through.
    """
    if rec.get("tool") != "pr-comment" or not _is_attempt_id(rec.get("attempt_id")):
        return "not an audit record"
    result = rec.get("result")
    if type(result) is not str or result not in AUDIT_RESULTS:
        return "unknown result"
    if (
        not _all_str(rec, ("version", "actor", "target", "pr_id"))
        or rec.get("actor_source") not in AUDIT_ACTOR_SOURCES
        or rec.get("action") not in AUDIT_ACTIONS
        or type(rec.get("stdin_tty")) is not bool
        or type(rec.get("decision_id")) is not list
        or type(rec.get("override")) is not list
        or type(rec.get("inputs")) is not dict
    ):
        return "missing or ill-typed field"
    if result == "attempted":
        if "writes" in rec or "hook_decision_id" in rec:
            return "missing or ill-typed field"
    else:
        writes = rec.get("writes")
        if type(writes) is not list or not writes or type(rec.get("hook_decision_id")) is not list:
            return "missing or ill-typed field"
    return None


def _check_log(rec: dict[str, Any]) -> str | None:
    if rec.get("level") not in LOG_LEVELS or rec.get("event") not in LOG_EVENTS:
        return "not a log record"
    attempt_id = rec.get("attempt_id", 0)
    pr = rec.get("pr", 0)
    details = rec.get("details", 0)
    if (
        not (attempt_id is None or _is_attempt_id(attempt_id))
        or not (pr is None or type(pr) is str)
        or not (details is None or type(details) is dict)
    ):
        return "missing or ill-typed field"
    return None


@dataclass(frozen=True)
class _Kind:
    name: str
    limit: int
    check: Callable[[dict[str, Any]], str | None]
    dedupe: bool


_AUDIT = _Kind(AUDIT_NAME, MAX_LINE_BYTES, _check_audit, True)
_LOG = _Kind(LOG_NAME, MAX_LOG_LINE_BYTES, _check_log, False)


def _load(raw: bytes, kind: _Kind) -> tuple[dict[str, Any], datetime] | str:
    """The record and its time, or the (fixed text) reason the line is skipped."""
    body = raw[:-1]
    if not body.isascii():
        return "not ASCII"
    try:
        value: Any = json.loads(
            body.decode("ascii"), object_pairs_hook=_no_duplicates, parse_constant=_no_constant
        )
    except (ValueError, RecursionError):
        return "not valid JSON"
    if type(value) is not dict:
        return "not a JSON object"
    rec: dict[str, Any] = value  # pyright: ignore[reportUnknownVariableType]
    moment = grammars.parse_rfc3339(rec.get("ts"))
    if moment is None:
        return "bad ts"
    reason = kind.check(rec)
    if reason is not None:
        return reason
    return rec, moment


class _Warner:
    """One warning per skipped line, capped; never includes the line's content."""

    def __init__(self, name: str) -> None:
        self._name = name
        self.skipped = 0

    def skip(self, number: int, reason: str) -> None:
        self.skipped += 1
        if self.skipped <= MAX_WARNINGS:
            LOG.warning("%s: line %d skipped (%s)", self._name, number, reason)

    def finish(self) -> None:
        if self.skipped > MAX_WARNINGS:
            LOG.warning(
                "%s: %d more malformed lines skipped", self._name, self.skipped - MAX_WARNINGS
            )


_STATUS_REASON: Final = {"long": "line too long", "torn": "torn line (no newline)"}


def _records(
    fh: BinaryIO, kind: _Kind, warner: _Warner | None
) -> Iterator[tuple[int, dict[str, Any], datetime]]:
    """The valid records of ``fh`` with their line numbers; skipped lines go to ``warner``."""
    for number, status, raw in _read_lines(fh, kind.limit):
        if status == "blank":
            continue
        if status != "ok":
            if warner is not None:
                warner.skip(number, _STATUS_REASON[status])
            continue
        loaded = _load(raw, kind)
        if isinstance(loaded, str):
            if warner is not None:
                warner.skip(number, loaded)
            continue
        yield number, loaded[0], loaded[1]


def _pair_key(rec: dict[str, Any]) -> str:
    return f"{rec['attempt_id']}{'a' if rec['result'] == 'attempted' else 'o'}"


# --- selecting and printing ---------------------------------------------------------------------


def _winners(fh: BinaryIO, cutoff: datetime | None, warner: _Warner) -> set[int]:
    """Line numbers of the last record per (attempt_id, kind) that is inside the window.

    A later copy outside the window removes the key: the last copy decides, then the filter.
    """
    last: dict[str, int] = {}
    for number, rec, moment in _records(fh, _AUDIT, warner):
        key = _pair_key(rec)
        if cutoff is None or moment >= cutoff:
            if key not in last and len(last) >= MAX_TRACKED:
                raise _TooManyRecords
            last[key] = number
        else:
            last.pop(key, None)
    return set(last.values())


def _emit_record(rec: dict[str, Any]) -> str:
    """One record as compact ASCII JSON. ``ensure_ascii`` escapes controls and non-ASCII; DEL
    is the one control character it leaves, and is escaped here."""
    text = json.dumps(rec, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    return text.replace("\x7f", "\\u007f")


def _selected(
    fh: BinaryIO,
    kind: _Kind,
    cutoff: datetime | None,
    winners: set[int] | None,
    warner: _Warner | None,
) -> Iterator[dict[str, Any]]:
    last_wanted = max(winners) if winners else 0
    for number, rec, moment in _records(fh, kind, warner):
        if winners is not None:
            if number > last_wanted:
                return
            if number not in winners:
                continue
        if cutoff is None or moment >= cutoff:
            yield rec


def _open_input(dirs: StateDirs, name: str) -> BinaryIO | None:
    """Open ``name`` below the state directory for reading, or ``None`` when it does not exist.

    Raises ``AppendError`` for what must not be read (a link, a non-regular, shared or
    hard-linked file) or cannot be opened.
    """
    path = dirs.state_path / name
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        fd = os.open(name, flags, dir_fd=dirs.state_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AppendError("cannot open (a symlink, or no access)", path, exc) from exc
    try:
        refuse_unsafe(fd, path)
        return os.fdopen(fd, "rb", buffering=_FLUSH_BYTES)
    except BaseException as exc:
        os.close(fd)
        if isinstance(exc, OSError):
            raise AppendError("cannot check the file", path, exc) from exc
        raise


def handle_export(args: argparse.Namespace) -> Outcome:
    """The ``export`` handler; reads ``args.since``, ``args.log`` and ``args.json``."""
    kind = _LOG if args.log else _AUDIT
    cutoff = None if args.since is None else grammars.since_cutoff(args.since, now_utc())
    try:
        with open_state_dirs() as dirs:
            fh = _open_input(dirs, kind.name)
            path = dirs.state_path / kind.name
    except StateUnsafeError as exc:
        return error_outcome("state_unsafe", str(exc), details=exc.details)
    except AppendError as exc:
        return error_outcome(
            "audit_unavailable", f"cannot read {kind.name}: {exc}", details=exc.details
        )
    as_json = bool(args.json)
    warner = _Warner(kind.name)
    winners: set[int] | None = None
    if fh is not None and kind.dedupe:
        try:
            winners = _winners(fh, cutoff, warner)
            warner.finish()
            fh.seek(0)
        except _TooManyRecords:
            fh.close()
            return error_outcome(
                "audit_unavailable",
                f"{kind.name} has too many records to export at once; narrow it with --since",
                details={"path": str(path)},
            )
        except BaseException:
            fh.close()
            raise

    def stream(write: Callable[[str], None]) -> None:
        pieces: list[str] = []
        size = 0
        count = 0

        def flush() -> None:
            nonlocal size
            if pieces:
                write("".join(pieces))
                pieces.clear()
                size = 0

        try:
            if as_json:
                pieces.append("[")
            if fh is not None:
                report = None if kind.dedupe else warner
                for rec in _selected(fh, kind, cutoff, winners, report):
                    text = _emit_record(rec)
                    piece = ("," if as_json and count else "") + text + ("" if as_json else "\n")
                    count += 1
                    pieces.append(piece)
                    size += len(piece)
                    if size >= _FLUSH_BYTES:
                        flush()
                if not kind.dedupe:
                    warner.finish()
            if as_json:
                pieces.append("]\n")
            flush()
        finally:
            if fh is not None:
                fh.close()

    return Outcome(0, stream=stream)
