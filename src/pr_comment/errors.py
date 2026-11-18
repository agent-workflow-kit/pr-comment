"""Exit codes and the error envelope (docs/interface.md: Exit codes, Error envelope).

Pure data and pure functions: no I/O. ``build_error`` returns the ``{"error": {...}}`` document
that ``schemas/cli/error.json`` describes, with every field present (null when it does not apply).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
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

CODE_EXIT: Final[dict[str, int]] = {
    "internal": EXIT_INTERNAL,
    **dict.fromkeys(_USAGE_CODES, EXIT_USAGE),
    "not_found": EXIT_NOT_FOUND,
    "vetoed": EXIT_VETOED,
    **dict.fromkeys(_FAILED_CODES, EXIT_FAILED),
    "confirmation_required": EXIT_NEEDS_HUMAN,
    "escalated": EXIT_NEEDS_HUMAN,
    "refused": EXIT_REFUSED,
}

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
_FIXED_LANDED_RETRY: Final[dict[str, tuple[bool | None, str]]] = {
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
# outcome_partial by the backend of `show` (the GitLab approval follows its note; GitHub does not).
_PARTIAL_GITHUB: Final = (False, "new_key")
# lower_contract_mismatch and own audit_failed before any write.
_BEFORE_WRITE: Final = (False, "same_command")
# forgectl's own audit_failed: its write landed.
_LOWER_AUDIT_FAILED: Final = (True, "none")

BACKENDS: Final = ("github", "gitlab")
RETRIES: Final = ("same_command", "new_key", "none")


def sanitize(text: str) -> str:
    """Replace control, format and surrogate characters (Cc, Cf, Cs) with ``<U+XXXX>``."""
    return "".join(
        f"<U+{ord(char):04X}>" if unicodedata.category(char) in ("Cc", "Cf", "Cs") else char
        for char in text
    )


def cut_bytes(text: str, limit: int = MAX_HOOK_STDERR_BYTES) -> str:
    """Cut ``text`` to at most ``limit`` UTF-8 bytes, never in the middle of a character.

    Surrogates (not encodable) are replaced first, so the result is always valid text.
    """
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return raw.decode("utf-8")
    return raw[:limit].decode("utf-8", errors="ignore")


def exit_code_for(code: str) -> int:
    """The exit code of an ``error.code``. Raises ``ValueError`` for an unknown code."""
    try:
        return CODE_EXIT[code]
    except KeyError:
        raise ValueError(f"unknown error code {code!r}") from None


def _has_landed_ids(landed: object) -> bool:
    if not isinstance(landed, Mapping):
        return False
    mapping: Mapping[str, Any] = landed  # pyright: ignore[reportUnknownVariableType]
    return any(bool(mapping.get(key)) for key in ("ids", "thread_ids"))


def landed_retry(
    code: str,
    *,
    details: Mapping[str, Any] | None = None,
    backend: str | None = None,
    from_forgectl: bool = False,
    write: tuple[bool, str] | None = None,
) -> tuple[bool | None, str | None]:
    """The ``(landed, retry)`` pair of an error code; ``(None, None)`` for a code outside exit 12.

    - ``backend`` (``show``'s) is required for ``outcome_partial``.
    - ``from_forgectl`` marks ``audit_failed`` as forgectl's (its write landed).
    - ``write`` gives the write's own ``(landed, retry)`` for ``lower_contract_mismatch`` and
      own ``audit_failed`` after a write; without it the failure came before any write.
    """
    if CODE_EXIT.get(code) != EXIT_FAILED:
        return None, None
    if code in _FIXED_LANDED_RETRY:
        return _FIXED_LANDED_RETRY[code]
    if code == "host_rejected":
        # A rerun is refused until a human deletes the leftover drafts in the web UI.
        if details is not None and "leftover_drafts" in details:
            return False, "none"
        return False, "same_command"
    if code == "outcome_partial":
        if backend == "gitlab":
            landed = details.get("landed") if details is not None else None
            return _has_landed_ids(landed), "same_command"
        if backend == "github":
            return _PARTIAL_GITHUB
        raise ValueError(f"outcome_partial needs backend github or gitlab, got {backend!r}")
    if code == "audit_failed" and from_forgectl:
        return _LOWER_AUDIT_FAILED
    # lower_contract_mismatch, own audit_failed
    if write is None:
        return _BEFORE_WRITE
    landed_value, retry = write
    if retry not in RETRIES:
        raise ValueError(f"unknown retry {retry!r}")
    return landed_value, retry


def build_error(
    code: str,
    message: str,
    *,
    reason: str | None = None,
    details: Mapping[str, Any] | None = None,
    retry_after: float | None = None,
    backend: str | None = None,
    from_forgectl: bool = False,
    write: tuple[bool, str] | None = None,
    lower_error: Mapping[str, Any] | None = None,
    hook: str | None = None,
    hook_stderr: str | None = None,
    escalated_by: str | None = None,
    escalated_hooks: Sequence[Mapping[str, Any]] | None = None,
    decision_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """The error envelope document ``{"error": {...}}``; every field is present.

    Raises ``ValueError`` for a combination the interface forbids (an unknown code, a refusal
    without a reason, a reason on any other code, an escalation without its fields).
    """
    exit_code_for(code)
    if code == "refused":
        if reason not in REFUSAL_REASONS:
            raise ValueError(f"refused needs a reason from the refusal list, got {reason!r}")
    elif reason is not None:
        raise ValueError(f"reason is only valid with code refused, not {code!r}")
    if code == "escalated":
        if escalated_by not in ("pr-comment", "forgectl") or not escalated_hooks:
            raise ValueError("escalated needs escalated_by and a non-empty escalated_hooks")
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
        "message": sanitize(message),
        "reason": reason,
        "details": dict(details) if details is not None else None,
        "retry_after": retry_after,
        "landed": landed,
        "retry": retry,
        "lower_error": dict(lower_error) if lower_error is not None else None,
        "hook": hook,
        "hook_stderr": hook_stderr,
        "escalated_by": escalated_by,
        "escalated_hooks": [dict(h) for h in escalated_hooks] if escalated_hooks else None,
        "decision_ids": list(decision_ids),
    }
    return {"error": error}
