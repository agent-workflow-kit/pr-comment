"""How forgectl results map to pr-comment results and errors (docs/interface.md: Exit codes,
Error envelope)."""

from __future__ import annotations

import json
import os
import signal
from typing import Any

import pytest

from pr_comment import cli
from pr_comment.errors import exit_code_for
from pr_comment.gateway import (
    ForgectlInterrupted,
    ForgectlResult,
    GatewayUsageError,
    ToolMissing,
)
from pr_comment.lower import (
    LowerFailure,
    failure_outcome,
    mismatch_outcome,
    partial_document,
    require_ok,
    tool_missing_outcome,
)
from pr_comment.output import check_outcome
from tests.fake_gateway import FakeGateway, ok

GATEWAY = FakeGateway(version="forgectl 0.1.0")


def result(
    exit_code: int | None,
    document: Any = None,
    *,
    write: bool = False,
    raw: bytes | None = None,
    **fields: Any,
) -> ForgectlResult:
    stdout = (
        raw if raw is not None else (b"" if document is None else json.dumps(document).encode())
    )
    return ForgectlResult(
        command="review", write=write, exit_code=exit_code, stdout=stdout, **fields
    )


def envelope(code: str, message: str = "m", **extra: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, **extra}}


def outcome_of(res: ForgectlResult, **kw: Any) -> Any:
    outcome = failure_outcome(res, gateway=GATEWAY, **kw)
    check_outcome(outcome)
    return outcome


def error_of(outcome: Any) -> dict[str, Any]:
    return outcome.document["error"]


def test_require_ok_returns_the_document() -> None:
    assert require_ok(ok("show", {"pr": "x"}), gateway=GATEWAY) == {"pr": "x"}


@pytest.mark.parametrize(("write", "code"), [(False, "network_error"), (True, "outcome_unknown")])
def test_require_ok_treats_exit_zero_with_a_fired_deadline_as_a_failure(
    write: bool, code: str
) -> None:
    res = result(0, {"pr": "x"}, write=write, timed_out=True)
    with pytest.raises(LowerFailure) as info:
        require_ok(res, gateway=GATEWAY)
    assert error_of(info.value.outcome)["code"] == code


@pytest.mark.parametrize(
    "raw",
    [b"", b"not json", b"[]", b'"s"', b"\xff\xfe{}", b'{"a": NaN}', b"[" * 100000, b"{} {}"],
)
def test_exit_zero_without_a_usable_document_is_a_contract_mismatch(raw: bytes) -> None:
    with pytest.raises(LowerFailure) as info:
        require_ok(result(0, raw=raw), gateway=GATEWAY)
    error = error_of(info.value.outcome)
    assert error["code"] == "lower_contract_mismatch" and info.value.outcome.exit_code == 12
    assert error["details"] == {"command": "review", "lower_version": "forgectl 0.1.0"}
    assert (error["landed"], error["retry"]) == (False, "same_command")  # a read: nothing landed


def test_truncated_success_output_is_a_mismatch_and_a_write_reports_landed() -> None:
    res = result(0, {"a": 1}, write=True, stdout_truncated=True)
    with pytest.raises(LowerFailure) as info:
        require_ok(res, gateway=GATEWAY)
    error = error_of(info.value.outcome)
    assert error["code"] == "lower_contract_mismatch"
    assert (error["landed"], error["retry"]) == (True, "none")  # forgectl said it posted


def test_nonzero_exit_through_require_ok_raises_the_failure() -> None:
    with pytest.raises(LowerFailure) as info:
        require_ok(result(10, envelope("not_found", details={"what": "pr"})), gateway=GATEWAY)
    assert info.value.outcome.exit_code == 10


def test_passthrough_copies_code_reason_details_retry_after_and_whole_envelope() -> None:
    env = envelope("rate_limited", "slow down", retry_after=7, details={"x": 1}, extra="kept")
    outcome = outcome_of(result(12, env))
    error = error_of(outcome)
    assert outcome.exit_code == 12 and error["code"] == "rate_limited"
    assert error["message"] == "slow down" and error["retry_after"] == 7
    assert error["details"] == {"x": 1} and error["lower_error"] == env["error"]
    assert (error["landed"], error["retry"]) == (False, "same_command")


