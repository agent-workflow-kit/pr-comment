"""The review order's write in process (``src/pr_comment/post.py``): what is posted, how every
answer of forgectl (a success, a failure, a bad document, a signal, a deadline) ends the run and
what the audit record says about it, against ``FakeGateway`` and ``PinnedGateway``."""

from __future__ import annotations

import errno
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pr_comment.actor import RunContext
from pr_comment.audit import AuditTrail
from pr_comment.config import LoadedConfig
from pr_comment.files import StateDirs, open_state_dirs
from pr_comment.gateway import (
    ForgectlInterrupted,
    ForgectlResult,
    GatewayInternalError,
    ToolMissing,
)
from pr_comment.jsonlog import LogWriter
from pr_comment.output import Outcome
from pr_comment.post import PostEnv, audit_record, lower_result_of, post_review
from pr_comment.prepare import Env, Prepared, Request, prepare
from tests import forgectl_harness as h
from tests.fake_gateway import Call, FakeGateway, ok
from tests.posting import PR, A, draft_doc, dry_reply, finding
from tests.schema_harness import validate
from tests.test_prepare import show_reply, write_draft

RUN = RunContext("human:alice", "flag", False, False)


class Rig:
    """A prepared review, an audit trail in a private state directory and a gateway that answers
    the write as the test scripts it."""

    def __init__(self, tmp_path: Path, *, backend: str = "github") -> None:
        pr = h.GITLAB_PR if backend == "gitlab" else PR
        self.backend, self.pr = backend, pr
        self.state = tmp_path / "state"
        self.dirs: StateDirs = open_state_dirs({"XDG_STATE_HOME": str(self.state)})
        self.trail = AuditTrail.open(self.dirs)
        self.log = LogWriter.open(self.dirs)
        self.draft = write_draft(
            tmp_path, draft_doc([finding("f1"), finding("f2", "b.py", None)], pr=pr)
        )
        self.gateway = FakeGateway(
            [show_reply(backend=backend, pr=pr), dry_reply(backend)], version="forgectl 0.55.2"
        )
        request = Request(
            command="review",
            pr=pr,
            actor="human:alice",
            draft_file=self.draft,
            decision_ids=("d1",),
        )
        self.prepared: Prepared = prepare(
            request,
            Env(
                gateway=self.gateway,
                config=LoadedConfig.defaults(),
                me_name="alice",
                tool_version="0.1.0",
            ),
        )
        self.env = PostEnv(
            gateway=self.gateway,
            trail=self.trail,
            log=self.log,
            run=RUN,
            config=LoadedConfig.defaults(),
        )

    def post(self, *replies: Any) -> Outcome:
        for reply in replies:
            self.gateway.push(reply)
        return post_review(self.prepared, self.env)

    @property
    def write_calls(self) -> list[Call]:
        return [c for c in self.gateway.calls if "--dry-run" not in c.args][1:]

    def audit(self) -> list[dict[str, Any]]:
        path = self.state / "pr-comment" / "audit.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()]

    def close(self) -> None:
        self.trail.close()
        self.log.close()
        self.dirs.close()


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    made = Rig(tmp_path)
    yield made
    made.close()


def posted_doc(pr: str = PR, **kw: Any) -> dict[str, Any]:
    kw.setdefault("ids", ["gh:rc:RC_1"])
    kw.setdefault("review_id", "gh:r:R_1")
    doc = h.post_doc("review", pr, **kw)
    doc.setdefault("neutralized_lines", 0)
    doc.setdefault("markers_removed", 0)
    return doc


def error_of(outcome: Outcome) -> dict[str, Any]:
    assert outcome.document is not None
    validate("pr-comment/cli/error.json", outcome.document)
    error: dict[str, Any] = outcome.document["error"]
    return error


def raw(exit_code: int | None, stdout: bytes = b"", **kw: Any) -> ForgectlResult:
    return ForgectlResult(command="review", write=True, exit_code=exit_code, stdout=stdout, **kw)


def envelope(code: str, **extra: Any) -> bytes:
    return json.dumps({"error": {"code": code, "message": "scripted", **extra}}).encode()


# --- what is sent -------------------------------------------------------------------------------


