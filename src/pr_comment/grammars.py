"""Value grammars for everything pr-comment forwards to forgectl or records.

One shared validator (docs/interface.md, forgectl edge): every ``check_*`` function returns the
value to use (a lowered sha, otherwise the value unchanged) or raises ``GrammarError``. The error
message names the expected form and never echoes the value, so it is safe to print.
``hook_decision_id`` is the one exception to raising: a hook-supplied id that fails is dropped
with a warning, never an exit 2.

Patterns are matched with ``fullmatch`` over ASCII classes only, so a trailing newline, a Unicode
digit or a lookalike character never passes.
"""

from __future__ import annotations

import math
import re
import unicodedata

MAX_DECISION_IDS = 32
MAX_THREADS = 100
MAX_BRANCH_LENGTH = 1024

HOOK_NAMES = ("pre-post", "post-post")
ESCALATION_SCOPES = ("pr-comment", "forgectl")

_PR_URL_RE = re.compile(r"https://[\x21-\x7e]+")
_PR_SHORT_RE = re.compile(r"(?!-)[\x21-\x7e]+#[0-9]+")
_SHA_RE = re.compile(r"[0-9A-Fa-f]{40}")
_PLAN_HASH_RE = re.compile(r"[0-9a-f]{64}")
_DEDUPE_KEY_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_DECISION_ID_RE = re.compile(r"[A-Za-z0-9._:@/-]{1,128}")
_ACTOR_RE = re.compile(r"(?:human|auto):[A-Za-z0-9._@-]{1,64}")
_NODE = r"[A-Za-z0-9_=-]{1,100}"
_THREAD_RE = re.compile(rf"gh:rt:{_NODE}|gl:d:[0-9a-f]{{40}}")
_REPLY_TARGET_RE = re.compile(
    rf"gh:(?:ic|rc|rt|r):{_NODE}|gl:d:[0-9a-f]{{40}}|gl:n:[1-9][0-9]{{0,18}}"
)
# schemas/defs.json, "duration" (string form).
_DURATION_RE = re.compile(r"(?=.*[1-9])[0-9]+(?:\.[0-9]+)?(?:ms|s|m|h|d)")
_BRANCH_FORBIDDEN_RE = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
_BRANCH_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


class GrammarError(ValueError):
    """A value that fails its grammar. The message describes the grammar, not the value."""


def _text(value: object, what: str) -> str:
    if type(value) is not str:
        raise GrammarError(f"invalid {what}: expected a string")
    return value


def _full(pattern: re.Pattern[str], value: object, what: str) -> str:
    text = _text(value, what)
    if not pattern.fullmatch(text):
        raise GrammarError(f"invalid {what}")
    return text


def check_pr(value: object) -> str:
    """``https://...`` or ``<something>#<digits>``, printable ASCII, no leading ``-``.

    forgectl parses the rest (docs/interface.md, Identifiers).
    """
    text = _text(value, "PR")
    if not (_PR_URL_RE.fullmatch(text) or _PR_SHORT_RE.fullmatch(text)):
        raise GrammarError("invalid PR (expected https://... or <repo>#<number>, no leading '-')")
    return text


def check_sha(value: object) -> str:
    """A full 40-hex commit sha; uppercase is lowered."""
    return _full(_SHA_RE, value, "sha (40 hex digits)").lower()


def check_plan_hash(value: object) -> str:
    return _full(_PLAN_HASH_RE, value, "plan hash (64 lowercase hex digits)")


def check_branch(value: object) -> str:
    """forgectl's branch grammar: git ``check-ref-format`` rules, no leading ``-`` and more.

    Mirrors the ``branch`` pattern of schemas/defs.json, plus what forgectl adds: at most 1024
    characters, no control, format or line/paragraph-separator character, and neither ``refs`` nor
    anything starting with ``refs/``. A component starting with ``.`` is refused too (git's rule).
    """
    text = _text(value, "branch")
    if (
        not 1 <= len(text) <= MAX_BRANCH_LENGTH
        or text[0] in "-/"
        or text[-1] in "/."
        or ".." in text
        or "@{" in text
        or "//" in text
        or _BRANCH_FORBIDDEN_RE.search(text)
        or text == "refs"
        or text.startswith("refs/")
        or any(part.startswith(".") or part.endswith(".lock") for part in text.split("/"))
        or any(unicodedata.category(char) in _BRANCH_FORBIDDEN_CATEGORIES for char in text)
    ):
        raise GrammarError("invalid branch name (git check-ref-format rules, no leading '-')")
    return text


def check_dedupe_key(value: object) -> str:
    return _full(_DEDUPE_KEY_RE, value, "dedupe key")


def check_decision_id(value: object) -> str:
    return _full(_DECISION_ID_RE, value, "decision id")


def check_actor(value: object) -> str:
    return _full(_ACTOR_RE, value, "actor (human:<name> or auto:<name>)")


def hook_decision_id(value: object) -> str | None:
    """The id to keep, or ``None`` to drop it with a warning. Never raises, whatever the value."""
    if type(value) is not str or len(value) > 128:
        return None
    return value if _DECISION_ID_RE.fullmatch(value) else None


def check_thread(value: object) -> str:
    """A thread id: ``gh:rt:<node>`` or ``gl:d:<40 hex>``."""
    return _full(_THREAD_RE, value, "thread id (gh:rt:<id> or gl:d:<40 hex>)")


def check_reply_target(value: object) -> str:
    """A comment, thread or review id in forgectl's forms."""
    return _full(
        _REPLY_TARGET_RE,
        value,
        "id (gh:ic:, gh:rc:, gh:rt:, gh:r:, gl:d:<40 hex> or gl:n:<number>)",
    )


def check_hook(value: object) -> str:
    """``<name>=<cmd>`` with a known hook name and a non-empty command."""
    text = _text(value, "hook")
    name, sep, cmd = text.partition("=")
    if not sep or name not in HOOK_NAMES or not cmd:
        raise GrammarError(
            f"invalid hook (expected <name>=<cmd> with name one of: {', '.join(HOOK_NAMES)})"
        )
    return text


def check_escalation_scope(value: object) -> str:
    text = _text(value, "scope")
    if text not in ESCALATION_SCOPES:
        raise GrammarError(f"invalid scope (expected one of: {', '.join(ESCALATION_SCOPES)})")
    return text


def check_timeout(value: object) -> object:
    """The duration grammar of schemas/defs.json: a positive number, or a string such as ``30s``.

    Returned unchanged.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isfinite(value) and value > 0:
            return value
    elif type(value) is str and _DURATION_RE.fullmatch(value):
        return value
    raise GrammarError("invalid timeout (a positive number or a duration such as 30s, 2m, 1h)")


def check_forgectl_config(value: object) -> str:
    """A non-empty path that does not start with ``-`` (it is passed as ``--config=<path>``)."""
    text = _text(value, "forgectl config path")
    if not text or text.startswith("-") or "\x00" in text:
        raise GrammarError("invalid forgectl config path (non-empty, no leading '-', no NUL)")
    return text
