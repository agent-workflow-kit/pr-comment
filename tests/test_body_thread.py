# ruff: noqa: F811
# (the imported fixtures ``cli``, ``state_home``, ``hooks`` and ``pids`` are then used as test
# arguments)
"""The GitLab review body thread follow-up (issue #36), through the real CLI against the fake
forgectl process: ``comments`` finds the thread, ``resolve`` resolves it, as the review's second
write (audit ``writes`` index 1, its own hooks), and a failure of it never changes the review.

Every test runs with a private HOME and state directory; no real forgectl is ever called.
"""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path
from typing import Any

import pytest

from pr_comment import hooks as hooks_module
from pr_comment import keys
from tests import forgectl_harness as h
from tests.deadlines import ShortWriteDeadline
from tests.forgectl_harness import FakeForgectl, ProcessCall, fail, ok, refuse
from tests.posting import DRAFT_ID, A, finding
from tests.schema_harness import validate
from tests.test_hooks import alive, pids, signal_self_when_started  # noqa: F401  (pids: fixture)
from tests.test_post_hooks import Hooks, hooks  # noqa: F401  (hooks: a fixture)
from tests.test_review_post import (  # noqa: F401  (cli and state_home are fixtures)
    GL_PR,
    Run,
    _FailAt,
    _start_cli,
    args_for,
    audit,
    check_edge,
    cli,
    draft_file,
    dry,
    flag,
    log,
    show,
    state_home,
    validate_audit,
)

KEY = keys.review_key(DRAFT_ID)
MARK = f"<!-- forgectl:dedupe:{KEY} -->"
BODY_T = "gl:d:" + "1" * 40
INLINE_T = "gl:d:" + "2" * 40
OTHER_T = "gl:d:" + "3" * 40


# --- builders ---------------------------------------------------------------------------------


def gl_show() -> dict[str, Any]:
    return show(pr=GL_PR, backend="gitlab")


def gl_dry(**kw: Any) -> dict[str, Any]:
    return dry(GL_PR, **kw)


def gl_post(**kw: Any) -> dict[str, Any]:
    kw.setdefault("ids", ["gl:n:11", "gl:n:12"])
    kw.setdefault("thread_ids", [BODY_T, INLINE_T])
    return ok("review", h.post_doc("review", GL_PR, **kw))


def body_thread(**kw: Any) -> dict[str, Any]:
    return h.thread_doc(BODY_T, [h.comment_doc("gl:n:11", f"a review body\n{MARK}")], **kw)


def inline_thread(**kw: Any) -> dict[str, Any]:
    return h.thread_doc(INLINE_T, [h.comment_doc("gl:n:12", "an inline finding")], **kw)


def comments(*threads: dict[str, Any]) -> dict[str, Any]:
    return ok("comments", h.comments_doc(GL_PR, threads=threads))


def resolved(thread: str = BODY_T, **kw: Any) -> dict[str, Any]:
    return ok("resolve", h.resolve_doc("resolve", thread, GL_PR, **kw))


def run_review(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, steps: list[dict[str, Any]], *extra: str
) -> tuple[int, Any, str]:
    draft = draft_file(tmp_path, [finding("f1")], pr=GL_PR)
    forgectl.script(gl_show(), gl_dry(), *steps)
    return cli(*args_for(draft, *extra, pr=GL_PR))


def log_hook(hooks: Hooks, root: Path, name: str) -> str:
    """A hook that appends each payload it gets (and a newline) to ``<name>.jsonl`` and its name
    to ``order``."""
    return hooks.script(
        f"cat >> {root}/{name}.jsonl\necho >> {root}/{name}.jsonl\necho {name} >> {root}/order"
    )


def resolve_gate(hooks: Hooks, root: Path, code: int) -> str:
    """A ``pre-post`` hook that exits ``code`` for the resolve only (the review passes)."""
    pattern = '\'"action": *"resolve"\''
    return hooks.script(f"cat > {root}/seen\ngrep -q {pattern} {root}/seen && exit {code}\nexit 0")


def resolves(forgectl: FakeForgectl) -> list[ProcessCall]:
    return [c for c in forgectl.calls() if c.subcommand == "resolve"]


def subcommands(forgectl: FakeForgectl) -> list[str]:
    return [c.subcommand for c in forgectl.calls() if c.subcommand != "--version"]


# --- the resolve -------------------------------------------------------------------------------


