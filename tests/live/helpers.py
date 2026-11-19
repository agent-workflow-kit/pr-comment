"""Pure helpers of the live suite (no network, no processes): diff reading, marker detection,
deterministic ids, HTML inspection and the form check of forwarded flags."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
MARKER = "<!-- pr-comment:v1 -->"
_DEDUPE_LINE_RE = re.compile(r"^<!-- forgectl:dedupe:[A-Za-z0-9._:-]{1,128} -->$")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def draft_id(run_id: str, label: str) -> str:
    """A draft id (26 Crockford base32 characters) fixed by the run id and a label, so running the
    same run id again posts the same keys and reports ``already_posted``."""
    digest = hashlib.sha256(f"{run_id}\0{label}".encode()).digest()
    number = int.from_bytes(digest[:17], "big") >> 8  # 130 bits
    chars = []
    for _ in range(26):
        chars.append(CROCKFORD[number & 31])
        number >>= 5
    return "".join(reversed(chars))


def added_lines(diff: str) -> list[tuple[str, int]]:
    """``(path, new line number)`` of every added line of a unified diff, in diff order."""
    found: list[tuple[str, int]] = []
    path: str | None = None
    new_line = 0
    in_hunk = False
    for raw in diff.split("\n"):
        if raw.startswith("diff --git "):
            in_hunk = False
            path = None
        elif not in_hunk and raw.startswith("+++ "):
            target = raw[4:]
            path = target[2:] if target.startswith("b/") else None
        elif raw.startswith("@@"):
            match = _HUNK_RE.match(raw)
            in_hunk = match is not None
            new_line = int(match.group(1)) if match else 0
        elif in_hunk and path is not None:
            if raw.startswith("+"):
                found.append((path, new_line))
                new_line += 1
            elif raw.startswith(" "):
                new_line += 1
            # "-" lines and "\ No newline" markers do not advance the new side
    return found


def pick_lines(lines: Sequence[tuple[str, int]], count: int) -> list[tuple[str, int]]:
    """``count`` positions from ``lines``, cycling when there are fewer (two comments on one line
    are allowed on both hosts). Empty when ``lines`` is."""
    if not lines:
        return []
    return [lines[i % len(lines)] for i in range(count)]


def has_marker(body: str) -> bool:
    """docs/interface.md, Marker detection (the authorship half is the caller's)."""
    lines = body.rstrip().split("\n")
    if lines and _DEDUPE_LINE_RE.match(lines[-1].strip()):
        lines = lines[:-1]
    while lines and not lines[-1].strip():
        lines.pop()
    return bool(lines) and lines[-1] == MARKER


def flatten_comments(document: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Every comment of a ``forgectl comments`` document, each with its thread under ``_thread``."""
    found: list[dict[str, Any]] = []
    for thread in (document or {}).get("threads") or []:
        for comment in thread.get("comments") or []:
            found.append({**comment, "_thread": thread})
    return found


def tagged(comments: Iterable[Mapping[str, Any]], tag: str) -> list[Mapping[str, Any]]:
    return [c for c in comments if tag in str(c.get("body", ""))]


_FENCE_LINE_RE = re.compile(r"^(`{3,}|~{3,})text$")
RUN_SPLIT = "<run split>"


def text_fence(body: str) -> tuple[str, int] | None:
    """The fence (character, length) of the first ``text`` block of a stored comment body."""
    for line in body.split("\n"):
        match = _FENCE_LINE_RE.match(line.rstrip())
        if match:
            return match.group(1)[0], len(match.group(1))
    return None


def fence_problems(body: str, backtick_run: int, tilde_run: int) -> list[str]:
    """What is wrong with the degraded suggestion block of a stored body whose suggestion held a
    backtick run and a tilde run of the given lengths (docs/interface.md, Suggestions: the fence
    is at most 255 characters, a backtick run of 255 or more gets a tilde fence, and only when both
    kinds reach 255 are backtick runs split with the visible marker)."""
    problems: list[str] = []
    fence = text_fence(body)
    if fence is None:
        return ["no text block"]
    char, length = fence
    if length > 255:
        problems.append(f"the fence is {length} characters, over 255")
    both = backtick_run >= 255 and tilde_run >= 255
    wants_tilde = backtick_run >= 255 and not both  # both: the backtick runs are split instead
    if wants_tilde and char != "~":
        problems.append("a backtick run of 255 or more needs a tilde fence")
    if not wants_tilde and char != "`":
        problems.append("a backtick fence was expected")
    if both != (RUN_SPLIT in body):
        problems.append(f"{RUN_SPLIT} {'missing' if both else 'present'}")
    return problems


def read_pidfile(path: Path, timeout: float = 5.0) -> int:
    """The pid a hook wrote with ``echo $! > file``: only a complete line counts (a half-written
    file is read again until the deadline). 0 when no pid appeared."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if text.endswith("\n") and text.strip().isdigit():
            return int(text.strip())
        if time.monotonic() >= deadline:
            return 0
        time.sleep(0.02)


BARE_FORWARDED = frozenset({"--dry-run", "--yes", "--override-escalation"})
VALUE_FORWARDED = frozenset(
    {
        "--actor",
        "--decision-id",
        "--dedupe-key",
        "--expected-base",
        "--expected-head",
        "--from",
        "--host",
        "--thread",
        "--to",
        "--timeout",
        "--config",
    }
)


def flag_form_problems(argv: Sequence[str]) -> list[str]:
    """Forwarded flags of one forgectl argv that are not in the documented ``--flag=value`` form.
    The reads' own ``--json`` and the stdin ``-`` after ``--file`` or ``--body-file`` are fine."""
    problems: list[str] = []
    for index, arg in enumerate(argv):
        if arg in VALUE_FORWARDED:
            problems.append(f"{arg} is not in --flag=value form")
        elif arg in ("--file", "--body-file"):
            if index + 1 >= len(argv) or argv[index + 1] != "-":
                problems.append(f"{arg} is not followed by -")
        elif arg.startswith("--") and "=" in arg:
            name = arg.split("=", 1)[0]
            if name in BARE_FORWARDED:
                problems.append(f"{name} must be bare")
    return problems


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.text: list[str] = []
        self.pre: list[str] = []  # the text of each <pre> block
        self.cells: list[str] = []  # the line-number cells, in document order
        self._pre_depth = 0
        self._cell: list[str] | None = None  # text of the open numbered cell
        self._cell_attr = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: v or "" for k, v in attrs}
        self.tags.append((tag, values))
        if tag == "pre":
            if self._pre_depth == 0:
                self.pre.append("")
            self._pre_depth += 1
        elif tag in ("td", "th") and self._cell is None:
            classes = values.get("class", "").split()
            if "data-line-number" in values or "blob-num" in classes:
                self._cell = []
                # a hidden number (tilde fences) is marked by a class and a dot in the attribute
                hidden = "blob-num-hidden" in classes
                self._cell_attr = "\u00b7" if hidden else values.get("data-line-number", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "pre" and self._pre_depth:
            self._pre_depth -= 1
        elif tag in ("td", "th") and self._cell is not None:
            self.cells.append(self._cell_attr or "".join(self._cell).strip())
            self._cell = None

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if self._pre_depth:
            self.pre[-1] += data
        if self._cell is not None:
            self._cell.append(data)


class Html:
    """Just enough of a rendered comment to answer the live suite's questions."""

    def __init__(self, source: str) -> None:
        self.source = source
        parser = _Collector()
        parser.feed(source)
        parser.close()
        self.tags = parser.tags
        self.text = "".join(parser.text)
        self.pre = parser.pre
        self._cells = parser.cells

    def line_cells(self) -> list[str]:
        """The line-number cells of any rendered diff: the ``data-line-number`` value, else the
        cell text (a host that hides the number shows dots or nothing)."""
        return list(self._cells)

    def has_body(self, lines: Sequence[str]) -> bool:
        """The lines appear verbatim, in order, inside one rendered code block."""
        if not lines:
            return False
        wanted = "\n".join(lines)
        return any(wanted in block for block in self.pre)

    def count(self, tag: str) -> int:
        return sum(1 for name, _ in self.tags if name == tag)

    def links(self) -> list[dict[str, str]]:
        return [attrs for name, attrs in self.tags if name == "a"]

    def links_to(self, name: str) -> list[dict[str, str]]:
        """Links whose target ends with the user or group name, or that carry a user-data attribute
        of that name (how a host marks a live mention)."""
        wanted = name.lower()
        found = []
        for attrs in self.links():
            href = attrs.get("href", "").rstrip("/").lower()
            data = {v.lower() for k, v in attrs.items() if k.startswith("data-")}
            if href.endswith("/" + wanted) or href == wanted or wanted in data:
                found.append(attrs)
        return found

    def hrefs_with_scheme(self, scheme: str) -> list[str]:
        return [
            a.get("href", "") for a in self.links() if a.get("href", "").lower().startswith(scheme)
        ]

    def shape(self) -> str:
        """The tag structure without ids, hrefs or text: two comments that render the same kind
        of thing have the same shape (how an applicable suggestion differs from a code block)."""
        parts = []
        for name, attrs in self.tags:
            classes = " ".join(sorted(attrs.get("class", "").split()))
            parts.append(f"{name}.{classes}" if classes else name)
        return " ".join(parts)


def line_cell_verdict(cells: Sequence[str], expected: int) -> str:
    """``match`` when a line-number cell is the expected line, ``mismatch`` when the cells show
    other numbers, ``hidden`` when none shows a number (dots or empty), ``absent`` without cells."""
    if not cells:
        return "absent"
    numbers = [c.strip() for c in cells if c.strip().isdigit()]
    if not numbers:
        return "hidden"
    return "match" if str(expected) in numbers else "mismatch"


def distinct_shapes(*shapes: str) -> bool:
    return len(set(shapes)) == len(shapes)
