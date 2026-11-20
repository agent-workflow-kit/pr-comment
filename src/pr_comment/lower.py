"""What a forgectl result means to pr-comment (docs/interface.md: Exit codes, Error envelope,
forgectl edge).

``require_ok`` returns the parsed JSON document of an exit 0, or raises ``LowerFailure`` carrying
the ``Outcome`` pr-comment ends with. ``failure_outcome`` maps any other result: forgectl's exit
code, ``code``, ``reason``, ``details`` and ``retry_after`` are copied, its envelope goes in
``lower_error`` (for a dry-run ``would_refuse``, forgectl's whole top document), and anything
pr-comment does not know (an exit code, a code, a reason, an ill-typed field, output that is not
UTF-8 JSON) is ``lower_contract_mismatch``. forgectl's text is never put in a message:
``message`` is forgectl's own on a passed-through error, otherwise a fixed sentence.

``ForgectlInterrupted`` (a signal during a call, also during ``lower_version``) is never caught
here: it reaches ``cli.main``, or the write path that maps it to ``outcome_unknown``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

from .errors import CODE_EXIT, cut_bytes, sanitize
from .gateway import (
    INSTALL_COMMAND,
    ForgectlResult,
    Gateway,
    ToolMissing,
)
from .output import Outcome, error_outcome

MAX_ENVELOPE_BYTES: Final = 1024 * 1024
MAX_MESSAGE_BYTES: Final = 4096
MAX_VERSION_CHARS: Final = 64

_EXIT2_PASSTHROUGH: Final = frozenset({"body_too_large", "invalid_pr", "config_invalid"})
# forgectl's exit-12 codes pr-comment passes through (its own codes are never taken from forgectl:
# ``tool_missing``, ``audit_unavailable``, ``state_unsafe`` and ``lower_contract_mismatch`` are
# pr-comment's, docs/interface.md: Exit codes). ``invalid_position`` is kept: pr-comment's own is
# built from forgectl's real-call answer and carries forgectl's ``details.invalid``.
_EXIT12_PASSTHROUGH: Final = frozenset(
    {
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
# forgectl's own ``tool_missing`` (gh or glab absent, ``details.install``) passes through on a host
# read only: a read gives ``codes``, a write never lists it, so there it stays pr-comment's own.
_EXIT12_READ_ONLY: Final = frozenset({"tool_missing"})
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
    def __init__(self, why: str, lower_error: Mapping[str, Any] | None = None) -> None:
        super().__init__(why)
        self.lower_error = lower_error


class MissingBackendError(ValueError):
    """A write failure that needs ``show``'s backend was lowered without one (a caller bug)."""


def tool_missing_outcome(exc: ToolMissing) -> Outcome:
    """Exit 12 ``tool_missing`` with the install command."""
    return error_outcome("tool_missing", exc.message, details={"install": INSTALL_COMMAND})


_FROM_GATEWAY: Final = object()


def mismatch_outcome(
    command: str,
    why: str,
    *,
    gateway: Gateway,
    write: tuple[bool | None, str] | None = None,
    lower_error: Mapping[str, Any] | None = None,
    version_text: str | None | object = _FROM_GATEWAY,
) -> Outcome:
    """Exit 12 ``lower_contract_mismatch``; ``write`` only when a write may have happened.

    ``version_text`` replaces the gateway's lower version in ``details.lower_version`` (None
    leaves it null); it is display-sanitized and cut here. Left out, the gateway is read.

    Reads the lower version through the gateway, so a signal during that read raises
    ``ForgectlInterrupted`` out of here; callers must not catch it.
    """
    shown = gateway.lower_version() if version_text is _FROM_GATEWAY else version_text
    assert shown is None or isinstance(shown, str)
    return error_outcome(
        "lower_contract_mismatch",
        f"forgectl {command} gave an answer pr-comment cannot use ({why})",
        details={"command": command, "lower_version": _display_version(shown)},
        write=write,
        lower_error=lower_error,
    )


