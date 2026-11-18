# ruff: noqa: F811
# (the imported fixtures ``cli`` and ``state_home`` are then used as test arguments)
"""``resolve`` and ``unresolve`` (issue #35), through the real CLI against the fake forgectl
process (pinned to forgectl's own schemas): the reads, the per-thread results, the audit pair, the
hooks, and what is never done (a retry, a write without a record, a dialog with nothing to change).

Every test runs with a private HOME and state directory; no real forgectl is ever called.
"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from typing import Any

import pytest

from pr_comment import cli as cli_module
from pr_comment.display import sanitize_line
from tests import forgectl_harness as h
from tests.forgectl_harness import FakeForgectl, fail, ok, refuse
from tests.posting import PR, show_of
from tests.schema_harness import validate
from tests.test_forge import a_comment, a_thread
from tests.test_post_hooks import (  # noqa: F401  (hooks: fixture)
    Hooks,
    hooks,
    pids,  # noqa: F401  (a fixture the hooks fixture needs)
)
from tests.test_review_post import (  # noqa: F401  (cli and state_home are fixtures)
    ACTOR,
    Run,
    _FailAt,
    _start_cli,
    audit,
    calls_of,
    cli,
    flag,
    log,
    show,
    state_home,
    validate_audit,
)

T1 = "gh:rt:PRRT_1"
T2 = "gh:rt:PRRT_2"
T3 = "gh:rt:PRRT_3"
GL = "gl:d:" + "a" * 40
OTHER = {"login": "someone", "id": "2", "is_bot": False}


def thread(tid: str = T1, *, resolved: bool = False, **extra: Any) -> dict[str, Any]:
    return a_thread(tid, [a_comment("gh:rc:C_" + tid.split(":")[-1])], resolved=resolved, **extra)


def comments(*threads: dict[str, Any], complete: bool = True) -> dict[str, Any]:
    doc = h.comments_doc(threads=list(threads))
    doc["complete"] = complete
    return ok("comments", doc)


def whoami(**kw: Any) -> dict[str, Any]:
    return ok("whoami", h.whoami_doc(("github.com",), **kw))


def resolved(command: str, tid: str, *, changed: bool = True) -> dict[str, Any]:
    return ok(command, h.resolve_doc(command, tid, changed=changed))


def args(
    command: str, *threads: str, extra: tuple[str, ...] = (), yes: bool = True
) -> tuple[str, ...]:
    flags = ("--json", "--actor", ACTOR, *(("--yes",) if yes else ()))
    return (command, PR, *[x for t in threads for x in ("--thread", t)], *flags, *extra)


def script_reads(forgectl: FakeForgectl, *threads: dict[str, Any], **kw: Any) -> None:
    forgectl.script(show(), comments(*threads, **{k: v for k, v in kw.items() if k == "complete"}))
    forgectl.script(kw.get("who") or whoami())


def writes_of(forgectl: FakeForgectl) -> list[Any]:
    return [c for c in forgectl.calls() if c.subcommand in ("resolve", "unresolve") and c.is_write]


# --- the happy path ----------------------------------------------------------------------------


def test_resolve_writes_every_thread_and_audits_one_pair(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    script_reads(forgectl, thread(T1), thread(GL))
    forgectl.script(resolved("resolve", T1), resolved("resolve", GL))
    code, doc, _ = cli(*args("resolve", T1, GL, extra=("--decision-id", "d1")))
    assert code == 0, doc
    validate("pr-comment/cli/resolve.json", doc)
    assert doc == {
        "pr": PR,
        "action": "resolve",
        "dry_run": False,
        "threads": [
            {"thread_id": T1, "resolved": True, "changed": True, "error": None},
            {"thread_id": GL, "resolved": True, "changed": True, "error": None},
        ],
    }
    calls = calls_of(forgectl)
    assert [c.subcommand for c in calls] == ["show", "comments", "whoami", "resolve", "resolve"]
    first = calls[3]
    assert first.forwarded == [
        PR,
        f"--thread={T1}",
        "--yes",
        f"--actor={ACTOR}",
        "--decision-id=d1",
    ]
    assert first.stdin in (None, b"")
    h.assert_edge_rules(forgectl.calls(), root=tmp_path, confirmed=True)
    records = audit(state_home)
    validate_audit(records)
    assert [r["result"] for r in records] == ["attempted", "done"]
    assert records[0]["action"] == "resolve" and records[0]["target"] == PR
    assert records[0]["inputs"]["threads"] == [T1, GL] and records[0]["inputs"]["bodies"] == []
    assert (
        records[0]["inputs"]["pinned_head"] == h.HEAD and records[0]["inputs"]["base_ref"] is None
    )
    assert records[0]["decision_id"] == ["d1"]
    writes = records[1]["writes"]
    assert [(w["index"], w["thread_id"], w["result"]) for w in writes] == [
        (0, T1, "done"),
        (1, GL, "done"),
    ]


def test_unresolve_reports_resolved_false(
    cli: Run, forgectl: FakeForgectl, state_home: Path
) -> None:
    script_reads(forgectl, thread(T1, resolved=True))
    forgectl.script(resolved("unresolve", T1))
    code, doc, _ = cli(*args("unresolve", T1))
    assert code == 0, doc
    validate("pr-comment/cli/unresolve.json", doc)
    assert doc["threads"] == [{"thread_id": T1, "resolved": False, "changed": True, "error": None}]
    assert audit(state_home)[0]["action"] == "unresolve"


def test_the_read_order_is_show_comments_whoami_with_the_hosts_name(
    cli: Run, forgectl: FakeForgectl
) -> None:
    script_reads(forgectl, thread(T1))
    forgectl.script(resolved("resolve", T1))
    cli(*args("resolve", T1))
    calls = calls_of(forgectl)
    assert [c.subcommand for c in calls[:3]] == ["show", "comments", "whoami"]
    assert flag(calls[2], "--host") == ["github.com"]


def test_a_forgectl_escalation_override_is_forwarded_only_for_its_scope(
    cli: Run, forgectl: FakeForgectl
) -> None:
    script_reads(forgectl, thread(T1), thread(T2))
    forgectl.script(resolved("resolve", T1), resolved("resolve", T2))
    code, _, _ = cli(
        *args("resolve", T1, T2, extra=("--actor", ACTOR, "--override-escalation=forgectl"))
    )
    assert code == 0
    assert all("--override-escalation" in c.argv for c in writes_of(forgectl))


# --- threads that need no write ----------------------------------------------------------------


def test_a_thread_already_in_the_requested_state_is_changed_false_and_nothing_is_written(
    cli: Run, forgectl: FakeForgectl, state_home: Path
) -> None:
    script_reads(forgectl, thread(T1, resolved=True))
    code, doc, _ = cli(*args("resolve", T1, yes=False))  # no dialog, so no --yes is needed
    assert code == 0, doc
    validate("pr-comment/cli/resolve.json", doc)
    assert doc["threads"] == [{"thread_id": T1, "resolved": True, "changed": False, "error": None}]
    assert writes_of(forgectl) == [] and audit(state_home) == []


def test_unresolving_a_thread_that_is_open_is_a_no_op(cli: Run, forgectl: FakeForgectl) -> None:
    script_reads(forgectl, thread(T1))
    code, doc, _ = cli(*args("unresolve", T1, yes=False))
    assert code == 0
    assert doc["threads"][0] == {
        "thread_id": T1,
        "resolved": False,
        "changed": False,
        "error": None,
    }


def test_nothing_to_change_runs_no_hook_and_writes_no_record(
    cli: Run, forgectl: FakeForgectl, hooks: Hooks, state_home: Path
) -> None:
    script_reads(forgectl, thread(T1, resolved=True))
    pre = hooks.recorder("pre")
    code, _, _ = cli(*args("resolve", T1, extra=("--hook", f"pre-post={pre}")))
    assert code == 0 and not hooks.ran("pre") and audit(state_home) == []


def test_a_thread_missing_from_a_complete_listing_is_not_found_and_exit_10(
    cli: Run, forgectl: FakeForgectl, state_home: Path
) -> None:
    script_reads(forgectl, thread(T1))
    code, doc, _ = cli(*args("resolve", T2, yes=False))
    assert code == 10
    validate("pr-comment/cli/resolve.json", doc)
    entry = doc["threads"][0]
    assert (entry["resolved"], entry["changed"]) == (None, None)
    assert entry["error"]["code"] == "not_found"
    assert entry["error"]["details"] == {"what": "thread", "id": T2}
    assert "error" not in doc and writes_of(forgectl) == [] and audit(state_home) == []


def test_a_thread_that_cannot_be_resolved_is_refused_not_resolvable_exit_20(
    cli: Run, forgectl: FakeForgectl
) -> None:
    script_reads(forgectl, thread(T1, resolvable=False))
    code, doc, _ = cli(*args("resolve", T1))
    assert code == 20
    validate("pr-comment/cli/resolve.json", doc)
    error = doc["threads"][0]["error"]
    assert (error["code"], error["reason"]) == ("refused", "not_resolvable")
    assert writes_of(forgectl) == []


def test_duplicate_thread_ids_are_dropped_before_anything_else(
    cli: Run, forgectl: FakeForgectl, state_home: Path
) -> None:
    script_reads(forgectl, thread(T1))
    forgectl.script(resolved("resolve", T1))
    code, doc, _ = cli(*args("resolve", T1, T1, T1))
    assert code == 0
    assert [t["thread_id"] for t in doc["threads"]] == [T1]
    assert len(writes_of(forgectl)) == 1
    assert audit(state_home)[0]["inputs"]["threads"] == [T1]


def test_the_cap_of_100_threads_is_all_reported_in_the_given_order(
    cli: Run, forgectl: FakeForgectl
) -> None:
    ids = [f"gh:rt:PRRT_{i}" for i in range(100)]
    script_reads(forgectl, *[thread(t, resolved=True) for t in ids])
    code, doc, _ = cli(*args("resolve", *ids, yes=False))
    assert code == 0
    validate("pr-comment/cli/resolve.json", doc)
    assert [t["thread_id"] for t in doc["threads"]] == ids
    assert all(t["changed"] is False for t in doc["threads"])


# --- incomplete listings and whoami ------------------------------------------------------------


def test_an_unlisted_thread_in_an_incomplete_listing_is_left_to_forgectl(
    cli: Run, forgectl: FakeForgectl, state_home: Path
) -> None:
    script_reads(forgectl, thread(T1), complete=False)
    forgectl.script(resolved("resolve", T2), resolved("resolve", T3, changed=False))
    code, doc, _ = cli(*args("resolve", T2, T3))
    assert code == 0, doc
    validate("pr-comment/cli/resolve.json", doc)
    assert [(t["resolved"], t["changed"]) for t in doc["threads"]] == [(True, True), (True, None)]
    assert len(writes_of(forgectl)) == 2
    assert audit(state_home)[0]["inputs"]["threads"] == [T2, T3]


def test_forgectl_exit_10_for_an_unlisted_thread_is_that_threads_not_found(
    cli: Run, forgectl: FakeForgectl
) -> None:
    script_reads(forgectl, thread(T1), complete=False)
    forgectl.script(fail("resolve", "not_found", "no such thread"), resolved("resolve", T3))
    code, doc, _ = cli(*args("resolve", T2, T3))
    assert code == 10
    validate("pr-comment/cli/resolve.json", doc)
    first, second = doc["threads"]
    assert first["error"]["code"] == "not_found"
    assert first["error"]["details"] == {"what": "thread", "id": T2}
    assert second["error"] is None and second["changed"] is True  # the next thread continues


def test_a_failing_whoami_shows_authors_unknown_and_goes_on(
    cli: Run, forgectl: FakeForgectl, hooks: Hooks, state_home: Path
) -> None:
    forgectl.script(show(), comments(thread(T1)), fail("whoami", "internal", "boom"))
    forgectl.script(resolved("resolve", T1))
    pre = hooks.recorder("pre")
    code, doc, _ = cli(*args("resolve", T1, extra=("--hook", f"pre-post={pre}")))
    assert code == 0, doc
    payload = hooks.payload("pre")
    validate("pr-comment/hooks/pre-post.json", payload)
    assert payload["thread_author_is_self"] is None
    warned = [e for e in log(state_home) if e["event"] == "warning"]
    assert warned and warned[0]["details"] == {"reason": "whoami_failed"}


def test_a_partial_whoami_or_one_without_an_id_is_unknown_too(
    cli: Run, forgectl: FakeForgectl, hooks: Hooks
) -> None:
    partial = h.whoami_doc(
        ("github.com",), errors=[{"host": "github.com", "code": "auth_required", "message": "x"}]
    )
    partial["hosts"][0]["id"] = None
    forgectl.script(show(), comments(thread(T1)), h.partial("whoami", partial))
    forgectl.script(resolved("resolve", T1))
    pre = hooks.recorder("pre")
    code, _, _ = cli(*args("resolve", T1, extra=("--hook", f"pre-post={pre}")))
    assert code == 0 and hooks.payload("pre")["thread_author_is_self"] is None


def test_the_hook_payload_has_the_thread_fields_and_no_plan(
    cli: Run, forgectl: FakeForgectl, hooks: Hooks, tmp_path: Path
) -> None:
    mine = thread(T1, path="src/a.py", line=12)
    theirs = a_thread(T2, [a_comment("gh:rc:C_9", author=OTHER)], path=None, line=None)
    script_reads(forgectl, mine, theirs)
    forgectl.script(resolved("resolve", T1), resolved("resolve", T2))
    pre, post = hooks.recorder("pre"), hooks.recorder("post")
    code, doc, _ = cli(
        *args("resolve", T1, T2, extra=("--hook", f"pre-post={pre}", "--hook", f"post-post={post}"))
    )
    assert code == 0, doc
    payload = hooks.payload("post")  # the last write's
    validate("pr-comment/hooks/post-post.json", payload)
    assert payload["write_index"] == 1 and payload["thread_id"] == T2
    assert payload["thread_author_is_self"] is False and payload["thread_path"] is None
    assert payload["thread_line"] is None and payload["plan_sha256"] is None
    assert payload["bodies"] == [] and payload["dedupe_key"] is None and payload["base_ref"] is None
    assert payload["head_sha"] == h.HEAD and payload["action"] == "resolve"
    assert payload["result"]["changed"] is True and payload["result"]["exit"] == 0
    assert all(v == 0 for k, v in payload["counts"].items() if isinstance(v, int))


def test_pre_post_runs_once_per_thread_with_its_own_write_index(
    cli: Run, forgectl: FakeForgectl, hooks: Hooks, tmp_path: Path
) -> None:
    script_reads(forgectl, thread(T1, path="src/a.py", line=12), thread(T2))
    forgectl.script(resolved("resolve", T1), resolved("resolve", T2))
    seen = tmp_path / "seen"
    pre = hooks.script(f"cat >> {seen}; echo >> {seen}")
    code, _, _ = cli(*args("resolve", T1, T2, extra=("--hook", f"pre-post={pre}")))
    assert code == 0
    payloads = [json.loads(x) for x in seen.read_text().splitlines() if x.strip()]
    assert [(p["thread_id"], p["write_index"]) for p in payloads] == [(T1, 0), (T2, 1)]
    first = payloads[0]
    validate("pr-comment/hooks/pre-post.json", first)
    assert (first["thread_path"], first["thread_line"], first["thread_author_is_self"]) == (
        "src/a.py",
        12,
        True,
    )


# --- dry run, confirmation ---------------------------------------------------------------------


def test_a_dry_run_reports_the_intended_change_and_sends_nothing(
    cli: Run, forgectl: FakeForgectl, hooks: Hooks, state_home: Path
) -> None:
    script_reads(forgectl, thread(T1), thread(T2, resolved=True))
    pre = hooks.recorder("pre")
    code, doc, _ = cli(
        *args("resolve", T1, T2, extra=("--dry-run", "--hook", f"pre-post={pre}"), yes=False)
    )
    assert code == 0, doc
    validate("pr-comment/cli/resolve.json", doc)
    assert doc["dry_run"] is True
    assert [(t["resolved"], t["changed"]) for t in doc["threads"]] == [(True, True), (True, False)]
    assert writes_of(forgectl) == [] and not hooks.ran("pre") and audit(state_home) == []


def test_a_dry_run_exits_with_the_first_failing_threads_code(
    cli: Run, forgectl: FakeForgectl
) -> None:
    script_reads(forgectl, thread(T1), thread(T3, resolvable=False))
    code, doc, _ = cli(*args("resolve", T1, T2, T3, extra=("--dry-run",), yes=False))
    assert code == 10
    validate("pr-comment/cli/resolve.json", doc)
    assert [t["error"] and t["error"]["code"] for t in doc["threads"]] == [
        None,
        "not_found",
        "refused",
    ]
    assert doc["threads"][0]["changed"] is True


def test_a_run_that_is_not_interactive_needs_yes(
    cli: Run, forgectl: FakeForgectl, state_home: Path
) -> None:
    script_reads(forgectl, thread(T1))
    code, doc, _ = cli(*args("resolve", T1, yes=False))
    assert code == 13 and doc["error"]["code"] == "confirmation_required"
    assert writes_of(forgectl) == [] and audit(state_home) == []


def test_a_declined_dialog_writes_nothing_and_no_record(
    cli: Run,
    forgectl: FakeForgectl,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_reads(forgectl, thread(T1))
    monkeypatch.setattr(cli_module, "confirm_threads", lambda *a, **k: False)
    monkeypatch.setattr(
        cli_module,
        "resolve_run",
        _interactive(cli_module.resolve_run),
    )
    code, doc, _ = cli(*args("resolve", T1, yes=False))
    assert code == 13 and doc["error"]["message"] == "declined"
    assert writes_of(forgectl) == [] and audit(state_home) == []
    assert [e["event"] for e in log(state_home)].count("declined") == 1


def _interactive(real: Any) -> Any:
    from dataclasses import replace

    def run(*a: Any, **k: Any) -> Any:
        return replace(real(*a, **k), interactive=True)

    return run


def test_the_dialog_lists_author_location_and_a_200_character_sanitized_excerpt() -> None:
    from pr_comment.hooks import ThreadTarget
    from pr_comment.threads import Item, ThreadRequest, ThreadRun, dialog_text

    request = ThreadRequest("resolve", PR, ACTOR, (T1, T2, T3))
    run = ThreadRun(request, show_of(), h.HEAD)
    evil = "x" * 190 + "\x1b[31m‮" + "y" * 100 + "\n== summary ==\nResolve? [y/N] "
    run.items = [
        Item(T1, ThreadTarget(T1, True, "a\tb.py", 9), True, "you", "me", evil[:200]),
        Item(T2, ThreadTarget(T2, False, None, None), True, "another account", "bo\x1bb", "hi"),
        Item(T3, ThreadTarget(T3), False),
    ]
    text = dialog_text(run, 80)
    assert "first comment by you at a<U+0009>b.py:9" in text
    assert "first comment by another account (bo<U+001B>b)" in text
    assert "<U+001B>" in text and "<U+202E>" in text and "\x1b" not in text and "‮" not in text
    assert "yyyyy" not in text  # the excerpt stops at 200 characters
    assert "(not in the listing" in text
    assert text.rstrip().endswith("RESOLVE 3 threads on " + PR)
    # every line of an excerpt sits behind the gutter: none starts like the heading or the prompt
    bad = [
        x
        for x in text.splitlines()
        if x.startswith(("== summary", "Resolve?")) and x != "== summary =="
    ]
    assert bad == [] and text.count("== summary ==") == 1
    assert sanitize_line(T1) == T1


# --- failures, partial success ------------------------------------------------------------------


def test_a_refusal_of_one_thread_does_not_stop_the_next_and_the_run_is_partial(
    cli: Run, forgectl: FakeForgectl, state_home: Path
) -> None:
    script_reads(forgectl, thread(T1), thread(T2), thread(T3))
    forgectl.script(
        resolved("resolve", T1),
        refuse("resolve", "not_resolvable", phase="read"),
        resolved("resolve", T3),
    )
    code, doc, _ = cli(*args("resolve", T1, T2, T3))
    assert code == 20
    validate("pr-comment/cli/resolve.json", doc)
    assert [(t["resolved"], t["changed"]) for t in doc["threads"]] == [
        (True, True),
        (None, None),
        (True, True),
    ]
    assert doc["threads"][1]["error"]["reason"] == "not_resolvable"
    records = audit(state_home)
    validate_audit(records)
    assert records[1]["result"] == "partial"
    assert [w["result"] for w in records[1]["writes"]] == ["done", "refused", "done"]


def test_exit_12_stops_the_run_and_later_threads_are_not_attempted(
    cli: Run, forgectl: FakeForgectl, state_home: Path
) -> None:
    script_reads(forgectl, thread(T1), thread(T2), thread(T3))
    forgectl.script(resolved("resolve", T1), fail("resolve", "network_error", "down"))
    code, doc, _ = cli(*args("resolve", T1, T2, T3))
    assert code == 12
    validate("pr-comment/cli/resolve.json", doc)
    done, failed, skipped = doc["threads"]
    assert done["changed"] is True
    assert failed["error"]["code"] == "network_error" and failed["error"]["landed"] is False
    assert skipped == {"thread_id": T3, "resolved": None, "changed": None, "error": None}
    assert len(writes_of(forgectl)) == 2
    records = audit(state_home)
    assert [w["thread_id"] for w in records[1]["writes"]] == [T1, T2]
    assert records[1]["result"] == "partial"


def test_an_unknown_outcome_is_never_retried_and_keeps_its_pair(
    cli: Run, forgectl: FakeForgectl, state_home: Path
) -> None:
    script_reads(forgectl, thread(T1), thread(T2))
    forgectl.script(fail("resolve", "outcome_unknown", "no answer"))
    code, doc, _ = cli(*args("resolve", T1, T2))
    assert code == 12
    error = doc["threads"][0]["error"]
    assert (error["code"], error["landed"], error["retry"]) == (
        "outcome_unknown",
        None,
        "same_command",
    )
    assert len(writes_of(forgectl)) == 1
    records = audit(state_home)
    assert records[1]["result"] == "outcome_unknown"
    assert records[1]["writes"][0]["lower"]["code"] == "outcome_unknown"


@pytest.mark.parametrize(
    "answer",
    [
        h.resolve_doc("resolve", T2),  # another thread
        h.resolve_doc("unresolve", T1),  # another action
        h.resolve_doc("resolve", T1, dry_run=True),  # a dry run's answer for a real call
        {**h.resolve_doc("resolve", T1), "resolved": False},  # not the state asked for
        {**h.resolve_doc("resolve", T1), "pr": "github.com/acme/other#7"},
        {**h.resolve_doc("resolve", T1), "changed": "yes"},
        {"pr": PR},
    ],
)
def test_an_answer_that_is_not_the_real_result_for_this_thread_is_a_mismatch_that_landed(
    answer: dict[str, Any],
) -> None:
    from pr_comment.hooks import ThreadTarget
    from pr_comment.threads import Item, ThreadRequest, ThreadRun, _lower
    from tests.fake_gateway import FakeGateway
    from tests.fake_gateway import ok as gateway_ok

    request = ThreadRequest("resolve", PR, ACTOR, (T1,))
    run = ThreadRun(request, show_of(), h.HEAD)
    item = Item(T1, ThreadTarget(T1), True)
    lowered = _lower(gateway_ok("resolve", answer), request, item, run.show, FakeGateway())
    assert lowered.read is None and lowered.failure is not None
    error = lowered.failure.document["error"]  # type: ignore[index]
    assert error["code"] == "lower_contract_mismatch"
    assert (error["landed"], error["retry"]) == (True, "none")
    assert lowered.lower.exit == 0  # forgectl said it succeeded: the audit never says failed


def test_a_failed_outcome_append_is_audit_failed_with_the_result_document(
    cli: Run,
    forgectl: FakeForgectl,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_reads(forgectl, thread(T1), thread(T2))
    forgectl.script(resolved("resolve", T1), resolved("resolve", T2))
    _FailAt(2).install(monkeypatch)
    code, doc, _ = cli(*args("resolve", T1, T2))
    assert code == 12
    validate("pr-comment/cli/error.json", doc)
    error = doc["error"]
    assert error["code"] == "audit_failed" and (error["landed"], error["retry"]) == (True, "none")
    assert [t["thread_id"] for t in error["details"]["outcome"]["threads"]] == [T1, T2]
    assert len(writes_of(forgectl)) == 2


def test_an_unavailable_audit_trail_sends_nothing(
    cli: Run,
    forgectl: FakeForgectl,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_reads(forgectl, thread(T1))
    _FailAt(1).install(monkeypatch)
    code, doc, _ = cli(*args("resolve", T1))
    assert code == 12 and doc["error"]["code"] == "audit_unavailable"
    assert writes_of(forgectl) == []


# --- hooks: veto and escalation -----------------------------------------------------------------


def test_a_veto_is_that_threads_error_and_the_next_thread_continues(
    cli: Run, forgectl: FakeForgectl, hooks: Hooks, state_home: Path
) -> None:
    script_reads(forgectl, thread(T1), thread(T2), thread(T3))
    forgectl.script(resolved("resolve", T2))
    # vetoes the first thread (before any record exists) and the third (after ``attempted``)
    veto = hooks.script(
        f'p=$(cat); case "$p" in *{T1}*|*{T3}*) echo "no" >&2; exit 3;; esac; exit 0'
    )
    code, doc, _ = cli(*args("resolve", T1, T2, T3, extra=("--hook", f"pre-post={veto}")))
    assert code == 11
    validate("pr-comment/cli/resolve.json", doc)
    errors = [t["error"] and t["error"]["code"] for t in doc["threads"]]
    assert errors == ["vetoed", None, "vetoed"]
    assert doc["threads"][0]["error"]["hook_stderr"] == "no\n"
    assert [c.forwarded[1] for c in writes_of(forgectl)] == [f"--thread={T2}"]
    records = audit(state_home)
    validate_audit(records)
    assert records[0]["inputs"]["threads"] == [T2, T3]  # T1 was vetoed before it was recorded
    writes = records[1]["writes"]
    assert [(w["thread_id"], w["result"], w["lower"] is None) for w in writes] == [
        (T2, "done", False),
        (T3, "vetoed", True),
    ]
    assert [e["event"] for e in log(state_home)].count("vetoed") == 2


def test_all_threads_vetoed_means_no_audit_record_and_exit_11(
    cli: Run, forgectl: FakeForgectl, hooks: Hooks, state_home: Path
) -> None:
    script_reads(forgectl, thread(T1), thread(T2))
    veto = hooks.recorder("veto", exit_code=2)
    code, doc, _ = cli(*args("resolve", T1, T2, extra=("--hook", f"pre-post={veto}")))
    assert code == 11 and writes_of(forgectl) == [] and audit(state_home) == []
    assert [t["error"]["code"] for t in doc["threads"]] == ["vetoed", "vetoed"]


def test_an_escalation_is_that_threads_error_unless_overridden_for_pr_comment(
    cli: Run, forgectl: FakeForgectl, hooks: Hooks, state_home: Path
) -> None:
    script_reads(forgectl, thread(T1))
    esc = hooks.recorder("esc", exit_code=13)
    code, doc, _ = cli(*args("resolve", T1, extra=("--hook", f"pre-post={esc}")))
    assert code == 13
    error = doc["threads"][0]["error"]
    assert error["code"] == "escalated" and error["escalated_by"] == "pr-comment"
    assert writes_of(forgectl) == [] and audit(state_home) == []


def test_the_override_for_pr_comment_lets_an_escalated_thread_through(
    cli: Run, forgectl: FakeForgectl, hooks: Hooks
) -> None:
    script_reads(forgectl, thread(T1))
    forgectl.script(resolved("resolve", T1))
    esc = hooks.recorder("esc", exit_code=13)
    code, _, _ = cli(
        *args(
            "resolve", T1, extra=("--hook", f"pre-post={esc}", "--override-escalation=pr-comment")
        )
    )
    assert code == 0 and len(writes_of(forgectl)) == 1


def test_decision_ids_a_hook_prints_join_that_threads_call_and_the_audit_outcome(
    cli: Run, forgectl: FakeForgectl, hooks: Hooks, state_home: Path
) -> None:
    script_reads(forgectl, thread(T1), thread(T2))
    forgectl.script(resolved("resolve", T1), resolved("resolve", T2))
    pre = hooks.script('cat >/dev/null; echo \'{"decision_id": "from-hook"}\'')
    code, _, _ = cli(*args("resolve", T1, T2, extra=("--hook", f"pre-post={pre}")))
    assert code == 0
    assert all("--decision-id=from-hook" in c.argv for c in writes_of(forgectl))
    records = audit(state_home)
    assert records[1]["hook_decision_id"] == ["from-hook"] and records[0]["decision_id"] == []


# --- signals -----------------------------------------------------------------------------------


def test_a_signal_during_a_write_is_outcome_unknown_and_stops_the_run(
    forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    script_reads(forgectl, thread(T1), thread(T2))
    forgectl.script(
        ok(
            "resolve",
            h.resolve_doc("resolve", T1),
            hang={"phase": "after_write_attempt"},
        )
    )
    n = forgectl.next_call_number() + 4  # show, comments, whoami, --version, the write
    proc = _start_cli(forgectl, tmp_path, state_home, *args("resolve", T1, T2))
    forgectl.wait_ready(n)
    os.kill(proc.pid, signal.SIGTERM)  # the exact pid this test started
    stdout, _ = proc.communicate(timeout=60)
    assert proc.returncode == 12
    doc = json.loads(stdout)
    validate("pr-comment/cli/resolve.json", doc)
    assert doc["threads"][0]["error"]["code"] == "outcome_unknown"
    assert doc["threads"][1] == {"thread_id": T2, "resolved": None, "changed": None, "error": None}
    records = audit(state_home)
    assert [r["result"] for r in records] == ["attempted", "outcome_unknown"]
    assert len(forgectl.writes()) == 1


def test_a_signal_before_any_write_exits_128_plus_n_and_writes_no_record(
    forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    forgectl.script(ok("show", h.show_doc(PR), hang={"phase": "before_write"}))
    n = forgectl.next_call_number()
    proc = _start_cli(forgectl, tmp_path, state_home, *args("resolve", T1))
    forgectl.wait_ready(n)
    os.kill(proc.pid, signal.SIGTERM)
    stdout, _ = proc.communicate(timeout=60)
    assert proc.returncode == 143 and stdout == b""
    assert audit(state_home) == []


# --- output ------------------------------------------------------------------------------------


def test_quiet_still_says_why_a_run_failed(
    cli: Run, forgectl: FakeForgectl, capsys: pytest.CaptureFixture[str]
) -> None:
    script_reads(forgectl, thread(T1))
    code, _, err = cli(*args("resolve", T2, extra=("--quiet",), yes=False))
    assert code == 10 and f"thread {T2}" in err and "not found" in err


def test_human_output_names_every_thread(cli: Run, forgectl: FakeForgectl) -> None:
    script_reads(forgectl, thread(T1), thread(T2, resolved=True))
    forgectl.script(resolved("resolve", T1))
    _, _, err = cli("resolve", PR, "--thread", T1, "--thread", T2, "--actor", ACTOR, "--yes")
    assert f"thread {T1}: resolved" in err and f"thread {T2}: already resolved" in err


def test_the_thread_payload_builder_refuses_a_posting_action() -> None:
    from pr_comment.hooks import ThreadTarget, payload_for_thread

    with pytest.raises(ValueError):
        payload_for_thread(
            action="review",
            pr=PR,
            repo="github.com/acme/widgets",
            actor=ACTOR,
            head_sha=h.HEAD,
            target=ThreadTarget(T1),
            hook="pre-post",
            attempt_id="a" * 16,
            write_index=0,
            decision_ids=(),
            override_escalation=(),
        )