def test_the_write_sends_the_confirmed_plans_input_and_command(rig: Rig) -> None:
    outcome = rig.post(ok("review", posted_doc()))
    assert outcome.exit_code == 0
    plan = rig.prepared.plan
    (write,) = rig.write_calls
    assert write.stdin == plan.input  # byte for byte, the object the dialog showed
    assert list(write.args) == plan.write_args()
    assert [a for a in plan.command()[2:] if a != "--json"] == plan.write_args()
    assert f"--expected-head={A}" in write.args and "--expected-base=main" in write.args
    assert f"--dedupe-key={plan.dedupe_key}" in write.args
    assert "--decision-id=d1" in write.args and "--actor=human:alice" in write.args
    assert "--yes" in write.args


def test_the_plan_is_never_rebuilt_for_the_write(rig: Rig) -> None:
    before = (rig.prepared.plan.input, rig.prepared.plan.plan_sha256)
    rig.post(ok("review", posted_doc()))
    assert (rig.prepared.plan.input, rig.prepared.plan.plan_sha256) == before
    assert [c.subcommand for c in rig.gateway.calls] == ["show", "review", "review"]


def test_the_lower_version_is_read_before_the_write_and_never_after(rig: Rig) -> None:
    reads: list[str] = []
    original = rig.gateway.lower_version

    def counting() -> str | None:
        reads.append("read")
        return original()

    rig.gateway.lower_version = counting  # type: ignore[method-assign]
    rig.post(raw(0, b"not json"))  # a mismatch needs the version
    assert len(reads) >= 1 and len(rig.write_calls) == 1


# --- success ------------------------------------------------------------------------------------


def test_the_result_document_validates_and_the_record_is_done(rig: Rig) -> None:
    outcome = rig.post(ok("review", posted_doc(head_verified=False)))
    doc = outcome.document
    assert outcome.exit_code == 0 and doc is not None
    validate("pr-comment/cli/review.json", doc)
    assert doc["head_verified"] is False and doc["plan"] is None and doc["dry_run"] is False
    assert doc["suggestions_degraded"] == [] and doc["moved"] == []
    records = rig.audit()
    assert [r["result"] for r in records] == ["attempted", "done"]
    assert records[1]["writes"][0]["lower"]["ids"] == ["gh:rc:RC_1"]


def test_forgectls_own_warnings_come_first_then_prcomments(rig: Rig) -> None:
    doc = posted_doc(warnings=["read_back_failed"], neutralized_lines=2)
    outcome = rig.post(ok("review", doc))
    assert outcome.document is not None
    assert outcome.document["warnings"] == ["read_back_failed", "neutralizing_mismatch"]
    validate("pr-comment/cli/review.json", outcome.document)


def test_a_gitlab_review_says_the_body_thread_was_not_resolved(tmp_path: Path) -> None:
    gl = Rig(tmp_path, backend="gitlab")
    try:
        assert gl.prepared.plan.follow_ups  # TODO(#36) performs it
        outcome = gl.post(
            ok("review", posted_doc(h.GITLAB_PR, ids=["gl:n:3"], review_id=None, thread_ids=[]))
        )
        assert outcome.document is not None
        assert outcome.document["body_thread_resolved"] is False
        assert outcome.document["warnings"] == ["body_thread_unresolved"]
        assert [r["result"] for r in gl.audit()] == ["attempted", "done"]
    finally:
        gl.close()


# --- a document that is not the real run's answer ------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"action": "comment"},
        {"dry_run": True},
        {"pr": "github.com/acme/widgets#8"},
        {"ids": ["not an id"]},
        {"ids": "gh:rc:RC_1"},
        {"warnings": ["Not-Valid"]},
        {"already_posted": "yes"},
        {"review_id": 7},
        {"neutralized_lines": -1},
    ],
)
def test_a_bad_answer_after_exit_0_is_a_mismatch_that_landed(
    rig: Rig, change: dict[str, Any]
) -> None:
    doc = {**posted_doc(), **change}
    outcome = rig.post(ForgectlResult("review", True, 0, stdout=json.dumps(doc).encode()))
    error = error_of(outcome)
    assert outcome.exit_code == 12 and error["code"] == "lower_contract_mismatch"
    assert (error["landed"], error["retry"]) == (True, "none")  # forgectl said it posted
    assert len(rig.write_calls) == 1
    assert [r["result"] for r in rig.audit()] == ["attempted", "done"]


@pytest.mark.parametrize("stdout", [b"", b"[]", b'{"a": 1, "a": 2}', b"\xff"])
def test_no_usable_document_after_exit_0_is_a_mismatch_that_landed(rig: Rig, stdout: bytes) -> None:
    error = error_of(rig.post(raw(0, stdout)))
    assert error["code"] == "lower_contract_mismatch"
    assert (error["landed"], error["retry"]) == (True, "none")