def test_refused_copies_reason_and_details() -> None:
    env = envelope("refused", "head moved", reason="stale_head", details={"head_sha": "a" * 40})
    outcome = outcome_of(result(20, env))
    error = error_of(outcome)
    assert outcome.exit_code == 20 and error["code"] == "refused"
    assert error["reason"] == "stale_head" and error["details"] == {"head_sha": "a" * 40}


def test_dry_run_would_refuse_becomes_a_refusal_with_lower_error() -> None:
    plan = {
        "dry_run": True,
        "would_refuse": {"reason": "too_large", "message": "big", "details": {"n": 3}},
    }
    error = error_of(outcome_of(result(20, plan)))
    assert (error["code"], error["reason"], error["message"]) == ("refused", "too_large", "big")
    assert error["lower_error"] == plan  # forgectl's whole top document, not a synthesized object


@pytest.mark.parametrize("reason", ["plan_changed", "no_head_match", None, 3])
def test_refusal_reasons_pr_comment_does_not_know_are_mismatches(reason: Any) -> None:
    env = envelope("refused", "x", reason=reason)
    assert error_of(outcome_of(result(20, env)))["code"] == "lower_contract_mismatch"
    plan = {"would_refuse": {"reason": reason, "message": "x"}}
    assert error_of(outcome_of(result(20, plan)))["code"] == "lower_contract_mismatch"


@pytest.mark.parametrize("code", ["body_too_large", "invalid_pr", "config_invalid"])
def test_exit_two_passes_only_the_documented_codes(code: str) -> None:
    outcome = outcome_of(result(2, envelope(code, details={"role": None})))
    assert outcome.exit_code == 2 and error_of(outcome)["code"] == code


@pytest.mark.parametrize("code", ["usage", "invalid_draft", "method_required", "nope"])
def test_any_other_exit_two_is_a_mismatch(code: str) -> None:
    outcome = outcome_of(result(2, envelope(code)))
    assert error_of(outcome)["code"] == "lower_contract_mismatch" and outcome.exit_code == 12


@pytest.mark.parametrize(
    ("exit_code", "code"),
    [
        (12, "not_found"),  # code does not fit the exit status
        (12, "state_unsafe"),  # pr-comment's own code, never taken from forgectl
        (12, "lower_contract_mismatch"),
        (12, "partial_results"),  # not an error code
        (10, "vetoed"),
        (13, "refused"),
        (20, "stale_head"),  # a reason in the code field
    ],
)
def test_codes_that_do_not_fit_are_mismatches(exit_code: int, code: str) -> None:
    assert error_of(outcome_of(result(exit_code, envelope(code))))["code"] == (
        "lower_contract_mismatch"
    )


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b"{}",
        b'{"error": 1}',
        b'{"error": {"code": 1, "message": "m"}}',
        b'{"error": {"code": "rate_limited"}}',
        b"\xff",
        json.dumps(envelope("rate_limited", retry_after=-1)).encode(),
        json.dumps(envelope("rate_limited", retry_after="x")).encode(),
        json.dumps(envelope("rate_limited", details=[])).encode(),
        json.dumps(envelope("rate_limited", "x" * 2_000_000)).encode(),
    ],
)
def test_malformed_envelopes_are_mismatches(raw: bytes) -> None:
    assert error_of(outcome_of(result(12, raw=raw)))["code"] == "lower_contract_mismatch"


@pytest.mark.parametrize("exit_code", [3, 4, 14, 21, 126, 127, 255])
def test_unknown_exit_statuses_are_mismatches(exit_code: int) -> None:
    assert error_of(outcome_of(result(exit_code, envelope("internal"))))["code"] == (
        "lower_contract_mismatch"
    )


def test_message_is_cut_but_kept_raw_otherwise() -> None:
    message = "<b>\x1b[31m" + "é" * 5000
    error = error_of(outcome_of(result(12, envelope("upstream_error", message))))
    assert error["message"].startswith("<b>\x1b[31m") and len(error["message"].encode()) <= 4096
    assert error["lower_error"]["message"] == message  # the envelope is carried whole


