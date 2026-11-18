"""The posting commands up to the plan, through the real CLI against the fake forgectl process
(``tests/fakebin/forgectl``): ``--dry-run``, ``--expect-plan``, the pre-check rounds and the exit
codes, with the edge rules of docs/interface.md checked over every logged call."""

from __future__ import annotations

import io
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pr_comment.cli import main
from tests import forgectl_harness as h
from tests.forgectl_harness import FakeForgectl, ProcessCall, assert_edge_rules, fail, ok, refuse
from tests.posting import MB, PR, A, B, draft_doc, finding
from tests.schema_harness import validate

Run = Callable[..., tuple[int, Any, str]]


@pytest.fixture
def cli(
    forgectl: FakeForgectl,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Run:
    """``cli(argv)`` runs ``main`` with the fake first on PATH: (exit, stdout JSON, stderr)."""
    fake = forgectl.environ()
    for key in [k for k in os.environ if k.startswith("XDG_") and k not in fake]:
        monkeypatch.delenv(key)  # pr-comment passes its environment on: keep the developer's out
    for key, value in fake.items():
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


def show(head: str = A, **kw: Any) -> dict[str, Any]:
    doc = h.show_doc(PR, **kw)
    doc["head"] = {**doc["head"], "sha": head}
    return ok("show", doc)


def draft_file(tmp_path: Path, findings: list[dict[str, Any]], **kw: Any) -> str:
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(draft_doc(findings, **kw)), encoding="utf-8")
    return str(path)


def reviews(calls: list[ProcessCall]) -> list[ProcessCall]:
    return [c for c in calls if c.subcommand == "review"]


def sent(call: ProcessCall) -> dict[str, Any]:
    assert call.stdin is not None
    document: dict[str, Any] = json.loads(call.stdin)
    return document


def edge(forgectl: FakeForgectl, tmp_path: Path, **kw: Any) -> None:
    assert_edge_rules(forgectl.calls(), root=tmp_path, **kw)


# --- --dry-run -------------------------------------------------------------------------------


