"""The error envelope: exit codes, the exit-12 landed/retry table, field rules and goldens."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from pr_comment import errors
from pr_comment.errors import build_error, landed_retry
from tests.schema_harness import load_schemas, validate

SHA = "a" * 40
ENVELOPE_KEYS = {
    "code",
    "message",
    "reason",
    "details",
    "retry_after",
    "landed",
    "retry",
    "lower_error",
    "hook",
    "hook_stderr",
    "escalated_by",
    "escalated_hooks",
    "decision_ids",
}

# Every error.code the schema lists, with the exit code docs/interface.md assigns it.
EXPECTED_EXIT = {
    "internal": 1,
    "usage": 2,
    "config_invalid": 2,
    "invalid_pr": 2,
    "invalid_draft": 2,
    "invalid_text": 2,
    "unsupported_draft_version": 2,
    "draft_pr_mismatch": 2,
    "approve_conflicts_event": 2,
    "body_too_large": 2,
    "not_found": 10,
    "vetoed": 11,
    "tool_missing": 12,
    "lower_contract_mismatch": 12,
    "audit_unavailable": 12,
    "audit_failed": 12,
    "state_unsafe": 12,
    "invalid_position": 12,
    "outcome_unknown": 12,
    "outcome_partial": 12,
    "quick_action_ran": 12,
    "approved_stale_head": 12,
    "auth_required": 12,
    "permission_denied": 12,
    "rate_limited": 12,
    "upstream_error": 12,
    "network_error": 12,
    "host_rejected": 12,
    "unsupported_host_version": 12,
    "wrong_host_response": 12,
    "confirmation_required": 13,
    "escalated": 13,
    "refused": 20,
}


def _details_for(code: str) -> dict[str, Any] | None:
    """The details the schema requires for a code (None when it requires none)."""
    return {
        "invalid_draft": {"errors": [{"pointer": "/body", "message": "bad"}]},
        "invalid_text": {"pointer": "/body", "kind": "control_character"},
        "unsupported_draft_version": {"supported": [1]},
        "draft_pr_mismatch": {"draft_pr": "github.com/example/r#1", "pr": "github.com/example/r#2"},
        "lower_contract_mismatch": {"command": "show", "lower_version": None},
        "invalid_position": {
            "invalid": [
                {"index": 0, "path": "a.py", "line": 1, "side": "right", "start_line": None}
            ]
        },
        "not_found": {"what": "pr", "id": "x"},
        "audit_failed": {"outcome": {"pr": "x"}},
    }.get(code)


def _build(code: str, **kwargs: Any) -> dict[str, Any]:
    if code == "refused":
        kwargs.setdefault("reason", "not_open")
    if code == "escalated":
        kwargs.setdefault("escalated_by", "pr-comment")
        kwargs.setdefault("escalated_hooks", [{"hook": "pre-post", "index": 0}])
    if code == "outcome_partial":
        kwargs.setdefault("backend", "github")
    if "details" not in kwargs and (details := _details_for(code)) is not None:
        kwargs["details"] = details
    return build_error(code, "boom", **kwargs)


# --- exit codes ------------------------------------------------------------------------------


def test_every_schema_error_code_has_the_documented_exit_code() -> None:
    schema_codes = set(load_schemas()["pr-comment/defs.json"]["$defs"]["error_code"]["enum"])
    assert schema_codes == set(EXPECTED_EXIT)
    assert errors.CODE_EXIT == EXPECTED_EXIT


def test_the_defined_exit_codes_are_exactly_the_documented_set() -> None:
    assert errors.EXIT_CODES == {0, 1, 2, 10, 11, 12, 13, 20}
    assert set(EXPECTED_EXIT.values()) <= errors.EXIT_CODES


def test_refusal_reasons_match_the_schema() -> None:
    schema = set(load_schemas()["pr-comment/defs.json"]["$defs"]["refusal_reason"]["enum"])
    assert errors.REFUSAL_REASONS == schema


def test_an_unknown_code_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown error code"):
        errors.exit_code_for("nope")
    with pytest.raises(ValueError, match="unknown error code"):
        build_error("nope", "x")


# --- the exit-12 landed/retry table ----------------------------------------------------------

SAME = (False, "same_command")

TABLE: list[tuple[str, dict[str, Any], tuple[bool | None, str]]] = [
    *[
        (code, {}, SAME)
        for code in (
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
        )
    ],
    ("host_rejected", {}, (False, "same_command")),
    ("host_rejected", {"details": {"leftover_drafts": 2}}, (False, "none")),
    ("host_rejected", {"details": {"leftover_drafts": 0}}, (False, "none")),
    ("host_rejected", {"details": {"other": 1}}, (False, "same_command")),
    ("lower_contract_mismatch", {}, (False, "same_command")),
    ("lower_contract_mismatch", {"write": (True, "none")}, (True, "none")),
    ("lower_contract_mismatch", {"write": (False, "same_command")}, (False, "same_command")),
    ("audit_failed", {}, (False, "same_command")),
    ("audit_failed", {"write": (True, "none")}, (True, "none")),
    ("audit_failed", {"from_forgectl": True}, (True, "none")),
    ("outcome_unknown", {}, (None, "same_command")),
    (
        "outcome_partial",
        {"backend": "gitlab", "details": {"landed": {"ids": ["n1"], "thread_ids": []}}},
        (True, "same_command"),
    ),
    (
        "outcome_partial",
        {"backend": "gitlab", "details": {"landed": {"ids": [], "thread_ids": ["gl:d:1"]}}},
        (True, "same_command"),
    ),
    (
        "outcome_partial",
        {"backend": "gitlab", "details": {"landed": {"ids": [], "thread_ids": []}}},
        (False, "same_command"),
    ),
    ("outcome_partial", {"backend": "gitlab", "details": {}}, (False, "same_command")),
    ("outcome_partial", {"backend": "gitlab"}, (False, "same_command")),
    (
        "outcome_partial",
        {"backend": "gitlab", "details": {"landed": "yes"}},
        (False, "same_command"),
    ),
    ("outcome_partial", {"backend": "github"}, (False, "new_key")),
    (
        "outcome_partial",
        {"backend": "github", "details": {"landed": {"ids": ["x"]}}},
        (False, "new_key"),
    ),
    ("quick_action_ran", {}, (True, "none")),
    ("approved_stale_head", {}, (True, "none")),
]


@pytest.mark.parametrize(("code", "kwargs", "expected"), TABLE)
def test_exit_twelve_landed_and_retry_follow_the_table(
    code: str, kwargs: dict[str, Any], expected: tuple[bool | None, str]
) -> None:
    assert landed_retry(code, **kwargs) == expected
    error = _build(code, **kwargs)["error"]
    assert (error["landed"], error["retry"]) == expected
    validate("pr-comment/cli/error.json", {"error": error})


def test_the_table_covers_every_exit_twelve_code() -> None:
    covered = {row[0] for row in TABLE}
    assert covered == {code for code, exit_ in EXPECTED_EXIT.items() if exit_ == 12}


def test_outcome_partial_needs_a_known_backend() -> None:
    for backend in (None, "", "bitbucket"):
        with pytest.raises(ValueError, match="backend"):
            landed_retry("outcome_partial", backend=backend)


def test_a_write_with_an_unknown_retry_is_rejected() -> None:
    with pytest.raises(ValueError, match="retry"):
        landed_retry("audit_failed", write=(True, "later"))


@pytest.mark.parametrize("code", [c for c, e in EXPECTED_EXIT.items() if e != 12])
def test_other_exits_have_landed_and_retry_null(code: str) -> None:
    error = _build(code)["error"]
    assert error["landed"] is None
    assert error["retry"] is None
    validate("pr-comment/cli/error.json", {"error": error})


def test_a_schema_violating_write_pair_is_caught_by_the_schema() -> None:
    # The schema is the second line of defense: a hand-made envelope with a wrong pair fails.
    bad = _build("tool_missing")
    bad["error"]["retry"] = "none"
    with pytest.raises(Exception, match="same_command"):
        validate("pr-comment/cli/error.json", bad)


# --- field rules -----------------------------------------------------------------------------


def test_every_envelope_carries_all_thirteen_fields() -> None:
    for code in EXPECTED_EXIT:
        error = _build(code)["error"]
        assert set(error) == ENVELOPE_KEYS, code


def test_exit_twenty_is_always_refused_with_a_reason() -> None:
    for reason in sorted(errors.REFUSAL_REASONS):
        details = {"head_sha": SHA} if reason == "stale_head" else None
        if reason == "stale_base":
            details = {"base_ref": "main"}
        if reason == "plan_changed":
            details = {"plan_sha256": "b" * 64}
        error = build_error("refused", "no", reason=reason, details=details)["error"]
        assert error["code"] == "refused"
        assert error["reason"] == reason
        validate("pr-comment/cli/error.json", {"error": error})
    assert errors.exit_code_for("refused") == 20


@pytest.mark.parametrize("reason", [None, "", "Stale_Head", "made_up"])
def test_refused_without_a_valid_reason_is_rejected(reason: str | None) -> None:
    with pytest.raises(ValueError, match="reason"):
        build_error("refused", "no", reason=reason)


def test_a_reason_on_any_other_code_is_rejected() -> None:
    with pytest.raises(ValueError, match="reason"):
        build_error("usage", "x", reason="stale_head")


def test_escalated_needs_its_fields_and_only_it_may_have_them() -> None:
    with pytest.raises(ValueError, match="escalated"):
        build_error("escalated", "x")
    with pytest.raises(ValueError, match="escalated"):
        build_error("escalated", "x", escalated_by="somebody", escalated_hooks=[{"hook": "p"}])
    with pytest.raises(ValueError, match="escalated"):
        build_error("vetoed", "x", escalated_by="pr-comment")


def test_hook_stderr_is_cut_to_4096_bytes_on_a_character_boundary() -> None:
    text = "é" * 3000  # 2 bytes each
    error = build_error("vetoed", "x", hook="pre-post", hook_stderr=text)["error"]
    cut = error["hook_stderr"]
    assert len(cut.encode("utf-8")) <= 4096
    assert len(cut.encode("utf-8")) >= 4094
    assert text.startswith(cut)
    validate("pr-comment/cli/error.json", {"error": error})


def test_hook_stderr_of_exactly_the_limit_is_kept_and_one_over_is_cut() -> None:
    at = build_error("vetoed", "x", hook="pre-post", hook_stderr="a" * 4096)["error"]
    over = build_error("vetoed", "x", hook="pre-post", hook_stderr="a" * 4097)["error"]
    assert at["hook_stderr"] == "a" * 4096
    assert over["hook_stderr"] == "a" * 4096


def test_hook_stderr_with_a_lone_surrogate_stays_encodable() -> None:
    error = build_error("vetoed", "x", hook="pre-post", hook_stderr="a\ud800b")["error"]
    error["hook_stderr"].encode("utf-8")


def test_a_hook_without_stderr_gets_an_empty_string() -> None:
    error = build_error("vetoed", "x", hook="pre-post")["error"]
    assert error["hook_stderr"] == ""


def test_hook_rules() -> None:
    with pytest.raises(ValueError, match="hook"):
        build_error("usage", "x", hook="pre-post")
    with pytest.raises(ValueError, match="hook"):
        build_error("vetoed", "x", hook="post-post")
    with pytest.raises(ValueError, match="hook_stderr"):
        build_error("vetoed", "x", hook_stderr="orphan")


def test_the_message_is_sanitized_and_the_details_are_copied() -> None:
    details = {"head_sha": SHA}
    error = build_error("refused", "a\nb‮", reason="stale_head", details=details)["error"]
    assert error["message"] == "a<U+000A>b<U+202E>"
    details["head_sha"] = "changed"
    assert error["details"] == {"head_sha": SHA}


def test_retry_after_lower_error_and_decision_ids_pass_through() -> None:
    lower = {"error": {"code": "rate_limited", "message": "slow down"}}
    error = build_error(
        "rate_limited",
        "slow",
        retry_after=30,
        lower_error=lower,
        decision_ids=["d1", "d2"],
    )["error"]
    assert (error["retry_after"], error["lower_error"]) == (30, lower)
    assert error["decision_ids"] == ["d1", "d2"]
    validate("pr-comment/cli/error.json", {"error": error})


def test_decision_ids_default_to_an_empty_list() -> None:
    assert build_error("usage", "x")["error"]["decision_ids"] == []


# --- goldens: one envelope per exit code -----------------------------------------------------

GOLDEN_CASES: list[tuple[str, Callable[[], dict[str, Any]]]] = [
    ("exit-01-internal", lambda: build_error("internal", "unexpected RuntimeError")),
    ("exit-02-usage", lambda: build_error("usage", "unrecognized arguments: --nope")),
    (
        "exit-02-invalid-draft",
        lambda: build_error(
            "invalid_draft", "draft is invalid", details=_details_for("invalid_draft")
        ),
    ),
    (
        "exit-10-not-found",
        lambda: build_error("not_found", "no such PR", details={"what": "pr", "id": "x#1"}),
    ),
    (
        "exit-11-vetoed",
        lambda: build_error(
            "vetoed", "hook vetoed the post", hook="pre-post", hook_stderr="no weekends"
        ),
    ),
    (
        "exit-12-rate-limited",
        lambda: build_error(
            "rate_limited",
            "rate limited",
            retry_after=60,
            lower_error={"error": {"code": "rate_limited", "message": "slow down"}},
        ),
    ),
    (
        "exit-12-host-rejected-leftover",
        lambda: build_error(
            "host_rejected", "host rejected the review", details={"leftover_drafts": 1}
        ),
    ),
    (
        "exit-12-outcome-partial-gitlab",
        lambda: build_error(
            "outcome_partial",
            "approval failed after the note",
            backend="gitlab",
            details={"landed": {"ids": ["n1"], "thread_ids": []}},
        ),
    ),
    (
        "exit-12-outcome-partial-github",
        lambda: build_error("outcome_partial", "partial", backend="github"),
    ),
    (
        "exit-12-outcome-unknown",
        lambda: build_error("outcome_unknown", "the call timed out", decision_ids=["d1"]),
    ),
    (
        "exit-13-escalated",
        lambda: build_error(
            "escalated",
            "a hook escalated",
            hook="pre-post",
            hook_stderr="needs a human",
            escalated_by="pr-comment",
            escalated_hooks=[{"hook": "pre-post", "index": 1}],
        ),
    ),
    ("exit-13-confirmation-required", lambda: build_error("confirmation_required", "pass --yes")),
    (
        "exit-20-stale-head",
        lambda: build_error(
            "refused", "head moved", reason="stale_head", details={"head_sha": SHA}
        ),
    ),
]


@pytest.mark.parametrize(("name", "make"), GOLDEN_CASES, ids=[name for name, _ in GOLDEN_CASES])
def test_golden_envelopes_validate_against_the_error_schema(
    golden_json: Callable[[str, Any], None], name: str, make: Callable[[], dict[str, Any]]
) -> None:
    document = make()
    validate("pr-comment/cli/error.json", document)
    golden_json(f"errors/{name}.json", document)


def test_the_goldens_cover_every_exit_code_but_zero() -> None:
    exits = {errors.exit_code_for(make()["error"]["code"]) for _, make in GOLDEN_CASES}
    assert exits == errors.EXIT_CODES - {0}
