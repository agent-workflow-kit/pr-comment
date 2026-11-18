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
    if code == "audit_failed" and not kwargs.get("from_forgectl"):
        kwargs.setdefault("write", (True, "none"))
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


def test_a_hand_made_envelope_with_a_wrong_pair_fails_the_schema() -> None:
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


def test_hook_stderr_with_a_lone_surrogate_is_replaced_not_dropped() -> None:
    error = build_error("vetoed", "x", hook="pre-post", hook_stderr="a\ud800b")["error"]
    assert error["hook_stderr"] == "a?b"


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


def test_the_message_is_kept_raw_in_the_envelope_and_the_details_are_copied() -> None:
    details = {"head_sha": SHA}
    error = build_error("refused", "a\nb‮", reason="stale_head", details=details)["error"]
    assert error["message"] == "a\nb‮"
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


# --- hardening (review round 1) ---------------------------------------------------------------


def test_own_audit_failed_without_a_write_is_rejected() -> None:
    with pytest.raises(ValueError, match="write"):
        landed_retry("audit_failed")
    with pytest.raises(ValueError, match="write"):
        build_error("audit_failed", "x", details=_details_for("audit_failed"))


def test_lower_contract_mismatch_without_a_write_is_before_any_write() -> None:
    assert landed_retry("lower_contract_mismatch") == (False, "same_command")


@pytest.mark.parametrize(
    "write",
    [
        (True, "same_command"),
        (False, "none"),
        (None, "none"),
        (None, "new_key"),
        (True, "new_key"),
        (1, "none"),
        (0, "same_command"),
        ("true", "none"),
        (True, "later"),
        (True,),
        True,
        [True, "none"],
        (True, None),
    ],
)
def test_a_write_pair_outside_the_four_allowed_is_rejected(write: Any) -> None:
    with pytest.raises(ValueError):
        landed_retry("audit_failed", write=write)
    with pytest.raises(ValueError):
        landed_retry("lower_contract_mismatch", write=write)


@pytest.mark.parametrize("write", [(True, "none"), (False, "same_command"), (False, "new_key")])
def test_the_allowed_write_pairs_pass_through(write: tuple[bool, str]) -> None:
    assert landed_retry("lower_contract_mismatch", write=write) == write
    assert landed_retry("audit_failed", write=write) == write
    assert landed_retry("audit_failed", write=(None, "same_command")) == (None, "same_command")


@pytest.mark.parametrize("code", ["tool_missing", "host_rejected", "usage", "outcome_unknown"])
def test_write_and_from_forgectl_are_rejected_for_codes_that_do_not_use_them(code: str) -> None:
    with pytest.raises(ValueError, match="write"):
        landed_retry(code, write=(True, "none"))
    with pytest.raises(ValueError, match="from_forgectl"):
        landed_retry(code, from_forgectl=True)


def test_from_forgectl_is_only_for_audit_failed_and_excludes_write() -> None:
    with pytest.raises(ValueError, match="from_forgectl"):
        landed_retry("lower_contract_mismatch", from_forgectl=True)
    with pytest.raises(ValueError, match="not both"):
        landed_retry("audit_failed", from_forgectl=True, write=(True, "none"))


def test_a_forgectl_escalation_cannot_name_a_pr_comment_hook() -> None:
    hooks = [{"hook": "pre-post", "index": 0}]
    with pytest.raises(ValueError, match="forgectl"):
        build_error(
            "escalated", "x", escalated_by="forgectl", escalated_hooks=hooks, hook="pre-post"
        )
    with pytest.raises(ValueError, match="forgectl"):
        build_error(
            "escalated",
            "x",
            escalated_by="forgectl",
            escalated_hooks=hooks,
            hook="pre-post",
            hook_stderr="e",
        )
    ok = build_error("escalated", "x", escalated_by="forgectl", escalated_hooks=hooks)["error"]
    assert ok["hook"] is None


