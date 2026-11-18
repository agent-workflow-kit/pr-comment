"""The confirmation dialog: the plan and its summary on stderr, one answer from the terminal.

This is the human gate (docs/interface.md, Confirmation), so the rules are strict:

* Everything untrusted that reaches the terminal (bodies, paths, finding ids, names) goes through
  ``display.py``. Posted text and ``--json`` output never do: the plan is untouched.
* Every line of a body, an inline comment or any other untrusted text sits behind a fixed gutter
  (``GUTTER``), so untrusted text can never start a line that looks like pr-comment's own
  headings, the summary or the prompt. A line longer than the terminal is hard-wrapped, with the
  gutter on every part, so a soft wrap cannot start a flush-left line either. A lone CR, ESC,
  bidi controls and C1 controls are marked by ``display.py``; nothing is dropped or truncated,
  so what the human reads is the plan.
* The hash shown is ``plan.plan_sha256``; the counts shown are the plan's, labelled as upper
  bounds where the interface says so.
* The answer is read from the real terminal: the caller passes the stdin descriptor and it must
  be a TTY at the prompt (a pipe, a file or ``/dev/null`` declines). Input typed before the prompt
  was shown is discarded, a line is at most ``MAX_ANSWER`` bytes, and the answer is compared
  with ``y`` or ``yes`` after trimming ASCII whitespace and ignoring ASCII case. Anything else, EOF
  or a read error declines. The answer is never echoed back by pr-comment.
* Nothing in the environment can answer for the human: no variable is read here.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import unicodedata
from collections.abc import Callable, Iterator
from typing import Any

from .display import marker, sanitize_line, sanitize_text
from .plan import FollowUp, Plan
from .prepare import Prepared

GUTTER = "  | "
DEFAULT_WIDTH = 80
MIN_ROOM = 1  # one character per line is the least that still makes progress
PROMPT = "Post? [y/N] "
MAX_ANSWER = 256
ALREADY_POSTED = (
    "forgectl reports this may already be posted; confirming posts nothing again and "
    "completes any pending approval or change request"
)
YES = frozenset({b"y", b"yes"})
_WHITESPACE = b" \t\r\n\x0b\x0c"


class Interrupted(Exception):
    """SIGINT, SIGTERM or SIGHUP arrived at the dialog: nothing was sent, exit ``128 + signum``
    with no envelope (as a signal before any write does elsewhere)."""

    def __init__(self, signum: int) -> None:
        super().__init__("interrupted")
        self.signum = signum


# --- the text --------------------------------------------------------------------------------


def _line(text: str) -> str:
    return sanitize_line(text)


def _cells(ch: str) -> int:
    """Terminal columns one character may take, rounded up: wide, fullwidth and ambiguous-width
    characters count 2, a tab 8, everything else (combining marks included) 1."""
    if ch == "\t":
        return 8
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 1


def _wrap(line: str, room: int) -> list[str]:
    """``line`` cut into parts of at most ``room`` columns (at least one part, nothing dropped)."""
    parts: list[str] = []
    part: list[str] = []
    used = 0
    for ch in line:
        need = _cells(ch)
        if part and used + need > room:
            parts.append("".join(part))
            part, used = [], 0
        part.append(ch)
        used += need
    parts.append("".join(part))
    return parts


def _gutter(text: str, width: int, encoding: str = "utf-8") -> list[str]:
    """Untrusted multi-line text as display lines, every one (and every wrapped part of one)
    behind the gutter. One column stays free so a full line never relies on the terminal's own
    wrap. Characters the encoding cannot carry are replaced by their marker before wrapping, so
    the wrapped lines are the lines that are shown."""
    room = max(MIN_ROOM, width - len(GUTTER) - 1)
    shown = encodable(sanitize_text(text), encoding)
    return [GUTTER + part for line in shown.split("\n") for part in _wrap(line, room)]


def _headline(plan: Plan) -> str:
    """The action and event in capitals: ``REVIEW: REQUEST_CHANGES``, ``APPROVE``, ``COMMENT``."""
    what = plan.action.upper()
    if plan.event is not None and plan.event != plan.action:
        what += f": {plan.event.upper()}"
    return what


def _follow_up(item: FollowUp) -> str:
    thread = "its thread is known after the post" if item.thread_id is None else item.thread_id
    return f"{item.action} ({_line(thread)})"


def plan_lines(plan: Plan, width: int = DEFAULT_WIDTH, encoding: str = "utf-8") -> list[str]:
    """The full plan: every body as forgectl will post it (with its dedupe line), then where each
    inline comment goes. Untrusted text is behind the gutter."""
    lines = [f"plan_sha256 {_line(plan.plan_sha256)}"]
    spots = {c.role: c for c in plan.inline_comments}
    for body in plan.shown_bodies():
        spot = spots.get(body.role)
        where = ""
        if spot is not None:
            start = f"{spot.start_line}-" if spot.start_line is not None else ""
            where = f" at {_line(spot.path)}:{start}{spot.line} ({_line(spot.side)})"
        lines.append(f"--- {_line(body.role)}{where}, {body.bytes} bytes")
        lines.extend(_gutter(body.text, width, encoding))
    return lines


def _count_bits(counts: dict[str, Any]) -> list[str]:
    """The summary's count lines. ``links``, ``images`` and ``raw_html`` are upper bounds."""
    moved: dict[str, int] = counts.get("moved", {})
    moved_total = sum(moved.values())
    by_reason = ", ".join(f"{reason} {n}" for reason, n in moved.items() if n)
    severity: dict[str, int] = counts.get("severity", {})
    sev = ", ".join(f"{name} {n}" for name, n in severity.items() if n) or "none"
    return [
        f"inline {counts.get('inline', 0)}, file-level {counts.get('file_level', 0)}, "
        f"moved {moved_total}" + (f" ({by_reason})" if by_reason else ""),
        f"findings by severity: {sev}",
        f"suggestions degraded {counts.get('suggestions_degraded', 0)}, "
        f"mentions neutralized {counts.get('mentions_neutralized', 0)}",
        # open decision 1: counts.links is 0 for ordinary and javascript: links, so it is not
        # claimed to be complete here, and neither are the other two.
        f"links at most {counts.get('links', 0)} (not every link form is counted), "
        f"images at most {counts.get('images', 0)}, raw HTML at most {counts.get('raw_html', 0)}",
        f"invisible characters marked {counts.get('invisible', 0)}",
    ]


