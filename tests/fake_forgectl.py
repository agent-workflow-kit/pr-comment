"""A fidelity fake of the ``forgectl`` command line, for process tests of pr-comment.

It is a program (``tests/fakebin/forgectl`` launches it) and a module (the tests import its
grammar, rules and validators). Its rules come from the pinned forgectl release
(``forgectl_pin.json``): that release's interface, schemas and command-line parser. They never
come from pr-comment's own code, so a test cannot pass by sharing a mistake with pr-comment.

What is modeled, for the commands pr-comment calls (``show diff files comments compare whoami
review comment reply approve resolve unresolve`` and ``--version``):

- the argv grammar: the same ``argparse`` construction forgectl uses (no abbreviations, a repeated
  flag is an error, ``--flag value`` and ``--flag=value``, empty values refused), and the value
  grammars (PR references, comment and thread ids, shas, branches, dedupe keys, actors, decision
  ids, durations);
- the order of the checks that need no host: parse, config file, closed host list (exit 20
  ``unknown_host``), human-only flags and required ``--expected-head``/``--expected-base`` (exit
  2), the body or review input read from stdin (strict UTF-8, size caps, empty bodies, the review
  document's own rules), neutralizing and the dedupe marker (so ``neutralized_lines``,
  ``markers_removed`` and a dry run's plan are computed from the real stdin);
- confirmation: a write that is not a dry run and has no ``--yes`` exits 13
  ``confirmation_required``;
- the host's answer, which a scenario scripts; every script is checked before it is printed: the
  document must validate against the pinned schema and agree with the argv and the input, an error
  must be one forgectl can emit for that command (code, exit code, ``reason``, ``details``), and a
  dry run's exit 20 is printed as forgectl does it (the command's result with ``would_refuse``);
- exit codes, stdout and stderr (the one ``forgectl: error:`` line), and signals: SIGINT, SIGTERM
  and SIGHUP exit 128+N with no envelope before a write was attempted and exit 12
  ``outcome_unknown`` after (a scripted hang).

Anything a scenario asks for that forgectl could not do is a *fake error*: the fake prints it on
stderr, exits 97 and logs it, and the fixture fails the test. The fake never stands in for a host:
it does not read a config file's content, run hooks, audit, or know what a host would answer.

The call log (``<dir>/calls.jsonl``) and the pids file (``<dir>/pids``, the format of
``tests.fakes``) are what tests assert on; see ``tests/forgectl_harness.py``.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import stat
import sys
import time
import unicodedata
from argparse import Namespace
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, NoReturn, TextIO

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

HERE = Path(__file__).resolve().parent
PIN: dict[str, Any] = json.loads((HERE / "forgectl_pin.json").read_text(encoding="utf-8"))
SCHEMAS_DIR = HERE / PIN["schemas_dir"]
VERSION: str = PIN["version"]

FAKE_ERROR_EXIT = 97
DIR_ENV = "FAKE_FORGECTL_DIR"

# --- the command set --------------------------------------------------------------------------

READ_COMMANDS = ("show", "diff", "files", "comments", "compare", "whoami")
WRITE_COMMANDS = ("review", "comment", "reply", "resolve", "unresolve", "approve")
MODELED = READ_COMMANDS + WRITE_COMMANDS
# forgectl has these too; pr-comment never calls them, so a call is a fake error, not a guess.
UNMODELED = (
    "prs",
    "snapshot",
    "snapshots",
    "poll",
    "reviews",
    "ci",
    "access",
    "merge",
    "browse",
    "export",
)
COMMAND_ORDER = (
    "prs",
    "show",
    "diff",
    "files",
    "comments",
    "reviews",
    "ci",
    "snapshot",
    "snapshots",
    "poll",
    "compare",
    "access",
    "whoami",
    "merge",
    "review",
    "comment",
    "reply",
    "resolve",
    "unresolve",
    "approve",
    "browse",
    "export",
)
BUILT_IN_HOSTS: dict[str, str] = {"github.com": "github", "gitlab.com": "gitlab"}

# --- limits (forgectl interface, Post order step 1) -----------------------------------------------

GITHUB_COMMENT_LIMIT = 262144
INLINE_COMMENT_LIMIT = 1024 * 1024
GITLAB_LIMIT = 1024 * 1024
FILE_LIMIT = 16 * 1024 * 1024
MAX_COMMENTS = 200
MAX_DECISION_IDS = 32
MAX_INPUT_CHARS = 2048
MAX_BRANCH_CHARS = 1024
HOOK_STDERR_LIMIT = 4096

# --- errors -------------------------------------------------------------------------------------

EXIT_BY_CODE: dict[str, int] = {
    "internal": 1,
    "usage": 2,
    "config_invalid": 2,
    "invalid_pr": 2,
    "body_too_large": 2,
    "method_required": 2,
    "not_found": 10,
    "vetoed": 11,
    "auth_required": 12,
    "permission_denied": 12,
    "rate_limited": 12,
    "upstream_error": 12,
    "network_error": 12,
    "tool_missing": 12,
    "host_rejected": 12,
    "outcome_unknown": 12,
    "outcome_partial": 12,
    "merge_unverified": 12,
    "invalid_position": 12,
    "quick_action_ran": 12,
    "approved_stale_head": 12,
    "wrong_host_response": 12,
    "audit_unavailable": 12,
    "audit_failed": 12,
    "partial_results": 12,
    "unsupported_host_version": 12,
    "confirmation_required": 13,
    "escalated": 13,
    "refused": 20,
}
REFUSAL_REASONS = frozenset(
    {
        "unknown_host",
        "stale_head",
        "stale_base",
        "no_head_match",
        "not_open",
        "not_ready",
        "method_not_allowed",
        "auto_merge_unavailable",
        "self_approval",
        "not_resolvable",
        "too_large",
        "drafts_present",
        "pending_review_exists",
    }
)
# `details` keys the interface's error table lists without a condition, by code or by reason.
REQUIRED_DETAILS_BY_CODE: dict[str, tuple[str, ...]] = {
    "method_required": ("allowed",),
    "invalid_position": ("invalid",),
    "outcome_partial": ("landed", "failed"),
    "host_rejected": ("status",),
    "quick_action_ran": ("ids",),
    "approved_stale_head": ("ids",),
    "audit_failed": ("outcome",),
    "body_too_large": ("index", "limit"),
    "config_invalid": ("key",),
    "tool_missing": ("install",),
    "wrong_host_response": ("observed",),
    "merge_unverified": ("observed", "head_sha"),
}
REQUIRED_DETAILS_BY_REASON: dict[str, tuple[str, ...]] = {
    "not_ready": ("blocked_by",),
    "drafts_present": ("count",),
    "stale_base": ("base_ref",),
    "not_open": ("state",),
    "self_approval": ("basis",),
    "method_not_allowed": ("allowed",),
    "auto_merge_unavailable": ("observed",),
}

# Which errors a command can end in once the host is involved. Codes the fake itself produces
# (usage, config_invalid, invalid_pr, body_too_large for input it read, confirmation_required,
# unknown_host) are not scriptable; a script names only what the host or a hook decides.
_HOST_READ_CODES = frozenset(
    {
        "not_found",
        "auth_required",
        "permission_denied",
        "rate_limited",
        "upstream_error",
        "network_error",
        "tool_missing",
        "host_rejected",
        "wrong_host_response",
        "unsupported_host_version",
        "internal",
    }
)
_POST_CODES = frozenset(
    {
        "vetoed",
        "escalated",
        "outcome_unknown",
        "outcome_partial",
        "quick_action_ran",
        "audit_unavailable",
        "audit_failed",
    }
)
ALLOWED_CODES: dict[str, frozenset[str]] = {
    "show": _HOST_READ_CODES,
    "files": _HOST_READ_CODES,
    "comments": _HOST_READ_CODES,
    "diff": _HOST_READ_CODES,
    "compare": _HOST_READ_CODES,
    "whoami": _HOST_READ_CODES,  # partial_results is a `partial` outcome, not an error
    "comment": _HOST_READ_CODES | _POST_CODES,
    "reply": _HOST_READ_CODES | _POST_CODES,
    "resolve": _HOST_READ_CODES | _POST_CODES,
    "unresolve": _HOST_READ_CODES | _POST_CODES,
    "review": _HOST_READ_CODES | _POST_CODES | {"invalid_position", "approved_stale_head"},
    "approve": _HOST_READ_CODES | _POST_CODES | {"approved_stale_head"},
}
_NOT_REVIEW = {"comment", "reply", "resolve", "unresolve", "show", "files", "comments", "whoami"}
ALLOWED_REASONS: dict[str, frozenset[str]] = {
    "show": frozenset(),
    "files": frozenset(),
    "comments": frozenset(),
    "whoami": frozenset(),
    "diff": frozenset({"too_large"}),
    "compare": frozenset({"too_large"}),
    "comment": frozenset(),
    "reply": frozenset(),
    "resolve": frozenset({"not_resolvable"}),
    "unresolve": frozenset({"not_resolvable"}),
    "review": frozenset(
        {
            "stale_head",
            "stale_base",
            "not_open",
            "self_approval",
            "too_large",
            "drafts_present",
            "pending_review_exists",
        }
    ),
    "approve": frozenset(
        {"stale_head", "stale_base", "not_open", "self_approval", "pending_review_exists"}
    ),
}
# These happen only after confirmation: a hook, or the write itself.
POST_CONFIRMATION_CODES = frozenset(
    {
        "vetoed",
        "escalated",
        "quick_action_ran",
        "approved_stale_head",
        "audit_failed",
        "outcome_unknown",
    }
)


class Fail(Exception):
    """A forgectl failure that ends the command with an exit code and the error envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        reason: str | None = None,
        details: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
        plan: dict[str, Any] | None = None,
        fields: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if code not in EXIT_BY_CODE:
            raise FakeError(f"unknown error code {code!r}")
        self.code = code
        self.message = message
        self.reason = reason
        self.details = details
        self.extra = extra or {}
        self.plan = plan
        self.fields = fields
        self.namespace: Namespace | None = None  # the parsed command line, when it got that far

    @property
    def exit_code(self) -> int:
        return EXIT_BY_CODE[self.code]

    def envelope(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.reason is not None:
            body["reason"] = self.reason
        if self.details:
            body["details"] = self.details
        body.update(self.extra)
        return {"error": body}


class FakeError(Exception):
    """The scenario or the call asks for something forgectl could not do (or the fake does not
    model). Exit 97; the fixture fails the test."""


def usage(message: str) -> Fail:
    return Fail("usage", message)


def refused(reason: str, message: str, details: dict[str, Any] | None = None) -> Fail:
    return Fail("refused", message, reason=reason, details=details)


# --- text sanitizing: what a scripted message must already look like -----------------------------

_EXTRA_MARKED = frozenset(
    [0x034F, 0x17B4, 0x17B5, 0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800]
    + list(range(0x180B, 0x1810))
    + list(range(0xFE00, 0xFE10))
    + list(range(0xE0100, 0xE01F0))
)


def unsanitized_char(text: str, *, allow: str = "\t\n") -> str | None:
    """The first character forgectl would have replaced with a ``<U+XXXX>`` marker, or None."""
    for ch in text:
        if ch in allow:
            continue
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp") or ord(ch) in _EXTRA_MARKED:
            return ch
    return None


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in _strings(k) + _strings(v)]
    return []


