"""Unified-diff parser for re-anchoring (docs/interface.md, Re-anchoring, "Reading the diff").

``parse_diff`` splits the diff text that forgectl prints into file sections. Paths come from
the ``---`` and ``+++`` lines and the ``rename from`` and ``rename to`` lines. A section that
carries none (a binary change, or an added or deleted file with headers only) takes its path
from a ``Binary files X and Y differ`` line or from the ``diff --git a/P b/P`` line, only when
exactly one reading exists, because those lines cannot be split reliably when a path has a space
or a space followed by ``b/``. Anything the parser cannot read with certainty raises
``AmbiguousDiffError``; the caller moves every right-side finding with ``diff_unavailable``.

The parser only reads. It maps no lines (TODO(#24): right-side mapping over these sections).
Paths are decoded, not sanitized: a decoded path can hold characters that forgectl prints as
``<U+XXXX>`` markers elsewhere (TODO(#24): compare paths in forgectl's printed form).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import NamedTuple


class SectionKind(StrEnum):
    CHANGED = "changed"
    RENAMED = "renamed"
    MODE_ONLY = "mode_only"
    BINARY = "binary"
    DELETED = "deleted"
    ADDED = "added"


class AmbiguousDiffError(ValueError):
    """The diff cannot be read unambiguously. Carries a fixed ``reason`` code and a 1-based line.

    The message never repeats diff content, so it is safe to log.
    """

    def __init__(self, reason: str, line: int) -> None:
        super().__init__(f"diff cannot be read unambiguously: {reason} (line {line})")
        self.reason = reason
        self.line = line


class HunkLine(NamedTuple):
    """One line of a hunk. ``tag`` is ``" "``, ``"+"`` or ``"-"``; ``text`` keeps any CR."""

    tag: str
    text: str


@dataclass(frozen=True, slots=True)
class Hunk:
    """A hunk with its header numbers (an omitted count is 1) and every content line."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[HunkLine, ...]


@dataclass(frozen=True, slots=True)
class FileSection:
    """One file of the diff. A path is ``None`` for ``/dev/null`` and for a mode-only section."""

    kind: SectionKind
    old_path: str | None
    new_path: str | None
    hunks: tuple[Hunk, ...]


@dataclass(frozen=True, slots=True)
class ParsedDiff:
    sections: tuple[FileSection, ...]

    def by_old_path(self) -> dict[str, FileSection]:
        """Sections keyed by the path they have at the older head; paths are unique."""
        return {s.old_path: s for s in self.sections if s.old_path is not None}


_HUNK_RE = re.compile(
    r"@@ -([0-9]{1,9})(?:,([0-9]{1,9}))? \+([0-9]{1,9})(?:,([0-9]{1,9}))? @@(?: |$)"
)
_MODE_RE = re.compile(r"[0-7]{6}")
_INDEX_RE = re.compile(r"index [0-9a-f]+\.\.[0-9a-f]+(?: [0-7]{6})?")
_SIMILARITY_RE = re.compile(r"(?:dis)?similarity index [0-9]{1,3}%")
_OCTAL_RE = re.compile(r"[0-3][0-7]{2}")
_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    '"': '"',
}
_BINARY_PREFIX = "Binary files "
_BINARY_SUFFIX = " differ"
_NO_NEWLINE_MARKER = "\\ No newline at end of file"
_MAX_PATH_LINE = 8192


class _Unreadable(Exception):
    pass


def _is_control(char: str) -> bool:
    return ord(char) < 0x20 or char == "\x7f"


def _unquote(token: str) -> str:
    """Decode git's C-style quoting; an unquoted token must be free of quote, backslash, control."""
    if token.startswith('"'):
        if len(token) < 2 or not token.endswith('"'):
            raise _Unreadable
        inner = token[1:-1]
        out = bytearray()
        i = 0
        while i < len(inner):
            char = inner[i]
            if char == "\\":
                escaped = inner[i + 1 : i + 2]
                octal = inner[i + 1 : i + 4]
                if escaped in _ESCAPES:
                    out += _ESCAPES[escaped].encode()
                    i += 2
                elif _OCTAL_RE.fullmatch(octal):
                    byte = int(octal, 8)
                    if byte == 0:
                        raise _Unreadable
                    out.append(byte)
                    i += 4
                else:
                    raise _Unreadable
            elif char == '"' or _is_control(char):
                raise _Unreadable
            else:
                out += char.encode("utf-8", "surrogatepass")
                i += 1
        try:
            return out.decode("utf-8")
        except UnicodeDecodeError:
            raise _Unreadable from None
    if any(c in '"\\' or _is_control(c) for c in token):
        raise _Unreadable
    return token