def summary_lines(prepared: Prepared) -> list[str]:
    plan = prepared.plan
    lines = [
        _headline(plan),
        f"PR {_line(plan.pr)}",
        f"head {_line(plan.head_sha)}, base {_line(plan.base_ref)}",
        f"actor {_line(plan.actor)}",
    ]
    if (
        plan.event == "approve"
        and plan.anchor_head is not None
        and plan.anchor_head != plan.head_sha
    ):
        # Approving at a head other than the one the findings were drafted at.
        lines.append(
            f"APPROVING {_line(plan.head_sha[:12])}; "
            f"FINDINGS DRAFTED AT {_line(plan.anchor_head[:12])}"
        )
    if plan.to is not None:
        lines.append(f"reply to {_line(plan.to)}")
    lines.extend(_count_bits(dict(plan.counts)))
    lines.append(
        "text: `<` and `![` are neutralized in what is posted (replaced by look-alike "
        f"characters; {plan.neutralized_lines} command lines spaced, "
        f"{plan.markers_removed} markers removed)"
    )
    if plan.mentions_neutralized:
        names = ", ".join(_line(n) for n in plan.mentions_neutralized)
        lines.append(f"mentions neutralized: {names}")
    if plan.mentions_policy == "allow":
        lines.append("mentions allowed this run: all (mentions.policy is allow)")
    elif plan.mentions_allowed:
        names = ", ".join(_line(n) for n in plan.mentions_allowed)
        lines.append("mentions allowed this run: " + names)
    if prepared.precheck.already_posted:
        lines.append(ALREADY_POSTED)
    for item in plan.follow_ups:
        lines.append(f"then: {_follow_up(item)}")
    return lines


def encodable(text: str, encoding: str) -> str:
    """``text`` with every character the stream's encoding cannot carry shown as ``<U+XXXX>``, so
    a neutralized look-alike never turns into the character it imitates (``＜`` into ``<``)."""
    try:
        text.encode(encoding)
        return text
    except UnicodeEncodeError:
        pass
    except LookupError:
        return encodable(text, "utf-8")
    out: list[str] = []
    for ch in text:
        try:
            ch.encode(encoding)
            out.append(ch)
        except UnicodeEncodeError:
            out.append(marker(ch))
    return "".join(out)