def test_an_unknown_exit_status_is_unknown_and_recorded_as_unknown_not_failed(rig: Rig) -> None:
    error = error_of(rig.post(raw(99, b"")))
    assert error["code"] == "lower_contract_mismatch"
    assert (error["landed"], error["retry"]) == (None, "same_command")
    records = rig.audit()
    assert records[1]["result"] == "outcome_unknown"
    assert records[1]["writes"][0]["lower"]["code"] == "outcome_unknown"


# --- failures that are not a clean answer ---------------------------------------------------------


def test_the_deadline_is_outcome_unknown_with_forgectls_envelope(rig: Rig) -> None:
    outcome = rig.post(raw(12, envelope("network_error"), timed_out=True))
    error = error_of(outcome)
    assert error["code"] == "outcome_unknown"
    assert (error["landed"], error["retry"]) == (None, "same_command")
    assert error["lower_error"]["code"] == "network_error"
    assert rig.audit()[1]["result"] == "outcome_unknown"
    assert len(rig.write_calls) == 1


def test_a_forgectl_killed_by_a_signal_is_outcome_unknown(rig: Rig) -> None:
    error = error_of(rig.post(raw(None, b"", signal=9)))
    assert error["code"] == "outcome_unknown" and error["landed"] is None


def test_a_signal_during_the_write_ends_as_outcome_unknown_and_records_forgectls_answer(
    rig: Rig,
) -> None:
    interrupted = ForgectlInterrupted(
        15, ForgectlResult("review", True, 12, stdout=envelope("outcome_unknown"))
    )
    outcome = rig.post(interrupted)
    error = error_of(outcome)
    assert outcome.exit_code == 12 and error["code"] == "outcome_unknown"
    assert (error["landed"], error["retry"]) == (None, "same_command")
    assert error["lower_error"]["code"] == "outcome_unknown"
    assert rig.audit()[1]["result"] == "outcome_unknown"
    assert len(rig.write_calls) == 1


def test_a_signal_after_forgectl_finished_still_reports_unknown_but_records_what_landed(
    rig: Rig,
) -> None:
    done = ForgectlResult("review", True, 0, stdout=json.dumps(posted_doc()).encode())
    outcome = rig.post(ForgectlInterrupted(2, done))
    error = error_of(outcome)
    assert error["code"] == "outcome_unknown" and error["retry"] == "same_command"
    records = rig.audit()
    assert records[1]["result"] == "done"  # the record keeps forgectl's own answer
    assert records[1]["writes"][0]["lower"]["ids"] == ["gh:rc:RC_1"]


def test_a_signal_with_no_result_at_all_is_outcome_unknown(rig: Rig) -> None:
    outcome = rig.post(ForgectlInterrupted(1, ForgectlResult("review", True, None)))
    assert error_of(outcome)["code"] == "outcome_unknown"
    assert rig.audit()[1]["result"] == "outcome_unknown"


def test_forgectl_missing_at_the_write_is_tool_missing_and_recorded_as_failed(rig: Rig) -> None:
    outcome = rig.post(ToolMissing("forgectl is not installed"))
    error = error_of(outcome)
    assert error["code"] == "tool_missing" and (error["landed"], error["retry"]) == (
        False,
        "same_command",
    )
    records = rig.audit()
    assert [r["result"] for r in records] == ["attempted", "failed"]
    assert records[1]["writes"][0]["lower"]["code"] == "tool_missing"


def test_an_internal_gateway_failure_is_recorded_and_ends_as_internal(rig: Rig) -> None:
    outcome = rig.post(GatewayInternalError("boom"))
    assert outcome.exit_code == 1 and error_of(outcome)["code"] == "internal"
    assert [r["result"] for r in rig.audit()] == ["attempted", "failed"]


# --- forgectl's failures ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exit_code", "code", "extra", "result", "pair"),
    [
        (12, "rate_limited", {"retry_after": 3}, "failed", (False, "same_command")),
        (12, "outcome_unknown", {}, "outcome_unknown", (None, "same_command")),
        (12, "quick_action_ran", {"details": {"ids": ["gh:rc:RC_1"]}}, "partial", (True, "none")),
        (
            12,
            "approved_stale_head",
            {"details": {"ids": ["gh:rc:RC_1"]}},
            "partial",
            (True, "none"),
        ),
        (11, "vetoed", {"hook": "pre-post", "hook_stderr": ""}, "failed", None),
        (2, "body_too_large", {"details": {"index": 0, "limit": 5}}, "failed", None),
    ],
)
def test_forgectls_failure_keeps_its_exit_code_and_code(
    rig: Rig,
    exit_code: int,
    code: str,
    extra: dict[str, Any],
    result: str,
    pair: tuple[bool | None, str] | None,
) -> None:
    outcome = rig.post(raw(exit_code, envelope(code, **extra)))
    error = error_of(outcome)
    assert outcome.exit_code == exit_code and error["code"] == code
    if pair is not None:
        assert (error["landed"], error["retry"]) == pair
    assert rig.audit()[1]["result"] == result
    assert len(rig.write_calls) == 1