def test_outcome_partial_needs_the_backend() -> None:
    env = envelope("outcome_partial", details={"landed": {"ids": ["gl:n:1"], "thread_ids": []}})
    gitlab = error_of(outcome_of(result(12, env, write=True), backend="gitlab"))
    assert (gitlab["code"], gitlab["landed"], gitlab["retry"]) == (
        "outcome_partial",
        True,
        "same_command",
    )
    github = error_of(outcome_of(result(12, env, write=True), backend="github"))
    assert (github["landed"], github["retry"]) == (False, "new_key")
    # A write failure that needs the backend, lowered without one, is a caller bug, not a mismatch.
    with pytest.raises(ValueError, match="backend"):
        outcome_of(result(12, env, write=True))
    with pytest.raises(ValueError, match="backend"):
        outcome_of(result(12, env, write=True), backend="bitbucket")


def test_gitlab_outcome_partial_without_details_landed_is_a_mismatch() -> None:
    out = error_of(
        outcome_of(result(12, envelope("outcome_partial"), write=True), backend="gitlab")
    )
    assert out["code"] == "lower_contract_mismatch"
    assert (out["landed"], out["retry"]) == (None, "same_command")


def test_gitlab_outcome_partial_with_malformed_landed_is_a_mismatch() -> None:
    env = envelope("outcome_partial", details={"landed": {"ids": "x"}})
    out = error_of(outcome_of(result(12, env, write=True), backend="gitlab"))
    assert out["code"] == "lower_contract_mismatch"


def test_host_rejected_leftover_drafts_means_no_retry() -> None:
    env = envelope("host_rejected", details={"status": 422, "leftover_drafts": 0})
    error = error_of(outcome_of(result(12, env, write=True)))
    assert (error["landed"], error["retry"]) == (False, "none")
    bad = envelope("host_rejected", details={"leftover_drafts": -1})
    assert error_of(outcome_of(result(12, bad, write=True)))["code"] == "lower_contract_mismatch"


@pytest.mark.parametrize(
    ("forgectl_outcome", "pair"),
    [
        ("posted", (True, "none")),
        ("approved", (True, "none")),
        ("resolved", (True, "none")),
        ("unresolved", (True, "none")),
        ("quick_action_ran", (True, "none")),
        ("failed", (False, "same_command")),
        ("outcome_unknown", (None, "same_command")),
        ("attempted", (None, "same_command")),
        ("outcome_partial", (None, "same_command")),
        ("something_new", (None, "same_command")),
        (3, (None, "same_command")),
        (None, (None, "same_command")),
    ],
)
def test_forgectl_audit_failed_pair_follows_its_outcome(
    forgectl_outcome: Any, pair: tuple[bool | None, str]
) -> None:
    env = envelope("audit_failed", details={"outcome": forgectl_outcome, "result": "x"})
    error = error_of(outcome_of(result(12, env, write=True)))
    assert error["code"] == "audit_failed" and (error["landed"], error["retry"]) == pair
    assert error["lower_error"] == env["error"]  # the whole envelope, forgectl's outcome inside
    assert error["details"] is None  # forgectl's enum string is never copied as our document


def test_forgectl_audit_failed_without_details_is_unknown() -> None:
    error = error_of(outcome_of(result(12, envelope("audit_failed"), write=True)))
    assert (error["landed"], error["retry"]) == (None, "same_command")


@pytest.mark.parametrize("code", ["tool_missing", "audit_unavailable"])
def test_pr_comments_own_exit_12_codes_are_never_taken_from_forgectl(code: str) -> None:
    env = envelope(code, "from forgectl")
    error = error_of(outcome_of(result(12, env)))
    assert error["code"] == "lower_contract_mismatch"
    assert error["lower_error"] == env["error"]  # kept for the human


def test_tool_missing_passes_through_only_when_a_read_allows_it() -> None:
    env = envelope("tool_missing", details={"install": "brew install gh"})
    codes = frozenset({"tool_missing"})
    error = error_of(outcome_of(result(12, env), codes=codes))
    assert error["code"] == "tool_missing" and error["details"] == {"install": "brew install gh"}
    error = error_of(outcome_of(result(12, env), codes=frozenset({"internal"})))
    assert error["code"] == "lower_contract_mismatch"


