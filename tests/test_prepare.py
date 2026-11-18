"""The posting orders up to the plan (``src/pr_comment/prepare.py``): usage, reads, re-anchoring,
the plan, the pre-check, ``--expect-plan`` and the dry-run result, against the pinned fake forgectl
(docs/interface.md: Review order, Approve order, Comment and reply order)."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

import pytest

from pr_comment.config import Footer, FooterOverride, LoadedConfig
from pr_comment.lower import LowerFailure
from pr_comment.output import Outcome, check_outcome
from pr_comment.prepare import (
    Env,
    Prepared,
    Request,
    RunFailure,
    check_binding,
    dry_run_outcome,
    prepare,
)
from tests import forgectl_harness as h
from tests.fake_gateway import FakeGateway, PinnedGateway, ok
from tests.posting import (
    MB,
    PR,
    A,
    B,
    Doc,
    draft_doc,
    dry_reply,
    error_reply,
    error_result,
    finding,
    refusal_reply,
)
from tests.schema_harness import validate

GITLAB_PR = h.GITLAB_PR


def show_reply(
    head: str = A,
    *,
    state: str = "open",
    base_ref: str = "main",
    backend: str = "github",
    pr: str = PR,
) -> Any:
    doc = h.show_doc(pr, backend=backend, state=state)
    if state != "open":
        doc["closed_at"] = "2026-01-03T00:00:00Z"
    if state == "merged":
        doc.update(
            merged_at="2026-01-03T00:00:00Z",
            merged_by="octo",
            merged_head_sha=head,
            merge_commit_sha="d" * 40,
        )
    doc["head"] = {**doc["head"], "sha": head}
    doc["base"] = {**doc["base"], "ref": base_ref}
    return ok("show", doc)


def compare_reply(
    diff: str | None = "", *, status: str = "ahead", to: str = B, from_: str = A
) -> Any:
    return ok("compare", h.compare_doc(from_sha=from_, to_sha=to, status=status, diff=diff))


def diff_reply(*, head: str = A, merge_base: str | None = MB) -> Any:
    doc = h.diff_doc(head_sha=head)
    doc["merge_base_sha"] = merge_base
    return ok("diff", doc)


def write_draft(tmp_path: Path, doc: Doc, name: str = "draft.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def request(command: str = "review", **kw: Any) -> Request:
    return Request(command=command, pr=kw.pop("pr", PR), actor=kw.pop("actor", "human:alice"), **kw)


def env(
    gateway: FakeGateway, *, stdin: bytes | None = None, config: LoadedConfig | None = None
) -> Env:
    return Env(
        gateway=gateway,
        config=config or LoadedConfig.defaults(),
        me_name="alice",
        tool_version="0.1.0",
        stdin=None if stdin is None else io.BytesIO(stdin),
    )


def run(req: Request, *replies: Any, stdin: bytes | None = None, **kw: Any) -> tuple[Prepared, Any]:
    gateway = PinnedGateway(list(replies))
    done = prepare(req, env(gateway, stdin=stdin, **kw))
    assert not gateway._replies, "a scripted reply was not used"  # pyright: ignore[reportPrivateUsage]
    return done, gateway


def outcome_of(req: Request, *replies: Any, pinned: bool = True, **kw: Any) -> Outcome:
    gateway: FakeGateway = PinnedGateway(list(replies)) if pinned else FakeGateway(list(replies))
    with pytest.raises((RunFailure, LowerFailure)) as caught:
        prepare(req, env(gateway, **kw))
    outcome = check_outcome(caught.value.outcome)
    assert outcome.document is not None
    validate("pr-comment/cli/error.json", outcome.document)
    return outcome


def error_of(req: Request, *replies: Any, **kw: Any) -> dict[str, Any]:
    outcome = outcome_of(req, *replies, **kw)
    assert outcome.document is not None
    error: dict[str, Any] = outcome.document["error"]
    error["_exit"] = outcome.exit_code
    return error


def no_write(gateway: FakeGateway) -> None:
    """Nothing was posted: every call is a read or a dry run (the gateway says whether a call
    can write, and ``--yes`` never appears)."""
    for call in gateway.calls:
        assert "--yes" not in call.args
        if call.subcommand in ("review", "comment", "reply", "approve", "resolve", "unresolve"):
            assert "--dry-run" in call.args


# --- review: the happy path ------------------------------------------------------------------


def test_a_review_runs_show_then_the_dry_run_and_sends_nothing(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1"), finding("f2", "b.py", None)]))
    done, gateway = run(request(draft_file=draft), show_reply(), dry_reply())
    assert [c.subcommand for c in gateway.calls] == ["show", "review"]
    no_write(gateway)
    assert done.plan.posted_inline == ("f1",) and done.plan.file_level == ("f2",)
    assert done.plan.pr == "github.com/acme/widgets#7"
    assert done.reanchored_draft is None
    assert gateway.calls[1].stdin == done.plan.input


def test_building_the_plan_twice_from_the_same_inputs_gives_the_same_hash(tmp_path: Path) -> None:
    draft = write_draft(
        tmp_path,
        draft_doc(
            [
                finding("f1", body="cc @amy <b>x</b> ![i](u)"),
                finding("f2", "b.py", 8, suggestion={"lines": ["x = 1"]}),
            ]
        ),
    )
    one, _ = run(request(draft_file=draft), show_reply(), dry_reply())
    two, _ = run(request(draft_file=draft), show_reply(), dry_reply())
    assert one.plan.plan_sha256 == two.plan.plan_sha256
    assert one.plan.input == two.plan.input


def test_the_draft_can_come_from_stdin(tmp_path: Path) -> None:
    raw = json.dumps(draft_doc([finding("f1")])).encode()
    done, _ = run(request(draft_file="-"), show_reply(), dry_reply(), stdin=raw)
    assert done.plan.posted_inline == ("f1",)


def test_the_repo_settings_come_from_the_canonical_repo_of_show(tmp_path: Path) -> None:
    from pr_comment.config import Config, RepoOverride

    config = LoadedConfig.defaults()
    config = LoadedConfig(
        config=Config(
            repos={
                "GitHub.com/ACME/Widgets": RepoOverride(footer=FooterOverride(human="Custom {sha}"))
            }
        ),
        config_hash=config.config_hash,
        config_path=None,
    )
    draft = write_draft(tmp_path, draft_doc([finding("f1")]))
    done, _ = run(request(draft_file=draft), show_reply(), dry_reply(), config=config)
    assert done.plan.bodies[0].text.startswith("Custom aaaaaaaaaaaa")


def test_a_gitlab_review_has_the_body_thread_follow_up(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1")], pr=GITLAB_PR))
    done, _ = run(
        request(pr=GITLAB_PR, draft_file=draft),
        show_reply(pr=GITLAB_PR, backend="gitlab"),
        dry_reply("gitlab"),
    )
    assert [(f.action, f.thread_id) for f in done.plan.follow_ups] == [("resolve", None)]
    assert done.plan.document()["follow_ups"] == [{"action": "resolve", "thread_id": None}]


# --- review: reads and refusals --------------------------------------------------------------


def test_a_moved_head_without_reanchor_to_is_stale_head_after_one_read(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1")]))
    gateway = PinnedGateway([show_reply(B)])
    with pytest.raises(RunFailure) as caught:
        prepare(request(draft_file=draft), env(gateway))
    error = check_outcome(caught.value.outcome).document
    assert error is not None
    assert error["error"]["reason"] == "stale_head"
    assert error["error"]["details"] == {"head_sha": B}
    assert "--reanchor-to" in error["error"]["message"]
    assert [c.subcommand for c in gateway.calls] == ["show"]


def test_reanchor_to_must_be_the_current_head(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1")]))
    error = error_of(request(draft_file=draft, reanchor_to=B), show_reply(A))
    assert error["_exit"] == 20 and error["reason"] == "stale_head"
    assert error["details"] == {"head_sha": A}


def test_a_retargeted_base_is_stale_base(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1")]))
    error = error_of(request(draft_file=draft), show_reply(base_ref="release"))
    assert error["_exit"] == 20 and error["reason"] == "stale_base"
    assert error["details"] == {"base_ref": "release"}


@pytest.mark.parametrize("state", ["closed", "merged"])
def test_a_review_of_a_pr_that_is_not_open_is_refused_before_anything_else(
    tmp_path: Path, state: str
) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1")]))
    error = error_of(request(draft_file=draft), show_reply(state=state))
    assert error["_exit"] == 20 and error["reason"] == "not_open"
    assert error["details"] == {"state": state}


def test_an_expected_head_must_equal_the_pinned_head(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1")]))
    error = error_of(request(draft_file=draft, expected_head=B), show_reply(A))
    assert error["_exit"] == 20 and error["reason"] == "stale_head"
    assert error["details"] == {"head_sha": A}


def test_a_draft_for_another_pr_is_refused(tmp_path: Path) -> None:
    other = "github.com/acme/other#9"
    draft = write_draft(tmp_path, draft_doc([finding("f1")], pr=other))
    error = error_of(
        request(draft_file=draft),
        show_reply(),
        error_reply(10, "not_found"),
        pinned=False,
    )
    assert error["_exit"] == 2 and error["code"] == "draft_pr_mismatch"
    assert error["details"]["pr"] == "github.com/acme/widgets#7"


def test_approve_with_a_request_changes_draft_is_refused_before_any_read(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1")], event="request_changes"))
    gateway = PinnedGateway([])
    with pytest.raises(RunFailure) as caught:
        prepare(request(draft_file=draft, approve=True), env(gateway))
    document = check_outcome(caught.value.outcome).document
    assert document is not None and document["error"]["code"] == "approve_conflicts_event"
    assert gateway.calls == []


def test_an_automatic_approving_review_needs_an_expected_head(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1")]))
    gateway = PinnedGateway([])
    with pytest.raises(RunFailure) as caught:
        prepare(request(draft_file=draft, approve=True, actor="auto:bot"), env(gateway))
    outcome = check_outcome(caught.value.outcome)
    assert outcome.exit_code == 2 and gateway.calls == []


@pytest.mark.parametrize(
    "raw",
    [b"", b"{", b"[]", b'{"schema_version": 2}', b"\xff\xfe", b"\xef\xbb\xbf{}"],
)
def test_an_unusable_draft_fails_before_any_forgectl_call(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "draft.json"
    path.write_bytes(raw)
    gateway = PinnedGateway([])
    with pytest.raises(RunFailure) as caught:
        prepare(request(draft_file=str(path)), env(gateway))
    assert check_outcome(caught.value.outcome).exit_code == 2
    assert gateway.calls == []


def test_a_draft_that_is_a_symlink_or_missing_fails_before_any_call(tmp_path: Path) -> None:
    real = write_draft(tmp_path, draft_doc([finding("f1")]))
    link = tmp_path / "link.json"
    os.symlink(real, link)
    for path in (str(link), str(tmp_path / "nope.json"), str(tmp_path)):
        gateway = PinnedGateway([])
        with pytest.raises(RunFailure):
            prepare(request(draft_file=path), env(gateway))
        assert gateway.calls == []


# --- review: re-anchoring --------------------------------------------------------------------


SHIFT = (
    "diff --git a/a.py b/a.py\nindex 1..2 100644\n--- a/a.py\n+++ b/a.py\n"
    "@@ -1,2 +1,4 @@\n x\n y\n+one\n+two\n"
)


def test_reanchor_to_maps_the_findings_and_posts_at_b(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1", "a.py", 5), finding("f2", "z.py", 2)]))
    done, gateway = run(
        request(draft_file=draft, reanchor_to=B),
        show_reply(B),
        compare_reply(SHIFT),
        dry_reply(),
    )
    assert [c.subcommand for c in gateway.calls] == ["show", "compare", "review"]
    assert gateway.calls[1].args[1:] == (f"--from={A}", f"--to={B}")
    assert done.plan.head_sha == B
    assert [(c.finding_id, c.path, c.line) for c in done.plan.inline_comments] == [
        ("f1", "a.py", 7),
        ("f2", "z.py", 2),
    ]
    assert f"--expected-head={B}" in gateway.calls[2].args
    assert done.reanchored_draft is not None
    assert done.reanchored_draft["anchor"]["head_sha"] == B
    assert [f["line"] for f in done.reanchored_draft["findings"]] == [7, 2]
    assert done.reanchored_draft["unanchored"] if "unanchored" in done.reanchored_draft else True


def test_reanchor_to_the_same_head_calls_no_compare(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1")]))
    done, gateway = run(request(draft_file=draft, reanchor_to=A), show_reply(A), dry_reply())
    assert [c.subcommand for c in gateway.calls] == ["show", "review"]
    assert done.reanchored_draft is not None


def test_a_force_push_moves_every_right_side_finding_with_diff_unavailable(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1"), finding("f2", "b.py", 4)]))
    done, _ = run(
        request(draft_file=draft, reanchor_to=B),
        show_reply(B),
        compare_reply(None, status="diverged"),
        dry_reply(),
    )
    assert [(m.id, m.reason) for m in done.plan.moved] == [
        ("f1", "diff_unavailable"),
        ("f2", "diff_unavailable"),
    ]
    assert done.plan.posted_inline == ()
    assert "could not be compared" in done.plan.bodies[0].text
    assert done.reanchored_draft is not None
    assert [u["reason"] for u in done.reanchored_draft["unanchored"]] == ["diff_unavailable"] * 2
    assert done.reanchored_draft["findings"] == []


@pytest.mark.parametrize("status", ["behind", "unavailable"])
def test_compare_without_a_diff_moves_the_right_side_findings(tmp_path: Path, status: str) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1")]))
    done, _ = run(
        request(draft_file=draft, reanchor_to=B),
        show_reply(B),
        compare_reply(None, status=status),
        dry_reply(),
    )
    assert [m.reason for m in done.plan.moved] == ["diff_unavailable"]


def test_compare_too_large_moves_the_right_side_findings(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1")]))
    too_large = error_result("compare", 20, "refused", reason="too_large", details={"limit": 100})
    done, _ = run(
        request(draft_file=draft, reanchor_to=B),
        show_reply(B),
        too_large,
        dry_reply(),
    )
    assert [m.reason for m in done.plan.moved] == ["diff_unavailable"]


def test_a_compare_for_another_head_is_stale_head(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1")]))
    error = error_of(
        request(draft_file=draft, reanchor_to=B),
        show_reply(B),
        compare_reply(SHIFT, to="d" * 40),
        pinned=False,
    )
    assert error["_exit"] == 20 and error["reason"] == "stale_head"
    assert error["details"] == {"head_sha": "d" * 40}


def test_a_failing_compare_passes_its_error_through(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1")]))
    error = error_of(
        request(draft_file=draft, reanchor_to=B),
        show_reply(B),
        error_result("compare", 12, "rate_limited", retry_after=5),
    )
    assert error["_exit"] == 12 and error["code"] == "rate_limited"
    assert error["retry_after"] == 5


def test_a_hostile_compare_diff_moves_instead_of_guessing(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1")]))
    done, _ = run(
        request(draft_file=draft, reanchor_to=B),
        show_reply(B),
        compare_reply("diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\n"),  # a hunk with no file headers
        dry_reply(),
    )
    assert [m.reason for m in done.plan.moved] == ["diff_unavailable"]


# --- review: the left-side check -------------------------------------------------------------


def test_a_left_side_finding_reads_the_merge_base_and_keeps_its_line_when_equal(
    tmp_path: Path,
) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1", side="left")]))
    done, gateway = run(request(draft_file=draft), show_reply(), diff_reply(head=A), dry_reply())
    assert [c.subcommand for c in gateway.calls] == ["show", "diff", "review"]
    assert done.plan.posted_inline == ("f1",)


def test_a_changed_merge_base_moves_a_left_side_finding(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1", side="left"), finding("f2")]))
    done, _ = run(
        request(draft_file=draft),
        show_reply(),
        diff_reply(head=A, merge_base="2" * 40),
        dry_reply(),
    )
    assert [(m.id, m.reason) for m in done.plan.moved] == [("f1", "base_changed")]
    assert done.plan.posted_inline == ("f2",)


def test_an_unknown_merge_base_moves_a_left_side_finding(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1", side="left")]))
    done, _ = run(
        request(draft_file=draft), show_reply(), diff_reply(head=A, merge_base=None), dry_reply()
    )
    assert [m.reason for m in done.plan.moved] == ["base_changed"]


def test_diff_too_large_makes_the_merge_base_unknown(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1", side="left")]))
    done, _ = run(
        request(draft_file=draft),
        show_reply(),
        error_result("diff", 20, "refused", reason="too_large", details={"limit": 1}),
        dry_reply(),
    )
    assert [m.reason for m in done.plan.moved] == ["base_changed"]


def test_the_merge_base_read_of_another_head_is_stale_head(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1", side="left")]))
    error = error_of(request(draft_file=draft), show_reply(), diff_reply(head=B))
    assert error["_exit"] == 20 and error["reason"] == "stale_head"


def test_without_a_left_side_finding_diff_is_not_read(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1")]))
    _, gateway = run(request(draft_file=draft), show_reply(), dry_reply())
    assert "diff" not in [c.subcommand for c in gateway.calls]


def test_reanchoring_reads_the_merge_base_at_b(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1", side="left")]))
    done, gateway = run(
        request(draft_file=draft, reanchor_to=B),
        show_reply(B),
        compare_reply(""),
        diff_reply(head=B),
        dry_reply(),
    )
    assert [c.subcommand for c in gateway.calls] == ["show", "compare", "diff", "review"]
    assert done.reanchored_draft is not None
    assert done.reanchored_draft["anchor"]["merge_base_sha"] == MB


# --- review: the pre-check rounds ------------------------------------------------------------


def test_an_invalid_position_round_moves_the_finding_and_the_plan_is_the_second(
    tmp_path: Path,
) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1"), finding("f2", "b.py", 4)]))
    invalid = {
        "invalid": [{"index": 1, "path": "b.py", "line": 4, "side": "right", "start_line": None}]
    }
    done, gateway = run(
        request(draft_file=draft),
        show_reply(),
        error_reply(12, "invalid_position", details=invalid),
        dry_reply(),
    )
    assert [c.subcommand for c in gateway.calls] == ["show", "review", "review"]
    assert [(m.id, m.reason) for m in done.plan.moved] == [("f2", "invalid_position")]
    assert gateway.calls[2].stdin == done.plan.input != gateway.calls[1].stdin
    # the re-anchored draft is not touched by the pre-check's moves
    assert done.precheck.rounds == ("initial", "invalid_position")


def test_a_second_invalid_position_exits_12(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1"), finding("f2", "b.py", 4)]))
    invalid = {
        "invalid": [{"index": 0, "path": "a.py", "line": 3, "side": "right", "start_line": None}]
    }
    error = error_of(
        request(draft_file=draft),
        show_reply(),
        error_reply(12, "invalid_position", details=invalid),
        error_reply(12, "invalid_position", details=invalid),
    )
    assert error["_exit"] == 12 and error["code"] == "invalid_position"


def test_the_already_posted_hint_does_not_end_the_command(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1")]))
    done, gateway = run(request(draft_file=draft), show_reply(), dry_reply(already_posted=True))
    assert done.precheck.already_posted is True
    assert document_of(done)["already_posted"] is True
    assert len(gateway.calls) == 2


@pytest.mark.parametrize(
    ("code", "exit_code", "extra"),
    [
        ("rate_limited", 12, {"retry_after": 9}),
        ("network_error", 12, {}),
        ("auth_required", 12, {}),
    ],
)
def test_rate_limits_and_network_errors_in_the_precheck_end_the_run(
    tmp_path: Path, code: str, exit_code: int, extra: dict[str, Any]
) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1")]))
    error = error_of(request(draft_file=draft), show_reply(), error_reply(exit_code, code, **extra))
    assert error["_exit"] == exit_code and error["code"] == code
    assert error["landed"] is False and error["retry"] == "same_command"


def test_a_rate_limit_during_the_reads_ends_the_run_before_the_precheck(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1")]))
    gateway = PinnedGateway([error_reply(12, "rate_limited", retry_after=3)])
    with pytest.raises(LowerFailure) as caught:
        prepare(request(draft_file=draft), env(gateway))
    assert check_outcome(caught.value.outcome).exit_code == 12
    assert [c.subcommand for c in gateway.calls] == ["show"]


def test_forgectls_dry_run_refusal_is_exit_20_with_the_lowered_envelope(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1")]))
    error = error_of(
        request(draft_file=draft),
        show_reply(),
        refusal_reply("stale_head", {"head_sha": B}),
    )
    assert error["_exit"] == 20 and error["reason"] == "stale_head"
    assert error["lower_error"]["would_refuse"]["reason"] == "stale_head"


def test_more_than_200_inline_comments_are_a_forgectl_usage_error_and_a_mismatch(
    tmp_path: Path,
) -> None:
    # forgectl takes at most 200 inline comments; pr-comment does not cap them itself, so the
    # pre-check ends with forgectl's usage error read as a contract mismatch, and nothing posts
    findings = [finding(f"f{i}", "a.py", i + 1) for i in range(201)]
    draft = write_draft(tmp_path, draft_doc(findings))
    error = error_of(
        request(draft_file=draft),
        show_reply(),
        error_reply(2, "usage"),
        pinned=False,
    )
    assert error["_exit"] == 12 and error["code"] == "lower_contract_mismatch"
    assert error["details"]["command"] == "review"


# --- plan binding and the dry-run result -----------------------------------------------------


def document_of(done: Prepared) -> dict[str, Any]:
    outcome = check_outcome(dry_run_outcome(done))
    assert outcome.exit_code == 0 and outcome.document is not None
    return outcome.document


def test_the_dry_run_result_validates_for_every_command(tmp_path: Path) -> None:
    draft = write_draft(
        tmp_path,
        draft_doc(
            [
                finding("f1", body="hi @amy", suggestion={"lines": ["```"]}),
                finding("f2", "b.py", None, severity="minor"),
            ]
        ),
    )
    body = tmp_path / "body.txt"
    body.write_text("hello @bob", encoding="utf-8")
    cases: list[tuple[Request, list[Any], str]] = [
        (request(draft_file=draft), [show_reply(), dry_reply()], "review"),
        (
            request("comment", body_file=str(body)),
            [show_reply(), dry_reply()],
            "comment",
        ),
        (
            request("reply", body_file=str(body), to="gh:rc:RC_1"),
            [show_reply(), dry_reply(threaded=True)],
            "reply",
        ),
        (
            request("approve", body_file=str(body), expected_head=A, expected_base="main"),
            [show_reply(), dry_reply()],
            "approve",
        ),
    ]
    for req, replies, name in cases:
        done, _ = run(req, *replies)
        doc = document_of(done)
        validate(f"pr-comment/cli/{name}.json", doc)
        assert doc["plan"]["plan_sha256"] == doc["plan_sha256"] == done.plan.plan_sha256
        assert doc["dry_run"] is True and doc["ids"] == [] and doc["review_id"] is None
        assert doc["already_posted"] is None
        assert doc["warnings"] == []


def test_the_review_dry_run_result_reports_the_fate_of_every_finding(tmp_path: Path) -> None:
    findings = [
        finding("f1", body="hi @amy", suggestion={"lines": ["```"]}),
        finding("f2", "b.py", None),
        finding("f3", "c.py", 5, side="left"),
    ]
    draft = write_draft(tmp_path, draft_doc(findings))
    done, _ = run(
        request(draft_file=draft),
        show_reply(),
        diff_reply(head=A, merge_base="2" * 40),
        dry_reply(),
    )
    doc = document_of(done)
    assert doc["posted_inline"] == ["f1"]
    assert doc["file_level"] == ["f2"]
    assert doc["moved"] == [{"id": "f3", "reason": "base_changed"}]
    assert doc["suggestions_degraded"] == [{"id": "f1", "reason": "fence"}]
    assert doc["mentions_neutralized"] == ["amy"]
    assert doc["event"] == "comment" and doc["draft_id"] == done.plan.draft_id
    assert doc["reanchored_draft"] is None
    assert doc["plan"]["commands"][0][:2] == ["forgectl", "review"]
    assert "--yes" in doc["plan"]["commands"][0]
    assert [b["role"] for b in doc["plan"]["bodies"]] == ["body", "inline:f1"]


def test_the_dry_run_result_carries_the_reanchored_draft(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1", "a.py", 5)]))
    done, _ = run(
        request(draft_file=draft, reanchor_to=B), show_reply(B), compare_reply(SHIFT), dry_reply()
    )
    doc = document_of(done)
    validate("pr-comment/cli/review.json", doc)
    assert doc["reanchored_draft"]["anchor"]["head_sha"] == B
    assert doc["reanchored_draft"]["findings"][0]["line"] == 7


def test_expect_plan_matching_passes_and_a_different_hash_is_plan_changed(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1")]))
    done, _ = run(request(draft_file=draft), show_reply(), dry_reply())
    good = Prepared(
        request(draft_file=draft, expect_plan=done.plan.plan_sha256),
        done.show,
        done.precheck,
        None,
    )
    assert check_binding(good) is None
    other = "0" * 64
    bad = Prepared(request(draft_file=draft, expect_plan=other), done.show, done.precheck, None)
    refused = check_outcome(check_binding(bad))  # type: ignore[arg-type]
    assert refused.exit_code == 20
    assert refused.document is not None
    validate("pr-comment/cli/error.json", refused.document)
    error = refused.document["error"]
    assert error["code"] == "refused" and error["reason"] == "plan_changed"
    assert error["details"] == {"plan_sha256": done.plan.plan_sha256}
    assert check_binding(done) is None  # no --expect-plan


def test_the_plan_changes_with_the_actor_and_the_decision_ids(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1", body="hi @amy")]))
    base, _ = run(request(draft_file=draft), show_reply(), dry_reply())
    changes: list[dict[str, Any]] = [
        {"actor": "auto:bot"},
        {"decision_ids": ("d1",)},
        {"allow_mentions": ("amy",)},
    ]
    for change in changes:
        other, _ = run(request(draft_file=draft, **change), show_reply(), dry_reply())
        assert other.plan.plan_sha256 != base.plan.plan_sha256


def test_a_footer_that_renders_to_a_non_letter_is_config_invalid(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1")]))
    config = LoadedConfig.defaults()
    config = LoadedConfig(
        config=config.config.model_copy(update={"footer": Footer(human="{user} did it")}),
        config_hash=config.config_hash,
        config_path=None,
    )
    error = error_of(request(draft_file=draft), show_reply(), config=config)
    assert error["_exit"] == 2 and error["code"] == "config_invalid"
    assert error["details"] == {"key": "footer.human"}


# --- comment, reply, approve -----------------------------------------------------------------


def body_file(tmp_path: Path, text: str | bytes = "hello", name: str = "body.txt") -> str:
    path = tmp_path / name
    path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    return str(path)


def test_a_comment_on_a_closed_pr_is_allowed(tmp_path: Path) -> None:
    done, gateway = run(
        request("comment", body_file=body_file(tmp_path)),
        show_reply(state="merged"),
        dry_reply(),
    )
    assert [c.subcommand for c in gateway.calls] == ["show", "comment"]
    no_write(gateway)
    assert done.plan.head_sha == A and done.plan.base_ref == "main"
    assert done.plan.bodies[0].text.endswith("hello\n\n<!-- pr-comment:v1 -->")


def test_a_comment_pins_the_expected_head_or_shows_and_a_moved_head_is_stale(
    tmp_path: Path,
) -> None:
    ok_done, _ = run(
        request("comment", body_file=body_file(tmp_path), expected_head=A),
        show_reply(A),
        dry_reply(),
    )
    assert ok_done.plan.head_sha == A
    error = error_of(
        request("comment", body_file=body_file(tmp_path), expected_head=B), show_reply(A)
    )
    assert error["_exit"] == 20 and error["reason"] == "stale_head"
    assert error["details"] == {"head_sha": A}


def test_a_comment_and_reply_are_hashed_with_the_head_show_read(tmp_path: Path) -> None:
    one, _ = run(request("comment", body_file=body_file(tmp_path)), show_reply(A), dry_reply())
    two, _ = run(request("comment", body_file=body_file(tmp_path)), show_reply(B), dry_reply())
    assert one.plan.plan_sha256 != two.plan.plan_sha256 and two.plan.head_sha == B


def test_a_reply_carries_to_and_forgectls_threaded(tmp_path: Path) -> None:
    done, gateway = run(
        request("reply", body_file=body_file(tmp_path), to="gh:ic:IC_1"),
        show_reply(),
        dry_reply(threaded=False),
    )
    assert "--to=gh:ic:IC_1" in gateway.calls[1].args
    assert done.precheck.threaded is False
    assert document_of(done)["threaded"] is False and document_of(done)["to"] == "gh:ic:IC_1"


def test_an_unknown_reply_target_exits_10_before_any_dialog(tmp_path: Path) -> None:
    error = error_of(
        request("reply", body_file=body_file(tmp_path), to="gh:rc:RC_nope"),
        show_reply(),
        error_reply(10, "not_found"),
    )
    assert error["_exit"] == 10 and error["details"] == {"what": "to", "id": "gh:rc:RC_nope"}


@pytest.mark.parametrize("text", ["", b"\xff\xfe"])
def test_an_empty_or_non_utf8_comment_body_is_a_usage_error_with_no_call(
    tmp_path: Path, text: str | bytes
) -> None:
    gateway = PinnedGateway([])
    with pytest.raises(RunFailure) as caught:
        prepare(request("comment", body_file=body_file(tmp_path, text)), env(gateway))
    assert check_outcome(caught.value.outcome).exit_code == 2 and gateway.calls == []


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("a\x07b", "control_character"),
        ("a\x00b", "control_character"),
        ("```suggestion\nx\n```", "suggestion_fence"),
    ],
)
def test_a_hostile_body_is_invalid_text(tmp_path: Path, text: str, kind: str) -> None:
    gateway = PinnedGateway([])
    with pytest.raises(RunFailure) as caught:
        prepare(request("comment", body_file=body_file(tmp_path, text)), env(gateway))
    error = check_outcome(caught.value.outcome).document
    assert error is not None and error["error"]["code"] == "invalid_text"
    assert error["error"]["details"] == {"pointer": "/body", "kind": kind}
    assert gateway.calls == []


def test_a_body_over_64_kib_is_refused_and_exactly_64_kib_passes(tmp_path: Path) -> None:
    gateway = PinnedGateway([])
    with pytest.raises(RunFailure):
        prepare(
            request("comment", body_file=body_file(tmp_path, "x" * (64 * 1024 + 1))), env(gateway)
        )
    assert gateway.calls == []
    done, _ = run(
        request("comment", body_file=body_file(tmp_path, "x" * (64 * 1024))),
        show_reply(),
        dry_reply(),
    )
    assert len(done.plan.input) > 64 * 1024  # the attribution line and the marker are added


def test_a_body_file_that_is_a_symlink_a_directory_or_missing_is_refused(tmp_path: Path) -> None:
    real = body_file(tmp_path)
    link = tmp_path / "link"
    os.symlink(real, link)
    for path in (str(link), str(tmp_path), str(tmp_path / "nope")):
        gateway = PinnedGateway([])
        with pytest.raises(RunFailure):
            prepare(request("comment", body_file=path), env(gateway))
        assert gateway.calls == []


def test_the_body_can_come_from_stdin(tmp_path: Path) -> None:
    done, _ = run(request("comment", body_file="-"), show_reply(), dry_reply(), stdin=b"from stdin")
    assert "from stdin" in done.plan.bodies[0].text


def test_an_approve_outside_an_interactive_run_needs_both_expected_values(tmp_path: Path) -> None:
    for kw in ({}, {"expected_head": A}, {"expected_base": "main"}):
        gateway = PinnedGateway([])
        with pytest.raises(RunFailure) as caught:
            prepare(request("approve", **kw), env(gateway))
        assert check_outcome(caught.value.outcome).exit_code == 2 and gateway.calls == []


def test_an_interactive_approve_pins_the_head_and_base_from_show() -> None:
    done, gateway = run(request("approve", interactive=True), show_reply(B), dry_reply())
    assert done.plan.head_sha == B and done.plan.base_ref == "main"
    assert f"--expected-head={B}" in gateway.calls[1].args
    assert "--expected-base=main" in gateway.calls[1].args


def test_an_approve_without_a_body_file_posts_the_attribution_and_the_marker() -> None:
    done, gateway = run(
        request("approve", expected_head=A, expected_base="main"), show_reply(), dry_reply()
    )
    assert "--body-file" in gateway.calls[1].args
    assert done.plan.bodies[0].text.endswith("<!-- pr-comment:v1 -->")
    assert gateway.calls[1].stdin == done.plan.input != b""


def test_a_blank_approve_body_file_counts_as_no_body_file(tmp_path: Path) -> None:
    blank, _ = run(
        request(
            "approve",
            body_file=body_file(tmp_path, " \n\t\r\n"),
            expected_head=A,
            expected_base="main",
        ),
        show_reply(),
        dry_reply(),
    )
    none, _ = run(
        request("approve", expected_head=A, expected_base="main"), show_reply(), dry_reply()
    )
    assert blank.plan.plan_sha256 == none.plan.plan_sha256


def test_an_approve_checks_state_head_and_base(tmp_path: Path) -> None:
    pins: dict[str, Any] = {"expected_head": A, "expected_base": "main"}
    closed = error_of(request("approve", **pins), show_reply(state="closed"))
    assert closed["reason"] == "not_open"
    stale = error_of(request("approve", **{**pins, "expected_head": B}), show_reply(A))
    assert stale["reason"] == "stale_head" and stale["details"] == {"head_sha": A}
    base = error_of(request("approve", **{**pins, "expected_base": "dev"}), show_reply())
    assert base["reason"] == "stale_base" and base["details"] == {"base_ref": "main"}


def test_an_automatic_approve_needs_an_expected_head() -> None:
    gateway = PinnedGateway([])
    with pytest.raises(RunFailure):
        prepare(
            request("approve", actor="auto:bot", expected_base="main", interactive=True),
            env(gateway),
        )
    assert gateway.calls == []


def test_a_base_ref_pr_comment_cannot_forward_is_a_usage_error(tmp_path: Path) -> None:
    error = error_of(
        request("comment", body_file=body_file(tmp_path)), show_reply(base_ref="HEAD@{1}")
    )
    assert error["_exit"] == 2 and error["code"] == "usage"


def test_nothing_is_ever_posted_by_the_orders_up_to_the_plan(tmp_path: Path) -> None:
    draft = write_draft(tmp_path, draft_doc([finding("f1")]))
    body = body_file(tmp_path)
    runs: list[tuple[Request, list[Any]]] = [
        (request(draft_file=draft), [show_reply(), dry_reply()]),
        (request("comment", body_file=body), [show_reply(), dry_reply()]),
        (
            request("reply", body_file=body, to="gh:rc:RC_1"),
            [show_reply(), dry_reply(threaded=True)],
        ),
        (request("approve", interactive=True), [show_reply(), dry_reply()]),
    ]
    for req, replies in runs:
        _, gateway = run(req, *replies)
        no_write(gateway)
        assert all(
            c.subcommand in ("show", "review", "comment", "reply", "approve") for c in gateway.calls
        )
        assert sum(1 for c in gateway.calls if "--dry-run" in c.args) == 1


# --- the capped mention report ---------------------------------------------------------------


def test_the_mention_report_is_capped_and_its_schema_says_so(tmp_path: Path) -> None:
    names = " ".join(f"@user{i}" for i in range(150)) + " @" + "n" * 200
    body = body_file(tmp_path, names)
    done, _ = run(request("comment", body_file=body), show_reply(), dry_reply())
    doc = document_of(done)
    assert len(doc["mentions_neutralized"]) == 100  # later new names are dropped, silently
    assert all(len(n) <= 128 for n in doc["mentions_neutralized"])
    assert done.plan.counts["mentions_neutralized"] == 151  # the count stays exact
    validate("pr-comment/cli/comment.json", doc)
    too_many = {**doc, "mentions_neutralized": [f"u{i}" for i in range(101)]}
    with pytest.raises(Exception, match="maxItems"):
        validate("pr-comment/cli/comment.json", too_many)
    too_long = {**doc, "mentions_neutralized": ["n" * 129]}
    with pytest.raises(Exception, match="maxLength"):
        validate("pr-comment/cli/comment.json", too_long)
