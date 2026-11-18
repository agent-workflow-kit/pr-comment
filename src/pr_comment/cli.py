"""Command-line entry point for pr-comment: the frozen command and flag surface, and dispatch.

The surface is exactly the Commands and Global flags sections of docs/interface.md. Parsing
errors exit 2 ``usage``; a command whose handler is not written yet exits 1 ``internal``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NoReturn, cast

from . import __version__

PROG = "pr-comment"
DESCRIPTION = "Posts reviews, replies and comments to PRs, safely and with attribution."

EXIT_INTERNAL = 1
EXIT_USAGE = 2

MAX_DECISION_IDS = 32
MAX_THREADS = 100
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
    return {"error": {"code": code, "message": message}}


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


@dataclass(frozen=True)
class _GlobalFlag:
    dest: str
    flags: tuple[str, ...]
    kwargs: dict[str, Any]
    default: Any

    @property
    def is_list(self) -> bool:
        return self.kwargs.get("action") == "append"


_GLOBAL_FLAGS = (
    _GlobalFlag(
        "json", ("--json",), {"action": "store_true", "help": "machine-readable JSON output"}, False
    ),
    _GlobalFlag("config", ("--config",), {"metavar": "PATH", "help": "config file"}, None),
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
            "action": "append",
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
            "action": "append",
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
            "action": "append",
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


def _add_global_flags(parser: argparse.ArgumentParser, *, prefix: str = "") -> None:
    # SUPPRESS keeps an absent flag out of the namespace, so a subcommand's namespace never
    # overwrites a value given before the command; `_Parser.parse_args` merges both and applies
    # the defaults.
    for spec in _GLOBAL_FLAGS:
        parser.add_argument(
            *spec.flags,
            dest=prefix + spec.dest,
            default=argparse.SUPPRESS,
            **spec.kwargs,
        )


class _TopParser(_Parser):
    def parse_args(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, args: list[str] | None = None, namespace: argparse.Namespace | None = None
    ) -> argparse.Namespace:
        parsed = cast(argparse.Namespace, super().parse_args(args, namespace))
        for spec in _GLOBAL_FLAGS:
            before = getattr(parsed, "top_" + spec.dest, None)
            if hasattr(parsed, "top_" + spec.dest):
                delattr(parsed, "top_" + spec.dest)
            after = getattr(parsed, spec.dest, None)
            if spec.is_list:
                value: Any = [*(before or []), *(after or [])]
            elif after is not None:
                value = after
            elif before is not None:
                value = before
            else:
                value = spec.default
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
    parser.add_argument("--expected-head", metavar="SHA")
    if base:
        parser.add_argument("--expected-base", metavar="REF")
    parser.add_argument("--dedupe-key", metavar="KEY")
    parser.add_argument("--allow-mention", action="append", default=[], metavar="NAME")


def build_parser() -> argparse.ArgumentParser:
    parser = _TopParser(prog=PROG, description=DESCRIPTION)
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    _add_global_flags(parser, prefix="top_")
    sub = parser.add_subparsers(
        dest=argparse.SUPPRESS, metavar="<command>", required=True, parser_class=_Parser
    )

    review = _leaf(sub, "review", "review", "post one review from a draft")
    review.add_argument("--draft-file", required=True, metavar="PATH|-")
    review.add_argument("--approve", action="store_true")
    review.add_argument("--reanchor-to", metavar="SHA")
    _add_common_posting_flags(review)

    comment = _leaf(sub, "comment", "comment", "post a general PR comment")
    comment.add_argument("--body-file", required=True, metavar="PATH|-")
    _add_common_posting_flags(comment)

    reply = _leaf(sub, "reply", "reply", "reply to a comment, thread or review")
    reply.add_argument("--to", required=True, metavar="ID")
    reply.add_argument("--body-file", required=True, metavar="PATH|-")
    _add_common_posting_flags(reply)

    approve = _leaf(sub, "approve", "approve", "approve a PR at a pinned head and base")
    approve.add_argument("--body-file", metavar="PATH|-")
    _add_common_posting_flags(approve, base=True)

    for name, text in (("resolve", "resolve review threads"), ("unresolve", "reopen threads")):
        thread = _leaf(sub, name, name, text)
        thread.add_argument("--thread", action="append", required=True, metavar="ID")

    draft = sub.add_parser("draft", help="draft tools")
    draft_sub = draft.add_subparsers(
        dest=argparse.SUPPRESS, metavar="<command>", required=True, parser_class=_Parser
    )
    check = _leaf(draft_sub, "check", "draft check", "validate a draft offline", positional=())
    check.add_argument("path", metavar="PATH|-")
    new = _leaf(draft_sub, "new", "draft new", "write a new draft for a PR")
    new.add_argument("--event", choices=("comment", "request_changes"))
    new.add_argument("--from", dest="from_draft", metavar="PATH|-")

    export = _leaf(sub, "export", "export", "print audit or log records", positional=())
    export.add_argument("--since", metavar="TIME")
    export.add_argument("--log", action="store_true")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    """Rules argparse cannot express. Raises ``UsageError``."""
    if args.expect_plan is not None and args.command not in POSTING_COMMANDS:
        raise UsageError(f"--expect-plan is only valid with {', '.join(sorted(POSTING_COMMANDS))}")
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
# TODO(#5): review, comment, reply and approve (posting); TODO(#6): resolve and unresolve;
# TODO(#7): draft check, draft new and export.
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


def _wants_json(argv: list[str]) -> bool:
    return "--json" in argv[: argv.index("--")] if "--" in argv else "--json" in argv


def _emit(outcome: Outcome, *, as_json: bool) -> None:
    if as_json:
        if outcome.document is not None:
            sys.stdout.write(json.dumps(outcome.document, ensure_ascii=False) + "\n")
    elif outcome.exit_code != 0 and outcome.document is not None:
        error = outcome.document.get("error")
        if isinstance(error, dict):
            print(f"{PROG}: {error.get('message', error.get('code'))}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        validate_args(args)
    except UsageError as exc:
        outcome = Outcome(EXIT_USAGE, error_envelope("usage", exc.message))
        if exc.usage:
            sys.stderr.write(exc.usage)
        _emit(outcome, as_json=_wants_json(argv))
        return outcome.exit_code
    try:
        outcome = HANDLERS[args.command](args)
    except Exception as exc:  # a handler bug must still honor the exit-code and envelope contract
        outcome = Outcome(
            EXIT_INTERNAL, error_envelope("internal", f"unexpected {type(exc).__name__}")
        )
    _emit(outcome, as_json=args.json)
    return outcome.exit_code