def _strip_prefix(name: str, prefix: str) -> str:
    if not name.startswith(prefix) or len(name) == len(prefix):
        raise _Unreadable
    return name[len(prefix) :]


def _dash_path(raw: str, prefix: str) -> str | None:
    """Path of a ``---`` or ``+++`` line: one trailing TAB removed, ``/dev/null`` is ``None``."""
    if raw.endswith("\t"):
        raw = raw[:-1]
    if raw == "/dev/null":
        return None
    return _strip_prefix(_unquote(raw), prefix)


def _rename_path(raw: str) -> str:
    name = _unquote(raw)
    if not name:
        raise _Unreadable
    return name


def _null_or_path(token: str, prefix: str) -> str | None:
    return None if token == "/dev/null" else _strip_prefix(_unquote(token), prefix)


def _equal_halves(middle: str, separator: str) -> tuple[str | None, str | None]:
    """Read ``a/P<separator>b/P``: two halves of one length, so the split is the middle."""
    half, odd = divmod(len(middle) - len(separator), 2)
    if odd or half < 0 or middle[half : half + len(separator)] != separator:
        raise _Unreadable
    old = _strip_prefix(_unquote(middle[:half]), "a/")
    new = _strip_prefix(_unquote(middle[half + len(separator) :]), "b/")
    if old != new:
        raise _Unreadable
    return old, new


def _readings(middle: str, separator: str, *, equal_only: bool) -> tuple[str | None, str | None]:
    """The only reading of ``old<separator>new`` as two paths of one file, in linear time.

    Candidates: equal paths split at the middle, and (unless ``equal_only``) ``/dev/null`` as the
    first or last token. Any other reading is ambiguous.
    """
    if len(middle) > _MAX_PATH_LINE:
        raise _Unreadable
    found: set[tuple[str | None, str | None]] = set()
    candidates = [lambda: _equal_halves(middle, separator)]
    if not equal_only:
        null_old = "/dev/null" + separator
        null_new = separator + "/dev/null"
        if middle.startswith(null_old):
            rest = middle[len(null_old) :]
            candidates.append(lambda: (None, _strip_prefix(_unquote(rest), "b/")))
        if middle.endswith(null_new):
            head = middle[: -len(null_new)]
            candidates.append(lambda: (_strip_prefix(_unquote(head), "a/"), None))
    for candidate in candidates:
        try:
            found.add(candidate())
        except _Unreadable:
            pass
    if len(found) != 1:
        raise _Unreadable
    return next(iter(found))


def _binary_paths(line: str) -> tuple[str | None, str | None]:
    """Paths of a ``Binary files X and Y differ`` line, only when exactly one reading exists."""
    return _readings(line[len(_BINARY_PREFIX) : -len(_BINARY_SUFFIX)], " and ", equal_only=False)


def _diff_line_path(line: str) -> str:
    """The path of a ``diff --git a/P b/P`` line, only when the halves are equal."""
    path = _readings(line[len("diff --git ") :], " ", equal_only=True)[0]
    if path is None:
        raise _Unreadable
    return path


def _binary_rename_matches(line: str, old: str, new: str) -> bool:
    """Whether the Binary line reads exactly ``a/<old>`` and ``b/<new>``, in linear time."""
    middle = line[len(_BINARY_PREFIX) : -len(_BINARY_SUFFIX)]
    if len(middle) > _MAX_PATH_LINE:
        return False
    try:
        if middle.startswith('"'):
            end = 1
            while end < len(middle) and middle[end] != '"':
                end += 2 if middle[end] == "\\" else 1
            first, rest = middle[: end + 1], middle[end + 1 :]
            if not rest.startswith(" and "):
                return False
            second = rest[5:]
        else:
            first = "a/" + old
            if not middle.startswith(first + " and "):
                return False
            second = middle[len(first) + 5 :]
        return _strip_prefix(_unquote(first), "a/") == old and (
            _strip_prefix(_unquote(second), "b/") == new
        )
    except _Unreadable:
        return False


def _check_mode(value: str) -> None:
    if not _MODE_RE.fullmatch(value):
        raise _Unreadable


