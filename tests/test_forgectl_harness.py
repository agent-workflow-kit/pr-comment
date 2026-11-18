"""The harness around the fake forgectl: the edge-rule assertions, the end-of-test checks and the
pinned gateway.

The assertions are for the tests of later issues, which run the real CLI against the fake and then
ask the call log whether pr-comment kept docs/interface.md's forgectl edge. Each rule is shown here
to report a violation and to pass a clean run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from pr_comment.gateway import ForgectlResult, ProcessGateway
from tests import fake_forgectl as ff
from tests.fake_gateway import FakeGateway, PinnedGateway
from tests.fake_gateway import ok as gateway_ok
from tests.forgectl_harness import (
    HEAD,
    OTHER_HEAD,
    PINNED_VERSION,
    PR,
    FakeForgectl,
    ProcessCall,
    assert_edge_rules,
    edge_violations,
    environment_violations,
    files_doc,
    form_violations,
    ok,
    pin_violations,
    plan_violations,
    post_doc,
    resolve_doc,
    show_doc,
    text_violations,
    yes_violations,
)

ROOT = Path(__file__).resolve().parent.parent
BASE = ["--json", "--timeout=60s"]


def call(*argv: str, n: int = 0, stdin: bytes | None = None, **kw: object) -> ProcessCall:
    """A logged call as the gateway would have made it."""
    fields: dict[str, object] = {
        "cwd": "/",
        "stdin_kind": "pipe" if "-" in argv else "devnull",
        "env": {"PATH": "/usr/bin:/bin", "HOME": "/h"},
    }
    fields.update(kw)
    return ProcessCall(n=n, argv=list(argv), stdin=stdin, **fields)  # type: ignore[arg-type]


COMMENT = ("comment", *BASE, PR, "--body-file", "-")
REVIEW = ("review", *BASE, PR, "--file", "-")


# --- forms ---------------------------------------------------------------------------------------


def test_a_clean_call_has_no_form_violation() -> None:
    clean = call(
        *REVIEW,
        f"--expected-head={HEAD}",
        "--expected-base=main",
        "--dedupe-key=prc:k",
        "--decision-id=d1",
        "--actor=auto:me",
        "--yes",
    )
    assert form_violations(clean) == []


@pytest.mark.parametrize(
    ("extra", "reported"),
    [
        (["--expected-head", HEAD], "must be written --expected-head=<value>"),
        (["--dedupe-key="], "empty value"),
        (["--dry-run=1"], "bare flag"),
        (["--yes=true"], "bare flag"),
        (["--allow-self"], "not a flag pr-comment forwards"),
        (["--expected-hea=" + HEAD], "not a flag pr-comment forwards"),
        (["--body-file=/tmp/x"], "must not use the = form"),
        (["--file=-"], "must not use the = form"),
        (["--verbose"], "not a flag pr-comment forwards"),
    ],
)
def test_a_flag_in_the_wrong_form_is_reported(extra: list[str], reported: str) -> None:
    problems = form_violations(call(*COMMENT, *extra))
    assert any(reported in p for p in problems), problems


def test_a_stdin_flag_without_the_dash_and_a_misplaced_json_flag_are_reported() -> None:
    assert any(
        "followed by the stdin '-'" in p
        for p in form_violations(call("comment", *BASE, PR, "--body-file", "x"))
    )
    assert any("--json must follow" in p for p in form_violations(call("comment", PR, "--json")))


# --- text only on stdin --------------------------------------------------------------------------


def test_body_text_in_argv_or_the_environment_is_reported() -> None:
    body = "the finding text that must stay on stdin"
    assert text_violations(call(*COMMENT), [body]) == []
    in_argv = call("comment", *BASE, PR, f"--dedupe-key={body}")
    assert "is in argv" in text_violations(in_argv, [body])[0]
    in_env = call(*COMMENT, env={"PATH": "/bin", "HOME": body})
    assert "in the environment (HOME)" in text_violations(in_env, [body])[0]
    assert text_violations(call(*COMMENT), [""]) == []  # an empty text matches nothing


def test_a_pr_comment_variable_a_relative_path_a_cwd_and_a_stdin_kind_are_reported() -> None:
    assert environment_violations(call(*COMMENT)) == []
    bad = call(
        *COMMENT,
        cwd="/work",
        stdin_kind="devnull",
        env={"PATH": "/usr/bin:rel:", "PR_COMMENT_TIMEOUT": "1s"},
    )
    text = "\n".join(environment_violations(bad))
    for part in ("PR_COMMENT_TIMEOUT", "not absolute", "'/work'", "stdin is devnull"):
        assert part in text
    read_on_pipe = call("show", *BASE, PR, stdin_kind="pipe")
    assert "expected devnull" in environment_violations(read_on_pipe)[0]


# --- --yes ---------------------------------------------------------------------------------------


def test_yes_on_a_pre_check_is_reported() -> None:
    pre = call(*COMMENT, "--dry-run", "--yes")
    assert yes_violations([pre], confirmed=True) == ["call 0: --yes on a pre-check (--dry-run)"]
    assert yes_violations([pre], confirmed=False) == ["call 0: --yes on a pre-check (--dry-run)"]


def test_yes_on_a_read_is_reported() -> None:
    read = call("show", *BASE, PR, "--yes")
    assert "--yes on a read (show)" in yes_violations([read], confirmed=True)[0]


def test_yes_before_confirmation_is_reported_and_after_it_is_required_on_the_write() -> None:
    write = call(*COMMENT, "--yes")
    assert "before pr-comment's confirmation" in yes_violations([write], confirmed=False)[0]
    assert yes_violations([write], confirmed=True) == []
    bare = call(*COMMENT)
    assert "a confirmed write without --yes" in yes_violations([bare], confirmed=True)[0]
    assert yes_violations([bare], confirmed=False) == []
    dry = call(*COMMENT, "--dry-run")
    assert yes_violations([dry, write], confirmed=True) == []


# --- the pins ------------------------------------------------------------------------------------


def test_the_pinned_head_and_base_must_be_passed_unchanged() -> None:
    good = call(*REVIEW, f"--expected-head={HEAD}", "--expected-base=main")
    assert pin_violations(good, head=HEAD, base="main") == []
    stale = call(*REVIEW, f"--expected-head={OTHER_HEAD}", "--expected-base=dev")
    text = "\n".join(pin_violations(stale, head=HEAD, base="main"))
    assert "--expected-head" in text and "--expected-base" in text
    assert "[]" in "\n".join(pin_violations(call(*REVIEW), head=HEAD, base=None))
    approve = call("approve", *BASE, PR, "--expected-head=" + OTHER_HEAD)
    assert pin_violations(approve, head=HEAD, base=None) != []
    assert pin_violations(call(*COMMENT), head=HEAD, base="main") == []  # comments pin nothing
    twice = call(*REVIEW, f"--expected-head={HEAD}", f"--expected-head={HEAD}")
    assert pin_violations(twice, head=HEAD, base=None) != []


# --- the plan ------------------------------------------------------------------------------------


def test_the_real_call_must_carry_the_confirmed_bytes() -> None:
    plan = b'{"event":"approve"}'
    dry = call(*REVIEW, "--dry-run", stdin=plan)
    write = call(*REVIEW, "--yes", n=1, stdin=plan)
    assert plan_violations([dry, write], plan) == []
    changed = call(*REVIEW, "--yes", n=1, stdin=plan + b" ")
    assert "differ from the confirmed" in plan_violations([dry, changed], plan)[0]
    assert (
        "dry run" in plan_violations([call(*REVIEW, "--dry-run", stdin=b"other"), write], plan)[0]
    )
    short = call(*REVIEW, "--yes", n=1, stdin=None)
    assert plan_violations([short], plan) != []
    assert plan_violations([call("show", *BASE, PR)], plan) == []


def test_the_aggregate_reports_every_class_and_skips_unmodeled_calls() -> None:
    body = "secret finding"
    bad = call("comment", *BASE, PR, f"--dedupe-key={body}", "--yes", "--dry-run")
    problems = edge_violations([bad], texts=[body], confirmed=True)
    assert len(problems) >= 2
    with pytest.raises(AssertionError, match="is in argv"):
        assert_edge_rules([bad], texts=[body])
    version = call("--version", n=1, stdin_kind="devnull")
    assert edge_violations([version]) == []


# --- end to end: the real gateway against the fake -----------------------------------------------


def gateway(fake: FakeForgectl) -> ProcessGateway:
    return ProcessGateway(environ=fake.environ(), deadline=60.0, timeout="60s")


def test_the_real_gateway_reads_the_pinned_version_from_the_fake(forgectl: FakeForgectl) -> None:
    assert gateway(forgectl).lower_version() == f"forgectl {PINNED_VERSION}"
    [version] = forgectl.calls()
    assert version.argv == ["--version"] and version.stdin_kind == "devnull"


def test_a_confirmed_post_through_the_real_gateway_keeps_every_edge_rule(
    forgectl: FakeForgectl,
) -> None:
    body = b"/lgtm\nplease look at the error path"
    forgectl.script(
        ok("comment", post_doc("comment", dry_run=True)),
        ok("comment", post_doc("comment", ids=["gh:ic:IC_1"])),
        ok("show", show_doc()),
    )
    gw = gateway(forgectl)
    dry = gw.call(
        "comment", [PR, "--dedupe-key=prc:k1", "--body-file", "-", "--dry-run"], stdin=body
    )
    posted = b" /lgtm\nplease look at the error path\n<!-- forgectl:dedupe:prc:k1 -->"
    assert json.loads(dry.stdout)["plan"]["body_bytes"] == len(posted)
    real = gw.call("comment", [PR, "--dedupe-key=prc:k1", "--body-file", "-", "--yes"], stdin=body)
    assert real.exit_code == 0 and real.write
    gw.call("show", [PR])
    calls = forgectl.calls()
    assert [c.exit_code for c in calls] == [0, 0, 0]
    assert_edge_rules(calls, texts=["lgtm", "error path"], confirmed=True, confirmed_input=body)
    assert calls[0].argv[:3] == ["comment", "--json", "--timeout=60s"]
    assert calls[2].stdin_kind == "devnull" and calls[0].stdin_kind == "pipe"


def test_a_tampered_write_is_caught_by_the_plan_check(forgectl: FakeForgectl) -> None:
    forgectl.script(
        ok("comment", post_doc("comment", dry_run=True)),
        ok("comment", post_doc("comment", ids=["gh:ic:IC_1"])),
    )
    gw = gateway(forgectl)
    gw.call("comment", [PR, "--body-file", "-", "--dry-run"], stdin=b"confirmed text")
    gw.call("comment", [PR, "--body-file", "-", "--yes"], stdin=b"confirmed text, then more")
    problems = edge_violations(forgectl.calls(), confirmed=True, confirmed_input=b"confirmed text")
    assert len(problems) == 1 and "call 1 (write)" in problems[0]


def test_a_confirmation_that_never_happened_is_caught_by_the_yes_check(
    forgectl: FakeForgectl,
) -> None:
    forgectl.script(ok("comment", post_doc("comment", ids=["gh:ic:IC_1"])))
    gateway(forgectl).call("comment", [PR, "--body-file", "-", "--yes"], stdin=b"x")
    assert edge_violations(forgectl.calls(), confirmed=False) != []


def test_a_write_without_yes_exits_thirteen_through_the_real_gateway(
    forgectl: FakeForgectl,
) -> None:
    forgectl.script(ok("resolve", resolve_doc("resolve", "gh:rt:T1")))
    result = gateway(forgectl).call("resolve", [PR, "--thread=gh:rt:T1"])
    assert result.exit_code == 13
    assert json.loads(result.stdout)["error"]["code"] == "confirmation_required"


def test_pr_comment_variables_never_reach_the_fake_through_the_real_gateway(
    forgectl: FakeForgectl,
) -> None:
    forgectl.script(ok("files", files_doc()))
    env = forgectl.environ({"PR_COMMENT_FORGECTL_BIN": "x", "PR_COMMENT_TIMEOUT": "9s"})
    ProcessGateway(environ=env, deadline=60.0).call("files", [PR])
    [seen] = forgectl.calls()
    assert not [k for k in seen.env if k.startswith("PR_COMMENT_")]
    assert environment_violations(seen) == []


# --- the end-of-test checks ----------------------------------------------------------------------


def run_inner(tmp_path: Path, test_body: str) -> subprocess.CompletedProcess[str]:
    """Run a one-test file, which uses the ``forgectl`` fixture, in its own pytest."""
    source = tmp_path / "test_inner.py"
    source.write_text(
        "from tests.forgectl_harness import *\n\n" + textwrap.dedent(test_body), encoding="utf-8"
    )
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "PYTHONPATH": str(ROOT),
    }
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.conftest",
            "-p",
            "no:cacheprovider",
            "-q",
            str(source),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=120,
        check=False,
    )


def test_a_scenario_that_violates_the_pinned_schema_fails_the_test_that_uses_it(
    tmp_path: Path,
) -> None:
    done = run_inner(
        tmp_path,
        """
        def test_it(forgectl):
            bad = {**show_doc(), "invented": 1}
            forgectl.script(ok("show", bad))
            forgectl.run(["show", "--json", PR])
        """,
    )
    assert done.returncode != 0, done.stdout
    assert "violates the pinned schema" in done.stdout and "invented" in done.stdout


def test_a_scripted_step_nobody_asked_for_fails_the_test(tmp_path: Path) -> None:
    done = run_inner(
        tmp_path,
        """
        def test_it(forgectl):
            forgectl.script(ok("show", show_doc()))
        """,
    )
    assert done.returncode != 0 and "never asked for" in done.stdout


def test_an_argv_forgectl_would_refuse_fails_the_test_that_made_it(tmp_path: Path) -> None:
    done = run_inner(
        tmp_path,
        """
        def test_it(forgectl):
            forgectl.script(ok("show", show_doc()))
            forgectl.run(["show", "--json", PR, "--dry-run"])
        """,
    )
    assert done.returncode != 0 and "forgectl would refuse this argv" in done.stdout


def test_a_clean_use_passes(tmp_path: Path) -> None:
    done = run_inner(
        tmp_path,
        """
        def test_it(forgectl):
            forgectl.script(ok("show", show_doc()))
            assert forgectl.run(["show", "--json", PR]).returncode == 0
        """,
    )
    assert done.returncode == 0, done.stdout


# --- the pinned gateway --------------------------------------------------------------------------


def test_the_pinned_gateway_reports_the_pinned_version() -> None:
    assert PinnedGateway().lower_version() == f"forgectl {PINNED_VERSION}"
    assert FakeGateway().lower_version() == "forgectl 0.1.0"  # the old default, kept for lower.py


def doc_result(command: str, document: object, *args: str) -> ForgectlResult:
    return gateway_ok(
        command, document, write=command in ff.WRITE_COMMANDS and "--dry-run" not in args
    )


@pytest.mark.parametrize(
    ("command", "args", "stdin"),
    [
        ("show", ["show-flag-it-does-not-take"], None),
        ("show", [PR, "--dry-run"], None),
        ("comment", [PR, "--body-file", "-", "--expected-head=" + HEAD], b"x"),
        ("comment", [PR, "--body-file", "-", "--dedupe-key="], b"x"),
        ("review", [PR, "--file", "-"], b'{"event":"approve"}'),  # no pinned head and base
        ("comment", [PR, "--body-file", "-"], b"  "),  # an empty body
        ("comment", ["example.test/o/r#1", "--body-file", "-"], b"x"),  # a host nobody declared
        ("reply", [PR, "--body-file", "-"], b"x"),  # no --to
    ],
)
def test_the_pinned_gateway_rejects_a_call_forgectl_would_refuse(
    command: str, args: list[str], stdin: bytes | None
) -> None:
    with pytest.raises(AssertionError, match="forgectl refuses this call"):
        PinnedGateway([gateway_ok("show", {})]).call(command, args, stdin=stdin)


def test_the_pinned_gateway_accepts_a_declared_host() -> None:
    pr = "git.example.test/o/r#1"
    gw = PinnedGateway(
        [gateway_ok("show", show_doc(pr, backend="gitlab"))], hosts={"git.example.test": "gitlab"}
    )
    assert gw.call("show", [pr]).exit_code == 0


def test_the_pinned_gateway_rejects_a_result_forgectl_could_not_print() -> None:
    with pytest.raises(AssertionError, match="could not have answered"):
        PinnedGateway([gateway_ok("show", {"pr": PR})]).call("show", [PR])
    wrong_pr = gateway_ok("files", files_doc("github.com/o/other#7"))
    with pytest.raises(AssertionError, match="not the PR asked for"):
        PinnedGateway([wrong_pr]).call("files", [PR])


def test_the_pinned_gateway_rejects_the_shapes_the_old_fake_let_through() -> None:
    not_found = ForgectlResult(
        command="show",
        write=False,
        exit_code=10,
        stdout=json.dumps(
            {"error": {"code": "not_found", "message": "m", "details": {"what": "pr"}}}
        ).encode(),
    )
    with pytest.raises(AssertionError, match="pinned error schema"):
        PinnedGateway([not_found]).call("show", [PR])
    wrong_exit = ForgectlResult(
        command="show",
        write=False,
        exit_code=12,
        stdout=json.dumps({"error": {"code": "not_found", "message": "m"}}).encode(),
    )
    with pytest.raises(AssertionError, match="exits 10"):
        PinnedGateway([wrong_exit]).call("show", [PR])


def test_the_pinned_gateway_accepts_what_forgectl_prints() -> None:
    not_found = ForgectlResult(
        command="show",
        write=False,
        exit_code=10,
        stdout=json.dumps({"error": {"code": "not_found", "message": "no such PR"}}).encode(),
    )
    posted = doc_result(
        "comment", post_doc("comment", ids=["gh:ic:IC_1"], neutralized_lines=1, markers_removed=0)
    )
    gw = PinnedGateway([gateway_ok("show", show_doc()), not_found, posted])
    assert gw.call("show", [PR]).exit_code == 0
    assert gw.call("show", [PR]).exit_code == 10
    assert gw.call("comment", [PR, "--body-file", "-", "--yes"], stdin=b"/x").exit_code == 0
    assert [c.subcommand for c in gw.calls] == ["show", "show", "comment"]


def test_the_pinned_gateway_holds_a_dry_run_refusal_to_the_plan_shape() -> None:
    envelope_on_dry_run = ForgectlResult(
        command="comment",
        write=False,
        exit_code=20,
        stdout=json.dumps(
            {"error": {"code": "refused", "reason": "stale_head", "message": "m"}}
        ).encode(),
    )
    with pytest.raises(AssertionError, match="would_refuse"):
        PinnedGateway([envelope_on_dry_run]).call(
            "approve",
            [PR, "--dry-run", f"--expected-head={HEAD}", "--expected-base=main"],
        )
