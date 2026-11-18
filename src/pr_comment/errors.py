"""Exit codes and the error envelope (docs/interface.md: Exit codes, Error envelope).

Pure data and pure functions: no I/O. ``build_error`` returns the ``{"error": {...}}`` document
that ``schemas/cli/error.json`` describes, with every field present (null when it does not apply).
"""

from __future__ import annotations

import copy
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, Final

EXIT_OK: Final = 0
EXIT_INTERNAL: Final = 1
EXIT_USAGE: Final = 2
EXIT_NOT_FOUND: Final = 10
EXIT_VETOED: Final = 11
EXIT_FAILED: Final = 12
EXIT_NEEDS_HUMAN: Final = 13
EXIT_REFUSED: Final = 20

EXIT_CODES: Final = frozenset(
    {
        EXIT_OK,
        EXIT_INTERNAL,
        EXIT_USAGE,
        EXIT_NOT_FOUND,
        EXIT_VETOED,
        EXIT_FAILED,
        EXIT_NEEDS_HUMAN,
        EXIT_REFUSED,
    }
)

MAX_HOOK_STDERR_BYTES: Final = 4096

# error.code -> exit code. Exit 20 is always code `refused`, with a `reason` from REFUSAL_REASONS.
_USAGE_CODES = (
    "usage",
    "config_invalid",
    "invalid_pr",
    "invalid_draft",
    "invalid_text",
    "unsupported_draft_version",
    "draft_pr_mismatch",
    "approve_conflicts_event",
    "body_too_large",
)
_FAILED_CODES = (
    "tool_missing",
    "lower_contract_mismatch",
    "audit_unavailable",
    "audit_failed",
    "state_unsafe",
    "invalid_position",
    "outcome_unknown",
    "outcome_partial",
    "quick_action_ran",
    "approved_stale_head",
    "auth_required",
    "permission_denied",
    "rate_limited",
    "upstream_error",
    "network_error",
    "host_rejected",
    "unsupported_host_version",
    "wrong_host_response",
)

CODE_EXIT: Final[Mapping[str, int]] = MappingProxyType(
    {
        "internal": EXIT_INTERNAL,
        **dict.fromkeys(_USAGE_CODES, EXIT_USAGE),
        "not_found": EXIT_NOT_FOUND,
        "vetoed": EXIT_VETOED,
        **dict.fromkeys(_FAILED_CODES, EXIT_FAILED),
        "confirmation_required": EXIT_NEEDS_HUMAN,
        "escalated": EXIT_NEEDS_HUMAN,
        "refused": EXIT_REFUSED,
    }
)

REFUSAL_REASONS: Final = frozenset(
    {
        "stale_head",
        "stale_base",
        "auto_approve_disabled",
        "auto_approve_reanchored",
        "not_open",
        "plan_changed",
        "self_approval",
        "drafts_present",
        "pending_review_exists",
        "not_resolvable",
        "too_large",
        "unknown_host",
    }
)

# Exit-12 `landed` and `retry` (interface: Error envelope). Codes whose pair is fixed:
_FIXED_LANDED_RETRY: Final[Mapping[str, tuple[bool | None, str]]] = MappingProxyType(
    {
        **dict.fromkeys(
            (
                "tool_missing",
                "audit_unavailable",
                "state_unsafe",
                "invalid_position",
                "auth_required",
                "permission_denied",
                "rate_limited",
                "upstream_error",
                "network_error",
                "unsupported_host_version",
                "wrong_host_response",
            ),
            (False, "same_command"),
        ),
        "outcome_unknown": (None, "same_command"),
        "quick_action_ran": (True, "none"),
        "approved_stale_head": (True, "none"),
    }
)
# outcome_partial by the backend of `show` (the GitLab approval follows its note; GitHub does not).
_PARTIAL_GITHUB: Final = (False, "new_key")
# lower_contract_mismatch before any write.
_BEFORE_WRITE: Final = (False, "same_command")
# forgectl's own audit_failed: its write landed.
_LOWER_AUDIT_FAILED: Final = (True, "none")
# The pairs a write can report (a landed write, a clean failure, a partial one, an unknown one).
_WRITE_PAIRS: Final = frozenset(
    {(True, "none"), (False, "same_command"), (False, "new_key"), (None, "same_command")}
)
_CODES_WITH_WRITE: Final = frozenset({"lower_contract_mismatch", "audit_failed"})

_DECISION_ID_RE: Final = re.compile(r"^[A-Za-z0-9._:@/-]{1,128}$")
MAX_DECISION_IDS: Final = 32
_INVISIBLE_CODE_POINTS: Final = frozenset(
    {0x034F, 0x17B4, 0x17B5, 0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800}
    | set(range(0x180B, 0x1810))
    | set(range(0xFE00, 0xFE10))
    | set(range(0xE0100, 0xE01F0))
)
_MARKED_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})

BACKENDS: Final = ("github", "gitlab")
RETRIES: Final = ("same_command", "new_key", "none")


