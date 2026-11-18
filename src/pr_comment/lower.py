"""What a forgectl result means to pr-comment (docs/interface.md: Exit codes, Error envelope,
forgectl edge).

``require_ok`` returns the parsed JSON document of an exit 0, or raises ``LowerFailure`` carrying
the ``Outcome`` pr-comment ends with. ``failure_outcome`` maps any other result: forgectl's exit
code, ``code``, ``reason``, ``details`` and ``retry_after`` are copied, its whole envelope goes in
``lower_error``, and anything pr-comment does not know (an exit code, a code, a reason, an
ill-typed field, output that is not UTF-8 JSON) is ``lower_contract_mismatch``. forgectl's text
is never put in a message: ``message`` is forgectl's own on a passed-through error, otherwise a
fixed sentence.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

from .errors import CODE_EXIT, cut_bytes
from .gateway import (
    INSTALL_COMMAND,
    ForgectlResult,
    Gateway,
    ToolMissing,
)
from .output import Outcome, error_outcome

MAX_ENVELOPE_BYTES: Final = 1024 * 1024
MAX_MESSAGE_BYTES: Final = 4096

_EXIT2_PASSTHROUGH: Final = frozenset({"body_too_large", "invalid_pr", "config_invalid"})
# forgectl's exit-12 codes pr-comment passes through (its own codes are never taken from forgectl).
_EXIT12_PASSTHROUGH: Final = frozenset(
    {
        "tool_missing",
        "audit_unavailable",
        "audit_failed",
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
    }
)
_EXIT20_REASONS: Final = frozenset(
    {
        "stale_head",
        "stale_base",
        "not_open",
        "self_approval",
        "drafts_present",
        "pending_review_exists",
        "not_resolvable",
        "too_large",
        "unknown_host",
    }
)
_PASSTHROUGH: Final[Mapping[int, frozenset[str]]] = {
    1: frozenset({"internal"}),
    2: _EXIT2_PASSTHROUGH,
    10: frozenset({"not_found"}),
    11: frozenset({"vetoed"}),
    12: _EXIT12_PASSTHROUGH,
    13: frozenset({"confirmation_required", "escalated"}),
    20: frozenset({"refused"}),
}


class LowerFailure(Exception):
    """A forgectl call that did not give a usable success; ``outcome`` is how the run ends."""

    def __init__(self, outcome: Outcome) -> None:
        super().__init__("forgectl call failed")
        self.outcome = outcome


class _Mismatch(Exception):
    pass


def tool_missing_outcome(exc: ToolMissing) -> Outcome:
    """Exit 12 ``tool_missing`` with the install command."""
    return error_outcome("tool_missing", exc.message, details={"install": INSTALL_COMMAND})


def mismatch_outcome(
    command: str,
    why: str,
    *,
    gateway: Gateway,
    write: tuple[bool | None, str] | None = None,
) -> Outcome:
    """Exit 12 ``lower_contract_mismatch``; ``write`` only when a write may have happened."""
    return error_outcome(
        "lower_contract_mismatch",
        f"forgectl {command} gave an answer pr-comment cannot use ({why})",
        details={"command": command, "lower_version": gateway.lower_version()},
        write=write,
    )


def _load_json(result: ForgectlResult, limit: int | None) -> Any:
    if result.stdout_truncated or (limit is not None and len(result.stdout) > limit):
        raise _Mismatch("output too large")
    try:
        text = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        raise _Mismatch("output is not UTF-8") from None

    def _reject(_name: str) -> Any:
        raise ValueError("non-finite number")

    try:
        return json.loads(text, parse_constant=_reject)
    except (ValueError, RecursionError):
        raise _Mismatch("output is not JSON") from None


def _unknown_write(result: ForgectlResult) -> tuple[bool | None, str] | None:
    return (None, "same_command") if result.write else None


def require_ok(result: ForgectlResult, *, gateway: Gateway, backend: str | None = None) -> Any:
    """The JSON document (an object) of a successful call; ``LowerFailure`` otherwise.

    Exit 0 with output that is not one JSON object is ``lower_contract_mismatch``; so is a
    forgectl that exits 0 and left stdout cut at the limit.
    """
    if result.exit_code != 0:
        raise LowerFailure(failure_outcome(result, gateway=gateway, backend=backend))
    try:
        document = _load_json(result, None)
    except _Mismatch as exc:
        raise LowerFailure(
            mismatch_outcome(result.command, str(exc), gateway=gateway, write=_landed(result))
        ) from None
    if not isinstance(document, dict):
        raise LowerFailure(
            mismatch_outcome(
                result.command, "output is not an object", gateway=gateway, write=_landed(result)
            )
        )
    return document


def _landed(result: ForgectlResult) -> tuple[bool | None, str] | None:
    """The write pair after a bad exit-0 answer: forgectl said it succeeded, so it landed."""
    return (True, "none") if result.write else None


def failure_outcome(
    result: ForgectlResult, *, gateway: Gateway, backend: str | None = None
) -> Outcome:
    """The outcome of a forgectl call that did not succeed (or succeeded with a bad answer).

    ``backend`` is ``show``'s, needed to read an ``outcome_partial``.
    """
    command = result.command
    if result.timed_out:
        if result.write:
            return error_outcome(
                "outcome_unknown",
                f"forgectl {command} did not finish in time; whether it posted is unknown",
            )
        return error_outcome("network_error", f"forgectl {command} did not finish in time")
    if result.exit_code is None:
        if result.write:
            return error_outcome(
                "outcome_unknown",
                f"forgectl {command} ended on a signal; whether it posted is unknown",
            )
        return mismatch_outcome(command, "ended on a signal", gateway=gateway)
    if result.exit_code == 0:
        return mismatch_outcome(
            command, "exit 0 without a usable document", gateway=gateway, write=_landed(result)
        )
    allowed = _PASSTHROUGH.get(result.exit_code)
    write = _unknown_write(result)
    if allowed is None:
        return mismatch_outcome(
            command, f"unknown exit status {result.exit_code}", gateway=gateway, write=write
        )
    try:
        document = _load_json(result, MAX_ENVELOPE_BYTES)
        return _passthrough(result, document, allowed, backend)
    except (_Mismatch, ValueError, TypeError) as exc:
        why = str(exc) if isinstance(exc, _Mismatch) else "malformed error envelope"
        return mismatch_outcome(command, why, gateway=gateway, write=write)


def _mapping(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _Mismatch(f"{what} is not an object")
    return value  # pyright: ignore[reportUnknownVariableType]


def _passthrough(
    result: ForgectlResult, document: object, allowed: frozenset[str], backend: str | None
) -> Outcome:
    top = _mapping(document, "output")
    exit_code = result.exit_code
    if exit_code == 20 and "error" not in top and "would_refuse" in top:
        refusal = _mapping(top["would_refuse"], "would_refuse")
        reason, message = refusal.get("reason"), refusal.get("message")
        details = refusal.get("details")
        if reason not in _EXIT20_REASONS or not isinstance(message, str):
            raise _Mismatch("unknown refusal")
        if details is not None and not isinstance(details, dict):
            raise _Mismatch("details is not an object")
        return error_outcome(
            "refused",
            cut_bytes(message, MAX_MESSAGE_BYTES),
            reason=reason,
            details=details,
            lower_error={
                "code": "refused",
                "reason": reason,
                "message": message,
                "details": details,
            },
        )
    error = _mapping(top.get("error"), "error")
    code, message = error.get("code"), error.get("message")
    if not isinstance(code, str) or not isinstance(message, str):
        raise _Mismatch("error has no code and message")
    if code not in allowed or CODE_EXIT.get(code) != exit_code:
        raise _Mismatch("error code does not fit the exit status")
    reason = error.get("reason")
    if code == "refused" and reason not in _EXIT20_REASONS:
        raise _Mismatch("unknown refusal")
    if code != "refused":
        reason = None
    details = error.get("details")
    if details is not None and not isinstance(details, dict):
        raise _Mismatch("details is not an object")
    fields: dict[str, Any] = {
        "reason": reason,
        "details": details,
        "retry_after": error.get("retry_after"),
        "lower_error": error,
        "backend": backend,
        "from_forgectl": code == "audit_failed",
    }
    if code == "escalated":
        fields["escalated_by"] = "forgectl"
        fields["escalated_hooks"] = error.get("escalated_hooks")
    decision_ids = error.get("decision_ids")
    if decision_ids is not None:
        fields["decision_ids"] = decision_ids
    # ValueError and TypeError from a field the schema forbids become a contract mismatch.
    return error_outcome(code, cut_bytes(message, MAX_MESSAGE_BYTES), **fields)
