"""``log.jsonl`` events (docs/interface.md, Audit and log; schemas/log.json).

Interface::

    log = LogWriter.open(state_dirs, verbose=args.verbose)   # never raises
    log.write("refused", pr=pr, attempt_id=trail.attempt_id, details={...})
    log.close()

``write`` returns whether a line was written and never raises for a file problem: a log write
failure only warns (once, on the ``pr_comment`` logger, which prints to stderr).
``LogWriter.disabled()`` writes nothing, for a run whose state directory could not be opened.

- Records are one ASCII line each (``ensure_ascii``), so untrusted ``details`` cannot break the
  line or reach a terminal raw. Callers must never put a body in ``details``.
- A line is capped at ``MAX_LOG_LINE_BYTES``: bigger ``details`` are replaced by
  ``{"truncated": true}`` and the event is kept.
- The default level follows the interface: ``warn`` for refusals, vetoes, escalations, declined
  dialogs, warnings and config warnings, ``info`` otherwise. The caller passes ``error`` (exit 1)
  or ``warn`` (exit 12) when the exit code decides, and ``debug`` for records that exist only with
  ``--verbose``; a ``debug`` record without ``verbose`` is dropped.
- Log lines are not fsynced (the audit file is the durable record); like every appended file they
  are opened ``O_APPEND | O_NOFOLLOW``, regular file only, 0600 on creation (``appendfile``).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from .appendfile import AppendError, AppendFile
from .audit import format_ts, now_utc
from .config import ConfigWarning
from .errors import sanitize
from .files import StateDirs
from .output import LOG

LOG_NAME: Final = "log.jsonl"
MAX_LOG_LINE_BYTES: Final = 64 * 1024
EVENTS: Final = (
    "usage_error",
    "refused",
    "declined",
    "vetoed",
    "escalated",
    "finding_moved",
    "suggestion_degraded",
    "precheck",
    "warning",
    "config_warning",
    "neutralizing_mismatch",
)
LEVELS: Final = ("error", "warn", "info", "debug")
_WARN_EVENTS: Final = frozenset(
    {"refused", "declined", "vetoed", "escalated", "warning", "config_warning"}
)


def _encode(record: Mapping[str, Any]) -> bytes:
    text = json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    return text.encode("ascii") + b"\n"


class LogWriter:
    """The run's ``log.jsonl``."""

    def __init__(
        self,
        file: AppendFile | None,
        *,
        verbose: bool = False,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self._file = file
        self._verbose = verbose
        self._clock = clock
        self._warned = False

    @classmethod
    def disabled(cls) -> LogWriter:
        return cls(None)

    @classmethod
    def open(
        cls,
        dirs: StateDirs,
        *,
        verbose: bool = False,
        clock: Callable[[], datetime] = now_utc,
    ) -> LogWriter:
        """Open ``log.jsonl``; a file that cannot be appended warns and gives a silent writer."""
        try:
            file = AppendFile.open(dirs.state_fd, LOG_NAME, dirs.state_path / LOG_NAME)
        except AppendError as exc:
            writer = cls(None, verbose=verbose, clock=clock)
            writer._warn(exc)
            return writer
        return cls(file, verbose=verbose, clock=clock)

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def _warn(self, exc: BaseException) -> None:
        if self._warned:
            return
        self._warned = True
        LOG.warning("log.jsonl not written: %s", sanitize(str(exc)))

    def write(
        self,
        event: str,
        *,
        level: str | None = None,
        pr: str | None = None,
        attempt_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> bool:
        """Append one record; return whether it was written. Raises ``ValueError`` only for an
        unknown ``event`` or ``level``."""
        if event not in EVENTS:
            raise ValueError(f"unknown log event {event!r}")
        if level is None:
            level = "warn" if event in _WARN_EVENTS else "info"
        if level not in LEVELS:
            raise ValueError(f"unknown log level {level!r}")
        if level == "debug" and not self._verbose:
            return False
        if self._file is None:
            return False
        record: dict[str, Any] = {
            "ts": format_ts(self._clock()),
            "level": level,
            "event": event,
            "pr": pr,
            "attempt_id": attempt_id,
            "details": None if details is None else dict(details),
        }
        try:
            try:
                line = _encode(record)
            except (TypeError, ValueError, RecursionError) as exc:
                raise ValueError(f"record cannot be serialized ({type(exc).__name__})") from None
            if len(line) > MAX_LOG_LINE_BYTES:
                record["details"] = {"truncated": True}
                line = _encode(record)
            self._file.append(line)
        except (ValueError, AppendError) as exc:
            self._warn(exc)
            return False
        return True


def write_config_warnings(log: LogWriter, warnings: Sequence[ConfigWarning]) -> None:
    """One ``config_warning`` line per file that sets ``allow_auto_approve`` or a ``mentions.*``
    key: ``details`` is ``{file, keys}``."""
    for item in warnings:
        log.write("config_warning", details={"file": item.file, "keys": list(item.keys)})