def parse_diff(text: str) -> ParsedDiff:
    """Parse ``text`` into file sections, or raise ``AmbiguousDiffError``.

    Lines are split on LF only (a CR is content). The text may end without a final newline.
    """
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    starts = [i for i, line in enumerate(lines) if line.startswith("diff --git ")]
    if lines and (not starts or starts[0] != 0):
        raise AmbiguousDiffError("text_before_first_section", 1)
    sections: list[FileSection] = []
    seen: set[str] = set()
    for n, start in enumerate(starts):
        if any(_is_control(c) and c != "\t" for c in lines[start]):
            raise AmbiguousDiffError("control_character_in_section_line", start + 1)
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        section = _parse_section(lines, start, end)
        names = {p for p in (section.old_path, section.new_path) if p is not None}
        if names & seen:
            raise AmbiguousDiffError("duplicate_path", start + 1)
        seen |= names
        sections.append(section)
    return ParsedDiff(tuple(sections))


def _parse_section(lines: list[str], start: int, end: int) -> FileSection:
    def fail(reason: str, at: int) -> AmbiguousDiffError:
        return AmbiguousDiffError(reason, at + 1)

    seen: set[str] = set()

    def once(key: str, at: int) -> None:
        if key in seen:
            raise fail("duplicate_header", at)
        seen.add(key)

    rename_from = rename_to = None
    binary_line: str | None = None
    dash_old: str | None = None
    dash_new: str | None = None
    full_similarity = False
    j = start + 1
    while j < end and not lines[j].startswith("@@"):
        line = lines[j]
        try:
            if line.startswith("--- "):
                once("dash", j)
                if j + 1 >= end or not lines[j + 1].startswith("+++ "):
                    raise fail("dash_without_plus", j)
                dash_old = _dash_path(line[4:], "a/")
                dash_new = _dash_path(lines[j + 1][4:], "b/")
                j += 1
            elif line.startswith("rename from "):
                once("rename_from", j)
                rename_from = _rename_path(line[12:])
            elif line.startswith("rename to "):
                once("rename_to", j)
                rename_to = _rename_path(line[10:])
            elif line.startswith(_BINARY_PREFIX) and line.endswith(_BINARY_SUFFIX):
                once("binary", j)
                binary_line = line
            elif line.startswith(("old mode ", "new mode ")):
                once(line[:3] + "_mode", j)
                _check_mode(line[9:])
            elif line.startswith("new file mode "):
                once("new_file", j)
                _check_mode(line[14:])
            elif line.startswith("deleted file mode "):
                once("deleted_file", j)
                _check_mode(line[18:])
            elif _SIMILARITY_RE.fullmatch(line):
                once("similarity", j)
                full_similarity = line == "similarity index 100%"
            elif _INDEX_RE.fullmatch(line):
                pass
            else:
                raise fail("unknown_header_line", j)
        except _Unreadable:
            raise fail("unreadable_path_or_mode", j) from None
        j += 1

    hunks = _parse_hunks(lines, j, end)
    is_new, is_deleted = "new_file" in seen, "deleted_file" in seen
    has_modes = "old_mode" in seen or "new_mode" in seen
    has_rename = rename_from is not None or rename_to is not None
    at = start
    if ("old_mode" in seen) != ("new_mode" in seen) or (has_modes and (is_new or is_deleted)):
        raise fail("inconsistent_modes", at)
    if is_new and is_deleted:
        raise fail("new_and_deleted", at)
    if has_rename and (rename_from is None or rename_to is None or rename_from == rename_to):
        raise fail("bad_rename_lines", at)
    if hunks and "dash" not in seen:
        raise fail("hunks_without_paths", at)
    if "dash" in seen and not hunks:
        raise fail("paths_without_hunks", at)

    old: str | None
    new: str | None
    if binary_line is not None:
        if hunks or "dash" in seen:
            raise fail("binary_with_hunks", at)
        if has_rename:
            if is_new or is_deleted:
                raise fail("rename_with_new_or_deleted", at)
            old, new = rename_from, rename_to
            if not _binary_rename_matches(binary_line, old, new):  # type: ignore[arg-type]
                raise fail("binary_line_disagrees_with_rename", at)
        else:
            try:
                old, new = _binary_paths(binary_line)
            except _Unreadable:
                raise fail("unreadable_binary_paths", at) from None
            if (old is None) != is_new or (new is None) != is_deleted:
                raise fail("binary_modes_disagree", at)
        kind = SectionKind.BINARY
    elif "dash" in seen:
        old, new = dash_old, dash_new
        if old is None and new is None:
            raise fail("both_paths_null", at)
        if old is None or new is None:
            if has_rename or (old is None) != is_new or (new is None) != is_deleted:
                raise fail("null_path_disagrees_with_headers", at)
            kind = SectionKind.ADDED if old is None else SectionKind.DELETED
        elif is_new or is_deleted:
            raise fail("null_path_disagrees_with_headers", at)
        elif has_rename:
            if (old, new) != (rename_from, rename_to):
                raise fail("rename_disagrees_with_paths", at)
            kind = SectionKind.RENAMED
        elif old != new:
            raise fail("paths_differ_without_rename", at)
        else:
            kind = SectionKind.CHANGED
    elif has_rename:
        if is_new or is_deleted:
            raise fail("rename_with_new_or_deleted", at)
        if not full_similarity:
            raise fail("rename_without_full_similarity", at)
        old, new, kind = rename_from, rename_to, SectionKind.RENAMED
    elif is_deleted or is_new:
        try:
            path = _diff_line_path(lines[start])
        except _Unreadable:
            raise fail("unreadable_diff_line_path", at) from None
        if is_deleted:
            old, new, kind = path, None, SectionKind.DELETED
        else:
            old, new, kind = None, path, SectionKind.ADDED
    elif has_modes:
        old, new, kind = None, None, SectionKind.MODE_ONLY
    else:
        raise fail("section_without_content", at)

    if kind in (SectionKind.ADDED, SectionKind.DELETED) and len(hunks) > 1:
        raise fail("added_or_deleted_with_several_hunks", at)

    for hunk in hunks:
        if kind is SectionKind.ADDED and (hunk.old_start, hunk.old_count) != (0, 0):
            raise fail("added_hunk_not_from_zero", at)
        if kind is SectionKind.DELETED and (hunk.new_start, hunk.new_count) != (0, 0):
            raise fail("deleted_hunk_not_to_zero", at)
    return FileSection(kind, old, new, tuple(hunks))


