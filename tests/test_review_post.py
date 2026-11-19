"""The review order's write (issue #29), through the real CLI against the fake forgectl process
(``tests/fakebin/forgectl``, pinned to forgectl's own schemas): the post, its audit pair, the
exit codes and what is never done (a retry, a rebuilt plan, a post without a record).

Every test runs with a private HOME and state directory; no real forgectl is ever called.
"""

from __future__ import annotations

import errno
import io
import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pr_comment import cli as cli_module
from pr_comment.cli import main
from tests import forgectl_harness as h
from tests.forgectl_harness import (
    FakeForgectl,
    ProcessCall,
    assert_edge_rules,
    fail,
    ok,
    refuse,
)
from tests.posting import PR, A, B, draft_doc, finding
from tests.schema_harness import validate

Run = Callable[..., tuple[int, Any, str]]

GL_PR = h.GITLAB_PR
ACTOR = "human:alice"


@pytest.fixture
def state_home(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def cli(
    forgectl: FakeForgectl,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state_home: Path,
) -> Run:
    """``cli(argv)`` runs ``main`` with the fake first on PATH: (exit, stdout JSON, stderr)."""
    env = forgectl.environ({"XDG_STATE_HOME": str(state_home)})
    for key in [k for k in os.environ if k.startswith("XDG_") and k not in env]:
        monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    def run(*argv: str, stdin: bytes | None = None) -> tuple[int, Any, str]:
        if stdin is not None:
            fake = type("Stdin", (), {"buffer": io.BytesIO(stdin), "isatty": lambda self: False})()
            monkeypatch.setattr(sys, "stdin", fake)
        code = main([*argv])
        captured = capsys.readouterr()
        document = json.loads(captured.out) if captured.out.strip() else None
        return code, document, captured.err

    return run


# --- builders ---------------------------------------------------------------------------------


def show(head: str = A, pr: str = PR, **kw: Any) -> dict[str, Any]:
    doc = h.show_doc(pr, **kw)
    doc["head"] = {**doc["head"], "sha": head}
    return ok("show", doc)


def draft_file(tmp_path: Path, findings: list[dict[str, Any]], **kw: Any) -> str:
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(draft_doc(findings, **kw)), encoding="utf-8")
    return str(path)


def dry(pr: str = PR, **kw: Any) -> dict[str, Any]:
    return ok("review", h.post_doc("review", pr, dry_run=True, **kw))


def posted(pr: str = PR, **kw: Any) -> dict[str, Any]:
    kw.setdefault("ids", ["gh:rc:RC_1", "gh:rc:RC_2"])
    kw.setdefault("review_id", "gh:r:R_1")
    return ok("review", h.post_doc("review", pr, **kw))


def args_for(draft: str, *extra: str, pr: str = PR) -> tuple[str, ...]:
    return ("review", pr, "--draft-file", draft, "--json", "--actor", ACTOR, "--yes", *extra)


def state_dir(state_home: Path) -> Path:
    return state_home / "pr-comment"


def lines_of(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="ascii").splitlines() if x.strip()]


def audit(state_home: Path) -> list[dict[str, Any]]:
    return lines_of(state_dir(state_home) / "audit.jsonl")


def log(state_home: Path) -> list[dict[str, Any]]:
    return lines_of(state_dir(state_home) / "log.jsonl")


def calls_of(forgectl: FakeForgectl) -> list[ProcessCall]:
    """The calls but the ``--version`` read (the lower version, read once per write)."""
    return [c for c in forgectl.calls() if c.subcommand != "--version"]


def reviews(forgectl: FakeForgectl) -> list[ProcessCall]:
    return [c for c in forgectl.calls() if c.subcommand == "review"]


def sent(call: ProcessCall) -> dict[str, Any]:
    assert call.stdin is not None
    document: dict[str, Any] = json.loads(call.stdin)
    return document


def flag(call: ProcessCall, name: str) -> list[str]:
    return [a.split("=", 1)[1] for a in call.argv if a.startswith(name + "=")]


def check_edge(forgectl: FakeForgectl, tmp_path: Path, **kw: Any) -> None:
    assert_edge_rules(forgectl.calls(), root=tmp_path, **kw)


def validate_audit(records: list[dict[str, Any]]) -> None:
    for record in records:
        validate("pr-comment/audit.json", record)
        assert set(record["actor"]) and record["tool"] == "pr-comment"


# --- the post ----------------------------------------------------------------------------------