def test_a_fresh_gitlab_review_resolves_its_body_thread_as_a_second_write(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    steps = [gl_post(), comments(body_thread(), inline_thread()), resolved()]
    code, doc, _ = run_review(cli, forgectl, tmp_path, steps, "--decision-id", "d1")
    assert code == 0, doc
    validate("pr-comment/cli/review.json", doc)
    assert doc["body_thread_resolved"] is True and doc["body_thread_id"] == BODY_T
    assert doc["warnings"] == [] and doc["already_posted"] is False
    assert subcommands(forgectl) == ["show", "review", "review", "comments", "resolve"]
    (call,) = resolves(forgectl)
    assert call.argv[2:4] == [GL_PR, f"--thread={BODY_T}"] or GL_PR in call.argv
    assert flag(call, "--thread") == [BODY_T] and "--yes" in call.argv
    assert flag(call, "--actor") == ["human:alice"] and flag(call, "--decision-id") == ["d1"]
    assert "--override-escalation" not in call.argv and "--dry-run" not in call.argv
    check_edge(forgectl, tmp_path, confirmed=True, head=A, base="main")

    records = audit(state_home)
    validate_audit(records)
    attempted, outcome = records
    assert (attempted["result"], outcome["result"]) == ("attempted", "done")
    first, second = outcome["writes"]
    assert (first["index"], first["action"], first["result"]) == (0, "review", "done")
    assert first["thread_id"] is None and first["dedupe_key"] == KEY
    assert (second["index"], second["action"], second["result"]) == (1, "resolve", "done")
    assert second["thread_id"] == BODY_T and second["dedupe_key"] is None
    assert second["lower"]["exit"] == 0 and second["lower"]["code"] is None


def test_the_plan_lists_the_follow_up_and_a_dry_run_does_not_run_it(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    code, doc, _ = run_review(cli, forgectl, tmp_path, [], "--dry-run")
    assert code == 0, doc
    validate("pr-comment/cli/review.json", doc)
    assert doc["plan"]["follow_ups"] == [{"action": "resolve", "thread_id": None}]
    assert doc["body_thread_resolved"] is None and doc["body_thread_id"] is None
    assert subcommands(forgectl) == ["show", "review"] and audit(state_home) == []


def test_an_already_posted_review_finds_the_thread_by_the_comment_in_ids(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    # a rerun: forgectl lists the comments it already holds, and no thread_ids
    steps = [
        gl_post(already_posted=True, ids=["gl:n:11"], thread_ids=[]),
        comments(inline_thread(), body_thread()),
        resolved(),
    ]
    forgectl.script()
    draft = draft_file(tmp_path, [finding("f1")], pr=GL_PR)
    forgectl.script(gl_show(), gl_dry(already_posted=True), *steps)
    code, doc, _ = cli(*args_for(draft, pr=GL_PR))
    assert code == 0, doc
    validate("pr-comment/cli/review.json", doc)
    assert doc["already_posted"] is True
    assert doc["body_thread_resolved"] is True and doc["body_thread_id"] == BODY_T
    outcome = audit(state_home)[1]
    assert [w["result"] for w in outcome["writes"]] == ["already_posted", "done"]
    assert outcome["result"] == "done"


def test_a_thread_that_is_already_resolved_is_reported_without_a_write_or_hooks(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, hooks: Hooks
) -> None:
    pre = hooks.script(f"cat >> {tmp_path}/pre.jsonl\necho >> {tmp_path}/pre.jsonl")
    steps = [gl_post(), comments(body_thread(resolved=True))]
    code, doc, _ = run_review(cli, forgectl, tmp_path, steps, "--hook", f"pre-post={pre}")
    assert code == 0, doc
    validate("pr-comment/cli/review.json", doc)
    assert doc["body_thread_resolved"] is True and doc["body_thread_id"] == BODY_T
    assert doc["warnings"] == [] and resolves(forgectl) == []
    outcome = audit(state_home)[1]
    assert outcome["result"] == "done" and len(outcome["writes"]) == 1
    seen = [x for x in (tmp_path / "pre.jsonl").read_text().splitlines() if x.strip()]
    assert [json.loads(x)["write_index"] for x in seen] == [0]  # no hook for a write not made


def test_a_review_approve_resolves_the_body_thread_too(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    key = keys.review_approve_key(DRAFT_ID, A)
    mark = f"<!-- forgectl:dedupe:{key} -->"
    thread = h.thread_doc(BODY_T, [h.comment_doc("gl:n:11", f"approving\n{mark}")])
    steps = [gl_post(head_verified=True), comments(thread), resolved()]
    code, doc, _ = run_review(cli, forgectl, tmp_path, steps, "--approve")
    assert code == 0, doc
    validate("pr-comment/cli/review.json", doc)
    assert doc["event"] == "approve" and doc["dedupe_key"] == key
    assert doc["body_thread_resolved"] is True
    outcome = audit(state_home)[1]
    assert [w["action"] for w in outcome["writes"]] == ["review", "resolve"]
    assert outcome["inputs"]["event"] == "approve"


def test_a_github_review_never_runs_the_follow_up(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(
        show(),
        dry(),
        ok("review", h.post_doc("review", ids=["gh:rc:RC_1"])),
    )
    code, doc, _ = cli(*args_for(draft))
    assert code == 0, doc
    assert doc["body_thread_resolved"] is None and doc["body_thread_id"] is None
    assert subcommands(forgectl) == ["show", "review", "review"]
    assert len(audit(state_home)[1]["writes"]) == 1


# --- the thread is not known: nothing is resolved ------------------------------------------------


def _other_key_thread() -> dict[str, Any]:
    other = f"<!-- forgectl:dedupe:{keys.review_key('0' * 26)} -->"
    return h.thread_doc(BODY_T, [h.comment_doc("gl:n:11", f"body\n{other}")])


@pytest.mark.parametrize(
    ("name", "threads"),
    [
        ("no_marker", [h.thread_doc(BODY_T, [h.comment_doc("gl:n:11", "no marker at all")])]),
        ("other_key", [_other_key_thread()]),
        (
            "marker_not_last",
            [h.thread_doc(BODY_T, [h.comment_doc("gl:n:11", f"{MARK}\ntrailing text")])],
        ),
        (
            "marker_in_a_later_comment",
            [
                h.thread_doc(
                    BODY_T,
                    [h.comment_doc("gl:n:11", "first"), h.comment_doc("gl:n:13", f"x\n{MARK}")],
                )
            ],
        ),
        ("only_an_inline_thread", [inline_thread()]),
        ("not_resolvable", [body_thread(resolvable=False)]),
        (
            "two_threads_with_the_marker",
            [
                body_thread(),
                h.thread_doc(INLINE_T, [h.comment_doc("gl:n:12", f"copy\n{MARK}")]),
            ],
        ),
        ("not_a_review_thread", [body_thread()]),
    ],
)
def test_a_thread_that_cannot_be_told_for_sure_is_never_resolved(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    name: str,
    threads: list[dict[str, Any]],
) -> None:
    post = gl_post(thread_ids=[OTHER_T]) if name == "not_a_review_thread" else gl_post()
    code, doc, _ = run_review(cli, forgectl, tmp_path, [post, comments(*threads)])
    assert code == 0, doc
    validate("pr-comment/cli/review.json", doc)
    assert doc["body_thread_resolved"] is False
    assert doc["warnings"] == ["body_thread_unresolved"]
    assert resolves(forgectl) == []
    records = audit(state_home)
    validate_audit(records)
    assert records[1]["result"] == "partial" and len(records[1]["writes"]) == 1
    assert records[1]["writes"][0]["result"] == "partial"  # landed, the follow-up not done
    assert records[1]["writes"][0]["lower"]["exit"] == 0


def test_a_review_with_no_thread_ids_reads_nothing(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    code, doc, _ = run_review(cli, forgectl, tmp_path, [gl_post(thread_ids=[])])
    assert code == 0, doc
    validate("pr-comment/cli/review.json", doc)
    assert doc["body_thread_resolved"] is False and doc["body_thread_id"] is None
    assert doc["warnings"] == ["body_thread_unresolved"]
    assert subcommands(forgectl) == ["show", "review", "review"]
    outcome = audit(state_home)[1]
    assert outcome["result"] == "partial" and len(outcome["writes"]) == 1
    assert outcome["writes"][0]["result"] == "partial"  # landed, its follow-up not tried
    assert outcome["writes"][0]["lower"]["exit"] == 0


def test_a_failed_comments_read_leaves_the_review_landed_and_unresolved(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    steps = [gl_post(), fail("comments", "network_error")]
    code, doc, _ = run_review(cli, forgectl, tmp_path, steps)
    assert code == 0, doc
    validate("pr-comment/cli/review.json", doc)
    assert doc["ids"] == ["gl:n:11", "gl:n:12"] and doc["body_thread_resolved"] is False
    assert doc["warnings"] == ["body_thread_unresolved"]
    assert len(forgectl.writes()) == 1 and resolves(forgectl) == []
    records = audit(state_home)
    validate_audit(records)
    assert records[1]["result"] == "partial"
    assert any(
        e["details"] and e["details"].get("reason") == "comments_read_failed"
        for e in log(state_home)
    )


# --- the resolve fails: the review stays landed, a rerun is safe ---------------------------


@pytest.mark.parametrize(
    ("step", "write_result", "code_name"),
    [
        (refuse("resolve", "not_resolvable", "gone"), "refused", "refused"),
        (fail("resolve", "network_error", phase="write"), "failed", "network_error"),
        (fail("resolve", "permission_denied", phase="write"), "failed", "permission_denied"),
        (fail("resolve", "outcome_unknown"), "outcome_unknown", "outcome_unknown"),
    ],
)
def test_a_failed_resolve_is_exit_0_with_a_partial_record_and_is_never_retried(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    step: dict[str, Any],
    write_result: str,
    code_name: str,
) -> None:
    steps = [gl_post(), comments(body_thread()), step]
    code, doc, _ = run_review(cli, forgectl, tmp_path, steps)
    assert code == 0, doc
    validate("pr-comment/cli/review.json", doc)
    assert doc["ids"] == ["gl:n:11", "gl:n:12"]  # the review's own answer is untouched
    assert doc["body_thread_resolved"] is False and doc["body_thread_id"] == BODY_T
    assert doc["warnings"] == ["body_thread_unresolved"]
    assert len(resolves(forgectl)) == 1 and len(forgectl.writes()) == 2  # never a second try
    records = audit(state_home)
    validate_audit(records)
    outcome = records[1]
    assert outcome["result"] == "partial"
    first, second = outcome["writes"]
    assert first["result"] == "done" and first["lower"]["exit"] == 0
    assert (second["index"], second["action"], second["result"]) == (1, "resolve", write_result)
    assert second["thread_id"] == BODY_T
    assert second["lower"]["code"] == code_name


def test_a_resolve_answer_that_is_not_the_asked_thread_is_not_a_resolve(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    # exit 0 with a document for another thread: forgectl said it succeeded (landed), but the
    # result cannot say the body thread is resolved
    wrong = ok("resolve", h.resolve_doc("resolve", OTHER_T, GL_PR))
    forgectl.allow_fake_errors = True  # the fake refuses to print a document for another thread
    steps = [gl_post(), comments(body_thread()), wrong]
    code, doc, _ = run_review(cli, forgectl, tmp_path, steps)
    assert code == 0, doc
    assert doc["body_thread_resolved"] is False and doc["warnings"] == ["body_thread_unresolved"]
    assert audit(state_home)[1]["result"] == "partial"


def test_a_rerun_after_a_failed_resolve_finds_the_review_posted_and_completes_it(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")], pr=GL_PR)
    forgectl.script(
        gl_show(),
        gl_dry(),
        gl_post(),
        comments(body_thread()),
        fail("resolve", "network_error", phase="write"),
        gl_show(),
        gl_dry(already_posted=True),
        gl_post(already_posted=True, ids=["gl:n:11"], thread_ids=[]),
        comments(body_thread()),
        resolved(),
    )
    first, doc1, _ = cli(*args_for(draft, pr=GL_PR))
    second, doc2, _ = cli(*args_for(draft, pr=GL_PR))
    assert (first, second) == (0, 0)
    assert doc1["body_thread_resolved"] is False and doc2["body_thread_resolved"] is True
    assert doc2["already_posted"] is True and doc2["warnings"] == []
    records = audit(state_home)
    assert [r["result"] for r in records] == ["attempted", "partial", "attempted", "done"]
    assert len([c for c in forgectl.writes() if c.subcommand == "review"]) == 2


# --- the audit pair of the second write ---------------------------------------------------------


def test_a_failed_outcome_append_after_a_landed_follow_up_is_audit_failed_not_a_retry(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps = [gl_post(), comments(body_thread()), resolved()]
    _FailAt(2).install(monkeypatch)
    code, doc, _ = run_review(cli, forgectl, tmp_path, steps)
    assert code == 12
    validate("pr-comment/cli/error.json", doc)
    error = doc["error"]
    assert error["code"] == "audit_failed"
    assert (error["landed"], error["retry"]) == (True, "none")
    assert error["details"]["outcome"]["body_thread_resolved"] is True
    assert len(forgectl.writes()) == 2  # the review and the resolve, each once


def test_an_audit_failure_after_a_failed_follow_up_still_says_landed(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps = [gl_post(), comments(body_thread()), fail("resolve", "network_error", phase="write")]
    _FailAt(2).install(monkeypatch)
    code, doc, _ = run_review(cli, forgectl, tmp_path, steps)
    assert code == 12
    error = doc["error"]
    assert error["code"] == "audit_failed"
    assert (error["landed"], error["retry"]) == (True, "none")  # the review landed
    assert error["details"]["outcome"]["warnings"] == ["body_thread_unresolved"]


def test_the_audit_record_holds_no_text_of_the_threads(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    steps = [gl_post(), comments(body_thread()), resolved()]
    run_review(cli, forgectl, tmp_path, steps)
    text = (state_home / "pr-comment" / "audit.jsonl").read_text()
    assert "a review body" not in text and "an inline finding" not in text


# --- hooks around the follow-up -----------------------------------------------------------------


def _payloads(path: Path) -> list[dict[str, Any]]:
    lines = [x for x in path.read_text().splitlines() if x.strip()]
    return [json.loads(x) for x in lines]


def _all_zero(counts: Any) -> bool:
    if isinstance(counts, dict):
        return all(_all_zero(v) for v in counts.values())  # pyright: ignore[reportUnknownVariableType]
    return counts == 0


def test_hooks_run_around_the_resolve_with_write_index_1_and_its_own_payload(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, hooks: Hooks
) -> None:
    pre = log_hook(hooks, tmp_path, "pre")
    post = log_hook(hooks, tmp_path, "post")
    steps = [gl_post(), comments(body_thread()), resolved()]
    code, doc, _ = run_review(
        cli, forgectl, tmp_path, steps, "--hook", f"pre-post={pre}", "--hook", f"post-post={post}"
    )
    assert code == 0, doc
    pres, posts = _payloads(tmp_path / "pre.jsonl"), _payloads(tmp_path / "post.jsonl")
    assert [p["write_index"] for p in pres] == [0, 1] and [p["write_index"] for p in posts] == [
        0,
        1,
    ]
    assert [p["action"] for p in pres] == ["review", "resolve"]
    for payload in (*pres, *posts):
        validate(f"pr-comment/hooks/{payload['hook']}.json", payload)
        assert payload["attempt_id"] == audit(state_home)[0]["attempt_id"]
        assert payload["plan_sha256"] == doc["plan_sha256"]
    second = pres[1]
    assert second["thread_id"] == BODY_T and second["event"] is None
    assert second["dedupe_key"] is None and second["bodies"] == [] and second["base_ref"] is None
    assert second["anchor_head"] is None and second["to"] is None
    assert second["thread_author_is_self"] is None and second["actor"] == "human:alice"
    assert _all_zero(second["counts"]) and not _all_zero(pres[0]["counts"])  # the review's differ
    assert second["object_id"] == GL_PR and second["head_sha"] == h.HEAD
    result = posts[1]["result"]
    assert result["exit"] == 0 and result["code"] is None and result["changed"] is True
    assert posts[0]["result"]["changed"] is None  # the review's own
    # review pre, review post, then the resolve pre and post, in that order
    assert (tmp_path / "order").read_text().split() == ["pre", "post", "pre", "post"]


def test_a_veto_of_the_follow_up_leaves_the_review_landed_and_is_recorded_as_vetoed(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, hooks: Hooks
) -> None:
    gate = resolve_gate(hooks, tmp_path, 1)
    steps = [gl_post(), comments(body_thread())]
    code, doc, _ = run_review(cli, forgectl, tmp_path, steps, "--hook", f"pre-post={gate}")
    assert code == 0, doc
    validate("pr-comment/cli/review.json", doc)
    assert doc["body_thread_resolved"] is False and doc["warnings"] == ["body_thread_unresolved"]
    assert doc["body_thread_id"] == BODY_T and resolves(forgectl) == []
    records = audit(state_home)
    validate_audit(records)
    assert records[1]["result"] == "partial"
    first, second = records[1]["writes"]
    assert first["result"] == "done"
    assert (second["index"], second["result"], second["lower"]) == (1, "vetoed", None)
    assert second["thread_id"] == BODY_T
    assert [e["event"] for e in log(state_home) if e["event"] == "vetoed"] == ["vetoed"]


def test_an_escalation_of_the_follow_up_is_recorded_and_the_override_allows_it(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, hooks: Hooks
) -> None:
    gate = resolve_gate(hooks, tmp_path, 13)
    steps = [gl_post(), comments(body_thread())]
    code, doc, _ = run_review(cli, forgectl, tmp_path, steps, "--hook", f"pre-post={gate}")
    assert code == 0, doc
    assert doc["body_thread_resolved"] is False
    second = audit(state_home)[1]["writes"][1]
    assert (second["result"], second["lower"]) == ("escalated", None)
    assert resolves(forgectl) == []


def test_an_override_of_the_pr_comment_scope_lets_the_follow_up_through(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, hooks: Hooks
) -> None:
    gate = resolve_gate(hooks, tmp_path, 13)
    steps = [gl_post(), comments(body_thread()), resolved()]
    code, doc, _ = run_review(
        cli,
        forgectl,
        tmp_path,
        steps,
        "--hook",
        f"pre-post={gate}",
        "--override-escalation=pr-comment",
    )
    assert code == 0, doc
    assert doc["body_thread_resolved"] is True
    (call,) = resolves(forgectl)
    assert "--override-escalation" not in call.argv  # that is the forgectl scope
    assert audit(state_home)[1]["result"] == "done"


def test_the_forgectl_scope_is_passed_to_the_resolve(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    steps = [gl_post(), comments(body_thread()), resolved()]
    code, doc, _ = run_review(cli, forgectl, tmp_path, steps, "--override-escalation=forgectl")
    assert code == 0, doc
    (call,) = resolves(forgectl)
    assert "--override-escalation" in call.argv


def test_an_id_a_hook_prints_reaches_the_resolve_and_the_audit_outcome(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, hooks: Hooks
) -> None:
    gate = hooks.script('cat > /dev/null\necho \'{"decision_id": "gate:1"}\'')
    steps = [gl_post(), comments(body_thread()), resolved()]
    code, doc, _ = run_review(
        cli, forgectl, tmp_path, steps, "--decision-id", "caller-1", "--hook", f"pre-post={gate}"
    )
    assert code == 0, doc
    (call,) = resolves(forgectl)
    assert flag(call, "--decision-id") == ["caller-1", "gate:1"]
    assert audit(state_home)[1]["hook_decision_id"] == ["gate:1"]


# --- a signal or the deadline during the follow-up ------------------------------------------------


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_a_signal_during_the_resolve_is_outcome_unknown_and_audited(
    forgectl: FakeForgectl, tmp_path: Path, state_home: Path, signum: int
) -> None:
    draft = draft_file(tmp_path, [finding("f1")], pr=GL_PR)
    forgectl.script(
        gl_show(),
        gl_dry(),
        gl_post(),
        comments(body_thread()),
        ok(
            "resolve",
            h.resolve_doc("resolve", BODY_T, GL_PR),
            hang={"phase": "after_write_attempt"},
        ),
    )
    n = (
        forgectl.next_call_number() + 5
    )  # show, the pre-check, --version, the review, comments, resolve
    proc = _start_cli(forgectl, tmp_path, state_home, *args_for(draft, pr=GL_PR))
    forgectl.wait_ready(n)
    os.kill(proc.pid, signum)  # the exact pid this test started
    stdout, _ = proc.communicate(timeout=60)
    assert proc.returncode == 12
    doc = json.loads(stdout)
    validate("pr-comment/cli/error.json", doc)
    assert doc["error"]["code"] == "outcome_unknown"
    records = audit(state_home)
    validate_audit(records)
    assert [r["result"] for r in records] == ["attempted", "partial"]
    first, second = records[1]["writes"]
    assert first["result"] == "done"
    assert (second["action"], second["result"]) == ("resolve", "outcome_unknown")
    assert len(forgectl.writes()) == 2  # nothing was tried again


def test_the_deadline_of_the_resolve_is_a_failed_follow_up_not_a_failed_review(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    short_write_deadline: ShortWriteDeadline,
) -> None:
    short_write_deadline()  # the resolve is the call that hangs; the earlier calls keep the default
    steps = [
        gl_post(),
        comments(body_thread()),
        ok(
            "resolve",
            h.resolve_doc("resolve", BODY_T, GL_PR),
            hang={"phase": "after_write_attempt"},
        ),
    ]
    forgectl.allow_fake_errors = True  # a call that is killed has no end event
    code, doc, _ = run_review(cli, forgectl, tmp_path, steps)
    assert code == 0, doc
    assert doc["body_thread_resolved"] is False and doc["ids"] == ["gl:n:11", "gl:n:12"]
    records = audit(state_home)
    assert records[1]["result"] == "partial"
    assert records[1]["writes"][1]["result"] == "outcome_unknown"
    assert len(resolves(forgectl)) == 1


# --- items of the first review: the thread, the answer, the signals ---------------------------


def _reasons(state_home: Path) -> list[str]:
    return [
        e["details"]["reason"]
        for e in log(state_home)
        if e["event"] == "warning" and e["details"] and e["details"].get("code")
    ]


def test_a_comments_read_that_is_not_complete_is_a_thread_not_found(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    incomplete = ok("comments", h.comments_doc(GL_PR, threads=[body_thread()], complete=False))
    code, doc, _ = run_review(cli, forgectl, tmp_path, [gl_post(), incomplete])
    assert code == 0, doc
    assert doc["body_thread_resolved"] is False and doc["body_thread_id"] is None
    assert doc["warnings"] == ["body_thread_unresolved"] and resolves(forgectl) == []
    assert "thread_not_found" in _reasons(state_home)
    assert audit(state_home)[1]["writes"][0]["result"] == "partial"


def test_an_already_posted_review_with_an_incomplete_read_resolves_nothing(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    incomplete = ok("comments", h.comments_doc(GL_PR, threads=[body_thread()], complete=False))
    draft = draft_file(tmp_path, [finding("f1")], pr=GL_PR)
    forgectl.script(
        gl_show(),
        gl_dry(already_posted=True),
        gl_post(already_posted=True, ids=["gl:n:11"], thread_ids=[]),
        incomplete,
    )
    code, doc, _ = cli(*args_for(draft, pr=GL_PR))
    assert code == 0, doc
    assert doc["body_thread_resolved"] is False and resolves(forgectl) == []


def test_an_inline_thread_with_the_marker_line_is_never_the_body_thread(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    placed = h.thread_doc(
        BODY_T, [h.comment_doc("gl:n:11", f"finding\n{MARK}")], path="src/a.py", line=3
    )
    code, doc, _ = run_review(cli, forgectl, tmp_path, [gl_post(), comments(placed)])
    assert code == 0, doc
    assert doc["body_thread_resolved"] is False and resolves(forgectl) == []
    assert "thread_not_found" in _reasons(state_home)


def test_an_answer_with_changed_false_is_not_a_write_and_runs_no_post_post_hook(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, hooks: Hooks
) -> None:
    # forgectl: changed false means the thread was already in the state when it re-read it, so
    # it wrote nothing, recorded nothing and ran no post-post hook
    pre = log_hook(hooks, tmp_path, "pre")
    post = log_hook(hooks, tmp_path, "post")
    steps = [gl_post(), comments(body_thread()), resolved(changed=False)]
    code, doc, _ = run_review(
        cli, forgectl, tmp_path, steps, "--hook", f"pre-post={pre}", "--hook", f"post-post={post}"
    )
    assert code == 0, doc
    validate("pr-comment/cli/review.json", doc)
    assert doc["body_thread_resolved"] is True and doc["warnings"] == []
    assert len(resolves(forgectl)) == 1
    assert [p["write_index"] for p in _payloads(tmp_path / "pre.jsonl")] == [0, 1]
    assert [p["write_index"] for p in _payloads(tmp_path / "post.jsonl")] == [0]
    records = audit(state_home)
    validate_audit(records)
    outcome = records[1]
    assert outcome["result"] == "done" and len(outcome["writes"]) == 1  # the review's only


def test_a_hook_id_equal_to_a_caller_id_is_not_passed_twice_to_the_resolve(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, hooks: Hooks
) -> None:
    gate = hooks.script('cat > /dev/null\necho \'{"decision_id": ["d1", "gate:1", "gate:1"]}\'')
    steps = [gl_post(), comments(body_thread()), resolved()]
    code, doc, _ = run_review(
        cli, forgectl, tmp_path, steps, "--decision-id", "d1", "--hook", f"pre-post={gate}"
    )
    assert code == 0, doc
    (call,) = resolves(forgectl)
    assert flag(call, "--decision-id") == ["d1", "gate:1"]
    assert audit(state_home)[1]["hook_decision_id"] == ["gate:1"]


def test_an_id_a_post_post_hook_prints_for_the_review_reaches_the_resolve_once(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, hooks: Hooks
) -> None:
    # too late for the review's own forgectl call, but the resolve is a later call
    late = hooks.script('cat > /dev/null\necho \'{"decision_id": "late:1"}\'')
    steps = [gl_post(), comments(body_thread()), resolved()]
    code, doc, _ = run_review(cli, forgectl, tmp_path, steps, "--hook", f"post-post={late}")
    assert code == 0, doc
    review = [c for c in forgectl.writes() if c.subcommand == "review"]
    assert all(flag(c, "--decision-id") == [] for c in review)
    (call,) = resolves(forgectl)
    assert flag(call, "--decision-id") == ["late:1"]
    assert audit(state_home)[1]["hook_decision_id"] == ["late:1"]


def _signal_after_hook(
    monkeypatch: pytest.MonkeyPatch, hook: str, action: str | None = None
) -> None:
    """Deliver SIGTERM to this process right after a hook of kind ``hook`` (for ``action``, when
    given) ended, so the signal lands outside any hook, while the signals are held."""
    original = hooks_module.HookRunner.run_sync

    def run_then_signal(self: Any, which: str, index: int, entry: Any, payload: Any) -> Any:
        result = original(self, which, index, entry, payload)
        if which == hook and (action is None or payload["action"] == action):
            os.kill(os.getpid(), signal.SIGTERM)
        return result

    monkeypatch.setattr(hooks_module.HookRunner, "run_sync", run_then_signal)


def _skipped_after_a_signal(
    code: int, doc: Any, forgectl: FakeForgectl, state_home: Path, *, calls: list[str]
) -> None:
    assert code == 0, doc
    validate("pr-comment/cli/review.json", doc)
    assert doc["ids"] == ["gl:n:11", "gl:n:12"]
    assert doc["body_thread_resolved"] is False and doc["warnings"] == ["body_thread_unresolved"]
    assert "skipped_after_signal" in _reasons(state_home)
    assert subcommands(forgectl) == calls
    records = audit(state_home)
    validate_audit(records)
    outcome = records[1]
    assert outcome["result"] == "partial" and len(outcome["writes"]) == 1
    assert outcome["writes"][0]["result"] == "partial"  # landed; the follow-up was not tried


@pytest.mark.usefixtures("default_signals")
def test_a_signal_held_after_the_review_skips_the_follow_up(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    hooks: Hooks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = hooks.recorder("post")
    _signal_after_hook(monkeypatch, "post-post")
    code, doc, _ = run_review(cli, forgectl, tmp_path, [gl_post()], "--hook", f"post-post={post}")
    _skipped_after_a_signal(code, doc, forgectl, state_home, calls=["show", "review", "review"])


@pytest.mark.usefixtures("default_signals")
def test_a_signal_during_the_review_post_post_hook_skips_the_follow_up(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, hooks: Hooks, pids: Path
) -> None:
    slow = hooks.script("sleep 60 & echo $! >> " + str(pids) + "; wait")
    with signal_self_when_started(pids, 2, signal.SIGTERM):
        code, doc, _ = run_review(
            cli, forgectl, tmp_path, [gl_post()], "--hook", f"post-post={slow}"
        )
    _skipped_after_a_signal(code, doc, forgectl, state_home, calls=["show", "review", "review"])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(alive(int(p)) for p in pids.read_text().split()):
        time.sleep(0.05)
    assert not any(alive(int(p)) for p in pids.read_text().split())


@pytest.mark.usefixtures("default_signals")
def test_a_signal_after_the_follow_up_pre_post_hook_makes_no_resolve_call(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    hooks: Hooks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pre = log_hook(hooks, tmp_path, "pre")
    _signal_after_hook(monkeypatch, "pre-post", "resolve")
    code, doc, _ = run_review(
        cli, forgectl, tmp_path, [gl_post(), comments(body_thread())], "--hook", f"pre-post={pre}"
    )
    _skipped_after_a_signal(
        code, doc, forgectl, state_home, calls=["show", "review", "review", "comments"]
    )
    assert resolves(forgectl) == []


def test_a_signal_during_the_follow_up_comments_read_is_outcome_unknown(
    forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")], pr=GL_PR)
    forgectl.script(
        gl_show(),
        gl_dry(),
        gl_post(),
        ok("comments", h.comments_doc(GL_PR), hang={"phase": "before_write"}),
    )
    n = forgectl.next_call_number() + 4  # show, the pre-check, --version, the review, comments
    proc = _start_cli(forgectl, tmp_path, state_home, *args_for(draft, pr=GL_PR))
    forgectl.wait_ready(n)
    os.kill(proc.pid, signal.SIGTERM)  # the exact pid this test started
    stdout, _ = proc.communicate(timeout=60)
    assert proc.returncode == 12
    doc = json.loads(stdout)
    validate("pr-comment/cli/error.json", doc)
    assert doc["error"]["code"] == "outcome_unknown"
    records = audit(state_home)
    validate_audit(records)
    assert [r["result"] for r in records] == ["attempted", "partial"]
    (only,) = records[1]["writes"]
    assert only["result"] == "partial" and only["lower"]["exit"] == 0  # landed, follow-up unknown
    assert resolves(forgectl) == [] and len(forgectl.writes()) == 1


@pytest.mark.usefixtures("default_signals")
def test_a_signal_during_the_follow_up_pre_post_hook_is_outcome_unknown(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, hooks: Hooks, pids: Path
) -> None:
    pattern = '\'"action": *"resolve"\''
    slow = hooks.script(
        f"cat > {tmp_path}/seen\n"
        f"if grep -q {pattern} {tmp_path}/seen; then sleep 60 & echo $! >> {pids}; wait; fi"
    )
    steps = [gl_post(), comments(body_thread())]
    # the review's hook: one word ($$); the resolve's: $$ and the sleep
    with signal_self_when_started(pids, 3, signal.SIGTERM):
        code, doc, _ = run_review(cli, forgectl, tmp_path, steps, "--hook", f"pre-post={slow}")
    assert code == 12, doc
    validate("pr-comment/cli/error.json", doc)
    assert doc["error"]["code"] == "outcome_unknown"
    assert resolves(forgectl) == [] and len(forgectl.writes()) == 1
    records = audit(state_home)
    validate_audit(records)
    assert [r["result"] for r in records] == ["attempted", "partial"]
    (only,) = records[1]["writes"]
    assert only["result"] == "partial" and only["lower"]["exit"] == 0