def test_a_refusal_the_review_cannot_give_is_a_mismatch(rig: Rig) -> None:
    error = error_of(rig.post(raw(20, envelope("refused", reason="not_resolvable"))))
    assert error["code"] == "lower_contract_mismatch"
    assert (error["landed"], error["retry"]) == (None, "same_command")
    assert rig.audit()[1]["result"] == "outcome_unknown"


def test_outcome_partial_needs_and_gets_the_backend(rig: Rig) -> None:
    details = {"landed": {"ids": [], "thread_ids": []}, "failed": "approve"}
    error = error_of(rig.post(raw(12, envelope("outcome_partial", details=details))))
    assert (error["code"], error["landed"], error["retry"]) == ("outcome_partial", False, "new_key")


# --- the audit failing --------------------------------------------------------------------------


def test_an_unavailable_audit_file_sends_nothing(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    from pr_comment.appendfile import AppendError, AppendFile

    def refuse(self: AppendFile, line: bytes, *, sync: bool = False) -> None:
        raise AppendError("disk full", "audit.jsonl", OSError(errno.ENOSPC, "full"))

    monkeypatch.setattr(AppendFile, "append", refuse)
    outcome = rig.post()
    error = error_of(outcome)
    assert error["code"] == "audit_unavailable" and (error["landed"], error["retry"]) == (
        False,
        "same_command",
    )
    assert rig.write_calls == []


def test_a_landed_write_is_never_retried_when_the_outcome_cannot_be_recorded(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pr_comment.appendfile import AppendError, AppendFile

    original = AppendFile.append
    count = {"n": 0}

    def flaky(self: AppendFile, line: bytes, *, sync: bool = False) -> None:
        count["n"] += 1
        if count["n"] >= 2:
            raise AppendError("fsync failed", "audit.jsonl", OSError(errno.EIO, "io"), written=True)
        original(self, line, sync=sync)

    monkeypatch.setattr(AppendFile, "append", flaky)
    outcome = rig.post(ok("review", posted_doc()))
    error = error_of(outcome)
    assert outcome.exit_code == 12 and error["code"] == "audit_failed"
    assert (error["landed"], error["retry"]) == (True, "none")
    assert error["details"]["outcome"]["review_id"] == "gh:r:R_1"
    assert len(rig.write_calls) == 1


# --- helpers ------------------------------------------------------------------------------------


def test_the_audit_record_holds_hashes_and_counts_never_a_body(rig: Rig) -> None:
    record = audit_record(rig.prepared, rig.env)
    inputs = record.inputs.to_dict()
    assert inputs["plan_sha256"] == rig.prepared.plan.plan_sha256
    assert inputs["draft_sha256"] == rig.prepared.draft_sha256 and inputs["draft_sha256"]
    assert [b["role"] for b in inputs["bodies"]] == ["body", "inline:f1"]
    assert all(len(b["sha256"]) == 64 for b in inputs["bodies"])
    assert "text" not in json.dumps(inputs)
    assert inputs["reanchor_to"] is None and inputs["anchor_head"] == A
    assert record.target == rig.prepared.plan.pr and record.decision_ids == ("d1",)


@pytest.mark.parametrize(
    ("document", "exit_code", "code", "result"),
    [
        (
            {"error": {"code": "refused", "reason": "stale_head", "landed": None}},
            20,
            "refused",
            "refused",
        ),
        (
            {"error": {"code": "host_rejected", "details": {"leftover_drafts": 0}}},
            12,
            "host_rejected",
            "failed",
        ),
    ],
)
def test_lower_result_of_reads_the_error_document(
    document: dict[str, Any], exit_code: int, code: str, result: str
) -> None:
    from pr_comment.audit import classify_write

    lower = lower_result_of(Outcome(exit_code, document), None)
    assert (lower.exit, lower.code) == (exit_code, code)
    assert classify_write(lower) == result
    assert lower.leftover_drafts is (code == "host_rejected")