def test_a_confirmed_review_posts_the_plan_byte_for_byte_and_audits_the_pair(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1"), finding("f2", "b.py", None, body="file @amy")])
    forgectl.script(show(), dry(), posted())
    code, doc, _ = cli(*args_for(draft, "--decision-id", "d1"))
    assert code == 0, doc
    validate("pr-comment/cli/review.json", doc)
    assert doc["dry_run"] is False and doc["plan"] is None
    assert doc["ids"] == ["gh:rc:RC_1", "gh:rc:RC_2"] and doc["review_id"] == "gh:r:R_1"
    assert doc["already_posted"] is False and doc["follow_up_done"] is False
    assert doc["posted_inline"] == ["f1"] and doc["file_level"] == ["f2"]
    assert doc["mentions_neutralized"] == ["amy"]
    assert doc["body_thread_resolved"] is None and doc["reanchored_draft"] is None
    assert doc["warnings"] == [] and doc["head_verified"] is None

    calls = calls_of(forgectl)
    assert [c.subcommand for c in calls] == ["show", "review", "review"]
    pre, real = calls[1], calls[2]
    assert pre.is_dry_run and real.is_write
    # (b) the real call carries exactly the plan the pre-check proved, and the pins of the plan
    assert real.stdin == pre.stdin and real.stdin is not None
    assert flag(real, "--expected-head") == [A] and flag(real, "--expected-base") == ["main"]
    assert flag(real, "--dedupe-key") == [doc["dedupe_key"]]
    assert flag(real, "--decision-id") == ["d1"] and flag(real, "--actor") == [ACTOR]
    assert "--yes" in real.argv and "--dry-run" not in real.argv
    review = sent(real)
    assert review["event"] == "comment" and len(review["comments"]) == 1
    check_edge(
        forgectl,
        tmp_path,
        texts=[review["body"], review["comments"][0]["body"]],
        confirmed=True,
        head=A,
        base="main",
        confirmed_input=real.stdin,
    )

    records = audit(state_home)
    validate_audit(records)
    attempted, outcome = records
    assert (attempted["result"], outcome["result"]) == ("attempted", "done")
    assert attempted["attempt_id"] == outcome["attempt_id"]
    assert attempted["action"] == "review" and attempted["target"] == PR
    assert attempted["decision_id"] == ["d1"] and attempted["actor"] == ACTOR
    assert outcome["inputs"]["plan_sha256"] == doc["plan_sha256"]
    assert outcome["inputs"]["dedupe_key"] == doc["dedupe_key"]
    assert outcome["inputs"]["pinned_head"] == A and outcome["inputs"]["event"] == "comment"
    (write,) = outcome["writes"]
    assert (write["index"], write["action"], write["result"]) == (0, "review", "done")
    assert write["lower"]["ids"] == ["gh:rc:RC_1", "gh:rc:RC_2"]
    assert write["lower"]["review_id"] == "gh:r:R_1" and write["dedupe_key"] == doc["dedupe_key"]
    assert "attempted" not in json.dumps(outcome["writes"])
    # no body in the record, only hashes (the draft text is not in the audit file)
    text = (state_dir(state_home) / "audit.jsonl").read_text()
    assert "file @" not in text and "body f1" not in text


def test_already_posted_is_success_and_is_recorded_as_such(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry(already_posted=True), posted(already_posted=True))
    code, doc, _ = cli(*args_for(draft))
    assert code == 0
    validate("pr-comment/cli/review.json", doc)
    assert doc["already_posted"] is True
    outcome = audit(state_home)[1]
    assert outcome["result"] == "already_posted"
    assert outcome["writes"][0]["result"] == "already_posted"
    assert outcome["writes"][0]["lower"]["already_posted"] is True


def test_the_dry_run_writes_no_audit_record_and_no_write(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry())
    code, doc, _ = cli(*args_for(draft, "--dry-run"))
    assert code == 0 and doc["dry_run"] is True
    assert forgectl.writes() == []
    assert audit(state_home) == []


def test_forgectl_reporting_neutralized_lines_on_the_real_call_is_a_warning(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the fake computes forgectl's counts from the real stdin and rejects a scripted other value,
    # so a text pr-comment left un-neutralized is built by making the plan carry a marker line
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry(), posted())
    from pr_comment import plan as plan_module

    original = plan_module.review_input

    def raw_input(event: str, body: str, comments: Any) -> bytes:
        return original(event, body + "\n/approve", comments)

    monkeypatch.setattr(plan_module, "review_input", raw_input)
    code, doc, _ = cli(*args_for(draft))
    assert code == 0, doc
    assert "neutralizing_mismatch" in doc["warnings"]
    events = [e for e in log(state_home) if e["event"] == "neutralizing_mismatch"]
    assert len(events) == 1 and events[0]["details"]["neutralized_lines"] >= 1


# --- exit codes: forgectl's answers pass through with the write's own landed and retry ----------


@pytest.mark.parametrize(
    ("code_name", "exit_code", "extra"),
    [
        ("rate_limited", 12, {"retry_after": 7}),
        ("network_error", 12, {}),
        ("upstream_error", 12, {}),
        ("auth_required", 12, {}),
        ("permission_denied", 12, {}),
    ],
)
def test_a_host_failure_of_the_write_is_exit_12_not_landed_and_never_retried(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    code_name: str,
    exit_code: int,
    extra: dict[str, Any],
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry(), fail("review", code_name, phase="write", **extra))
    code, doc, _ = cli(*args_for(draft))
    assert code == exit_code
    validate("pr-comment/cli/error.json", doc)
    error = doc["error"]
    assert error["code"] == code_name and error["landed"] is False
    assert error["retry"] == "same_command" and error["lower_error"]["code"] == code_name
    assert len(reviews(forgectl)) == 2  # the pre-check and one write: never a second write
    assert len(forgectl.writes()) == 1
    records = audit(state_home)
    validate_audit(records)
    assert records[1]["result"] == "failed"
    assert records[1]["writes"][0]["lower"]["code"] == code_name


def test_outcome_unknown_is_reported_unknown_and_not_retried(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry(), fail("review", "outcome_unknown"))
    code, doc, _ = cli(*args_for(draft))
    assert code == 12
    validate("pr-comment/cli/error.json", doc)
    error = doc["error"]
    assert error["code"] == "outcome_unknown"
    assert error["landed"] is None and error["retry"] == "same_command"
    assert len(forgectl.writes()) == 1
    records = audit(state_home)
    assert records[1]["result"] == "outcome_unknown"
    assert records[1]["writes"][0]["lower"]["code"] == "outcome_unknown"


def test_github_outcome_partial_is_not_landed_and_needs_a_new_key(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    details = {"landed": {"ids": [], "thread_ids": []}, "failed": "request_changes"}
    forgectl.script(show(), dry(), fail("review", "outcome_partial", details=details))
    code, doc, _ = cli(*args_for(draft))
    assert code == 12
    error = doc["error"]
    assert error["code"] == "outcome_partial"
    assert (error["landed"], error["retry"]) == (False, "new_key")
    assert audit(state_home)[1]["result"] == "partial"
    assert len(forgectl.writes()) == 1


def test_github_event_change_is_outcome_partial_from_the_dry_run_with_no_write(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    # real forgectl reads the dedupe key before the dry run stops, so the refusal to apply a new
    # event to a reused key comes from the pre-check (#93); nothing is sent, nothing is audited
    draft = draft_file(tmp_path, [finding("f1")])
    details = {"landed": {"ids": ["gh:r:PRR_1"], "thread_ids": []}, "failed": "request_changes"}
    forgectl.script(show(), fail("review", "outcome_partial", details=details, phase="read"))
    code, doc, _ = cli(*args_for(draft))
    assert code == 12
    error = doc["error"]
    assert error["code"] == "outcome_partial"
    assert (error["landed"], error["retry"]) == (False, "new_key")
    assert error["details"] == details
    assert forgectl.writes() == []
    assert audit(state_home) == []


@pytest.mark.parametrize(
    ("landed", "want"),
    [
        ({"ids": ["gl:n:5"], "thread_ids": []}, True),
        ({"ids": [], "thread_ids": ["gl:d:" + "2" * 40]}, True),
        ({"ids": [], "thread_ids": []}, False),
    ],
)
def test_gitlab_outcome_partial_follows_what_landed(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    landed: dict[str, Any],
    want: bool,
) -> None:
    draft = draft_file(tmp_path, [finding("f1")], pr=GL_PR)
    details = {"landed": landed, "failed": "request_changes"}
    forgectl.script(
        show(pr=GL_PR, backend="gitlab"),
        dry(GL_PR),
        fail("review", "outcome_partial", details=details),
    )
    code, doc, _ = cli(*args_for(draft, pr=GL_PR))
    assert code == 12
    error = doc["error"]
    assert (error["landed"], error["retry"]) == (want, "same_command")
    assert audit(state_home)[1]["result"] == "partial"


@pytest.mark.parametrize(
    ("reason", "details", "backend"),
    [
        ("stale_head", {"head_sha": B}, "github"),
        ("pending_review_exists", {}, "github"),
        ("drafts_present", {"count": 2}, "gitlab"),
    ],
)
def test_a_refusal_of_the_real_call_passes_through_as_exit_20(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    reason: str,
    details: dict[str, Any],
    backend: str,
) -> None:
    pr = GL_PR if backend == "gitlab" else PR
    draft = draft_file(tmp_path, [finding("f1")], pr=pr)
    forgectl.script(
        show(pr=pr, backend=backend),
        dry(pr),
        refuse("review", reason, details=details, phase="write"),
    )
    code, doc, _ = cli(*args_for(draft, pr=pr))
    assert code == 20
    validate("pr-comment/cli/error.json", doc)
    assert doc["error"]["code"] == "refused" and doc["error"]["reason"] == reason
    assert doc["error"]["landed"] is None and doc["error"]["lower_error"]["reason"] == reason
    records = audit(state_home)
    assert [r["result"] for r in records] == ["attempted", "refused"]
    assert records[1]["writes"][0]["lower"]["reason"] == reason
    assert len(forgectl.writes()) == 1
    assert [e["event"] for e in log(state_home) if e["event"] == "refused"] == ["refused"]
    assert {e["attempt_id"] for e in log(state_home) if e["event"] == "refused"} == {
        records[0]["attempt_id"]
    }


def test_a_veto_of_forgectls_hook_passes_through_as_exit_11(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(
        show(), dry(), fail("review", "vetoed", hook="pre-post", hook_stderr="no", phase="write")
    )
    code, doc, _ = cli(*args_for(draft))
    assert code == 11
    assert doc["error"]["code"] == "vetoed" and doc["error"]["lower_error"]["code"] == "vetoed"
    assert audit(state_home)[1]["result"] == "failed"
    assert len(forgectl.writes()) == 1
    vetoed = [e for e in log(state_home) if e["event"] == "vetoed"]
    assert [e["level"] for e in vetoed] == ["warn"]
    assert vetoed[0]["attempt_id"] == audit(state_home)[0]["attempt_id"]
    assert vetoed[0]["details"]["code"] == "vetoed"


def test_an_escalation_of_forgectls_hook_passes_through_as_exit_13_and_is_logged(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(
        show(),
        dry(),
        fail(
            "review",
            "escalated",
            hook="pre-post",
            hook_stderr="",
            escalated_hooks=[{"hook": "pre-post", "index": 0}],
            phase="write",
        ),
    )
    code, doc, _ = cli(*args_for(draft))
    assert code == 13 and doc["error"]["code"] == "escalated"
    escalated = [e for e in log(state_home) if e["event"] == "escalated"]
    assert [e["level"] for e in escalated] == ["warn"]
    assert escalated[0]["details"]["code"] == "escalated"


def test_invalid_position_on_the_real_call_is_exit_12_with_nothing_moved(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(
        show(),
        dry(),
        fail(
            "review",
            "invalid_position",
            details={
                "invalid": [
                    {"index": 0, "path": "a.py", "line": 3, "side": "right", "start_line": None}
                ]
            },
            phase="read",
        ),
    )
    code, doc, _ = cli(*args_for(draft))
    assert code == 12
    assert doc["error"]["code"] == "invalid_position"
    assert (doc["error"]["landed"], doc["error"]["retry"]) == (False, "same_command")
    assert len(forgectl.writes()) == 1  # nothing was re-planned and re-sent
    assert audit(state_home)[1]["result"] == "failed"


def test_host_rejected_with_leftover_drafts_is_not_a_rerun(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")], pr=GL_PR)
    forgectl.script(
        show(pr=GL_PR, backend="gitlab"),
        dry(GL_PR),
        fail("review", "host_rejected", details={"status": 422, "leftover_drafts": 2}),
    )
    code, doc, _ = cli(*args_for(draft, pr=GL_PR))
    assert code == 12
    assert (doc["error"]["landed"], doc["error"]["retry"]) == (False, "none")


# --- before the write -------------------------------------------------------------------------


def test_a_stale_head_refuses_before_any_pre_check_or_audit_record(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(head=B))
    code, doc, _ = cli(*args_for(draft))
    assert code == 20 and doc["error"]["reason"] == "stale_head"
    assert [c.subcommand for c in calls_of(forgectl)] == ["show"]
    assert audit(state_home) == []
    assert [e["event"] for e in log(state_home)] == ["refused"]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [({"state": "closed"}, "not_open")],
)
def test_not_open_refuses_before_rendering(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    overrides: dict[str, Any],
    reason: str,
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(**overrides))
    code, doc, _ = cli(*args_for(draft))
    assert code == 20 and doc["error"]["reason"] == reason
    assert [c.subcommand for c in calls_of(forgectl)] == ["show"]
    assert audit(state_home) == []


def test_stale_base_refuses_before_rendering(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    doc = h.show_doc(PR)
    doc["base"] = {**doc["base"], "ref": "release"}
    forgectl.script(ok("show", doc))
    code, error, _ = cli(*args_for(draft))
    assert code == 20 and error["error"]["reason"] == "stale_base"
    assert [c.subcommand for c in calls_of(forgectl)] == ["show"]


def test_expect_plan_mismatch_refuses_before_the_audit_attempt(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry())
    code, doc, _ = cli(*args_for(draft, "--expect-plan", "0" * 64))
    assert code == 20 and doc["error"]["reason"] == "plan_changed"
    assert forgectl.writes() == []
    assert audit(state_home) == []


# --- the audit file ----------------------------------------------------------------------------


def test_an_unwritable_audit_file_ends_the_run_before_any_forgectl_call(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    directory = state_dir(state_home)
    directory.mkdir(parents=True, mode=0o700)
    for parent in (state_home, directory):
        parent.chmod(0o700)
    (directory / "tmp").mkdir(mode=0o700)
    target = directory / "audit.jsonl"
    target.write_text("")
    target.chmod(0o666)  # writable by others: refused
    code, doc, _ = cli(*args_for(draft))
    assert code == 12
    validate("pr-comment/cli/error.json", doc)
    error = doc["error"]
    assert error["code"] == "audit_unavailable"
    assert (error["landed"], error["retry"]) == (False, "same_command")
    assert forgectl.calls() == []


def test_an_unsafe_state_directory_ends_the_run_before_any_forgectl_call(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    directory = state_dir(state_home)
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o755)
    code, doc, _ = cli(*args_for(draft))
    assert code == 12 and doc["error"]["code"] == "state_unsafe"
    assert (doc["error"]["landed"], doc["error"]["retry"]) == (False, "same_command")
    assert forgectl.calls() == []


def test_usage_errors_come_before_the_state_check(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    directory = state_dir(state_home)
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o755)
    missing = str(tmp_path / "nope.json")
    code, doc, _ = cli(*args_for(missing))
    assert code == 2
    assert forgectl.calls() == []


class _FailAt:
    """``AppendFile.append`` that fails from its ``n``-th call (1-based): the audit file's first
    append is ``attempted``, the second the outcome; log lines go through the same class, so only
    the sync appends (the audit file's) are counted."""

    def __init__(self, n: int, *, written: bool = False) -> None:
        self.n, self.count, self.written = n, 0, written

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pr_comment import appendfile

        original = appendfile.AppendFile.append
        outer = self

        def append(self: Any, line: bytes, *, sync: bool = False) -> None:
            if sync:
                outer.count += 1
                if outer.count >= outer.n:
                    raise appendfile.AppendError(
                        "no space left on device", "audit.jsonl", OSError(errno.ENOSPC, "full")
                    )
            original(self, line, sync=sync)

        monkeypatch.setattr(appendfile.AppendFile, "append", append)


def test_a_failed_attempted_append_sends_nothing(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry())
    _FailAt(1).install(monkeypatch)
    code, doc, _ = cli(*args_for(draft))
    assert code == 12 and doc["error"]["code"] == "audit_unavailable"
    assert (doc["error"]["landed"], doc["error"]["retry"]) == (False, "same_command")
    assert forgectl.writes() == []  # the pre-check ran, the write never did


def test_a_failed_outcome_append_after_a_landed_write_is_audit_failed_and_not_a_retry(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry(), posted())
    _FailAt(2).install(monkeypatch)
    code, doc, _ = cli(*args_for(draft))
    assert code == 12
    validate("pr-comment/cli/error.json", doc)
    error = doc["error"]
    assert error["code"] == "audit_failed"
    assert (error["landed"], error["retry"]) == (True, "none")
    assert error["details"]["outcome"]["ids"] == ["gh:rc:RC_1", "gh:rc:RC_2"]
    assert len(forgectl.writes()) == 1  # never posted again
    failed = [
        e for e in log(state_home) if e["details"] and e["details"].get("code") == "audit_failed"
    ]
    assert [(e["event"], e["level"]) for e in failed] == [("warning", "warn")]


def test_an_audit_failure_after_a_refusal_logs_the_refusal_and_the_audit_failure(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(
        show(), dry(), refuse("review", "stale_head", details={"head_sha": B}, phase="write")
    )
    _FailAt(2).install(monkeypatch)
    code, doc, _ = cli(*args_for(draft))
    assert code == 12 and doc["error"]["code"] == "audit_failed"
    codes = [(e["event"], (e["details"] or {}).get("code")) for e in log(state_home)]
    assert ("refused", "refused") in codes and ("warning", "audit_failed") in codes


def test_a_failed_outcome_append_after_a_failed_write_keeps_the_writes_pair(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry(), fail("review", "outcome_unknown"))
    _FailAt(2).install(monkeypatch)
    code, doc, _ = cli(*args_for(draft))
    error = doc["error"]
    assert code == 12 and error["code"] == "audit_failed"
    assert (error["landed"], error["retry"]) == (None, "same_command")
    assert error["details"]["outcome"]["error"]["code"] == "outcome_unknown"


# --- the dialog ---------------------------------------------------------------------------------


@pytest.fixture
def interactive(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], list[int]]:
    """Make the run interactive and the dialog answer ``yes`` or ``no`` (the dialog itself is
    tested in test_confirm*.py)."""
    asked: list[int] = []

    def setup(answer: bool) -> list[int]:
        monkeypatch.setattr(cli_module, "stream_is_tty", lambda stream: True)

        def confirm(prepared: Any, **kw: Any) -> bool:
            asked.append(1)
            return answer

        monkeypatch.setattr(cli_module, "confirm", confirm)
        return asked

    return setup


def test_a_declined_dialog_posts_nothing_and_writes_no_audit_record(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    interactive: Callable[[bool], list[int]],
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry())
    asked = interactive(False)
    code, doc, _ = cli("review", PR, "--draft-file", draft, "--json", "--actor", ACTOR)
    assert code == 13 and doc["error"]["code"] == "confirmation_required"
    assert doc["error"]["message"] == "declined"
    assert asked == [1] and forgectl.writes() == []
    assert audit(state_home) == []
    assert [e["event"] for e in log(state_home) if e["event"] == "declined"] == ["declined"]


def test_an_accepted_dialog_posts_what_the_dialog_showed(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    interactive: Callable[[bool], list[int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry(), posted())
    interactive(True)
    shown: list[Any] = []

    def confirm(prepared: Any, **kw: Any) -> bool:
        shown.append(prepared.plan.input)
        return True

    monkeypatch.setattr(cli_module, "confirm", confirm)
    code, doc, _ = cli("review", PR, "--draft-file", draft, "--json", "--actor", ACTOR)
    assert code == 0 and len(shown) == 1
    assert reviews(forgectl)[1].stdin == shown[0]  # the bytes the human confirmed
    assert [r["result"] for r in audit(state_home)] == ["attempted", "done"]


def test_a_non_interactive_run_without_yes_posts_nothing_and_records_nothing(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry())
    code, doc, _ = cli("review", PR, "--draft-file", draft, "--json", "--actor", ACTOR)
    assert code == 13 and doc["error"]["code"] == "confirmation_required"
    assert forgectl.writes() == [] and audit(state_home) == []


# --- dedupe: a rerun reuses the key and never posts twice --------------------------------------


def test_a_rerun_after_outcome_unknown_reuses_the_key_and_a_reanchored_rerun_is_already_posted(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(
        show(),
        dry(),
        fail("review", "outcome_unknown"),
        show(),
        dry(already_posted=True),
        posted(already_posted=True),
    )
    code, first, _ = cli(*args_for(draft))
    assert code == 12 and first["error"]["code"] == "outcome_unknown"
    code, second, _ = cli(*args_for(draft))
    assert code == 0 and second["already_posted"] is True
    writes = forgectl.writes()
    assert len(writes) == 2
    assert flag(writes[0], "--dedupe-key") == flag(writes[1], "--dedupe-key")
    assert flag(writes[0], "--dedupe-key") == [second["dedupe_key"]]
    records = audit(state_home)
    assert [r["result"] for r in records] == [
        "attempted",
        "outcome_unknown",
        "attempted",
        "already_posted",
    ]
    assert records[0]["attempt_id"] != records[2]["attempt_id"]


def test_after_a_push_the_reanchored_rerun_has_the_same_key(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(
        show(),
        dry(),
        fail("review", "outcome_unknown"),
        show(head=B),
        ok("compare", h.compare_doc(PR, from_sha=A, to_sha=B, diff="")),
        dry(head_sha=B, already_posted=True),
        posted(head_sha=B, already_posted=True),
    )
    code, _, _ = cli(*args_for(draft))
    assert code == 12
    code, doc, _ = cli(*args_for(draft, "--reanchor-to", B))
    assert code == 0, doc
    assert doc["already_posted"] is True and doc["head_sha"] == B
    validate("pr-comment/cli/review.json", doc)
    assert doc["reanchored_draft"]["anchor"]["head_sha"] == B
    writes = forgectl.writes()
    assert flag(writes[0], "--dedupe-key") == flag(writes[1], "--dedupe-key")
    assert flag(writes[1], "--expected-head") == [B]


# --- signals and the deadline ------------------------------------------------------------------


def _start_cli(
    forgectl: FakeForgectl, tmp_path: Path, state_home: Path, *argv: str
) -> subprocess.Popen[bytes]:
    env = forgectl.environ({"XDG_STATE_HOME": str(state_home)})
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    proc = subprocess.Popen(
        [sys.executable, "-m", "pr_comment", *argv],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(tmp_path),
        start_new_session=True,
    )
    return proc


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_a_signal_during_the_write_is_outcome_unknown_and_audited(
    forgectl: FakeForgectl, tmp_path: Path, state_home: Path, signum: int
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(
        show(),
        dry(),
        ok(
            "review",
            h.post_doc("review", ids=["gh:rc:RC_1"]),
            hang={"phase": "after_write_attempt"},
        ),
    )
    forgectl.allow_unused_steps = False
    n = forgectl.next_call_number() + 3  # show, the pre-check, --version, the write
    proc = _start_cli(forgectl, tmp_path, state_home, *args_for(draft))
    forgectl.wait_ready(n)
    os.kill(proc.pid, signum)  # the exact pid this test started
    stdout, _ = proc.communicate(timeout=60)
    assert proc.returncode == 12
    doc = json.loads(stdout)
    validate("pr-comment/cli/error.json", doc)
    error = doc["error"]
    assert error["code"] == "outcome_unknown"
    assert (error["landed"], error["retry"]) == (None, "same_command")
    records = audit(state_home)
    validate_audit(records)
    assert [r["result"] for r in records] == ["attempted", "outcome_unknown"]
    assert records[1]["writes"][0]["lower"]["code"] == "outcome_unknown"
    assert len(forgectl.writes()) == 1


def test_a_signal_before_the_write_exits_128_plus_n_and_writes_no_record(
    forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(ok("show", h.show_doc(PR), hang={"phase": "before_write"}))
    n = forgectl.next_call_number()
    proc = _start_cli(forgectl, tmp_path, state_home, *args_for(draft))
    forgectl.wait_ready(n)
    os.kill(proc.pid, signal.SIGTERM)
    stdout, _ = proc.communicate(timeout=60)
    assert proc.returncode == 143 and stdout == b""
    assert audit(state_home) == []


def test_the_deadline_of_a_write_is_outcome_unknown_and_never_retried(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pr_comment import gateway

    monkeypatch.setattr(gateway, "DEADLINE_SLACK_SECONDS", 0.0)
    monkeypatch.setenv("PR_COMMENT_TIMEOUT", "1s")
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(
        show(),
        dry(),
        ok(
            "review",
            h.post_doc("review", ids=["gh:rc:RC_1"]),
            hang={"phase": "after_write_attempt"},
        ),
    )
    forgectl.allow_fake_errors = True  # a call that is killed has no end event
    code, doc, _ = cli(*args_for(draft))
    assert code == 12
    error = doc["error"]
    assert error["code"] == "outcome_unknown"
    assert (error["landed"], error["retry"]) == (None, "same_command")
    assert len(forgectl.writes()) == 1
    assert audit(state_home)[1]["result"] == "outcome_unknown"


def test_the_deadline_during_a_pre_write_hook_of_a_review_is_outcome_unknown(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the live deadline_kill scenario (#93): a slow forgectl hook, killed at the deadline before
    # forgectl's own write; from outside that is a write of unknown outcome, never `landed: false`
    from pr_comment import gateway

    monkeypatch.setattr(gateway, "DEADLINE_SLACK_SECONDS", 0.0)
    monkeypatch.setenv("PR_COMMENT_TIMEOUT", "4s")  # the reads before the write must finish in it
    # todo(#94): this deadline also bounds the show and dry-run calls; a load stall there reads
    # as network_error
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(
        show(),
        dry(),
        ok("review", h.post_doc("review", ids=["gh:rc:RC_1"]), hang={"phase": "before_write"}),
    )
    forgectl.allow_fake_errors = True  # a call that is killed has no end event
    code, doc, _ = cli(*args_for(draft))
    assert code == 12
    error = doc["error"]
    assert error["code"] == "outcome_unknown"
    assert (error["landed"], error["retry"]) == (None, "same_command")
    assert audit(state_home)[1]["result"] == "outcome_unknown"


def test_a_forgectl_network_error_without_a_kill_is_not_landed_and_a_plain_rerun(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    # forgectl's own budget ran out before its write (post order step 9, no `attempted` record):
    # forgectl's table says nothing landed, so the documented pair is false / same_command (#93)
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry(), fail("review", "network_error", phase="write"))
    code, doc, _ = cli(*args_for(draft))
    assert code == 12
    error = doc["error"]
    assert error["code"] == "network_error"
    assert (error["landed"], error["retry"]) == (False, "same_command")
    assert audit(state_home)[1]["result"] == "failed"


def test_attempted_is_on_disk_while_forgectl_runs(
    forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(
        show(),
        dry(),
        ok(
            "review",
            h.post_doc("review", ids=["gh:rc:RC_1"]),
            hang={"phase": "after_write_attempt"},
        ),
    )
    n = forgectl.next_call_number() + 3  # show, the pre-check, --version, the write
    proc = _start_cli(forgectl, tmp_path, state_home, *args_for(draft))
    forgectl.wait_ready(n)
    records = audit(state_home)  # read before the signal: the write is in flight
    os.kill(proc.pid, signal.SIGTERM)  # the exact pid this test started
    proc.communicate(timeout=60)
    assert [r["result"] for r in records] == ["attempted"]


# --- a signal between the forgectl answer and the end of the audit outcome -----------------------

_SLOW_OUTCOME = """\
import os
import sys
import time

from pr_comment import appendfile
from pr_comment.cli import main

original = appendfile.AppendFile.append


def slow(self, line, *, sync=False):
    if sync and b"attempted" not in line:  # the outcome record: slow and announced
        open(os.environ["PRC_TEST_READY"], "w").close()
        time.sleep(2)
    original(self, line, sync=sync)


appendfile.AppendFile.append = slow
sys.exit(main(sys.argv[1:]))
"""


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_a_signal_while_the_outcome_is_appended_waits_for_the_record(
    forgectl: FakeForgectl, tmp_path: Path, state_home: Path, signum: int
) -> None:
    import time

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    launcher = scripts / "slow_outcome.py"
    launcher.write_text(_SLOW_OUTCOME, encoding="utf-8")
    ready = tmp_path / "outcome-append-started"
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry(), posted())
    env = forgectl.environ({"XDG_STATE_HOME": str(state_home), "PRC_TEST_READY": str(ready)})
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    proc = subprocess.Popen(
        [sys.executable, str(launcher), *args_for(draft)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(tmp_path),
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 30
        while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "the outcome append never started"
        os.kill(proc.pid, signum)  # the exact pid this test started
        stdout, stderr = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    # the write landed and its answer was known: the record is complete and the run ends with it
    assert proc.returncode == 0, stderr
    doc = json.loads(stdout)
    validate("pr-comment/cli/review.json", doc)
    assert doc["ids"] == ["gh:rc:RC_1", "gh:rc:RC_2"]
    records = audit(state_home)
    validate_audit(records)
    assert [r["result"] for r in records] == ["attempted", "done"]
    assert len(forgectl.writes()) == 1


_SLOW_ATTEMPTED = _SLOW_OUTCOME.replace(
    'if sync and b"attempted" not in line:  # the outcome record: slow and announced',
    'if sync and b"attempted" in line:  # the attempted record: slow and announced',
)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_a_signal_while_attempted_is_appended_sends_no_write(
    forgectl: FakeForgectl, tmp_path: Path, state_home: Path, signum: int
) -> None:
    import time

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    launcher = scripts / "slow_attempted.py"
    launcher.write_text(_SLOW_ATTEMPTED, encoding="utf-8")
    ready = tmp_path / "attempted-append-started"
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry())
    env = forgectl.environ({"XDG_STATE_HOME": str(state_home), "PRC_TEST_READY": str(ready)})
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    proc = subprocess.Popen(
        [sys.executable, str(launcher), *args_for(draft)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(tmp_path),
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 30
        while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "the attempted append never started"
        os.kill(proc.pid, signum)  # the exact pid this test started
        stdout, stderr = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    # the signal came before the write was sent: no write, no envelope, a closed pair
    assert proc.returncode == 128 + signum, stderr
    assert stdout == b""
    assert forgectl.writes() == []
    records = audit(state_home)
    validate_audit(records)
    assert [r["result"] for r in records] == ["attempted", "failed"]
    assert records[1]["attempt_id"] == records[0]["attempt_id"]


# --- an unexpected error after ``attempted`` ----------------------------------------------------


def test_an_unexpected_error_of_the_write_call_is_outcome_unknown_with_a_closed_pair(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pr_comment import gateway as gateway_module

    original = gateway_module.ProcessGateway.call

    def call(self: Any, subcommand: str, args: Any = (), *, stdin: bytes | None = None) -> Any:
        if subcommand == "review" and "--yes" in args:
            raise RuntimeError("boom")
        return original(self, subcommand, args, stdin=stdin)

    monkeypatch.setattr(gateway_module.ProcessGateway, "call", call)
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry())
    code, doc, _ = cli(*args_for(draft))
    assert code == 12
    validate("pr-comment/cli/error.json", doc)
    error = doc["error"]
    assert error["code"] == "outcome_unknown"
    assert (error["landed"], error["retry"]) == (None, "same_command")
    records = audit(state_home)
    validate_audit(records)
    assert [r["result"] for r in records] == ["attempted", "outcome_unknown"]
    assert records[1]["attempt_id"] == records[0]["attempt_id"]


def test_an_unexpected_error_after_the_write_is_outcome_unknown_with_a_closed_pair(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pr_comment import post as post_module

    def broken(*args: Any, **kw: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(post_module, "result_document", broken)
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry(), posted())
    code, doc, _ = cli(*args_for(draft))
    assert code == 12
    validate("pr-comment/cli/error.json", doc)
    error = doc["error"]
    assert error["code"] == "outcome_unknown"
    assert (error["landed"], error["retry"]) == (None, "same_command")
    assert len(forgectl.writes()) == 1  # never posted again
    records = audit(state_home)
    validate_audit(records)
    assert [r["result"] for r in records] == ["attempted", "outcome_unknown"]


def test_a_bug_before_forgectl_ran_is_internal_and_logged_at_error(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pr_comment import gateway as gateway_module

    original = gateway_module.ProcessGateway.call

    def call(self: Any, subcommand: str, args: Any = (), *, stdin: bytes | None = None) -> Any:
        if subcommand == "review" and "--yes" in args:
            raise gateway_module.GatewayInternalError("bug")
        return original(self, subcommand, args, stdin=stdin)

    monkeypatch.setattr(gateway_module.ProcessGateway, "call", call)
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry())
    code, doc, _ = cli(*args_for(draft))
    assert code == 1 and doc["error"]["code"] == "internal"
    assert [r["result"] for r in audit(state_home)] == ["attempted", "failed"]
    internal = [e for e in log(state_home) if e["level"] == "error"]
    assert [(e["event"], (e["details"] or {}).get("code")) for e in internal] == [
        ("warning", "internal")
    ]


# --- what forgectl's schema allows --------------------------------------------------------------


def test_a_landed_answer_with_many_and_long_warnings_is_read_whole(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    warnings = [f"warn_{i}" for i in range(65)] + ["w" + "a" * 100]
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry(), posted(warnings=warnings))
    code, doc, _ = cli(*args_for(draft))
    assert code == 0, doc
    validate("pr-comment/cli/review.json", doc)
    assert doc["ids"] == ["gh:rc:RC_1", "gh:rc:RC_2"] and doc["review_id"] == "gh:r:R_1"
    assert set(warnings) <= set(doc["warnings"])
    assert audit(state_home)[1]["result"] == "done"


# --- goldens ------------------------------------------------------------------------------------

_VOLATILE = {
    "ts": "<ts>",
    "attempt_id": "<attempt>",
    "version": "<version>",
    "plan_sha256": "<plan>",
    "sha256": "<sha256>",
    "bytes": "<bytes>",
    "config_hash": "<config>",
    "config_path": None,
}


def _stable(value: Any) -> Any:
    """The document with what changes per run or per release (times, ids, the tool version in the
    attribution line and so in every hash) replaced."""
    if isinstance(value, dict):
        return {
            k: (_VOLATILE[k] if k in _VOLATILE and v is not None else _stable(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


def test_goldens_of_the_review_result_its_dry_run_and_the_audit_pair(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    golden_json: Callable[[str, Any], None],
) -> None:
    draft = draft_file(
        tmp_path,
        [
            finding("f1"),
            finding("f2", "b.py", None, body="file-level @amy"),
            finding("f3", "a.py", 5, suggestion={"lines": ["x = 1"]}),
        ],
    )
    forgectl.script(show(), dry(), show(), dry(), posted(ids=["gh:rc:RC_1", "gh:rc:RC_2"]))
    code, preview, _ = cli(*args_for(draft, "--dry-run"))
    assert code == 0
    validate("pr-comment/cli/review.json", preview)
    code, result, _ = cli(*args_for(draft))
    assert code == 0
    validate("pr-comment/cli/review.json", result)
    # the dry run is the plan the post carried
    assert preview["plan_sha256"] == result["plan_sha256"]
    for key in ("posted_inline", "moved", "file_level", "suggestions_degraded", "dedupe_key"):
        assert preview[key] == result[key]
    records = audit(state_home)
    validate_audit(records)
    golden_json("review/result.json", _stable(result))
    golden_json("review/dry-run.json", _stable({**preview, "plan": None}))
    golden_json("review/audit-pair.json", _stable(records))