def dialog_text(prepared: Prepared, width: int = DEFAULT_WIDTH, encoding: str = "utf-8") -> str:
    """The whole dialog before the prompt: the plan, then the summary just above the prompt."""
    out = ["== plan =="]
    out += plan_lines(prepared.plan, width, encoding)
    out.append("== summary ==")
    out += summary_lines(prepared)
    return encodable("\n".join(out) + "\n", encoding)


# --- the answer ------------------------------------------------------------------------------


def parse_answer(raw: bytes) -> bool:
    """True only for ``y`` or ``yes`` (ASCII case-insensitive) around ASCII whitespace."""
    return raw.strip(_WHITESPACE).lower() in YES


def _flush_input(fd: int) -> None:
    """Discard input typed before the prompt appeared: type-ahead cannot answer for the human."""
    try:
        import termios
    except ImportError:
        return
    try:
        termios.tcflush(fd, termios.TCIFLUSH)
    except (termios.error, OSError, ValueError):
        pass  # not a terminal, or one that is gone: the read declines


def read_answer(fd: int) -> bytes | None:
    """One line from the terminal, at most ``MAX_ANSWER`` bytes; None for EOF, a read error, a
    descriptor that is not a TTY or a line that is too long."""
    try:
        if not os.isatty(fd):
            return None
        data = b""
        while b"\n" not in data:
            chunk = os.read(fd, MAX_ANSWER + 1 - len(data))
            if not chunk:
                return None  # EOF (an empty read), even after some bytes with no newline
            data += chunk
            if len(data) > MAX_ANSWER:
                return None
        return data.split(b"\n", 1)[0]  # the first line only; anything after it is not an answer
    except (OSError, ValueError):
        return None


@contextlib.contextmanager
def _interruptible() -> Iterator[None]:
    """SIGINT, SIGTERM and SIGHUP raise ``Interrupted`` while the dialog is up (main thread)."""
    names = ("SIGINT", "SIGTERM", "SIGHUP")
    wanted = [getattr(signal, n) for n in names if hasattr(signal, n)]
    saved: dict[int, Any] = {}

    def handler(signum: int, _frame: object) -> None:
        raise Interrupted(signum)

    try:
        for sig in wanted:
            saved[sig] = signal.signal(sig, handler)
    except ValueError:  # not the main thread: the default actions apply
        saved = {}
    try:
        yield
    finally:
        for sig, previous in saved.items():
            signal.signal(sig, previous)


def _terminal_size() -> tuple[int, str]:
    """The width of the terminal on stderr (80 when it has none; the environment is not read) and
    the encoding stderr writes."""
    stream = sys.stderr
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        columns = os.get_terminal_size(stream.fileno()).columns
    except (AttributeError, ValueError, OSError):
        columns = 0
    return (columns if columns > 0 else DEFAULT_WIDTH), encoding


def confirm(prepared: Prepared, *, stdin_fd: int, write: Callable[[str], None]) -> bool:
    """Show the dialog and read the answer; True only for ``y`` or ``yes``.

    ``write`` prints to stderr. Raises ``Interrupted`` on SIGINT, SIGTERM or SIGHUP.
    """
    return confirm_with(
        lambda width, encoding: dialog_text(prepared, width, encoding),
        stdin_fd=stdin_fd,
        write=write,
    )


def confirm_with(
    render: Callable[[int, str], str], *, stdin_fd: int, write: Callable[[str], None]
) -> bool:
    """``confirm`` for any dialog: ``render(width, encoding)`` is the text above the prompt (the
    resolve and unresolve dialog, ``threads.py``). The same strict answer rules apply."""
    width, encoding = _terminal_size()
    text = render(width, encoding)
    try:
        with _interruptible():
            write(text)
            _flush_input(stdin_fd)
            write(PROMPT)
            raw = read_answer(stdin_fd)
            if raw is None or len(raw) > MAX_ANSWER:
                _flush_input(stdin_fd)
                with contextlib.suppress(OSError, ValueError):
                    write("\n")
                return False
            return parse_answer(raw)
    except Interrupted:
        write("\n")
        raise
