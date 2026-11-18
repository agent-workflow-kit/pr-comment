"""The pre-check rounds (``src/pr_comment/precheck.py``; docs/interface.md: Pre-check)."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from pr_comment.forge import Forge
from pr_comment.gateway import ForgectlInterrupted, ForgectlResult, ToolMissing
from pr_comment.lower import LowerFailure
from pr_comment.output import check_outcome
from pr_comment.plan import Plan, build_review_plan, build_text_plan
from pr_comment.precheck import Prechecked, run_precheck
from tests import forgectl_harness as h
from tests.fake_gateway import FakeGateway, PinnedGateway, ok
from tests.posting import (
    A,
    context,
    draft_doc,
    dry_doc,
    dry_error,
    dry_ok,
    dry_refusal,
    finding,
    prepared_of,
    show_of,
    source_of,
)
from tests.schema_harness import validate

Build = Callable[[Mapping[str, str]], Plan]


def review_builder(*findings: dict[str, Any], **ctx: Any) -> Build:
    source, _ = source_of(draft_doc(list(findings)))
    c = context(**ctx)
    return lambda moves: build_review_plan(c, source, moves)


def files_reply(*paths: str, complete: bool, head: str = A) -> ForgectlResult:
    listed = [
        {"path": p, "previous_path": None, "status": "modified", "additions": 1, "deletions": 0}
        for p in paths
    ]
    doc = h.files_doc(head_sha=head, files=listed)
    doc["files_complete"] = complete
    if not complete:
        doc["changed_files"] = len(paths) + 5
    return ok("files", doc)


def run(
    build: Build, *replies: Any, rounds: list[dict[str, Any]] | None = None
) -> tuple[Prechecked, PinnedGateway]:
    gateway = PinnedGateway(list(replies))
    done = run_precheck(
        gateway, Forge(gateway), build, on_round=None if rounds is None else rounds.append
    )
    assert not gateway._replies, "a scripted reply was not used"  # pyright: ignore[reportPrivateUsage]
    return done, gateway


def failure_of(build: Build, *replies: Any) -> dict[str, Any]:
    gateway = PinnedGateway(list(replies))
    with pytest.raises(LowerFailure) as caught:
        run_precheck(gateway, Forge(gateway), build)
    outcome = check_outcome(caught.value.outcome)
    assert outcome.document is not None
    validate("pr-comment/cli/error.json", outcome.document)
    error: dict[str, Any] = outcome.document["error"]
    error["_exit"] = outcome.exit_code
    return error


def loose_failure_of(build: Build, *replies: Any) -> dict[str, Any]:
    """Like ``failure_of`` for answers the pinned forgectl could never print (hostile)."""
    gateway = FakeGateway(list(replies))
    with pytest.raises(LowerFailure) as caught:
        run_precheck(gateway, Forge(gateway), build)
    outcome = check_outcome(caught.value.outcome)
    assert outcome.document is not None
    validate("pr-comment/cli/error.json", outcome.document)
    error: dict[str, Any] = outcome.document["error"]
    error["_exit"] = outcome.exit_code
    return error


def invalid(*indexes: Any) -> dict[str, Any]:
    return {
        "invalid": [
            {"index": i, "path": "a.py", "line": 3, "side": "right", "start_line": None}
            for i in indexes
        ]
    }


# --- a pre-check that passes -----------------------------------------------------------------


def test_the_precheck_is_the_real_call_with_dry_run_and_the_same_input() -> None:
    build = review_builder(finding("f1"), finding("f2", "b.py", 4), decision_ids=["d1"])
    plan = build({})
    done, gateway = run(build, dry_ok(plan))
    (call,) = gateway.calls
    assert call.subcommand == "review"
    assert call.stdin == plan.input
    assert "--dry-run" in call.args and "--yes" not in call.args
    assert call.args == tuple(plan.precheck_args())
    assert f"--expected-head={A}" in call.args and "--expected-base=main" in call.args
    assert f"--dedupe-key={plan.dedupe_key}" in call.args and "--decision-id=d1" in call.args
    assert done.plan == plan
    assert done.rounds == ("initial",)
    assert done.already_posted is None and done.warnings == ()


def test_a_large_cjk_review_passes_the_precheck_within_forgectls_file_limit() -> None:
    text = "漢" * 21000  # 126000 bytes as \\u escapes, 63000 as UTF-8
    build = review_builder(*(finding(f"f{i}", f"p{i}.py", 3, body=text) for i in range(150)))
    plan = build({})
    assert 9_000_000 < len(plan.input) < 16 * 1024 * 1024
    done, gateway = run(build, dry_ok(plan))
    assert done.rounds == ("initial",) and gateway.calls[0].stdin == plan.input


def test_the_dedupe_hint_never_ends_the_command() -> None:
    build = review_builder(finding("f1"))
    plan = build({})
    done, gateway = run(build, dry_ok(plan, already_posted=True))
    assert done.already_posted is True
    assert len(gateway.calls) == 1


def test_a_pending_gitlab_approval_still_leads_to_the_real_call() -> None:
    # forgectl reports already_posted: true in a dry run even when a state step is still needed
    pr = "gitlab.com/acme/widgets#7"
    source, _ = source_of(draft_doc([finding("f1")], pr=pr), approve=True)
    c = context(show_of(pr, backend="gitlab"))
    plan = build_review_plan(c, source)
    done, _ = run(lambda m: build_review_plan(c, source, m), dry_ok(plan, already_posted=True))
    assert done.already_posted is True
    assert done.plan.event == "approve"


def test_every_precheck_call_is_a_read() -> None:
    build = review_builder(finding("f1"))
    plan = build({})
    _, gateway = run(build, dry_ok(plan))
    assert all("--dry-run" in c.args for c in gateway.calls)


def test_comment_reply_and_approve_prechecks() -> None:
    c = context()
    comment = build_text_plan(c, action="comment", text="hi", head_sha=A, base_ref="main")
    reply = build_text_plan(
        c, action="reply", text="hi", head_sha=A, base_ref="main", to="gh:rc:RC_1"
    )
    approve = build_text_plan(c, action="approve", text=None, head_sha=A, base_ref="main")
    for plan in (comment, reply, approve):
        extra = {"threaded": True} if plan.action == "reply" else {}
        done, gateway = run(lambda _m, p=plan: p, dry_ok(plan, **extra))
        assert gateway.calls[0].stdin == plan.input
        assert done.threaded is (True if plan.action == "reply" else None)


def test_a_reply_to_a_general_comment_is_not_threaded() -> None:
    plan = build_text_plan(
        context(), action="reply", text="hi", head_sha=A, base_ref="main", to="gh:ic:IC_1"
    )
    done, _ = run(lambda _m: plan, dry_ok(plan, threaded=False))
    assert done.threaded is False


def test_a_reply_without_threaded_is_a_contract_mismatch() -> None:
    c = context()
    plan = build_text_plan(
        c, action="reply", text="hi", head_sha=A, base_ref="main", to="gh:rc:RC_1"
    )
    doc = dry_doc(plan)
    doc.pop("threaded", None)
    gateway = FakeGateway([ok("reply", doc, write=False)])
    with pytest.raises(LowerFailure) as caught:
        run_precheck(gateway, Forge(gateway), lambda _m: plan)
    assert caught.value.outcome.exit_code == 12


def test_a_neutralizing_mismatch_is_a_warning_not_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    build = review_builder(finding("f1"))
    plan = build({})
    doc = dry_doc(plan)
    doc["neutralized_lines"] = 2
    gateway = FakeGateway([ok("review", doc, write=False)])
    done = run_precheck(gateway, Forge(gateway), build)
    assert done.warnings == ("neutralizing_mismatch",)
    assert any("neutralizing_mismatch" in r.getMessage() for r in caplog.records)


def test_a_dry_run_without_the_fields_pr_comment_reads_is_a_contract_mismatch() -> None:
    build = review_builder(finding("f1"))
    plan = build({})
    doc = dry_doc(plan)
    del doc["already_posted"]
    gateway = FakeGateway([ok("review", doc, write=False)])
    with pytest.raises(LowerFailure) as caught:
        run_precheck(gateway, Forge(gateway), build)
    error = check_outcome(caught.value.outcome).document
    assert error is not None and error["error"]["code"] == "lower_contract_mismatch"
    assert error["error"]["details"]["command"] == "review"


# --- the invalid_position round --------------------------------------------------------------


def test_an_invalid_position_moves_the_finding_and_runs_the_precheck_once_more() -> None:
    build = review_builder(finding("f1"), finding("f2", "b.py", 4), finding("f3", "c.py", 5))
    first = build({})
    second = build({"f2": "invalid_position"})
    log: list[dict[str, Any]] = []
    done, gateway = run(
        build,
        dry_error(first, 12, "invalid_position", details=invalid(1)),
        dry_ok(second),
        rounds=log,
    )
    assert done.plan == second
    assert [m.id for m in done.plan.moved] == ["f2"]
    assert done.plan.posted_inline == ("f1", "f3")
    assert done.moves == {"f2": "invalid_position"}
    assert done.rounds == ("initial", "invalid_position")
    assert gateway.calls[0].stdin == first.input
    assert gateway.calls[1].stdin == second.input != first.input
    assert [(r["event"], r["round"], r["result"]) for r in log] == [
        ("precheck", "initial", "invalid_position"),
        ("precheck", "invalid_position", "ok"),
    ]
    assert log[1]["moved"] == [{"id": "f2", "reason": "invalid_position"}]


def test_several_invalid_positions_move_together() -> None:
    build = review_builder(finding("f1"), finding("f2", "b.py", 4))
    first = build({})
    both = build({"f1": "invalid_position", "f2": "invalid_position"})
    done, _ = run(
        build, dry_error(first, 12, "invalid_position", details=invalid(1, 0, 1)), dry_ok(both)
    )
    assert done.plan.posted_inline == ()
    assert [m.id for m in done.plan.moved] == ["f1", "f2"]


def test_a_second_invalid_position_exits_12() -> None:
    build = review_builder(finding("f1"), finding("f2", "b.py", 4))
    first = build({})
    second = build({"f1": "invalid_position"})
    error = failure_of(
        build,
        dry_error(first, 12, "invalid_position", details=invalid(0)),
        dry_error(second, 12, "invalid_position", details=invalid(0)),
    )
    assert error["_exit"] == 12 and error["code"] == "invalid_position"
    assert error["landed"] is False and error["retry"] == "same_command"
    assert error["details"]["invalid"][0]["index"] == 0


@pytest.mark.parametrize("index", [5, -1, None, True, "0", 1.0])
def test_an_invalid_position_outside_the_plan_is_a_contract_mismatch(index: Any) -> None:
    build = review_builder(finding("f1"))
    first = build({})
    error = loose_failure_of(
        build, dry_error(first, 12, "invalid_position", details=invalid(index))
    )
    assert error["code"] == "lower_contract_mismatch"
    assert error["details"]["command"] == "review"


def test_an_invalid_position_without_items_is_a_contract_mismatch() -> None:
    build = review_builder(finding("f1"))
    first = build({})
    for details in ({"invalid": []}, {}, None, {"invalid": "x"}, {"invalid": [7]}):
        error = loose_failure_of(build, dry_error(first, 12, "invalid_position", details=details))
        assert error["code"] == "lower_contract_mismatch"


# --- the too_large round ---------------------------------------------------------------------


def test_too_large_with_an_incomplete_file_list_moves_the_unlisted_paths() -> None:
    build = review_builder(finding("f1", "listed.py", 2), finding("f2", "missing.py", 4))
    first = build({})
    second = build({"f2": "files_incomplete"})
    log: list[dict[str, Any]] = []
    done, gateway = run(
        build,
        dry_refusal(first, "too_large"),
        files_reply("listed.py", complete=False),
        dry_ok(second),
        rounds=log,
    )
    assert [(m.id, m.reason) for m in done.plan.moved] == [("f2", "files_incomplete")]
    assert [c.subcommand for c in gateway.calls] == ["review", "files", "review"]
    assert done.rounds == ("initial", "too_large")
    assert [r["result"] for r in log] == ["refused", "ok"]
    assert log[0]["reason"] == "too_large"


def test_too_large_with_a_complete_file_list_passes_through() -> None:
    build = review_builder(finding("f1", "listed.py", 2))
    first = build({})
    error = failure_of(
        build,
        dry_refusal(first, "too_large", {"limit": 5}),
        files_reply("listed.py", complete=True),
    )
    assert error["_exit"] == 20 and error["reason"] == "too_large"
    assert error["code"] == "refused"
    assert error["details"] == {"limit": 5}
    assert error["lower_error"]["would_refuse"]["reason"] == "too_large"


def test_too_large_with_every_path_listed_passes_through() -> None:
    build = review_builder(finding("f1", "listed.py", 2))
    first = build({})
    error = failure_of(
        build, dry_refusal(first, "too_large"), files_reply("listed.py", complete=False)
    )
    assert error["_exit"] == 20 and error["reason"] == "too_large"


def test_a_renamed_file_is_listed_by_its_new_path_only() -> None:
    """forgectl (Position validation): a renamed file matches by its new path, on either side."""
    build = review_builder(
        finding("f1", "old.py", 2, side="left"),
        finding("f2", "new.py", 3),
        finding("f3", "z.py", 4),
    )
    first = build({})
    listed = [
        {
            "path": "new.py",
            "previous_path": "old.py",
            "status": "renamed",
            "additions": 0,
            "deletions": 0,
        }
    ]
    doc = h.files_doc(files=listed)
    doc["files_complete"] = False
    doc["changed_files"] = 9
    second = build({"f1": "files_incomplete", "f3": "files_incomplete"})
    done, _ = run(build, dry_refusal(first, "too_large"), ok("files", doc), dry_ok(second))
    assert done.moves == {"f1": "files_incomplete", "f3": "files_incomplete"}
    assert done.plan.posted_inline == ("f2",)


def test_a_file_list_for_another_head_is_a_stale_head() -> None:
    build = review_builder(finding("f1"))
    first = build({})
    error = failure_of(
        build, dry_refusal(first, "too_large"), files_reply("a.py", complete=False, head="b" * 40)
    )
    assert error["_exit"] == 20 and error["reason"] == "stale_head"
    assert error["details"] == {"head_sha": "b" * 40}


def test_a_failing_files_read_passes_its_error_through() -> None:
    build = review_builder(finding("f1"))
    first = build({})
    error = failure_of(
        build,
        dry_refusal(first, "too_large"),
        ForgectlResult(
            command="files",
            write=False,
            exit_code=12,
            stdout=json.dumps({"error": {"code": "rate_limited", "message": "slow down"}}).encode(),
        ),
    )
    assert error["_exit"] == 12 and error["code"] == "rate_limited"


def test_a_second_too_large_passes_through() -> None:
    build = review_builder(finding("f1", "a.py", 2), finding("f2", "b.py", 3))
    first = build({})
    second = build({"f2": "files_incomplete"})
    error = failure_of(
        build,
        dry_refusal(first, "too_large"),
        files_reply("a.py", complete=False),
        dry_refusal(second, "too_large"),
    )
    assert error["_exit"] == 20 and error["reason"] == "too_large"


def test_too_large_then_invalid_position_runs_both_rounds_in_order() -> None:
    build = review_builder(
        finding("f1", "a.py", 2), finding("f2", "b.py", 3), finding("f3", "c.py", 4)
    )
    first = build({})
    second = build({"f3": "files_incomplete"})
    third = build({"f3": "files_incomplete", "f1": "invalid_position"})
    done, gateway = run(
        build,
        dry_refusal(first, "too_large"),
        files_reply("a.py", "b.py", complete=False),
        dry_error(second, 12, "invalid_position", details=invalid(0)),
        dry_ok(third),
    )
    assert done.rounds == ("initial", "too_large", "invalid_position")
    assert done.moves == {"f3": "files_incomplete", "f1": "invalid_position"}
    assert [c.subcommand for c in gateway.calls] == ["review", "files", "review", "review"]


def test_invalid_position_then_too_large_passes_the_refusal_through() -> None:
    build = review_builder(finding("f1", "a.py", 2), finding("f2", "b.py", 3))
    first = build({})
    second = build({"f1": "invalid_position"})
    error = failure_of(
        build,
        dry_error(first, 12, "invalid_position", details=invalid(0)),
        dry_refusal(second, "too_large"),
    )
    assert error["_exit"] == 20 and error["reason"] == "too_large"


def test_too_large_on_a_comment_passes_through_without_a_files_read() -> None:
    plan = build_text_plan(context(), action="comment", text="x", head_sha=A, base_ref="main")
    # a comment has no inline paths: forgectl cannot refuse it as too_large (the reason is not one
    # a comment dry run can give), so the answer is a contract mismatch, not a files round
    error = loose_failure_of(lambda _m: plan, dry_refusal(plan, "too_large"))
    assert error["code"] == "lower_contract_mismatch"


# --- other answers ---------------------------------------------------------------------------


def test_body_too_large_names_the_body_role() -> None:
    build = review_builder(finding("f1"), finding("f2", "b.py", 4))
    plan = build({})
    error = loose_failure_of(
        build, dry_error(plan, 2, "body_too_large", details={"index": None, "limit": 262144})
    )
    assert error["_exit"] == 2 and error["code"] == "body_too_large"
    assert error["details"] == {"index": None, "limit": 262144, "role": "body"}


def test_body_too_large_maps_an_inline_index_to_its_finding() -> None:
    build = review_builder(finding("f1"), finding("f2", "b.py", 4))
    plan = build({})
    error = loose_failure_of(
        build, dry_error(plan, 2, "body_too_large", details={"index": 1, "limit": 1048576})
    )
    assert error["details"]["role"] == "inline:f2"


def test_body_too_large_with_an_index_outside_the_plan_is_a_mismatch() -> None:
    build = review_builder(finding("f1"))
    plan = build({})
    error = loose_failure_of(
        build, dry_error(plan, 2, "body_too_large", details={"index": 7, "limit": 1})
    )
    assert error["code"] == "lower_contract_mismatch"


@pytest.mark.parametrize("details", [{"limit": 1048576}, None])
def test_body_too_large_on_a_review_without_an_index_key_is_a_mismatch(
    details: dict[str, Any] | None,
) -> None:
    build = review_builder(finding("f1"))
    plan = build({})
    error = loose_failure_of(build, dry_error(plan, 2, "body_too_large", details=details))
    assert error["code"] == "lower_contract_mismatch"


def test_body_too_large_on_a_comment_is_the_body() -> None:
    huge = "x" * 300_000  # GitHub's comment limit is 262144 bytes
    plan = build_text_plan(context(), action="comment", text=huge, head_sha=A, base_ref="main")
    error = loose_failure_of(
        lambda _m: plan,
        dry_error(plan, 2, "body_too_large", details={"index": None, "limit": 262144}),
    )
    assert error["details"]["role"] == "body" and error["_exit"] == 2


@pytest.mark.parametrize(
    ("exit_code", "code", "extra"),
    [
        (12, "rate_limited", {"retry_after": 30}),
        (12, "network_error", {}),
        (12, "auth_required", {}),
        (12, "upstream_error", {}),
        (10, "not_found", {}),
    ],
)
def test_forgectl_failures_in_the_precheck_pass_through_before_any_dialog(
    exit_code: int, code: str, extra: dict[str, Any]
) -> None:
    build = review_builder(finding("f1"))
    plan = build({})
    error = failure_of(build, dry_error(plan, exit_code, code, **extra))
    assert error["_exit"] == exit_code and error["code"] == code
    assert error["lower_error"]["code"] == code
    if "retry_after" in extra:
        assert error["retry_after"] == 30


@pytest.mark.parametrize(
    ("reason", "details"),
    [
        ("stale_head", {"head_sha": "b" * 40}),
        ("stale_base", {"base_ref": "dev"}),
        ("not_open", {"state": "closed"}),
        ("self_approval", {"basis": "authenticated"}),
    ],
)
def test_forgectls_dry_run_refusals_become_exit_20_with_lower_error(
    reason: str, details: dict[str, Any] | None
) -> None:
    build = review_builder(finding("f1"))
    plan = build({})
    error = failure_of(build, dry_refusal(plan, reason, details))
    assert error["_exit"] == 20 and error["code"] == "refused" and error["reason"] == reason
    assert error["details"] == details
    assert error["lower_error"]["would_refuse"]["reason"] == reason


def test_an_unknown_to_exits_10_in_the_precheck_and_names_it() -> None:
    plan = build_text_plan(
        context(), action="reply", text="x", head_sha=A, base_ref="main", to="gh:rc:RC_nope"
    )
    error = failure_of(lambda _m: plan, dry_error(plan, 10, "not_found"))
    assert error["_exit"] == 10 and error["code"] == "not_found"
    assert error["details"] == {"what": "to", "id": "gh:rc:RC_nope"}


def test_a_pending_review_is_a_refusal_even_if_a_dry_run_rarely_finds_it() -> None:
    build = review_builder(finding("f1"))
    plan = build({})
    error = loose_failure_of(build, dry_refusal(plan, "pending_review_exists"))
    assert error["_exit"] == 20 and error["reason"] == "pending_review_exists"


@pytest.mark.parametrize(
    ("code", "exit_code"),
    [
        ("vetoed", 11),
        ("outcome_unknown", 12),
        ("outcome_partial", 12),
        ("quick_action_ran", 12),
        ("approved_stale_head", 12),
        ("audit_failed", 12),
        ("escalated", 13),
        ("confirmation_required", 13),
    ],
)
def test_write_only_codes_in_a_dry_run_are_a_contract_mismatch(code: str, exit_code: int) -> None:
    build = review_builder(finding("f1"))
    plan = build({})
    gateway = FakeGateway([dry_error(plan, exit_code, code, details={"landed": {"ids": []}})])
    with pytest.raises(LowerFailure) as caught:
        run_precheck(gateway, Forge(gateway), build)
    document = caught.value.outcome.document
    assert document is not None and document["error"]["code"] == "lower_contract_mismatch"


@pytest.mark.parametrize("action", ["comment", "reply", "approve"])
def test_invalid_position_on_a_non_review_action_is_a_contract_mismatch(action: str) -> None:
    kwargs: dict[str, Any] = {"to": "gh:rc:RC_x"} if action == "reply" else {}
    plan = build_text_plan(
        context(), action=action, text="x", head_sha=A, base_ref="main", **kwargs
    )
    gateway = FakeGateway([dry_error(plan, 12, "invalid_position", details=invalid(0))])
    with pytest.raises(LowerFailure) as caught:
        run_precheck(gateway, Forge(gateway), lambda _m: plan)
    document = caught.value.outcome.document
    assert document is not None and document["error"]["code"] == "lower_contract_mismatch"


def test_a_forgectl_that_exits_early_without_output_is_a_contract_mismatch() -> None:
    build = review_builder(finding("f1"))
    gateway = FakeGateway([ForgectlResult(command="review", write=False, exit_code=0, stdout=b"")])
    with pytest.raises(LowerFailure) as caught:
        run_precheck(gateway, Forge(gateway), build)
    assert caught.value.outcome.exit_code == 12


def test_a_timeout_is_a_network_error_and_nothing_else_is_called() -> None:
    build = review_builder(finding("f1"))
    gateway = FakeGateway(
        [ForgectlResult(command="review", write=False, exit_code=None, timed_out=True)]
    )
    with pytest.raises(LowerFailure) as caught:
        run_precheck(gateway, Forge(gateway), build)
    document = caught.value.outcome.document
    assert document is not None and document["error"]["code"] == "network_error"
    assert len(gateway.calls) == 1


def test_a_signal_and_a_missing_forgectl_propagate() -> None:
    build = review_builder(finding("f1"))
    result = ForgectlResult(command="review", write=False, exit_code=None, signal=15)
    with pytest.raises(ForgectlInterrupted):
        gateway = FakeGateway([ForgectlInterrupted(15, result)])
        run_precheck(gateway, Forge(gateway), build)
    with pytest.raises(ToolMissing):
        gateway = FakeGateway([ToolMissing("forgectl is missing")])
        run_precheck(gateway, Forge(gateway), build)


def test_forgectls_invalid_pr_passes_through_as_a_usage_error() -> None:
    build = review_builder(finding("f1"))
    plan = build({})
    error = loose_failure_of(build, dry_error(plan, 2, "invalid_pr"))
    assert error["_exit"] == 2 and error["code"] == "invalid_pr"


def test_the_neutralized_counts_of_prcomments_input_are_zero_in_forgectls_eyes() -> None:
    hostile = "ok\n/approve now\r\nforgectl:dedupe:x\n /also\n  /not"
    build = review_builder(
        finding("f1", body=hostile, suggestion={"lines": ["/x", "```"]}), finding("f2", "b", 2)
    )
    plan = build({})
    done, _ = run(build, dry_ok(plan))
    assert done.warnings == ()
    comment = build_text_plan(
        context(), action="comment", text=hostile, head_sha=A, base_ref="main"
    )
    run(lambda _m: comment, dry_ok(comment))
    assert dry_doc(plan)["neutralized_lines"] == 0 and dry_doc(plan)["markers_removed"] == 0
    assert dry_doc(comment)["neutralized_lines"] == 0
    assert dry_doc(comment)["markers_removed"] == 0


def _golden_bodies() -> list[tuple[str, str]]:
    raw = (Path(__file__).parent / "golden" / "assemble" / "bodies.txt").read_text(encoding="utf-8")
    bodies: list[tuple[str, str]] = []
    for block in raw.rstrip("\n").split("\n\n=====\n\n"):
        header, _, text = block.partition("\n")
        assert header.startswith("# ")
        bodies.append((header.split()[1], text))
    return bodies


@pytest.mark.parametrize(("name", "text"), _golden_bodies(), ids=[n for n, _ in _golden_bodies()])
def test_forgectl_neutralizes_nothing_in_any_golden_body(name: str, text: str) -> None:
    """Acceptance criterion: for every golden body forgectl's neutralizing finds nothing to do, as
    a comment body and as a review body with the same text as an inline comment."""
    as_comment = prepared_of("comment", ["--dedupe-key=k"], text.encode("utf-8"))
    review = json.dumps(
        {
            "event": "comment",
            "body": text,
            "comments": [{"path": "a.py", "line": 3, "side": "right", "body": text}],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    as_review = prepared_of("review", ["--dedupe-key=k"], review)
    for prep in (as_comment, as_review):
        assert (prep.neutralized_lines, prep.markers_removed) == (0, 0), name