# --- identifiers (forgectl interface, Identifiers and Hosts) -------------------------------------

_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_HOST = rf"(?P<host>{_LABEL}(?:\.{_LABEL})*)(?::(?P<port>[0-9]{{1,5}}))?"
_SEG = r"[A-Za-z0-9._-]+"
_PROJECT = rf"(?P<project>{_SEG}(?:/{_SEG})*)"
_PROJECT_LAZY = rf"(?P<project>{_SEG}(?:/{_SEG})*?)"
_NUMBER = r"(?P<number>[1-9][0-9]{0,9})"
_TAIL = r"(?:\?(?P<query>[A-Za-z0-9._~=&-]*))?(?:#(?P<fragment>[A-Za-z0-9._~=&-]*))?"
_SHORT_SHA = r"[0-9A-Fa-f]{7,40}"
_ASCII = re.ASCII
_HOST_RE = re.compile(_HOST, _ASCII)
_PR_TEXT_RE = re.compile(rf"{_HOST}/{_PROJECT}#{_NUMBER}", _ASCII)
_GITHUB_URL_RE = re.compile(
    rf"https://{_HOST}/{_PROJECT_LAZY}"
    rf"/pull/{_NUMBER}(?:(?:/files|/commits(?:/{_SHORT_SHA})?|/checks)?/?|\.(?:diff|patch)/?){_TAIL}",
    _ASCII,
)
_GITLAB_URL_RE = re.compile(
    rf"https://{_HOST}/{_PROJECT_LAZY}(?:/-)?"
    rf"/merge_requests/{_NUMBER}(?:(?:/diffs|/commits|/pipelines)?/?){_TAIL}",
    _ASCII,
)
_SHA_RE = re.compile(r"[0-9a-fA-F]{40}", _ASCII)
_NODE = r"[A-Za-z0-9_=-]{1,100}"
_TYPED_ID_RE = re.compile(
    rf"(?:(?P<gh>gh):(?P<ghkind>ic|rc|rt|r):(?P<ghnode>{_NODE})"
    r"|(?P<gl>gl):d:(?P<discussion>[0-9a-f]{40})"
    r"|(?P<gln>gl):n:(?P<note>[1-9][0-9]{0,18}))",
    _ASCII,
)
ACTOR_RE = re.compile(r"(?:human|auto):[A-Za-z0-9._@-]{1,64}", _ASCII)
DECISION_ID_RE = re.compile(r"[A-Za-z0-9._:@/-]{1,128}", _ASCII)
_DEDUPE_KEY_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}", _ASCII)
_DURATION_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h|d)?", _ASCII)
_UNIT_SECONDS = {None: 1.0, "ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_MAX_DURATION_SECONDS = 100 * 365 * 86400.0
_BRANCH_FORBIDDEN_CHARS = frozenset(" ~^:?*[\\\x7f")
_BRANCH_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_BAD_PROJECT_SEGMENTS_IN_URL = frozenset({"-", "merge_requests"})


def _shown(text: str) -> str:
    return repr(text if len(text) <= 100 else text[:100] + "...")


def _normalize_host(host: str, port: str | None) -> str | None:
    host = host.lower()
    if port is not None:
        if port.startswith("0") or not 1 <= int(port) <= 65535:
            return None
        if port == "443":
            port = None
    if port is None and host.startswith("www.") and host[4:] in BUILT_IN_HOSTS:
        host = host[4:]
    if len(host) > 253:
        return None
    return host if port is None else f"{host}:{port}"


@dataclass(frozen=True)
class PrInput:
    host: str
    project: str
    number: int
    kind: str | None
    raw: str = field(default="", compare=False)

    def __str__(self) -> str:
        return f"{self.host}/{self.project}#{self.number}"


@dataclass(frozen=True)
class TypedId:
    prefix: str
    kind: str
    node: str

    def __str__(self) -> str:
        return f"{self.prefix}:{self.kind}:{self.node}"


def _clean_segments(project: str) -> bool:
    return not any(seg in (".", "..") for seg in project.split("/"))


def parse_pr(text: str) -> PrInput:
    match = None
    kind: str | None = None
    if len(text) <= MAX_INPUT_CHARS:
        if text.startswith("https://"):
            match = _GITHUB_URL_RE.fullmatch(text)
            kind = "pull"
            if match is None:
                match = _GITLAB_URL_RE.fullmatch(text)
                kind = "merge_requests"
        else:
            match = _PR_TEXT_RE.fullmatch(text)
    parsed: PrInput | None = None
    if match is not None:
        host = _normalize_host(match["host"], match["port"])
        project = match["project"]
        if host is not None and _clean_segments(project):
            bad = kind == "merge_requests" and _BAD_PROJECT_SEGMENTS_IN_URL.intersection(
                project.split("/")
            )
            if not bad:
                parsed = PrInput(host, project, int(match["number"]), kind, text)
    if parsed is None:
        raise Fail("invalid_pr", f"not a pull request URL or host/<project path>#N: {_shown(text)}")
    return parsed


def parse_host(text: str) -> str:
    match = _HOST_RE.fullmatch(text) if len(text) <= MAX_INPUT_CHARS else None
    host = _normalize_host(match["host"], match["port"]) if match else None
    if host is None:
        raise usage(f"not a valid host: {_shown(text)}")
    return host


def parse_typed_id(text: str) -> TypedId:
    match = _TYPED_ID_RE.fullmatch(text) if len(text) <= MAX_INPUT_CHARS else None
    if match is None:
        raise usage(f"not a valid comment or thread id: {_shown(text)}")
    if match["gh"]:
        return TypedId("gh", match["ghkind"], match["ghnode"])
    if match["discussion"]:
        return TypedId("gl", "d", match["discussion"])
    return TypedId("gl", "n", match["note"])


def parse_sha(text: str) -> str:
    if len(text) != 40 or not _SHA_RE.fullmatch(text):
        raise usage(f"not a full 40-hex sha: {_shown(text)}")
    return text.lower()


def parse_branch(name: str) -> str:
    if not _valid_branch(name):
        raise usage(f"not a valid branch name: {_shown(name)}")
    return name


def _valid_branch(name: str) -> bool:
    if not name or len(name) > MAX_BRANCH_CHARS or name in ("@", "HEAD", "refs"):
        return False
    if name[0] in "-/" or name[-1] in "/." or name.startswith("refs/"):
        return False
    if ".." in name or "@{" in name or "//" in name:
        return False
    for ch in name:
        if (
            ch in _BRANCH_FORBIDDEN_CHARS
            or unicodedata.category(ch) in _BRANCH_FORBIDDEN_CATEGORIES
        ):
            return False
    return not any(part.startswith(".") or part.endswith(".lock") for part in name.split("/"))


# --- the argument parser (the same argparse construction as forgectl's) --------------------------


def _bad(message: str) -> NoReturn:
    raise argparse.ArgumentTypeError(message)


def nonempty(value: str) -> str:
    if not value:
        _bad("must not be empty")
    return value


def duration(value: str) -> float:
    match = _DURATION_RE.fullmatch(value)
    seconds = float(match[1]) * _UNIT_SECONDS[match[2]] if match else math.nan
    if not math.isfinite(seconds) or seconds <= 0:
        _bad(f"not a positive duration (seconds, or a number with ms, s, m, h or d): {value!r}")
    if seconds > _MAX_DURATION_SECONDS:
        _bad(f"longer than 100 years: {value!r}")
    return seconds


def actor_type(value: str) -> str:
    if not ACTOR_RE.fullmatch(value):
        _bad(f"must look like human:<user> or auto:<name>: {value!r}")
    return value


def decision_id_type(value: str) -> str:
    if not DECISION_ID_RE.fullmatch(value):
        _bad(f"not a valid decision id: {value!r}")
    return value


def dedupe_key_type(value: str) -> str:
    if not _DEDUPE_KEY_RE.fullmatch(value):
        _bad(f"not a valid dedupe key: {value!r}")
    return value


_SEEN: ContextVar[set[str] | None] = ContextVar("fake_forgectl_seen_flags", default=None)


def _note_flag(parser: argparse.ArgumentParser, dest: str, option: str | None) -> None:
    seen = _SEEN.get()
    if seen is None:
        return
    if dest in seen:
        parser.error(f"{option} given more than once")
    seen.add(dest)


class _Once(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        _note_flag(parser, self.dest, option_string)
        setattr(namespace, self.dest, values)


class _OnceFlag(_Once):
    def __init__(
        self,
        option_strings: list[str],
        dest: str,
        default: Any = False,
        required: bool = False,
        help: str | None = None,
    ) -> None:
        super().__init__(
            option_strings, dest, nargs=0, default=default, required=required, help=help
        )

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        super().__call__(parser, namespace, True, option_string)


class _MustFollowCommand(argparse.Action):
    def __init__(self, option_strings: list[str], dest: str, **kw: Any) -> None:
        super().__init__(
            option_strings, dest, nargs="?", default=argparse.SUPPRESS, help=argparse.SUPPRESS
        )

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        parser.error(
            f"{option_string} must follow the command (forgectl <command> ... {option_string})"
        )


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise usage(message)

    def _get_option_tuples(self, option_string: str) -> list[Any]:
        return []

    def _print_message(self, message: str, file: Any = None) -> None:
        if message:
            write_line(file if file is not None else sys.stderr, message.removesuffix("\n"))


def _globals(parser: argparse.ArgumentParser, *, sub: bool) -> None:
    flag_default: Any = argparse.SUPPRESS if sub else False
    value_default: Any = argparse.SUPPRESS if sub else None
    parser.add_argument("--json", action=_OnceFlag, default=flag_default)
    parser.add_argument("--verbose", action=_OnceFlag, default=flag_default)
    parser.add_argument("--quiet", action=_OnceFlag, default=flag_default)
    parser.add_argument(
        "--config", metavar="PATH", type=nonempty, action=_Once, default=value_default
    )
    parser.add_argument(
        "--timeout", metavar="D", type=duration, action=_Once, default=value_default
    )


def _writes(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--yes", action=_OnceFlag)
    parser.add_argument("--dry-run", action=_OnceFlag)
    parser.add_argument("--actor", type=actor_type, action=_Once, default=None, metavar="A")
    parser.add_argument(
        "--decision-id", dest="decision_id", type=decision_id_type, action="append", metavar="D"
    )
    parser.add_argument("--override-escalation", action=_OnceFlag)


def _once(parser: argparse.ArgumentParser, flag: str, **kw: Any) -> None:
    kw.setdefault("default", None)
    parser.add_argument(flag, action=_Once, **kw)


def _expected(parser: argparse.ArgumentParser) -> None:
    _once(parser, "--expected-head", type=parse_sha, metavar="SHA")
    _once(parser, "--expected-base", type=parse_branch, metavar="REF")


def _pr(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("pr", type=parse_pr)


def build_parser(version: str = VERSION) -> argparse.ArgumentParser:
    parser = _Parser(prog="forgectl", allow_abbrev=False)
    parser.add_argument("--version", action="version", version=f"forgectl {version}")
    _globals(parser, sub=False)
    for flag in ("--yes", "--dry-run", "--actor", "--decision-id", "--override-escalation"):
        parser.add_argument(flag, action=_MustFollowCommand)
    subs = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    def add(name: str) -> argparse.ArgumentParser:
        sub = subs.add_parser(name, allow_abbrev=False)
        _globals(sub, sub=True)
        return sub

    for name in COMMAND_ORDER:
        if name in UNMODELED:
            subs.add_parser(name, add_help=False).add_argument("rest", nargs=argparse.REMAINDER)
            continue
        p = add(name)
        if name in ("show", "diff", "files", "comments"):
            _pr(p)
        elif name == "compare":
            _pr(p)
            p.add_argument("--from", dest="from_sha", type=parse_sha, required=True, action=_Once)
            p.add_argument("--to", dest="to_sha", type=parse_sha, required=True, action=_Once)
            p.add_argument("--no-diff", dest="no_diff", action=_OnceFlag)
        elif name == "whoami":
            _once(p, "--host", type=parse_host, metavar="HOST")
        elif name == "review":
            _pr(p)
            _once(p, "--file", type=nonempty, required=True, default=argparse.SUPPRESS)
            _expected(p)
            p.add_argument("--allow-self", action=_OnceFlag)
            _once(p, "--dedupe-key", dest="dedupe_key", type=dedupe_key_type)
            _writes(p)
        elif name == "comment":
            _pr(p)
            _once(
                p,
                "--body-file",
                dest="body_file",
                type=nonempty,
                required=True,
                default=argparse.SUPPRESS,
            )
            _once(p, "--dedupe-key", dest="dedupe_key", type=dedupe_key_type)
            _writes(p)
        elif name == "reply":
            _pr(p)
            _once(p, "--to", type=parse_typed_id, required=True, default=argparse.SUPPRESS)
            _once(
                p,
                "--body-file",
                dest="body_file",
                type=nonempty,
                required=True,
                default=argparse.SUPPRESS,
            )
            _once(p, "--dedupe-key", dest="dedupe_key", type=dedupe_key_type)
            _writes(p)
        elif name in ("resolve", "unresolve"):
            _pr(p)
            _once(p, "--thread", type=parse_typed_id, required=True, default=argparse.SUPPRESS)
            _writes(p)
        elif name == "approve":
            _pr(p)
            _expected(p)
            p.add_argument("--allow-self", action=_OnceFlag)
            _once(p, "--body-file", dest="body_file", type=nonempty)
            _once(p, "--dedupe-key", dest="dedupe_key", type=dedupe_key_type)
            _writes(p)
    return parser


def parse_argv(argv: list[str], version: str = VERSION) -> Namespace:
    """Parse as forgectl does; raises ``Fail`` (usage) for anything the surface does not allow."""
    parser = build_parser(version)
    token = _SEEN.set(set())
    try:
        # parse_args is parse_known_args plus the error for what is left over; an unmodeled
        # command is found before that error, since its own flags are not known here
        args, extras = parser.parse_known_args(argv)
        if args.command in UNMODELED:
            raise FakeError(f"forgectl {args.command} is not modeled by the fake")
        if extras:
            parser.error("unrecognized arguments: " + " ".join(extras))
    finally:
        _SEEN.reset(token)
    if args.verbose and args.quiet:
        raise usage("--verbose and --quiet cannot be combined")
    return args


def wants_json(argv: list[str]) -> bool:
    head = argv[: argv.index("--")] if "--" in argv else argv
    return any(arg == "--json" or arg.startswith("--json=") for arg in head)


# --- the input of a post (forgectl interface, Post order step 1) ---------------------------------

_LINE_BREAK = re.compile("\r\n|[\n\r\x0b\x0c\x1c\x1d\x1e\x85  ]")
_MARKER = re.compile(r"(forgectl:dedupe):", re.IGNORECASE)
STDIN_SPELLINGS = ("-", "/dev/stdin", "/dev/fd/0")


def neutralize(body: str) -> tuple[str, int, int]:
    """(text, neutralized_lines, markers_removed): split on every line separator, rejoin with LF,
    one leading space on each line starting with ``/``; marker-shaped text defused."""
    lines = _LINE_BREAK.split(body)
    count = 0
    for i, line in enumerate(lines):
        if line.startswith("/"):
            lines[i] = " " + line
            count += 1
    text, removed = _MARKER.subn(r"\1-", "\n".join(lines))
    return text, count, removed


def marker_line(key: str) -> str:
    return f"<!-- forgectl:dedupe:{key} -->"


def read_input(source: str, cap: int, stdin: Any) -> bytes:
    """At most ``cap`` bytes of a path or of ``-`` (the inherited stdin, read up to the cap)."""
    if source == "-":
        data = stdin.read(cap)
        return data if isinstance(data, bytes) else str(data).encode("utf-8")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(source, flags)
    except OSError as err:
        raise usage(f"the body file cannot be opened ({err.strerror})") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise usage("the body file is not a regular file")
        chunks: list[bytes] = []
        total = 0
        while total < cap:
            data = os.read(fd, min(65536, cap - total))
            if not data:
                break
            chunks.append(data)
            total += len(data)
        return b"".join(chunks)
    finally:
        os.close(fd)


def limit_for(backend: str, to: TypedId | None) -> int:
    if backend != "github":
        return GITLAB_LIMIT
    if to is not None and to.prefix == "gh" and to.kind in ("rt", "rc"):
        return INLINE_COMMENT_LIMIT
    return GITHUB_COMMENT_LIMIT


def _too_large(limit: int, index: int | None, what: str | None = None) -> Fail:
    what = what or ("the body" if index is None else f"inline comment {index}")
    return Fail(
        "body_too_large",
        f"{what} is larger than {limit} bytes",
        details={"index": index, "limit": limit},
    )


@dataclass(frozen=True)
class Prepared:
    """What a post would send: the plan of a dry run and the counts of every output."""

    event: str | None
    body: str | None
    comments: int
    sha256: str | None
    nbytes: int
    neutralized_lines: int
    markers_removed: int

    def plan(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "body_sha256": self.sha256,
            "body_bytes": self.nbytes,
            "inline_comments": self.comments,
        }


def prepare_body(source: str, *, limit: int, key: str | None, stdin: Any) -> Prepared:
    raw = read_input(source, limit + 1, stdin)
    if len(raw) > limit:
        raise _too_large(limit, None)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise usage("the body is not valid UTF-8") from None
    if not text.strip():
        raise usage("the body is empty")
    posted, count, removed = neutralize(text)
    if key is not None:
        posted += ("" if posted.endswith("\n") else "\n") + marker_line(key)
    data = posted.encode("utf-8")
    if len(data) > limit:
        raise _too_large(limit, None)
    return Prepared(None, posted, 0, hashlib.sha256(data).hexdigest(), len(data), count, removed)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("a key is repeated")
    return dict(pairs)


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _text(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise usage(f"review file: {where} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise usage(f"review file: {where} is not valid text (a lone surrogate)") from None
    return value


def _int(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise usage(f"review file: {where} must be an integer of at least 1")
    return value


@dataclass(frozen=True)
class RawComment:
    index: int
    path: str
    line: int
    side: str
    start_line: int | None
    body: str


def parse_review_input(raw: bytes) -> tuple[str, str | None, tuple[RawComment, ...]]:
    """The review file as forgectl's rules allow it (schemas/review-input.json and the interface's
    own: no empty review, ``request_changes`` needs a body, at most 200 comments)."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise usage("the review file is not valid UTF-8") from None
    try:
        doc = json.loads(text, object_pairs_hook=_pairs, parse_constant=_no_constant)
    except (ValueError, RecursionError) as err:
        raise usage(f"the review file is not valid JSON ({type(err).__name__})") from None
    if not isinstance(doc, dict):
        raise usage("review file: the top level must be an object")
    extra = sorted(set(doc) - {"event", "body", "comments"})
    if extra:
        raise usage(f"review file: unknown field {extra[0]!r}")
    event = doc.get("event")
    if event not in ("comment", "request_changes", "approve"):
        raise usage("review file: event must be comment, request_changes or approve")
    body = doc.get("body")
    if body is not None:
        body = _text(body, "body")
        if not body.strip():
            raise usage("review file: body is empty (use null for none)")
    items = doc.get("comments", [])
    if not isinstance(items, list):
        raise usage("review file: comments must be a list")
    if len(items) > MAX_COMMENTS:
        raise usage(f"review file: comments holds {len(items)} items; at most {MAX_COMMENTS}")
    comments = tuple(_review_comment(i, item) for i, item in enumerate(items))
    if event == "request_changes" and body is None:
        raise usage("review file: request_changes needs a body")
    if event == "comment" and body is None and not comments:
        raise usage("review file: an empty review (a comment with no body and no comments)")
    return event, body, comments


def _review_comment(index: int, item: object) -> RawComment:
    where = f"comments[{index}]"
    if not isinstance(item, dict):
        raise usage(f"review file: {where} must be an object")
    extra = sorted(set(item) - {"path", "line", "side", "start_line", "body"})
    if extra:
        raise usage(f"review file: {where}: unknown field {extra[0]!r}")
    for key in ("path", "line", "body"):
        if key not in item:
            raise usage(f"review file: {where}.{key} is required")
    path = _text(item["path"], f"{where}.path")
    if not path:
        raise usage(f"review file: {where}.path is empty")
    side = item.get("side", "right")
    if side not in ("left", "right"):
        raise usage(f"review file: {where}.side must be left or right")
    start = item.get("start_line")
    if "start_line" in item:
        start = _int(start, f"{where}.start_line")
    body = _text(item["body"], f"{where}.body")
    if not body.strip():
        raise usage(f"review file: {where}.body is empty")
    return RawComment(index, path, _int(item["line"], f"{where}.line"), side, start, body)


def prepare_review(source: str, key: str | None, backend: str, stdin: Any) -> Prepared:
    raw = read_input(source, FILE_LIMIT + 1, stdin)
    if len(raw) > FILE_LIMIT:
        raise _too_large(FILE_LIMIT, None, "the review file")
    event, body_in, comments_in = parse_review_input(raw)
    gitlab = backend == "gitlab"
    body_limit = GITLAB_LIMIT if gitlab else GITHUB_COMMENT_LIMIT
    inline_limit = GITLAB_LIMIT if gitlab else INLINE_COMMENT_LIMIT
    if key is not None and body_in is None and not comments_in:
        raise usage("--dedupe-key needs a review body or inline comments to carry the marker")
    neutralized = markers = 0
    body: str | None = None
    if body_in is not None:
        body, n, m = neutralize(body_in)
        neutralized += n
        markers += m
    marker = None if key is None else marker_line(key)

    def with_marker(text: str) -> str:
        return text + ("" if text.endswith("\n") else "\n") + marker_line(key or "")

    if marker is not None and not (gitlab and body is None):
        body = marker if body is None else with_marker(body)
    if body is not None and len(body.encode("utf-8")) > body_limit:
        raise _too_large(body_limit, None, "the review body")
    posted: list[dict[str, Any]] = []
    for n_, item in enumerate(comments_in):
        text, n, m = neutralize(item.body)
        neutralized += n
        markers += m
        if marker is not None and gitlab and body is None and n_ == len(comments_in) - 1:
            text = with_marker(text)
        if len(text.encode("utf-8")) > inline_limit:
            raise _too_large(inline_limit, item.index)
        posted.append(
            {
                "path": item.path,
                "line": item.line,
                "side": item.side,
                "start_line": item.start_line,
                "body": text,
            }
        )
    if body is None and not posted:
        return Prepared(event, None, 0, None, 0, neutralized, markers)
    canonical = json.dumps(
        {"body": body, "comments": posted},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    total = len((body or "").encode("utf-8")) + sum(len(c["body"].encode("utf-8")) for c in posted)
    return Prepared(
        event, body, len(posted), hashlib.sha256(canonical).hexdigest(), total, neutralized, markers
    )


# --- schemas -------------------------------------------------------------------------------------


@cache
def _registry() -> tuple[Registry[Any], dict[str, Any]]:
    registry: Registry[Any] = Registry()
    by_id: dict[str, Any] = {}
    for path in sorted(SCHEMAS_DIR.rglob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        by_id[doc["$id"]] = doc
        registry = registry.with_resource(
            doc["$id"], Resource.from_contents(doc, default_specification=DRAFT202012)
        )
    return registry, by_id


def schema_errors(schema_id: str, document: Any) -> list[str]:
    """Every violation of ``document`` against the pinned schema ``schema_id`` ("cli/review.json"
    or "defs.json#/$defs/id"), as ``pointer: message`` lines; empty when valid."""
    registry, by_id = _registry()
    name = f"forgectl/{schema_id}"
    if name.split("#", 1)[0] not in by_id:
        raise FakeError(f"no pinned schema {schema_id!r}")
    validator = Draft202012Validator(
        {"$schema": "https://json-schema.org/draft/2020-12/schema", "$ref": name},
        registry=registry,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )
    return sorted(
        f"/{'/'.join(str(p) for p in e.absolute_path)}: {e.message}"[:300]
        for e in validator.iter_errors(document)
    )


def document_schema(command: str) -> str:
    return f"cli/{command}.json"


# --- output --------------------------------------------------------------------------------------


def dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=True)


def write_text(stream: TextIO | None, text: str) -> bool:
    if stream is None:
        return False
    try:
        stream.write(text)
        stream.flush()
    except (OSError, ValueError):
        with contextlib.suppress(OSError, ValueError, AttributeError):
            fd = stream.fileno()
            devnull = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull, fd)
            finally:
                os.close(devnull)
        return False
    return True


def write_line(stream: TextIO | None, text: str) -> bool:
    return write_text(stream, text + "\n")


def error_text(err: Fail) -> str:
    label = err.code if err.reason is None else f"{err.code} ({err.reason})"
    return f"forgectl: error: {label}: {err.message.replace(chr(10), ' ')}"


# --- scripted answers: what a scenario may say, and the checks on it -----------------------------


@dataclass
class Context:
    """Everything a run knows once the command line is parsed and the input is read."""

    args: Namespace
    argv: list[str]
    command: str
    backend: str | None
    pr: str | None
    prep: Prepared | None
    hosts: dict[str, str]
    version: str
    confirmed: bool  # a write that may proceed past confirmation (--yes), or a dry run
    dry_run: bool


def _would_refuse_result(ctx: Context, err: Fail) -> dict[str, Any]:
    """The ``--dry-run`` result for an exit 20 (forgectl: Global flags): the command's output with
    ``would_refuse``; fields the refusal came too early to know are null."""
    args = ctx.args
    command = ctx.command
    refusal = {"reason": err.reason, "message": err.message, "details": err.details or None}
    pr = str(args.pr)
    result: dict[str, Any]
    if command in ("resolve", "unresolve"):
        result = {
            "pr": pr,
            "action": command,
            "thread_id": str(args.thread),
            "resolved": None,
            "changed": False,
            "dry_run": True,
            "would_refuse": refusal,
        }
    else:
        result = {
            "pr": pr,
            "action": command,
            "ids": [],
            "thread_ids": [],
            "review_id": None,
            "already_posted": False,
            "follow_up_done": False,
            "neutralized_lines": 0,
            "markers_removed": 0,
            "warnings": [],
            "head_sha": None,
            "dry_run": True,
            "would_refuse": refusal,
            "plan": {"event": None, "body_sha256": None, "body_bytes": 0, "inline_comments": 0},
        }
        if command in ("review", "approve"):
            result["head_verified"] = None
        if command == "reply":
            result["threaded"] = False
        if ctx.prep is not None:
            # the refusal came after the input was read, as forgectl's write commands do
            result["neutralized_lines"] = ctx.prep.neutralized_lines
            result["markers_removed"] = ctx.prep.markers_removed
            result["plan"] = ctx.prep.plan()
    if err.plan and "plan" in result:
        result["plan"].update(err.plan)
    if err.fields:
        result.update(err.fields)
    return result


def check_error(ctx: Context, err: Fail, *, scripted: bool = True) -> None:
    """An error must be one forgectl can emit for this command, in its exact shape. A scripted one
    (``scripted``) also names only what the host or a hook decides: the fake itself answers for
    the command line, the input, the closed host list and confirmation."""
    command = ctx.command
    own = ("usage", "config_invalid", "invalid_pr", "body_too_large", "confirmation_required")
    if scripted:
        if err.code in own[:4]:
            raise FakeError(
                f"{err.code} comes from the command line or the input, which the fake checks "
                "itself; script a host or hook outcome"
            )
        if err.code == "confirmation_required":
            raise FakeError("confirmation_required is made by the fake from the argv (no --yes)")
        if err.code == "partial_results":
            raise FakeError("partial_results is a `partial` outcome (whoami)")
        if err.reason == "unknown_host":
            raise FakeError("unknown_host is made by the fake from the closed host list")
    if err.code == "refused":
        if err.reason not in ALLOWED_REASONS[command] | (
            frozenset() if scripted else frozenset({"unknown_host"})
        ):
            raise FakeError(f"forgectl {command} cannot refuse with reason {err.reason!r}")
    elif err.code == "partial_results" and command == "whoami" and not scripted:
        pass
    elif err.code not in ALLOWED_CODES[command] | (frozenset() if scripted else frozenset(own)):
        raise FakeError(f"forgectl {command} cannot fail with code {err.code!r}")
    if err.reason is not None and err.code != "refused":
        raise FakeError("reason is only for code 'refused'")
    if err.code in ("vetoed", "escalated") and not (
        "hook" in err.extra and "hook_stderr" in err.extra
    ):
        raise FakeError(f"{err.code} carries hook and hook_stderr (even when empty)")
    if err.code == "escalated" and not err.extra.get("escalated_hooks"):
        raise FakeError("escalated carries escalated_hooks")
    if err.code != "escalated" and "escalated_hooks" in err.extra:
        raise FakeError("escalated_hooks is only on escalated")
    if err.code not in ("vetoed", "escalated") and (
        "hook" in err.extra or "hook_stderr" in err.extra
    ):
        raise FakeError("hook and hook_stderr are only on vetoed and escalated")
    if "retry_after" in err.extra and err.code not in (
        "rate_limited",
        "upstream_error",
        "network_error",
    ):
        # a read's last retryable failure carries the wait the host asked for, when known
        raise FakeError("retry_after is only on rate_limited, upstream_error and network_error")
    required = REQUIRED_DETAILS_BY_CODE.get(err.code, ()) + REQUIRED_DETAILS_BY_REASON.get(
        err.reason or "", ()
    )
    missing = [k for k in required if k not in (err.details or {})]
    if missing:
        raise FakeError(f"{err.reason or err.code} needs details {missing}")
    hook_stderr = err.extra.get("hook_stderr")
    if isinstance(hook_stderr, str) and len(hook_stderr.encode("utf-8")) > HOOK_STDERR_LIMIT:
        raise FakeError("hook_stderr is cut to 4096 bytes")
    for text in _strings(err.envelope()):
        bad = unsanitized_char(text)
        if bad is not None:
            raise FakeError(
                f"scripted text holds U+{ord(bad):04X}, which forgectl prints as a <U+XXXX> marker"
            )
    problems = schema_errors("cli/error.json", err.envelope())
    if problems:
        raise FakeError("scripted error violates the pinned error schema: " + "; ".join(problems))
    # a stale_head refusal in a dry run is a read-time refusal, so is any reason of the table
    if err.code in POST_CONFIRMATION_CODES and ctx.dry_run:
        raise FakeError(f"{err.code} cannot happen in a dry run (no hook runs, nothing is written)")


def check_document(
    ctx: Context, doc: dict[str, Any], *, partial: bool = False, fill: bool = True
) -> dict[str, Any]:
    """A result must be one forgectl prints: valid against the pinned schema and consistent with
    the argv and the input. With ``fill``, missing derived fields of a post (the counts and a dry
    run's plan) are filled from the input; a given one must equal it."""
    command = ctx.command
    args = ctx.args
    doc = json.loads(dumps(doc))  # a copy, in JSON types
    prep = ctx.prep
    if command in ("review", "comment", "reply", "approve") and prep is not None:
        for key, want in (
            ("neutralized_lines", prep.neutralized_lines),
            ("markers_removed", prep.markers_removed),
        ):
            if key in doc and doc[key] != want:
                raise FakeError(f"{key} is {doc[key]!r} but the input gives {want}")
            if fill:
                doc.setdefault(key, want)
        if ctx.dry_run:
            plan = doc.setdefault("plan", {}) if fill else doc.get("plan", {})
            for key, want in prep.plan().items():
                if key in plan and plan[key] != want:
                    raise FakeError(f"plan.{key} is {plan[key]!r} but the input gives {want!r}")
                if fill:
                    plan.setdefault(key, want)
    if (
        fill
        and ctx.dry_run
        and command in ("review", "comment", "reply", "approve", "resolve", "unresolve")
    ):
        doc.setdefault("would_refuse", None)
    problems = schema_errors(document_schema(command), doc)
    if problems:
        raise FakeError("scripted document violates the pinned schema: " + "; ".join(problems))
    for text in _strings(doc):
        bad = unsanitized_char(text, allow="\t\n\r")
        if bad is not None:
            raise FakeError(
                f"scripted text holds U+{ord(bad):04X}, which forgectl prints as a <U+XXXX> marker"
            )
    _check_consistency(ctx, doc, args, partial=partial)
    return doc


def _same_pr(doc_pr: str, pr: str) -> bool:
    return doc_pr.lower() == pr.lower()


def _check_consistency(
    ctx: Context, doc: dict[str, Any], args: Namespace, *, partial: bool
) -> None:
    command = ctx.command
    if command == "whoami":
        hosts = sorted(h["host"] for h in doc["hosts"])
        want = [args.host] if args.host else sorted(set(BUILT_IN_HOSTS) | set(ctx.hosts))
        if hosts != sorted(want):
            raise FakeError(f"whoami lists hosts {hosts}, forgectl lists {sorted(want)}")
        for entry in doc["hosts"]:
            backend = ctx.hosts.get(entry["host"]) or BUILT_IN_HOSTS.get(entry["host"])
            if entry["backend"] != backend:
                raise FakeError(f"whoami: host {entry['host']} has backend {backend}")
        if partial != bool(doc["errors"]):
            raise FakeError("whoami: exit 12 (partial_results) lists errors, exit 0 lists none")
        return
    if not _same_pr(doc["pr"], str(args.pr)):
        raise FakeError(f"document pr {doc['pr']!r} is not the PR asked for, {str(args.pr)!r}")
    if command in ("show",):
        want = ctx.backend
        if doc["backend"] != want:
            raise FakeError(f"show: backend is {doc['backend']!r}, the host's is {want!r}")
    if command == "compare":
        if doc["from_sha"] != args.from_sha or doc["to_sha"] != args.to_sha:
            raise FakeError("compare: from_sha and to_sha are the ones asked for")
        if args.no_diff and "diff" in doc:
            raise FakeError("compare --no-diff prints no `diff` key")
        if not args.no_diff and "diff" not in doc:
            raise FakeError("compare without --no-diff prints a `diff` key")
    if command in ("resolve", "unresolve"):
        if doc["thread_id"] != str(args.thread):
            raise FakeError("thread_id is not the one asked for")
        if doc["dry_run"] and doc["changed"]:
            raise FakeError("a dry run changes nothing: changed is false")
        want = command == "resolve"
        if doc["resolved"] not in (want, None):
            raise FakeError(f"{command}: resolved is {doc['resolved']!r}")
        if doc["dry_run"] != ctx.dry_run:
            raise FakeError("dry_run does not match --dry-run")
        return
    if command in ("review", "comment", "reply", "approve"):
        _check_post(ctx, doc, args)


def _check_post(ctx: Context, doc: dict[str, Any], args: Namespace) -> None:
    command = ctx.command
    if doc["dry_run"] != ctx.dry_run:
        raise FakeError("dry_run does not match --dry-run")
    key = getattr(args, "dedupe_key", None)
    prep = ctx.prep
    would_refuse = doc.get("would_refuse")
    if would_refuse is not None and not ctx.dry_run:
        raise FakeError("would_refuse is for a dry run")
    prefix = "gh:" if ctx.backend == "github" else "gl:"
    for ident in doc["ids"] + doc["thread_ids"] + ([doc["review_id"]] if doc["review_id"] else []):
        if not ident.startswith(prefix):
            raise FakeError(f"id {ident!r} is not a {ctx.backend} id ({prefix}...)")
    if ctx.backend == "gitlab" and doc["review_id"] is not None:
        raise FakeError("a GitLab post has no review_id")
    if ctx.backend == "github" and command in ("review", "approve") and doc["thread_ids"]:
        raise FakeError("a GitHub review reports no thread_ids (a read of `comments` gives them)")
    head = doc["head_sha"]
    expected = getattr(args, "expected_head", None)
    if command in ("review", "approve") and expected and head is not None and head != expected:
        raise FakeError("head_sha differs from --expected-head: forgectl exits 20 stale_head")
    event = prep.event if prep is not None else None
    if command == "approve":
        event = "approve"  # the approve command's own event; its plan reports null
    verified = doc.get("head_verified")
    if command in ("review", "approve") and verified is not None and event != "approve":
        raise FakeError("head_verified is null unless the event is approve")
    if command == "reply" and ctx.backend == "github":
        # GitHub has no thread under a general comment or a review: a reply to `gh:ic:` or `gh:r:`
        # is a new general comment and reports threaded: false; a reply to a review comment or
        # thread joins its thread
        want = args.to.kind in ("rc", "rt")
        if doc["threaded"] is not want:
            raise FakeError(f"reply to {args.to}: threaded is {want} on GitHub")
    if doc["already_posted"]:
        text = prep is not None and prep.sha256 is not None
        stateless = (
            command == "approve"
            and not text
            or (command == "review" and event == "approve" and not text)
        )
        if key is None and not stateless:
            raise FakeError("already_posted needs a --dedupe-key (or no text and an approval)")
    if doc["follow_up_done"]:
        if key is None or ctx.backend != "gitlab" or event not in ("approve", "request_changes"):
            raise FakeError(
                "follow_up_done: only a GitLab dedupe hit whose approve or request_changes "
                "state step was still missing"
            )
        if not doc["already_posted"]:
            raise FakeError("follow_up_done follows a dedupe hit (already_posted)")
    if ctx.dry_run and (doc["follow_up_done"]):
        raise FakeError("a dry run performs no state step")


def check_call(
    argv: list[str], stdin: bytes | None, *, hosts: Mapping[str, str] | None = None
) -> str | None:
    """Why forgectl would refuse this call before asking any host (its command line, the closed
    host list, the flags it requires, the input it reads from stdin), or None."""
    import io

    try:
        build_call(argv, hosts or {}, io.BytesIO(stdin or b""), check_config=False)
    except Fail as err:
        return f"forgectl refuses this call ({err.code}, exit {err.exit_code}): {err.message}"
    except FakeError as err:
        return str(err)
    except SystemExit:
        return None  # --version
    return None


def judge(
    argv: list[str],
    stdin: bytes | None,
    exit_code: int | None,
    stdout: bytes,
    *,
    hosts: Mapping[str, str] | None = None,
) -> list[str]:
    """What is wrong with an answer to ``argv`` (and ``stdin``) that forgectl could not have
    given: a call it refuses, a document that breaks the pinned schema or contradicts the argv or
    the input, an exit code that is not its error's, an error the command cannot end in. Empty
    when the answer is one forgectl could print."""
    import io

    try:
        ctx = build_call(argv, hosts or {}, io.BytesIO(stdin or b""), check_config=False)
    except Fail as err:
        return [f"forgectl refuses this call ({err.code}, exit {err.exit_code}): {err.message}"]
    except FakeError as err:
        return [str(err)]
    except SystemExit:
        return []  # --version
    if exit_code is None or exit_code > 128:
        return [] if not stdout else ["a run killed by a signal printed a result"]
    try:
        text = stdout.decode("utf-8")
        document: Any = json.loads(text)
    except ValueError:
        return [f"stdout is not a JSON document (exit {exit_code})"]
    try:
        if exit_code == 0:
            check_document(ctx, document, fill=False)
        elif (
            isinstance(document, dict)
            and "error" in document
            and isinstance(document["error"], dict)
        ):
            body: dict[str, Any] = document["error"]
            code = body.get("code")
            if code not in EXIT_BY_CODE:
                return [f"unknown error code {code!r}"]
            extra = {
                k: body[k]
                for k in ("retry_after", "hook", "hook_stderr", "decision_ids", "escalated_hooks")
                if k in body
            }
            err = Fail(
                code,
                body.get("message", ""),
                reason=body.get("reason"),
                details=body.get("details"),
                extra=extra,
            )
            if err.exit_code != exit_code:
                return [f"{code} exits {err.exit_code}, not {exit_code}"]
            check_error(ctx, err, scripted=False)
            if err.code == "refused" and ctx.dry_run and ctx.command in WRITE_COMMANDS:
                return ["a refusal on a dry run prints the plan with would_refuse, not an envelope"]
        elif exit_code == 20 and ctx.dry_run and ctx.command in WRITE_COMMANDS:
            check_document(ctx, document, fill=False)
            if not document.get("would_refuse"):
                return ["exit 20 on a dry run carries would_refuse"]
        elif exit_code == 12 and ctx.command == "whoami":
            check_document(ctx, document, partial=True, fill=False)
        else:
            return [f"exit {exit_code} prints an error envelope"]
    except FakeError as err:
        return [str(err)]
    return []


# --- the call log and the scenario ---------------------------------------------------------------


def _start_of(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/stat").read_bytes().rpartition(b")")[2].split()[19].decode()
    except OSError:
        return "?"


def _stdin_kind() -> str:
    try:
        target = os.readlink("/proc/self/fd/0")
    except OSError:
        return "closed"
    if target == "/dev/null":
        return "devnull"
    try:
        mode = os.fstat(0).st_mode
    except OSError:
        return "closed"
    if stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
        return "pipe"
    if stat.S_ISCHR(mode) and os.isatty(0):
        return "tty"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


class Recorder:
    """Appends to ``<dir>/calls.jsonl`` (one JSON line per event, keyed by the call's number) and
    ``<dir>/pids`` (``pid pgid starttime``, the format of ``tests.fakes``)."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.n = self._claim()

    def _claim(self) -> int:
        with open(self.dir / "lock", "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            counter = self.dir / "count"
            n = int(counter.read_text()) if counter.exists() else 0
            counter.write_text(str(n + 1))
            return n

    def event(self, name: str, /, **data: Any) -> None:
        line = dumps({"n": self.n, "event": name, **data}) + "\n"
        with open(self.dir / "calls.jsonl", "ab") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.write(line.encode("ascii"))

    def track(self) -> None:
        pid = os.getpid()
        with open(self.dir / "pids", "a") as handle:
            handle.write(f"{pid} {os.getpgid(pid)} {_start_of(pid)}\n")


class TeeStdin:
    """The binary stdin, recording what forgectl reads of it (a body is capped, so the log shows
    whether forgectl stopped reading early)."""

    def __init__(self, recorder: Recorder, stream: Any) -> None:
        self.recorder = recorder
        self.stream = stream
        self.chunks: list[bytes] = []

    def read(self, size: int = -1) -> bytes:
        data = self.stream.read(size)
        self.chunks.append(data)
        return data

    def flush_log(self) -> None:
        if self.chunks:
            self.recorder.event(
                "stdin",
                b64=base64.b64encode(b"".join(self.chunks)).decode("ascii"),
            )


def _take_step(directory: Path) -> tuple[dict[str, Any], int | None]:
    """The scenario and the index of the next step (None when the scenario is out of steps)."""
    scenario = json.loads((directory / "scenario.json").read_text(encoding="utf-8"))
    with open(directory / "lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = directory / "steps"
        used = int(state.read_text()) if state.exists() else 0
        if used >= len(scenario.get("steps", [])):
            return scenario, None
        state.write_text(str(used + 1))
        return scenario, used


# --- the run -------------------------------------------------------------------------------------


def bind_host(host: str, hosts: Mapping[str, str]) -> str:
    backend = BUILT_IN_HOSTS.get(host) or hosts.get(host)
    if backend is None:
        raise refused(
            "unknown_host", f"host {_shown(host)} is not built in or declared in the config"
        )
    return backend


def bind_pr(pr: PrInput, hosts: Mapping[str, str]) -> tuple[str, str]:
    """(backend, normalized ``host/path#N``) of a parsed PR, as forgectl binds it."""
    backend = bind_host(pr.host, hosts)
    invalid = Fail("invalid_pr", f"not a valid {backend} pull request: {_shown(pr.raw or str(pr))}")
    expected_kind = "pull" if backend == "github" else "merge_requests"
    if pr.kind not in (None, expected_kind):
        raise invalid
    segments = pr.project.split("/")
    if backend == "github" and len(segments) != 2:
        raise invalid
    checked = segments if backend == "gitlab" else segments[:1]
    if any(seg.startswith("-") for seg in checked):
        raise invalid
    return backend, str(pr)


@dataclass
class HumanContext:
    actor: str
    is_human: bool
    interactive: bool
    yes: bool
    dry_run: bool


def build_context(args: Namespace, *, stdin_tty: bool, stderr_tty: bool) -> HumanContext:
    command = args.command
    body_file = getattr(args, "body_file", None)
    input_file = getattr(args, "file", None)
    explicit: str | None = args.actor
    if explicit is not None:
        actor, is_human = explicit, explicit.startswith("human:")
    elif stdin_tty:
        actor, is_human = "human:me", True
    else:
        actor, is_human = "auto:unknown", False
    if not is_human:
        for name in ("auto", "override_blocks", "allow_self", "override_escalation"):
            if getattr(args, name, False):
                flag = "--" + name.replace("_", "-")
                raise usage(f"{flag} is human-only: it needs --actor human:<user> or a TTY")
    if stdin_tty and (body_file in STDIN_SPELLINGS or input_file in STDIN_SPELLINGS):
        raise usage("'-' reads the body from stdin, which is a terminal; pass a file")
    interactive = stdin_tty and stderr_tty and not actor.startswith("auto:") and not args.yes
    if command in ("review", "approve") and not interactive:
        missing = [
            "--" + name.replace("_", "-")
            for name in ("expected_head", "expected_base")
            if getattr(args, name, None) is None
        ]
        if missing:
            raise usage(
                f"{command} needs {' and '.join(missing)} unless the run is interactive "
                "(stdin and stderr terminals, a human actor, no --yes, no '-' input)"
            )
    if command == "approve" and args.dedupe_key is not None and body_file is None:
        raise usage("--dedupe-key needs --body-file")
    distinct: dict[str, None] = {}
    for decision in args.decision_id or []:
        distinct[decision] = None
        if len(distinct) > MAX_DECISION_IDS:
            raise usage(f"at most {MAX_DECISION_IDS} distinct --decision-id values are allowed")
    return HumanContext(actor, is_human, interactive, args.yes, args.dry_run)


def _check_config(path: str | None) -> None:
    if path is None:
        return
    p = Path(path)
    if not p.exists():
        raise Fail(
            "config_invalid",
            f"{path}: no such file, or a symbolic link to nothing",
            details={"key": path},
        )
    if not p.is_file():
        raise FakeError(
            f"--config {path!r} is not a regular file: forgectl's refusal is not modeled"
        )


class Interrupted(BaseException):
    """SIGINT, SIGTERM or SIGHUP arrived (forgectl handles SIGHUP as SIGTERM)."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def _on_signal(signum: int, frame: Any) -> None:
    raise Interrupted(signum)


def install_signal_handlers() -> None:
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _on_signal)


def _hang(hang: dict[str, Any], rec: Recorder, ctx: Context) -> None:
    """Wait as forgectl does while a host answers slowly. A signal ends the wait as ``Interrupted``;
    a wait that runs out just returns."""
    phase = hang.get("phase", "before_write")
    if phase not in ("before_write", "after_write_attempt", "after_outcome"):
        raise FakeError(f"unknown hang phase {phase!r}")
    if phase == "after_write_attempt":
        writes = ctx.command in WRITE_COMMANDS and not ctx.dry_run and ctx.confirmed
        if not writes:
            raise FakeError(
                "after_write_attempt: this call reaches no write (a read, a dry run, no --yes)"
            )
    if phase == "after_outcome" and not (ctx.command in WRITE_COMMANDS and not ctx.dry_run):
        raise FakeError("after_outcome: this call records no outcome")
    if float(hang.get("term_delay", 0)) > 2.0:
        raise FakeError("forgectl ends its children within 2 s of a signal: term_delay <= 2")
    rec.event("hang", phase=phase)
    (rec.dir / f"ready-{rec.n}").write_text(phase)
    deadline = time.monotonic() + float(hang.get("seconds", 30))
    while time.monotonic() < deadline:
        time.sleep(0.02)


def build_call(
    argv: list[str],
    hosts: Mapping[str, str],
    stdin: Any,
    *,
    version: str = VERSION,
    stdin_tty: bool = False,
    stderr_tty: bool = False,
    check_config: bool = True,
) -> Context:
    """Everything forgectl settles before a host is asked, in its order: parse, the config file,
    the closed host list, the human-only and required flags, the input. Raises ``Fail`` (with the
    parsed ``namespace`` when parsing got that far) or ``FakeError`` for what is not modeled."""
    if any(a in ("-h", "--help") for a in argv):
        raise FakeError("--help output is not modeled")
    args = parse_argv(argv, version)
    try:
        command: str = args.command
        if command in UNMODELED:
            raise FakeError(f"forgectl {command} is not modeled by the fake")
        if not args.json:
            raise FakeError("human output is not modeled: pr-comment always passes --json")
        if check_config:
            _check_config(getattr(args, "config", None))
        backend: str | None = None
        pr_id: str | None = None
        if command == "whoami":
            if args.host is not None:
                bind_host(args.host, hosts)
        else:
            backend, pr_id = bind_pr(args.pr, hosts)
        prep: Prepared | None = None
        if command in WRITE_COMMANDS:
            human = build_context(args, stdin_tty=stdin_tty, stderr_tty=stderr_tty)
            if human.interactive:
                raise FakeError("an interactive run (a dialog on a terminal) is not modeled")
            assert backend is not None
            prep = _prepare(args, backend, stdin)
        return Context(
            args=args,
            argv=argv,
            command=command,
            backend=backend,
            pr=pr_id,
            prep=prep,
            hosts=dict(hosts),
            version=version,
            confirmed=bool(getattr(args, "yes", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
        )
    except Fail as err:
        err.namespace = args
        raise


def run(argv: list[str], directory: Path, rec: Recorder, stdin_raw: Any) -> int:
    json_mode = wants_json(argv)
    tee = TeeStdin(rec, stdin_raw)
    try:
        try:
            scenario = json.loads((directory / "scenario.json").read_text(encoding="utf-8"))
            ctx = build_call(
                argv,
                scenario.get("hosts", {}),
                tee,
                version=scenario.get("version", VERSION),
                stdin_tty=_stdin_kind() == "tty",
                stderr_tty=os.isatty(2),
            )
            return _answer(ctx, directory, rec, json_mode)
        except SystemExit as stop:  # argparse's --version
            code = stop.code if isinstance(stop.code, int) else 0
            rec.event("end", exit=code, kind="version" if code == 0 else "exit")
            return code
        except Fail as err:
            return _fail(err, rec, json_mode)
    finally:
        tee.flush_log()


def _prepare(args: Namespace, backend: str, stdin: Any) -> Prepared:
    command = args.command
    key = getattr(args, "dedupe_key", None)
    if command == "review":
        return prepare_review(args.file, key, backend, stdin)
    if command == "approve":
        if args.body_file is None:
            return Prepared(None, None, 0, None, 0, 0, 0)
        limit = GITLAB_LIMIT if backend == "gitlab" else GITHUB_COMMENT_LIMIT
        return prepare_body(args.body_file, limit=limit, key=key, stdin=stdin)
    if command in ("resolve", "unresolve"):
        return Prepared(None, None, 0, None, 0, 0, 0)
    to = args.to if command == "reply" else None
    return prepare_body(args.body_file, limit=limit_for(backend, to), key=key, stdin=stdin)


def _early_context(args: Namespace) -> Context:
    """A context for a refusal that came before any host or input was read."""
    return Context(
        args=args,
        argv=[],
        command=args.command,
        backend=None,
        pr=None,
        prep=None,
        hosts={},
        version=VERSION,
        confirmed=False,
        dry_run=True,
    )


def _fail(err: Fail, rec: Recorder, json_mode: bool) -> int:
    """Print a failure found before the host was asked (the command line, the config, the closed
    host list, the input)."""
    args = err.namespace
    printable: dict[str, Any] | None = None
    if (
        err.exit_code == 20
        and args is not None
        and getattr(args, "dry_run", False)
        and args.command in WRITE_COMMANDS
    ):
        printable = _would_refuse_result(_early_context(args), err)
        problems = schema_errors(document_schema(args.command), printable)
        if problems:
            raise FakeError(
                "would_refuse result violates the pinned schema: " + "; ".join(problems)
            )
    if printable is not None:
        write_line(sys.stdout, dumps(printable))
    elif json_mode:
        write_line(sys.stdout, dumps(err.envelope()))
    write_line(sys.stderr, error_text(err))
    kind = "usage" if err.exit_code == 2 else "refusal"
    rec.event("end", exit=err.exit_code, kind=kind, code=err.code, reason=err.reason)
    return err.exit_code


def _answer(ctx: Context, directory: Path, rec: Recorder, json_mode: bool) -> int:
    """Everything after the command line and the input: the scripted answer of the host."""
    command = ctx.command
    scenario, index = _take_step(directory)
    if index is None:
        raise FakeError(f"no scripted step left for forgectl {command} (the scenario ran out)")
    step = scenario["steps"][index]
    rec.event("step", index=index, label=step.get("label"))
    if step.get("command") != command:
        raise FakeError(f"step {index} is for {step.get('command')!r}, the call is {command!r}")
    outcome = step["outcome"]
    kind = outcome["kind"]
    err: Fail | None = None
    document: dict[str, Any] | None = None
    if kind == "error":
        spec = outcome["error"]
        extra = {
            k: spec[k]
            for k in ("retry_after", "hook", "hook_stderr", "decision_ids", "escalated_hooks")
            if k in spec
        }
        err = Fail(
            spec["code"],
            spec["message"],
            reason=spec.get("reason"),
            details=spec.get("details"),
            extra=extra,
            plan=outcome.get("plan"),
            fields=outcome.get("fields"),
        )
        check_error(ctx, err)
    elif kind in ("ok", "partial"):
        document = check_document(ctx, outcome["document"], partial=kind == "partial")
        if kind == "partial" and command != "whoami":
            raise FakeError("partial results are for whoami")
    else:
        raise FakeError(f"unknown outcome kind {kind!r}")
    stderr_lines = outcome.get("stderr_lines", [])
    for line in stderr_lines:
        if not line.startswith("forgectl: warning: "):
            raise FakeError("a stderr line is a `forgectl: warning:` line")
        if unsanitized_char(line):
            raise FakeError("a stderr line is sanitized")

    # confirmation: a write with no --yes and no dry run stops at exit 13, unless it ended earlier
    pre_confirmation = _is_pre_confirmation(ctx, err, document)
    if command in WRITE_COMMANDS and not ctx.dry_run and not ctx.confirmed and not pre_confirmation:
        rec.event("confirmation_blocked", scripted=kind)
        err = Fail(
            "confirmation_required",
            "this write needs confirmation: run it on a terminal as a human, or pass --yes",
        )
        document = None
        hang = None
    else:
        hang = outcome.get("hang")
    if hang is not None:
        phase = hang.get("phase", "before_write")
        try:
            _hang(hang, rec, ctx)
        except Interrupted as sig:
            delay = float(hang.get("term_delay", 0))
            if delay:
                time.sleep(delay)
            if phase == "before_write":
                rec.event("end", exit=128 + sig.signum, kind="signal", signal=sig.signum)
                return 128 + sig.signum
            if phase == "after_write_attempt":
                # forgectl: a signal after the write was attempted and before its outcome is
                # recorded is exit 12 outcome_unknown, never 128+N
                err = Fail(
                    "outcome_unknown",
                    "a signal arrived after the write was attempted; it may have landed",
                )
                document = None
            # after_outcome: the outcome is already recorded; it is printed and is the exit
    elif "delay" in outcome:
        time.sleep(float(outcome["delay"]))
    for line in stderr_lines:
        write_line(sys.stderr, line)
    if err is not None:
        if err.exit_code == 20 and ctx.dry_run and command in WRITE_COMMANDS:
            doc = _would_refuse_result(ctx, err)
            problems = schema_errors(document_schema(command), doc)
            if problems:
                raise FakeError(
                    "would_refuse result violates the pinned schema: " + "; ".join(problems)
                )
            write_line(sys.stdout, dumps(doc))
        else:
            write_line(sys.stdout, dumps(err.envelope()))
        write_line(sys.stderr, error_text(err))
        rec.event("end", exit=err.exit_code, kind="error", code=err.code, reason=err.reason)
        return err.exit_code
    assert document is not None
    write_line(sys.stdout, dumps(document))
    if kind == "partial":
        message = outcome.get("message", "partial results: a host failed")
        write_line(sys.stderr, f"forgectl: error: partial_results: {message}")
        rec.event("end", exit=12, kind="partial")
        return 12
    rec.event("end", exit=0, kind="ok")
    return 0


def _is_pre_confirmation(ctx: Context, err: Fail | None, doc: dict[str, Any] | None) -> bool:
    """Whether the scripted outcome happens before forgectl's confirmation step: every refusal
    and read failure does; a success does only when there is nothing to write (a dedupe hit with
    no state step left, a thread already in the requested state)."""
    if ctx.command not in WRITE_COMMANDS:
        return True
    if err is not None:
        return err.code not in POST_CONFIRMATION_CODES
    assert doc is not None
    if ctx.command in ("resolve", "unresolve"):
        return not doc["changed"]
    return bool(doc["already_posted"]) and not doc["follow_up_done"]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    directory = Path(os.environ[DIR_ENV])
    rec = Recorder(directory)
    rec.track()
    rec.event(
        "start",
        argv=argv,
        cwd=os.getcwd(),
        stdin_kind=_stdin_kind(),
        env={
            k: v
            for k, v in os.environ.items()
            if k in ("HOME", "XDG_CONFIG_HOME", "PATH", "TMPDIR") or k.startswith("PR_COMMENT_")
        },
        pid=os.getpid(),
    )
    # the handlers forgectl installs: a signal before a write attempt exits 128+N, no envelope
    install_signal_handlers()
    stdin_raw = sys.stdin.buffer if sys.stdin is not None else _Empty()
    try:
        code = run(argv, directory, rec, stdin_raw)
    except Interrupted as sig:
        rec.event("end", exit=128 + sig.signum, kind="signal", signal=sig.signum)
        return 128 + sig.signum
    except FakeError as err:
        rec.event("fake_error", message=str(err))
        write_line(sys.stderr, f"FAKE-FORGECTL ERROR: {err}")
        rec.event("end", exit=FAKE_ERROR_EXIT, kind="fake_error")
        return FAKE_ERROR_EXIT
    return code


class _Empty:
    def read(self, size: int = -1) -> bytes:
        return b""


if __name__ == "__main__":
    sys.exit(main())
