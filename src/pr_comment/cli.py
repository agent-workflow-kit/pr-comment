"""Command-line entry point for pr-comment: the frozen command and flag surface, and dispatch.

The surface is exactly the Commands and Global flags sections of docs/interface.md. Parsing
errors exit 2 ``usage``; a command whose handler is not written yet exits 1 ``internal``.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import logging
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn, cast

from . import __version__, grammars
from .actor import ActorError, resolve_run, stream_is_tty, with_me_name
from .config import ConfigError, load_config
from .errors import sanitize
from .files import StateUnsafeError, open_state_dirs
from .grammars import MAX_DECISION_IDS, MAX_THREADS
from .jsonlog import LogWriter, write_config_warnings
from .output import LOG, PROG, Outcome, check_outcome, error_outcome, internal_outcome, render

DESCRIPTION = "Posts reviews, replies and comments to PRs, safely and with attribution."

MAX_USAGE_MESSAGE = 200
POSTING_COMMANDS = frozenset({"review", "comment", "reply", "approve"})


class UsageError(Exception):
    """A command line that does not match the frozen surface (exit 2, code ``usage``).

    ``code`` is ``invalid_pr`` for a PR argument that fails its check; both exit 2.
    """

    def __init__(self, message: str, usage: str = "", code: str = "usage") -> None:
        super().__init__(message)
        self.message = message
        self.usage = usage
        self.code = code


Handler = Callable[[argparse.Namespace], Outcome]


def _truncate(text: str, limit: int = MAX_USAGE_MESSAGE) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _version() -> str:
    try:
        return importlib.metadata.version("pr-comment")
    except importlib.metadata.PackageNotFoundError:
        return __version__


# --- value types -----------------------------------------------------------------------------


def _checked(check: Callable[[str], str]) -> Callable[[str], str]:
    """An argparse ``type`` that runs a shared grammar (grammars.py); argparse names the flag."""

    def convert(value: str) -> str:
        try:
            return check(value)
        except grammars.GrammarError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None

    return convert


# --- parser ----------------------------------------------------------------------------------


def _fixed_text(message: str) -> str:
    """Drop the caller's value from an argparse message (argparse echoes unknown arguments,
    invalid choices and explicit values given to a flag that takes none).

    What remains names only parts of the parser's own definition.
    """
    for marker in ("invalid choice:", "ignored explicit argument"):
        if marker in message:
            return message[: message.index(marker)] + marker.removesuffix(":")
    if message.startswith("unrecognized arguments:"):
        return "unrecognized arguments"
    return message


class _Parser(argparse.ArgumentParser):
    """Raises ``UsageError`` instead of printing and exiting, and takes no abbreviations."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(**kwargs)

    def error(self, message: str) -> NoReturn:
        raise UsageError(_fixed_text(message), self.format_usage())


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
        current = getattr(namespace, self.dest, None)
        if current is not None and current != values:
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
            "type": _checked(grammars.check_plan_hash),
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
            "type": _checked(grammars.check_escalation_scope),
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
            "type": _checked(grammars.check_actor),
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
            "type": _checked(grammars.check_decision_id),
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
            "type": _checked(grammars.check_hook),
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
    parser.add_argument(
        "--expected-head", action=_StoreOnce, metavar="SHA", type=_checked(grammars.check_sha)
    )
    if base:
        parser.add_argument(
            "--expected-base",
            action=_StoreOnce,
            metavar="REF",
            type=_checked(grammars.check_branch),
        )
    parser.add_argument(
        "--dedupe-key", action=_StoreOnce, metavar="KEY", type=_checked(grammars.check_dedupe_key)
    )
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
    review.add_argument("--draft-file", action=_StoreOnce, required=True, metavar="PATH|-")
    review.add_argument("--approve", action="store_true")
    review.add_argument(
        "--reanchor-to", action=_StoreOnce, metavar="SHA", type=_checked(grammars.check_sha)
    )
    _add_common_posting_flags(review)

    comment = _leaf(sub, "comment", "comment", "post a general PR comment")
    comment.add_argument("--body-file", action=_StoreOnce, required=True, metavar="PATH|-")
    _add_common_posting_flags(comment)

    reply = _leaf(sub, "reply", "reply", "reply to a comment, thread or review")
    reply.add_argument(
        "--to",
        action=_StoreOnce,
        required=True,
        metavar="ID",
        type=_checked(grammars.check_reply_target),
    )
    reply.add_argument("--body-file", action=_StoreOnce, required=True, metavar="PATH|-")
    _add_common_posting_flags(reply)

    approve = _leaf(sub, "approve", "approve", "approve a PR at a pinned head and base")
    approve.add_argument("--body-file", action=_StoreOnce, metavar="PATH|-")
    _add_common_posting_flags(approve, base=True)

    for name, text in (("resolve", "resolve review threads"), ("unresolve", "reopen threads")):
        thread = _leaf(sub, name, name, text)
        thread.add_argument(
            "--thread",
            action=AppendAction,
            max_count=MAX_THREADS,
            default=[],
            required=True,
            metavar="ID",
            type=_checked(grammars.check_thread),
        )

    draft = sub.add_parser("draft", help="draft tools")
    _add_global_flags(draft, prefix="draft_")
    draft_sub = draft.add_subparsers(
        dest=argparse.SUPPRESS, metavar="<command>", required=True, parser_class=_Parser
    )
    check = _leaf(draft_sub, "check", "draft check", "validate a draft offline", positional=())
    check.add_argument("path", metavar="PATH|-")
    new = _leaf(draft_sub, "new", "draft new", "write a new draft for a PR")
    new.add_argument("--event", action=_StoreOnce, choices=("comment", "request_changes"))
    new.add_argument("--from", action=_StoreOnce, dest="from_draft", metavar="PATH|-")

    export = _leaf(sub, "export", "export", "print audit or log records", positional=())
    # TODO(#38): validate --since (RFC 3339 or a duration such as 7d).
    export.add_argument("--since", action=_StoreOnce, metavar="TIME")
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
    # Value grammars live in grammars.py and run as argparse types, so argparse names the flag;
    # the PR argument is checked here because its error code differs (`invalid_pr`).
    pr = getattr(args, "pr", None)
    if pr is not None:
        try:
            grammars.check_pr(pr)
        except grammars.GrammarError as exc:
            raise UsageError(str(exc), code="invalid_pr") from None
    if args.expect_plan is not None and args.command not in POSTING_COMMANDS:
        raise UsageError(f"--expect-plan is only valid with {', '.join(sorted(POSTING_COMMANDS))}")
    # The parse-time caps count per parser level; these count the merged total.
    if len(args.decision_id) > MAX_DECISION_IDS:
        raise UsageError(f"at most {MAX_DECISION_IDS} --decision-id values")
    if args.command in ("resolve", "unresolve") and len(args.thread) > MAX_THREADS:
        raise UsageError(f"at most {MAX_THREADS} --thread values")