def test_a_would_refuse_form_is_a_mismatch_when_the_call_is_a_read() -> None:
    plan = {"would_refuse": {"reason": "too_large", "message": "x"}}
    error = error_of(
        outcome_of(result(20, plan), codes=frozenset({"refused"}), reasons=frozenset({"too_large"}))
    )
    assert error["code"] == "lower_contract_mismatch" and error["lower_error"] == plan


def test_invalid_position_from_forgectl_passes_through() -> None:
    env = envelope("invalid_position", details={"invalid": [{"index": 0}]})
    error = error_of(outcome_of(result(12, env, write=True)))
    assert error["code"] == "invalid_position" and error["details"] == env["error"]["details"]


def test_timed_out_write_keeps_forgectls_verdict_but_stays_unknown() -> None:
    env = envelope("rate_limited", "x")
    res = result(12, env, write=True, timed_out=True)
    error = error_of(outcome_of(res))
    assert (error["code"], error["landed"], error["retry"]) == (
        "outcome_unknown",
        None,
        "same_command",
    )
    assert error["lower_error"] == env["error"]
    junk = error_of(outcome_of(result(None, raw=b"not json", write=True, timed_out=True)))
    assert junk["code"] == "outcome_unknown" and junk["lower_error"] is None


def test_partial_document_returns_the_whoami_shape_for_exit_12_only() -> None:
    document = {
        "hosts": [{"host": "a", "backend": "github", "login": "me", "id": 1}],
        "errors": [{"host": "b", "code": "auth_required", "message": "no"}],
    }
    res = ForgectlResult(
        command="whoami", write=False, exit_code=12, stdout=json.dumps(document).encode()
    )
    assert partial_document(res) == document
    assert error_of(outcome_of(res))["code"] == "lower_contract_mismatch"  # behavior unchanged
    for bad in (
        ForgectlResult(command="whoami", write=False, exit_code=0, stdout=res.stdout),
        ForgectlResult(command="whoami", write=False, exit_code=12, stdout=b"[]"),
        ForgectlResult(command="whoami", write=False, exit_code=12, stdout=b"nope"),
        ForgectlResult(command="whoami", write=False, exit_code=12, stdout=b'{"hosts": []}'),
        ForgectlResult(
            command="whoami",
            write=False,
            exit_code=12,
            stdout=json.dumps({**document, "error": {}}).encode(),
        ),
    ):
        assert partial_document(bad) is None


@pytest.mark.parametrize(
    "fields",
    [{"timed_out": True}, {"stdout_truncated": True}, {"exit_code": None}],
)
def test_partial_document_is_none_for_a_killed_cut_or_signalled_result(
    fields: dict[str, Any],
) -> None:
    document = {"hosts": [], "errors": []}
    base: dict[str, Any] = {
        "command": "whoami",
        "write": False,
        "exit_code": 12,
        "stdout": json.dumps(document).encode(),
    }
    assert partial_document(ForgectlResult(**base)) == document
    assert partial_document(ForgectlResult(**{**base, **fields})) is None


def test_mismatch_outcome_lets_an_interrupted_version_read_propagate() -> None:
    class Interrupting(FakeGateway):
        def lower_version(self) -> str | None:
            raise ForgectlInterrupted(signal.SIGTERM, result(None, signal=15))

    with pytest.raises(ForgectlInterrupted):
        mismatch_outcome("show", "why", gateway=Interrupting())
    with pytest.raises(ForgectlInterrupted):
        failure_outcome(result(12, b"", raw=b"x"), gateway=Interrupting())


def test_vetoed_keeps_hook_fields_in_lower_error_only() -> None:
    env = envelope("vetoed", "no", hook="pre-post", hook_stderr="why")
    error = error_of(outcome_of(result(11, env)))
    assert error["hook"] is None and error["hook_stderr"] is None
    assert error["lower_error"]["hook_stderr"] == "why"


def test_escalated_by_forgectl_copies_hooks_and_decision_ids() -> None:
    hooks = [{"hook": "pre-post", "index": 0}]
    env = envelope("escalated", escalated_hooks=hooks, decision_ids=["d1"], hook="pre-post")
    error = error_of(outcome_of(result(13, env)))
    assert error["escalated_by"] == "forgectl" and error["escalated_hooks"] == hooks
    assert error["decision_ids"] == ["d1"]
    missing = error_of(outcome_of(result(13, envelope("escalated"))))
    assert missing["code"] == "lower_contract_mismatch"