def sanitize(text: str, *, one_line: bool = True) -> str:
    """Replace the characters forgectl's Sanitizing rules list with ``<U+XXXX>``.

    These are the categories Cc, Cf, Cs, Co, Cn, Zl and Zp and the listed invisible code points.
    ``one_line`` (the default) also marks tab and LF; pass ``False`` for multi-line text.
    """
    if not isinstance(text, str):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError(f"sanitize needs str, got {type(text).__name__}")
    keep = "" if one_line else "\t\n"
    return "".join(
        f"<U+{ord(char):04X}>"
        if char not in keep
        and (
            ord(char) in _INVISIBLE_CODE_POINTS or unicodedata.category(char) in _MARKED_CATEGORIES
        )
        else char
        for char in text
    )


def cut_bytes(text: str, limit: int = MAX_HOOK_STDERR_BYTES) -> str:
    """Cut ``text`` to at most ``limit`` UTF-8 bytes, never in the middle of a character.

    The cut is silent (no marker). Surrogates (not encodable) are replaced first, so the result
    is always valid text. Raises ``TypeError`` for a non-string.
    """
    if not isinstance(text, str):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError(f"cut_bytes needs str, got {type(text).__name__}")
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return raw.decode("utf-8")
    return raw[:limit].decode("utf-8", errors="ignore")


def exit_code_for(code: str) -> int:
    """The exit code of an ``error.code``. Raises ``ValueError`` for an unknown code."""
    if not isinstance(code, str):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError(f"unknown error code {code!r}")
    try:
        return CODE_EXIT[code]
    except KeyError:
        raise ValueError(f"unknown error code {code!r}") from None


def _is_count(value: object) -> bool:
    return type(value) is int and value >= 0


def _partial_has_landed(details: Mapping[str, Any] | None) -> bool:
    """Whether a GitLab ``outcome_partial``'s ``details.landed`` holds ids or thread ids."""
    landed: object = details.get("landed") if details is not None else None
    if not isinstance(landed, Mapping):
        raise ValueError("outcome_partial on gitlab needs details.landed as a mapping")
    mapping: Mapping[str, Any] = landed  # pyright: ignore[reportUnknownVariableType]
    found = False
    for key in ("ids", "thread_ids"):
        value: object = mapping.get(key, [])
        if not isinstance(value, list):
            raise ValueError(f"details.landed.{key} must be a list")
        found = found or bool(value)  # pyright: ignore[reportUnknownArgumentType]
    return found


def _check_write(write: object) -> tuple[bool | None, str]:
    if not isinstance(write, tuple) or len(write) != 2:  # pyright: ignore[reportUnknownArgumentType]
        raise ValueError("write must be a (landed, retry) pair")
    landed: object = write[0]  # pyright: ignore[reportUnknownVariableType]
    retry: object = write[1]  # pyright: ignore[reportUnknownVariableType]
    if not (landed is True or landed is False or landed is None):
        raise ValueError(f"write landed must be True, False or None, got {landed!r}")
    if not isinstance(retry, str) or retry not in RETRIES:
        raise ValueError(f"unknown retry {retry!r}")
    if (landed, retry) not in _WRITE_PAIRS:
        raise ValueError(f"write {(landed, retry)!r} is not a pair a write can report")
    return landed, retry


def landed_retry(
    code: str,
    *,
    details: Mapping[str, Any] | None = None,
    backend: str | None = None,
    from_forgectl: bool = False,
    write: tuple[bool | None, str] | None = None,
) -> tuple[bool | None, str | None]:
    """The ``(landed, retry)`` pair of an error code; ``(None, None)`` for a code outside exit 12.

    - ``backend`` (``show``'s) is required for ``outcome_partial``.
    - ``from_forgectl`` marks ``audit_failed`` as forgectl's (its write landed).
    - ``write`` gives the write's own ``(landed, retry)``: required for own ``audit_failed``
      (it always follows a write), optional for ``lower_contract_mismatch`` (without it the
      failure came before any write).

    Raises ``ValueError`` for a malformed ``write``, for ``write`` or ``from_forgectl`` on a code
    that does not use them, and for malformed ``details`` where the pair depends on them.
    """
    exit_code_for(code)
    if write is not None and code not in _CODES_WITH_WRITE:
        raise ValueError(f"write is not valid with code {code!r}")
    if from_forgectl and code != "audit_failed":
        raise ValueError(f"from_forgectl is not valid with code {code!r}")
    if CODE_EXIT[code] != EXIT_FAILED:
        return None, None
    if code in _FIXED_LANDED_RETRY:
        return _FIXED_LANDED_RETRY[code]
    if code == "host_rejected":
        # A rerun is refused until a human deletes the leftover drafts in the web UI. The key's
        # presence decides (fail-safe: never invite a rerun that could double-post).
        if details is not None and "leftover_drafts" in details:
            if not _is_count(details["leftover_drafts"]):
                raise ValueError("details.leftover_drafts must be an integer >= 0")
            return False, "none"
        return False, "same_command"
    if code == "outcome_partial":
        if backend == "gitlab":
            return _partial_has_landed(details), "same_command"
        if backend == "github":
            return _PARTIAL_GITHUB
        raise ValueError(f"outcome_partial needs backend github or gitlab, got {backend!r}")
    if code == "audit_failed":
        if from_forgectl:
            if write is not None:
                raise ValueError("audit_failed takes from_forgectl or write, not both")
            return _LOWER_AUDIT_FAILED
        if write is None:
            raise ValueError("own audit_failed always follows a write: write is required")
        return _check_write(write)
    # lower_contract_mismatch
    if write is None:
        return _BEFORE_WRITE
    return _check_write(write)