def _display_version(version: str | None) -> str | None:
    """The version text as ``details.lower_version``: display-sanitized (whatever the gateway
    did), cut to 64 characters, or None when nothing is left."""
    if version is None:
        return None
    kept: list[str] = []
    size = 0
    for char in version:
        piece = sanitize(char)  # one character gives a character or one whole ``<U+XXXX>`` marker
        if size + len(piece) > MAX_VERSION_CHARS:
            break
        kept.append(piece)
        size += len(piece)
    return cut_bytes("".join(kept)) or None


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """An object with a repeated key is ambiguous (parsers disagree on which wins): a mismatch."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


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
        return json.loads(text, parse_constant=_reject, object_pairs_hook=_no_duplicates)
    except (ValueError, RecursionError):
        raise _Mismatch("output is not JSON") from None


def _unknown_write(result: ForgectlResult) -> tuple[bool | None, str] | None:
    return (None, "same_command") if result.write else None


def require_ok(
    result: ForgectlResult,
    *,
    gateway: Gateway,
    backend: str | None = None,
    codes: frozenset[str] | None = None,
    reasons: frozenset[str] | None = None,
) -> Any:
    """The JSON document (an object) of a successful call; ``LowerFailure`` otherwise.

    ``codes`` and ``reasons`` narrow what a failure may pass through (see ``failure_outcome``).

    Exit 0 with output that is not one JSON object is ``lower_contract_mismatch``; so is a
    forgectl that exits 0 and left stdout cut at the limit.
    """
    if result.timed_out or result.exit_code != 0:
        raise LowerFailure(
            failure_outcome(result, gateway=gateway, backend=backend, codes=codes, reasons=reasons)
        )
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
    result: ForgectlResult,
    *,
    gateway: Gateway,
    backend: str | None = None,
    codes: frozenset[str] | None = None,
    reasons: frozenset[str] | None = None,
) -> Outcome:
    """The outcome of a forgectl call that did not succeed (or succeeded with a bad answer).

    ``backend`` is ``show``'s, needed to read an ``outcome_partial``: a write failure whose
    envelope is one without a backend raises ``MissingBackendError`` (a caller bug, never a
    contract mismatch).

    ``codes`` (error codes) and ``reasons`` (``refused`` reasons) are what the command can give:
    a read never answers ``vetoed``, ``outcome_partial`` or ``stale_head``, so a forgectl that
    does is a contract mismatch, not a passthrough (and no backend is needed to say so).
    ``None`` means no narrowing.
    """
    command = result.command
    if result.timed_out:
        if result.write:
            # The deadline kill is conservative: whatever forgectl left (``result.exit_code`` and
            # output are kept), the write's outcome is unknown. A parseable envelope it printed
            # before it ended is kept in ``lower_error`` for the human.
            return error_outcome(
                "outcome_unknown",
                f"forgectl {command} did not finish in time; whether it posted is unknown",
                lower_error=_envelope_error(result),
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
    if allowed is not None and result.exit_code == 12 and codes is not None:
        allowed = allowed | (_EXIT12_READ_ONLY & codes)
    if allowed is None:
        return mismatch_outcome(
            command, f"unknown exit status {result.exit_code}", gateway=gateway, write=write
        )
    try:
        document = _load_json(result, MAX_ENVELOPE_BYTES)
        return _passthrough(result, document, allowed, backend, codes, reasons)
    except MissingBackendError:
        raise
    except (_Mismatch, ValueError, TypeError) as exc:
        # A malformed ``leftover_drafts`` on ``host_rejected`` also lands here: no allowed pair
        # means "none" with an unknown landed, so it is (None, same_command). todo(#39)
        why = str(exc) if isinstance(exc, _Mismatch) else "malformed error envelope"
        lower_error = exc.lower_error if isinstance(exc, _Mismatch) else None
        return mismatch_outcome(command, why, gateway=gateway, write=write, lower_error=lower_error)


def _envelope_error(result: ForgectlResult) -> dict[str, Any] | None:
    """forgectl's ``error`` object from a result, when its stdout holds a parseable envelope."""
    try:
        document = _load_json(result, MAX_ENVELOPE_BYTES)
    except _Mismatch:
        return None
    if isinstance(document, dict):
        error: object = document.get("error")  # pyright: ignore[reportUnknownMemberType]
        if isinstance(error, dict):
            return error  # pyright: ignore[reportUnknownVariableType]
    return None