# --- dispatch --------------------------------------------------------------------------------


def _not_implemented(args: argparse.Namespace) -> Outcome:
    return error_outcome("internal", f"{args.command} is not implemented yet")


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
_COMMAND_VALUE_FLAGS = frozenset(
    {
        "--allow-mention",
        "--body-file",
        "--dedupe-key",
        "--draft-file",
        "--event",
        "--expected-base",
        "--expected-head",
        "--from",
        "--reanchor-to",
        "--since",
        "--thread",
        "--to",
    }
)
VALUE_FLAGS = _VALUE_FLAGS | _COMMAND_VALUE_FLAGS
_VALUELESS_FLAGS = frozenset(
    {
        "--approve",
        "--dry-run",
        "--help",
        "--json",
        "--log",
        "--quiet",
        "--verbose",
        "--version",
        "--yes",
        "-h",
    }
)
_COMMANDS_WITH_PR = frozenset({"review", "comment", "reply", "approve", "resolve", "unresolve"})


def _wants_json(argv: list[str]) -> bool:
    """True when a token before ``--`` asks for JSON (``--json`` or ``--json=<anything>``)."""
    head = argv[: argv.index("--")] if "--" in argv else argv
    return any(token == "--json" or token.startswith("--json=") for token in head)


MAX_ARGV_TOKENS = 1024


def _has_flag(argv: list[str], flag: str) -> bool:
    """True when ``flag`` appears as a token before ``--`` (used when parsing itself failed)."""
    head = argv[: argv.index("--")] if "--" in argv else argv
    return flag in head


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


def _command_path(argv: list[str]) -> tuple[list[str], int]:
    """The command words (``["draft", "new"]``) and the index after them; empty when absent."""
    words: list[str] = []
    skip = False
    for index, token in enumerate(argv):
        if skip:
            skip = False
        elif token in _VALUE_FLAGS:
            skip = True
        elif token.startswith("-"):
            continue
        else:
            words.append(token)
            if token != "draft" or len(words) == 2:
                return words, index + 1
    return words, len(argv)


def _leading_dash_pr(argv: list[str]) -> bool:
    """True when the PR slot of a command holds a token starting with ``-``.

    argparse reads such a token as an option, so the command would fail with a missing-argument
    usage error instead of ``invalid_pr``. Scans the tokens after the command, skipping known
    flags and their values. An unknown dash token is the PR argument only when no positional
    follows it: with one (``comment --bogus a#1 ...``) the PR is present and argparse reports the
    unknown flag as usage. A ``--`` ends the scan: the token after it is a positional and
    ``validate_args`` checks it. ``-h``, ``--help`` and ``--version`` before ``--`` always reach
    argparse.
    """
    head = argv[: argv.index("--")] if "--" in argv else argv
    if any(token in ("-h", "--help", "--version") for token in head):
        return False
    words, start = _command_path(argv)
    if not (words and words[-1] in _COMMANDS_WITH_PR) and words != ["draft", "new"]:
        return False
    unknown_dash = False
    skip = False
    for token in argv[start:]:
        if skip:
            skip = False
            continue
        if token == "--":
            return False
        if token == "-":
            return True
        if not token.startswith("-"):
            return False  # the PR argument is present; validate_args checks it
        name = token.partition("=")[0]
        if name in VALUE_FLAGS:
            skip = "=" not in token
        elif name not in _VALUELESS_FLAGS:
            unknown_dash = True
    return unknown_dash