def _check_retry_after(value: object) -> None:
    if value is None:
        return
    if type(value) is not int and type(value) is not float:
        raise ValueError("retry_after must be a finite number >= 0 or None")
    if not math.isfinite(value) or value < 0:
        raise ValueError("retry_after must be a finite number >= 0 or None")


def _check_decision_ids(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("decision_ids must be a list of strings")
    ids: list[str] = list(value)  # pyright: ignore[reportUnknownArgumentType]
    if len(ids) > MAX_DECISION_IDS:
        raise ValueError(f"decision_ids has more than {MAX_DECISION_IDS} entries")
    for item in ids:
        if not isinstance(item, str) or not _DECISION_ID_RE.fullmatch(item):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError("decision_ids entries must match the decision-id grammar")
    return ids


def _check_escalated_hooks(value: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError("escalated_hooks must be a list of {hook, index}")
    hooks: list[dict[str, Any]] = []
    for item in value:
        if (
            not isinstance(item, Mapping)  # pyright: ignore[reportUnnecessaryIsInstance]
            or set(item) != {"hook", "index"}
            or not isinstance(item["hook"], str)
            or not _is_count(item["index"])
        ):
            raise ValueError("escalated_hooks entries must be {hook: str, index: int >= 0}")
        hooks.append(copy.deepcopy(dict(item)))
    return hooks


def _check_mapping(name: str, value: object) -> None:
    if value is not None and not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping or None, got {type(value).__name__}")


def build_error(
    code: str,
    message: str,
    *,
    reason: str | None = None,
    details: Mapping[str, Any] | None = None,
    retry_after: float | None = None,
    backend: str | None = None,
    from_forgectl: bool = False,
    write: tuple[bool | None, str] | None = None,
    lower_error: Mapping[str, Any] | None = None,
    hook: str | None = None,
    hook_stderr: str | None = None,
    escalated_by: str | None = None,
    escalated_hooks: Sequence[Mapping[str, Any]] | None = None,
    decision_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """The error envelope document ``{"error": {...}}``; every field is present.

    The message is kept as given: ``--json`` output is not display-sanitized (human rendering
    sanitizes it). Raises ``ValueError`` for a combination the interface forbids (an unknown
    code, a refusal without a reason, a reason on any other code, an escalation without its
    fields, a malformed field) and ``TypeError`` for a field of the wrong type.
    """
    exit_code_for(code)
    if not isinstance(message, str):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError(f"message must be str, got {type(message).__name__}")
    if reason is not None and not isinstance(reason, str):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError(f"reason must be str or None, got {type(reason).__name__}")
    if hook_stderr is not None and not isinstance(hook_stderr, str):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError(f"hook_stderr must be str or None, got {type(hook_stderr).__name__}")
    _check_mapping("details", details)
    _check_mapping("lower_error", lower_error)
    _check_retry_after(retry_after)
    ids = _check_decision_ids(decision_ids)
    if code == "refused":
        if reason not in REFUSAL_REASONS:
            raise ValueError(f"refused needs a reason from the refusal list, got {reason!r}")
    elif reason is not None:
        raise ValueError(f"reason is only valid with code refused, not {code!r}")
    hooks: list[dict[str, Any]] | None = None
    if code == "escalated":
        if escalated_by not in ("pr-comment", "forgectl") or not escalated_hooks:
            raise ValueError("escalated needs escalated_by and a non-empty escalated_hooks")
        hooks = _check_escalated_hooks(escalated_hooks)
        if escalated_by == "forgectl" and (hook is not None or hook_stderr is not None):
            raise ValueError("hook and hook_stderr name pr-comment's own hook, not forgectl's")
    elif escalated_by is not None or escalated_hooks is not None:
        raise ValueError("escalated_by and escalated_hooks are only valid with code escalated")
    if hook is not None:
        if hook != "pre-post" or code not in ("vetoed", "escalated"):
            raise ValueError("hook is only valid as pre-post with code vetoed or escalated")
        hook_stderr = cut_bytes(hook_stderr or "")
    elif hook_stderr is not None:
        raise ValueError("hook_stderr needs hook")
    landed, retry = landed_retry(
        code, details=details, backend=backend, from_forgectl=from_forgectl, write=write
    )
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "reason": reason,
        "details": copy.deepcopy(dict(details)) if details is not None else None,
        "retry_after": retry_after,
        "landed": landed,
        "retry": retry,
        "lower_error": copy.deepcopy(dict(lower_error)) if lower_error is not None else None,
        "hook": hook,
        "hook_stderr": hook_stderr,
        "escalated_by": escalated_by,
        "escalated_hooks": hooks,
        "decision_ids": ids,
    }
    return {"error": error}
