"""Command-line entry point for pr-comment: the frozen command and flag surface, and dispatch.

The surface is exactly the Commands and Global flags sections of docs/interface.md. Parsing
errors exit 2 ``usage``; a command whose handler is not written yet exits 1 ``internal``.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import sys
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn, cast

from . import __version__
from .actor import ActorError, resolve_run, stream_is_tty

PROG = "pr-comment"
DESCRIPTION = "Posts reviews, replies and comments to PRs, safely and with attribution."

EXIT_INTERNAL = 1
EXIT_USAGE = 2

MAX_DECISION_IDS = 32
MAX_THREADS = 100
MAX_USAGE_MESSAGE = 200
POSTING_COMMANDS = frozenset({"review", "comment", "reply", "approve"})

_ACTOR_RE = re.compile(r"^(human|auto):[A-Za-z0-9._@-]{1,64}$")
_DECISION_ID_RE = re.compile(r"^[A-Za-z0-9._:@/-]{1,128}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HOOK_NAMES = ("pre-post", "post-post")
_ESCALATION_SCOPES = ("pr-comment", "forgectl")


class UsageError(Exception):
    """A command line that does not match the frozen surface (exit 2, code ``usage``)."""

    def __init__(self, message: str, usage: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.usage = usage


@dataclass(frozen=True)
class Outcome:
    """What a handler returns: the exit code and the ``--json`` document (None prints nothing)."""

    exit_code: int
    document: dict[str, Any] | None = None


Handler = Callable[[argparse.Namespace], Outcome]


def error_envelope(code: str, message: str) -> dict[str, Any]:
    """A minimal error envelope (schemas/cli/error.json: only ``code`` and ``message`` required)."""
    # TODO(#4): replace with the result objects and the full envelope in errors.py and output.py.
    return {"error": {"code": code, "message": _sanitize(message)}}


def _sanitize(text: str) -> str:
    """Replace control, format and surrogate characters (Cc, Cf, Cs) with ``<U+XXXX>``."""
    return "".join(
        f"<U+{ord(char):04X}>" if unicodedata.category(char) in ("Cc", "Cf", "Cs") else char
        for char in text
    )


def _truncate(text: str, limit: int = MAX_USAGE_MESSAGE) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _version() -> str:
    try:
        return importlib.metadata.version("pr-comment")
    except importlib.metadata.PackageNotFoundError:
        return __version__


# --- value types -----------------------------------------------------------------------------


def _matching(pattern: re.Pattern[str], what: str) -> Callable[[str], str]:
    def check(value: str) -> str:
        if not pattern.fullmatch(value):
            raise argparse.ArgumentTypeError(f"invalid {what}")
        return value

    return check


def _escalation_scope(value: str) -> str:
    if value not in _ESCALATION_SCOPES:
        raise argparse.ArgumentTypeError(
            f"invalid scope (expected one of: {', '.join(_ESCALATION_SCOPES)})"
        )
    return value


def _hook(value: str) -> str:
    name, sep, cmd = value.partition("=")
    if not sep or name not in _HOOK_NAMES or not cmd:
        raise argparse.ArgumentTypeError(
            f"expected <name>=<cmd> with name one of: {', '.join(_HOOK_NAMES)}"
        )
    return value


# --- parser ----------------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    """Raises ``UsageError`` instead of printing and exiting, and takes no abbreviations."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(**kwargs)

    def error(self, message: str) -> NoReturn:
        raise UsageError(message, self.format_usage())


