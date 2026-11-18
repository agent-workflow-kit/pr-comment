"""Actor resolution, interactive detection and the human-only flag rules.

docs/interface.md, Actors and interactive runs. The actor, how it was obtained and the interactive
state are computed once per run from facts the caller of ``resolve_run`` passes in (TTY state, the
OS user), never from environment variables or ``TERM``, so a caller cannot spoof them by setting
them. Human-only rules are advisory against a same-user process that claims ``--actor human:x``.
"""

from __future__ import annotations

import argparse
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Same grammar as the actor name in the --actor grammar and `me.name`.
_NAME_RE = re.compile(r"[A-Za-z0-9._@-]{1,64}")
UNKNOWN_HUMAN = "human:unknown"
DEFAULT_AUTO = "auto:unknown"

# Command -> the namespace attribute that may hold `-` (read from stdin).
_INPUT_ATTRS = {
    "review": "draft_file",
    "comment": "body_file",
    "reply": "body_file",
    "approve": "body_file",
    "draft check": "path",
    "draft new": "from_draft",
}

# `draft check -` reads stdin but has no dialog: it is not interactive, yet a TTY stdin is no error.
_REFUSE_DASH_ON_TTY = frozenset({"review", "comment", "reply", "approve", "draft new"})


class ActorError(ValueError):
    """A flag combination the actor and interactive rules refuse (exit 2, code ``usage``)."""


@dataclass(frozen=True)
class RunContext:
    """The per-run identity, for dispatch and the audit record."""

    actor: str
    actor_source: str  # "flag", "tty" (default human from a TTY stdin) or "default" (auto:unknown)
    stdin_tty: bool
    interactive: bool

    def audit_fields(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "actor_source": self.actor_source,
            "stdin_tty": self.stdin_tty,
        }


def os_username() -> str | None:
    """The OS username from the password database, never from USER or LOGNAME."""
    try:
        import pwd  # not on Windows; the tool supports Linux, WSL and macOS

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, OSError):
        return None


def stream_is_tty(stream: object) -> bool:
    """True only for an open stream that reports a terminal; anything odd is not a TTY."""
    try:
        return bool(stream.isatty())  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType]
    except (AttributeError, ValueError, OSError):
        return False


def _human_default(os_user: Callable[[], str | None]) -> str:
    # TODO(#7): `me.name` from the config takes precedence over the OS username.
    try:
        name = os_user()
    except (KeyError, OSError):
        return UNKNOWN_HUMAN
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return UNKNOWN_HUMAN
    return f"human:{name}"


def reads_stdin(args: argparse.Namespace) -> bool:
    attr = _INPUT_ATTRS.get(args.command)
    return attr is not None and getattr(args, attr, None) == "-"


def resolve_run(
    args: argparse.Namespace,
    *,
    stdin_tty: bool,
    stderr_tty: bool,
    os_user: Callable[[], str | None] = os_username,
) -> RunContext:
    """Resolve the run context and apply the flag rules. Raises ``ActorError``."""
    dash = reads_stdin(args)
    if dash and stdin_tty and args.command in _REFUSE_DASH_ON_TTY:
        raise ActorError(
            "stdin is a terminal: `-` cannot be read; pipe the input or pass a file path"
        )

    flag: str | None = args.actor
    if flag is not None:
        actor, source = flag, "flag"
    elif stdin_tty:
        actor, source = _human_default(os_user), "tty"
    else:
        actor, source = DEFAULT_AUTO, "default"

    human = actor.startswith("human:")
    # `not dash` mirrors docs/interface.md; it only matters for `draft check -` on a TTY, since
    # the other `-` inputs with a TTY stdin were refused above.
    interactive = stdin_tty and stderr_tty and human and not args.yes and not dash

    # --allow-mention exists only on the posting commands; --override-escalation is global.
    if getattr(args, "allow_mention", None) and not (human and interactive):
        raise ActorError(
            "--allow-mention needs a human: actor and an interactive run "
            "(stdin and stderr must be terminals; no --yes, no `-` input)"
        )
    if args.override_escalation and not human:
        raise ActorError("--override-escalation needs a human actor (--actor human:<name>)")

    return RunContext(actor, source, stdin_tty, interactive)