@pytest.mark.parametrize("value", [None, "2", 1.5, True, -1])
def test_leftover_drafts_must_be_a_non_negative_integer(value: Any) -> None:
    with pytest.raises(ValueError, match="leftover_drafts"):
        landed_retry("host_rejected", details={"leftover_drafts": value})


def test_leftover_drafts_presence_means_no_retry_even_at_zero() -> None:
    assert landed_retry("host_rejected", details={"leftover_drafts": 0}) == (False, "none")


@pytest.mark.parametrize(
    "details",
    [
        None,
        {},
        {"landed": "yes"},
        {"landed": None},
        {"landed": {"ids": "n1", "thread_ids": []}},
        {"landed": {"ids": [], "thread_ids": "t"}},
    ],
)
def test_outcome_partial_on_gitlab_needs_a_well_formed_landed(details: Any) -> None:
    with pytest.raises(ValueError, match="landed|list"):
        landed_retry("outcome_partial", backend="gitlab", details=details)


@pytest.mark.parametrize("bad", [True, False, -1, float("nan"), float("inf"), "5", [5]])
def test_retry_after_must_be_a_finite_non_negative_number(bad: Any) -> None:
    with pytest.raises(ValueError, match="retry_after"):
        build_error("rate_limited", "x", retry_after=bad)


@pytest.mark.parametrize("ok", [None, 0, 0.5, 30])
def test_retry_after_accepts_plain_numbers(ok: Any) -> None:
    assert build_error("rate_limited", "x", retry_after=ok)["error"]["retry_after"] == ok


@pytest.mark.parametrize(
    "ids",
    ["d1", [1], ["bad id"], [""], ["a" * 129], ["d"] * 33, None, [None]],
)
def test_decision_ids_are_validated_against_the_grammar(ids: Any) -> None:
    with pytest.raises(ValueError, match="decision_ids"):
        build_error("usage", "x", decision_ids=ids)


def test_decision_ids_at_the_limits_are_accepted() -> None:
    ids = ["a" * 128] + ["d"] * 31
    assert build_error("usage", "x", decision_ids=ids)["error"]["decision_ids"] == ids


@pytest.mark.parametrize(
    "hooks",
    [
        "pre-post",
        [{"hook": "pre-post"}],
        [{"hook": "pre-post", "index": -1}],
        [{"hook": "pre-post", "index": True}],
        [{"hook": "pre-post", "index": "0"}],
        [{"hook": 1, "index": 0}],
        [{"hook": "pre-post", "index": 0, "extra": 1}],
        ["pre-post"],
    ],
)
def test_escalated_hooks_entries_are_validated(hooks: Any) -> None:
    with pytest.raises(ValueError, match="escalated"):
        build_error("escalated", "x", escalated_by="pr-comment", escalated_hooks=hooks)


@pytest.mark.parametrize(
    ("kwargs", "exc"),
    [
        ({"message": 5}, TypeError),
        ({"message": None}, TypeError),
        ({"message": b"x"}, TypeError),
        ({"details": [1]}, TypeError),
        ({"details": "x"}, TypeError),
        ({"lower_error": 5}, TypeError),
        ({"reason": ["x"]}, TypeError),
    ],
)
def test_build_error_rejects_wrongly_typed_fields_with_a_clear_error(
    kwargs: dict[str, Any], exc: type[Exception]
) -> None:
    args: dict[str, Any] = {"message": "m", **kwargs}
    message = args.pop("message")
    with pytest.raises(exc):
        build_error("usage", message, **args)


def test_hook_stderr_must_be_text() -> None:
    with pytest.raises(TypeError, match="hook_stderr"):
        build_error("vetoed", "x", hook="pre-post", hook_stderr=b"x")  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("code", [[], {}, None, 5])
def test_an_unhashable_or_non_string_code_is_a_value_error(code: Any) -> None:
    with pytest.raises(ValueError, match="unknown error code"):
        build_error(code, "x")
    with pytest.raises(ValueError, match="unknown error code"):
        errors.exit_code_for(code)
    with pytest.raises(TypeError, match="reason"):
        build_error("refused", "x", reason=[])  # pyright: ignore[reportArgumentType]