class AppendAction(argparse.Action):
    """Appends in place (linear time) and fails as soon as the count exceeds ``max_count``."""

    def __init__(
        self,
        option_strings: Sequence[str],
        dest: str,
        max_count: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(option_strings, dest, **kwargs)
        self.max_count = max_count

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        items = getattr(namespace, self.dest, None)
        if not isinstance(items, list) or items is self.default:
            items = []
            setattr(namespace, self.dest, items)
        items.append(values)
        if self.max_count is not None and len(items) > self.max_count:
            parser.error(f"at most {self.max_count} {option_string} values")


class _StoreOnce(argparse.Action):
    """Stores a value; a second, different value is a usage error."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        if hasattr(namespace, self.dest) and getattr(namespace, self.dest) != values:
            parser.error(f"{option_string} given more than once with different values")
        setattr(namespace, self.dest, values)


@dataclass(frozen=True)
class _GlobalFlag:
    dest: str
    flags: tuple[str, ...]
    kwargs: dict[str, Any]
    default: Any

    @property
    def is_list(self) -> bool:
        return self.kwargs.get("action") is AppendAction

    @property
    def is_bool(self) -> bool:
        return self.kwargs.get("action") == "store_true"

    @property
    def takes_value(self) -> bool:
        return not self.is_bool


_GLOBAL_FLAGS = (
    _GlobalFlag(
        "json", ("--json",), {"action": "store_true", "help": "machine-readable JSON output"}, False
    ),
    _GlobalFlag(
        "config",
        ("--config",),
        {"action": _StoreOnce, "metavar": "PATH", "help": "config file"},
        None,
    ),
    _GlobalFlag(
        "dry_run",
        ("--dry-run",),
        {"action": "store_true", "help": "build the plan, send nothing"},
        False,
    ),
    _GlobalFlag(
        "yes", ("--yes",), {"action": "store_true", "help": "skip the confirmation dialog"}, False
    ),
    _GlobalFlag(
        "expect_plan",
        ("--expect-plan",),
        {
            "action": _StoreOnce,
            "metavar": "SHA256",
            "type": _matching(_SHA256_RE, "plan hash (64 lowercase hex digits)"),
            "help": "refuse unless the final plan hash matches (posting commands only)",
        },
        None,
    ),
    _GlobalFlag(
        "override_escalation",
        ("--override-escalation",),
        {
            "action": AppendAction,
            "metavar": "SCOPE",
            "type": _escalation_scope,
            "help": "human-only: allow escalations of this scope (pr-comment or forgectl)",
        },
        [],
    ),
    _GlobalFlag(
        "actor",
        ("--actor",),
        {
            "action": _StoreOnce,
            "metavar": "ACTOR",
            "type": _matching(_ACTOR_RE, "actor (human:<name> or auto:<name>)"),
            "help": "who acts",
        },
        None,
    ),
    _GlobalFlag(
        "decision_id",
        ("--decision-id",),
        {
            "action": AppendAction,
            "max_count": MAX_DECISION_IDS,
            "metavar": "ID",
            "type": _matching(_DECISION_ID_RE, "decision id"),
            "help": f"repeatable, at most {MAX_DECISION_IDS}",
        },
        [],
    ),
    _GlobalFlag(
        "hook",
        ("--hook",),
        {
            "action": AppendAction,
            "metavar": "NAME=CMD",
            "type": _hook,
            "help": "per-invocation hook, run after configured hooks",
        },
        [],
    ),
    _GlobalFlag(
        "verbose", ("--verbose",), {"action": "store_true", "help": "more human output"}, False
    ),
    _GlobalFlag(
        "quiet", ("--quiet",), {"action": "store_true", "help": "less human output"}, False
    ),
)


# Argparse namespaces of the three parser levels never share a destination: before the command
# (`top_`), between `draft` and its subcommand (`draft_`) and after the command (no prefix).
# SUPPRESS keeps an absent flag out of the namespace, so a level never overwrites another;
# `_TopParser.parse_args` merges the levels and applies the defaults.
_LEVELS = ("top_", "draft_", "")


def _add_global_flags(parser: argparse.ArgumentParser, *, prefix: str = "") -> None:
    for spec in _GLOBAL_FLAGS:
        parser.add_argument(
            *spec.flags,
            dest=prefix + spec.dest,
            default=argparse.SUPPRESS,
            **spec.kwargs,
        )
    parser.add_argument("--version", action="version", version=f"{PROG} {_version()}")


class _TopParser(_Parser):
    def parse_args(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, args: list[str] | None = None, namespace: argparse.Namespace | None = None
    ) -> argparse.Namespace:
        parsed = cast(argparse.Namespace, super().parse_args(args, namespace))
        for spec in _GLOBAL_FLAGS:
            given: list[Any] = []
            for prefix in _LEVELS:
                name = prefix + spec.dest
                if hasattr(parsed, name):
                    given.append(getattr(parsed, name))
                    delattr(parsed, name)
            if spec.is_list:
                value: Any = [item for items in given for item in items]
            elif spec.is_bool:
                value = any(given)
            elif len(set(given)) > 1:
                self.error(f"{spec.flags[0]} given more than once with different values")
            else:
                value = given[0] if given else spec.default
            setattr(parsed, spec.dest, value)
        return parsed


def _leaf(
    subparsers: Any, name: str, key: str, help_text: str, *, positional: tuple[str, ...] = ("pr",)
) -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = subparsers.add_parser(name, help=help_text)
    parser.set_defaults(command=key)
    for pos in positional:
        parser.add_argument(pos)
    _add_global_flags(parser)
    return parser


def _add_common_posting_flags(parser: argparse.ArgumentParser, *, base: bool = False) -> None:
    # TODO(#6): validate the forwarded value grammars of --expected-head, --expected-base and
    # --dedupe-key (and of the PR argument, including `invalid_pr` and a leading dash) here.
    parser.add_argument("--expected-head", metavar="SHA")
    if base:
        parser.add_argument("--expected-base", metavar="REF")
    parser.add_argument("--dedupe-key", metavar="KEY")
    # TODO(#16): validate and apply the mention allowlist. The human-only, interactive-only rule
    # for --allow-mention is enforced in actor.resolve_run.
    parser.add_argument("--allow-mention", action=AppendAction, default=[], metavar="NAME")


def build_parser() -> argparse.ArgumentParser:
    parser = _TopParser(prog=PROG, description=DESCRIPTION)
    _add_global_flags(parser, prefix="top_")
    sub = parser.add_subparsers(
        dest=argparse.SUPPRESS, metavar="<command>", required=True, parser_class=_Parser
    )

    review = _leaf(sub, "review", "review", "post one review from a draft")
    review.add_argument("--draft-file", required=True, metavar="PATH|-")
    review.add_argument("--approve", action="store_true")
    # TODO(#6): validate the --reanchor-to value grammar.
    review.add_argument("--reanchor-to", metavar="SHA")
    _add_common_posting_flags(review)

    comment = _leaf(sub, "comment", "comment", "post a general PR comment")
    comment.add_argument("--body-file", required=True, metavar="PATH|-")
    _add_common_posting_flags(comment)

    reply = _leaf(sub, "reply", "reply", "reply to a comment, thread or review")
    # TODO(#6): validate the --to id grammar.
    reply.add_argument("--to", required=True, metavar="ID")
    reply.add_argument("--body-file", required=True, metavar="PATH|-")
    _add_common_posting_flags(reply)

    approve = _leaf(sub, "approve", "approve", "approve a PR at a pinned head and base")
    approve.add_argument("--body-file", metavar="PATH|-")
    _add_common_posting_flags(approve, base=True)

    for name, text in (("resolve", "resolve review threads"), ("unresolve", "reopen threads")):
        thread = _leaf(sub, name, name, text)
        # TODO(#6): validate the --thread id grammar (gh:rt: or gl:d:).
        thread.add_argument(
            "--thread",
            action=AppendAction,
            max_count=MAX_THREADS,
            default=[],
            required=True,
            metavar="ID",
        )

    draft = sub.add_parser("draft", help="draft tools")
    _add_global_flags(draft, prefix="draft_")
    draft_sub = draft.add_subparsers(
        dest=argparse.SUPPRESS, metavar="<command>", required=True, parser_class=_Parser
    )
    check = _leaf(draft_sub, "check", "draft check", "validate a draft offline", positional=())
    check.add_argument("path", metavar="PATH|-")
    new = _leaf(draft_sub, "new", "draft new", "write a new draft for a PR")
    new.add_argument("--event", choices=("comment", "request_changes"))
    new.add_argument("--from", dest="from_draft", metavar="PATH|-")

    export = _leaf(sub, "export", "export", "print audit or log records", positional=())
    # TODO(#38): validate --since (RFC 3339 or a duration such as 7d).
    export.add_argument("--since", metavar="TIME")
    export.add_argument("--log", action="store_true")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    """Rules argparse cannot express. Raises ``UsageError``."""
    try:
        args.run = resolve_run(
            args, stdin_tty=stream_is_tty(sys.stdin), stderr_tty=stream_is_tty(sys.stderr)
        )
    except ActorError as exc:
        raise UsageError(str(exc)) from exc
    # TODO(#6): the grammars enforced at parse time (--actor, --decision-id, --expect-plan,
    # --hook, --override-escalation and the thread cap) move into the shared validator.
    if args.expect_plan is not None and args.command not in POSTING_COMMANDS:
        raise UsageError(f"--expect-plan is only valid with {', '.join(sorted(POSTING_COMMANDS))}")
    # The parse-time caps count per parser level; these count the merged total.
    if len(args.decision_id) > MAX_DECISION_IDS:
        raise UsageError(f"at most {MAX_DECISION_IDS} --decision-id values")
    if args.command in ("resolve", "unresolve") and len(args.thread) > MAX_THREADS:
        raise UsageError(f"at most {MAX_THREADS} --thread values")


# --- dispatch --------------------------------------------------------------------------------


def _not_implemented(args: argparse.Namespace) -> Outcome:
    return Outcome(
        EXIT_INTERNAL, error_envelope("internal", f"{args.command} is not implemented yet")
    )


# One handler per command path. Later issues replace the entries with real handlers.
# TODO(#29): review; TODO(#30): comment and reply; TODO(#31): approve and review --approve;
# TODO(#35): resolve and unresolve; TODO(#15): draft check; TODO(#37): draft new;
# TODO(#38): export.
HANDLERS: dict[str, Handler] = {
    key: _not_implemented
    for key in (
        "review",
        "comment",
        "reply",
        "approve",
        "resolve",
        "unresolve",
        "draft check",
        "draft new",
        "export",
    )
}

_VALUE_FLAGS = frozenset(flag for spec in _GLOBAL_FLAGS if spec.takes_value for flag in spec.flags)


def _wants_json(argv: list[str]) -> bool:
    """True when a token before ``--`` asks for JSON (``--json`` or ``--json=<anything>``)."""
    head = argv[: argv.index("--")] if "--" in argv else argv
    return any(token == "--json" or token.startswith("--json=") for token in head)


MAX_ARGV_TOKENS = 1024


def _dash_dash_before_command(argv: list[str]) -> bool:
    """True when ``--`` appears before the command token (or between ``draft`` and its subcommand).

    argparse treats that differently across Python versions, so it is refused up front.
    """
    skip = False
    seen_draft = False
    for token in argv:
        if token == "--":
            return True
        if skip:
            skip = False
        elif token in _VALUE_FLAGS:
            skip = True
        elif token.startswith("-"):
            continue
        elif token == "draft" and not seen_draft:
            seen_draft = True
        else:
            return False
    return False


def _render(outcome: Outcome, *, as_json: bool) -> tuple[str, str]:
    """The stdout and stderr text of an outcome, built before anything is written."""
    if as_json:
        if outcome.document is None:
            return "", ""
        return json.dumps(outcome.document, ensure_ascii=True) + "\n", ""
    if outcome.exit_code != 0 and outcome.document is not None:
        error = outcome.document.get("error")
        if isinstance(error, dict):
            message = _sanitize(str(error.get("message", error.get("code"))))
            return "", f"{PROG}: {message}\n"
    return "", ""


def _check_outcome(result: object) -> Outcome:
    if not isinstance(result, Outcome):
        raise TypeError("handler did not return an Outcome")
    if type(result.exit_code) is not int:
        raise TypeError("handler exit code is not an integer")
    if result.document is not None and not isinstance(result.document, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError("handler document is not a dict")
    return result


def _write(stream: Any, text: str) -> None:
    # TODO(#4): tolerate None/closed stdout/stderr in _write
    """Write text that cannot fail on the stream's encoding."""
    if text:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        stream.write(text.encode(encoding, "backslashreplace").decode(encoding, "replace"))
        stream.flush()


def _fail_usage(argv: list[str], message: str, usage: str) -> int:
    message = _truncate(_sanitize(message))
    outcome = Outcome(EXIT_USAGE, error_envelope("usage", message))
    stdout, stderr = _render(outcome, as_json=_wants_json(argv))
    _write(sys.stderr, usage)
    _write(sys.stderr, stderr)
    _write(sys.stdout, stdout)
    return outcome.exit_code


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    if len(argv) > MAX_ARGV_TOKENS:  # argparse is quadratic in repeated flags
        return _fail_usage(
            argv, f"too many arguments (at most {MAX_ARGV_TOKENS})", parser.format_usage()
        )
    if _dash_dash_before_command(argv):
        return _fail_usage(argv, "'--' is not allowed before the command", parser.format_usage())
    try:
        args = parser.parse_args(argv)
        validate_args(args)
    except UsageError as exc:
        return _fail_usage(argv, exc.message, exc.usage)
    try:
        outcome = _check_outcome(HANDLERS[args.command](args))
        stdout, stderr = _render(outcome, as_json=args.json)
    except Exception as exc:  # a handler bug must still honor the exit-code and envelope contract
        outcome = Outcome(
            EXIT_INTERNAL, error_envelope("internal", f"unexpected {type(exc).__name__}")
        )
        stdout, stderr = _render(outcome, as_json=args.json)
    _write(sys.stderr, stderr)
    _write(sys.stdout, stdout)
    return outcome.exit_code