def test_review_dry_run_prints_the_plan_and_posts_nothing(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1"), finding("f2", "b.py", None)])
    forgectl.script(show(), ok("review", h.post_doc("review", dry_run=True)))
    code, doc, _ = cli(
        "review", PR, "--draft-file", draft, "--dry-run", "--json", "--actor", "human:alice"
    )
    assert code == 0
    validate("pr-comment/cli/review.json", doc)
    assert doc["dry_run"] is True and doc["action"] == "review"
    assert doc["posted_inline"] == ["f1"] and doc["file_level"] == ["f2"]
    assert doc["ids"] == [] and doc["review_id"] is None and doc["already_posted"] is None
    plan = doc["plan"]
    assert plan["plan_sha256"] == doc["plan_sha256"]
    assert [b["role"] for b in plan["bodies"]] == ["body", "inline:f1"]
    calls = forgectl.calls()
    assert [c.subcommand for c in calls] == ["show", "review"]
    assert forgectl.writes() == []
    # the pre-check carried exactly the plan: the body and the inline comment, byte for byte
    review = sent(calls[1])
    assert review["body"] == plan["bodies"][0]["text"]
    assert [c["body"] for c in review["comments"]] == [plan["bodies"][1]["text"]]
    assert "--dry-run" in calls[1].argv and "--yes" not in calls[1].argv
    edge(
        forgectl,
        tmp_path,
        texts=[b["text"] for b in plan["bodies"]],
        confirmed=False,
        head=A,
        base="main",
    )


def test_the_fake_reports_no_neutralizing_for_prcomments_input(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    hostile = "ok\n/approve now\r\n /also\nforgectl:dedupe:x\n<img src=x> ![i](u) @amy"
    draft = draft_file(
        tmp_path, [finding("f1", body=hostile, suggestion={"lines": ["/x", "```"]})], body=hostile
    )
    forgectl.script(show(), ok("review", h.post_doc("review", dry_run=True)))
    code, doc, _ = cli(
        "review", PR, "--draft-file", draft, "--dry-run", "--json", "--actor", "human:alice"
    )
    assert code == 0
    assert doc["warnings"] == []  # forgectl's neutralized_lines and markers_removed were 0
    assert doc["neutralized_lines"] >= 4  # ours: the `/` lines spaced by pr-comment itself
    assert doc["markers_removed"] >= 1


def test_the_same_inputs_give_the_same_hash_across_runs(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1", body="hi @amy <b>")])
    forgectl.script(
        show(),
        ok("review", h.post_doc("review", dry_run=True)),
        show(),
        ok("review", h.post_doc("review", dry_run=True)),
    )
    args = ("review", PR, "--draft-file", draft, "--dry-run", "--json", "--actor", "human:alice")
    _, first, _ = cli(*args)
    _, second, _ = cli(*args)
    assert first["plan_sha256"] == second["plan_sha256"]
    assert first["plan"] == second["plan"]


def test_the_dry_run_hint_from_forgectl_is_reported_and_the_run_goes_on(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), ok("review", h.post_doc("review", dry_run=True, already_posted=True)))
    code, doc, _ = cli(
        "review", PR, "--draft-file", draft, "--dry-run", "--json", "--actor", "human:alice"
    )
    assert code == 0 and doc["already_posted"] is True


def test_comment_reply_and_approve_dry_runs(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    body = tmp_path / "body.txt"
    body.write_text("looks good @bob", encoding="utf-8")
    forgectl.script(
        show(),
        ok("comment", h.post_doc("comment", dry_run=True)),
        show(),
        ok("reply", h.post_doc("reply", dry_run=True, threaded=True)),
        show(),
        ok("approve", h.post_doc("approve", dry_run=True)),
    )
    common = ("--dry-run", "--json", "--actor", "human:alice")
    code, comment, _ = cli("comment", PR, "--body-file", str(body), *common)
    assert code == 0
    validate("pr-comment/cli/comment.json", comment)
    assert comment["mentions_neutralized"] == ["bob"]
    code, reply, _ = cli("reply", PR, "--to", "gh:rc:RC_1", "--body-file", str(body), *common)
    assert code == 0
    validate("pr-comment/cli/reply.json", reply)
    assert reply["to"] == "gh:rc:RC_1" and reply["threaded"] is True
    code, approve, _ = cli("approve", PR, "--expected-head", A, "--expected-base", "main", *common)
    assert code == 0
    validate("pr-comment/cli/approve.json", approve)
    assert approve["event"] == "approve"
    assert forgectl.writes() == []
    edge(forgectl, tmp_path, texts=["looks good"], confirmed=False)


# --- --expect-plan ---------------------------------------------------------------------------


def test_expect_plan_binds_a_preview_to_the_post(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    dry = ok("review", h.post_doc("review", dry_run=True))
    forgectl.script(show(), dry, show(), dry, show(), dry)
    base = ("review", PR, "--draft-file", draft, "--json", "--actor", "human:alice")
    _, preview, _ = cli(*base, "--dry-run")
    digest = preview["plan_sha256"]
    # the same flags with the hash: --dry-run passes
    code, again, _ = cli(*base, "--dry-run", "--expect-plan", digest)
    assert code == 0 and again["plan_sha256"] == digest
    # a changed decision id changes the plan: refused before any dialog or write, dry run too
    code, error, _ = cli(*base, "--dry-run", "--expect-plan", digest, "--decision-id", "d1")
    assert code == 20
    validate("pr-comment/cli/error.json", error)
    assert error["error"]["code"] == "refused" and error["error"]["reason"] == "plan_changed"
    assert error["error"]["details"]["plan_sha256"] != digest
    assert forgectl.writes() == []


def test_expect_plan_is_checked_after_the_precheck_and_before_any_dialog(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), ok("review", h.post_doc("review", dry_run=True)))
    code, error, _ = cli(
        "review", PR, "--draft-file", draft, "--json", "--actor", "human:alice", "--yes",
        "--expect-plan", "0" * 64,
    )  # fmt: skip
    assert code == 20 and error["error"]["reason"] == "plan_changed"
    assert [c.subcommand for c in forgectl.calls()] == ["show", "review"]
    assert all("--yes" not in c.argv for c in forgectl.calls())
    assert forgectl.writes() == []


def test_a_precheck_failure_wins_over_expect_plan(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), fail("review", "rate_limited", retry_after=4))
    code, error, _ = cli(
        "review", PR, "--draft-file", draft, "--json", "--actor", "human:alice", "--dry-run",
        "--expect-plan", "0" * 64,
    )  # fmt: skip
    assert code == 12 and error["error"]["code"] == "rate_limited"


def test_a_real_run_that_passes_the_plan_stops_before_any_write(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    # the dialog and the writes are later issues: nothing is posted and the run says so
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), ok("review", h.post_doc("review", dry_run=True)))
    code, error, _ = cli(
        "review", PR, "--draft-file", draft, "--json", "--actor", "human:alice", "--yes"
    )
    assert code == 1 and error["error"]["code"] == "internal"
    assert forgectl.writes() == []


# --- the pre-check rounds --------------------------------------------------------------------


def test_invalid_position_moves_the_finding_and_the_second_precheck_is_the_plan(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1"), finding("f2", "b.py", 4)])
    invalid = {
        "invalid": [{"index": 1, "path": "b.py", "line": 4, "side": "right", "start_line": None}]
    }
    forgectl.script(
        show(),
        fail("review", "invalid_position", details=invalid, phase="read"),
        ok("review", h.post_doc("review", dry_run=True)),
    )
    code, doc, _ = cli(
        "review", PR, "--draft-file", draft, "--dry-run", "--json", "--actor", "human:alice"
    )
    assert code == 0
    assert doc["moved"] == [{"id": "f2", "reason": "invalid_position"}]
    assert doc["posted_inline"] == ["f1"]
    first, second = reviews(forgectl.calls())
    assert len(sent(first)["comments"]) == 2 and len(sent(second)["comments"]) == 1
    assert sent(second)["body"] == doc["plan"]["bodies"][0]["text"]


def test_a_second_invalid_position_exits_12(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1"), finding("f2", "b.py", 4)])
    invalid = {
        "invalid": [{"index": 0, "path": "a.py", "line": 3, "side": "right", "start_line": None}]
    }
    forgectl.script(
        show(),
        fail("review", "invalid_position", details=invalid, phase="read"),
        fail("review", "invalid_position", details=invalid, phase="read"),
    )
    code, error, _ = cli(
        "review", PR, "--draft-file", draft, "--dry-run", "--json", "--actor", "human:alice"
    )
    assert code == 12 and error["error"]["code"] == "invalid_position"
    assert error["error"]["landed"] is False and error["error"]["retry"] == "same_command"


def test_too_large_reads_files_and_moves_the_unlisted_paths(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1", "a.py", 3), finding("f2", "gone.py", 4)])
    incomplete = h.files_doc()
    incomplete["files_complete"] = False
    incomplete["changed_files"] = 9
    forgectl.script(
        show(),
        refuse("review", "too_large", details={"limit": 100}),
        ok("files", incomplete),
        ok("review", h.post_doc("review", dry_run=True)),
    )
    code, doc, _ = cli(
        "review", PR, "--draft-file", draft, "--dry-run", "--json", "--actor", "human:alice"
    )
    assert code == 0
    assert doc["moved"] == [{"id": "f2", "reason": "files_incomplete"}]
    assert [c.subcommand for c in forgectl.calls()] == ["show", "review", "files", "review"]


def test_too_large_with_a_complete_list_is_exit_20(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1", "a.py", 3)])
    forgectl.script(
        show(),
        refuse("review", "too_large", details={"limit": 100}),
        ok("files", h.files_doc()),
    )
    code, error, _ = cli(
        "review", PR, "--draft-file", draft, "--dry-run", "--json", "--actor", "human:alice"
    )
    assert code == 20 and error["error"]["reason"] == "too_large"
    assert error["error"]["lower_error"]["would_refuse"]["reason"] == "too_large"


def test_a_refused_dry_run_prints_only_the_error_envelope(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), refuse("review", "stale_base", details={"base_ref": "dev"}))
    code, doc, _ = cli(
        "review", PR, "--draft-file", draft, "--dry-run", "--json", "--actor", "human:alice"
    )
    assert code == 20
    assert list(doc) == ["error"]
    assert doc["error"]["reason"] == "stale_base"


def test_an_unknown_reply_target_exits_10_at_the_precheck(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    body = tmp_path / "body.txt"
    body.write_text("x", encoding="utf-8")
    forgectl.script(show(), fail("reply", "not_found", phase="read"))
    code, error, _ = cli(
        "reply", PR, "--to", "gh:rc:RC_nope", "--body-file", str(body), "--json", "--yes",
        "--actor", "human:alice",
    )  # fmt: skip
    assert (
        code == 10
        and error["error"]["details"]
        == {
            "what": "to",
            "id": "gh:rc:RC_nope",
        }
        or error["error"]["details"]["what"] == "to"
    )


@pytest.mark.parametrize(
    ("code", "exit_code"),
    [("rate_limited", 12), ("network_error", 12), ("auth_required", 12), ("upstream_error", 12)],
)
def test_host_failures_during_the_precheck_end_the_run_with_nothing_posted(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, code: str, exit_code: int
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), fail("review", code, phase="read"))
    got, error, _ = cli(
        "review", PR, "--draft-file", draft, "--dry-run", "--json", "--actor", "human:alice"
    )
    assert got == exit_code and error["error"]["code"] == code
    assert forgectl.writes() == []


# --- reads and re-anchoring through the CLI --------------------------------------------------


def test_reanchor_to_prints_the_reanchored_draft_and_uses_b(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1", "a.py", 5)])
    shift = (
        "diff --git a/a.py b/a.py\nindex 1..2 100644\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,2 +1,4 @@\n x\n y\n+one\n+two\n"
    )
    forgectl.script(
        show(B),
        ok("compare", h.compare_doc(from_sha=A, to_sha=B, diff=shift)),
        ok("review", h.post_doc("review", dry_run=True, head_sha=B)),
    )
    code, doc, _ = cli(
        "review", PR, "--draft-file", draft, "--reanchor-to", B, "--dry-run", "--json",
        "--actor", "human:alice",
    )  # fmt: skip
    assert code == 0
    validate("pr-comment/cli/review.json", doc)
    assert doc["head_sha"] == B
    assert doc["reanchored_draft"]["findings"][0]["line"] == 7
    assert doc["plan"]["inline_comments"][0]["line"] == 7
    assert f"--expected-head={B}" in reviews(forgectl.calls())[0].argv
    edge(forgectl, tmp_path, confirmed=False, head=B, base="main")


def test_a_force_push_moves_every_finding_into_the_body(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1"), finding("f2", "b.py", 4)])
    forgectl.script(
        show(B),
        ok("compare", h.compare_doc(from_sha=A, to_sha=B, status="diverged", diff=None)),
        ok("review", h.post_doc("review", dry_run=True, head_sha=B)),
    )
    code, doc, _ = cli(
        "review", PR, "--draft-file", draft, "--reanchor-to", B, "--dry-run", "--json",
        "--actor", "human:alice",
    )  # fmt: skip
    assert code == 0
    assert [m["reason"] for m in doc["moved"]] == ["diff_unavailable"] * 2
    assert doc["posted_inline"] == []
    assert len(doc["reanchored_draft"]["unanchored"]) == 2


def test_a_moved_head_is_stale_head_and_nothing_else_is_called(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(B))
    code, error, _ = cli(
        "review", PR, "--draft-file", draft, "--dry-run", "--json", "--actor", "human:alice"
    )
    assert code == 20 and error["error"]["reason"] == "stale_head"
    assert error["error"]["details"] == {"head_sha": B}
    assert [c.subcommand for c in forgectl.calls()] == ["show"]


def test_the_left_side_check_reads_diff(cli: Run, forgectl: FakeForgectl, tmp_path: Path) -> None:
    draft = draft_file(tmp_path, [finding("f1", side="left")])
    moved_base = h.diff_doc(head_sha=A)
    moved_base["merge_base_sha"] = "2" * 40
    forgectl.script(
        show(), ok("diff", moved_base), ok("review", h.post_doc("review", dry_run=True))
    )
    code, doc, _ = cli(
        "review", PR, "--draft-file", draft, "--dry-run", "--json", "--actor", "human:alice"
    )
    assert code == 0 and doc["moved"] == [{"id": "f1", "reason": "base_changed"}]
    assert MB != "2" * 40


def test_a_draft_from_stdin(cli: Run, forgectl: FakeForgectl) -> None:
    raw = json.dumps(draft_doc([finding("f1")])).encode()
    forgectl.script(show(), ok("review", h.post_doc("review", dry_run=True)))
    code, doc, _ = cli(
        "review", PR, "--draft-file", "-", "--dry-run", "--json", "--actor", "human:alice",
        stdin=raw,
    )  # fmt: skip
    assert code == 0 and doc["posted_inline"] == ["f1"]


# --- usage and refusals before the plan ------------------------------------------------------


def test_a_bad_draft_exits_2_with_no_forgectl_call(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{", encoding="utf-8")
    code, error, _ = cli(
        "review", PR, "--draft-file", str(path), "--dry-run", "--json", "--actor", "human:alice"
    )
    assert code == 2 and error["error"]["code"] == "invalid_draft"
    assert forgectl.calls() == []


def test_config_errors_win_over_the_offline_draft_errors(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The config is loaded before step 1 of every order (docs/interface.md: Review order)."""
    path = tmp_path / "bad.json"
    path.write_text("{", encoding="utf-8")
    monkeypatch.setenv("PR_COMMENT_TIMEOUT", "not a duration")
    code, error, _ = cli(
        "review", PR, "--draft-file", str(path), "--dry-run", "--json", "--actor", "human:alice"
    )
    assert code == 2 and error["error"]["code"] == "config_invalid"
    assert forgectl.calls() == []


def test_a_missing_forgectl_is_tool_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    draft = draft_file(tmp_path, [finding("f1")])
    code = main(
        ["review", PR, "--draft-file", draft, "--dry-run", "--json", "--actor", "human:alice"]
    )
    error = json.loads(capsys.readouterr().out)
    assert code == 12 and error["error"]["code"] == "tool_missing"
    assert "install" in error["error"]["details"]


def test_the_human_run_prints_the_error_on_stderr(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(B))
    code, doc, err = cli("review", PR, "--draft-file", draft, "--dry-run", "--actor", "human:alice")
    assert code == 20 and doc is None
    assert "head moved" in err


def test_a_human_dry_run_shows_the_plan_hash_and_every_body(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1", body="tab\there \u202e")])
    forgectl.script(show(), ok("review", h.post_doc("review", dry_run=True)))
    code, doc, err = cli("review", PR, "--draft-file", draft, "--dry-run", "--actor", "human:alice")
    assert code == 0 and doc is None
    assert "dry run: nothing was sent (plan_sha256 " in err
    assert "--- body" in err and "--- inline:f1" in err and "<!-- forgectl:dedupe:prc:rv:" in err
    assert "\u202e" not in err and "<U+202E>" in err  # display-sanitized


def test_approve_outside_an_interactive_run_needs_both_pins_and_calls_nothing(
    cli: Run, forgectl: FakeForgectl
) -> None:
    code, error, _ = cli("approve", PR, "--dry-run", "--json", "--actor", "human:alice")
    assert code == 2 and error["error"]["code"] == "usage"
    assert forgectl.calls() == []


def test_a_comment_body_that_is_hostile_never_reaches_forgectl(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    body = tmp_path / "body.txt"
    body.write_bytes(b"fine\x00bad")
    code, error, _ = cli(
        "comment", PR, "--body-file", str(body), "--dry-run", "--json", "--actor", "human:alice"
    )
    assert code == 2 and error["error"]["code"] == "invalid_text"
    assert error["error"]["details"] == {"pointer": "/body", "kind": "control_character"}
    assert forgectl.calls() == []


def test_a_lone_surrogate_in_a_finding_path_is_refused_before_planning(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    # json.dumps escapes the lone surrogate as \ud800, which the loader decodes back to it.
    draft = draft_file(tmp_path, [finding("f1", "a\ud800.py", 3)])
    assert b"\\ud800" in Path(draft).read_bytes()
    code, error, _ = cli(
        "review", PR, "--draft-file", draft, "--dry-run", "--json", "--actor", "human:alice"
    )
    assert code == 2 and error["error"]["code"] == "invalid_text"
    assert error["error"]["details"] == {"pointer": "/findings/0/path", "kind": "control_character"}
    assert forgectl.calls() == []


def test_a_lone_surrogate_in_an_unanchored_path_is_refused_before_planning(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    entry = {"finding": finding("u1", "a\udc00.py", 4), "head_sha": "d" * 40, "reason": "changed"}
    draft = draft_file(tmp_path, [finding("f1", "a.py", 3)], unanchored=[entry])
    code, error, _ = cli(
        "review", PR, "--draft-file", draft, "--dry-run", "--json", "--actor", "human:alice"
    )
    assert code == 2 and error["error"]["code"] == "invalid_text"
    assert error["error"]["details"] == {
        "pointer": "/unanchored/0/finding/path",
        "kind": "control_character",
    }
    assert forgectl.calls() == []