def test_cut_bytes_and_sanitize_reject_non_strings() -> None:
    with pytest.raises(TypeError, match="str"):
        errors.cut_bytes(b"x")  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError, match="str"):
        errors.sanitize(None)  # pyright: ignore[reportArgumentType]


def test_cut_bytes_truncates_silently_without_a_marker() -> None:
    cut = errors.cut_bytes("a" * 5000)
    assert cut == "a" * 4096


def test_the_envelope_does_not_alias_its_inputs() -> None:
    details = {"errors": [{"pointer": "/b", "message": "m"}]}
    lower = {"error": {"details": {"n": [1]}}}
    hooks = [{"hook": "pre-post", "index": 0}]
    error = build_error(
        "escalated",
        "x",
        escalated_by="pr-comment",
        escalated_hooks=hooks,
        details=details,
        lower_error=lower,
    )["error"]
    details["errors"][0]["message"] = "changed"
    lower["error"]["details"]["n"].append(2)
    hooks[0]["index"] = 9
    assert error["details"]["errors"][0]["message"] == "m"
    assert error["lower_error"]["error"]["details"]["n"] == [1]
    assert error["escalated_hooks"] == [{"hook": "pre-post", "index": 0}]


def test_the_code_and_landed_tables_are_read_only() -> None:
    with pytest.raises(TypeError):
        errors.CODE_EXIT["usage"] = 99  # type: ignore[index]  # pyright: ignore[reportIndexIssue]
    with pytest.raises(TypeError):
        errors._FIXED_LANDED_RETRY["tool_missing"] = (True, "none")  # type: ignore[index]  # pyright: ignore[reportIndexIssue, reportPrivateUsage]
    assert errors.exit_code_for("usage") == 2
    assert landed_retry("tool_missing") == (False, "same_command")


def test_sanitize_marks_every_category_the_interface_lists() -> None:
    cases = {
        "\x00": "<U+0000>",
        "\x85": "<U+0085>",
        "\u200b": "<U+200B>",  # Cf
        "\ud800": "<U+D800>",  # Cs
        "\ue000": "<U+E000>",  # Co
        "\u0378": "<U+0378>",  # Cn (unassigned)
        "\u2028": "<U+2028>",  # Zl
        "\u2029": "<U+2029>",  # Zp
        "\ufe0f": "<U+FE0F>",  # variation selector
        "\U000e0100": "<U+E0100>",  # variation selector supplement
        "\u034f": "<U+034F>",
        "\u17b4": "<U+17B4>",
        "\u17b5": "<U+17B5>",
        "\u180b": "<U+180B>",
        "\u180f": "<U+180F>",
        "\u115f": "<U+115F>",
        "\u1160": "<U+1160>",
        "\u3164": "<U+3164>",
        "\uffa0": "<U+FFA0>",
        "\u2800": "<U+2800>",
    }
    for char, marked in cases.items():
        assert errors.sanitize(char) == marked, hex(ord(char))
    assert errors.sanitize("plain é中 ok") == "plain é中 ok"


def test_sanitize_keeps_tab_and_lf_only_for_multi_line_text() -> None:
    assert errors.sanitize("a\tb\nc", one_line=False) == "a\tb\nc"
    assert errors.sanitize("a\tb\nc") == "a<U+0009>b<U+000A>c"
    assert errors.sanitize("a\rb", one_line=False) == "a<U+000D>b"


@pytest.mark.parametrize("code", sorted(EXPECTED_EXIT))
def test_minimal_schema_valid_details_build_a_schema_valid_envelope_for_every_code(
    code: str,
) -> None:
    document = _build(code)
    validate("pr-comment/cli/error.json", document)
    assert errors.exit_code_for(document["error"]["code"]) == EXPECTED_EXIT[code]
