"""Value grammars for everything pr-comment forwards to forgectl or records.

One shared validator (docs/interface.md, forgectl edge): every ``check_*`` function returns the
value to use (a lowered sha, otherwise the value unchanged) or raises ``GrammarError``. The error
message names the expected form and never echoes the value, so it is safe to print.
``hook_decision_id`` is the one exception to raising: a hook-supplied id that fails is dropped
with a warning, never an exit 2.

The config loader (config.py) calls ``check_timeout`` and ``check_forgectl_config`` and turns a
``GrammarError`` into exit 2 ``config_invalid`` with ``details.key``. Caller not written yet: the
hooks module (#33) must call ``hook_decision_id``.

Patterns are matched with ``fullmatch`` over ASCII classes only, so a trailing newline, a Unicode
digit or a lookalike character never passes.
"""

from __future__ import annotations

import math
import re
import unicodedata
from decimal import Decimal
from fractions import Fraction

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
MENTION_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*")
_NODE = r"[A-Za-z0-9_=-]{1,100}"
_THREAD_RE = re.compile(rf"gh:rt:{_NODE}|gl:d:[0-9a-f]{{40}}")
_REPLY_TARGET_RE = re.compile(
    rf"gh:(?:ic|rc|rt|r):{_NODE}|gl:d:[0-9a-f]{{40}}|gl:n:[1-9][0-9]{{0,18}}"
)
# schemas/defs.json, "duration" (string forms): <number><unit>, or a bare number meaning seconds.
_DURATION_RE = re.compile(r"(?=.*[1-9])([0-9]+(?:\.[0-9]+)?)(ms|s|m|h|d)?")
_UNIT_SECONDS = {
    None: Fraction(1),
    "s": Fraction(1),
    "ms": Fraction(1, 1000),
    "m": Fraction(60),
    "h": Fraction(3600),
    "d": Fraction(86400),
}
# forgectl refuses a duration over 100 years (docs/interface.md, "Reading files"); a year is
# taken as 365.25 days.
MAX_TIMEOUT_SECONDS = Fraction(36525 * 86400)
# A real duration is a few characters; this keeps the exact arithmetic below cheap.
_MAX_DURATION_LENGTH = 400
_BRANCH_FORBIDDEN_RE = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
_BRANCH_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_SURROGATE_RE = re.compile("[\ud800-\udfff]")


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
        or text in ("@", "refs")
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


# TODO(#33): the hooks module drops a failing hook-supplied id with a warning (interface.md, Hooks).
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


def check_mention_name(value: object) -> str:
    """An ``--allow-mention`` value: a name of the Mentions section, a team as ``org/team``.

    The same grammar as the config key ``mentions.allow`` entries (no leading ``@``). The matcher
    compares it in ASCII lowercase, so the value comes back unchanged.
    """
    return _full(MENTION_NAME_RE, value, "mention name (a name, or a team as org/team)")


def check_hook(value: object) -> str:
    """``<name>=<cmd>`` with a known hook name and a non-empty command without NUL or surrogates."""
    text = _text(value, "hook")
    name, sep, cmd = text.partition("=")
    if not sep or name not in HOOK_NAMES or not cmd or "\x00" in cmd or _SURROGATE_RE.search(cmd):
        raise GrammarError(
            f"invalid hook (expected <name>=<cmd> with name one of: {', '.join(HOOK_NAMES)})"
        )
    return text


def check_escalation_scope(value: object) -> str:
    text = _text(value, "scope")
    if text not in ESCALATION_SCOPES:
        raise GrammarError(f"invalid scope (expected one of: {', '.join(ESCALATION_SCOPES)})")
    return text


# The config loader (config.py) calls this for `timeout` and PR_COMMENT_TIMEOUT; a GrammarError
# becomes exit 2 config_invalid with details.key.
def check_timeout(value: object) -> str:
    """The duration grammar of schemas/defs.json, capped at forgectl's 100 years.

    A positive number (int or float, as read from YAML), or a string: ``<number><unit>`` with unit
    ``ms``, ``s``, ``m``, ``h`` or ``d``, or a bare number meaning seconds. Zero, infinite and NaN
    values and anything over 100 years raise ``GrammarError``. A number comes back as a plain
    decimal string (never ``1e-05`` or ``1e+16``), so it can be forwarded as ``--timeout=<value>``;
    a string comes back unchanged.
    """
    message = "invalid timeout (a positive duration of at most 100 years, such as 30, 30s, 2m, 1h)"
    if isinstance(value, bool):
        raise GrammarError(message)
    if isinstance(value, int):  # exact: no float conversion, so any size is judged, not crashed on
        if 0 < value <= MAX_TIMEOUT_SECONDS:
            return str(value)
    elif isinstance(value, float):
        if math.isfinite(value) and 0 < value <= MAX_TIMEOUT_SECONDS:
            text = format(Decimal(repr(value)), "f")
            return text.removesuffix(".0") if text.endswith(".0") else text
    elif type(value) is str and len(value) <= _MAX_DURATION_LENGTH:
        match = _DURATION_RE.fullmatch(value)
        if match and Fraction(match[1]) * _UNIT_SECONDS[match[2]] <= MAX_TIMEOUT_SECONDS:
            return value
    raise GrammarError(message)


def duration_seconds(value: object) -> Fraction:
    """The exact seconds of a duration that ``check_timeout`` accepts (``GrammarError`` if not)."""
    text = check_timeout(value)
    match = _DURATION_RE.fullmatch(text)
    if match is None:  # check_timeout returns plain decimal strings for numbers
        raise GrammarError("invalid timeout")
    return Fraction(match[1]) * _UNIT_SECONDS[match[2]]


# The config loader (config.py) calls this for `forgectl_config` and PR_COMMENT_FORGECTL_CONFIG and
# turns a GrammarError into exit 2 config_invalid with details.key.
def check_forgectl_config(value: object) -> str:
    """A non-empty path that does not start with ``-`` (it is passed as ``--config=<path>``)."""
    text = _text(value, "forgectl config path")
    if not text or text.startswith("-") or "\x00" in text or _SURROGATE_RE.search(text):
        raise GrammarError(
            "invalid forgectl config path (non-empty, no leading '-', no NUL or surrogate)"
        )
    return text
