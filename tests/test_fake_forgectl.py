"""The fake forgectl: what it prints, what it refuses to print, and how it behaves under signals.

The argument grammar and the input rules are held to the real forgectl by
``test_fake_forgectl_conformance.py``. These tests cover the rest: the pin, scripted answers (every
one is checked against the pinned schemas and against the call before it is printed), confirmation,
the post input (neutralizing, the dedupe marker, the dry run's plan), and signals.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests import fake_forgectl as ff
from tests.forgectl_harness import (
    BASE_SHA,
    FAKEBIN,
    GITLAB_PR,
    HEAD,
    OTHER_HEAD,
    PIN,
    PINNED_VERSION,
    PR,
    FakeForgectl,
    comments_doc,
    compare_doc,
    diff_doc,
    fail,
    files_doc,
    ok,
    partial,
    post_doc,
    refuse,
    resolve_doc,
    show_doc,
    whoami_doc,
)

THREAD = "gh:rt:PRRT_1"
BAD_POSITION = {"index": 0, "path": "a.py", "line": 3, "side": "right", "start_line": None}
PINS = [f"--expected-head={HEAD}", "--expected-base=main"]


def argv(command: str, *rest: str, pr: str = PR) -> list[str]:
    return [command, "--json", "--timeout=60s", pr, *rest]


def out(proc: subprocess.CompletedProcess[bytes]) -> Any:
    return json.loads(proc.stdout) if proc.stdout.strip() else None


# --- the pin -------------------------------------------------------------------------------------


def test_the_pinned_version_is_in_one_file_and_is_what_the_fake_reports(
    forgectl: FakeForgectl,
) -> None:
    assert PIN["version"] == PINNED_VERSION == ff.VERSION
    proc = forgectl.run(["--version"])
    assert (proc.returncode, proc.stdout) == (0, f"forgectl {PINNED_VERSION}\n".encode())


def test_a_scenario_can_report_another_version(forgectl: FakeForgectl) -> None:
    forgectl.set_version("0.1.0")
    assert forgectl.run(["--version"]).stdout == b"forgectl 0.1.0\n"


def test_the_schemas_are_the_pinned_release_byte_for_byte() -> None:
    import hashlib

    root = Path(ff.SCHEMAS_DIR)
    rows = [
        f"{p.relative_to(root).as_posix()} {hashlib.sha256(p.read_bytes()).hexdigest()}\n"
        for p in sorted(root.rglob("*"))
        if p.is_file()
    ]
    assert len(rows) == PIN["schemas_files"]
    assert hashlib.sha256("".join(rows).encode()).hexdigest() == PIN["schemas_sha256"]
    assert PIN["tag"] == f"v{PINNED_VERSION}"


def test_every_ref_of_the_pinned_schemas_resolves() -> None:
    registry, by_id = ff._registry()  # pyright: ignore[reportPrivateUsage]
    assert "forgectl/defs.json" in by_id and "forgectl/cli/review.json" in by_id
    for command in ff.MODELED:
        assert ff.schema_errors(f"cli/{command}.json", {}) != []  # resolves, and {} is no result


# --- scripted documents --------------------------------------------------------------------------

DOCS: list[tuple[str, list[str], dict[str, Any], bytes]] = [
    ("show", argv("show"), show_doc(), b""),
    ("diff", argv("diff"), diff_doc(diff="diff --git a/a b/a\n"), b""),
    ("files", argv("files"), files_doc(), b""),
    ("comments", argv("comments"), comments_doc(), b""),
    (
        "compare",
        ["compare", "--json", PR, f"--from={HEAD}", f"--to={OTHER_HEAD}"],
        compare_doc(),
        b"",
    ),
    ("whoami", ["whoami", "--json"], whoami_doc(), b""),
    (
        "review",
        argv("review", "--file", "-", "--yes", *PINS),
        post_doc("review", ids=["gh:rc:RC_1"], review_id="gh:r:R_1"),
        b'{"event":"comment","body":"hello"}',
    ),
    (
        "comment",
        argv("comment", "--body-file", "-", "--yes"),
        post_doc("comment", ids=["gh:ic:IC_1"]),
        b"hello",
    ),
    (
        "reply",
        argv("reply", "--to=gh:rt:T_1", "--body-file", "-", "--yes"),
        post_doc("reply", ids=["gh:rc:RC_2"]),
        b"hello",
    ),
    (
        "approve",
        argv("approve", "--yes", *PINS),
        post_doc("approve", review_id="gh:r:R_2"),
        b"",
    ),
    (
        "resolve",
        argv("resolve", f"--thread={THREAD}", "--yes"),
        resolve_doc("resolve", THREAD),
        b"",
    ),
    (
        "unresolve",
        argv("unresolve", f"--thread={THREAD}", "--yes"),
        resolve_doc("unresolve", THREAD),
        b"",
    ),
]


@pytest.mark.parametrize(("command", "args", "document", "stdin"), DOCS, ids=[d[0] for d in DOCS])
def test_a_valid_scripted_document_is_printed_as_json_with_exit_zero(
    forgectl: FakeForgectl,
    command: str,
    args: list[str],
    document: dict[str, Any],
    stdin: bytes,
) -> None:
    forgectl.script(ok(command, document))
    proc = forgectl.run(args, stdin=stdin)
    assert (proc.returncode, proc.stderr) == (0, b""), proc.stderr
    printed = out(proc)
    assert ff.schema_errors(f"cli/{command}.json", printed) == []
    assert {k: printed[k] for k in document} == document  # the fake adds only derived fields
    assert proc.stdout.isascii() and proc.stdout.endswith(b"\n")


def test_a_document_that_breaks_the_pinned_schema_is_a_fake_error(forgectl: FakeForgectl) -> None:
    bad = show_doc()
    del bad["approvals_left"]
    forgectl.script(ok("show", bad))
    proc = forgectl.run(argv("show"))
    assert proc.returncode == ff.FAKE_ERROR_EXIT and proc.stdout == b""
    assert b"violates the pinned schema" in proc.stderr
    problems = forgectl.check()
    assert len(problems) == 1 and "approvals_left" in problems[0]
    forgectl.allow_fake_errors = True


def test_a_scripted_extra_field_is_a_fake_error(forgectl: FakeForgectl) -> None:
    forgectl.script(ok("files", {**files_doc(), "invented": True}))
    assert forgectl.run(argv("files")).returncode == ff.FAKE_ERROR_EXIT
    forgectl.allow_fake_errors = True


def test_a_document_for_another_pr_is_a_fake_error(forgectl: FakeForgectl) -> None:
    forgectl.script(ok("files", files_doc("github.com/acme/other#7")))
    proc = forgectl.run(argv("files"))
    assert proc.returncode == ff.FAKE_ERROR_EXIT and b"not the PR asked for" in proc.stderr
    forgectl.allow_fake_errors = True


def test_the_pr_is_compared_in_forgectls_normalized_form(forgectl: FakeForgectl) -> None:
    forgectl.script(ok("files", files_doc(PR)))
    proc = forgectl.run(argv("files", pr="https://GitHub.com:443/acme/widgets/pull/7/files"))
    assert proc.returncode == 0, proc.stderr


def test_a_step_for_another_command_is_a_fake_error(forgectl: FakeForgectl) -> None:
    forgectl.script(ok("files", files_doc()))
    proc = forgectl.run(argv("comments"))
    assert proc.returncode == ff.FAKE_ERROR_EXIT and b"is for 'files'" in proc.stderr
    forgectl.allow_fake_errors = True
    forgectl.allow_unused_steps = True


def test_a_call_with_no_step_left_is_a_fake_error(forgectl: FakeForgectl) -> None:
    proc = forgectl.run(argv("show"))
    assert proc.returncode == ff.FAKE_ERROR_EXIT and b"ran out" in proc.stderr
    forgectl.allow_fake_errors = True


def test_steps_are_taken_in_order_and_an_unused_one_fails_the_test(forgectl: FakeForgectl) -> None:
    forgectl.script(ok("files", files_doc()), ok("comments", comments_doc()))
    assert forgectl.run(argv("files")).returncode == 0
    assert forgectl.unused_steps() == 1
    assert "never asked for" in forgectl.check()[0]
    assert forgectl.run(argv("comments")).returncode == 0
    assert forgectl.check() == []


def test_unmodeled_commands_and_human_output_are_fake_errors(forgectl: FakeForgectl) -> None:
    for call in (["prs", "--json", "--author=me"], ["show", PR], ["show", PR, "--help"]):
        assert forgectl.run(call).returncode == ff.FAKE_ERROR_EXIT
    forgectl.allow_fake_errors = True


def test_a_document_with_a_marker_character_forgectl_would_have_replaced_is_a_fake_error(
    forgectl: FakeForgectl,
) -> None:
    forgectl.script(ok("diff", diff_doc(diff="a‮b")))
    assert forgectl.run(argv("diff")).returncode == ff.FAKE_ERROR_EXIT
    forgectl.allow_fake_errors = True


# --- facts the documents must agree with ---------------------------------------------------------


def _post_case(
    forgectl: FakeForgectl, document: dict[str, Any], *rest: str, stdin: bytes = b"x"
) -> int:
    forgectl.script(ok("comment", document))
    proc = forgectl.run(argv("comment", "--body-file", "-", *rest), stdin=stdin)
    if proc.returncode == ff.FAKE_ERROR_EXIT:
        forgectl.allow_fake_errors = True
    return proc.returncode


@pytest.mark.parametrize(
    "document",
    [
        post_doc("comment", ids=["gl:n:5"]),  # a GitLab id on a GitHub PR
        post_doc("comment", already_posted=True),  # a dedupe hit with no dedupe key
        post_doc("comment", follow_up_done=True, already_posted=True),
        post_doc("comment", neutralized_lines=3),  # the body has no line starting with /
        post_doc("comment", markers_removed=1),
        post_doc("comment", dry_run=True),  # the call is not a dry run
        post_doc("comment", warnings=["Read-Back"]),
    ],
    ids=[
        "id-of-another-host",
        "already-posted-without-key",
        "follow-up-on-github",
        "neutralized-count",
        "markers-count",
        "dry-run-flag",
        "warning-shape",
    ],
)
def test_a_post_result_that_contradicts_the_call_is_a_fake_error(
    forgectl: FakeForgectl, document: dict[str, Any]
) -> None:
    assert _post_case(forgectl, document, "--yes") == ff.FAKE_ERROR_EXIT


def test_a_dedupe_hit_needs_the_key_and_is_exit_zero_without_confirmation(
    forgectl: FakeForgectl,
) -> None:
    doc = post_doc("comment", already_posted=True, ids=["gh:ic:IC_9"])
    assert _post_case(forgectl, doc, "--dedupe-key=k1") == 0  # no --yes: nothing to write


@pytest.mark.parametrize(
    ("to", "threaded", "accepted"),
    [
        ("gh:rt:T_1", True, True),
        ("gh:rc:C_1", True, True),
        ("gh:ic:I_1", False, True),
        ("gh:r:R_1", False, True),
        ("gh:ic:I_1", True, False),
        ("gh:rt:T_1", False, False),
    ],
)
def test_a_github_reply_is_threaded_only_under_a_review_comment_or_thread(
    forgectl: FakeForgectl, to: str, threaded: bool, accepted: bool
) -> None:
    forgectl.script(ok("reply", post_doc("reply", ids=["gh:rc:C_2"], threaded=threaded)))
    proc = forgectl.run(argv("reply", f"--to={to}", "--body-file", "-", "--yes"), stdin=b"x")
    assert (proc.returncode == 0) is accepted, proc.stderr
    if not accepted:
        forgectl.allow_fake_errors = True


def test_a_gitlab_review_has_no_review_id(forgectl: FakeForgectl) -> None:
    forgectl.script(ok("review", post_doc("review", GITLAB_PR, review_id="gl:n:1", ids=["gl:n:2"])))
    proc = forgectl.run(
        argv("review", "--file", "-", "--yes", *PINS, pr=GITLAB_PR),
        stdin=b'{"event":"comment","body":"x"}',
    )
    assert proc.returncode == ff.FAKE_ERROR_EXIT
    forgectl.allow_fake_errors = True


def test_a_review_result_at_another_head_than_the_pinned_one_is_a_fake_error(
    forgectl: FakeForgectl,
) -> None:
    forgectl.script(ok("review", post_doc("review", head_sha=OTHER_HEAD)))
    proc = forgectl.run(
        argv("review", "--file", "-", "--yes", *PINS), stdin=b'{"event":"comment","body":"x"}'
    )
    assert proc.returncode == ff.FAKE_ERROR_EXIT and b"stale_head" in proc.stderr
    forgectl.allow_fake_errors = True


def test_head_verified_is_null_unless_the_event_approves(forgectl: FakeForgectl) -> None:
    forgectl.script(ok("review", post_doc("review", head_verified=True)))
    proc = forgectl.run(
        argv("review", "--file", "-", "--yes", *PINS), stdin=b'{"event":"comment","body":"x"}'
    )
    assert proc.returncode == ff.FAKE_ERROR_EXIT
    forgectl.allow_fake_errors = True


def test_a_gitlab_follow_up_after_a_dedupe_hit_is_allowed(forgectl: FakeForgectl) -> None:
    doc = post_doc(
        "review",
        GITLAB_PR,
        already_posted=True,
        follow_up_done=True,
        ids=["gl:n:4"],
        head_verified=False,
    )
    forgectl.script(ok("review", doc))
    proc = forgectl.run(
        argv("review", "--file", "-", "--yes", "--dedupe-key=k", *PINS, pr=GITLAB_PR),
        stdin=b'{"event":"approve","body":"x"}',
    )
    assert proc.returncode == 0, proc.stderr


def test_a_bodyless_approve_that_is_already_in_effect_needs_no_key(forgectl: FakeForgectl) -> None:
    forgectl.script(ok("approve", post_doc("approve", already_posted=True, head_verified=True)))
    proc = forgectl.run(argv("approve", *PINS))  # no --yes: nothing to write
    assert proc.returncode == 0, proc.stderr


def test_whoami_lists_the_hosts_forgectl_lists(forgectl: FakeForgectl) -> None:
    forgectl.script(ok("whoami", whoami_doc(["github.com"])))
    assert forgectl.run(["whoami", "--json"]).returncode == ff.FAKE_ERROR_EXIT
    forgectl.script(ok("whoami", whoami_doc(["github.com"])))
    assert forgectl.run(["whoami", "--json", "--host=github.com"]).returncode == 0
    forgectl.allow_fake_errors = True
    forgectl.allow_unused_steps = True


def test_whoami_partial_results_print_the_result_and_exit_twelve(forgectl: FakeForgectl) -> None:
    errors = [{"host": "gitlab.com", "code": "auth_required", "message": "no token"}]
    forgectl.script(partial("whoami", whoami_doc(errors=errors), "gitlab.com: auth_required"))
    proc = forgectl.run(["whoami", "--json"])
    assert proc.returncode == 12
    printed = out(proc)
    assert "error" not in printed and printed["errors"] == errors
    assert proc.stderr == b"forgectl: error: partial_results: gitlab.com: auth_required\n"


def test_whoami_exit_zero_lists_no_errors_and_partial_lists_some(forgectl: FakeForgectl) -> None:
    err = {"host": "gitlab.com", "code": "auth_required", "message": "m"}
    forgectl.script(ok("whoami", whoami_doc(errors=[err])), partial("whoami", whoami_doc()))
    assert forgectl.run(["whoami", "--json"]).returncode == ff.FAKE_ERROR_EXIT
    assert forgectl.run(["whoami", "--json"]).returncode == ff.FAKE_ERROR_EXIT
    forgectl.allow_fake_errors = True


def test_a_declared_host_is_known_and_has_its_backend(forgectl: FakeForgectl) -> None:
    forgectl.declare_host("git.example.test", "gitlab")
    pr = "git.example.test/acme/widgets#7"
    forgectl.script(ok("show", show_doc(pr, backend="gitlab")))
    assert forgectl.run(argv("show", pr=pr)).returncode == 0
    forgectl.script(ok("show", show_doc(pr, backend="github")))
    assert forgectl.run(argv("show", pr=pr)).returncode == ff.FAKE_ERROR_EXIT
    forgectl.allow_fake_errors = True


# --- scripted errors -----------------------------------------------------------------------------


def test_each_error_is_printed_as_forgectls_envelope_with_its_exit_code(
    forgectl: FakeForgectl,
) -> None:
    cases = [
        (argv("show"), fail("show", "not_found", "no such PR"), 10, b""),
        (argv("files"), fail("files", "rate_limited", "slow", retry_after=30), 12, b""),
        (
            argv("approve", "--yes", *PINS),
            refuse(
                "approve", "stale_head", "moved", details={"head_sha": OTHER_HEAD}, phase="write"
            ),
            20,
            b"",
        ),
        (
            argv("approve", "--yes", *PINS),
            refuse("approve", "self_approval", "no", details={"basis": "configured_me"}),
            20,
            b"",
        ),
        (
            argv("resolve", f"--thread={THREAD}", "--yes"),
            refuse("resolve", "not_resolvable", "not a thread"),
            20,
            b"",
        ),
        (
            argv("approve", "--yes", *PINS),
            fail(
                "approve",
                "outcome_partial",
                "the note landed",
                details={"landed": {"ids": ["gh:ic:X"], "thread_ids": []}, "failed": "approve"},
            ),
            12,
            b"",
        ),
    ]
    for call, step, code, stdin in cases:
        forgectl.script(step)
        proc = forgectl.run(call, stdin=stdin)
        envelope = out(proc)
        assert proc.returncode == code, (call, proc.stderr)
        assert (
            list(envelope) == ["error"]
            and envelope["error"]["code"] == step["outcome"]["error"]["code"]
        )
        assert ff.schema_errors("cli/error.json", envelope) == []
        line = proc.stderr.decode().strip()
        assert line.startswith("forgectl: error: ") and "\n" not in line


@pytest.mark.parametrize(
    ("command", "step", "why"),
    [
        ("show", fail("show", "not_found", "m", details={"what": "pr"}), "pinned error schema"),
        ("show", fail("show", "not_found", "m", reason="stale_head"), "reason is only for"),
        ("show", refuse("show", "stale_head", "m", details={"head_sha": HEAD}), "cannot refuse"),
        ("show", fail("show", "outcome_unknown", "m"), "cannot fail with code"),
        ("show", fail("show", "vetoed", "m", hook="pre-post", hook_stderr=""), "cannot fail"),
        ("show", fail("show", "usage", "m"), "checks itself"),
        ("show", fail("show", "confirmation_required", "m"), "made by the fake"),
        ("show", refuse("show", "unknown_host", "m"), "closed host list"),
        ("show", fail("show", "rate_limited", "m", hook="x", hook_stderr=""), "only on vetoed"),
        ("show", fail("show", "not_found", "m", retry_after=5), "retry_after is only"),
        ("approve", fail("approve", "outcome_partial", "m"), "needs details"),
        ("approve", fail("approve", "host_rejected", "m"), "needs details"),
        ("approve", refuse("approve", "stale_base", "m"), "needs details"),
        (
            "approve",
            refuse("approve", "drafts_present", "m", details={"count": 1}),
            "cannot refuse",
        ),
        ("approve", fail("approve", "vetoed", "m"), "hook and hook_stderr"),
        ("approve", fail("approve", "escalated", "m", hook="h", hook_stderr=""), "escalated_hooks"),
        (
            "comment",
            fail("comment", "invalid_position", "m", details={"invalid": [BAD_POSITION]}),
            "cannot fail",
        ),
        ("comment", fail("comment", "not_found", "bad\x1b[31m"), "marker"),
        (
            "comment",
            fail("comment", "vetoed", "m", hook="pre-post", hook_stderr="x" * 4097),
            "4096",
        ),
    ],
)
def test_an_error_forgectl_could_not_print_is_a_fake_error(
    forgectl: FakeForgectl, command: str, step: dict[str, Any], why: str
) -> None:
    forgectl.script(step)
    base = {
        "show": argv("show"),
        "approve": argv("approve", "--yes", *PINS),
        "comment": argv("comment", "--body-file", "-", "--yes"),
    }[command]
    proc = forgectl.run(base, stdin=b"x")
    assert proc.returncode == ff.FAKE_ERROR_EXIT and why in proc.stderr.decode(), proc.stderr
    forgectl.allow_fake_errors = True


def test_a_veto_and_an_escalation_carry_their_hook_fields(forgectl: FakeForgectl) -> None:
    forgectl.script(
        fail(
            "comment",
            "vetoed",
            "a pre-post hook refused",
            hook="pre-post",
            hook_stderr="",
            decision_ids=["d1"],
        ),
        fail(
            "comment",
            "escalated",
            "needs a human",
            hook="pre-post",
            hook_stderr="why",
            escalated_hooks=[{"hook": "pre-post", "index": 0}],
        ),
    )
    call = argv("comment", "--body-file", "-", "--yes")
    veto = forgectl.run(call, stdin=b"x")
    assert veto.returncode == 11 and out(veto)["error"]["hook_stderr"] == ""
    escalated = forgectl.run(call, stdin=b"x")
    assert escalated.returncode == 13 and out(escalated)["error"]["escalated_hooks"]


def test_a_refusal_on_a_dry_run_is_the_plan_with_would_refuse_and_exit_twenty(
    forgectl: FakeForgectl,
) -> None:
    forgectl.script(
        refuse("approve", "not_open", "closed", details={"state": "closed"}),
        refuse(
            "approve",
            "stale_head",
            "moved",
            details={"head_sha": OTHER_HEAD},
            fields={"head_sha": OTHER_HEAD},
        ),
        refuse("resolve", "not_resolvable", "no"),
    )
    proc = forgectl.run(argv("approve", "--body-file", "-", "--dry-run", *PINS), stdin=b"/x\nplain")
    printed = out(proc)
    assert proc.returncode == 20 and "error" not in printed
    assert printed["would_refuse"] == {
        "reason": "not_open",
        "message": "closed",
        "details": {"state": "closed"},
    }
    assert printed["dry_run"] is True and printed["neutralized_lines"] == 1
    assert printed["plan"]["body_bytes"] == len(b" /x\nplain")
    assert ff.schema_errors("cli/approve.json", printed) == []
    assert proc.stderr.startswith(b"forgectl: error: refused (not_open): closed")

    second = forgectl.run(argv("approve", "--dry-run", *PINS))
    assert second.returncode == 20 and out(second)["head_sha"] == OTHER_HEAD
    third = forgectl.run(argv("resolve", f"--thread={THREAD}", "--dry-run"))
    result = out(third)
    assert third.returncode == 20 and result["resolved"] is None and result["changed"] is False


def test_a_refusal_without_a_dry_run_is_the_error_envelope(forgectl: FakeForgectl) -> None:
    forgectl.script(refuse("approve", "not_open", "closed", details={"state": "closed"}))
    proc = forgectl.run(argv("approve", "--yes", *PINS))
    assert proc.returncode == 20
    assert out(proc) == {
        "error": {
            "code": "refused",
            "message": "closed",
            "reason": "not_open",
            "details": {"state": "closed"},
        }
    }


def test_a_hook_or_write_outcome_cannot_happen_in_a_dry_run(forgectl: FakeForgectl) -> None:
    forgectl.script(fail("comment", "vetoed", "m", hook="pre-post", hook_stderr=""))
    proc = forgectl.run(argv("comment", "--body-file", "-", "--dry-run"), stdin=b"x")
    assert proc.returncode == ff.FAKE_ERROR_EXIT and b"dry run" in proc.stderr
    forgectl.allow_fake_errors = True


# --- confirmation --------------------------------------------------------------------------------


def test_a_write_without_yes_exits_thirteen_and_a_dry_run_needs_none(
    forgectl: FakeForgectl,
) -> None:
    forgectl.script(
        ok("comment", post_doc("comment", ids=["gh:ic:IC_1"])),
        ok("comment", post_doc("comment", dry_run=True)),
    )
    forgectl.allow_confirmation_blocked = True
    blocked = forgectl.run(argv("comment", "--body-file", "-"), stdin=b"hello")
    assert blocked.returncode == 13
    assert out(blocked)["error"]["code"] == "confirmation_required"
    assert blocked.stderr.startswith(b"forgectl: error: confirmation_required: ")
    assert forgectl.calls()[0].confirmation_blocked
    dry = forgectl.run(argv("comment", "--body-file", "-", "--dry-run"), stdin=b"hello")
    assert dry.returncode == 0 and out(dry)["dry_run"] is True and out(dry)["would_refuse"] is None


def test_a_thread_already_in_the_requested_state_needs_no_confirmation(
    forgectl: FakeForgectl,
) -> None:
    forgectl.script(
        ok("resolve", resolve_doc("resolve", THREAD, changed=False)),
        ok("resolve", resolve_doc("resolve", THREAD)),
    )
    forgectl.allow_confirmation_blocked = True
    assert forgectl.run(argv("resolve", f"--thread={THREAD}")).returncode == 0
    assert forgectl.run(argv("resolve", f"--thread={THREAD}")).returncode == 13


def test_a_refusal_before_confirmation_wins_over_confirmation(forgectl: FakeForgectl) -> None:
    forgectl.script(fail("comment", "not_found", "no such PR"))
    proc = forgectl.run(argv("comment", "--body-file", "-"), stdin=b"x")
    assert proc.returncode == 10


def test_a_veto_without_yes_is_confirmation_required_because_hooks_follow_confirmation(
    forgectl: FakeForgectl,
) -> None:
    forgectl.allow_confirmation_blocked = True
    forgectl.script(fail("comment", "vetoed", "m", hook="pre-post", hook_stderr=""))
    proc = forgectl.run(argv("comment", "--body-file", "-"), stdin=b"x")
    assert proc.returncode == 13 and out(proc)["error"]["code"] == "confirmation_required"


# --- when in the post order an error happens ------------------------------------------------------

REVIEW_CALL = argv("review", "--file", "-", *PINS)
REVIEW_STDIN = b'{"event":"comment","body":"hello"}'
GITLAB_REVIEW = argv("review", "--file", "-", *PINS, pr=GITLAB_PR)
COMMENT_CALL = argv("comment", "--body-file", "-")
LANDED = {"landed": {"ids": ["gh:ic:X"], "thread_ids": []}, "failed": "approve"}

WRITE_TIME = [
    (REVIEW_CALL, REVIEW_STDIN, refuse("review", "pending_review_exists")),
    (
        REVIEW_CALL,
        REVIEW_STDIN,
        refuse("review", "stale_head", details={"head_sha": OTHER_HEAD}, phase="write"),
    ),
    (
        GITLAB_REVIEW,
        REVIEW_STDIN,
        refuse("review", "drafts_present", details={"count": 1}, phase="write"),
    ),
    (COMMENT_CALL, b"x", fail("comment", "host_rejected", details={"status": 422})),
    (COMMENT_CALL, b"x", fail("comment", "permission_denied")),
    (COMMENT_CALL, b"x", fail("comment", "rate_limited", retry_after=3)),
    (COMMENT_CALL, b"x", fail("comment", "auth_required")),
    (COMMENT_CALL, b"x", fail("comment", "network_error")),
    (COMMENT_CALL, b"x", fail("comment", "upstream_error")),
    (COMMENT_CALL, b"x", fail("comment", "audit_unavailable")),
    (COMMENT_CALL, b"x", fail("comment", "quick_action_ran", details={"ids": ["gh:ic:X"]})),
    (REVIEW_CALL, REVIEW_STDIN, fail("review", "outcome_partial", details=LANDED)),
    (REVIEW_CALL, REVIEW_STDIN, fail("review", "approved_stale_head", details={"ids": ["gh:r:R"]})),
    (argv("resolve", f"--thread={THREAD}"), b"", fail("resolve", "network_error")),
]


@pytest.mark.parametrize(
    ("call", "stdin", "step"),
    WRITE_TIME,
    ids=[
        f"{s['outcome']['error'].get('reason') or s['outcome']['error']['code']}-{i}"
        for i, (_, _, s) in enumerate(WRITE_TIME)
    ],
)
def test_an_error_that_happens_after_confirmation_is_exit_thirteen_without_yes(
    forgectl: FakeForgectl, call: list[str], stdin: bytes, step: dict[str, Any]
) -> None:
    forgectl.allow_confirmation_blocked = True
    forgectl.script(step)
    proc = forgectl.run(call, stdin=stdin)
    assert proc.returncode == 13 and out(proc)["error"]["code"] == "confirmation_required"
    assert forgectl.calls()[0].confirmation_blocked
    # with --yes the scripted error is what the run ends in
    forgectl.script(step)
    forgectl.allow_unused_steps = False
    yes = forgectl.run([*call, "--yes"], stdin=stdin)
    assert yes.returncode == ff.EXIT_BY_CODE[step["outcome"]["error"]["code"]], yes.stderr


@pytest.mark.parametrize(
    ("call", "stdin", "step"),
    [
        (REVIEW_CALL, REVIEW_STDIN, refuse("review", "not_open", details={"state": "closed"})),
        (
            REVIEW_CALL,
            REVIEW_STDIN,
            refuse("review", "stale_head", details={"head_sha": OTHER_HEAD}, phase="read"),
        ),
        (
            REVIEW_CALL,
            REVIEW_STDIN,
            refuse("review", "self_approval", details={"basis": "authenticated"}),
        ),
        (REVIEW_CALL, REVIEW_STDIN, refuse("review", "stale_base", details={"base_ref": "dev"})),
        (
            REVIEW_CALL,
            REVIEW_STDIN,
            fail("review", "invalid_position", details={"invalid": [BAD_POSITION]}),
        ),
        (
            GITLAB_REVIEW,
            REVIEW_STDIN,
            refuse("review", "drafts_present", details={"count": 2}, phase="read"),
        ),
        (
            REVIEW_CALL,
            REVIEW_STDIN,
            fail("review", "outcome_partial", details=LANDED, phase="read"),
        ),
        (COMMENT_CALL, b"x", fail("comment", "not_found")),
        (COMMENT_CALL, b"x", fail("comment", "rate_limited", phase="read")),
    ],
)
def test_an_error_before_confirmation_is_the_exit_of_that_error_without_yes(
    forgectl: FakeForgectl, call: list[str], stdin: bytes, step: dict[str, Any]
) -> None:
    forgectl.script(step)
    proc = forgectl.run(call, stdin=stdin)
    assert proc.returncode == ff.EXIT_BY_CODE[step["outcome"]["error"]["code"]], proc.stderr
    assert not forgectl.calls()[0].confirmation_blocked


@pytest.mark.parametrize(
    "step",
    [
        refuse("review", "pending_review_exists"),
        fail("review", "host_rejected", details={"status": 422}, phase="write"),
        fail("review", "not_found", phase="write"),  # a read-only code has no write phase
        fail("review", "outcome_unknown", phase="read"),  # a write-only code has no read phase
    ],
)
def test_a_write_time_error_cannot_happen_in_a_dry_run(
    forgectl: FakeForgectl, step: dict[str, Any]
) -> None:
    forgectl.script(step)
    proc = forgectl.run([*REVIEW_CALL, "--dry-run"], stdin=REVIEW_STDIN)
    assert proc.returncode == ff.FAKE_ERROR_EXIT, proc.stderr
    forgectl.allow_fake_errors = True


def test_a_host_failure_in_a_dry_run_is_a_read_failure(forgectl: FakeForgectl) -> None:
    forgectl.script(fail("review", "host_rejected", details={"status": 422}))
    proc = forgectl.run([*REVIEW_CALL, "--dry-run"], stdin=REVIEW_STDIN)
    assert proc.returncode == 12


def test_what_a_blocked_write_would_have_said_or_waited_for_is_not_printed(
    forgectl: FakeForgectl,
) -> None:
    forgectl.allow_confirmation_blocked = True
    step = fail(
        "comment",
        "host_rejected",
        details={"status": 422},
        stderr_lines=["forgectl: warning: slow host"],
        delay=5,
    )
    forgectl.script(step)
    started = time.monotonic()
    proc = forgectl.run(COMMENT_CALL, stdin=b"x")
    assert proc.returncode == 13 and b"warning" not in proc.stderr
    assert time.monotonic() - started < 4


@pytest.mark.parametrize("phase", ["after_write_attempt", "after_outcome"])
def test_a_hang_after_the_write_cannot_follow_an_outcome_before_confirmation(
    forgectl: FakeForgectl, phase: str
) -> None:
    forgectl.script(fail("comment", "not_found", hang={"phase": phase, "seconds": 0.05}))
    proc = forgectl.run([*COMMENT_CALL, "--yes"], stdin=b"x")
    assert proc.returncode == ff.FAKE_ERROR_EXIT and b"before confirmation" in proc.stderr
    forgectl.script(
        ok(
            "comment",
            post_doc("comment", already_posted=True, ids=["gh:ic:IC_9"]),
            hang={"phase": phase, "seconds": 0.05},
        )
    )
    proc = forgectl.run([*COMMENT_CALL, "--dedupe-key=k", "--yes"], stdin=b"x")
    assert proc.returncode == ff.FAKE_ERROR_EXIT and b"before confirmation" in proc.stderr
    forgectl.allow_fake_errors = True


def test_the_error_phases_are_the_post_orders(forgectl: FakeForgectl) -> None:
    assert ff.error_phases("show", "rate_limited", None) == {"read"}
    assert ff.error_phases("comment", "vetoed", None) == {"write"}
    assert ff.error_phases("review", "refused", "pending_review_exists") == {"write"}
    assert ff.error_phases("review", "refused", "not_open") == {"read"}
    assert ff.error_phases("review", "refused", "stale_head") == {"read", "write"}
    assert ff.error_phases("review", "host_rejected", None) == {"read", "write"}


# --- a resolve of something that is not a thread -------------------------------------------------


@pytest.mark.parametrize("flags", [["--yes"], []])
@pytest.mark.parametrize("thread", ["gh:ic:abc", "gh:rc:abc", "gh:r:abc", "gl:n:12"])
def test_resolving_what_is_not_a_review_thread_is_refused_before_any_host_read(
    forgectl: FakeForgectl, thread: str, flags: list[str]
) -> None:
    proc = forgectl.run(argv("unresolve", f"--thread={thread}", *flags))
    assert proc.returncode == 20
    error = out(proc)["error"]
    assert (error["reason"], error["message"]) == (
        "not_resolvable",
        "only review threads (gh:rt: and gl:d: ids) can be unresolved",
    )
    assert forgectl.calls()[0].step is None  # no scripted host answer was taken


def test_a_dry_run_of_resolving_what_is_not_a_thread_says_would_refuse(
    forgectl: FakeForgectl,
) -> None:
    proc = forgectl.run(argv("resolve", "--thread=gh:ic:abc", "--dry-run"))
    printed = out(proc)
    assert proc.returncode == 20 and printed["resolved"] is None and printed["changed"] is False
    assert printed["would_refuse"]["reason"] == "not_resolvable"
    assert ff.schema_errors("cli/resolve.json", printed) == []


def test_a_review_thread_and_a_gitlab_discussion_are_resolvable(forgectl: FakeForgectl) -> None:
    gl = "gl:d:" + "0f" * 20
    forgectl.script(
        ok("resolve", resolve_doc("resolve", THREAD)),
        ok("unresolve", resolve_doc("unresolve", gl, GITLAB_PR)),
    )
    assert forgectl.run(argv("resolve", f"--thread={THREAD}", "--yes")).returncode == 0
    call = argv("unresolve", f"--thread={gl}", "--yes", pr=GITLAB_PR)
    assert forgectl.run(call).returncode == 0


# --- the errors a command can end in --------------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "stdin", "step", "why"),
    [
        (["whoami", "--json"], b"", fail("whoami", "auth_required"), "cannot fail with code"),
        (["whoami", "--json"], b"", fail("whoami", "not_found"), "cannot fail with code"),
        (COMMENT_CALL, b"x", fail("comment", "outcome_partial", details=LANDED), "cannot fail"),
        (
            argv("resolve", f"--thread={THREAD}"),
            b"",
            fail("resolve", "quick_action_ran", details={"ids": []}),
            "cannot fail",
        ),
        (
            argv("resolve", f"--thread={THREAD}"),
            b"",
            fail("resolve", "approved_stale_head", details={"ids": []}),
            "cannot fail",
        ),
        (
            argv("approve", *PINS),
            b"",
            fail("approve", "invalid_position", details={"invalid": [BAD_POSITION]}),
            "cannot fail",
        ),
        (
            REVIEW_CALL,
            REVIEW_STDIN,
            refuse("review", "drafts_present", details={"count": 1}),
            "only a gitlab refusal",
        ),
        (
            GITLAB_REVIEW,
            REVIEW_STDIN,
            refuse("review", "pending_review_exists"),
            "only a github refusal",
        ),
        (
            argv("approve", *PINS),
            b"",
            refuse("approve", "drafts_present", details={"count": 1}),
            "cannot refuse",
        ),
    ],
)
def test_an_error_a_command_cannot_end_in_is_a_fake_error(
    forgectl: FakeForgectl, call: list[str], stdin: bytes, step: dict[str, Any], why: str
) -> None:
    forgectl.script(step)
    proc = forgectl.run([*call, "--yes"] if call[0] != "whoami" else call, stdin=stdin)
    assert proc.returncode == ff.FAKE_ERROR_EXIT and why in proc.stderr.decode(), proc.stderr
    forgectl.allow_fake_errors = True


def test_the_errors_a_command_can_end_in_are_accepted(forgectl: FakeForgectl) -> None:
    forgectl.script(
        refuse("review", "drafts_present", details={"count": 1}, phase="write"),
        refuse("review", "pending_review_exists"),
        fail("approve", "outcome_partial", details=LANDED),
        fail("review", "invalid_position", details={"invalid": [BAD_POSITION]}),
        fail("comment", "quick_action_ran", details={"ids": ["gh:ic:X"]}),
    )
    assert forgectl.run([*GITLAB_REVIEW, "--yes"], stdin=REVIEW_STDIN).returncode == 20
    assert forgectl.run([*REVIEW_CALL, "--yes"], stdin=REVIEW_STDIN).returncode == 20
    assert forgectl.run(argv("approve", "--yes", *PINS)).returncode == 12
    assert forgectl.run([*REVIEW_CALL, "--yes"], stdin=REVIEW_STDIN).returncode == 12
    assert forgectl.run([*COMMENT_CALL, "--yes"], stdin=b"x").returncode == 12


# --- what a result reports ------------------------------------------------------------------------


def _bad_resolve(**changes: Any) -> dict[str, Any]:
    return {**resolve_doc("resolve", THREAD), **changes}


@pytest.mark.parametrize(
    ("command", "call", "stdin", "document"),
    [
        ("comment", COMMENT_CALL, b"x", post_doc("comment", ids=["gh:ic:A", "gh:ic:B"])),
        ("comment", COMMENT_CALL, b"x", post_doc("comment")),
        (
            "comment",
            COMMENT_CALL,
            b"x",
            post_doc("comment", ids=["gh:ic:A"], thread_ids=["gh:rt:T"]),
        ),
        ("comment", COMMENT_CALL, b"x", post_doc("comment", ids=["gh:ic:A"], review_id="gh:r:R")),
        (
            "reply",
            argv("reply", "--to=gh:rt:T_1", "--body-file", "-"),
            b"x",
            post_doc("reply", ids=["gh:rc:A"], review_id="gh:r:R"),
        ),
        (
            "reply",
            argv("reply", "--to=gh:rt:T_1", "--body-file", "-"),
            b"x",
            post_doc("reply", ids=["gh:rc:A", "gh:rc:B"]),
        ),
        (
            "comment",
            [*COMMENT_CALL, "--dry-run"],
            b"x",
            post_doc("comment", dry_run=True, ids=["gh:ic:A"]),
        ),
        (
            "review",
            [*REVIEW_CALL, "--dry-run"],
            REVIEW_STDIN,
            post_doc("review", dry_run=True, review_id="gh:r:R"),
        ),
        ("resolve", argv("resolve", f"--thread={THREAD}"), b"", _bad_resolve(resolved=False)),
        ("resolve", argv("resolve", f"--thread={THREAD}"), b"", _bad_resolve(resolved=None)),
        (
            "resolve",
            argv("resolve", f"--thread={THREAD}"),
            b"",
            _bad_resolve(
                would_refuse={"reason": "not_resolvable", "message": "m", "details": None}
            ),
        ),
        ("show", argv("show"), b"", show_doc(number=8)),
        ("show", argv("show"), b"", show_doc(repo="github.com/acme/other")),
        ("files", argv("files"), b"", {**files_doc(), "changed_files": 2}),
        (
            "files",
            argv("files"),
            b"",
            {**files_doc(), "files_complete": True, "changed_files": None},
        ),
    ],
    ids=[
        "comment-two-ids",
        "comment-no-id",
        "comment-thread",
        "comment-review-id",
        "reply-review-id",
        "reply-two-ids",
        "dry-comment-ids",
        "dry-review-id",
        "resolve-wrong-state",
        "resolve-null-state",
        "resolve-would-refuse-on-success",
        "show-number",
        "show-repo",
        "files-count",
        "files-complete-unknown-count",
    ],
)
def test_a_result_that_breaks_what_the_command_reports_is_a_fake_error(
    forgectl: FakeForgectl, command: str, call: list[str], stdin: bytes, document: dict[str, Any]
) -> None:
    forgectl.script(ok(command, document))
    if command in ff.WRITE_COMMANDS and "--dry-run" not in call:
        call = [*call, "--yes"]
    proc = forgectl.run(call, stdin=stdin)
    assert proc.returncode == ff.FAKE_ERROR_EXIT, proc.stderr
    forgectl.allow_fake_errors = True


def test_a_gitlab_files_list_may_be_complete_with_an_unknown_count(forgectl: FakeForgectl) -> None:
    pr = GITLAB_PR
    forgectl.script(ok("files", {**files_doc(pr), "changed_files": None}))
    assert forgectl.run(argv("files", pr=pr)).returncode == 0


def test_a_dry_run_refusal_after_the_pr_read_reports_the_head_it_read(
    forgectl: FakeForgectl,
) -> None:
    forgectl.script(
        refuse("approve", "not_open", details={"state": "closed"}),
        refuse("approve", "stale_head", details={"head_sha": OTHER_HEAD}),
        refuse("approve", "self_approval", details={"basis": "authenticated"}),
        refuse("approve", "stale_base", details={"base_ref": "dev"}),
        refuse("approve", "not_open", details={"state": "closed"}, fields={"head_sha": OTHER_HEAD}),
    )
    heads = [out(forgectl.run(argv("approve", "--dry-run", *PINS)))["head_sha"] for _ in range(5)]
    assert heads == [HEAD, OTHER_HEAD, HEAD, HEAD, OTHER_HEAD]


def test_a_gitlab_dry_run_drafts_refusal_reports_the_pinned_head(forgectl: FakeForgectl) -> None:
    forgectl.script(refuse("review", "drafts_present", details={"count": 1}))
    proc = forgectl.run([*GITLAB_REVIEW, "--dry-run"], stdin=REVIEW_STDIN)
    assert out(proc)["head_sha"] == HEAD


# --- the post input, as forgectl reads it --------------------------------------------------------


def _plan(forgectl: FakeForgectl, command: str, call: list[str], stdin: bytes, **doc: Any) -> Any:
    forgectl.script(ok(command, post_doc(command, dry_run=True, **doc)))
    proc = forgectl.run(call, stdin=stdin)
    assert proc.returncode == 0, proc.stderr
    return out(proc)


def test_the_plan_of_a_comment_is_computed_from_the_neutralized_body(
    forgectl: FakeForgectl,
) -> None:
    body = "ok\r\n/approve /lgtm\x85text"
    printed = _plan(
        forgectl, "comment", argv("comment", "--body-file", "-", "--dry-run"), body.encode()
    )
    posted = "ok\n /approve\n /lgtm\ntext"
    assert printed["neutralized_lines"] == 2 and printed["markers_removed"] == 0
    assert printed["plan"] == {
        "event": None,
        "body_sha256": hashlib.sha256(posted.encode()).hexdigest(),
        "body_bytes": len(posted.encode()),
        "inline_comments": 0,
    }


def test_the_dedupe_marker_is_the_last_line_and_marker_shaped_text_is_defused(
    forgectl: FakeForgectl,
) -> None:
    body = "see forgectl:dedupe:old and FORGECTL:DEDUPE:x"
    printed = _plan(
        forgectl,
        "comment",
        argv("comment", "--body-file", "-", "--dry-run", "--dedupe-key=k:1"),
        body.encode(),
    )
    posted = "see forgectl:dedupe-old and FORGECTL:DEDUPE-x\n<!-- forgectl:dedupe:k:1 -->"
    assert printed["markers_removed"] == 2
    assert printed["plan"]["body_sha256"] == hashlib.sha256(posted.encode()).hexdigest()


def test_the_review_plan_matches_forgectls_own_golden_vector(forgectl: FakeForgectl) -> None:
    """The input and the plan of forgectl's golden ``review/github-dry-run`` (its test suite, at the
    pinned tag): the canonical JSON that ``body_sha256`` hashes is the same one."""
    review = {
        "event": "comment",
        "body": "words",
        "comments": [{"path": "src/app.py", "line": 2, "body": "use c here"}],
    }
    printed = _plan(
        forgectl,
        "review",
        argv("review", "--file", "-", "--dry-run", *PINS),
        json.dumps(review).encode(),
    )
    assert printed["plan"] == {
        "event": "comment",
        "body_sha256": "6e5365aecbfe8172f7ba3a79e284814c417ed7ab4bd1ef140b0d1cf0eaca0f11",
        "body_bytes": 15,
        "inline_comments": 1,
    }


def test_the_approve_plan_has_no_event_and_the_review_plan_has_one(forgectl: FakeForgectl) -> None:
    approve = _plan(
        forgectl, "approve", argv("approve", "--dry-run", "--body-file", "-", *PINS), b"lgtm"
    )
    assert approve["plan"]["event"] is None and approve["plan"]["body_bytes"] == 4
    review = _plan(
        forgectl,
        "review",
        argv("review", "--file", "-", "--dry-run", *PINS),
        b'{"event":"approve"}',
    )
    assert review["plan"] == {
        "event": "approve",
        "body_sha256": None,
        "body_bytes": 0,
        "inline_comments": 0,
    }


def test_a_gitlab_review_puts_the_marker_on_the_last_inline_comment_when_there_is_no_body(
    forgectl: FakeForgectl,
) -> None:
    doc = {
        "event": "comment",
        "comments": [
            {"path": "a", "line": 1, "body": "one"},
            {"path": "a", "line": 2, "body": "two"},
        ],
    }
    forgectl.script(ok("review", post_doc("review", GITLAB_PR, dry_run=True)))
    proc = forgectl.run(
        argv("review", "--file", "-", "--dry-run", "--dedupe-key=k", *PINS, pr=GITLAB_PR),
        stdin=json.dumps(doc).encode(),
    )
    canonical = json.dumps(
        {
            "body": None,
            "comments": [
                {"path": "a", "line": 1, "side": "right", "start_line": None, "body": "one"},
                {
                    "path": "a",
                    "line": 2,
                    "side": "right",
                    "start_line": None,
                    "body": "two\n<!-- forgectl:dedupe:k -->",
                },
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert out(proc)["plan"]["body_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()


def test_a_plan_that_contradicts_the_input_is_a_fake_error(forgectl: FakeForgectl) -> None:
    forgectl.script(ok("comment", post_doc("comment", dry_run=True, plan={"body_bytes": 999})))
    proc = forgectl.run(argv("comment", "--body-file", "-", "--dry-run"), stdin=b"hello")
    assert proc.returncode == ff.FAKE_ERROR_EXIT and b"plan.body_bytes" in proc.stderr
    forgectl.allow_fake_errors = True


def test_forgectl_reads_no_more_than_the_cap_plus_one_byte_of_a_body(
    forgectl: FakeForgectl,
) -> None:
    forgectl.allow_usage_errors = True
    proc = forgectl.run(argv("comment", "--body-file", "-", "--yes"), stdin=b"x" * (262144 + 5000))
    assert out(proc)["error"]["code"] == "body_too_large"
    assert len(forgectl.calls()[0].stdin or b"") == 262145


def test_the_log_records_argv_stdin_environment_and_stdin_kind(forgectl: FakeForgectl) -> None:
    forgectl.script(ok("comment", post_doc("comment", ids=["gh:ic:IC_1"])), ok("show", show_doc()))
    forgectl.run(
        argv("comment", "--body-file", "-", "--yes", "--actor=auto:me"),
        stdin=b"body \xe2\x9c\x93",
        env={"PR_COMMENT_TIMEOUT": "9s"},
    )
    forgectl.run(argv("show"))
    write, read = forgectl.calls()
    assert write.argv[0] == "comment" and write.stdin == b"body \xe2\x9c\x93"
    assert write.stdin_kind == "pipe" and write.cwd == "/" and write.exit_code == 0
    assert write.env["PR_COMMENT_TIMEOUT"] == "9s" and write.env["PATH"] == str(FAKEBIN)
    assert write.forwarded == [
        "github.com/acme/widgets#7",
        "--body-file",
        "-",
        "--yes",
        "--actor=auto:me",
    ]
    assert read.stdin_kind == "pipe"  # the harness pipes empty input; the gateway uses /dev/null
    assert write.is_write and not read.is_write


def test_an_existing_config_file_is_accepted_and_a_missing_one_is_config_invalid(
    forgectl: FakeForgectl, tmp_path: Path
) -> None:
    config = tmp_path / "forgectl.yaml"
    config.write_text("{}\n")
    forgectl.script(ok("files", files_doc()))
    assert forgectl.run(argv("files", f"--config={config}")).returncode == 0
    forgectl.allow_usage_errors = True
    missing = forgectl.run(argv("files", f"--config={tmp_path / 'nope.yaml'}"))
    assert missing.returncode == 2 and out(missing)["error"]["code"] == "config_invalid"
    assert out(missing)["error"]["details"] == {"key": str(tmp_path / "nope.yaml")}


# --- signals -------------------------------------------------------------------------------------


def _hang(
    forgectl: FakeForgectl, call: list[str], step: dict[str, Any], stdin: bytes = b"x"
) -> subprocess.Popen[bytes]:
    forgectl.script(step)
    n = forgectl.next_call_number()
    proc = forgectl.start(call, stdin=stdin)
    forgectl.wait_ready(n)
    return proc


@pytest.mark.parametrize(
    ("signum", "exit_code"),
    [(signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129)],
)
def test_a_signal_before_a_write_exits_128_plus_n_with_no_output(
    forgectl: FakeForgectl, signum: int, exit_code: int
) -> None:
    step = fail("comment", "not_found", "m", hang={"phase": "before_write"})
    proc = _hang(forgectl, argv("comment", "--body-file", "-", "--yes"), step)
    os.kill(proc.pid, signum)
    stdout, stderr = proc.communicate(timeout=30)
    assert (proc.returncode, stdout, stderr) == (exit_code, b"", b"")
    call = forgectl.calls()[0]
    assert (call.kind, call.exit_code, call.hung_phase) == ("signal", exit_code, "before_write")


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_a_signal_after_the_write_was_attempted_is_outcome_unknown_exit_twelve(
    forgectl: FakeForgectl, signum: int
) -> None:
    step = ok(
        "comment",
        post_doc("comment", ids=["gh:ic:IC_1"]),
        hang={"phase": "after_write_attempt"},
    )
    proc = _hang(forgectl, argv("comment", "--body-file", "-", "--yes"), step)
    os.kill(proc.pid, signum)
    stdout, _ = proc.communicate(timeout=30)
    assert proc.returncode == 12
    envelope = json.loads(stdout)
    assert envelope["error"]["code"] == "outcome_unknown"
    assert ff.schema_errors("cli/error.json", envelope) == []


def test_a_signal_after_the_outcome_was_recorded_exits_with_that_outcome(
    forgectl: FakeForgectl,
) -> None:
    step = ok("comment", post_doc("comment", ids=["gh:ic:IC_1"]), hang={"phase": "after_outcome"})
    proc = _hang(forgectl, argv("comment", "--body-file", "-", "--yes"), step)
    os.kill(proc.pid, signal.SIGTERM)
    stdout, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0 and json.loads(stdout)["ids"] == ["gh:ic:IC_1"]


def test_a_hang_that_reaches_no_write_cannot_claim_one(forgectl: FakeForgectl) -> None:
    for call, step in [
        (argv("show"), ok("show", show_doc(), hang={"phase": "after_write_attempt"})),
        (
            argv("comment", "--body-file", "-", "--dry-run"),
            ok("comment", post_doc("comment", dry_run=True), hang={"phase": "after_write_attempt"}),
        ),
        (
            argv("comment", "--body-file", "-", "--yes"),
            ok(
                "comment",
                post_doc("comment"),
                hang={"phase": "after_write_attempt", "term_delay": 5},
            ),
        ),
    ]:
        forgectl.script(step)
        proc = forgectl.run(call, stdin=b"x")
        assert proc.returncode == ff.FAKE_ERROR_EXIT, proc.stderr
    forgectl.allow_fake_errors = True


def test_the_hang_runs_out_and_the_scripted_answer_is_printed(forgectl: FakeForgectl) -> None:
    forgectl.script(ok("show", show_doc(), hang={"seconds": 0.05}))
    proc = forgectl.run(argv("show"))
    assert proc.returncode == 0 and out(proc)["pr"] == PR


def test_the_fake_ends_with_its_process_recorded_so_the_tracker_can_end_it(
    forgectl: FakeForgectl,
) -> None:
    from tests.fakes import recorded

    forgectl.script(ok("show", show_doc(), hang={"seconds": 30}))
    proc = forgectl.start(argv("show"))
    forgectl.wait_ready(0)
    assert {pid for pid, _, _ in recorded(forgectl.directory)} == {proc.pid}
    os.kill(proc.pid, signal.SIGTERM)
    proc.communicate(timeout=30)
    assert proc.returncode == 143


# --- the base of the process tests ---------------------------------------------------------------


def test_the_fake_runs_when_the_child_path_holds_nothing_but_its_directory(
    forgectl: FakeForgectl,
) -> None:
    env = forgectl.environ()
    assert env["PATH"] == str(FAKEBIN)
    proc = subprocess.run(
        ["forgectl", "--version"],
        capture_output=True,
        env=env,
        cwd="/",
        timeout=30,
        check=False,
        executable=str(FAKEBIN / "forgectl"),
    )
    assert proc.stdout == f"forgectl {PINNED_VERSION}\n".encode()


def test_the_launcher_needs_the_interpreter_variable() -> None:
    env = {"FAKE_FORGECTL_DIR": "/nonexistent", "PATH": str(FAKEBIN)}
    proc = subprocess.run(
        [str(FAKEBIN / "forgectl"), "--version"], capture_output=True, env=env, check=False
    )
    assert proc.returncode != 0 and b"FAKE_FORGECTL_PYTHON" in proc.stderr
    assert sys.executable  # the harness sets it from the interpreter that runs the tests


def test_the_launcher_needs_the_scenario_directory_variable() -> None:
    env = {"FAKE_FORGECTL_PYTHON": sys.executable, "PATH": str(FAKEBIN)}
    proc = subprocess.run(
        [str(FAKEBIN / "forgectl"), "--version"], capture_output=True, env=env, check=False
    )
    assert proc.returncode != 0 and b"FAKE_FORGECTL_DIR" in proc.stderr
    assert b"Traceback" not in proc.stderr and b"KeyError" not in proc.stderr


def test_base_sha_is_not_a_head() -> None:
    assert BASE_SHA != HEAD != OTHER_HEAD


def test_a_github_files_list_may_say_incomplete_when_it_is_as_long_as_changed_files(
    forgectl: FakeForgectl,
) -> None:
    doc = files_doc()
    assert doc["changed_files"] == len(doc["files"])
    forgectl.script(ok("files", {**doc, "files_complete": False}))
    assert forgectl.run(argv("files")).returncode == 0


def test_a_dry_run_refusal_reports_the_scripted_head_over_the_pinned_one(
    forgectl: FakeForgectl,
) -> None:
    forgectl.script(
        refuse("approve", "not_open", details={"state": "closed"}, fields={"head_sha": OTHER_HEAD})
    )
    proc = forgectl.run(argv("approve", "--dry-run", *PINS))
    assert out(proc)["head_sha"] == OTHER_HEAD


@pytest.mark.parametrize(
    "step",
    [
        refuse("review", "drafts_present", details={"count": 1}),
        refuse("review", "stale_head", details={"head_sha": OTHER_HEAD}),
    ],
)
def test_a_write_script_for_a_both_sides_refusal_needs_an_explicit_phase(
    forgectl: FakeForgectl, step: dict[str, Any]
) -> None:
    forgectl.script(step)
    proc = forgectl.run([*GITLAB_REVIEW, "--yes"], stdin=REVIEW_STDIN)
    assert proc.returncode == ff.FAKE_ERROR_EXIT and b"explicit phase" in proc.stderr
    forgectl.allow_fake_errors = True


def test_a_both_sides_refusal_with_a_phase_works(forgectl: FakeForgectl) -> None:
    forgectl.script(refuse("review", "drafts_present", details={"count": 1}, phase="write"))
    proc = forgectl.run([*GITLAB_REVIEW, "--yes"], stdin=REVIEW_STDIN)
    assert proc.returncode == 20, proc.stderr