def test_a_killed_write_with_a_network_error_envelope_printed_stays_outcome_unknown() -> None:
    env = envelope("network_error", "budget ran out")
    res = result(None, env, write=True, timed_out=True)
    error = error_of(outcome_of(res))
    assert (error["code"], error["landed"], error["retry"]) == (
        "outcome_unknown",
        None,
        "same_command",
    )
    assert error["lower_error"] == env["error"]


def test_timeouts_and_signals() -> None:
    read = error_of(outcome_of(result(None, timed_out=True)))
    assert (read["code"], read["landed"], read["retry"]) == ("network_error", False, "same_command")
    write = error_of(outcome_of(result(None, timed_out=True, write=True)))
    assert (write["code"], write["landed"], write["retry"]) == (
        "outcome_unknown",
        None,
        "same_command",
    )
    died = error_of(outcome_of(result(None, signal=9, write=True)))
    assert died["code"] == "outcome_unknown"
    assert error_of(outcome_of(result(None, signal=9)))["code"] == "lower_contract_mismatch"


def test_failure_outcome_of_an_exit_zero_is_a_mismatch() -> None:
    assert error_of(outcome_of(result(0, {})))["code"] == "lower_contract_mismatch"


def test_tool_missing_has_the_install_command() -> None:
    outcome = tool_missing_outcome(ToolMissing("forgectl was not found"))
    check_outcome(outcome)
    error = error_of(outcome)
    assert outcome.exit_code == 12 and error["code"] == "tool_missing"
    assert error["details"] == {
        "install": "uv tool install git+https://github.com/agent-workflow-kit/forgectl"
    }
    assert (error["landed"], error["retry"]) == (False, "same_command")
    assert exit_code_for("tool_missing") == 12


# --- the fake gateway ---------------------------------------------------------------------------


def test_fake_gateway_validates_records_and_replies_in_order() -> None:
    fake = FakeGateway([ok("show", {"a": 1}), ToolMissing("x")])
    assert require_ok(fake.call("show", ["o/r#1"]), gateway=fake) == {"a": 1}
    with pytest.raises(ToolMissing):
        fake.call("comment", ["o/r#1", "--body-file", "-"], stdin=b"hi")
    assert [(c.subcommand, c.stdin) for c in fake.calls] == [("show", None), ("comment", b"hi")]
    with pytest.raises(AssertionError):
        fake.call("show", ["o/r#1"])


def test_fake_gateway_refuses_what_the_real_gateway_refuses() -> None:
    fake = FakeGateway([ok("show", {})])
    with pytest.raises(GatewayUsageError):
        fake.call("show", ["--pr", "x"])


def test_fake_gateway_gives_replies_the_calls_write_flag_and_fails_a_disagreement() -> None:
    args = ["o/r#1", "--file", "-"]
    fake = FakeGateway([ok("review", {}), ok("review", {}, write=False)])
    assert fake.call("review", args, stdin=b"{}").write is True
    with pytest.raises(AssertionError, match="write"):
        fake.call("review", args, stdin=b"{}")  # a scripted read for a real write
    dry = FakeGateway([ok("review", {}, write=False), ok("show", {}, write=True)])
    assert dry.call("review", [*args, "--dry-run"], stdin=b"{}").write is False
    with pytest.raises(AssertionError, match="write"):
        dry.call("show", ["o/r#1"])  # a scripted write for a read


def test_fake_gateway_resolve_can_be_scripted_to_be_missing() -> None:
    assert FakeGateway().resolve() == "/fake/forgectl"
    missing = FakeGateway(missing=ToolMissing("gone"))
    with pytest.raises(ToolMissing):
        missing.resolve()


# --- cli ----------------------------------------------------------------------------------------


def test_cli_exits_128_plus_n_with_no_output_when_a_call_is_interrupted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(_args: object) -> Any:
        raise ForgectlInterrupted(signal.SIGTERM, result(None, signal=15))

    monkeypatch.setitem(cli.HANDLERS, "comment", handler)
    body = os.devnull
    code = cli.main(["comment", "o/r#1", f"--body-file={body}", "--json"])
    captured = capsys.readouterr()
    assert code == 128 + 15 and captured.out == ""