def partial_document(result: ForgectlResult) -> dict[str, Any] | None:
    """The ``{hosts, errors}`` document of a ``whoami`` that exited 12 ``partial_results``.

    That exit prints the normal object (``hosts`` and ``errors``, no ``error`` key) and the code
    on stderr only, so ``failure_outcome`` reads it as a mismatch. A caller that can use some
    hosts reads it here: None unless the exit is 12 and the output is an object with a ``hosts``
    list and an ``errors`` list and no ``error`` key.
    """
    if result.exit_code != 12 or result.timed_out or result.stdout_truncated:
        return None
    try:
        document = _load_json(result, None)
    except _Mismatch:
        return None
    if not isinstance(document, dict) or "error" in document:
        return None
    hosts: object = document.get("hosts")  # pyright: ignore[reportUnknownMemberType]
    errors: object = document.get("errors")  # pyright: ignore[reportUnknownMemberType]
    if not isinstance(hosts, list) or not isinstance(errors, list):
        return None
    return document  # pyright: ignore[reportUnknownVariableType]


def _mapping(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _Mismatch(f"{what} is not an object")
    return value  # pyright: ignore[reportUnknownVariableType]


def _passthrough(
    result: ForgectlResult,
    document: object,
    allowed: frozenset[str],
    backend: str | None,
    codes: frozenset[str] | None,
    reasons: frozenset[str] | None,
) -> Outcome:
    top = _mapping(document, "output")
    exit_code = result.exit_code
    if exit_code == 20 and "error" not in top and "would_refuse" in top:
        refusal = _mapping(top["would_refuse"], "would_refuse")
        reason, message = refusal.get("reason"), refusal.get("message")
        details = refusal.get("details")
        if reason not in _EXIT20_REASONS or not isinstance(message, str):
            raise _Mismatch("unknown refusal")
        if codes is not None:  # a read never runs with --dry-run
            raise _Mismatch("dry-run refusal on a read", top)
        if reasons is not None and reason not in reasons:
            raise _Mismatch("refusal this command cannot give", top)
        if details is not None and not isinstance(details, dict):
            raise _Mismatch("details is not an object")
        return error_outcome(
            "refused",
            cut_bytes(message, MAX_MESSAGE_BYTES),
            reason=reason,
            details=details,
            lower_error=top,
        )
    error = _mapping(top.get("error"), "error")
    code, message = error.get("code"), error.get("message")
    if not isinstance(code, str) or not isinstance(message, str):
        raise _Mismatch("error has no code and message")
    if code not in allowed or CODE_EXIT.get(code) != exit_code:
        raise _Mismatch("error code does not fit the exit status", error)
    reason = error.get("reason")
    if code == "refused" and reason not in _EXIT20_REASONS:
        raise _Mismatch("unknown refusal")
    if codes is not None and code not in codes:
        raise _Mismatch("error code this command cannot give", error)
    if code == "refused" and reasons is not None and reason not in reasons:
        raise _Mismatch("refusal this command cannot give", error)
    if code != "refused":
        reason = None
    details = error.get("details")
    if details is not None and not isinstance(details, dict):
        raise _Mismatch("details is not an object")
    if code == "outcome_partial" and backend not in ("github", "gitlab"):
        raise MissingBackendError("outcome_partial needs show's backend (github or gitlab)")
    fields: dict[str, Any] = {
        "reason": reason,
        "details": details,
        "retry_after": error.get("retry_after"),
        "lower_error": error,
        "backend": backend,
    }
    if code == "audit_failed":
        # forgectl's details.outcome is an enum string, not pr-comment's own result document: it
        # picks the pair and stays in lower_error only.
        fields["from_forgectl"] = True
        fields["write"] = _audit_failed_pair(details)
        fields["details"] = None
    if code == "escalated":
        fields["escalated_by"] = "forgectl"
        fields["escalated_hooks"] = error.get("escalated_hooks")
    decision_ids = error.get("decision_ids")
    if decision_ids is not None:
        fields["decision_ids"] = decision_ids
    # ValueError and TypeError from a field the schema forbids become a contract mismatch.
    return error_outcome(code, cut_bytes(message, MAX_MESSAGE_BYTES), **fields)


_AUDIT_LANDED: Final = frozenset(
    {"posted", "approved", "resolved", "unresolved", "quick_action_ran"}
)


def _audit_failed_pair(details: Mapping[str, Any] | None) -> tuple[bool | None, str]:
    """``(landed, retry)`` of forgectl's ``audit_failed`` from its ``details.outcome`` (the
    audit result of the write): landed ones are done, ``failed`` did not land, anything else
    (``outcome_unknown``, ``attempted``, a missing or unknown value) is unknown."""
    outcome = details.get("outcome") if details is not None else None
    if isinstance(outcome, str):
        if outcome in _AUDIT_LANDED:
            return True, "none"
        if outcome == "failed":
            return False, "same_command"
    return None, "same_command"