def _parse_hunks(lines: list[str], j: int, end: int) -> list[Hunk]:
    hunks: list[Hunk] = []
    prev_old_end = prev_new_end = 0
    while j < end:
        header = _HUNK_RE.match(lines[j])
        if header is None:
            raise AmbiguousDiffError("malformed_hunk_header", j + 1)
        old_start, new_start = int(header[1]), int(header[3])
        old_count = 1 if header[2] is None else int(header[2])
        new_count = 1 if header[4] is None else int(header[4])
        if old_count == new_count == 0:
            raise AmbiguousDiffError("empty_hunk", j + 1)
        if (old_count > 0 and old_start < 1) or (new_count > 0 and new_start < 1):
            raise AmbiguousDiffError("hunk_start_zero_with_lines", j + 1)
        old_first = old_start if old_count else old_start + 1
        new_first = new_start if new_count else new_start + 1
        if old_first <= prev_old_end or new_first <= prev_new_end:
            raise AmbiguousDiffError("hunks_out_of_order", j + 1)
        prev_old_end = old_start + old_count - 1 if old_count else old_start
        prev_new_end = new_start + new_count - 1 if new_count else new_start

        header_at = j
        j += 1
        remaining_old, remaining_new = old_count, new_count
        body: list[HunkLine] = []
        last = ""  # tag of the content line just before, "" after a marker or at the start
        while remaining_old > 0 or remaining_new > 0 or (j < end and lines[j].startswith("\\")):
            if j >= end:
                raise AmbiguousDiffError("truncated_hunk", header_at + 1)
            line = lines[j]
            tag = line[:1]
            if tag == "\\":
                ended = {
                    "-": remaining_old == 0,
                    "+": remaining_new == 0,
                    " ": remaining_old == 0 and remaining_new == 0,
                }.get(last, False)
                if line != _NO_NEWLINE_MARKER or not ended:
                    raise AmbiguousDiffError("misplaced_no_newline_marker", j + 1)
                last = ""
            elif tag in (" ", ""):  # a bare empty line is a context line stripped of its space
                if remaining_old <= 0 or remaining_new <= 0:
                    raise AmbiguousDiffError("hunk_longer_than_counts", j + 1)
                remaining_old -= 1
                remaining_new -= 1
                body.append(HunkLine(" ", line[1:]))
                last = " "
            elif tag == "-" and remaining_old > 0:
                remaining_old -= 1
                body.append(HunkLine("-", line[1:]))
                last = "-"
            elif tag == "+" and remaining_new > 0:
                remaining_new -= 1
                body.append(HunkLine("+", line[1:]))
                last = "+"
            else:
                raise AmbiguousDiffError("bad_hunk_line", j + 1)
            j += 1
        hunks.append(Hunk(old_start, old_count, new_start, new_count, tuple(body)))
    return hunks