# --- reads: duplicate keys, read-only code filters, the version text (#26) ----------------------


def test_a_document_with_a_duplicate_key_is_a_contract_mismatch() -> None:
    raw = b'{"head_sha": "' + b"a" * 40 + b'", "head_sha": "' + b"b" * 40 + b'"}'
    with pytest.raises(LowerFailure) as caught:
        require_ok(result(0, raw=raw), gateway=GATEWAY)
    assert caught.value.outcome.document["error"]["code"] == "lower_contract_mismatch"  # type: ignore[index]


def test_a_duplicate_key_in_an_error_envelope_is_a_mismatch() -> None:
    raw = b'{"error": {"code": "not_found", "code": "internal", "message": "m"}}'
    outcome = outcome_of(result(10, raw=raw))
    assert outcome.document["error"]["code"] == "lower_contract_mismatch"


def test_lower_version_in_a_mismatch_is_sanitized_and_cut_to_64_characters() -> None:
    gateway = FakeGateway(version="forgectl \x1b[31m0.1‮" + "x" * 200)
    outcome = mismatch_outcome("show", "why", gateway=gateway)
    version = outcome.document["error"]["details"]["lower_version"]  # type: ignore[index]
    assert len(version) <= 64 and "\x1b" not in version and "‮" not in version
    assert "<U+001B>" in version


def test_lower_version_cut_never_leaves_part_of_a_marker() -> None:
    for pad in range(54, 64):
        gateway = FakeGateway(version="a" * pad + "\x01tail")
        outcome = mismatch_outcome("show", "why", gateway=gateway)
        version = outcome.document["error"]["details"]["lower_version"]  # type: ignore[index]
        assert len(version) <= 64
        assert "<" not in version or version.count("<U+0001>") == 1, version
        assert version.startswith("a" * pad)
    gateway = FakeGateway(version="a" * 60 + "\x01tail")
    version = mismatch_outcome("show", "why", gateway=gateway).document["error"]["details"][  # type: ignore[index]
        "lower_version"
    ]
    assert version == "a" * 60


def test_lower_version_of_none_stays_none() -> None:
    outcome = mismatch_outcome("show", "why", gateway=FakeGateway(version=None))
    assert outcome.document["error"]["details"]["lower_version"] is None  # type: ignore[index]


READ_CODES = frozenset({"not_found", "internal"})


def test_a_code_outside_the_allowed_set_is_a_mismatch_not_a_passthrough() -> None:
    allowed = outcome_of(result(10, envelope("not_found")), codes=READ_CODES)
    assert allowed.document["error"]["code"] == "not_found"
    outcome = outcome_of(result(11, envelope("vetoed", hook="h", hook_stderr="")), codes=READ_CODES)
    assert outcome.document["error"]["code"] == "lower_contract_mismatch"
    assert outcome.document["error"]["lower_error"]["code"] == "vetoed"


def test_outcome_partial_on_a_read_is_a_mismatch_without_needing_a_backend() -> None:
    outcome = outcome_of(
        result(12, envelope("outcome_partial", details={"landed": {}, "failed": "note"})),
        codes=READ_CODES,
    )
    assert outcome.document["error"]["code"] == "lower_contract_mismatch"


def test_a_refusal_reason_outside_the_allowed_set_is_a_mismatch() -> None:
    refused = envelope("refused", reason="stale_head")
    outcome = outcome_of(result(20, refused), codes=frozenset({"refused"}), reasons=frozenset())
    assert outcome.document["error"]["code"] == "lower_contract_mismatch"
    allowed = outcome_of(
        result(20, envelope("refused", reason="too_large")),
        codes=frozenset({"refused"}),
        reasons=frozenset({"too_large"}),
    )
    assert allowed.document["error"]["reason"] == "too_large"


def test_require_ok_passes_the_filters_to_a_failure() -> None:
    with pytest.raises(LowerFailure) as caught:
        require_ok(
            result(11, envelope("vetoed", hook="h", hook_stderr="")),
            gateway=GATEWAY,
            codes=READ_CODES,
        )
    assert caught.value.outcome.document["error"]["code"] == "lower_contract_mismatch"  # type: ignore[index]
