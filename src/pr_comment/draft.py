"""Loading and validating review drafts (docs/interface.md: Review draft, Draft versioning).

A draft is untrusted, agent-generated input. ``load_draft`` opens it without following a final
symlink, without blocking, as a regular file or stdin, at most 16 MiB, strict UTF-8 and strict JSON
(no duplicate keys, no NaN or infinity). ``validate_document`` then checks every rule of
schemas/review-draft.json plus what a schema cannot express (byte caps, unique ids, ``start_line``
below ``line``, the 500-item limit, the branch grammar of ``anchor.base_ref``).

Every refusal is a ``DraftError`` whose text and ``details`` are fixed strings and JSON pointers
made of the schema's own field names and array indexes: no part of the draft, a path or an unknown
key is ever echoed.

``jsonschema`` is a test-only dependency (design: Dependencies), so the schema's rules are written
out here; tests/test_draft.py checks them against the schema.

``text_issues`` applies Text pipeline steps 1 and 2 (control characters, lone surrogates and
suggestion fences: ``invalid_text``) to every text field of a schema-valid draft; ``parse_draft``
refuses the first one. The draft data is not changed: the line-separator rewrite is
``textpipe.normalize_lines``.

The line-separator rewrite is applied where bodies are built, in ``render.py`` (#18)

Not here: ``draft check`` output belongs to #15.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import stat
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from . import grammars, textpipe
from .output import Outcome, error_outcome

MAX_DRAFT_BYTES: Final = 16 * 1024 * 1024
MAX_TEXT_BYTES: Final = 64 * 1024
MAX_PATH_BYTES: Final = 4096
MAX_TITLE_CHARS: Final = 256
MAX_ITEMS: Final = 500
MAX_REPORTED_ERRORS: Final = 100
# The schema's pr pattern has nested quantifiers; a real PR is far shorter than this.
MAX_PR_CHARS: Final = 2048

SUPPORTED_VERSIONS: Final = (1,)
EVENTS: Final = ("comment", "request_changes")
SIDES: Final = ("right", "left")
SEVERITIES: Final = ("blocker", "major", "minor", "nit", "info")
MOVED_REASONS: Final = (
    "changed",
    "file_removed",
    "diff_unavailable",
    "base_changed",
    "invalid_position",
    "files_incomplete",
)

# Without ^ and $: matched with fullmatch, so a trailing newline never passes. Each equals its
# schema pattern (tests/test_draft.py checks that).
DRAFT_ID_PATTERN: Final = r"[0-9A-HJKMNP-TV-Z]{26}"
FINDING_ID_PATTERN: Final = r"[A-Za-z0-9._-]{1,64}"
PR_PATTERN: Final = (
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?"
    r"(?:/(?!\.\.?(?:/|#))[A-Za-z0-9._-]+)+#[1-9][0-9]{0,9}"
)
ONE_LINE_PATTERN: Final = r"[^\n\r\u000b\u000c\u001c-\u001e\u0085  ]*"

_DRAFT_ID_RE: Final = re.compile(DRAFT_ID_PATTERN)
_FINDING_ID_RE: Final = re.compile(FINDING_ID_PATTERN)
_PR_RE: Final = re.compile(PR_PATTERN)
_ONE_LINE_RE: Final = re.compile(ONE_LINE_PATTERN)

_TOP_KEYS: Final = frozenset(
    {
        "schema_version",
        "draft_id",
        "pr",
        "anchor",
        "event",
        "body",
        "findings",
        "unanchored",
        "meta",
    }
)
_TOP_REQUIRED: Final = ("schema_version", "draft_id", "pr", "anchor", "event", "body", "findings")
_ANCHOR_KEYS: Final = ("head_sha", "base_ref", "base_sha", "merge_base_sha")
_FINDING_KEYS: Final = frozenset(
    {"id", "path", "line", "start_line", "side", "severity", "title", "body", "suggestion"}
)
_FINDING_REQUIRED: Final = ("id", "path", "line", "body")
_UNANCHORED_KEYS: Final = ("finding", "head_sha", "reason")

_READ_CHUNK: Final = 1024 * 1024


class _Reader(Protocol):
    def read(self, size: int, /) -> bytes: ...


class DraftError(Exception):
    """A draft that cannot be used: exit 2 ``invalid_draft``, ``unsupported_draft_version`` or
    ``invalid_text``.

    ``invalid_text`` carries ``details`` of ``{pointer, kind}``.

    ``message`` and ``details`` hold fixed text only (never draft content).
    """

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def outcome(self) -> Outcome:
        return error_outcome(self.code, self.message, details=self.details)


@dataclass(frozen=True)
class Draft:
    """A validated draft: its parsed JSON object and the sha256 of the bytes it was read from."""

    data: dict[str, Any]
    sha256: str


def invalid_draft(errors: Sequence[dict[str, str]]) -> DraftError:
    """The ``invalid_draft`` error for a non-empty list of ``{pointer, message}`` entries."""
    first = errors[0]
    where = first["pointer"] or "the document"
    text = f"invalid draft: {first['message']} (at {where})"
    if len(errors) > 1:
        text += f" and {len(errors) - 1} more"
    return DraftError("invalid_draft", text, {"errors": [dict(error) for error in errors]})


def _fail(message: str) -> DraftError:
    return invalid_draft([{"pointer": "", "message": message}])


# --- reading ---------------------------------------------------------------------------------


def read_limited(stream: _Reader, limit: int = MAX_DRAFT_BYTES) -> bytes:
    """All of ``stream`` when it holds at most ``limit`` bytes; reads at most ``limit`` + 1."""
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = stream.read(min(_READ_CHUNK, limit + 1 - total))
        except OSError:
            raise _fail("draft could not be read") from None
        if chunk is None:  # pyright: ignore[reportUnnecessaryComparison]  # a non-blocking stream
            raise _fail("draft could not be read")
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise _fail("draft is larger than 16 MiB")


def _read_file(path: str) -> bytes:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | getattr(os, "O_NOCTTY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _fail("draft file is a symbolic link") from None
        raise _fail("draft file cannot be opened") from None
    except ValueError:  # an embedded NUL or a path that cannot be encoded
        raise _fail("draft file cannot be opened") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _fail("draft file is not a regular file")
        if info.st_size > MAX_DRAFT_BYTES:
            raise _fail("draft is larger than 16 MiB")
        with os.fdopen(fd, "rb", buffering=0, closefd=False) as handle:
            return read_limited(handle)
    except OSError:
        raise _fail("draft file cannot be read") from None
    finally:
        os.close(fd)


def load_draft(source: str, *, stdin: _Reader | None = None) -> Draft:
    """Read, parse and validate a draft from a file path, or stdin when ``source`` is ``-``.

    Raises ``DraftError``. A TTY check on stdin is the caller's (``-`` with a TTY exits 2 usage).
    """
    if source == "-":
        stream = stdin if stdin is not None else getattr(sys.stdin, "buffer", None)
        if stream is None:
            raise _fail("draft could not be read from stdin")
        raw = read_limited(stream)
    else:
        raw = _read_file(source)
    return parse_draft(raw)


# --- parsing ---------------------------------------------------------------------------------


class _Refused(ValueError):
    """A JSON text refused for a fixed reason (``str(exc)`` is that reason)."""


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _Refused("draft has an object with a duplicate key")
        result[key] = value
    return result


def _constant(_name: str) -> Any:
    raise _Refused("draft holds NaN or infinity, which JSON cannot represent")


def _float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise _Refused("draft holds a number too large to represent")
    return value


_INT_DIGITS_LIMIT: Final = 4000
_HUGE: Final = 10**4300


def _int(text: str) -> int:
    """``int(text)`` that never raises: past Python's digit limit, a plain int of the same sign.

    Such a number is far outside every range the draft allows, so the stand-in only has to be
    out of range the same way.
    """
    if len(text) <= _INT_DIGITS_LIMIT:
        return int(text)
    return -_HUGE if text.startswith("-") else _HUGE


def _parse_json(raw: bytes) -> tuple[Any, str | None]:
    """The document and no failure, or no document and the fixed failure text.

    The failure is returned, not raised from the handler, so the error that reaches the caller
    has no ``__context__`` holding draft content (decoder text, ``JSONDecodeError.doc``).
    """
    failure: str
    try:
        text = raw.decode("utf-8")
        document: Any = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_constant=_constant,
            parse_float=_float,
            parse_int=_int,
        )
        return document, None
    except UnicodeDecodeError:
        failure = "draft is not valid UTF-8"
    except _Refused as exc:
        failure = str(exc)
    except RecursionError:
        failure = "draft is nested too deeply"
    except ValueError:  # JSONDecodeError
        failure = "draft is not valid JSON"
    return None, failure


def parse_draft(raw: bytes) -> Draft:
    """Parse and validate a draft from its bytes. Raises ``DraftError``."""
    if len(raw) > MAX_DRAFT_BYTES:
        raise _fail("draft is larger than 16 MiB")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise _fail("draft starts with a byte order mark")
    document, failure = _parse_json(raw)
    if failure is not None:
        raise _fail(failure)
    if isinstance(document, dict):
        version: object = document.get("schema_version")  # pyright: ignore[reportUnknownMemberType]
        if type(version) is int and version not in SUPPORTED_VERSIONS:
            raise DraftError(
                "unsupported_draft_version",
                "unsupported draft schema_version (supported: "
                + ", ".join(str(v) for v in SUPPORTED_VERSIONS)
                + ")",
                {"supported": list(SUPPORTED_VERSIONS)},
            )
    errors = validate_document(document)
    if errors:
        raise invalid_draft(errors)
    issues = text_issues(document)
    if issues:
        first = issues[0]
        raise DraftError(
            "invalid_text",
            f"invalid draft text: {_TEXT_KIND_MESSAGES[first['kind']]} (at {first['pointer']})",
            dict(first),
        )
    return Draft(document, hashlib.sha256(raw).hexdigest())


# --- text pipeline ---------------------------------------------------------------------------

_TEXT_KIND_MESSAGES: Final = {
    textpipe.CONTROL_CHARACTER: "control character or lone surrogate",
    textpipe.SUGGESTION_FENCE: "suggestion fence",
}


def _text_fields(finding: object, pointer: str) -> Iterator[tuple[str, str, bool]]:
    """``(pointer, text, multi_line)`` for each text field of one finding that is a string."""
    if not isinstance(finding, dict):
        return
    fields: dict[str, Any] = finding  # pyright: ignore[reportUnknownVariableType]
    for key, multi_line in (("path", False), ("title", False), ("body", True)):
        value = fields.get(key)
        if type(value) is str:
            yield f"{pointer}/{key}", value, multi_line
    suggestion: object = fields.get("suggestion")
    lines: object = suggestion.get("lines") if isinstance(suggestion, dict) else None  # pyright: ignore[reportUnknownMemberType]
    if isinstance(lines, list):
        for index, line in enumerate(lines):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            if type(line) is str:
                yield f"{pointer}/suggestion/lines/{index}", line, False


def _all_text_fields(document: object) -> Iterator[tuple[str, str, bool]]:
    if not isinstance(document, dict):
        return
    root: dict[str, Any] = document  # pyright: ignore[reportUnknownVariableType]
    if type(root.get("body")) is str:
        yield "/body", root["body"], True
    findings: object = root.get("findings")
    if isinstance(findings, list):
        for index, item in enumerate(findings):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            yield from _text_fields(item, f"/findings/{index}")
    unanchored: object = root.get("unanchored")
    if isinstance(unanchored, list):
        for index, entry in enumerate(unanchored):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            if isinstance(entry, dict):
                yield from _text_fields(
                    entry.get("finding"),  # pyright: ignore[reportUnknownMemberType]
                    f"/unanchored/{index}/finding",
                )


def text_issues(document: object) -> list[dict[str, str]]:
    """Text pipeline steps 1 and 2 over every text field: ``{pointer, kind}`` entries.

    Step 1 problems (``control_character``) come before step 2 problems (``suggestion_fence``),
    each in document order. Suggestion fences are looked for only in multi-line text (the draft
    ``body`` and a finding ``body``); a suggestion line holding a fence degrades later. Only the
    pointer and the kind are returned, never the text. Fields of the wrong type are skipped
    (``validate_document`` reports them).
    """
    controls: list[dict[str, str]] = []
    fences: list[dict[str, str]] = []
    for pointer, text, multi_line in _all_text_fields(document):
        normalized = textpipe.normalize_lines(text)
        if textpipe.has_forbidden_character(normalized):
            controls.append({"pointer": pointer, "kind": textpipe.CONTROL_CHARACTER})
        elif multi_line and textpipe.suggestion_fence(normalized):
            fences.append({"pointer": pointer, "kind": textpipe.SUGGESTION_FENCE})
    return controls + fences


# --- validation ------------------------------------------------------------------------------


class _Issues:
    """The ``{pointer, message}`` list, capped so a hostile draft cannot make a huge answer."""

    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []
        self._full = False

    def add(self, pointer: str, message: str) -> None:
        if len(self.items) < MAX_REPORTED_ERRORS:
            self.items.append({"pointer": pointer, "message": message})
        elif not self._full:
            self._full = True
            self.items.append({"pointer": "", "message": "further errors omitted"})


def _utf8_len(text: str) -> int:
    # surrogatepass: a lone surrogate from a JSON escape counts as 3 bytes; refusing it is the text
    # pipeline's job (invalid_text), not a reason to crash here.
    return len(text.encode("utf-8", "surrogatepass"))


def _is_int(value: object) -> bool:
    return type(value) is int


def _check_object(
    value: object,
    pointer: str,
    issues: _Issues,
    *,
    allowed: Sequence[str] | frozenset[str],
    required: Sequence[str],
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        issues.add(pointer, "must be an object")
        return None
    obj: dict[str, Any] = value  # pyright: ignore[reportUnknownVariableType]
    for key in required:
        if key not in obj:
            issues.add(pointer, f"missing required property {key}")
    if any(key not in allowed for key in obj):
        issues.add(pointer, "unknown property")
    return obj


def _check_text(
    value: object,
    pointer: str,
    issues: _Issues,
    *,
    max_bytes: int,
    non_empty: bool = False,
    one_line: bool = False,
) -> None:
    if type(value) is not str:
        issues.add(pointer, "must be a string")
    elif non_empty and not value:
        issues.add(pointer, "must not be empty")
    elif _utf8_len(value) > max_bytes:
        issues.add(pointer, f"is longer than {max_bytes} bytes of UTF-8")
    elif one_line and not _ONE_LINE_RE.fullmatch(value):
        issues.add(pointer, "must be one line")


def _check_pattern(value: object, pointer: str, issues: _Issues, pattern: re.Pattern[str]) -> None:
    if type(value) is not str or not pattern.fullmatch(value):
        issues.add(pointer, "is not in the required form")


def _check_sha(value: object, pointer: str, issues: _Issues, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    try:
        ok = grammars.check_sha(value) == value
    except grammars.GrammarError:
        ok = False
    if not ok:
        issues.add(pointer, "must be 40 lowercase hex digits" + (" or null" if nullable else ""))


def _check_anchor(value: object, issues: _Issues) -> None:
    anchor = _check_object(value, "/anchor", issues, allowed=_ANCHOR_KEYS, required=_ANCHOR_KEYS)
    if anchor is None:
        return
    if "head_sha" in anchor:
        _check_sha(anchor["head_sha"], "/anchor/head_sha", issues)
    if "base_ref" in anchor:
        try:
            grammars.check_branch(anchor["base_ref"])
        except grammars.GrammarError:
            issues.add("/anchor/base_ref", "is not a valid branch name")
    for key in ("base_sha", "merge_base_sha"):
        if key in anchor:
            _check_sha(anchor[key], f"/anchor/{key}", issues, nullable=True)


def _check_suggestion(value: object, pointer: str, issues: _Issues) -> None:
    suggestion = _check_object(value, pointer, issues, allowed=("lines",), required=("lines",))
    if suggestion is None or "lines" not in suggestion:
        return
    lines: object = suggestion["lines"]
    if not isinstance(lines, list):
        issues.add(f"{pointer}/lines", "must be an array")
        return
    total = 0
    sound = True
    for index, line in enumerate(lines):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        if type(line) is not str:
            issues.add(f"{pointer}/lines/{index}", "must be a string")
            sound = False
        elif not _ONE_LINE_RE.fullmatch(line):
            issues.add(f"{pointer}/lines/{index}", "must be one line")
            sound = False
        else:
            total += _utf8_len(line) + 1
    if sound and total - 1 > MAX_TEXT_BYTES:
        issues.add(pointer, f"is longer than {MAX_TEXT_BYTES} bytes of UTF-8")


def _check_finding(value: object, pointer: str, issues: _Issues, seen: set[str]) -> None:
    finding = _check_object(
        value, pointer, issues, allowed=_FINDING_KEYS, required=_FINDING_REQUIRED
    )
    if finding is None:
        return
    if "id" in finding:
        identifier = finding["id"]
        if type(identifier) is not str or not _FINDING_ID_RE.fullmatch(identifier):
            issues.add(f"{pointer}/id", "is not in the required form")
        elif identifier in seen:
            issues.add(f"{pointer}/id", "duplicates an id of another finding")
        else:
            seen.add(identifier)
    if "path" in finding:
        _check_text(
            finding["path"],
            f"{pointer}/path",
            issues,
            max_bytes=MAX_PATH_BYTES,
            non_empty=True,
            one_line=True,
        )
    line: object = finding.get("line")
    file_level = "line" in finding and line is None
    if "line" in finding and not file_level and (not _is_int(line) or line < 1):  # pyright: ignore[reportOperatorIssue]
        issues.add(f"{pointer}/line", "must be an integer of at least 1, or null")
    if file_level:
        for key in ("start_line", "side", "suggestion"):
            if key in finding:
                issues.add(f"{pointer}/{key}", "is not allowed on a file-level finding")
    elif "start_line" in finding:
        start = finding["start_line"]
        if not _is_int(start) or start < 1:
            issues.add(f"{pointer}/start_line", "must be an integer of at least 1")
        elif _is_int(line) and start >= line:  # pyright: ignore[reportOperatorIssue]
            issues.add(f"{pointer}/start_line", "must be less than line")
    side_ok = True
    if "side" in finding and not file_level:
        side_ok = type(finding["side"]) is str and finding["side"] in SIDES
        if not side_ok:
            issues.add(f"{pointer}/side", "must be right or left")
    if "severity" in finding and not (
        type(finding["severity"]) is str and finding["severity"] in SEVERITIES
    ):
        issues.add(f"{pointer}/severity", "is not a known severity")
    if "title" in finding:
        title = finding["title"]
        if type(title) is not str:
            issues.add(f"{pointer}/title", "must be a string")
        elif len(title) > MAX_TITLE_CHARS:
            issues.add(f"{pointer}/title", f"is longer than {MAX_TITLE_CHARS} characters")
        elif not _ONE_LINE_RE.fullmatch(title):
            issues.add(f"{pointer}/title", "must be one line")
    if "body" in finding:
        _check_text(
            finding["body"], f"{pointer}/body", issues, max_bytes=MAX_TEXT_BYTES, non_empty=True
        )
    if "suggestion" in finding and not file_level:
        if side_ok and finding.get("side") == "left":
            issues.add(f"{pointer}/suggestion", "is not allowed on side left")
        else:
            _check_suggestion(finding["suggestion"], f"{pointer}/suggestion", issues)


def _check_unanchored(value: object, pointer: str, issues: _Issues, seen: set[str]) -> None:
    entry = _check_object(
        value, pointer, issues, allowed=_UNANCHORED_KEYS, required=_UNANCHORED_KEYS
    )
    if entry is None:
        return
    if "finding" in entry:
        _check_finding(entry["finding"], f"{pointer}/finding", issues, seen)
    if "head_sha" in entry:
        _check_sha(entry["head_sha"], f"{pointer}/head_sha", issues)
    if "reason" in entry and not (
        type(entry["reason"]) is str and entry["reason"] in MOVED_REASONS
    ):
        issues.add(f"{pointer}/reason", "is not a known moved reason")


def _item_count(value: object) -> int:
    return len(value) if isinstance(value, list) else 0  # pyright: ignore[reportUnknownArgumentType]


def validate_document(document: object) -> list[dict[str, str]]:
    """Every problem of a parsed draft as ``{pointer, message}`` entries; empty when valid.

    Pointers are RFC 6901 and made only of the schema's field names and array indexes. At most
    ``MAX_REPORTED_ERRORS`` entries are returned (a last one says more were omitted). The
    document is not changed.
    """
    issues = _Issues()
    root = _check_object(document, "", issues, allowed=_TOP_KEYS, required=_TOP_REQUIRED)
    if root is None:
        return issues.items
    if "schema_version" in root:
        version = root["schema_version"]
        if not _is_int(version):
            issues.add("/schema_version", "must be an integer")
        elif version not in SUPPORTED_VERSIONS:
            issues.add("/schema_version", "is not a supported version")
    if "draft_id" in root:
        _check_pattern(root["draft_id"], "/draft_id", issues, _DRAFT_ID_RE)
    if "pr" in root:
        pr = root["pr"]
        if type(pr) is str and len(pr) > MAX_PR_CHARS:
            issues.add("/pr", f"is longer than {MAX_PR_CHARS} characters")
        else:
            _check_pattern(pr, "/pr", issues, _PR_RE)
    if "anchor" in root:
        _check_anchor(root["anchor"], issues)
    if "event" in root and not (type(root["event"]) is str and root["event"] in EVENTS):
        issues.add("/event", "must be comment or request_changes")
    if "body" in root:
        _check_text(root["body"], "/body", issues, max_bytes=MAX_TEXT_BYTES)
    if "meta" in root and not isinstance(root["meta"], dict):
        issues.add("/meta", "must be an object")
    _check_items(root, issues)
    return issues.items


def _check_items(root: dict[str, Any], issues: _Issues) -> None:
    findings: object = root.get("findings")
    unanchored: object = root.get("unanchored")
    if "findings" in root and not isinstance(findings, list):
        issues.add("/findings", "must be an array")
    if "unanchored" in root and not isinstance(unanchored, list):
        issues.add("/unanchored", "must be an array")
    count_findings = _item_count(findings)
    count_unanchored = _item_count(unanchored)
    if count_findings > MAX_ITEMS:
        issues.add("/findings", f"has more than {MAX_ITEMS} items")
        return
    if count_findings + count_unanchored > MAX_ITEMS:
        issues.add(
            "/unanchored", f"findings and unanchored together have more than {MAX_ITEMS} items"
        )
        return
    seen: set[str] = set()
    if isinstance(findings, list):
        for index, item in enumerate(findings):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            _check_finding(item, f"/findings/{index}", issues, seen)
    if isinstance(unanchored, list):
        for index, item in enumerate(unanchored):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            _check_unanchored(item, f"/unanchored/{index}", issues, seen)