def _write(stream: Any, text: str) -> None:
    # TODO(#4): tolerate None/closed stdout/stderr in _write
    """Write text that cannot fail on the stream's encoding."""
    if text:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        stream.write(text.encode(encoding, "backslashreplace").decode(encoding, "replace"))
        stream.flush()


def _fail_usage(argv: list[str], message: str, usage: str, code: str = "usage") -> int:
    outcome = error_outcome(code, _truncate(message))
    stdout, stderr = render(
        outcome,
        as_json=_wants_json(argv),
        quiet=_has_flag(argv, "--quiet"),
        verbose=_has_flag(argv, "--verbose"),
    )
    _write(sys.stderr, usage)
    _write(sys.stderr, stderr)
    _write(sys.stdout, stdout)
    return outcome.exit_code


class _StderrLogHandler(logging.Handler):
    """Prints log records (tracebacks included) to stderr with every untrusted character marked.

    Without it an uncaught exception's traceback would reach stderr raw through Python's
    last-resort handler, and the exception text may hold untrusted data.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _write(sys.stderr, sanitize(self.format(record), one_line=False) + "\n")
        except Exception:
            self.handleError(record)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    handler: logging.Handler = (
        logging.NullHandler() if _has_flag(argv, "--quiet") else _StderrLogHandler()
    )
    LOG.addHandler(handler)
    try:
        return _main(argv)
    finally:
        LOG.removeHandler(handler)


def _log_config_warnings(args: argparse.Namespace) -> None:
    """Write the config's warnings to log.jsonl; nothing is opened when there are none.

    A state directory that is unsafe does not end the run here: the posting orders check it again
    ([Files]) and refuse with `state_unsafe` before any write.
    """
    warnings = args.loaded_config.warnings
    if not warnings:
        return
    try:
        dirs = open_state_dirs()
    except StateUnsafeError as exc:
        LOG.warning("log.jsonl not written: %s", sanitize(str(exc)))
        return
    try:
        log = LogWriter.open(dirs, verbose=args.verbose)
        try:
            write_config_warnings(log, warnings)
        finally:
            log.close()
    finally:
        dirs.close()


def _load_config_into(args: argparse.Namespace) -> Outcome | None:
    """Load the config once per run into ``args.loaded_config``; an invalid config is the outcome.

    ``draft check`` is offline and ``export`` reads local files only: neither reads config, so a
    broken config never blocks reading the audit trail (interface.md, Commands).
    ``loaded_config.warnings`` go to log.jsonl as ``config_warning`` lines (a failure only warns).
    TODO(#26) and TODO(#29): callers pass the canonical repo id to ``loaded_config.for_repo``.
    TODO(#33): merge ``loaded_config.hooks_for(name, args.hook)`` when running hooks.
    """
    if args.command in ("draft check", "export"):
        return None
    try:
        args.loaded_config = load_config(args.config, env=os.environ)
    except ConfigError as exc:
        # The key can be a file name or user text (control characters, a lone surrogate from a
        # non-UTF-8 --config path), so both fields are sanitized.
        return error_outcome(
            "config_invalid", sanitize(str(exc)), details={"key": sanitize(exc.key)}
        )
    # The flag rules were checked in validate_args and do not depend on the name; the config's
    # `me.name` only replaces the OS username of a TTY default actor.
    args.run = with_me_name(args.run, args.loaded_config.config.me.name)
    _log_config_warnings(args)
    return None


def _main(argv: list[str]) -> int:
    parser = build_parser()
    if len(argv) > MAX_ARGV_TOKENS:  # argparse is quadratic in repeated flags
        return _fail_usage(
            argv, f"too many arguments (at most {MAX_ARGV_TOKENS})", parser.format_usage()
        )
    if _dash_dash_before_command(argv):
        return _fail_usage(argv, "'--' is not allowed before the command", parser.format_usage())
    if _leading_dash_pr(argv):
        return _fail_usage(
            argv, "invalid PR (a PR argument must not start with '-')", "", "invalid_pr"
        )
    try:
        args = parser.parse_args(argv)
        validate_args(args)
    except UsageError as exc:
        return _fail_usage(argv, exc.message, exc.usage, exc.code)
    try:
        outcome = _load_config_into(args) or check_outcome(HANDLERS[args.command](args))
        stdout, stderr = render(outcome, as_json=args.json, quiet=args.quiet, verbose=args.verbose)
    except Exception as exc:  # a handler bug must still honor the exit-code and envelope contract
        outcome = internal_outcome(exc)
        stdout, stderr = render(outcome, as_json=args.json, quiet=args.quiet, verbose=args.verbose)
    _write(sys.stderr, stderr)
    _write(sys.stdout, stdout)
    return outcome.exit_code
